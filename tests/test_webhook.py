"""Siri/Shortcuts LAN capture endpoint — zero API cost (brain.respond is
monkeypatched). Run: ./venv/bin/python -m unittest discover tests -v"""
import json
import pathlib
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import memory  # noqa: E402

TEST_DB = pathlib.Path("/tmp/penny_webhook_test.db")


def fresh_db():
    memory.DB_PATH = TEST_DB
    if TEST_DB.exists():
        TEST_DB.unlink()
    memory.init()


class TestWebhookCapture(unittest.TestCase):
    SECRET = "test-secret-value"

    @classmethod
    def setUpClass(cls):
        import webhook
        cls.webhook = webhook
        webhook.WEBHOOK_SECRET = cls.SECRET  # set on the module directly — robust
        cls.orig_respond = webhook.brain.respond

        def fake_respond(text, *a, **k):
            # mirrors respond()'s own logging so tests can assert on it without
            # a real API call — webhook.py itself does no logging of its own,
            # by design, so it inherits whatever brain.respond() does
            memory.log_msg("user", text)
            reply = f"echo: {text}"
            memory.log_msg("assistant", reply)
            return reply

        webhook.brain.respond = fake_respond
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), webhook._Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.webhook.brain.respond = cls.orig_respond

    def setUp(self):
        fresh_db()  # no owner_chat_id set -> _reply_and_send skips the real Telegram call

    def _post(self, body, secret="__use_default__", path="/capture"):
        url = f"http://127.0.0.1:{self.port}{path}"
        headers = {"Content-Type": "application/json"}
        if secret != "__omit__":
            headers["X-Aissistant-Secret"] = self.SECRET if secret == "__use_default__" else secret
        data = json.dumps(body).encode() if not isinstance(body, (bytes, str)) else (
            body.encode() if isinstance(body, str) else body
        )
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            resp = urllib.request.urlopen(req)
            return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_valid_request_returns_reply_from_brain(self):
        status, payload = self._post({"text": "remind me to call mom"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["reply"], "echo: remind me to call mom")

    def test_wrong_secret_is_rejected(self):
        status, payload = self._post({"text": "hi"}, secret="wrong")
        self.assertEqual(status, 401)
        self.assertFalse(payload["ok"])

    def test_missing_secret_header_is_rejected(self):
        status, payload = self._post({"text": "hi"}, secret="__omit__")
        self.assertEqual(status, 401)

    def test_empty_text_is_rejected(self):
        status, payload = self._post({"text": "   "})
        self.assertEqual(status, 400)

    def test_missing_text_key_is_rejected(self):
        status, payload = self._post({})
        self.assertEqual(status, 400)

    def test_unknown_path_is_404(self):
        status, payload = self._post({"text": "hi"}, path="/nope")
        self.assertEqual(status, 404)

    def test_oversized_body_is_rejected(self):
        big = {"text": "x" * 5000}
        status, payload = self._post(big)
        self.assertEqual(status, 413)

    def test_user_and_assistant_turn_logged_like_any_other_message(self):
        self._post({"text": "buy milk"})
        rows = memory.recent_msgs(5)
        self.assertTrue(any(r["role"] == "user" and r["content"] == "buy milk" for r in rows))
        self.assertTrue(any(r["role"] == "assistant" and "echo: buy milk" in r["content"] for r in rows))

    def test_capture_succeeds_even_if_telegram_notify_fails(self):
        # simulate a Telegram delivery hiccup deterministically (no real
        # network call) — the capture itself (brain.respond) already
        # succeeded and must still be reported as success
        memory.set_setting("owner_chat_id", "12345")

        class ExplodingBot:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                raise RuntimeError("simulated Telegram outage")

            async def __aexit__(self, *a):
                pass

        orig_bot = self.webhook.Bot
        self.webhook.Bot = ExplodingBot
        try:
            status, payload = self._post({"text": "buy milk"})
        finally:
            self.webhook.Bot = orig_bot
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["reply"], "echo: buy milk")


if __name__ == "__main__":
    unittest.main()
