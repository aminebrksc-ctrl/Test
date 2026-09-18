import base64
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app import Bot, ServiceError, handler_for, split_text


class BotTests(unittest.TestCase):
    def setUp(self):
        self.bot = Bot({"ALLOWED_USER_IDS":"42,43", "OPENAI_API_KEY":"test-placeholder",
                        "WEBHOOK_SECRET":"a"*32, "DAILY_IMAGE_LIMIT":"1"}, db_path=":memory:")
        self.sent, self.calls, self.photos = [], [], []
        self.bot.telegram = lambda method, payload: self.sent.append((method,payload))
        self.bot.photo = lambda user, data: self.photos.append((user,data))
        def fake_ai(path, payload):
            self.calls.append((path,payload))
            if path == "images/generations":
                return {"data":[{"b64_json":base64.b64encode(b"fake-image").decode()}]}
            return {"output":[{"type":"message", "content":[{"type":"output_text","text":"سلام"}]}]}
        self.bot.ai = fake_ai

    def tearDown(self):
        self.bot.db.close()

    def update(self, uid=1, user=42, text="سلام", chat_type="private"):
        return {"update_id":uid, "message":{"from":{"id":user}, "chat":{"id":user,"type":chat_type},"text":text}}

    def test_no_unauthorized_spending(self):
        self.assertEqual(self.bot.enqueue(self.update(user=9)),200)
        self.assertFalse(self.bot.work_once())
        self.bot.process(9,"/image cat")
        self.assertEqual(self.calls,[])

    def test_groups_ignored(self):
        self.bot.enqueue(self.update(chat_type="group"))
        self.assertFalse(self.bot.work_once())

    def test_duplicate_update_billed_once_and_prompt_scrubbed(self):
        self.bot.enqueue(self.update())
        self.bot.enqueue(self.update())
        self.assertTrue(self.bot.work_once())
        self.assertFalse(self.bot.work_once())
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.bot.db.execute("SELECT text FROM jobs").fetchone()[0],"")

    def test_history_isolation_and_reset(self):
        self.bot.process(42,"من سارا هستم")
        self.bot.process(43,"سلام")
        self.assertEqual(len(self.calls[-1][1]["input"]),1)
        self.bot.process(42,"نام من چیست؟")
        self.assertEqual(len(self.calls[-1][1]["input"]),3)
        self.bot.process(42,"/new")
        self.assertEqual(self.bot.session(42),([],"chat"))
        self.assertEqual(len(self.bot.session(43)[0]),2)

    def test_image_mode_and_quota(self):
        self.bot.process(42,"🎨 ساخت عکس")
        self.bot.process(42,"یک گربه")
        self.assertEqual(self.photos,[(42,b"fake-image")])
        self.assertEqual(self.calls[0][0],"images/generations")
        self.bot.process(42,"/image another cat")
        self.assertEqual(len(self.photos),1)

    def test_id_works_before_allowlist_setup(self):
        self.bot.process(9,"/id")
        self.assertIn("9",self.sent[-1][1]["text"])
        self.assertEqual(self.calls,[])

    def test_error_does_not_leak_or_auto_retry(self):
        def fail(*args):
            raise ServiceError(401)
        self.bot.ai = fail
        self.bot.enqueue(self.update())
        self.bot.work_once()
        self.assertFalse(self.bot.work_once())
        self.assertNotIn("test-placeholder",json.dumps(self.sent))
        self.assertEqual(self.bot.db.execute("SELECT state FROM jobs").fetchone()[0],"failed")

    def test_unicode_message_limits(self):
        original = "🌙سلام"*3000
        chunks = list(split_text(original))
        self.assertEqual("".join(chunks),original)
        self.assertTrue(all(len(c.encode("utf-16-le"))//2 <= 3500 for c in chunks))

    def test_webhook_rejects_forgery_accepts_secret(self):
        server = ThreadingHTTPServer(("127.0.0.1",0),handler_for(self.bot))
        thread = threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}/telegram"
        try:
            with self.assertRaises(HTTPError) as exc:
                urlopen(Request(url,data=json.dumps(self.update()).encode()),timeout=3)
            self.assertEqual(exc.exception.code,403)
            headers={"X-Telegram-Bot-Api-Secret-Token":"a"*32}
            with urlopen(Request(url,data=json.dumps(self.update()).encode(),headers=headers),timeout=3) as response:
                self.assertEqual(response.status,200)
            self.assertTrue(self.bot.work_once())
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
