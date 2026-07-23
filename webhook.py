"""LAN capture endpoint for hands-free input (a Siri Shortcut, an iOS
automation, anything on the same Wi-Fi that can POST JSON). Deliberately
minimal: one endpoint, one shared secret, reuses brain.respond() end to end
so a Siri-captured message gets the exact same tool calls, empty-promise
guard, and budget caps as a Telegram message — no parallel capture logic to
drift out of sync. OFF unless WEBHOOK_SECRET is set in .env."""
import hmac
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from telegram import Bot

import brain
import memory
from config import ASSISTANT_NAME, TELEGRAM_TOKEN, WEBHOOK_PORT, WEBHOOK_SECRET

log = logging.getLogger("penny.webhook")

MAX_BODY_BYTES = 4000  # a runaway/garbage request must not tie up the handler


def _reply_and_send(text: str) -> str:
    """Full brain turn (blocking — this runs on the webhook's own thread, not
    the bot's asyncio loop) plus a best-effort Telegram notify, so the
    exchange shows up in chat history like anything typed directly. The
    capture itself (brain.respond) is the thing the Shortcut is waiting on —
    a Telegram delivery hiccup afterward must not turn a successful capture
    into a reported failure."""
    import asyncio

    reply = brain.respond(text)  # if this raises, the caller correctly reports failure

    async def _send():
        chat_id = memory.get_setting("owner_chat_id")
        if chat_id:
            bot = Bot(token=TELEGRAM_TOKEN)
            async with bot:
                await bot.send_message(chat_id=chat_id, text=f"🎙️ {reply}")

    try:
        asyncio.run(_send())
    except Exception:
        log.exception("webhook: capture succeeded but the Telegram notify failed")
    return reply


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # keep assistant.log free of raw HTTP access-log noise

    def _json(self, status: int, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        """Liveness check for a companion app's "Connected" indicator — same
        secret as /capture (a health endpoint that answers unauthenticated
        would let anyone on the LAN/tailnet fingerprint that this port is an
        aissistant instance) and reveals nothing beyond that fact."""
        if self.path != "/health":
            self._json(404, {"ok": False, "error": "not found"})
            return
        given = self.headers.get("X-Aissistant-Secret", "")
        if not hmac.compare_digest(given, WEBHOOK_SECRET):
            self._json(401, {"ok": False, "error": "unauthorized"})
            return
        self._json(200, {"ok": True, "assistant": ASSISTANT_NAME})

    def do_POST(self):
        if self.path != "/capture":
            self._json(404, {"ok": False, "error": "not found"})
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0 or length > MAX_BODY_BYTES:
            self._json(413, {"ok": False, "error": "bad request size"})
            return
        given = self.headers.get("X-Aissistant-Secret", "")
        if not hmac.compare_digest(given, WEBHOOK_SECRET):
            self._json(401, {"ok": False, "error": "unauthorized"})
            return
        raw = self.rfile.read(length)
        try:
            text = str(json.loads(raw).get("text", "")).strip()
        except Exception:
            text = ""
        if not text:
            self._json(400, {"ok": False, "error": "missing text"})
            return
        try:
            reply = _reply_and_send(text)
            self._json(200, {"ok": True, "reply": reply})
        except Exception:
            log.exception("webhook capture failed")
            self._json(500, {"ok": False, "reply": "Something glitched on my end — try again."})


def maybe_start():
    if not WEBHOOK_SECRET:
        log.info("webhook capture: OFF (set WEBHOOK_SECRET in .env to enable)")
        return
    server = ThreadingHTTPServer(("0.0.0.0", WEBHOOK_PORT), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True, name="webhook").start()
    log.info("webhook capture: ON — %s listening on port %d (LAN only)", ASSISTANT_NAME, WEBHOOK_PORT)
