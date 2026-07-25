"""Reminder engine + daily digests.

v1.5 split (see SONNET_HANDOFF_v1.5.md): a SCHEDULED reminder (a moment she
asked to be pinged — `reminders` table, one-shot) is now a different thing
from a NAG (the repeating "still not done" chase, item.next_remind_at /
remind_count, which only starts once due_at has actually passed). Before
this split they shared one column, which is why a party-time ping and an
overdue chase used to be indistinguishable, and why "remind me at 3pm" could
turn into an infinite nag chain with no relationship to any deadline.
Quiet hours are respected except P5. Everything proactive is budgeted
(daily_ping_cap) and logged to the messages table so the brain can see its
own pings."""
import asyncio
import json
import logging
from datetime import datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import gcal
import memory
from config import EVENING_DIGEST, MORNING_DIGEST, QUIET_END_HOUR, QUIET_START_HOUR, TZ

log = logging.getLogger("penny.scheduler")

# minutes between repeat nags, by priority (0 = remind once, then digests only)
ESCALATION_MIN = {5: 30, 4: 120, 3: 360, 2: 1440, 1: 0}
MAX_NAGS = 5  # after this many pings, fall back to digests only (was 12 — the flood's biggest lever)
NAG_WINDOW_HOURS = 24  # a nag chain stops chasing this long past due_at; item falls to the stale sweep instead
DAILY_PING_CAP = 10  # soft cap on proactive sends/day; scheduled (one-shot) reminders and P5 are always exempt
SPARE_ENERGY_SHOWN = 3   # how many undated backlog items a digest offers (rotated — see digest_buckets)
BUNDLE_ROWS_SHOWN = 5    # items given their own button row in one bundle; the rest are named in a tail line
STALE_SWEEP_COOLDOWN_DAYS = 3  # how long before the same expired item may be swept again

# She can change all of this by just asking Penny — stored as pref_* settings.
STYLE_MULT = {"gentle": 2.0, "normal": 1.0, "persistent": 0.5}


def pref(key, default):
    v = memory.get_setting("pref_" + key)
    return v if v is not None else default


def pref_int(key, default) -> int:
    try:
        return int(pref(key, default))
    except (TypeError, ValueError):
        return default


def escalation_minutes(priority: int) -> int:
    base = pref_int(f"nag_interval_p{priority}", ESCALATION_MIN.get(priority, 360))
    mult = STYLE_MULT.get(pref("reminder_style", "normal"), 1.0)
    return int(base * mult) if base else 0


def max_nags() -> int:
    n = pref_int("max_nags", MAX_NAGS)
    if pref("reminder_style", "normal") == "gentle":
        n = min(n, 4)
    return n


def icon(e: str) -> str:
    """Decorative emoji for scheduled/templated messages (reminders, digests,
    gmail alerts) — these are plain Python f-strings, never seen by the model,
    so emoji_level has to be checked here explicitly or it's silently ignored
    outside conversation. 'minimal' (the default) and 'none' both suppress
    decoration here: someone who set minimal does not expect scheduled pings
    to stay fully decorated just because the model didn't write them."""
    return f"{e} " if pref("emoji_level", "minimal") == "normal" else ""


def quiet_now(now: datetime) -> bool:
    h = now.hour
    start = pref_int("quiet_start_hour", QUIET_START_HOUR)
    end = pref_int("quiet_end_hour", QUIET_END_HOUR)
    if start > end:  # e.g. 22 -> 8, spans midnight
        return h >= start or h < end
    return start <= h < end


def pings_today() -> int:
    return memory.counter_today("pings")


def bump_pings(n: int = 1) -> int:
    return memory.bump_counter("pings", n)


def log_proactive(prefix: str, text: str):
    """Every scheduled send is logged to the messages table so the brain can
    see its own pings — a reply like 'headed to watch fest' now has its
    trigger in history instead of looking unprompted."""
    memory.log_msg("assistant", f"[{prefix}] {text}")


# events (social/appointment) happen at a fixed time or don't happen at all —
# "snooze 1h" and "push to tomorrow" don't mean anything for trivia night or a
# dinner reservation, only for a task you can actually do at a different time
EVENT_CATEGORIES = ("social", "appointment")


def item_button_row(item_id: int, category: str = "task", mute: bool = False) -> list:
    if category in EVENT_CATEGORIES:
        row = [InlineKeyboardButton("✅ Done", callback_data=f"done:{item_id}")]
    else:
        row = [
            InlineKeyboardButton("✅ Done", callback_data=f"done:{item_id}"),
            InlineKeyboardButton("⏰ +1h", callback_data=f"snooze:{item_id}:60"),
            InlineKeyboardButton("🌙 Tomorrow", callback_data=f"tmrw:{item_id}"),
        ]
    if mute:
        row.append(InlineKeyboardButton("🔕", callback_data=f"mute:{item_id}"))
    return row


def item_buttons(item_id: int, category: str = "task", mute: bool = False) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([item_button_row(item_id, category, mute)])


def checklist_markup(items, max_buttons=8) -> InlineKeyboardMarkup:
    """Rows of tappable '✓ n' buttons for the numbered list message."""
    buttons, row, rows = 0, [], []
    for idx, it in enumerate(items, 1):
        if buttons >= max_buttons:
            break
        row.append(InlineKeyboardButton(f"✓ {idx}", callback_data=f"done:{it['id']}"))
        buttons += 1
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows) if rows else None


def render_list(items) -> str:
    show_overdue = pref("reminder_overdue_label", "yes") == "yes"
    if not items:
        tail = icon("🎉").strip()
        return f"Your list is empty. Nothing is waiting on you right now{' ' + tail if tail else ''}"
    flame = {5: "🔴", 4: "🟠", 3: "🟡", 2: "🟢", 1: "⚪️"}
    header = icon("📋").strip()
    lines = [f"{header + ' ' if header else ''}Your list (tap ✓ to check off):", ""]
    now = datetime.now(TZ)
    warn = icon("⚠️")
    for idx, it in enumerate(items, 1):
        due = ""
        d = memory.parse_dt(it["due_at"])
        if d:
            if d < now:
                due = f" — {warn}overdue" if show_overdue else ""
            else:
                due = f" — due {d.strftime('%a %-m/%-d %-I:%M%p').replace(':00PM','PM').replace(':00AM','AM')}"
        repeat = " 🔁" if it["recurrence"] else ""
        progress = f" — {it['progress_current'] or 0}/{it['progress_target']}" if it["progress_target"] is not None else ""
        lines.append(f"{idx}. {flame.get(it['priority'], '🟡')} {it['title']}{progress}{due}{repeat}")
    return "\n".join(lines)


def _fill_progress(text: str, it) -> str:
    """Substitutes {current}/{target} placeholders in a custom reminder_text
    against the item's live progress_current/progress_target columns — the
    text itself is a stable template set once at creation, never rewritten
    per update, so it can never drift out of sync with the real count the
    way two independently hand-edited strings did."""
    if it["progress_target"] is None:
        return text
    return (text.replace("{current}", str(it["progress_current"] or 0))
                .replace("{target}", str(it["progress_target"])))


def _reminder_text(entry: dict, show_overdue: bool) -> str:
    """entry: {'item': row, 'kind': 'scheduled'|'nag'}. A custom reminder_text
    on the item (E2) overrides the default template verbatim for BOTH kinds —
    it's her wording for every future ping on this item, not just one."""
    it = entry["item"]
    if it["reminder_text"]:
        return _fill_progress(it["reminder_text"], it)
    if it["progress_target"] is not None:
        progress = f" — {it['progress_current'] or 0}/{it['progress_target']}"
    else:
        progress = ""
    if entry["kind"] == "scheduled":
        return f"{icon('⏰')}Reminder: {it['title']}{progress}"
    when = f" ({icon('⚠️')}overdue)" if show_overdue else ""
    prefix = f"{icon('⏰')}Nudge #{it['remind_count'] + 1}"
    return f"{prefix}: {it['title']}{progress}{when}"


async def check_reminders(context):
    """Runs every minute. Collects everything due this tick — scheduled
    one-shot pings and nag escalations, separately — and sends one message
    if there's more than one due at once instead of flooding N messages."""
    chat_id = memory.get_setting("owner_chat_id")
    if not chat_id or pref("notifications_enabled", "yes") == "no":
        return  # due items stay due — deferred, not lost, same as quiet hours
    now = datetime.now(TZ)
    # Before deciding what's due: move any recurring item whose whole day has
    # passed onto its next occurrence. A missed day used to end the series
    # outright (the next occurrence is only born on check-off), leaving a
    # permanently-overdue row that digests then described as "today's". Done
    # here, ahead of the due calculation, so nothing is ever chased or narrated
    # against a date that's already gone.
    try:
        if rolled := memory.roll_forward_recurring(now):
            log.info("rolled %d missed recurring occurrence(s) forward: %s", len(rolled), rolled)
    except Exception:
        log.exception("recurrence roll-forward failed (reminders continue normally)")
    quiet = quiet_now(now)
    show_overdue = pref("reminder_overdue_label", "yes") == "yes"
    cap = pref_int("daily_ping_cap", DAILY_PING_CAP)
    budget_left = cap - pings_today()

    due = {}  # item_id -> entry, de-duplicated (an item can't fire twice in one tick)
    for r in memory.due_scheduled_reminders(now):
        if quiet and r["priority"] < 5:
            continue  # will fire right after quiet hours end
        due[r["id"]] = {"item": r, "kind": "scheduled", "reminder_id": r["reminder_id"]}

    for it in memory.due_nags(now, grace_minutes_fn=escalation_minutes):
        if it["id"] in due:
            continue
        if quiet and it["priority"] < 5:
            continue
        d = memory.parse_dt(it["due_at"])
        if d and (now - d).total_seconds() > NAG_WINDOW_HOURS * 3600:
            # past the chase window: stop nagging, let the morning digest's
            # stale sweep surface it gently instead of pinging forever.
            # remind_count must end up >0 here even if this is the very first
            # check (bot was down, item surfaced already 24h+ overdue) — that's
            # the signal due_nags() uses to tell "never nagged" apart from
            # "deliberately stopped nagging"; leaving it at 0 would make this
            # item look fresh again on the very next tick.
            memory.update_item(it["id"], next_remind_at=None, remind_count=max(it["remind_count"], 1))
            continue
        if it["priority"] < 5 and budget_left <= 0:
            continue  # daily cap respected for nags (never for scheduled/P5 — see handoff B4)
        due[it["id"]] = {"item": it, "kind": "nag", "reminder_id": None}

    if not due:
        return
    entries = list(due.values())

    try:
        if len(entries) == 1:
            await _send_single(context, chat_id, entries[0], show_overdue)
        else:
            await _send_bundle(context, chat_id, entries, show_overdue)
    except Exception:
        log.exception("failed to send reminder(s)")
        return

    for e in entries:
        if e["kind"] == "scheduled":
            memory.mark_reminder_fired(e["reminder_id"])
        else:
            it = e["item"]
            nag = it["remind_count"]
            interval = escalation_minutes(it["priority"])
            if interval and nag + 1 < max_nags():
                nxt = (now + timedelta(minutes=interval)).isoformat(timespec="seconds")
            else:
                nxt = None
            memory.update_item(it["id"], next_remind_at=nxt, remind_count=nag + 1)


async def _send_single(context, chat_id, entry: dict, show_overdue: bool):
    it = entry["item"]
    text = _reminder_text(entry, show_overdue)
    # the mute button only makes sense on a repeating nag — a scheduled
    # reminder is a one-shot moment she asked for, nothing to mute yet
    markup = item_buttons(it["id"], it["category"], mute=(entry["kind"] == "nag" and it["remind_count"] >= 1))
    await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)
    log_proactive("reminder", text)
    bump_pings(1)


async def _send_bundle(context, chat_id, entries: list, show_overdue: bool):
    # Cap the button rows. Bundling one notification instead of N was the right
    # call, but an uncapped bundle became its own kind of flood: 8 items with 8
    # four-button rows on 2026-07-25, 7 items on 07-21 — a wall that reads as
    # "everything is late" and gets dismissed wholesale. The overflow is still
    # NAMED (never silently dropped, same rule the digest cutoff follows), it
    # just doesn't get its own row of buttons; the digest and "list" cover it.
    shown, overflow = entries[:BUNDLE_ROWS_SHOWN], entries[BUNDLE_ROWS_SHOWN:]
    lines = [f"{len(entries)} things need you right now:", ""]
    for idx, e in enumerate(shown, 1):
        lines.append(f"{idx}. {e['item']['title']}")
    if overflow:
        lines.append("")
        lines.append("Also waiting: " + ", ".join(e["item"]["title"] for e in overflow)
                     + " — say \"list\" to work through these.")
    text = "\n".join(lines)
    # one message, one notification (the whole point of bundling) — but a full
    # Done/+1h/Tomorrow row per item instead of a bare checkmark, so what you
    # can do with a reminder doesn't depend on how many fired at once
    rows = [
        item_button_row(e["item"]["id"], e["item"]["category"], mute=(e["kind"] == "nag" and e["item"]["remind_count"] >= 1))
        for e in shown
    ]
    markup = InlineKeyboardMarkup(rows)
    await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)
    log_proactive("reminder", text)
    bump_pings(1)  # one notification event, however many items it covers


async def digest_tick(context):
    """Runs every minute; fires digests at their (chat-adjustable) times.
    If the Mac was asleep at digest time, sends within a 2h grace window."""
    if pref("notifications_enabled", "yes") == "no":
        return  # deferred, not lost — resumes firing once turned back on
    now = datetime.now(TZ)
    today = now.date().isoformat()
    for name, default, fn in (
        ("morning", MORNING_DIGEST, morning_digest),
        ("evening", EVENING_DIGEST, evening_digest),
    ):
        if pref(f"{name}_digest_enabled", "yes") == "no":
            continue
        t = pref(f"{name}_digest_time", default)
        try:
            h, m = (int(x) for x in t.split(":"))
        except (ValueError, AttributeError):
            h, m = (int(x) for x in default.split(":"))
        sched = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if memory.get_setting(f"{name}_digest_sent") == today or now < sched:
            continue
        if now > sched + timedelta(hours=2):
            # missed the whole grace window (Mac asleep) — skip today, don't fire at odd hours
            memory.set_setting(f"{name}_digest_sent", today)
            continue
        try:
            await fn(context)
            # mark sent only AFTER success: a transient Telegram/network failure
            # retries on the next minute tick instead of losing the day's digest
            memory.set_setting(f"{name}_digest_sent", today)
        except Exception:
            log.exception("%s digest failed (will retry within the grace window)", name)


def digest_buckets(items: list, now: datetime) -> tuple:
    """The one place 'what's due today' and 'what's genuinely spare-energy
    eligible' get computed — used by both the plain digest and fed into the
    smart digest's prompt, so there is exactly one filtering rule instead of
    a Python version and a re-derived English-prose version that can drift
    out of sync (confirmed: the spare-energy fix here once only touched this
    function, leaving the smart prompt's copy of the same rule stale)."""
    def _pings(i):
        return [p for p in (memory.parse_dt(t) for t in memory.pending_reminder_times(i["id"])) if p]

    def _is_today(i) -> bool:
        d = memory.parse_dt(i["due_at"])
        if d:
            return d.date() <= now.date()
        # An item with NO due date but a ping scheduled for today IS a today
        # item — the ping is the only date it has. Live hole (jarvis,
        # 2026-07-25): "remind me to respond to Brian at 7pm tonight" saved a
        # 7pm reminder and no due_at, so the item pinged correctly but was
        # invisible to every "what's due today" calculation, and would have
        # been offered under "if there's spare energy" instead. Note the
        # asymmetry with a night-before ping for a LATER event (Polo Club:
        # pings 9pm tonight, due 10:30am tomorrow) — that has a due date, so
        # the branch above already correctly keeps it out of today.
        return any(p.date() == now.date() for p in _pings(i))

    due_today = [i for i in items if _is_today(i)]
    # "spare energy" is for flexible, undated backlog work she could get ahead
    # on. Anything with a specific future due_at — a dinner reservation, trivia
    # night, a Sunday-only recurring email, a reminder tied to a day later this
    # week — can't be done early no matter how much energy she has, so it must
    # never land here just because it happens to not be due *today*. A
    # category blocklist (social/appointment only) isn't enough: a dated task
    # in any other category (e.g. "ask boss to leave early" for a future day)
    # slips right through it.
    #
    # Rotated, not just capped. Undated backlog never expires on its own, so a
    # fixed [:3] slice of a stably-ordered list offers the SAME three items
    # every single morning — penny's "let Brooks pick timing for birthday
    # sushi" and "check Partiful reminders" led this section every day for over
    # two weeks (nagged 7 and 8 times), which trains the reader to skip the
    # section entirely. Rotating by day-of-year gives every undated item a turn
    # and costs nothing; the weekly tidy-up (maintenance.py) is still what
    # eventually asks whether to keep or drop them.
    # ...and a pending ping on a future day disqualifies an item from
    # spare-energy for exactly the same reason a future due_at does: it's
    # scheduled, not flexible backlog she could pull forward.
    pool = [i for i in items if i not in due_today and not i["due_at"] and not _pings(i)]
    if len(pool) > SPARE_ENERGY_SHOWN:
        offset = (now.timetuple().tm_yday * SPARE_ENERGY_SHOWN) % len(pool)
        pool = (pool + pool)[offset:offset + SPARE_ENERGY_SHOWN]
    return due_today, pool[:SPARE_ENERGY_SHOWN]


def digest_entry_label(item, now: datetime) -> str:
    """A due-today title with its real date attached, and an explicit overdue
    marker — what the smart digest gets instead of a bare title.

    "Due today" deliberately includes anything due on or before today (an
    overdue item genuinely still needs you), but handing the model only titles
    left it to guess when each one was for, and it guessed "today". Penny's
    digests called a Tuesday movie "tonight at 7 PM" on both the Wednesday and
    the Thursday after it happened. Now the bucket says so in words."""
    d = memory.parse_dt(item["due_at"])
    if not d:
        return item["title"]
    if d.date() < now.date():
        return f"{item['title']} (overdue, was due {d.strftime('%A %B %-d')} at {d.strftime('%-I:%M %p')})"
    return f"{item['title']} (due today at {d.strftime('%-I:%M %p')})"


async def morning_digest(context):
    chat_id = memory.get_setting("owner_chat_id")
    if not chat_id:
        return
    now = datetime.now(TZ)
    items = memory.open_items()
    due_today, spare_energy = digest_buckets(items, now)

    # Model-composed digest (default): plan-shaped, sequences the day, flags
    # real conflicts/missing prep. compose_digest() returns None on ANY failure
    # or budget stop, in which case the plain f-string build below ships instead
    # — a digest can never be lost to this feature. digest_style=plain opts out.
    if pref("digest_style", "smart") == "smart":
        import brain  # deferred: brain imports this module at load time
        smart = await asyncio.to_thread(
            brain.compose_digest,
            [digest_entry_label(i, now) for i in due_today],
            [i["title"] for i in spare_energy],
            len(items),
        )
        if smart:
            text = f"{icon('☀️')}Morning! It's {now.strftime('%A, %B %-d')}.\n\n{smart}"
            await context.bot.send_message(chat_id=chat_id, text=text)
            log_proactive("digest", text)
            bump_pings(1)
            await _stale_sweep(context, chat_id)
            return

    parts = [f"{icon('☀️')}Morning! It's {now.strftime('%A, %B %-d')}."]

    if gcal.enabled():
        # Google HTTP call off the event loop — a slow API must not freeze the bot
        today = await asyncio.to_thread(gcal.upcoming_events, 1)
        if today:
            parts.append("\nToday:")
            for e in today:
                t = e["start"][11:16] if len(e["start"]) > 10 else "all day"
                parts.append(f"  • {t} — {e['title']}")
        else:
            parts.append("\nNothing on the calendar today.")

    if due_today:
        parts.append("\nNeeds you today:")
        # a silent [:N] cutoff here once dropped two genuinely-due items off the
        # bottom of the list with zero indication anything was missing — cap
        # the same way the list footer does, with a visible "+N more" instead
        shown, rest = due_today[:8], due_today[8:]
        for i in shown:
            # same distinction the smart digest now gets: "due today" includes
            # things that were due days ago, and calling those today's is wrong
            d = memory.parse_dt(i["due_at"])
            late = f" (still open from {d.strftime('%b %-d')})" if d and d.date() < now.date() else ""
            parts.append(f"  • {i['title']}{late}")
        if rest:
            parts.append(f"  …+{len(rest)} more due today (say \"list\" to see everything)")
    if spare_energy:
        parts.append("\nIf there's spare energy:")
        for i in spare_energy:
            parts.append(f"  • {i['title']}")
    if not items:
        parts.append("\nYour list is clear.")
    else:
        parts.append(f"\n({len(items)} things safely on the list — say \"list\" anytime.)")
    text = "\n".join(parts)
    await context.bot.send_message(chat_id=chat_id, text=text)
    log_proactive("digest", text)
    bump_pings(1)
    await _stale_sweep(context, chat_id)


async def _stale_sweep(context, chat_id):
    """Items whose nag chain gave up (24h+ past due) get one gentle, guilt-free
    follow-up with checklist buttons — never re-nagged.

    Two live problems fixed here. (1) It named nothing: the body was a bare
    "These came and went" and the titles existed only inside the buttons, so
    the message carried no information on a lock screen or in a notification
    summary. (2) "Never re-nagged" was only true within a single morning — the
    sweep itself runs every morning, so the same expired items came back as a
    second ping right behind the digest, 7 mornings out of 7 for penny. An item
    now waits out a cooldown before it may be swept again."""
    today = datetime.now(TZ).date()
    seen = {}
    try:
        seen = json.loads(memory.get_setting("stale_swept_on") or "{}")
    except ValueError:
        seen = {}  # unparseable (hand-edited/corrupt) — treat as "never swept"

    def _due_for_sweep(item) -> bool:
        last = memory.parse_dt(seen.get(str(item["id"])))
        return not last or (today - last.date()).days >= STALE_SWEEP_COOLDOWN_DAYS

    stale = [i for i in memory.overdue_stale_items(NAG_WINDOW_HOURS) if _due_for_sweep(i)][:5]
    if not stale:
        return
    lines = ["These came and went — clear them, or give them a new time:", ""]
    for it in stale:
        d = memory.parse_dt(it["due_at"])
        lines.append(f"  • {it['title']}" + (f" (was due {d.strftime('%b %-d')})" if d else ""))
    stale_text = "\n".join(lines)
    await context.bot.send_message(
        chat_id=chat_id, text=stale_text, reply_markup=checklist_markup(stale, max_buttons=len(stale))
    )
    log_proactive("digest", stale_text)
    bump_pings(1)
    # record the sweep only after a successful send, so a failed message doesn't
    # start a 3-day cooldown on items the owner never actually saw
    seen.update({str(i["id"]): today.isoformat() for i in stale})
    memory.set_setting("stale_swept_on", json.dumps(seen))


async def evening_digest(context):
    chat_id = memory.get_setting("owner_chat_id")
    if not chat_id:
        return
    show_done = pref("digest_show_completed", "yes") == "yes"
    done = memory.completed_today()
    items = memory.open_items()
    parts = [f"{icon('🌙')}Evening check-in."]
    if show_done:
        if done:
            parts.append(f"\nYou knocked out {len(done)} thing{'s' if len(done) != 1 else ''} today:")
            for i in done[:8]:
                parts.append(f"  • {i['title']}")
        else:
            parts.append("\nNo check-offs today — that's fine, some days are like that.")
    if items:
        parts.append(f"\n{len(items)} open item{'s' if len(items) != 1 else ''}, all written down. None of it needs to live in your head tonight.")
    parts.append("\nAnything still in your head, drop it here before bed.")
    text = "\n".join(parts)
    await context.bot.send_message(chat_id=chat_id, text=text)
    log_proactive("digest", text)
    bump_pings(1)
