"""Private Starmah Telegram bot. Python 3.12+, no third-party dependencies."""
import base64
import hmac
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

LOG = logging.getLogger("starmah")
IMAGE_BUTTON = "🎨 ساخت عکس"
NEW_BUTTON = "✨ گفت‌وگوی تازه"
HELP = ("سلام! من دستیار استارماه هستم 🌙\nپیامت را بنویس تا صحبت کنیم.\n"
        "برای تولید عکس دکمهٔ 🎨 ساخت عکس را بزن و سپس توضیح عکس را بفرست.\n"
        "/image توضیح عکس — تولید تصویر\n/new — پاک‌کردن حافظهٔ گفت‌وگو\n"
        "/id — شناسهٔ تلگرام شما\n/privacy — حریم خصوصی")
PRIVACY = ("متن پیام‌ها برای پاسخ‌گویی به OpenAI ارسال می‌شود. آخرین پیام‌های گفت‌وگو "
           "روی سرور بات نگهداری می‌شوند. با /new آن‌ها را از حافظهٔ بات پاک کن. "
           "این کار پیام‌های تلگرام یا داده‌های نزد ارائه‌دهندهٔ API را حذف نمی‌کند. "
           "رمز و اطلاعات محرمانه نفرست. تصاویر ساخته‌شده در تاریخچهٔ بات نگهداری نمی‌شوند.")


class ServiceError(Exception):
    def __init__(self, status=0):
        self.status = status
        super().__init__("Upstream request failed")


def request_json(url, payload, headers=None, timeout=180):
    req = Request(url, data=json.dumps(payload).encode(),
                  headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urlopen(req, timeout=timeout) as response:
            return json.load(response)
    except HTTPError as exc:
        # Never log URLs, response bodies or exceptions: they can contain tokens/prompts.
        raise ServiceError(exc.code) from None
    except Exception:
        raise ServiceError() from None


def split_text(text, limit=3500):
    # Telegram measures message limits in UTF-16 code units.
    part, units = [], 0
    for char in text:
        width = 2 if ord(char) > 0xFFFF else 1
        if units + width > limit:
            yield "".join(part)
            part, units = [], 0
        part.append(char)
        units += width
    if part:
        yield "".join(part)


class Bot:
    def __init__(self, env=None, db_path=None):
        env = os.environ if env is None else env
        self.token = env.get("TELEGRAM_BOT_TOKEN", "")
        self.key = env.get("OPENAI_API_KEY", "")
        self.secret = env.get("WEBHOOK_SECRET", "")
        self.base = env.get("BASE_URL", env.get("RENDER_EXTERNAL_URL", "")).rstrip("/")
        self.allowed = {int(x.strip()) for x in env.get("ALLOWED_USER_IDS", "").split(",") if x.strip()}
        self.text_model = env.get("TEXT_MODEL", "gpt-4.1-mini")
        self.image_model = env.get("IMAGE_MODEL", "gpt-image-1.5")
        self.limits = {"text": int(env.get("DAILY_TEXT_LIMIT", "100")),
                       "image": int(env.get("DAILY_IMAGE_LIMIT", "5"))}
        self.db = sqlite3.connect(db_path or env.get("DB_PATH", "state.sqlite3"), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.ready = False
        with self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY, user INTEGER, text TEXT,
                  state TEXT DEFAULT 'queued', created REAL);
                CREATE TABLE IF NOT EXISTS sessions(user INTEGER PRIMARY KEY, history TEXT, mode TEXT);
                CREATE TABLE IF NOT EXISTS usage(user INTEGER, day TEXT, kind TEXT, count INTEGER,
                  PRIMARY KEY(user,day,kind));
            """)
            # Interrupted jobs are not regenerated automatically (avoids duplicate API charges).
            self.db.execute("UPDATE jobs SET state='interrupted', text='' WHERE state='working'")

    def telegram(self, method, payload):
        result = request_json(f"https://api.telegram.org/bot{self.token}/{method}", payload, timeout=30)
        if not result.get("ok"):
            raise ServiceError(result.get("error_code", 0))
        return result.get("result")

    def say(self, user, text):
        for chunk in split_text(text):
            self.telegram("sendMessage", {"chat_id": user, "text": chunk,
                "reply_markup": {"keyboard": [[IMAGE_BUTTON, NEW_BUTTON]], "resize_keyboard": True}})

    def photo(self, user, data):
        boundary = uuid.uuid4().hex
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{user}\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"starmah.png\"\r\n"
                "Content-Type: image/png\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
        req = Request(f"https://api.telegram.org/bot{self.token}/sendPhoto", data=body,
                      headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urlopen(req, timeout=60) as response:
                if not json.load(response).get("ok"):
                    raise ServiceError()
        except Exception:
            raise ServiceError() from None

    def ai(self, path, payload):
        # No automatic retry on potentially billable requests.
        return request_json("https://api.openai.com/v1/" + path, payload,
                            {"Authorization": "Bearer " + self.key}, timeout=240)

    def enqueue(self, update):
        if not isinstance(update, dict):
            return 400
        msg = update.get("message")
        uid = update.get("update_id")
        if not isinstance(msg, dict) or type(uid) is not int:
            return 200
        user = msg.get("from", {}).get("id")
        chat = msg.get("chat", {})
        if type(user) is not int or chat.get("type") != "private" or chat.get("id") != user:
            return 200
        text = msg.get("text", "")
        if not isinstance(text, str) or len(text) > 8000:
            return 200
        command = text.split(maxsplit=1)[0].split("@")[0] if text else ""
        if user not in self.allowed and command not in ("/id", "/start", "/privacy"):
            return 200
        with self.lock, self.db:
            if self.db.execute("SELECT 1 FROM jobs WHERE id=?", (uid,)).fetchone():
                return 200
            if self.db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','working')").fetchone()[0] >= 100:
                return 503
            # Silently reject flood messages before they can consume paid API resources.
            last = self.db.execute("SELECT max(created) FROM jobs WHERE user=?", (user,)).fetchone()[0]
            if last and time.time() - last < 1:
                return 200
            self.db.execute("INSERT INTO jobs(id,user,text,created) VALUES (?,?,?,?)", (uid,user,text,time.time()))
        return 200

    def quota(self, user, kind):
        day = datetime.now(timezone.utc).date().isoformat()
        with self.lock, self.db:
            row = self.db.execute("SELECT count FROM usage WHERE user=? AND day=? AND kind=?", (user,day,kind)).fetchone()
            if row and row[0] >= self.limits[kind]:
                return False
            self.db.execute("INSERT INTO usage VALUES (?,?,?,1) ON CONFLICT(user,day,kind) DO UPDATE SET count=count+1", (user,day,kind))
        return True

    def session(self, user, history=None, mode=None):
        with self.lock, self.db:
            row = self.db.execute("SELECT history,mode FROM sessions WHERE user=?", (user,)).fetchone()
            old_history, old_mode = (json.loads(row[0]), row[1]) if row else ([], "chat")
            if history is None and mode is None:
                return old_history, old_mode
            self.db.execute("INSERT OR REPLACE INTO sessions VALUES (?,?,?)",
                (user, json.dumps(old_history if history is None else history, ensure_ascii=False), old_mode if mode is None else mode))

    def process(self, user, text):
        command = text.split(maxsplit=1)[0].split("@")[0] if text else ""
        if command == "/id":
            return self.say(user, f"شناسهٔ تلگرام شما: {user}")
        if command == "/privacy":
            return self.say(user, PRIVACY)
        if user not in self.allowed:
            return self.say(user, "این بات شخصی است. برای نمایش شناسهٔ خود /id را بفرست؛ مدیر باید دسترسی را فعال کند.")
        if command in ("/start", "/help"):
            return self.say(user, HELP)
        if command in ("/new", "/cancel") or text == NEW_BUTTON:
            self.session(user, history=[], mode="chat")
            return self.say(user, "گفت‌وگوی تازه شروع شد. چه کمکی می‌خواهی؟")
        history, mode = self.session(user)
        if text == IMAGE_BUTTON or text.strip() == "/image":
            self.session(user, mode="image")
            return self.say(user, "عکسی را که می‌خواهی با جزئیات توصیف کن. برای انصراف /cancel را بفرست.")
        if not text:
            return self.say(user, "فعلاً پیام متنی و تولید عکس از توضیح متنی را پشتیبانی می‌کنم.")
        if text.startswith("/") and command != "/image":
            return self.say(user, HELP)
        if not self.key:
            return self.say(user, "اتصال هوش مصنوعی هنوز توسط مدیر فعال نشده است.")
        image = mode == "image" or command == "/image" or text.startswith("عکس:")
        kind = "image" if image else "text"
        if not self.quota(user, kind):
            return self.say(user, "سهمیهٔ امروز این بخش تمام شده است؛ فردا دوباره امتحان کن.")
        if image:
            prompt = text.split(maxsplit=1)[1] if command == "/image" and " " in text else text.removeprefix("عکس:").strip()
            if not prompt:
                return self.say(user, "توضیح تصویر را هم بنویس.")
            self.say(user, "در حال ساخت عکس هستم؛ ممکن است چند دقیقه طول بکشد 🎨")
            result = self.ai("images/generations", {"model": self.image_model, "prompt": prompt,
                           "size": "1024x1024", "quality": "low", "n": 1})
            self.photo(user, base64.b64decode(result["data"][0]["b64_json"], validate=True))
            self.session(user, mode="chat")
        else:
            self.telegram("sendChatAction", {"chat_id":user, "action":"typing"})
            messages = history + [{"role":"user", "content":text}]
            result = self.ai("responses", {"model":self.text_model, "input":messages,
                "instructions": "You are Starmah, a helpful personal assistant. Reply in Persian unless asked otherwise. "
                "Use clear plain text. You have no live browsing. For image creation tell the user to use the 🎨 ساخت عکس button or /image followed by a description. Do not claim to have generated an image in text chat.",
                "max_output_tokens":1600, "store":False})
            answer = "\n".join(c.get("text", "") for item in result.get("output", [])
                      for c in item.get("content", []) if c.get("type") == "output_text")
            if not answer:
                answer = "پاسخی دریافت نشد؛ لطفاً درخواستت را به شکل دیگری بنویس."
            self.say(user, answer)
            self.session(user, history=(messages + [{"role":"assistant", "content":answer}])[-12:])

    def work_once(self):
        with self.lock, self.db:
            row = self.db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY id LIMIT 1").fetchone()
            if not row:
                return False
            self.db.execute("UPDATE jobs SET state='working' WHERE id=?", (row["id"],))
        state = "done"
        try:
            self.process(row["user"], row["text"])
        except Exception as exc:
            state = "failed"
            LOG.warning("Request failed: %s, status=%s", type(exc).__name__, getattr(exc,"status",0))
            try:
                self.say(row["user"], "انجام درخواست ممکن نشد؛ ممکن است سرویس در دسترس نباشد، اعتبار کافی نباشد یا درخواست پذیرفته نشده باشد. مدیر اتصال و اعتبار را بررسی کند.")
            except Exception:
                pass
        finally:
            with self.lock, self.db:
                self.db.execute("UPDATE jobs SET state=?,text='' WHERE id=?", (state,row["id"]))
                self.db.execute("DELETE FROM jobs WHERE state NOT IN ('queued','working') AND created<?", (time.time()-172800,))
                self.db.execute("DELETE FROM usage WHERE day < date('now','-7 days')")
        return True

    def run_worker(self):
        while not self.stop.is_set():
            if not self.work_once():
                self.stop.wait(0.3)

    def connect(self):
        if not (self.token and self.base and re.fullmatch(r"[A-Za-z0-9_-]{32,256}", self.secret)):
            LOG.warning("Setup pending: set TELEGRAM_BOT_TOKEN, WEBHOOK_SECRET and public BASE_URL")
            return
        if not self.base.startswith("https://"):
            raise ValueError("BASE_URL must use HTTPS")
        me = self.telegram("getMe", {})
        if me.get("username", "").lower() != "starmah_bot":
            raise ValueError("Telegram token belongs to a different bot")
        self.telegram("setWebhook", {"url":self.base+"/telegram", "secret_token":self.secret,
                      "allowed_updates":["message"], "max_connections":2, "drop_pending_updates":False})
        self.telegram("setMyCommands", {"commands":[
            {"command":"start","description":"شروع و راهنما"},
            {"command":"image","description":"تولید عکس"},
            {"command":"new","description":"گفت‌وگوی تازه"},
            {"command":"id","description":"شناسهٔ من"},
            {"command":"privacy","description":"حریم خصوصی"}]})
        self.ready = True


def handler_for(bot):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, value):
            data = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path not in ("/", "/healthz"):
                return self.reply(404, {"error":"not_found"})
            self.reply(200, {"service":"starmah-bot", "status":"running",
                              "telegram_connected":bot.ready, "ai_configured":bool(bot.key)})

        def do_POST(self):
            if self.path != "/telegram":
                return self.reply(404, {"error":"not_found"})
            supplied = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if not bot.secret or not hmac.compare_digest(supplied.encode(), bot.secret.encode()):
                return self.reply(403, {"error":"forbidden"})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 65536:
                    return self.reply(413, {"error":"body_size"})
                self.connection.settimeout(10)
                update = json.loads(self.rfile.read(size))
                status = bot.enqueue(update)
            except (ValueError, TypeError, TimeoutError):
                return self.reply(400, {"error":"invalid_body"})
            self.reply(status, {"ok":status == 200})
    return Handler


def main():
    logging.basicConfig(level=logging.INFO)
    bot = Bot()
    server = ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", "10000"))), handler_for(bot))
    threading.Thread(target=bot.run_worker, daemon=True).start()
    def setup():
        try:
            bot.connect()
        except Exception as exc:
            LOG.error("Telegram setup failed: %s (check token and HTTPS URL)", type(exc).__name__)
    threading.Thread(target=setup, daemon=True).start()
    try:
        server.serve_forever()
    finally:
        bot.stop.set()
        server.server_close()


if __name__ == "__main__":
    main()
