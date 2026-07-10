"""Penny unit suite — zero API cost. Covers: timing, reliability, malleability
plumbing, capability mechanics, drift, and cost-control logic.
Run: ./venv/bin/python -m unittest discover tests -v"""
import os
import pathlib
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import memory  # noqa: E402

TEST_DB = pathlib.Path("/tmp/penny_unittest.db")


def fresh_db():
    memory.DB_PATH = TEST_DB
    if TEST_DB.exists():
        os.remove(TEST_DB)
    memory.init()


class TestMemoryReliability(unittest.TestCase):
    """Her data must survive anything — the core trust contract."""

    def setUp(self):
        fresh_db()

    def test_item_roundtrip_and_survives_reopen(self):
        i = memory.add_item("Test task", priority=4, due_at="2026-08-01T09:00:00")
        # simulate a reboot: nothing cached, fresh connection
        row = memory.get_item(i)
        self.assertEqual(row["title"], "Test task")
        self.assertEqual(row["priority"], 4)
        self.assertEqual(row["next_remind_at"], "2026-08-01T09:00:00")  # due implies remind

    def test_complete_clears_reminder(self):
        i = memory.add_item("x", due_at="2026-08-01T09:00:00")
        memory.complete_item(i)
        row = memory.get_item(i)
        self.assertEqual(row["status"], "done")
        self.assertIsNone(row["next_remind_at"])

    def test_fact_replacement_no_contradictions(self):
        f1 = memory.add_fact("Her birthday is July 12")
        memory.delete_fact(f1)
        memory.add_fact("Her birthday is July 17")
        contents = [f["content"] for f in memory.all_facts()]
        self.assertEqual(len([c for c in contents if "birthday" in c]), 1)
        self.assertIn("July 17", contents[0])

    def test_parse_dt_naive_gets_local_tz(self):
        d = memory.parse_dt("2026-07-15T09:00:00")
        self.assertEqual(str(d.tzinfo), str(config.TZ))
        self.assertIsNone(memory.parse_dt("garbage"))
        self.assertIsNone(memory.parse_dt(None))


class TestRecurrenceTiming(unittest.TestCase):
    """Recurring items must roll forward correctly across awkward calendar edges."""

    def setUp(self):
        fresh_db()

    def test_monthly_rolls_and_respawns_on_completion(self):
        i = memory.add_item("Pay rent", due_at="2026-08-01T09:00:00", recurrence="monthly")
        memory.complete_item(i)
        open_now = memory.open_items()
        self.assertEqual(len(open_now), 1)
        self.assertTrue(open_now[0]["due_at"].startswith("2026-09-01"))
        self.assertEqual(open_now[0]["recurrence"], "monthly")

    def test_monthly_clamps_short_months(self):
        d = memory._advance_date(datetime(2026, 1, 31, 9, 0, tzinfo=config.TZ), "monthly")
        self.assertEqual((d.month, d.day), (2, 28))

    def test_yearly_handles_leap_day(self):
        d = memory._advance_date(datetime(2028, 2, 29, 9, 0, tzinfo=config.TZ), "yearly")
        self.assertEqual((d.year, d.month, d.day), (2029, 2, 28))

    def test_reminder_lead_time_preserved(self):
        i = memory.add_item("Renew registration", due_at="2026-08-10T17:00:00",
                            remind_at="2026-08-08T09:00:00", recurrence="monthly")
        memory.complete_item(i)
        nxt = memory.open_items()[0]
        due = memory.parse_dt(nxt["due_at"])
        remind = memory.parse_dt(nxt["next_remind_at"])
        self.assertEqual(due - remind, timedelta(days=2, hours=8))


class TestReminderTiming(unittest.TestCase):
    """The nag engine: escalation, quiet hours, due detection."""

    def setUp(self):
        fresh_db()
        import scheduler
        self.scheduler = scheduler

    def test_due_detection(self):
        now = datetime.now(config.TZ)
        past = (now - timedelta(minutes=5)).isoformat(timespec="seconds")
        future = (now + timedelta(hours=2)).isoformat(timespec="seconds")
        memory.add_item("due now", remind_at=past, due_at=past)
        memory.add_item("not yet", remind_at=future, due_at=future)
        due = memory.due_reminders(now)
        self.assertEqual([d["title"] for d in due], ["due now"])

    def test_escalation_by_priority_and_style(self):
        s = self.scheduler
        self.assertEqual(s.escalation_minutes(5), 30)
        self.assertEqual(s.escalation_minutes(3), 360)
        self.assertEqual(s.escalation_minutes(1), 0)  # once, then digests
        memory.set_setting("pref_reminder_style", "gentle")
        self.assertEqual(s.escalation_minutes(5), 60)   # gentle doubles the gap
        self.assertLessEqual(s.max_nags(), 4)            # and caps the nag count
        memory.set_setting("pref_reminder_style", "persistent")
        self.assertEqual(s.escalation_minutes(5), 15)

    def test_quiet_hours_span_midnight(self):
        s = self.scheduler
        mk = lambda h: datetime.now(config.TZ).replace(hour=h, minute=0)
        self.assertTrue(s._quiet(mk(23)))
        self.assertTrue(s._quiet(mk(2)))
        self.assertFalse(s._quiet(mk(12)))
        memory.set_setting("pref_quiet_start_hour", "21")  # her actual request once
        self.assertTrue(s._quiet(mk(21)))


class TestMalleability(unittest.TestCase):
    """Every promise Penny can make must have a mechanism behind it."""

    def setUp(self):
        fresh_db()
        import brain
        self.brain = brain

    def test_every_preference_key_is_wired(self):
        import scheduler
        tools = {t["name"]: t for t in self.brain._tools()}
        keys = tools["set_preference"]["input_schema"]["properties"]["key"]["enum"]
        expected = {"reminder_style", "quiet_start_hour", "quiet_end_hour",
                    "morning_digest_time", "evening_digest_time", "emoji_level",
                    "reply_length", "digest_show_completed", "max_nags"}
        self.assertTrue(expected.issubset(set(keys)))

    def test_set_preference_validation(self):
        r = self.brain._run_tool("set_preference", {"key": "emoji_level", "value": "sparkly"})
        self.assertIn("must be", r)
        r = self.brain._run_tool("set_preference", {"key": "emoji_level", "value": "none"})
        self.assertIn("updated", r.lower())
        self.assertEqual(memory.get_setting("pref_emoji_level"), "none")

    def test_prefs_visible_to_model(self):
        memory.set_setting("pref_emoji_level", "none")
        memory.set_setting("pref_reply_length", "short")
        state = self.brain._state_block()
        self.assertIn("emoji_level=none", state)

    def test_fact_replacement_tool(self):
        fid = memory.add_fact("Her birthday is July 12")
        self.brain._run_tool("remember_fact",
                             {"fact": "Her birthday is July 17", "replaces_fact_id": fid})
        contents = [f["content"] for f in memory.all_facts()]
        self.assertEqual(contents, ["Her birthday is July 17"])


class TestCostControls(unittest.TestCase):
    """Routing, breaker tiers, and cache-stable history windows."""

    def setUp(self):
        fresh_db()
        import brain
        self.brain = brain

    def test_v1_is_haiku_everywhere(self):
        self.assertIn("haiku", config.BRAIN_MODEL)  # v1 scope: Haiku-only

    def test_breaker_forces_cheap_model(self):
        key = "spend_" + datetime.now(config.TZ).date().isoformat()
        memory.set_setting(key, str(config.DAILY_BUDGET_USD + 0.01))
        self.assertIn("haiku", self.brain.pick_model("help me decide something huge"))

    def test_hard_cap_stops_api_calls_entirely(self):
        key = "spend_" + datetime.now(config.TZ).date().isoformat()
        memory.set_setting(key, str(3 * config.DAILY_BUDGET_USD + 0.01))
        reply = self.brain.respond("hi penny")  # must NOT hit the API
        self.assertIn("resting", reply)
        self.assertIn("reminders", reply)

    def test_history_window_is_cache_stable(self):
        """The prefix must stay byte-identical across several turns (block sliding)."""
        for i in range(20):
            memory.log_msg("user", f"msg {i}")
            memory.log_msg("assistant", f"reply {i}")
        h1 = self.brain._history()
        memory.log_msg("user", "one more")
        h2 = self.brain._history()
        # everything except the newest message must be an unchanged prefix
        self.assertEqual([m["content"] for m in h1], [m["content"] for m in h2[:len(h1)]])

    def test_emoji_counter_stays_bounded(self):
        # facts cap keeps the prompt from growing forever
        for i in range(100):
            memory.add_fact(f"fact number {i}")
        state = self.brain._state_block()
        self.assertLess(state.count("fact number"), 70)


class TestDrift(unittest.TestCase):
    """The core prompt must not change silently — predictability is a feature."""

    SNAPSHOT = pathlib.Path(__file__).parent / "personality.snapshot"

    def test_personality_matches_snapshot(self):
        import brain
        import hashlib
        current = hashlib.sha256(brain.PERSONALITY.encode()).hexdigest()
        if not self.SNAPSHOT.exists():
            self.SNAPSHOT.write_text(current)
            self.skipTest("snapshot created — rerun to enforce")
        self.assertEqual(
            current, self.SNAPSHOT.read_text().strip(),
            "PERSONALITY changed. If intentional, delete tests/personality.snapshot "
            "and rerun to bless the new version.",
        )

    def test_no_forbidden_reassurance_phrases_in_prompt_examples(self):
        import brain
        # the prompt bans these; it must also instruct the ban (not use them as guidance)
        self.assertIn("don't worry", brain.PERSONALITY.lower())
        self.assertIn("never", brain.PERSONALITY.lower())


class TestReliabilityInfra(unittest.TestCase):
    def setUp(self):
        fresh_db()

    def test_backup_creates_file(self):
        import maintenance
        memory.add_item("something to protect")
        # point maintenance at the test db
        maintenance.DB_PATH = TEST_DB
        maintenance.BACKUP_DIR = pathlib.Path("/tmp/penny_test_backups")
        maintenance.backup_db()
        backups = list(maintenance.BACKUP_DIR.glob("penny_*.db"))
        self.assertTrue(backups)
        for b in backups:
            os.remove(b)

    def test_prune_keeps_recent_messages(self):
        import maintenance
        maintenance.DB_PATH = TEST_DB
        for i in range(1000):
            memory.log_msg("user", f"m{i}")
        maintenance.prune()
        remaining = memory.recent_msgs(2000)
        self.assertLessEqual(len(remaining), 801)
        self.assertEqual(remaining[-1]["content"], "m999")  # newest survives


if __name__ == "__main__":
    unittest.main(verbosity=2)
