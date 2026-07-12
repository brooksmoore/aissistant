"""The Claude brain: personality, tools, and the conversation loop.
Everything she says flows through respond(); Claude decides what to capture,
remind, remember, and put on the calendar."""
import json
import logging
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
that doesn't exist; when in doubt, capture it.

JUDGE: Set priority 1-5 and remind_at yourself — remind at the USEFUL moment (evening before a morning thing, \
days before a birthday). One clarifying question is allowed — not just for ambiguous timing, but any time a \
message could mean either "drop/cancel this" or something else (a complaint about wording, a correction to how \
it was categorized, venting). NEVER guess toward dropping or canceling an item — especially one involving another \
named person — when the intent isn't unmistakable; ask first. A wrong guess that deletes a real commitment is \
far worse than one extra question. Use recurrence (weekly/monthly/yearly) for repeating things — the next \
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
fact is wrong — replace it via replaces_fact_id and never repeat or defend the old value.

HER RULES: Style, reminder cadence, quiet hours, digests (time, content, or off entirely), email watching, and \
"leave me alone for a while" (notifications_enabled — pauses every reminder, digest, and email alert at once, \
nothing lost, resumes on request) are all hers to set — change them with set_preference and confirm in one \
sentence. emoji_level and reminder_overdue_label genuinely govern reminders/digests/email alerts, not just this \
chat — if she wants a specific reminder phrased differently (not just less alarming, an actual custom format), \
say plainly there's no mechanism for that yet rather than promising it. Style feedback gets stored immediately, \
permanently. NEVER say something is changed, saved, paused, or turned off without a successful tool call behind \
it — if no tool covers the request, say so plainly instead of confirming a change that didn't happen.

KNOWLEDGE: Answer general-knowledge questions confidently (which stores carry what, typical return windows, \
cooking, travel basics). You have no live internet — share what you know and name the one thing worth verifying; \
never a bare "look it up yourself."

TIME: Resolve every relative date against the current datetime below to explicit ISO in {TIMEZONE}. When \
calendar tools are present, put anything with a fixed time on the real calendar (item = to-do, event = time block). \
When answering "what's on my calendar/list" for a day or range, a general fact (a routine, a usual day off) NEVER \
replaces or hides a specific item due that day — check every open item against the range and merge them in; a day \
with both a routine fact and a due item must mention the item, not just the routine.

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
                    "remind_at": {"type": "string", "description": "ISO local datetime for the first reminder ping. Omit for no ping (it still shows in daily digests)."},
                    "recurrence": {
                        "type": "string",
                        "enum": ["daily", "weekly", "monthly", "yearly"],
                        "description": "Set only if she describes something that repeats on its own cycle (a daily habit = daily, rent on the 1st = monthly, car registration every July = yearly, trash night = weekly). Requires due_at. When she checks it off, the next occurrence is created automatically.",
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
                    "remind_at": {"type": "string", "description": "next reminder ping, ISO local datetime"},
                    "status": {"type": "string", "enum": ["open", "dropped"]},
                    "recurrence": {
                        "type": "string",
                        "enum": ["none", "daily", "weekly", "monthly", "yearly"],
                        "description": "Set to start/change a repeat cycle, or 'none' to stop it repeating.",
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
                            "emoji_level",          # none | minimal | normal — applies to scheduled reminders/digests/email alerts too, not just chat replies
                            "reminder_overdue_label", # yes | no — whether reminders/list/digests call a late item "overdue" at all
                            "reply_length",         # short | normal
                            "digest_show_completed", # yes | no — list finished items in the evening digest
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
            )
            return f"Saved as item #{item_id}."
        if name == "complete_item":
            memory.complete_item(inp["item_id"])
            return "Marked done."
        if name == "update_item":
            fields = {k: v for k, v in inp.items() if k != "item_id"}
            if "remind_at" in fields:
                fields["next_remind_at"] = fields.pop("remind_at")
                fields["remind_count"] = 0
            if fields.get("recurrence") == "none":
                fields["recurrence"] = None
            memory.update_item(inp["item_id"], **fields)
            return "Updated."
        if name == "set_preference":
            key, value = inp["key"], str(inp["value"]).strip()
            if key == "reminder_style" and value not in ("gentle", "normal", "persistent"):
                return "Invalid style — use gentle, normal, or persistent."
            if key == "emoji_level" and value not in ("none", "minimal", "normal"):
                return "emoji_level must be none, minimal, or normal."
            if key == "reply_length" and value not in ("short", "normal"):
                return "reply_length must be short or normal."
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
            if (key.startswith("nag_interval") or key == "max_nags") and not value.isdigit():
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
    now = datetime.now(TZ)
    facts = memory.all_facts()[-60:]  # cap: facts accumulate for years; keep prompt bounded
    facts_txt = "\n".join(f"- [f{f['id']}] {f['content']}" for f in facts) or "(nothing yet — she's new; learn her name early)"
    items = memory.open_items()[:40]
    if items:
        lines = []
        for i in items:
            bits = [f"#{i['id']} [P{i['priority']}] ({i['category']}) {i['title']}"]
            if i["due_at"]:
                bits.append(f"due {i['due_at']}")
            if i["next_remind_at"]:
                bits.append(f"next ping {i['next_remind_at']}")
            if i["recurrence"]:
                bits.append(f"repeats {i['recurrence']}")
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
        f"ALL notifications (reminders/digests/email alerts) {'PAUSED' if scheduler.pref('notifications_enabled', 'yes') == 'no' else 'on'}"
        + (f", gmail watch {'OFF' if scheduler.pref('gmail_watch_enabled', 'yes') == 'no' else 'on'}" if gcal.enabled() else "")
    )
    return (
        f"\n--- CURRENT STATE ---\n"
        f"Now: {now.strftime('%A, %B %d, %Y at %I:%M %p')} ({TIMEZONE})\n\n"
        f"What you know about her:\n{facts_txt}\n\n"
        f"Her open items:\n{items_txt}\n\n"
        f"Her calendar (next 7 days):\n{cal_txt}\n\n"
        f"Your current behavior settings: {prefs_txt}\n"
    )


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

    if today_spend() >= 3 * DAILY_BUDGET_USD:
        # True hard cap: no API call at all. Reminders, buttons, and digests
        # keep working (they never use the API); only conversation rests.
        text = ("I've used up my thinking budget for today, so I'm resting until midnight — "
                "but everything is saved, and your reminders and check-off buttons still work fine.")
        memory.log_msg("assistant", text)
        log.warning("hard spend cap hit ($%.2f); conversation paused for today", today_spend())
        return text

    model = pick_model(user_text, has_image=bool(image_b64))
    tools = _tools()
    # Second cache breakpoint on the newest history message. The state block sits
    # between the cached personality and the messages, so this entry only survives
    # within one turn — which is exactly where it pays: a multi-tool brain-dump
    # re-reads the whole prefix once per tool round-trip. Default 5m TTL (a 1h TTL
    # here just doubles the write premium on an entry the next turn invalidates).
    if len(messages) > 1 and isinstance(messages[-1]["content"], str):
        messages[-1]["content"] = [
            {"type": "text", "text": messages[-1]["content"], "cache_control": {"type": "ephemeral"}}
        ]
    # The personality (+ tools) prefix is identical every turn -> prompt-cached (~90% off
    # input cost on hits). The state block is computed ONCE per turn: recomputing it
    # inside the tool loop changed the prompt prefix every iteration (minute timestamp,
    # freshly captured items) and busted the intra-turn cache on every round-trip.
    system = [
        {"type": "text", "text": PERSONALITY, "cache_control": {"type": "ephemeral", "ttl": "1h"}},
        {"type": "text", "text": _state_block()},
    ]

    captured = []   # item titles saved this turn, for the fallback confirmation
    did = []        # plain-English record of every successful non-capture action
    for _ in range(10):
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=4096,  # a big brain-dump can mean 10+ tool calls in one response
                system=system,
                tools=tools,
                messages=messages,
                extra_headers={"anthropic-beta": "extended-cache-ttl-2025-04-11"},
            )
        except anthropic.BadRequestError:
            # If the extended-TTL beta is ever retired, strip ttl hints and continue
            # on the standard 5-minute cache rather than failing her message.
            log.warning("extended cache TTL rejected; retrying with default cache")
            for b in system:
                if isinstance(b, dict) and b.get("cache_control", {}).get("ttl"):
                    b["cache_control"] = {"type": "ephemeral"}
            resp = client.messages.create(
                model=model, max_tokens=4096, system=system,
                tools=tools, messages=messages,
            )
        spent = _track_usage(model, resp.usage)
        # Execute every complete tool call we got — even if the response was
        # truncated (stop_reason "max_tokens"), partial progress must be saved.
        tool_results = []
        for block in resp.content:
            if block.type == "tool_use":
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
        if not tool_results:
            break
        messages.append({"role": "assistant", "content": resp.content})
        messages.append({"role": "user", "content": tool_results})

    log.info("turn: model=%s spend_today=$%.4f", model.split("-")[1], spent)

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
