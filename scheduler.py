"""Reminder engine + daily digests. Reminders escalate by priority and keep
nagging (gently) until she taps ✓ Done. Quiet hours are respected except P5."""
import logging
from datetime import datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import gcal
import memory
from config import EVENING_DIGEST, MORNING_DIGEST, QUIET_END_HOUR, QUIET_START_HOUR, TZ

log = logging.getLogger("penny.scheduler")

# minutes between repeat nags, by priority (0 = remind once, then digests only)
ESCALATION_MIN = {5: 30, 4: 120, 3: 360, 2: 1440, 1: 0}
MAX_NAGS = 12  # after this many pings, fall back to digests only

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


def _quiet(now: datetime) -> bool:
    h = now.hour
    start = pref_int("quiet_start_hour", QUIET_START_HOUR)
    end = pref_int("quiet_end_hour", QUIET_END_HOUR)
    if start > end:  # e.g. 22 -> 8, spans midnight
        return h >= start or h < end
    return start <= h < end


def item_buttons(item_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Done", callback_data=f"done:{item_id}"),
        InlineKeyboardButton("⏰ +1h", callback_data=f"snooze:{item_id}:60"),
        InlineKeyboardButton("🌙 Tomorrow", callback_data=f"tmrw:{item_id}"),
    ]])


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
    if not items:
        return "Your list is empty. Nothing is waiting on you right now 🎉"
    flame = {5: "🔴", 4: "🟠", 3: "🟡", 2: "🟢", 1: "⚪️"}
    lines = ["📋 Your list (tap ✓ to check off):", ""]
    now = datetime.now(TZ)
    for idx, it in enumerate(items, 1):
        due = ""
        d = memory.parse_dt(it["due_at"])
        if d:
            due = " — ⚠️ overdue" if d < now else f" — due {d.strftime('%a %-m/%-d %-I:%M%p').replace(':00PM','PM').replace(':00AM','AM')}"
        repeat = " 🔁" if it["recurrence"] else ""
        lines.append(f"{idx}. {flame.get(it['priority'], '🟡')} {it['title']}{due}{repeat}")
    return "\n".join(lines)


async def check_reminders(context):
    """Runs every minute. Sends due reminder pings with check-off buttons."""
    chat_id = memory.get_setting("owner_chat_id")
    if not chat_id:
        return
    now = datetime.now(TZ)
    for item in memory.due_reminders(now):
        if _quiet(now) and item["priority"] < 5:
            continue  # will fire right after quiet hours end
        d = memory.parse_dt(item["due_at"])
        when = ""
        if d:
            when = " (⚠️ overdue)" if d < now else f" (due {d.strftime('%a %-I:%M %p')})"
        nag = item["remind_count"]
        prefix = "⏰ Reminder" if nag == 0 else f"⏰ Nudge #{nag + 1}"
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"{prefix}: {item['title']}{when}",
                reply_markup=item_buttons(item["id"]),
            )
        except Exception:
            log.exception("failed to send reminder for item %s", item["id"])
            continue
        interval = escalation_minutes(item["priority"])
        if interval and nag + 1 < max_nags():
            nxt = (now + timedelta(minutes=interval)).isoformat(timespec="seconds")
        else:
            nxt = None
        memory.update_item(item["id"], next_remind_at=nxt, remind_count=nag + 1)


async def digest_tick(context):
    """Runs every minute; fires digests at their (chat-adjustable) times.
    If the Mac was asleep at digest time, sends within a 2h grace window."""
    now = datetime.now(TZ)
    today = now.date().isoformat()
    for name, default, fn in (
        ("morning", MORNING_DIGEST, morning_digest),
        ("evening", EVENING_DIGEST, evening_digest),
    ):
        t = pref(f"{name}_digest_time", default)
        try:
            h, m = (int(x) for x in t.split(":"))
        except (ValueError, AttributeError):
            h, m = (int(x) for x in default.split(":"))
        sched = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if memory.get_setting(f"{name}_digest_sent") == today or now < sched:
            continue
        memory.set_setting(f"{name}_digest_sent", today)
        if now <= sched + timedelta(hours=2):
            try:
                await fn(context)
            except Exception:
                log.exception("%s digest failed", name)


async def morning_digest(context):
    chat_id = memory.get_setting("owner_chat_id")
    if not chat_id:
        return
    now = datetime.now(TZ)
    items = memory.open_items()
    parts = [f"☀️ Morning! It's {now.strftime('%A, %B %-d')}."]

    if gcal.enabled():
        today = [e for e in gcal.upcoming_events(1)]
        if today:
            parts.append("\nToday:")
            for e in today:
                t = e["start"][11:16] if len(e["start"]) > 10 else "all day"
                parts.append(f"  • {t} — {e['title']}")
        else:
            parts.append("\nNothing on the calendar today.")

    due_today = [i for i in items if (d := memory.parse_dt(i["due_at"])) and d.date() <= now.date()]
    if due_today:
        parts.append("\nNeeds you today:")
        for i in due_today[:6]:
            parts.append(f"  • {i['title']}")
    top = [i for i in items if i not in due_today][:3]
    if top:
        parts.append("\nIf there's spare energy:")
        for i in top:
            parts.append(f"  • {i['title']}")
    if not items:
        parts.append("\nYour list is clear.")
    else:
        parts.append(f"\n({len(items)} things safely on the list — say \"list\" anytime.)")
    await context.bot.send_message(chat_id=chat_id, text="\n".join(parts))


async def evening_digest(context):
    chat_id = memory.get_setting("owner_chat_id")
    if not chat_id:
        return
    show_done = pref("digest_show_completed", "yes") == "yes"
    done = memory.completed_today()
    items = memory.open_items()
    parts = ["🌙 Evening check-in."]
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
    await context.bot.send_message(chat_id=chat_id, text="\n".join(parts))
