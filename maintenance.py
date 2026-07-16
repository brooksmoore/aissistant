"""Housekeeping that runs itself: nightly DB backup, and one weekly job that
surfaces stale items for a guilt-free prune and quietly tidies up her facts.
Everything here is free except the weekly fact pass (one cheap Haiku call)."""
import asyncio
import glob
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta

import memory
from config import CLASSIFIER_MODEL, HARD_CAP_USD, DB_PATH, INSTANCE_DIR, LOG_PATH, TZ
from scheduler import icon, pref

log = logging.getLogger("penny.maintenance")

BACKUP_DIR = INSTANCE_DIR / "backups"
BACKUP_KEEP_DAYS = 30
STALE_DAYS = 14
WEEKLY_WEEKDAY = 6   # Sunday
WEEKLY_HOUR = 17     # 5pm local, same window as her evening wind-down


# ---------- daily backup ----------

def backup_db():
    """Copies penny.db into backups/ once a day via SQLite's own backup API
    (safe to run against a live, in-use database), and prunes old copies."""
    today = datetime.now(TZ).date().isoformat()
    if memory.get_setting("last_backup_date") == today:
        return
    BACKUP_DIR.mkdir(exist_ok=True)
    dest = BACKUP_DIR / f"penny_{today}.db"
    try:
        src = sqlite3.connect(str(DB_PATH))
        dst = sqlite3.connect(str(dest))
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
    except Exception:
        log.exception("db backup failed")
        return
    memory.set_setting("last_backup_date", today)
    prune()
    cutoff = datetime.now(TZ) - timedelta(days=BACKUP_KEEP_DAYS)
    for f in glob.glob(str(BACKUP_DIR / "penny_*.db")):
        try:
            stamp = os.path.basename(f)[len("penny_"):-len(".db")]
            if datetime.fromisoformat(stamp).replace(tzinfo=TZ) < cutoff:
                os.remove(f)
        except Exception:
            continue
    log.info("db backup written: %s", dest.name)


def prune():
    """Keeps the working set bounded over years of use: old chat rows and
    email markers age out (items and facts are never pruned here), and the
    log is truncated when it gets big. All free, all reversible via backups."""
    try:
        with sqlite3.connect(str(DB_PATH)) as con:
            con.execute(
                "DELETE FROM messages WHERE id < (SELECT COALESCE(MAX(id),0) FROM messages) - 800"
            )
            cutoff = (datetime.now(TZ) - timedelta(days=30)).isoformat()
            con.execute("DELETE FROM seen_emails WHERE ts < ?", (cutoff,))
        logpath = LOG_PATH
        if logpath.exists() and logpath.stat().st_size > 5_000_000:
            tail = logpath.read_bytes()[-1_000_000:]
            logpath.write_bytes(b"[log trimmed]\n" + tail)
            log.info("penny.log trimmed")
    except Exception:
        log.exception("prune failed")


async def backup_tick(context):
    """Runs hourly; backup_db() no-ops after the first successful run each day."""
    await asyncio.to_thread(backup_db)  # disk I/O off the event loop


# ---------- weekly fact tidy-up ----------

FACT_TIDY_PROMPT = """Below is one person's stored memory facts, used to personalize an anxiety-support \
personal assistant. Clean this list:
- Merge duplicates or near-duplicates into a single fact.
- Drop facts that are purely one-time and now clearly in the past (a specific past date/event that already \
happened), UNLESS they describe a recurring pattern worth keeping (e.g. "usually sees X on Fridays").
- Keep everything else as-is: people, preferences, routines, identity, ongoing situations.
- Do not invent anything new, and do not drop anything you're not sure is stale.

Today's date: {today}

Current facts:
{facts}

Reply with ONLY a JSON array, nothing else:
[{{"content": "...", "category": "identity|people|preferences|routine|situation|general"}}]
"""


def fact_maintenance():
    import brain  # deferred: brain imports memory, avoid an import cycle at module load

    if brain.today_spend() >= HARD_CAP_USD:
        log.warning("skipping fact maintenance: daily hard cap already reached")
        return
    facts = memory.all_facts()
    if not facts:
        return
    listing = "\n".join(f"- [{f['category']}] {f['content']}" for f in facts)
    prompt = FACT_TIDY_PROMPT.format(today=datetime.now(TZ).strftime("%Y-%m-%d"), facts=listing)
    try:
        raw = brain.quick(prompt, model=CLASSIFIER_MODEL, max_tokens=2000)
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        cleaned = json.loads(match.group(0)) if match else None
    except Exception:
        log.exception("fact maintenance call failed")
        return
    if not cleaned:
        return
    rows = [(c.get("content", "").strip(), c.get("category", "general"))
            for c in cleaned if isinstance(c, dict) and c.get("content", "").strip()]
    # never let a parsing hiccup or an overzealous model silently wipe her memory —
    # guard on the rows actually written, not the raw JSON entry count (blank-content
    # entries used to pass the count check and then get filtered out)
    if len(rows) < max(1, len(facts) // 2):
        log.warning("fact maintenance produced %d usable facts from %d — skipping, looks unsafe", len(rows), len(facts))
        return
    memory.replace_facts(rows)
    log.info("fact maintenance: %d -> %d facts", len(facts), len(rows))


# ---------- weekly stale-item review ----------

def _stale_digest_text() -> str | None:
    stale = memory.stale_open_items(STALE_DAYS)
    if not stale:
        return None
    now = datetime.now(TZ)
    lines = [f"{icon('🗂')}Weekly tidy-up — these have sat on your list 2+ weeks:"]
    for it in stale[:10]:
        created = memory.parse_dt(it["created_at"])
        since = created.strftime("%b %-d") if created else "a while"
        lines.append(f"  • {it['title']} (since {since})")
    if len(stale) > 10:
        lines.append(f"  …and {len(stale) - 10} more.")
    lines.append("\nKeep, drop, or shrink any of these — just tell me, no guilt either way.")
    return "\n".join(lines)


PATTERN_REVIEW_PROMPT = """{state}

Based on these items (statuses, due dates, categories, how long things have \
been open), write at most 4 short bullets of genuinely useful observations for \
the owner's Sunday review: what keeps sliding, what reliably gets done, one \
concrete suggestion grounded in the data. Plain text bullets ("• "), no \
headers, no flattery, no invented details. If the data is too thin to say \
anything real, reply with exactly NONE."""


def _pattern_review_text() -> str | None:
    """One weekly Haiku pass over her items for pattern insights ("Jordan tasks
    cluster and slip together"). Returns None on any failure, thin data, or
    budget stop — the weekly message just goes out without it."""
    import brain  # deferred: brain imports memory; avoid an import cycle at module load

    if brain.today_spend() >= HARD_CAP_USD:
        return None
    if len(memory.open_items()) + len(memory.completed_today()) < 5:
        return None  # not enough signal to say anything worth a bullet
    try:
        text = brain.quick(PATTERN_REVIEW_PROMPT.format(state=brain._state_block()), max_tokens=250)
    except Exception:
        log.exception("weekly pattern review failed (skipping)")
        return None
    if not text or text.strip().upper().startswith("NONE") or len(text) > 800:
        return None
    return f"{icon('🧭')}This week's patterns:\n{text}"


async def weekly_tick(context):
    """Runs hourly; fires once per ISO week, Sunday evening: surfaces stale
    items and week-level patterns to her, then quietly tidies up facts in
    the background."""
    now = datetime.now(TZ)
    this_week = now.strftime("%G-W%V")
    if memory.get_setting("last_weekly_review") == this_week:
        return
    if now.weekday() != WEEKLY_WEEKDAY or now.hour < WEEKLY_HOUR:
        return
    memory.set_setting("last_weekly_review", this_week)

    chat_id = memory.get_setting("owner_chat_id")
    stale_text = _stale_digest_text()
    patterns = await asyncio.to_thread(_pattern_review_text)  # Claude call off the event loop
    text = "\n\n".join(p for p in (stale_text, patterns) if p)
    # notifications_enabled only silences the PING — fact tidy-up below still
    # runs regardless, since it's silent housekeeping, not a message to her
    if chat_id and text and pref("notifications_enabled", "yes") != "no":
        try:
            await context.bot.send_message(chat_id=chat_id, text=text)
            # tally + log like every other proactive send (v1.5 rule) — was
            # silently missed here before, hiding this ping from the brain
            # and from the pings_* budget accounting
            from scheduler import bump_pings, log_proactive
            log_proactive("digest", text)
            bump_pings(1)
        except Exception:
            log.exception("weekly review message failed to send")

    try:
        await asyncio.to_thread(fact_maintenance)  # Claude call off the event loop
    except Exception:
        log.exception("weekly fact maintenance failed")
