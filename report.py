"""Owner's report — Brooks's window into how Penny is doing without asking Jordan.
Run:  ./venv/bin/python report.py [days]     (default 7; zero API cost)"""
import sys
from datetime import datetime, timedelta

import memory
from config import ASSISTANT_NAME, DAILY_BUDGET_USD, OWNER_PRONOUN_OBJ, OWNER_PRONOUN_POSS, TZ


def main(days=7):
    memory.init()
    since = (datetime.now(TZ) - timedelta(days=days)).isoformat(timespec="seconds")
    with memory._c() as con:
        turns = con.execute(
            "SELECT COUNT(*) c FROM messages WHERE role='user' AND ts >= ?", (since,)
        ).fetchone()["c"]
        active_days = con.execute(
            "SELECT COUNT(DISTINCT substr(ts,1,10)) c FROM messages WHERE role='user' AND ts >= ?", (since,)
        ).fetchone()["c"]
        created = con.execute(
            "SELECT COUNT(*) c FROM items WHERE created_at >= ?", (since,)
        ).fetchone()["c"]
        completed = con.execute(
            "SELECT COUNT(*) c FROM items WHERE status='done' AND completed_at >= ?", (since,)
        ).fetchone()["c"]
        open_now = con.execute("SELECT COUNT(*) c FROM items WHERE status='open'").fetchone()["c"]
        overdue = con.execute(
            "SELECT COUNT(*) c FROM items WHERE status='open' AND due_at IS NOT NULL AND due_at < ?",
            (datetime.now(TZ).isoformat(timespec="seconds"),),
        ).fetchone()["c"]
        facts = con.execute("SELECT COUNT(*) c FROM facts").fetchone()["c"]
        glitches = con.execute(
            "SELECT COUNT(*) c FROM messages WHERE role='assistant' AND ts >= ? AND "
            "(content LIKE '%glitch%' OR content LIKE '%didn''t save properly%')", (since,)
        ).fetchone()["c"]
        spend_rows = con.execute(
            "SELECT key, value FROM settings WHERE key LIKE 'spend_%' ORDER BY key DESC LIMIT ?", (days,)
        ).fetchall()
        ping_rows = con.execute(
            "SELECT key, value FROM settings WHERE key LIKE 'pings_%' ORDER BY key DESC LIMIT ?", (days,)
        ).fetchall()
        incident_rows = con.execute(
            "SELECT key, value FROM settings WHERE key LIKE 'incident_claims_%' ORDER BY key DESC LIMIT ?", (days,)
        ).fetchall()
        prefs = con.execute("SELECT key, value FROM settings WHERE key LIKE 'pref_%'").fetchall()

    total_spend = sum(float(r["value"]) for r in spend_rows)
    total_pings = sum(int(r["value"]) for r in ping_rows)
    total_incidents = sum(int(r["value"]) for r in incident_rows)
    avg_pings = total_pings / max(len(ping_rows), 1)
    print(f"=== {ASSISTANT_NAME} report — last {days} days ===")
    print(f"Conversations: {turns} messages from {OWNER_PRONOUN_OBJ} across {active_days} active day(s)")
    print(f"Items: {created} captured, {completed} completed, {open_now} open now ({overdue} overdue)")
    print(f"Facts known: {facts}")
    print(f"Incidents (glitch/failed-save replies): {glitches}")
    print(f"Empty-promise guard trips (claimed a change with no tool call): {total_incidents}"
          + ("  [v1.5 acceptance criterion 6: should be 0]" if total_incidents else ""))
    print(f"Proactive pings: {total_pings} total, avg {avg_pings:.1f}/day"
          + ("  [v1.5 acceptance criterion 5: should average <=5/day]" if ping_rows else " (no data yet)"))
    print(f"Spend: ${total_spend:.3f} total, avg ${total_spend / max(days, 1):.3f}/day (cap ${DAILY_BUDGET_USD:.2f}/day)")
    for r in spend_rows:
        print(f"  {r['key'][6:]}: ${float(r['value']):.3f}")
    print(f"{OWNER_PRONOUN_POSS.capitalize()} preferences: "
          + (", ".join(f"{r['key'][5:]}={r['value']}" for r in prefs) or "(defaults)"))


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 7)
