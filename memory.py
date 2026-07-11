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
    remind_lead_seconds INTEGER
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

def add_item(title, details="", category="task", priority=3, due_at=None, remind_at=None, recurrence=None) -> int:
    if due_at and not remind_at:
        remind_at = due_at
    # remember the ORIGINAL due->reminder lead: nags overwrite next_remind_at,
    # so a recurring item's respawn must not derive its lead from the drifted value
    lead = None
    if due_at and remind_at:
        d, r = parse_dt(due_at), parse_dt(remind_at)
        if d and r:
            lead = max(0, int((d - r).total_seconds()))
    with _c() as con:
        cur = con.execute(
            "INSERT INTO items (title, details, category, priority, due_at, created_at, next_remind_at, recurrence, remind_lead_seconds)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (title, details, category, int(priority), due_at, now_iso(), remind_at, recurrence, lead),
        )
        return cur.lastrowid


def _advance_date(dt: datetime, unit: str) -> datetime:
    """Rolls a datetime forward one recurrence cycle, keeping time-of-day and
    clamping to the last valid day of the target month (e.g. Jan 31 -> Feb 28)."""
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
    allowed = {"title", "details", "category", "priority", "due_at", "status", "next_remind_at", "remind_count", "recurrence"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    with _c() as con:
        con.execute(f"UPDATE items SET {sets} WHERE id=?", (*fields.values(), item_id))


def complete_item(item_id):
    """Marks an item done. If it's recurring, immediately spawns the next
    occurrence (same lead between due date and reminder, rolled forward)."""
    item = get_item(item_id)
    if not item or item["status"] != "open":
        return  # already done/dropped — a second ✅ tap must not double-spawn a recurrence
    with _c() as con:
        con.execute(
            "UPDATE items SET status='done', completed_at=?, next_remind_at=NULL WHERE id=?",
            (now_iso(), item_id),
        )
    if item["recurrence"] and item["due_at"]:
        due = parse_dt(item["due_at"])
        lead = item["remind_lead_seconds"]
        if lead is None:  # pre-migration row: fall back to the live reminder, if sane
            remind = parse_dt(item["next_remind_at"])
            lead = int((due - remind).total_seconds()) if remind and remind <= due else 0
        new_due = _advance_date(due, item["recurrence"])
        new_remind = new_due - timedelta(seconds=lead)
        add_item(
            title=item["title"],
            details=item["details"],
            category=item["category"],
            priority=item["priority"],
            due_at=new_due.isoformat(timespec="seconds"),
            remind_at=new_remind.isoformat(timespec="seconds"),
            recurrence=item["recurrence"],
        )


def open_items() -> list:
    with _c() as con:
        return con.execute(
            "SELECT * FROM items WHERE status='open' ORDER BY priority DESC, due_at IS NULL, due_at"
        ).fetchall()


def due_reminders(now: datetime) -> list:
    with _c() as con:
        rows = con.execute(
            "SELECT * FROM items WHERE status='open' AND next_remind_at IS NOT NULL"
        ).fetchall()
    return [r for r in rows if (d := parse_dt(r["next_remind_at"])) and d <= now]


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
