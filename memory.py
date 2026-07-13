"""SQLite persistence: items (the list), facts (her life), messages (chat history), settings."""
import calendar
import sqlite3
from datetime import datetime, timedelta
from typing import Optional

from config import DB_PATH, TZ

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    details TEXT DEFAULT '',
    category TEXT DEFAULT 'task',
    priority INTEGER DEFAULT 3,
    due_at TEXT,
    status TEXT DEFAULT 'open',
    created_at TEXT,
    completed_at TEXT,
    next_remind_at TEXT,
    remind_count INTEGER DEFAULT 0,
    recurrence TEXT,
    remind_lead_seconds INTEGER,
    recurrence_until TEXT,
    reminder_text TEXT
);
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL,
    fire_at TEXT NOT NULL,
    fired_at TEXT,
    note TEXT
);
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    category TEXT DEFAULT 'general',
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    ts TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS seen_emails (
    msg_id TEXT PRIMARY KEY,
    sender TEXT,
    subject TEXT,
    summary TEXT,
    kind TEXT,
    ts TEXT
);
"""


def _c() -> sqlite3.Connection:
    # generous timeout: writes come from both the bot's worker threads and the
    # scheduler's jobs; without it a slow backup can surface "database is locked"
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init():
    with _c() as con:
        con.execute("PRAGMA journal_mode=WAL")  # readers never block the writer
        con.executescript(SCHEMA)
        # migrations for dbs created before these columns existed
        cols = {r["name"] for r in con.execute("PRAGMA table_info(items)")}
        if "recurrence" not in cols:
            con.execute("ALTER TABLE items ADD COLUMN recurrence TEXT")
        if "remind_lead_seconds" not in cols:
            con.execute("ALTER TABLE items ADD COLUMN remind_lead_seconds INTEGER")
        if "recurrence_until" not in cols:
            con.execute("ALTER TABLE items ADD COLUMN recurrence_until TEXT")
        if "reminder_text" not in cols:
            con.execute("ALTER TABLE items ADD COLUMN reminder_text TEXT")
    _migrate_reminders_table()


def _migrate_reminders_table() -> int:
    """One-time data backfill (v1.5): before this release, an item's first
    scheduled ping and its repeat-nag-if-ignored chase shared one column
    (next_remind_at), which is the root of several bugs (a party-time ping
    indistinguishable from an overdue chase). The `reminders` table now owns
    one-shot scheduled pings; next_remind_at/remind_count are reserved for
    nag escalation once an item is actually overdue. Items with a pending
    first ping (remind_count==0) get that ping moved over; next_remind_at is
    cleared so it isn't ALSO read as an in-progress nag. Gated on a settings
    flag so this runs exactly once ever, not on every restart. Returns the
    number of rows migrated (for logging/tests)."""
    if get_setting("migrated_reminders_table_v1_5"):
        return 0
    with _c() as con:
        rows = con.execute(
            "SELECT id, next_remind_at FROM items WHERE status='open' AND next_remind_at IS NOT NULL AND remind_count=0"
        ).fetchall()
        for r in rows:
            con.execute("INSERT INTO reminders (item_id, fire_at) VALUES (?,?)", (r["id"], r["next_remind_at"]))
            con.execute("UPDATE items SET next_remind_at=NULL WHERE id=?", (r["id"],))
    set_setting("migrated_reminders_table_v1_5", "done")
    return len(rows)


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def parse_dt(s: Optional[str]) -> Optional[datetime]:
    """Parse an ISO string; naive values are assumed to be local time."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt


# ---------- items ----------

RECURRENCE_UNITS = ("daily", "weekly", "monthly", "yearly")


def add_item(title, details="", category="task", priority=3, due_at=None, remind_at=None,
             recurrence=None, recurrence_until=None, reminder_text=None) -> int:
    """remind_at is one ISO datetime, or several separated by ' | ' (e.g. night-before
    + day-of). Each becomes a one-shot row in `reminders` — a scheduled ping fires once
    and does not itself start a nag chain (see due_nags / due_scheduled_reminders)."""
    if recurrence and recurrence not in RECURRENCE_UNITS:
        # an unrecognized unit must fail loudly, not silently store a value
        # _advance_date can't roll forward (which used to freeze due_at forever)
        raise ValueError(f"unsupported recurrence {recurrence!r} — must be one of {RECURRENCE_UNITS}")
    if due_at and not remind_at:
        remind_at = due_at
    remind_times = [t.strip() for t in remind_at.split("|")] if remind_at else []
    remind_times = [t for t in remind_times if parse_dt(t)]  # drop anything unparseable, don't raise mid-capture
    # remember the ORIGINAL due->first-reminder lead (from the earliest time given): nags
    # never touch the reminders table, so a recurring item's respawn always has a clean lead
    lead = None
    if due_at and remind_times:
        d, r = parse_dt(due_at), parse_dt(min(remind_times))
        if d and r:
            lead = max(0, int((d - r).total_seconds()))
    with _c() as con:
        cur = con.execute(
            "INSERT INTO items (title, details, category, priority, due_at, created_at, recurrence,"
            " remind_lead_seconds, recurrence_until, reminder_text)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (title, details, category, int(priority), due_at, now_iso(), recurrence,
             lead, recurrence_until, reminder_text),
        )
        item_id = cur.lastrowid
        for t in remind_times:
            con.execute("INSERT INTO reminders (item_id, fire_at) VALUES (?,?)", (item_id, t))
        return item_id


def _advance_date(dt: datetime, unit: str) -> datetime:
    """Rolls a datetime forward one recurrence cycle, keeping time-of-day and
    clamping to the last valid day of the target month (e.g. Jan 31 -> Feb 28)."""
    if unit == "daily":
        return dt + timedelta(days=1)
    if unit == "weekly":
        return dt + timedelta(weeks=1)
    if unit == "monthly":
        month = dt.month + 1
        year = dt.year + (month - 1) // 12
        month = (month - 1) % 12 + 1
        day = min(dt.day, calendar.monthrange(year, month)[1])
        return dt.replace(year=year, month=month, day=day)
    if unit == "yearly":
        day = 28 if (dt.month == 2 and dt.day == 29) else dt.day
        return dt.replace(year=dt.year + 1, day=day)
    return dt


def get_item(item_id) -> Optional[sqlite3.Row]:
    with _c() as con:
        return con.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()


def update_item(item_id, **fields):
    allowed = {"title", "details", "category", "priority", "due_at", "status", "next_remind_at",
               "remind_count", "recurrence", "recurrence_until", "reminder_text"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    if fields.get("recurrence") and fields["recurrence"] not in RECURRENCE_UNITS:
        raise ValueError(f"unsupported recurrence {fields['recurrence']!r} — must be one of {RECURRENCE_UNITS}")
    sets = ", ".join(f"{k}=?" for k in fields)
    with _c() as con:
        con.execute(f"UPDATE items SET {sets} WHERE id=?", (*fields.values(), item_id))


def complete_item(item_id):
    """Marks an item done. Cancels any not-yet-fired scheduled reminders (no
    posthumous pings). If it's recurring, immediately spawns the next
    occurrence (same lead between due date and reminder, rolled forward) —
    unless recurrence_until has been passed, which ends the series."""
    item = get_item(item_id)
    if not item or item["status"] != "open":
        return  # already done/dropped — a second ✅ tap must not double-spawn a recurrence
    with _c() as con:
        con.execute(
            "UPDATE items SET status='done', completed_at=?, next_remind_at=NULL WHERE id=?",
            (now_iso(), item_id),
        )
        con.execute("DELETE FROM reminders WHERE item_id=? AND fired_at IS NULL", (item_id,))
    if item["recurrence"] and item["due_at"]:
        due = parse_dt(item["due_at"])
        lead = item["remind_lead_seconds"]
        if lead is None:  # pre-migration row: fall back to the live reminder, if sane
            remind = parse_dt(item["next_remind_at"])
            lead = int((due - remind).total_seconds()) if remind and remind <= due else 0
        new_due = _advance_date(due, item["recurrence"])
        until = parse_dt(item["recurrence_until"]) if "recurrence_until" in item.keys() else None
        if until and new_due.date() > until.date():
            return  # series has ended — don't spawn past the requested end date
        new_remind = new_due - timedelta(seconds=lead)
        add_item(
            title=item["title"],
            details=item["details"],
            category=item["category"],
            priority=item["priority"],
            due_at=new_due.isoformat(timespec="seconds"),
            remind_at=new_remind.isoformat(timespec="seconds"),
            recurrence=item["recurrence"],
            recurrence_until=item["recurrence_until"] if "recurrence_until" in item.keys() else None,
            reminder_text=item["reminder_text"] if "reminder_text" in item.keys() else None,
        )


def open_items() -> list:
    with _c() as con:
        return con.execute(
            "SELECT * FROM items WHERE status='open' ORDER BY priority DESC, due_at IS NULL, due_at"
        ).fetchall()


def due_nags(now: datetime) -> list:
    """Open items whose due_at has passed and are due for another nag ping —
    the repeating 'still not done' chase. Separate from one-shot scheduled
    reminders (due_scheduled_reminders): a nag only starts once an item is
    actually overdue, and next_remind_at/remind_count pace the repeats."""
    with _c() as con:
        rows = con.execute(
            "SELECT * FROM items WHERE status='open' AND due_at IS NOT NULL"
        ).fetchall()
    out = []
    for r in rows:
        d = parse_dt(r["due_at"])
        if not d or d > now:
            continue
        nxt = parse_dt(r["next_remind_at"])
        if nxt and nxt > now:
            continue  # already nagged, waiting for the next escalation interval
        out.append(r)
    return out


def due_scheduled_reminders(now: datetime) -> list:
    """Unfired one-shot `reminders` rows whose fire_at has arrived, for items
    still open. Each row's item columns are included via the join; access the
    reminders-row id as row["reminder_id"] (not row["id"], which is the item's)."""
    with _c() as con:
        rows = con.execute(
            "SELECT r.id AS reminder_id, r.fire_at, r.note, i.* FROM reminders r"
            " JOIN items i ON i.id = r.item_id"
            " WHERE r.fired_at IS NULL AND i.status='open'"
        ).fetchall()
    return [r for r in rows if (d := parse_dt(r["fire_at"])) and d <= now]


def add_reminder(item_id, fire_at, note=None) -> int:
    with _c() as con:
        cur = con.execute(
            "INSERT INTO reminders (item_id, fire_at, note) VALUES (?,?,?)", (item_id, fire_at, note)
        )
        return cur.lastrowid


def mark_reminder_fired(reminder_id):
    with _c() as con:
        con.execute("UPDATE reminders SET fired_at=? WHERE id=?", (now_iso(), reminder_id))


def delete_unfired_reminders(item_id):
    with _c() as con:
        con.execute("DELETE FROM reminders WHERE item_id=? AND fired_at IS NULL", (item_id,))


def pending_reminder_times(item_id) -> list:
    with _c() as con:
        rows = con.execute(
            "SELECT fire_at FROM reminders WHERE item_id=? AND fired_at IS NULL ORDER BY fire_at", (item_id,)
        ).fetchall()
    return [r["fire_at"] for r in rows]


def replace_item_reminders(item_id, remind_at):
    """Clears any not-yet-fired scheduled pings for the item and replaces them
    with a fresh set parsed from remind_at (one ISO datetime, or several
    separated by ' | ')."""
    delete_unfired_reminders(item_id)
    times = [t.strip() for t in remind_at.split("|") if parse_dt(t.strip())]
    with _c() as con:
        for t in times:
            con.execute("INSERT INTO reminders (item_id, fire_at) VALUES (?,?)", (item_id, t))


def overdue_stale_items(hours: int) -> list:
    """Open items whose due_at passed more than `hours` ago — these have
    fallen out of the nag chain (see NAG_WINDOW_HOURS in scheduler.py) and
    are surfaced gently in the morning digest instead of nagged forever."""
    cutoff = datetime.now(TZ) - timedelta(hours=hours)
    with _c() as con:
        rows = con.execute(
            "SELECT * FROM items WHERE status='open' AND due_at IS NOT NULL ORDER BY due_at"
        ).fetchall()
    return [r for r in rows if (d := parse_dt(r["due_at"])) and d < cutoff]


def stale_open_items(days: int) -> list:
    """Open, non-recurring items sitting untouched since before the cutoff —
    recurring items are excluded since they're waiting for their date, not stale."""
    cutoff = (datetime.now(TZ) - timedelta(days=days)).isoformat(timespec="seconds")
    with _c() as con:
        return con.execute(
            "SELECT * FROM items WHERE status='open' AND recurrence IS NULL AND created_at <= ?"
            " ORDER BY created_at",
            (cutoff,),
        ).fetchall()


def completed_today() -> list:
    today = datetime.now(TZ).date().isoformat()
    with _c() as con:
        return con.execute(
            "SELECT * FROM items WHERE status='done' AND completed_at >= ?", (today,)
        ).fetchall()


# ---------- facts ----------

def add_fact(content, category="general") -> int:
    with _c() as con:
        cur = con.execute(
            "INSERT INTO facts (content, category, created_at) VALUES (?,?,?)",
            (content, category, now_iso()),
        )
        return cur.lastrowid


def all_facts() -> list:
    with _c() as con:
        return con.execute("SELECT * FROM facts ORDER BY id").fetchall()


def delete_fact(fact_id):
    with _c() as con:
        con.execute("DELETE FROM facts WHERE id=?", (fact_id,))


def replace_facts(rows):
    """Atomically replaces the whole facts table. rows: iterable of (content, category)."""
    with _c() as con:
        con.execute("DELETE FROM facts")
        con.executemany(
            "INSERT INTO facts (content, category, created_at) VALUES (?,?,?)",
            [(content, category, now_iso()) for content, category in rows],
        )


# ---------- chat history ----------

def log_msg(role, content):
    with _c() as con:
        con.execute("INSERT INTO messages (role, content, ts) VALUES (?,?,?)", (role, content, now_iso()))


def recent_msgs(n=30) -> list:
    with _c() as con:
        rows = con.execute("SELECT id, role, content FROM messages ORDER BY id DESC LIMIT ?", (n,)).fetchall()
    return list(reversed(rows))


# ---------- settings ----------

def get_setting(key) -> Optional[str]:
    with _c() as con:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None


def set_setting(key, value):
    with _c() as con:
        con.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, str(value)))


# ---------- date-keyed counters (pings_YYYY-MM-DD, incident_claims_YYYY-MM-DD, ...) ----------

def counter_today(prefix: str) -> int:
    return int(get_setting(f"{prefix}_" + datetime.now(TZ).date().isoformat()) or 0)


def bump_counter(prefix: str, n: int = 1) -> int:
    key = f"{prefix}_" + datetime.now(TZ).date().isoformat()
    total = int(get_setting(key) or 0) + n
    set_setting(key, str(total))
    return total


# ---------- emails ----------

def email_seen(msg_id) -> bool:
    with _c() as con:
        return con.execute("SELECT 1 FROM seen_emails WHERE msg_id=?", (msg_id,)).fetchone() is not None


def mark_email(msg_id, sender, subject, summary, kind):
    with _c() as con:
        con.execute(
            "INSERT OR REPLACE INTO seen_emails (msg_id, sender, subject, summary, kind, ts)"
            " VALUES (?,?,?,?,?,?)",
            (msg_id, sender, subject, summary, kind, now_iso()),
        )
