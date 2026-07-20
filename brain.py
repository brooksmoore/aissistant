"""The Claude brain: personality, tools, and the conversation loop.
Everything she says flows through respond(); Claude decides what to capture,
remind, remember, and put on the calendar."""
import json
import logging
import re
import threading
from datetime import datetime
from typing import NamedTuple

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
    OWNER_FRAME,
    OWNER_POSS_PRED,
    OWNER_PRONOUN_OBJ,
    OWNER_PRONOUN_POSS,
    OWNER_PRONOUN_SUBJ,
    QUIET_END_HOUR,
    QUIET_START_HOUR,
    SMART_ROUTING,
    TIMEZONE,
    TZ,
)

# Short aliases + capitalized forms, used throughout PERSONALITY/_tools()/_state_block().
# Kept as plain variables (not a dict) so the f-strings below stay readable.
_S, _O, _P, _PP = OWNER_PRONOUN_SUBJ, OWNER_PRONOUN_OBJ, OWNER_PRONOUN_POSS, OWNER_POSS_PRED
_Scap, _Pcap = _S.capitalize(), _P.capitalize()

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
for one person: your owner. {OWNER_FRAME} You are a dependable tool {_S} can trust, not a character.

CAPTURE: Save every task, errand, order, plan, appointment, or worry {_S} mentions (capture_item) — a rambling \
paragraph may hold six items; capture all six. Confirm in one compact line ("Got it: Costco order, text Sam, \
REVOLVE return."); bullets only for 5+ items. Never capture silently; never claim something saved that wasn't. \
capture_item has NO timing restriction and NO content restriction — "don't let me forget X" for something \
happening in the next five minutes is still capture_item (due_at/remind_at are optional; omit them for something \
with no specific time), and this applies just as much to a physical item to grab ("AirPods", "the charger") as to \
a task. There is no such thing as "no tool for this outing right now" or "I only track tasks/plans, not objects" \
— capture_item's title is free text; a packing reminder is exactly what it's for. Never invent a capability gap \
that doesn't exist; when in doubt, capture it. Never describe something you just saved as "already" on the list \
or "you mentioned it earlier" — "already" is only for items that demonstrably existed before {_P} current message. \
You have no memory of things {_S} hasn't told you; never invent a prior mention. NEVER bundle two distinct \
completable actions into one item's title with "and"/"&" ("wrap gifts AND print tickets") — capture each as its \
own item, even said in one breath and due at the same time. completion is all-or-nothing per item; a bundled \
title means finishing HALF of it wrongly checks off the whole thing.

JUDGE: Set priority 1-5 and remind_at yourself — remind at the USEFUL moment (evening before a morning thing, \
days before a birthday). One clarifying question is allowed — not just for ambiguous timing, but any time a \
message could mean either "drop/cancel this" or something else (a complaint about wording, a correction to how \
it was categorized, venting), OR when "that reminder"/"it"/"this one" could genuinely point at TWO OR MORE \
distinct recent items and {_S} hasn't said which. Only ask when more than one real candidate exists — if only one \
open item could possibly be meant, resolve it silently; asking about an unambiguous reference is friction, not \
safety. Default when multiple candidates exist: "that reminder" right after a [reminder]/[digest]/[email] ping \
means THAT ping, not an item discussed earlier in the conversation, unless {_S} names the earlier item explicitly. \
NEVER guess toward dropping, canceling, completing, or rescheduling the WRONG item — especially anything involving \
another named person — when genuinely torn between candidates; ask first in THAT case only. A wrong guess that \
touches is far worse than one extra question. Real incident (2026-07-19, jarvis): asked to reset a pushup count \
and set a reminder, the model also silently called complete_item on "Take clubs out of car" — an item the message \
never referenced at all, apparently pattern-matched from an unrelated "car"-adjacent item completed the day \
before. complete_item/update_item must ONLY ever target an item {_S} explicitly named or unambiguously referred to \
in THIS message — never an item that merely shares a word, a category, or general topic with something mentioned \
recently; when nothing in {_P} current message points at a specific item, don't touch any item at all. Use \
recurrence (weekly/monthly/yearly) for repeating things — the next occurrence spawns itself on check-off.

STATUS UPDATES: If {_S} mentions progress on a tracked item without confirming it's finished ("headed to X", \
"about to start X") do NOT complete_item on a guess — but never reply with a content-free pleasantry either. Say \
what happens to the item ("Have fun — I'll leave 'Go to Watchfest' on your list, tap done when you're back or just \
tell me"). A reply that doesn't mention the tracked item at all leaves {_O} unable to tell whether it's still being \
watched.

SPEAK: 1-3 short, complete, natural sentences ("I'll remind you tonight at 7:30" — never "pinged tonight", \
never a bare "Done."). Every confirmation names what changed. Obey the emoji_level and reply_length preferences; \
at most one emoji regardless. No headers, no sign-offs, no restating {_P} list unprompted.

CALM CORRECTLY: Name a feeling once, plainly, then go concrete — never "don't worry" / "breathe easy" / \
"you're all set"; blanket reassurance feeds the anxiety loop. Overwhelmed → exactly ONE next action anchored to \
a moment ("after work, text Sam"). Re-asked "did you save it?" → confirm once, briefly. Decisions → 2-3 plain \
sentences of trade-off, then ONE recommendation. Never shame overdue items: reschedule, shrink, or drop guilt-free.

MEMORY: The items and facts below are the complete permanent record; the chat scroll is windowed. Never say \
"this is our first chat" or deny prior conversations — answer "what did I tell you" from the state. Store \
durable personal facts with remember_fact. {_S.upper()} IS ALWAYS RIGHT about {_P} own life: a correction means the stored \
fact is wrong — replace it via replaces_fact_id and never repeat or defend the old value. History lines starting \
[reminder]/[digest]/[email] are scheduled pings YOU sent {_O} — not things {_S} said. If {_S} replies to one \
("headed to watch fest"), that [reminder] line just above is what {_S}'s reacting to. NEVER GUESS about your own \
past turns — if {_S} says a change you made was wrong, that means you changed the WRONG thing, not that nothing \
happened; don't claim "I didn't actually make that change" unless the item's CURRENT state (below) proves it. \
Fix it by acting on the current state and naming the new result — don't narrate an unverified story about what \
did or didn't happen before.

{_P.upper()} RULES: Style, reminder cadence, quiet hours, digests (time, content, or off entirely), email watching, and \
"leave me alone for a while" (notifications_enabled — pauses every reminder, digest, and email alert at once, \
nothing lost, resumes on request) are all {_PP} to set — change them with set_preference and confirm in one \
sentence. emoji_level and reminder_overdue_label genuinely govern reminders/digests/email alerts, not just this \
chat. Custom reminder wording (E2) is real: set_preference/update_item's reminder_text applies {_P} exact phrasing \
to every future ping on an item ("0/100 done today" instead of "Reminder: pushups"). daily_ping_cap limits how \
many proactive pings {_S} gets per day (state block shows pings today: N (cap M)) — if {_S} asks why {_S} got pinged \
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
with both a routine fact and a due item must mention the item, not just the routine. If {_S} asks why something \
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
            "description": f"Save a new to-do, errand, order to track, social plan, or idea to {_P} list. Use once per distinct item.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short imperative title, e.g. 'Reply to Sarah's text'"},
                    "details": {"type": "string", "description": f"Any context worth keeping (links, names, amounts, what {_S} said)"},
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
                        "description": f"Set only if {_S} describes something that repeats on its own cycle (a daily habit = daily, rent on the 1st = monthly, car registration every July = yearly, trash night = weekly). Requires due_at. When {_S} checks it off, the next occurrence is created automatically.",
                    },
                    "recurrence_until": {
                        "type": "string",
                        "description": f"ISO date the recurrence series should stop (e.g. {_S} says 'this summer' -> pick a sensible end like late September). Only meaningful with recurrence set; omit for an open-ended series.",
                    },
                    "reminder_text": {
                        "type": "string",
                        "description": f"{_Pcap} exact custom wording for every future ping on this item, verbatim, instead of the default 'Reminder: {{title}}' — e.g. {_S} wants pushup pings to say '0/100 done today'.",
                    },
                },
                "required": ["title", "category", "priority"],
            },
        },
        {
            "name": "complete_item",
            "description": f"Mark an item done ({_S} says {_S} did it, or it's clearly no longer needed).",
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
                        "description": f"{_Pcap} exact custom wording for every future ping on this item, verbatim, instead of the default template.",
                    },
                },
                "required": ["item_id"],
            },
        },
        {
            "name": "set_preference",
            "description": f"Change {ASSISTANT_NAME}'s own behavior when {_S} asks: reminder aggressiveness, nag cadence, quiet hours, digest times.",
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
            "description": f"Store a durable fact about {_P} life (people, preferences, routines, ongoing situations). Not for to-dos.",
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
                "description": f"Create a real event on {_P} Google Calendar.",
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


class ToolResult(NamedTuple):
    """Structured tool outcome — replaces the old convention of sniffing
    English prefixes ("Tool error", "Unknown", "Invalid") on the returned
    string to decide success. That heuristic was load-bearing for the
    empty-promise guard and the captured/did tracking that drives it: any
    new error phrasing that didn't start with one of those exact words would
    have been silently read as success. `ok` is now the single source of
    truth for callers; `message` is only ever shown to the model/user."""
    ok: bool
    message: str


def _run_tool(name: str, inp: dict) -> ToolResult:
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
            return ToolResult(True, f"Saved as item #{item_id}.")
        if name == "complete_item":
            memory.complete_item(inp["item_id"])
            return ToolResult(True, "Marked done.")
        if name == "update_item":
            fields = {k: v for k, v in inp.items() if k != "item_id"}
            remind_at = fields.pop("remind_at", None)
            if fields.get("recurrence") == "none":
                fields["recurrence"] = None
            if fields.get("recurrence_until") == "":
                fields["recurrence_until"] = None
            if "title" in fields and "reminder_text" not in fields:
                # Items with a custom reminder_text (the "N/100 done today"
                # progress-tracking pattern) keep title and reminder_text
                # mirrored by convention — the scheduler shows reminder_text
                # over title at ping time, so a title-only update silently
                # freezes the actual reminder wording. Real incident
                # (2026-07-19, jarvis): "Did 25 pushups" updated the title to
                # "25/100..." but left reminder_text at "0/100...", so the
                # next reminder still showed the stale count. Code-level
                # guard, not a prompt instruction — the same divergence has
                # now happened twice despite prompt wording alone.
                current = memory.get_item(inp["item_id"])
                if current and current["reminder_text"]:
                    fields["reminder_text"] = fields["title"]
            if fields:
                memory.update_item(inp["item_id"], **fields)
            if remind_at is not None:
                # replaces pending pings, not a column on items — see v1.5 split
                # in scheduler.py (scheduled reminders vs the nag chase)
                memory.replace_item_reminders(inp["item_id"], remind_at)
            return ToolResult(True, "Updated.")
        if name == "set_preference":
            key, value = inp["key"], str(inp["value"]).strip()
            if key == "reminder_style" and value not in ("gentle", "normal", "persistent"):
                return ToolResult(False, "Invalid style — use gentle, normal, or persistent.")
            if key == "emoji_level" and value not in ("none", "minimal", "normal"):
                return ToolResult(False, "emoji_level must be none, minimal, or normal.")
            if key == "reply_length" and value not in ("short", "normal"):
                return ToolResult(False, "reply_length must be short or normal.")
            if key == "digest_style" and value not in ("smart", "plain"):
                return ToolResult(False, "digest_style must be smart or plain.")
            if key == "digest_show_completed" and value not in ("yes", "no"):
                return ToolResult(False, "digest_show_completed must be yes or no.")
            if key.endswith("_digest_enabled") and value not in ("yes", "no"):
                return ToolResult(False, f"{key} must be yes or no.")
            if key in ("notifications_enabled", "gmail_watch_enabled", "reminder_overdue_label") and value not in ("yes", "no"):
                return ToolResult(False, f"{key} must be yes or no.")
            if key.endswith("_hour") and not (value.isdigit() and 0 <= int(value) <= 23):
                return ToolResult(False, "Hour must be 0-23.")
            if key.endswith("_time"):
                try:
                    h, m = value.split(":")
                    assert 0 <= int(h) <= 23 and 0 <= int(m) <= 59
                except (ValueError, AssertionError):
                    return ToolResult(False, "Time must be HH:MM (24h).")
            if (key.startswith("nag_interval") or key in ("max_nags", "daily_ping_cap")) and not value.isdigit():
                return ToolResult(False, "Must be a whole number.")
            memory.set_setting("pref_" + key, value)
            return ToolResult(True, f"Preference updated: {key} = {value}.")
        if name == "remember_fact":
            replaced = inp.get("replaces_fact_id")
            if replaced:
                memory.delete_fact(replaced)
            memory.add_fact(inp["fact"], inp.get("category", "general"))
            return ToolResult(True, "Old fact replaced." if replaced else "Remembered.")
        if name == "create_calendar_event":
            eid = gcal.create_event(inp["title"], inp["start"], inp.get("end"), inp.get("notes", ""))
            return ToolResult(True, f"Event created (id {eid}).")
        if name == "update_calendar_event":
            gcal.update_event(inp["event_id"], inp.get("title"), inp.get("start"), inp.get("end"), inp.get("notes"))
            return ToolResult(True, "Event updated.")
        if name == "delete_calendar_event":
            gcal.delete_event(inp["event_id"])
            return ToolResult(True, "Event deleted.")
        return ToolResult(False, f"Unknown tool {name}.")
    except Exception as e:
        log.exception("tool %s failed", name)
        return ToolResult(False, f"Tool error: {e}")


def _state_block() -> str:
    """Items/facts/prefs/calendar — everything EXCEPT the current timestamp, so
    this block is byte-identical between two turns where nothing changed and
    can carry its own cache breakpoint. The timestamp changes every single
    turn by definition; folding it in here used to bust that cache on every
    message (see respond()'s cache_control comment)."""
    facts = memory.all_facts()[-60:]  # cap: facts accumulate for years; keep prompt bounded
    facts_txt = "\n".join(f"- [f{f['id']}] {f['content']}" for f in facts) or f"(nothing yet — {_S}'s new; learn {_P} name early)"
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
        f"What you know about {_O}:\n{facts_txt}\n\n"
        f"{_Pcap} open items:\n{items_txt}\n\n"
        f"{_Pcap} calendar (next 7 days):\n{cal_txt}\n\n"
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
            ok = result.ok
            if block.name == "capture_item" and ok:
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
            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result.message})
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
            f"(automated system check, not a message from {_O} — {_S} has not seen your "
            "last reply yet and did not correct you: your last reply reads like it's "
            f"claiming a change happened, but no tool call succeeded this turn. Look at "
            f"what {_S} actually asked. Three honest outcomes are possible, pick whichever "
            "is true: (1) something genuinely needs doing — make the real tool call(s) "
            "now and confirm; (2) you can't do it — say so plainly; (3) nothing actually "
            f"needed fixing — {_S} asked a question, not for a change, and your wording "
            "just sounded like a claim. In case (3), do NOT invent a tool call to have "
            f"something to point to — just answer {_P} question accurately with no action "
            "at all. Whichever it is, do not say 'you're right', don't reference this "
            f"check, don't apologize for a mistake {_S} hasn't actually raised — reply as "
            f"your first and only response to {_O}.)"
        )}]})
        resp = _create(messages)
        _execute_tool_calls(resp.content)
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        # Real incident (2026-07-18, jarvis): the corrective round-trip itself
        # produced a second hollow claim ("You're back to 25/100 — next
        # reminder at 5:02pm") with no tool call behind it either, three
        # times in one conversation, and that second lie went straight to
        # her uncaught since nothing re-checked the do-over. One retry is
        # all this guard spends — if it's still just talk, don't forward a
        # second hallucinated confirmation; say so plainly instead.
        if text and not captured and not did and (claims_change(text) or llm_claims_change(text)):
            log.warning("empty-promise guard tripped AGAIN on the corrective retry (incident #%d today): %r", n, text[:600])
            # Neutral on purpose: this path fires both when she asked for a
            # change (resending is the right ask) AND when she's asking a
            # plain diagnostic question about something already wrong (real
            # incident, 2026-07-19: "why did you mark the car clubs done?" —
            # "mind sending it again?" made no sense as a reply to a question,
            # nothing to resend). Don't presume which one this is.
            text = "Something's not adding up between what I said and what's actually saved — let me know how you'd like this handled."

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
                f"(automated system check, not a message from {_O} — {_S} has not seen your "
                f"reply yet: {_P} message may have contained items you did not capture: "
                + "; ".join(missed[:5]) + f". If any of these are genuinely distinct new items "
                f"{_S} asked to save, capture them now and confirm everything in one reply. If "
                "they are duplicates of items you already saved or not actually asks, change "
                "nothing and simply restate your confirmation. Reply as your first and only "
                f"response to {_O} — do not mention this check.)"
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

Authoritative for today's digest — already correctly filtered by code, do not
second-guess, add to, or drop from these two lists; your job is order and
tone only, not filtering:
Due today: {due_today_list}
Spare energy (undated, optional, offer only if non-empty): {spare_energy_list}

Write the owner's morning digest for {today}. Rules:
- Using ONLY the "Due today" list above, order those items into a realistic sequence given their times (see the state below for exact times); name the ONE thing that matters most first.
- Then a "Heads up:" section ONLY if you find real problems in the next 3 days using the state below: time conflicts, an item missing obvious prep (a gift not bought before its wrapping day), or something due on a day off. Skip the section entirely if there are none — never invent a concern.
- If the spare-energy list above is non-empty, offer those exact items as "if there's spare energy" — nothing else belongs in that section, and nothing from it belongs in "due today" or vice versa.
- Plain text, no markdown headers/bold, max 12 short lines, warm but not chatty. Do not greet by name. Never claim anything was changed or handled — this is a read-only summary.
- End with exactly this line: ({n_open} things safely on the list — say "list" anytime.)"""


def compose_digest(due_today_titles: list = None, spare_energy_titles: list = None, n_open: int = None) -> str | None:
    """Model-written morning digest: plan-shaped, conflict-aware. The two
    title lists are the SAME due-today/spare-energy filtering scheduler.py's
    plain digest uses (scheduler.digest_buckets) — handing them over as
    ground truth means the model only writes sequencing/tone/heads-up prose
    around already-correct data, instead of re-deriving "what's due today"
    itself from the raw state block. That re-derivation is exactly what drifted
    out of sync with the Python filter after a live spare-energy bug fix
    only touched one of the two implementations. Returns None on any failure
    or if spending is capped — the caller ALWAYS has the plain f-string
    digest as fallback, so a digest can never be lost to this feature."""
    if today_spend() >= HARD_CAP_USD:
        log.warning("smart digest skipped: daily hard cap reached")
        return None
    now = datetime.now(TZ)
    if due_today_titles is None or spare_energy_titles is None or n_open is None:
        # standalone call (tests, ad-hoc use) — fall back to computing fresh
        import scheduler
        items = memory.open_items()
        due_today, spare_energy = scheduler.digest_buckets(items, now)
        due_today_titles = [i["title"] for i in due_today]
        spare_energy_titles = [i["title"] for i in spare_energy]
        n_open = len(items)
    try:
        text = quick(
            DIGEST_PROMPT.format(
                state=_state_block(),
                today=now.strftime("%A, %B %-d"),
                due_today_list=", ".join(due_today_titles) or "(none)",
                spare_energy_list=", ".join(spare_energy_titles) or "(none)",
                n_open=n_open,
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
