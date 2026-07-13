"""v1.5 suite: the reminders-table split, notification budget/bundling, the
empty-promise guard, recurrence end dates, custom reminder text, and the
zero-API acknowledgment shortcut. See SONNET_HANDOFF_v1.5.md for the incident
context each of these was written against.
Run: ./venv/bin/python -m unittest discover tests -v"""
import asyncio
import os
import pathlib
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import memory  # noqa: E402

TEST_DB = pathlib.Path("/tmp/penny_v1_5.db")


def fresh_db():
    memory.DB_PATH = TEST_DB
    if TEST_DB.exists():
        os.remove(TEST_DB)
    memory.init()


def run(coro):
    return asyncio.run(coro)


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, **kw):
        self.sent.append(kw)


class FakeContext:
    def __init__(self):
        self.bot = FakeBot()


class TestRemindersTableMigration(unittest.TestCase):
    def setUp(self):
        fresh_db()

    def test_migration_is_idempotent(self):
        # simulate a pre-v1.5 row: a pending first ping, never yet nagged
        with memory._c() as con:
            con.execute(
                "INSERT INTO items (title, status, created_at, due_at, next_remind_at, remind_count)"
                " VALUES ('legacy', 'open', ?, ?, ?, 0)",
                (memory.now_iso(), "2026-08-01T09:00:00", "2026-08-01T09:00:00"),
            )
        memory.set_setting("migrated_reminders_table_v1_5", "")  # force it to look unmigrated
        moved_first = memory._migrate_reminders_table()
        moved_second = memory._migrate_reminders_table()
        self.assertEqual(moved_first, 1)
        self.assertEqual(moved_second, 0)  # gated — must not re-run and re-clear a live nag time

    def test_migration_moves_pending_ping_and_clears_next_remind_at(self):
        with memory._c() as con:
            con.execute(
                "INSERT INTO items (title, status, created_at, due_at, next_remind_at, remind_count)"
                " VALUES ('legacy', 'open', ?, ?, ?, 0)",
                (memory.now_iso(), "2026-08-01T09:00:00", "2026-08-01T09:00:00"),
            )
        memory.set_setting("migrated_reminders_table_v1_5", "")
        memory._migrate_reminders_table()
        item = memory.open_items()[0]
        self.assertIsNone(item["next_remind_at"])
        self.assertEqual(memory.pending_reminder_times(item["id"]), ["2026-08-01T09:00:00"])

    def test_migration_leaves_in_progress_nags_alone(self):
        """A row already mid-nag-chain (remind_count > 0) already got its first
        ping historically — must not manufacture a duplicate scheduled reminder."""
        with memory._c() as con:
            con.execute(
                "INSERT INTO items (title, status, created_at, due_at, next_remind_at, remind_count)"
                " VALUES ('legacy', 'open', ?, ?, ?, 2)",
                (memory.now_iso(), "2026-08-01T09:00:00", "2026-08-01T09:00:00"),
            )
        memory.set_setting("migrated_reminders_table_v1_5", "")
        memory._migrate_reminders_table()
        item = memory.open_items()[0]
        self.assertEqual(item["next_remind_at"], "2026-08-01T09:00:00")  # untouched
        self.assertEqual(memory.pending_reminder_times(item["id"]), [])


class TestMultiReminderParsing(unittest.TestCase):
    def setUp(self):
        fresh_db()

    def test_pipe_separated_remind_at_creates_multiple_rows(self):
        i = memory.add_item("Renew registration", due_at="2026-08-10T09:00:00",
                             remind_at="2026-08-08T20:30:00 | 2026-08-10T08:00:00")
        times = memory.pending_reminder_times(i)
        self.assertEqual(times, ["2026-08-08T20:30:00", "2026-08-10T08:00:00"])

    def test_unparseable_time_in_pipe_list_is_dropped_not_raised(self):
        i = memory.add_item("x", due_at="2026-08-10T09:00:00", remind_at="2026-08-08T20:30:00 | garbage")
        self.assertEqual(memory.pending_reminder_times(i), ["2026-08-08T20:30:00"])

    def test_replace_item_reminders_clears_and_resets(self):
        i = memory.add_item("x", due_at="2026-08-10T09:00:00", remind_at="2026-08-08T20:30:00")
        memory.replace_item_reminders(i, "2026-08-09T09:00:00 | 2026-08-10T07:00:00")
        self.assertEqual(memory.pending_reminder_times(i), ["2026-08-09T09:00:00", "2026-08-10T07:00:00"])

    def test_recurring_respawn_carries_recurrence_until_and_reminder_text(self):
        i = memory.add_item("Trivia", due_at="2026-07-14T19:00:00", recurrence="weekly",
                             recurrence_until="2026-09-22", reminder_text="Trivia tonight!")
        memory.complete_item(i)
        nxt = memory.open_items()[0]
        self.assertEqual(nxt["recurrence_until"], "2026-09-22")
        self.assertEqual(nxt["reminder_text"], "Trivia tonight!")

    def test_recurrence_stops_after_until_date(self):
        i = memory.add_item("Trivia", due_at="2026-09-15T19:00:00", recurrence="weekly",
                             recurrence_until="2026-09-20")
        memory.complete_item(i)  # next would be 2026-09-22 -> past the until date
        self.assertEqual(memory.open_items(), [])

    def test_recurrence_open_ended_when_until_not_set(self):
        i = memory.add_item("Trivia", due_at="2026-07-14T19:00:00", recurrence="weekly")
        memory.complete_item(i)
        self.assertEqual(len(memory.open_items()), 1)


class TestBundlingAndPingBudget(unittest.TestCase):
    def setUp(self):
        fresh_db()
        import scheduler
        self.scheduler = scheduler
        memory.set_setting("owner_chat_id", "12345")
        memory.set_setting("pref_quiet_start_hour", "0")
        memory.set_setting("pref_quiet_end_hour", "0")

    def _due_item(self, title, priority=3):
        now = datetime.now(config.TZ)
        past = (now - timedelta(minutes=5)).isoformat(timespec="seconds")
        return memory.add_item(title, due_at=past, remind_at=past, priority=priority)

    def test_multiple_due_items_bundle_into_one_message(self):
        self._due_item("thing one")
        self._due_item("thing two")
        self._due_item("thing three")
        ctx = FakeContext()
        run(self.scheduler.check_reminders(ctx))
        self.assertEqual(len(ctx.bot.sent), 1)
        text = ctx.bot.sent[0]["text"]
        self.assertIn("thing one", text)
        self.assertIn("thing two", text)
        self.assertIn("thing three", text)

    def test_single_due_item_is_not_bundled(self):
        self._due_item("only thing")
        ctx = FakeContext()
        run(self.scheduler.check_reminders(ctx))
        self.assertEqual(len(ctx.bot.sent), 1)
        self.assertIn("only thing", ctx.bot.sent[0]["text"])
        self.assertNotIn("things need you", ctx.bot.sent[0]["text"])

    def test_bundle_counts_as_one_ping_not_n(self):
        self._due_item("a")
        self._due_item("b")
        run(self.scheduler.check_reminders(FakeContext()))
        self.assertEqual(self.scheduler.pings_today(), 1)

    def test_daily_cap_defers_nags_but_not_scheduled_or_p5(self):
        memory.set_setting("pref_daily_ping_cap", "0")  # already at cap
        nag_id = self._due_item("a lower-priority overdue nag", priority=3)
        # force it into the NAG track (not the one-shot scheduled path) so the
        # cap actually applies to it
        memory.delete_unfired_reminders(nag_id)
        now = datetime.now(config.TZ)
        past = (now - timedelta(minutes=5)).isoformat(timespec="seconds")
        memory.update_item(nag_id, next_remind_at=past)
        p5_id = self._due_item("urgent p5 thing", priority=5)
        memory.delete_unfired_reminders(p5_id)
        memory.update_item(p5_id, next_remind_at=past)
        ctx = FakeContext()
        run(self.scheduler.check_reminders(ctx))
        sent_text = " ".join(m["text"] for m in ctx.bot.sent)
        self.assertNotIn("a lower-priority overdue nag", sent_text)  # capped
        self.assertIn("urgent p5 thing", sent_text)  # P5 exempt from the cap

    def test_scheduled_first_fire_exempt_from_cap(self):
        memory.set_setting("pref_daily_ping_cap", "0")
        self._due_item("first-time scheduled ping", priority=3)
        ctx = FakeContext()
        run(self.scheduler.check_reminders(ctx))
        self.assertIn("first-time scheduled ping", ctx.bot.sent[0]["text"])

    def test_proactive_sends_are_logged_with_prefix(self):
        self._due_item("loggable thing")
        run(self.scheduler.check_reminders(FakeContext()))
        rows = memory.recent_msgs(5)
        self.assertTrue(any(r["content"].startswith("[reminder]") for r in rows))


class TestNagCutoffAndStaleSweep(unittest.TestCase):
    def setUp(self):
        fresh_db()
        import scheduler
        self.scheduler = scheduler
        memory.set_setting("owner_chat_id", "12345")
        memory.set_setting("pref_quiet_start_hour", "0")
        memory.set_setting("pref_quiet_end_hour", "0")

    def test_nag_stops_after_window_and_clears_next_remind_at(self):
        now = datetime.now(config.TZ)
        long_past = (now - timedelta(hours=25)).isoformat(timespec="seconds")
        i = memory.add_item("ancient overdue thing", due_at=long_past, priority=3)
        memory.delete_unfired_reminders(i)
        memory.update_item(i, next_remind_at=long_past)
        ctx = FakeContext()
        run(self.scheduler.check_reminders(ctx))
        self.assertEqual(ctx.bot.sent, [])  # past the 24h chase window — not nagged
        self.assertIsNone(memory.get_item(i)["next_remind_at"])

    def test_stale_item_appears_in_morning_digest_with_buttons(self):
        now = datetime.now(config.TZ)
        long_past = (now - timedelta(hours=30)).isoformat(timespec="seconds")
        memory.add_item("fell out of the nag chain", due_at=long_past, priority=3)
        ctx = FakeContext()
        run(self.scheduler.morning_digest(ctx))
        # main digest + the stale-sweep followup
        self.assertEqual(len(ctx.bot.sent), 2)
        stale_msg = ctx.bot.sent[1]
        self.assertIn("came and went", stale_msg["text"])
        self.assertIsNotNone(stale_msg["reply_markup"])

    def test_max_nags_default_is_five(self):
        self.assertEqual(self.scheduler.MAX_NAGS, 5)


class TestMuteButtonDBEffect(unittest.TestCase):
    """bot.py's on_button 'mute' handler does two things at the DB layer —
    tested directly here since driving python-telegram-bot's Update/CallbackQuery
    objects for a real handler test is out of proportion to what's being checked."""

    def setUp(self):
        fresh_db()

    def test_mute_clears_nag_state_and_pending_pings(self):
        i = memory.add_item("nagging thing", due_at="2026-08-01T09:00:00", priority=3)
        memory.update_item(i, next_remind_at="2026-08-01T09:00:00", remind_count=3)
        memory.add_reminder(i, "2026-08-02T09:00:00")  # a snooze someone added
        # the handler's exact effect (bot.py on_button, action == "mute"):
        memory.update_item(i, next_remind_at=None)
        memory.delete_unfired_reminders(i)
        item = memory.get_item(i)
        self.assertIsNone(item["next_remind_at"])
        self.assertEqual(memory.pending_reminder_times(i), [])
        self.assertEqual(item["status"], "open")  # still on the list — just silenced


class TestEmptyPromiseGuardPatternMatcher(unittest.TestCase):
    def setUp(self):
        import brain
        self.brain = brain

    def test_detects_claim_phrases(self):
        claims = [
            "I've turned off your evening digest.",
            "Turned on quiet mode for you.",
            "I've set your reminders to gentle.",
            "I've moved that to Friday.",
            "I've updated the time.",
            "I've changed your preference.",
            "I've paused notifications.",
            "That's now on your list.",
            "Your reminder is now set for 8pm.",
            "It's on the calendar.",
            "Checked off the dentist thing.",
            "Dropped the birthday item.",
            "Saved that to memory.",
        ]
        for text in claims:
            with self.subTest(text=text):
                self.assertTrue(self.brain.claims_change(text), f"should have matched: {text!r}")

    def test_does_not_flag_ordinary_text(self):
        ordinary = [
            "What's on your list today?",
            "I'll remind you tonight at 7:30.",
            "Costco usually has better bulk prices.",
            "How's your day going?",
        ]
        for text in ordinary:
            with self.subTest(text=text):
                self.assertFalse(self.brain.claims_change(text), f"should NOT have matched: {text!r}")


class TestAckShortcutRegex(unittest.TestCase):
    def setUp(self):
        import bot
        self.ACK_RE = bot.ACK_RE

    def test_matches_plain_acks(self):
        for text in ["thanks", "Thanks!", "thank you", "thx", "ok", "okay", "Okay.",
                     "got it", "perfect", "great", "👍", "🙏", "❤️", "thanks 🙏"]:
            with self.subTest(text=text):
                self.assertTrue(self.ACK_RE.match(text), f"should match: {text!r}")

    def test_does_not_match_near_misses(self):
        for text in ["ok but move it to 5pm", "thanks, also add milk", "great idea, let's do it",
                     "okay so what about tomorrow", "got it, thanks"]:
            with self.subTest(text=text):
                self.assertFalse(self.ACK_RE.match(text), f"should NOT match: {text!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
