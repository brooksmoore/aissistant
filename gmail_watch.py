"""Gmail inbox watcher (personal inbox only, read-only). Polls for new unread
mail, triages it with a cheap model, and pings her only for things that matter."""
import asyncio
import json
import logging
import re
from datetime import datetime

import brain
import gcal
import memory
from config import CLASSIFIER_MODEL, HARD_CAP_USD, GOOGLE_TOKEN, TZ
from scheduler import _quiet, icon as deco_icon, item_buttons, pref

log = logging.getLogger("penny.gmail")

_service = None


def enabled() -> bool:
    return GOOGLE_TOKEN.exists()


def _svc():
    global _service
    if _service is None:
        _service = gcal.service("gmail", "v1")
    return _service


def _header(msg, name):
    for h in msg.get("payload", {}).get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def fetch_new() -> list:
    """New unseen unread messages from the primary inbox: [{id, sender, subject, snippet}]."""
    try:
        resp = _svc().users().messages().list(
            userId="me", q="in:inbox is:unread category:primary newer_than:2d", maxResults=25
        ).execute()
    except Exception:
        log.exception("gmail list failed")
        return []
    out = []
    for ref in resp.get("messages", []):
        if memory.email_seen(ref["id"]):
            continue
        try:
            msg = _svc().users().messages().get(
                userId="me", id=ref["id"], format="metadata",
                metadataHeaders=["From", "Subject"],
            ).execute()
        except Exception:
            continue
        out.append({
            "id": ref["id"],
            "sender": _header(msg, "From"),
            "subject": _header(msg, "Subject"),
            "snippet": msg.get("snippet", "")[:300],
        })
    return out


TRIAGE_PROMPT = """You triage a personal (non-work) email inbox for a busy woman with anxiety. \
For each email below, classify it:

- "urgent": time-sensitive and personally important (appointment change, payment problem, someone needs an answer today)
- "needs_reply": a real person is waiting on a response from her
- "delivery": package/order/shipping updates (Amazon etc.)
- "fyi": mildly useful info, no action
- "ignore": marketing, newsletters, promos, notifications nobody reads

Mailing lists, neighborhood platforms (Nextdoor and the like), newsletters, and automated/no-reply senders are \
NEVER "urgent" or "needs_reply" — regardless of how emotional or urgent-sounding the content is. "needs_reply" \
strictly means one individual person personally addressing HER by name or context, not a broadcast post. \
Example: a Nextdoor neighborhood post announcing a death in the community is heartfelt but is "fyi" at most — \
it is not a person waiting on a reply from her, and its raw text must never become a task on her list.

For each email also write "topic": a neutral 3-6 word summary suitable as a task title (never the raw subject \
or preview verbatim — especially never verbatim distressing/personal content from someone else's message).

Reply with ONLY a JSON array like:
[{"id": "...", "kind": "urgent|needs_reply|delivery|fyi|ignore", "summary": "one short human sentence", "topic": "3-6 word neutral topic"}]

Emails:
"""


def triage(emails: list) -> list:
    if not emails:
        return []
    listing = "\n".join(
        f"- id: {e['id']}\n  from: {e['sender']}\n  subject: {e['subject']}\n  preview: {e['snippet']}"
        for e in emails
    )
    try:
        raw = brain.quick(TRIAGE_PROMPT + listing, model=CLASSIFIER_MODEL, max_tokens=1500)
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        results = json.loads(match.group(0)) if match else []
    except Exception:
        log.exception("triage failed")
        results = []
    by_id = {e["id"]: e for e in emails}
    out = []
    for r in results:
        e = by_id.get(r.get("id"))
        if e:
            out.append({
                **e,
                "kind": r.get("kind", "fyi"),
                "summary": r.get("summary", e["subject"]),
                "topic": r.get("topic", e["subject"])[:70],
            })
    # anything the model dropped: mark fyi so we don't reprocess forever
    triaged_ids = {r.get("id") for r in results}
    for e in emails:
        if e["id"] not in triaged_ids:
            out.append({**e, "kind": "fyi", "summary": e["subject"], "topic": e["subject"][:70]})
    return out


async def poll(context):
    """Job: every N minutes, surface only what matters."""
    if not enabled():
        return
    # both the master pause and the feature-specific toggle stop polling
    # entirely — nothing is fetched or marked seen, so resuming picks up
    # cleanly rather than needing to reconnect Gmail
    if pref("notifications_enabled", "yes") == "no" or pref("gmail_watch_enabled", "yes") == "no":
        return
    chat_id = memory.get_setting("owner_chat_id")
    if not chat_id:
        return
    if brain.today_spend() >= HARD_CAP_USD:
        # hard cap tripped: skip entirely (nothing fetched or marked, so today's
        # mail is simply triaged on a later poll instead of spending past the cap)
        return
    # network + Claude calls off the event loop: a big fetch must not freeze the bot
    emails = await asyncio.to_thread(fetch_new)
    if not emails:
        return
    triaged = await asyncio.to_thread(triage, emails)
    quiet = _quiet(datetime.now(TZ))
    urgent, needs_reply, deliveries = [], [], []
    for e in triaged:
        memory.mark_email(e["id"], e["sender"], e["subject"], e["summary"], e["kind"])
        if e["kind"] == "urgent":
            urgent.append(e)
        elif e["kind"] == "needs_reply":
            needs_reply.append(e)
        elif e["kind"] == "delivery":
            deliveries.append(e)

    # needs_reply goes on the list + next digest silently — no standalone ping.
    # Only "urgent" (time-sensitive, personally important) interrupts immediately.
    for e in needs_reply:
        sender = re.sub(r"\s*<.*>", "", e["sender"]).strip()
        memory.add_item(
            title=f"Reply to {sender} — {e['topic']}"[:70],
            details=e["summary"],
            category="message",
            priority=3,
        )

    for e in urgent:
        sender = re.sub(r"\s*<.*>", "", e["sender"]).strip()
        # during quiet hours, don't ping now — but the item still goes on the list
        # with a due reminder, so check_reminders surfaces it right after quiet ends
        # (previously these were marked seen and silently lost forever)
        item_id = memory.add_item(
            title=f"Reply to {sender} — {e['topic']}"[:70],
            details=e["summary"],
            category="message",
            priority=4,
            remind_at=memory.now_iso() if quiet else None,
        )
        if not quiet:
            icon = deco_icon("🚨").strip()
            lead = f"{icon} " if icon else ""
            text = f"{lead}Email from {sender}: {e['summary']}\n\nI put it on your list — check it off when you've replied."
            await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=item_buttons(item_id))
            memory.log_msg("assistant", f"[email] {text}")
            memory.bump_counter("pings", 1)
    if deliveries and not quiet:
        box = deco_icon("📦")
        lines = [f"  {box}{e['summary']}" for e in deliveries[:5]]
        text = "Package update:\n" + "\n".join(lines)
        await context.bot.send_message(chat_id=chat_id, text=text)
        memory.log_msg("assistant", f"[email] {text}")
        memory.bump_counter("pings", 1)
