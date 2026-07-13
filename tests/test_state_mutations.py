"""State-mutation suite: items, facts, and preferences — the three separate
stores aissistant uses (see memory.SCHEMA). Items are the to-do list, facts
are durable free-text context the model reads every turn, preferences are a
fixed, validated set of behavioral toggles specific code checks. Confusing
the three is exactly how empty promises happen: a request that maps to a
real preference key is enforced; one that only becomes a fact is not, unless
something was actually built to read it (see reminder_overdue_label/
emoji_level in scheduler.py). This suite exists to make every declared
preference key provably wired, and to pin down edge-case behavior at the
storage layer so regressions like the "daily" recurrence bug or the
digest-enable gap can't recur silently.
Run: ./venv/bin/python -m unittest discover tests -v"""
import os
import pathlib
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import memory  # noqa: E402

TEST_DB = pathlib.Path("/tmp/penny_state_mutations.db")


def fresh_db():
    memory.DB_PATH = TEST_DB
    if TEST_DB.exists():
        os.remove(TEST_DB)
    memory.init()


class TestItemMutationEdgeCases(unittest.TestCase):
    def setUp(self):
        fresh_db()

    def test_get_item_missing_id_returns_none(self):
        self.assertIsNone(memory.get_item(99999))

    def test_complete_item_missing_id_does_not_raise(self):
        memory.complete_item(99999)  # must be a safe no-op, not a crash

    def test_update_item_missing_id_does_not_raise(self):
        memory.update_item(99999, title="ghost")  # no row to touch, no error

    def test_update_item_rejects_disallowed_fields_silently(self):
        """update_item's allowlist must actually filter — a caller passing an
        unexpected kwarg (typo, or a field that shouldn't be externally
        settable) must not reach raw SQL."""
        i = memory.add_item("x")
        memory.update_item(i, title="y", not_a_real_column="whatever", id=999)
        row = memory.get_item(i)
        self.assertEqual(row["title"], "y")
        self.assertEqual(row["id"], i)  # the bogus id=999 must not have applied

    def test_update_item_with_no_valid_fields_is_a_safe_noop(self):
        i = memory.add_item("x")
        memory.update_item(i)  # no kwargs at all
        memory.update_item(i, bogus="value")  # only disallowed kwargs
        self.assertEqual(memory.get_item(i)["title"], "x")

    def test_complete_item_on_dropped_item_is_a_noop(self):
        """The open-status guard added for the double-tap bug must also cover
        'dropped', not just 'done' — a stale button on a dropped item must
        not resurrect/complete it or spawn a recurrence."""
        i = memory.add_item("x", due_at="2026-08-01T09:00:00", recurrence="weekly")
        memory.update_item(i, status="dropped")
        memory.complete_item(i)
        self.assertEqual(memory.get_item(i)["status"], "dropped")
        self.assertEqual(len(memory.open_items()), 0)  # no recurrence spawned

    def test_empty_string_recurrence_behaves_like_none(self):
        """add_item's validation only runs 'if recurrence' — an empty string
        is falsy and skips validation, so it must not silently misbehave on
        completion (no crash, no bogus respawn)."""
        i = memory.add_item("x", due_at="2026-08-01T09:00:00", recurrence="")
        memory.complete_item(i)
        self.assertEqual(len(memory.open_items()), 0)

    def test_update_item_recurrence_none_string_is_rejected(self):
        """Contract check: memory.update_item's recurrence validation treats
        the literal string 'none' as an invalid unit (only RECURRENCE_UNITS
        or Python None clear a recurrence). brain._run_tool translates the
        model's 'none' to Python None before calling memory — this test
        pins that translation as load-bearing, not incidental."""
        i = memory.add_item("x", due_at="2026-08-01T09:00:00", recurrence="weekly")
        with self.assertRaises(ValueError):
            memory.update_item(i, recurrence="none")
        memory.update_item(i, recurrence=None)  # the actual way to clear it
        self.assertIsNone(memory.get_item(i)["recurrence"])

    def test_remind_at_independent_of_due_at_when_both_given(self):
        i = memory.add_item("x", due_at="2026-08-10T17:00:00", remind_at="2026-08-08T09:00:00")
        row = memory.get_item(i)
        self.assertEqual(row["due_at"], "2026-08-10T17:00:00")
        # scheduled ping lives in `reminders` now; next_remind_at is nag-only
        self.assertEqual(memory.pending_reminder_times(i), ["2026-08-08T09:00:00"])
        self.assertIsNone(row["next_remind_at"])

    def test_open_items_ordering_priority_desc_then_due_at(self):
        memory.add_item("low, no due", priority=2)
        memory.add_item("high, later due", priority=5, due_at="2026-09-01T09:00:00")
        memory.add_item("high, sooner due", priority=5, due_at="2026-08-01T09:00:00")
        memory.add_item("mid, no due", priority=3)
        titles = [i["title"] for i in memory.open_items()]
        self.assertEqual(titles, ["high, sooner due", "high, later due", "mid, no due", "low, no due"])

    def test_due_reminders_excludes_items_with_no_reminder_set(self):
        """An item with neither due_at nor remind_at must never be treated as
        due — no scheduled ping was ever created, and there's no due_at to
        make it nag-eligible either."""
        memory.add_item("no ping wanted", due_at=None, remind_at=None)
        now = datetime.now(config.TZ)
        self.assertEqual(memory.due_scheduled_reminders(now), [])
        self.assertEqual(memory.due_nags(now), [])

    def test_stale_open_items_excludes_recurring_and_respects_cutoff(self):
        old = (datetime.now(config.TZ) - timedelta(days=20)).isoformat(timespec="seconds")
        recent = (datetime.now(config.TZ) - timedelta(days=1)).isoformat(timespec="seconds")
        with memory._c() as con:
            con.execute("INSERT INTO items (title, status, created_at) VALUES (?,?,?)", ("old one-off", "open", old))
            con.execute("INSERT INTO items (title, status, created_at) VALUES (?,?,?)", ("recent one-off", "open", recent))
            con.execute("INSERT INTO items (title, status, created_at, recurrence, due_at) VALUES (?,?,?,?,?)",
                        ("old but recurring", "open", old, "weekly", "2026-08-01T09:00:00"))
        stale = [i["title"] for i in memory.stale_open_items(14)]
        self.assertEqual(stale, ["old one-off"])


class TestFactMutationEdgeCases(unittest.TestCase):
    def setUp(self):
        fresh_db()

    def test_delete_fact_missing_id_does_not_raise(self):
        memory.delete_fact(99999)

    def test_all_facts_ordered_by_id(self):
        ids = [memory.add_fact(f"fact {i}") for i in range(5)]
        self.assertEqual([f["id"] for f in memory.all_facts()], ids)

    def test_replace_facts_is_a_full_atomic_replace(self):
        memory.add_fact("old one")
        memory.add_fact("old two")
        memory.replace_facts([("new one", "general")])
        contents = [f["content"] for f in memory.all_facts()]
        self.assertEqual(contents, ["new one"])

    def test_replace_facts_has_no_guard_at_the_memory_layer(self):
        """By design: memory.replace_facts() is a dumb, unguarded primitive —
        the wipe-prevention guard lives in maintenance.fact_maintenance(),
        which counts usable rows before ever calling this. Any other future
        caller of replace_facts() must bring its own safety check; this test
        documents that the memory layer will not save them from a mistake."""
        memory.add_fact("only fact")
        memory.replace_facts([])
        self.assertEqual(memory.all_facts(), [])


class TestPreferenceMutationEdgeCases(unittest.TestCase):
    def setUp(self):
        fresh_db()

    def test_get_setting_missing_key_returns_none(self):
        self.assertIsNone(memory.get_setting("nope"))

    def test_set_setting_overwrites_existing_value(self):
        memory.set_setting("k", "1")
        memory.set_setting("k", "2")
        self.assertEqual(memory.get_setting("k"), "2")

    def test_pref_int_falls_back_on_garbage_stored_value(self):
        import scheduler
        memory.set_setting("pref_max_nags", "not-a-number")
        self.assertEqual(scheduler.pref_int("max_nags", 12), 12)


# Every set_preference key must appear here with one valid and one invalid
# value — adding a key to brain.py's enum without extending this map fails
# the coverage test below, forcing new preferences to prove they validate.
VALID_VALUES = {
    "reminder_style": "persistent",
    "quiet_start_hour": "5",
    "quiet_end_hour": "9",
    "morning_digest_time": "07:15",
    "evening_digest_time": "21:45",
    "morning_digest_enabled": "no",
    "evening_digest_enabled": "no",
    "notifications_enabled": "no",
    "gmail_watch_enabled": "no",
    "nag_interval_p5": "10",
    "nag_interval_p4": "10",
    "nag_interval_p3": "10",
    "nag_interval_p2": "10",
    "max_nags": "3",
    "emoji_level": "normal",
    "reply_length": "short",
    "digest_show_completed": "no",
    "reminder_overdue_label": "no",
    "daily_ping_cap": "5",
}
INVALID_VALUES = {
    "reminder_style": "aggressive",
    "quiet_start_hour": "25",
    "quiet_end_hour": "-1",
    "morning_digest_time": "9pm",
    "evening_digest_time": "24:00",
    "morning_digest_enabled": "sure",
    "evening_digest_enabled": "sure",
    "notifications_enabled": "sure",
    "gmail_watch_enabled": "sure",
    "nag_interval_p5": "soon",
    "nag_interval_p4": "soon",
    "nag_interval_p3": "soon",
    "nag_interval_p2": "soon",
    "max_nags": "many",
    "emoji_level": "lots",
    "reply_length": "long",
    "digest_show_completed": "sure",
    "reminder_overdue_label": "sure",
    "daily_ping_cap": "many",
}


class TestEveryPreferenceKeyValidates(unittest.TestCase):
    """Data-driven sweep: every key set_preference advertises must reject a
    bad value and accept+persist a good one. This is the test that would
    have caught the 'daily' recurrence gap and the missing digest-enable
    mechanism before they shipped — an unvalidated or unenforced key can no
    longer be added silently."""

    def setUp(self):
        fresh_db()
        import brain
        self.brain = brain

    def test_every_enum_key_has_valid_and_invalid_cases_defined(self):
        tools = {t["name"]: t for t in self.brain._tools()}
        keys = tools["set_preference"]["input_schema"]["properties"]["key"]["enum"]
        for key in keys:
            self.assertIn(key, VALID_VALUES, f"{key} has no valid-value test case — add one")
            self.assertIn(key, INVALID_VALUES, f"{key} has no invalid-value test case — add one")

    def test_invalid_values_are_rejected(self):
        for key, bad in INVALID_VALUES.items():
            with self.subTest(key=key):
                r = self.brain._run_tool("set_preference", {"key": key, "value": bad})
                self.assertNotIn("updated", r.lower(), f"{key} accepted invalid value {bad!r}: {r}")
                self.assertIsNone(memory.get_setting("pref_" + key))

    def test_valid_values_are_accepted_and_persisted(self):
        for key, good in VALID_VALUES.items():
            with self.subTest(key=key):
                r = self.brain._run_tool("set_preference", {"key": key, "value": good})
                self.assertIn("updated", r.lower(), f"{key} rejected valid value {good!r}: {r}")
                self.assertEqual(memory.get_setting("pref_" + key), good)


if __name__ == "__main__":
    unittest.main(verbosity=2)
