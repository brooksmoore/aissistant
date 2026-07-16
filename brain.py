"""The Claude brain: personality, tools, and the conversation loop.
Everything she says flows through respond(); Claude decides what to capture,
remind, remember, and put on the calendar."""
import json
import logging
import re
import threading
from datetime import datetime

import anthropic

import gcal
import memory
import scheduler
from config import (
    ANTHROPIC_API_KEY,
    ASSISTANT_NAME,
    BRAIN_MODEL,
    CLASSIFIER_MODEL,
    DAILY_BUDGET_USD,
    HARD_CAP_USD,
    EVENING_DIGEST,
    MORNING_DIGEST,
    QUIET_END_HOUR,
    QUIET_START_HOUR,
    SMART_ROUTING,
    TIMEZONE,
    TZ,
)

log = logging.getLogger("penny.brain")
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
_spend_lock = threading.Lock()  # respond() runs in worker threads while jobs call quick()

# ---------- cost metering (estimates, $/token) ----------
# (input, output, cache_write, cache_read) per token
PRICES = {
    "sonnet": (3e-6, 15e-6, 3.75e-6, 0.3e-6),
    "haiku": (1e-6, 5e-6, 1.25e-6, 0.1e-6),
}


def _price_row(model: str):
    return PRICES["haiku"] if "haiku" in model else PRICES["sonnet"]


def _track_usage(model: str, usage) -> float:
    """Add this call's estimated cost to today's tally; returns today's total."""
    p_in, p_out, p_cw, p_cr = _price_row(model)
    cost = (
        getattr(usage, "input_tokens", 0) * p_in
        + getattr(usage, "output_tokens", 0) * p_out
        + (getattr(usage, "cache_creation_input_tokens", 0) or 0) * p_cw
        + (getattr(usage, "cache_read_input_tokens", 0) or 0) * p_cr
    )
    key = "spend_" + datetime.now(TZ).date().isoformat()
    with _spend_lock:
        total = float(memory.get_setting(key) or 0) + cost
        memory.set_setting(key, f"{total:.6f}")
    return total


def today_spend() -> float:
    key = "spend_" + datetime.now(TZ).date().isoformat()
    return float(memory.get_setting(key) or 0)


# turns that deserve the big model: decisions, planning, drafting, emotional support
THINK_WORDS = (
    # Only EXPLICIT asks for judgment escalate — emotional vocabulary ("stressed",
    # "overwhelmed") must NOT, or an anxious user routes to the big model constantly.
    "help me decide", "help me think", "help me figure", "what do you think",
    "what should i do", "should i go", "should i do", "torn between", "pros and cons",
    "draft a text", "draft an email", "write a text", "write an email", "your advice",
)


def pick_model(text: str, has_image: bool = False) -> str:
    if today_spend() >= DAILY_BUDGET_USD:
        return CLASSIFIER_MODEL  # breaker tripped: never go silent, just go cheap
    if not SMART_ROUTING:
        return BRAIN_MODEL
    low = text.lower()
    if any(w in low for w in THINK_WORDS):
        return BRAIN_MODEL
    return CLASSIFIER_MODEL  # everything else: capture, check-off, list chat, photos, voice

PERSONALITY = f"""You are {ASSISTANT_NAME} — a personable, plain-spoken assistant serving as the external brain \
for one person: your owner. Her anxiety comes from holding everything in her head; your job is to hold it for \
her, reliably and calmly. You are a dependable tool she can trust, not a character.

CAPTURE: Save every task, errand, order, plan, appointment, or worry she mentions (capture_item) — a rambling \
paragraph may hold six items; capture all six. Confirm in one compact line ("Got it: Costco order, text Sam, \
REVOLVE return."); bullets only for 5+ items. Never capture silently; never claim something saved that wasn't. \
capture_item has NO timing restriction and NO content restriction — "don't let me forget X" for something \
happening in the next five minutes is still capture_item (due_at/remind_at are optional; omit them for something \
with no specific time), and this applies just as much to a physical item to grab ("AirPods", "the charger") as to \
a task. There is no such thing as "no tool for this outing right now" or "I only track tasks/plans, not objects" \
— capture_item's title is free text; a packing reminder is exactly what it's for. Never invent a capability gap \
that doesn't exist; when in doubt, capture it. Never describe something you just saved as "already" on the list \
or "you mentioned it earlier" — "already" is only for items that demonstrably existed before her current message. \
You have no memory of things she hasn't told you; never invent a prior mention. NEVER bundle two distinct \
completable actions into one item's title with "and"/"&" ("wrap gifts AND print tickets") — capture each as its \
own item, even said in one breath and due at the same time. completion is all-or-nothing per item; a bundled \
title means finishing HALF of it wrongly checks off the whole thing.

JUDGE: Set priority 1-5 and remind_at yourself — remind at the USEFUL moment (evening before a morning thing, \
days before a birthday). One clarifying question is allowed — not just for ambiguous timing, but any time a \
message could mean either "drop/cancel this" or something else (a complaint about wording, a correction to how \
it was categorized, venting), OR when "that reminder"/"it"/"this one" could genuinely point at TWO OR MORE \
distinct recent items and she hasn't said which. Only ask when more than one real candidate exists — if only one \
open item could possibly be meant, resolve it silently; asking about an unambiguous reference is friction, not \
safety. Default when multiple candidates exist: "that reminder" right after a [reminder]/[digest]/[email] ping \
means THAT ping, not an item discussed earlier in the conversation, unless she names the earlier item explicitly. \
NEVER guess toward dropping, canceling, or rescheduling the WRONG item — especially anything involving another \
named person — when genuinely torn between candidates; ask first in THAT case only. A wrong guess that touches \
is far worse than one extra question. Use recurrence (weekly/monthly/yearly) for repeating things — the next \
occurrence spawns itself on check-off.

STATUS UPDATES: If she mentions progress on a tracked item without confirming it's finished ("headed to X", \
"about to start X") do NOT complete_item on a guess — but never reply with a content-free pleasantry either. Say \
what happens to the item ("Have fun — I'll leave 'Go to Watchfest' on your list, tap done when you're back or just \
tell me"). A reply that doesn't mention the tracked item at all leaves her unable to tell whether it's still being \
watched.

SPEAK: 1-3 short, complete, natural sentences ("I'll remind you tonight at 7:30" — never "pinged tonight", \
never a bare "Done."). Every confirmation names what changed. Obey the emoji_level and reply_length preferences; \
at most one emoji regardless. No headers, no sign-offs, no restating her list unprompted.

CALM CORRECTLY: Name a feeling once, plainly, then go concrete — never "don't worry" / "breathe easy" / \
"you're all set"; blanket reassurance feeds the anxiety loop. Overwhelmed → exactly ONE next action anchored to \
a moment ("after work, text Sam"). Re-asked "did you save it?" → confirm once, briefly. Decisions → 2-3 plain \
sentences of trade-off, then ONE recommendation. Never shame overdue items: reschedule, shrink, or drop guilt-free.

MEMORY: The items and facts below are the complete permanent record; the chat scroll is windowed. Never say \
"this is our first chat" or deny prior conversations — answer "what did I tell you" from the state. Store \
durable personal facts with remember_fact. SHE IS ALWAYS RIGHT about her own life: a correction means the stored \
fact is wrong — replace it via replaces_fact_id and never repeat or defend the old value. History lines starting \
[reminder]/[digest]/[email] are scheduled pings YOU sent her — not things she said. If she replies to one \
("headed to watch fest"), that [reminder] line just above is what she's reacting to. NEVER GUESS about your own \
past turns — if she says a change you made was wrong, that means you changed the WRONG thing, not that nothing \
happened; don't claim "I didn't actually make that change" unless the item's CURRENT state (below) proves it. \
Fix it by acting on the current state and naming the new result — don't narrate an unverified story about what \
did or didn't happen before.

HER RULES: Style, reminder cadence, quiet hours, digests (time, content, or off entirely), email watching, and \
"leave me alone for a while" (notifications_enabled — pauses every reminder, digest, and email alert at once, \
nothing lost, resumes on request) are all hers to set — change them with set_preference and confirm in one \
sentence. emoji_level and reminder_overdue_label genuinely govern reminders/digests/email alerts, not just this \
chat. Custom reminder wording (E2) is real: set_preference/update_item's reminder_text applies her exact phrasing \
to every future ping on an item ("0/100 done today" instead of "Reminder: pushups"). daily_ping_cap limits how \
many proactive pings she gets per day (state block shows pings today: N (cap M)) — if she asks why she got pinged \
so much, or asks for fewer, set it. Style feedback gets stored immediately, permanently. NEVER say something is \
changed, saved, paused, or turned off without a successful tool call behind it — if no tool covers the request, \
say so plainly instead of confirming a change that didn't happen.

KNOWLEDGE: Answer general-knowledge questions confidently (which stores carry what, typical return windows, \
cooking, travel basics). You have no live internet — share what you know and name the one thing worth verifying; \
never a bare "look it up yourself."

TIME: Resolve every relative date against the current datetime below to explicit ISO in {TIMEZONE}. When \
calendar tools are present, put anything with a fixed time on the real calendar (item = to-do, event = time block). \
When answering "what's on my calendar/list" for a day or range, a general fact (a routine, a usual day off) NEVER \
replaces or hides a specific item due that day — check every open item against the range and merge them in; a day \
with both a routine fact and a due item must mention the item, not just the routine. If she asks why something \
isn't on "today's" list, a digest you sent earlier is a trimmed summary, not the source of truth — a morning \
digest only shows the first several due items by design and can genuinely omit real ones. Always recompute the \
answer from each item's own `due` date in the state below; never conclude an item is scheduled for a different \
day just because a previous digest message didn't mention it.

Item [#] and fact [f#] IDs below are real — use them for complete_item / update_item / replaces_fact_id. Items \
you captured earlier this conversation appear below: your own work, not duplicates. "What's on my list" → \
summarize from state (no tool needed), leading with today.
"""


def _tools() -> list:
    tools = [
        {
            "name": "capture_item",
            "description": "Save a new to-do, errand, order to track, social plan, or idea to her list. Use once per distinct item.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short imperative title, e.g. 'Reply to Sarah's text'"},
                    "details": {"type": "string", "description": "Any context worth keeping (links, names, amounts, what she said)"},
                    "category": {"type": "string", "enum": ["task", "errand", "shopping", "order", "social", "message", "appointment", "work", "health", "idea", "other"]},
                    "priority": {"type": "integer", "minimum": 1, "maximum": 5},
                    "due_at": {"type": "string", "description": "ISO local datetime deadline, omit if none"},
                    "remind_at": {
                        "type": "string",
                        "description": "ISO local datetime for a reminder ping. For more than one ping on the "
                        "same item (e.g. night-before + day-of), separate them with ' | ': "
                        "'2026-07-16T20:30:00 | 2026-07-17T15:00:00'. Omit entirely for no ping (it still shows in daily digests).",
                    },
                    "recurrence": {
                        "type": "string",
                        "enum": ["daily", "weekly", "monthly", "yearly"],
                        "description": "Set only if she describes something that repeats on its own cycle (a daily habit = daily, rent on the 1st = monthly, car registration every July = yearly, trash night = weekly). Requires due_at. When she checks it off, the next occurrence is created automatically.",
                    },
                    "recurrence_until": {
                        "type": "string",
                        "description": "ISO date the recurrence series should stop (e.g. she says 'this summer' -> pick a sensible end like late September). Only meaningful with recurrence set; omit for an open-ended series.",
                    },
                    "reminder_text": {
                        "type": "string",
                        "description": "Her exact custom wording for every future ping on this item, verbatim, instead of the default 'Reminder: {title}' — e.g. she wants pushup pings to say '0/100 done today'.",
                    },
                },
                "required": ["title", "category", "priority"],
            },
        },
        {
            "name": "complete_item",
            "description": "Mark an item done (she says she did it, or it's clearly no longer needed).",
            "input_schema": {
                "type": "object",
                "properties": {"item_id": {"type": "integer"}},
                "required": ["item_id"],
            },
        },
        {
            "name": "update_item",
            "description": "Change an existing item: retitle, reprioritize, reschedule its deadline or next reminder, add details, or drop it (status='dropped').",
            "input_schema": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "integer"},
                    "title": {"type": "string"},
                    "details": {"type": "string"},
                    "priority": {"type": "integer", "minimum": 1, "maximum": 5},
                    "due_at": {"type": "string"},
                    "remind_at": {
                        "type": "string",
                        "description": "Replaces this item's pending pings — one ISO datetime, or several "
                        "separated by ' | ' for night-before + day-of style reminders.",
                    },
                    "status": {"type": "string", "enum": ["open", "dropped"]},
                    "recurrence": {
                        "type": "string",
                        "enum": ["none", "daily", "weekly", "monthly", "yearly"],
                        "description": "Set to start/change a repeat cycle, or 'none' to stop it repeating.",
                    },
                    "recurrence_until": {
                        "type": "string",
                        "description": "ISO date the recurrence series should stop, or empty string to make it open-ended again.",
                    },
                    "reminder_text": {
                        "type": "string",
                        "description": "Her exact custom wording for every future ping on this item, verbatim, instead of the default template.",
                    },
                },
                "required": ["item_id"],
            },
        },
        {
            "name": "set_preference",
            "description": "Change Penny's own behavior when she asks: reminder aggressiveness, nag cadence, quiet hours, digest times.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "enum": [
                            "reminder_style",       # gentle | normal | persistent (scales all nag cadences)
                            "quiet_start_hour",     # 0-23
                            "quiet_end_hour",       # 0-23
                            "morning_digest_time",  # HH:MM 24h
                            "evening_digest_time",  # HH:MM 24h
                            "morning_digest_enabled", # yes | no — turn the morning digest off entirely
                            "evening_digest_enabled", # yes | no — turn the evening digest off entirely
                            "notifications_enabled", # yes | no — master pause: reminders, digests, and email alerts all go silent (nothing is lost, they resume once turned back on)
                            "gmail_watch_enabled",  # yes | no — stop watching/pinging about email (only meaningful once Gmail is connected)
                            "nag_interval_p5",      # minutes between nags for priority 5
                            "nag_interval_p4",
                            "nag_interval_p3",
                            "nag_interval_p2",
                            "max_nags",             # pings per item before falling back to digests
                            "daily_ping_cap",       # soft cap on proactive pings/day (nags + email defer past it; scheduled reminders and P5 always send)
                            "emoji_level",          # none | minimal | normal — applies to scheduled reminders/digests/email alerts too, not just chat replies
                            "reminder_overdue_label", # yes | no — whether reminders/list/digests call a late item "overdue" at all
                            "reply_length",         # short | normal
                            "digest_show_completed", # yes | no — list finished items in the evening digest
                            "digest_style",         # smart | plain — smart: model-written morning plan with conflict warnings; plain: simple list
                        ],
                    },
                    "value": {"type": "string"},
                },
                "required": ["key", "value"],
            },
        },
        {
            "name": "remember_fact",
            "description": "Store a durable fact about her life (people, preferences, routines, ongoing situations). Not for to-dos.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "fact": {"type": "string"},
                    "category": {"type": "string", "enum": ["identity", "people", "preferences", "routine", "situation", "general"]},
                    "replaces_fact_id": {"type": "integer", "description": "If this corrects a stored fact, its [f#] id — the old fact is deleted"},
                },
                "required": ["fact"],
            },
        },
    ]
    if gcal.enabled():
        tools += [
            {
                "name": "create_calendar_event",
                "description": "Create a real event on her Google Calendar.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "start": {"type": "string", "description": "ISO local datetime"},
                        "end": {"type": "string", "description": "ISO local datetime; omit for 1 hour"},
                        "notes": {"type": "string"},
                    },
                    "required": ["title", "start"],
                },
            },
            {
                "name": "update_calendar_event",
                "description": "Move/rename a calendar event. Event IDs are in the calendar state.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "event_id": {"type": "string"},
                        "title": {"type": "string"},
                        "start": {"type": "string"},
                        "end": {"type": "string"},
                        "notes": {"type": "string"},
                    },
                    "required": ["event_id"],
                },
            },
            {
                "name": "delete_calendar_event",
                "description": "Delete a calendar event.",
                "input_schema": {
                    "type": "object",
                    "properties": {"event_id": {"type": "string"}},
                    "required": ["event_id"],
                },
            },
        ]
    return tools


def _run_tool(name: str, inp: dict) -> str:
    try:
        if name == "capture_item":
            item_id = memory.add_item(
                title=inp["title"],
                details=inp.get("details", ""),
                category=inp["category"],
                priority=inp["priority"],
                due_at=inp.get("due_at"),
                remind_at=inp.get("remind_at"),
                recurrence=inp.get("recurrence"),
                recurrence_until=inp.get("recurrence_until"),
                reminder_text=inp.get("reminder_text"),
            )
            return f"Saved as item #{item_id}."
        if name == "complete_item":
            memory.complete_item(inp["item_id"])
            return "Marked done."
        if name == "update_item":
            fields = {k: v for k, v in inp.items() if k != "item_id"}
            remind_at = fields.pop("remind_at", None)
            if fields.get("recurrence") == "none":
                fields["recurrence"] = None
            if fields.get("recurrence_until") == "":
                fields["recurrence_until"] = None
            if fields:
                memory.update_item(inp["item_id"], **fields)
            if remind_at is not None:
                # replaces pending pings, not a column on items — see v1.5 split
                # in scheduler.py (scheduled reminders vs the nag chase)
                memory.replace_item_reminders(inp["item_id"], remind_at)
            return "Updated."
        if name == "set_preference":
            key, value = inp["key"], str(inp["value"]).strip()
            if key == "reminder_style" and value not in ("gentle", "normal", "persistent"):
                return "Invalid style — use gentle, normal, or persistent."
            if key == "emoji_level" and value not in ("none", "minimal", "normal"):
                return "emoji_level must be none, minimal, or normal."
            if key == "reply_length" and value not in ("short", "normal"):
                return "reply_length must be short or normal."
            if key == "digest_style" and value not in ("smart", "plain"):
                return "digest_style must be smart or plain."
            if key == "digest_show_completed" and value not in ("yes", "no"):
                return "digest_show_completed must be yes or no."
            if key.endswith("_digest_enabled") and value not in ("yes", "no"):
                return f"{key} must be yes or no."
            if key in ("notifications_enabled", "gmail_watch_enabled", "reminder_overdue_label") and value not in ("yes", "no"):
                return f"{key} must be yes or no."
            if key.endswith("_hour") and not (value.isdigit() and 0 <= int(value) <= 23):
                return "Hour must be 0-23."
            if key.endswith("_time"):
                try:
                    h, m = value.split(":")
                    assert 0 <= int(h) <= 23 and 0 <= int(m) <= 59
                except (ValueError, AssertionError):
                    return "Time must be HH:MM (24h)."
            if (key.startswith("nag_interval") or key in ("max_nags", "daily_ping_cap")) and not value.isdigit():
                return "Must be a whole number."
            memory.set_setting("pref_" + key, value)
            return f"Preference updated: {key} = {value}."
        if name == "remember_fact":
            replaced = inp.get("replaces_fact_id")
            if replaced:
                memory.delete_fact(replaced)
            memory.add_fact(inp["fact"], inp.get("category", "general"))
            return "Old fact replaced." if replaced else "Remembered."
        if name == "create_calendar_event":
            eid = gcal.create_event(inp["title"], inp["start"], inp.get("end"), inp.get("notes", ""))
            return f"Event created (id {eid})."
        if name == "update_calendar_event":
            gcal.update_event(inp["event_id"], inp.get("title"), inp.get("start"), inp.get("end"), inp.get("notes"))
            return "Event updated."
        if name == "delete_calendar_event":
            gcal.delete_event(inp["event_id"])
            return "Event deleted."
        return f"Unknown tool {name}."
    except Exception as e:
        log.exception("tool %s failed", name)
        return f"Tool error: {e}"


def _state_block() -> str:
    """Items/facts/prefs/calendar — everything EXCEPT the current timestamp, so
    this block is byte-identical between two turns where nothing changed and
    can carry its own cache breakpoint. The timestamp changes every single
    turn by definition; folding it in here used to bust that cache on every
    message (see respond()'s cache_control comment)."""
    facts = memory.all_facts()[-60:]  # cap: facts accumulate for years; keep prompt bounded
    facts_txt = "\n".join(f"- [f{f['id']}] {f['content']}" for f in facts) or "(nothing yet — she's new; learn her name early)"
    items = memory.open_items()[:40]
    if items:
        lines = []
        for i in items:
            bits = [f"#{i['id']} [P{i['priority']}] ({i['category']}) {i['title']}"]
            if i["due_at"]:
                bits.append(f"due {i['due_at']}")
            pings = memory.pending_reminder_times(i["id"])
            if pings:
                short = [p[5:16].replace("T", " ") for p in pings]  # MM-DD HH:MM
                bits.append(f"pings {', '.join(short)}")
            if i["next_remind_at"]:
                bits.append(f"overdue, next nag {i['next_remind_at']}")
            if i["recurrence"]:
                until = f" until {i['recurrence_until']}" if i["recurrence_until"] else ""
                bits.append(f"repeats {i['recurrence']}{until}")
            if i["reminder_text"]:
                bits.append(f"custom ping text: \"{i['reminder_text']}\"")
            if i["details"]:
                bits.append(f"— {i['details'][:120]}")
            lines.append(" ".join(bits))
        items_txt = "\n".join(lines)
    else:
        items_txt = "(list is empty)"
    cal_txt = gcal.upcoming_text() if gcal.enabled() else "(calendar not connected yet)"
    prefs_txt = (
        f"emoji_level={scheduler.pref('emoji_level', 'minimal')}, "
        f"reply_length={scheduler.pref('reply_length', 'short')}, "
        f"reminder_style={scheduler.pref('reminder_style', 'normal')}, "
        # defaults must mirror what the scheduler actually uses (.env-overridable),
        # or the model confidently states wrong quiet hours / digest times
        f"quiet={scheduler.pref('quiet_start_hour', QUIET_START_HOUR)}:00-{scheduler.pref('quiet_end_hour', QUIET_END_HOUR)}:00, "
        f"morning digest {'OFF' if scheduler.pref('morning_digest_enabled', 'yes') == 'no' else scheduler.pref('morning_digest_time', MORNING_DIGEST)}, "
        f"evening digest {'OFF' if scheduler.pref('evening_digest_enabled', 'yes') == 'no' else scheduler.pref('evening_digest_time', EVENING_DIGEST)}, "
        f"nag cadence P5/P4/P3 = {scheduler.escalation_minutes(5)}/{scheduler.escalation_minutes(4)}/{scheduler.escalation_minutes(3)} min, "
        f"max {scheduler.max_nags()} nags per item, "
        f"reminders call late items 'overdue'={scheduler.pref('reminder_overdue_label', 'yes')}, "
        f"pings today: {scheduler.pings_today()} (cap {scheduler.pref_int('daily_ping_cap', scheduler.DAILY_PING_CAP)}), "
        f"ALL notifications (reminders/digests/email alerts) {'PAUSED' if scheduler.pref('notifications_enabled', 'yes') == 'no' else 'on'}"
        + (f", gmail watch {'OFF' if scheduler.pref('gmail_watch_enabled', 'yes') == 'no' else 'on'}" if gcal.enabled() else "")
    )
    return (
        f"\n--- CURRENT STATE ---\n"
        f"What you know about her:\n{facts_txt}\n\n"
        f"Her open items:\n{items_txt}\n\n"
        f"Her calendar (next 7 days):\n{cal_txt}\n\n"
        f"Your current behavior settings: {prefs_txt}\n"
    )


def _now_line() -> str:
    """The one genuinely-volatile line, split out of _state_block() so that
    block can cache-hit across turns where nothing else changed. Kept as its
    own tiny, uncached system block placed AFTER the state block's breakpoint
    — its content changes every turn, but at one short line the cost of
    resending it fresh is negligible next to resending the whole state block."""
    now = datetime.now(TZ)
    return f"Now: {now.strftime('%A, %B %d, %Y at %I:%M %p')} ({TIMEZONE})"


def _history() -> list:
    """Rebuild recent chat as alternating user/assistant messages (trimmed to keep tokens down)."""
    msgs = []
    rows = memory.recent_msgs(24)
    if rows:
        # Slide the window in blocks of 8, not per-message: the prefix stays
        # byte-identical across several turns so the conversation cache HITS.
        anchor = ((rows[-1]["id"] - 16) // 8) * 8
        rows = [r for r in rows if r["id"] > anchor]
    for row in rows:
        role = row["role"] if row["role"] in ("user", "assistant") else "user"
        content = row["content"][:1000]
        if msgs and msgs[-1]["role"] == role:
            msgs[-1]["content"] += "\n" + content
        else:
            msgs.append({"role": role, "content": content})
    if msgs and msgs[0]["role"] == "assistant":
        msgs.insert(0, {"role": "user", "content": "(session start)"})
    return msgs


def respond(user_text: str, image_b64: str = None, image_media_type: str = "image/jpeg") -> str:
    """Full conversational turn. Blocking — call via asyncio.to_thread from the bot."""
    memory.log_msg("user", user_text)
    messages = _history()
    if image_b64:
        last = messages[-1]
        last["content"] = [
            {"type": "image", "source": {"type": "base64", "media_type": image_media_type, "data": image_b64}},
            {"type": "text", "text": str(last["content"])},
        ]

    if today_spend() >= HARD_CAP_USD:
        # True hard cap: no API call at all. Reminders, buttons, and digests
        # keep working (they never use the API); only conversation rests.
        text = ("I've used up my thinking budget for today, so I'm resting until midnight — "
                "but everything is saved, and your reminders and check-off buttons still work fine.")
        memory.log_msg("assistant", text)
        log.warning("hard spend cap hit ($%.2f); conversation paused for today", today_spend())
        return text

    model = pick_model(user_text, has_image=bool(image_b64))
    tools = _tools()
    # Third cache breakpoint on the newest history message. Default 5m TTL (a
    # 1h TTL here just doubles the write premium on an entry the next turn
    # invalidates) — this one pays off within a single multi-tool round-trip
    # (see below) and, when the state block also hits, across close-together
    # turns too (see the state-block breakpoint's comment).
    if len(messages) > 1 and isinstance(messages[-1]["content"], str):
        messages[-1]["content"] = [
            {"type": "text", "text": messages[-1]["content"], "cache_control": {"type": "ephemeral"}}
        ]
    # The personality (+ tools) prefix is identical every turn -> prompt-cached (~90% off
    # input cost on hits). The state block is computed ONCE per turn: recomputing it
    # inside the tool loop changed the prompt prefix every iteration (minute timestamp,
    # freshly captured items) and busted the intra-turn cache on every round-trip.
    #
    # The state block gets its own breakpoint too. Anthropic's cache match is
    # prefix-based: a breakpoint only hits if EVERYTHING before it is
    # byte-identical to a prior cached request. The state block used to open
    # with a to-the-minute "Now: ..." line, which meant it — and everything
    # cached after it — differed on literally every single turn, so no turn
    # ever benefited from caching beyond the personality block alone. The
    # timestamp now lives in its own tiny block AFTER this breakpoint: on any
    # turn where her items/facts/prefs didn't change (the common case for two
    # messages minutes apart), this entire block cache-hits too.
    system = [
        {"type": "text", "text": PERSONALITY, "cache_control": {"type": "ephemeral", "ttl": "1h"}},
        {"type": "text", "text": _state_block(), "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": _now_line()},
    ]

    captured = []   # item titles saved this turn, for the fallback confirmation
    did = []        # plain-English record of every successful non-capture action

    def _execute_tool_calls(blocks) -> list:
        """Runs every tool_use block, classifies the result into captured/did
        (closed over), and returns the tool_result content for the next turn."""
        tool_results = []
        for block in blocks:
            if block.type != "tool_use":
                continue
            # grab the title before complete_item wipes our chance to name it
            pre_title = None
            if block.name in ("complete_item", "update_item"):
                row = memory.get_item(block.input.get("item_id", -1))
                pre_title = row["title"] if row else None
            result = _run_tool(block.name, block.input)
            ok = not result.startswith(("Tool error", "Unknown", "Invalid"))
            if block.name == "capture_item" and result.startswith("Saved"):
                captured.append(block.input.get("title", "item"))
            elif ok and block.name == "complete_item" and pre_title:
                did.append(f"checked off \"{pre_title}\"")
            elif ok and block.name == "update_item" and pre_title:
                did.append(f"updated \"{pre_title}\"")
            elif ok and block.name == "set_preference":
                did.append(f"changed {block.input.get('key','a setting')}")
            elif ok and block.name == "remember_fact":
                did.append("saved that to memory")
            elif ok and block.name.endswith("calendar_event"):
                did.append("updated the calendar")
            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        return tool_results

    def _create(msgs):
        try:
            r = client.messages.create(
                model=model, max_tokens=4096, system=system, tools=tools, messages=msgs,
                extra_headers={"anthropic-beta": "extended-cache-ttl-2025-04-11"},
            )
        except anthropic.BadRequestError:
            # If the extended-TTL beta is ever retired, strip ttl hints and continue
            # on the standard 5-minute cache rather than failing her message.
            log.warning("extended cache TTL rejected; retrying with default cache")
            for b in system:
                if isinstance(b, dict) and b.get("cache_control", {}).get("ttl"):
                    b["cache_control"] = {"type": "ephemeral"}
            r = client.messages.create(model=model, max_tokens=4096, system=system, tools=tools, messages=msgs)
        nonlocal spent
        spent = _track_usage(model, r.usage)
        return r

    spent = today_spend()
    resp = None
    for _ in range(10):
        resp = _create(messages)
        # Execute every complete tool call we got — even if the response was
        # truncated (stop_reason "max_tokens"), partial progress must be saved.
        tool_results = _execute_tool_calls(resp.content)
        if not tool_results:
            break
        messages.append({"role": "assistant", "content": resp.content})
        messages.append({"role": "user", "content": tool_results})

    log.info("turn: model=%s spend_today=$%.4f", model.split("-")[1], spent)

    text = "".join(b.text for b in resp.content if b.type == "text").strip()

    # Empty-promise guard: the model stated a change ("turned off", "saved
    # that", ...) but made zero tool calls this turn. Give it exactly one
    # corrective round-trip — either it actually makes the calls now, or it
    # honestly walks the claim back. This is what tonight's incident was:
    # Penny said "I've turned off your evening digest" with nothing behind it.
    #
    # Real incident (2026-07-16, jarvis): she asked a plain diagnostic
    # question ("why did you nudge me a minute after the initial reminder?"),
    # the model's accurate explanatory answer ("I made an error sending that
    # second ping so quickly") tripped this guard, and the ONLY two outcomes
    # this prompt used to offer — "do something" or "admit you couldn't" —
    # left no room for the actually-correct third answer: nothing needed
    # fixing, she just asked what happened. Forced to pick one of two, the
    # model invented an update_item call and silently moved a real due date
    # a full day out. The corrective prompt now explicitly allows "just
    # explain, no action needed" as a valid outcome.
    if text and not captured and not did and (claims_change(text) or llm_claims_change(text)):
        n = memory.bump_counter("incident_claims")
        log.warning("empty-promise guard tripped (incident #%d today): %r", n, text[:600])
        messages.append({"role": "assistant", "content": resp.content})
        messages.append({"role": "user", "content": [{"type": "text", "text": (
            "(automated system check, not a message from her — she has not seen your "
            "last reply yet and did not correct you: your last reply reads like it's "
            "claiming a change happened, but no tool call succeeded this turn. Look at "
            "what she actually asked. Three honest outcomes are possible, pick whichever "
            "is true: (1) something genuinely needs doing — make the real tool call(s) "
            "now and confirm; (2) you can't do it — say so plainly; (3) nothing actually "
            "needed fixing — she asked a question, not for a change, and your wording "
            "just sounded like a claim. In case (3), do NOT invent a tool call to have "
            "something to point to — just answer her question accurately with no action "
            "at all. Whichever it is, do not say 'you're right', don't reference this "
            "check, don't apologize for a mistake she hasn't actually raised — reply as "
            "your first and only response to her.)"
        )}]})
        resp = _create(messages)
        _execute_tool_calls(resp.content)
        text = "".join(b.text for b in resp.content if b.type == "text").strip()

    # Capture-completeness check: a long message that saved at least one item
    # gets a cheap second look for siblings the model missed ("a rambling
    # paragraph may hold six items"). One corrective round-trip max; the checker
    # failing or returning nonsense costs nothing (fail-open, NONE-biased).
    if captured and len(user_text) >= 100:
        missed = _missed_captures(user_text, captured)
        if missed:
            log.warning("capture check found %d possibly-missed item(s): %r", len(missed), missed)
            messages.append({"role": "assistant", "content": resp.content})
            messages.append({"role": "user", "content": [{"type": "text", "text": (
                "(automated system check, not a message from her — she has not seen your "
                "reply yet: her message may have contained items you did not capture: "
                + "; ".join(missed[:5]) + ". If any of these are genuinely distinct new items "
                "she asked to save, capture them now and confirm everything in one reply. If "
                "they are duplicates of items you already saved or not actually asks, change "
                "nothing and simply restate your confirmation. Reply as your first and only "
                "response to her — do not mention this check.)"
            )}]})
            resp = _create(messages)
            _execute_tool_calls(resp.content)
            text = "".join(b.text for b in resp.content if b.type == "text").strip()

    if not text:
        if captured:
            text = "Got it — " + ", ".join(captured[:8]) + ". All on the list."
        elif did:
            text = ("Done — " + " and ".join(did[:3]) + ".").capitalize()
        else:
            text = "That didn't save properly on my end — send it once more?"
    memory.log_msg("assistant", text)
    return text


CLAIM_PATTERN = (
    r"turned off|turned on|i've set|i've moved|i've updated|i've changed|i've paused|"
    r"now on your list|reminders? (?:is|are)? ?(?:now )?set|on the calendar|checked off|"
    # "Got it:" / "noted" are capture confirmations — with a real capture behind
    # them the guard never runs (captured is non-empty), so matching them here
    # only ever fires on the empty-promise case (seen live 2026-07-16: "Got it:
    # bring AirPods..." with zero tool calls and nothing saved)
    r"dropped the|saved that|\bgot it\b|\bnoted\b"
)
_CLAIM_RE = re.compile(CLAIM_PATTERN, re.IGNORECASE)


def claims_change(text: str) -> bool:
    """True if text asserts a behavior/state change happened. Used by the
    empty-promise guard to catch a claim made with no tool call behind it —
    deliberately conservative (see CLAIM_PATTERN); false positives just cost
    one cheap corrective retry, not a wrong answer."""
    return bool(_CLAIM_RE.search(text))


JUDGE_PROMPT = """Reply with exactly one word, YES or NO.

Does the following assistant message assert that the assistant HAS ALREADY \
made, saved, changed, completed, or scheduled something during this turn? \
Promises about the future ("I'll remind you at 9"), questions, summaries of \
existing state, and general conversation are all NO. Only a claim that an \
action has already been performed is YES.

Assistant message:
{text}"""


MISSED_CAPTURE_PROMPT = """The user sent their assistant this message:
---
{user_text}
---
The assistant saved these items from it: {saved}

List any OTHER distinct, actionable to-do items the user explicitly asked to \
save in that message that are NOT covered by the saved list. Rules: reply \
NONE unless you are confident something real was missed; near-duplicates, \
rephrasings of saved items, questions, and general chat are NOT missed items. \
Reply with NONE, or one missed item per line (max 5), nothing else."""


def _missed_captures(user_text: str, captured: list) -> list:
    """Cheap post-capture completeness check. Returns possibly-missed item
    descriptions, or [] (including on any error — fail-open: this must never
    block a reply or invent work)."""
    try:
        raw = quick(
            MISSED_CAPTURE_PROMPT.format(user_text=user_text[:2000], saved=", ".join(captured)),
            max_tokens=150,
        )
    except Exception:
        log.exception("capture check failed (failing open)")
        return []
    if raw.strip().upper().startswith("NONE"):
        return []
    lines = [l.strip("-• ").strip() for l in raw.splitlines() if l.strip()]
    return [l for l in lines if l and l.upper() != "NONE"][:5]


def llm_claims_change(text: str) -> bool:
    """Second layer of the empty-promise guard: a cheap model judgment for
    claim phrasings the regex can't anticipate ("that's off your plate now",
    "consider it handled"). Costs ~$0.0005/turn. Fails OPEN (returns False)
    on any error — this layer must never be able to block or delay a reply."""
    try:
        verdict = quick(JUDGE_PROMPT.format(text=text[:1200]), max_tokens=5)
        return verdict.strip().upper().startswith("YES")
    except Exception:
        log.exception("claim judge failed (failing open)")
        return False


def quick(prompt: str, model: str = None, max_tokens: int = 800) -> str:
    """One-shot completion with no tools/history — used for email triage etc."""
    m = model or CLASSIFIER_MODEL
    resp = client.messages.create(
        model=m,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    _track_usage(m, resp.usage)
    return "".join(b.text for b in resp.content if b.type == "text").strip()


DIGEST_PROMPT = """{state}

Write the owner's morning digest for {today}. Rules:
- Order today's due items into a realistic sequence given their times; name the ONE thing that matters most first.
- Then a "Heads up:" section ONLY if you find real problems in the next 3 days: time conflicts, an item missing obvious prep (a gift not bought before its wrapping day), or something due on a day off. Skip the section entirely if there are none — never invent a concern.
- Items with a specific future due date are NOT suggestions for today; only genuinely undated items may be offered as "if there's spare energy" (max 3).
- Plain text, no markdown headers/bold, max 12 short lines, warm but not chatty. Do not greet with her name. Never claim anything was changed or handled — this is a read-only summary.
- End with exactly this line: ({n_open} things safely on the list — say "list" anytime.)"""


def compose_digest() -> str | None:
    """Model-written morning digest: plan-shaped, conflict-aware. Returns None
    on any failure or if spending is capped — the caller ALWAYS has the plain
    f-string digest as fallback, so a digest can never be lost to this feature."""
    if today_spend() >= HARD_CAP_USD:
        log.warning("smart digest skipped: daily hard cap reached")
        return None
    now = datetime.now(TZ)
    try:
        text = quick(
            DIGEST_PROMPT.format(
                state=_state_block(),
                today=now.strftime("%A, %B %-d"),
                n_open=len(memory.open_items()),
            ),
            max_tokens=400,
        )
    except Exception:
        log.exception("smart digest failed; falling back to plain digest")
        return None
    # sanity: an empty or absurdly long reply falls back rather than shipping
    if not text or len(text) > 1500:
        return None
    return text
