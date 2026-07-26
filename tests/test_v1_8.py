"""v1.8 suite — the fixes from the 2026-07-25 transcript review of jarvis
(07-19 → 07-25) and penny (07-19 → 07-25). Every test here is written against
a specific thing that actually went wrong in a live chat; the docstrings name
the incident so a future session can tell a regression from a redesign.
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

TEST_DB = pathlib.Path("/tmp/penny_v1_8.db")


def fresh_db():
    memory.DB_PATH = TEST_DB
    if TEST_DB.exists():
        os.remove(TEST_DB)
    memory.init()


def run(coro):
    return asyncio.run(coro)


def iso(dt):
    return dt.isoformat(timespec="seconds")


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, **kw):
        self.sent.append(kw)


class FakeContext:
    def __init__(self):
        self.bot = FakeBot()


class _FakeTextBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _FakeToolBlock:
    def __init__(self, name, inp, block_id="toolu_x"):
        self.type = "tool_use"
        self.id = block_id
        self.name = name
        self.input = inp


class _FakeResp:
    def __init__(self, *blocks):
        self.content = list(blocks)
        self.usage = object()
        self.stop_reason = "end_turn"


def text_resp(text):
    return _FakeResp(_FakeTextBlock(text))


class _Script:
    """Replays canned model responses and counts how many calls were made."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0

    def __call__(self, **kw):
        self.calls += 1
        if not self.responses:
            raise AssertionError(f"model called {self.calls} times — more than the test scripted")
        return self.responses.pop(0)


# ---------------------------------------------------------------- recurrence


class TestMissedRecurrenceRollsForward(unittest.TestCase):
    """Real incident (jarvis, 2026-07-23 → 07-25): the daily pushup item was
    never checked off on the 23rd. A recurring item's next occurrence is only
    born inside complete_item, so the series simply STOPPED — item #98 sat
    open, due 07-23 11:59pm, remind_count 2, while the 07-24 and 07-25 morning
    digests both presented it as *today's* pushups (0/100, "log as you go").
    A missed day must roll the cycle forward, not end it."""

    def setUp(self):
        fresh_db()

    def test_daily_item_missed_two_days_rolls_to_today(self):
        now = datetime.now(config.TZ)
        two_days_ago = (now - timedelta(days=2)).replace(hour=23, minute=59, second=0, microsecond=0)
        i = memory.add_item("Pushups", due_at=iso(two_days_ago), recurrence="daily",
                            progress_target=100)
        memory.log_progress(i, delta=25)
        memory.update_item(i, remind_count=2)

        rolled = memory.roll_forward_recurring(now)

        self.assertEqual([r[0] for r in rolled], [i])
        row = memory.get_item(i)
        self.assertEqual(row["status"], "open")
        self.assertEqual(memory.parse_dt(row["due_at"]).date(), now.date())
        self.assertEqual(memory.parse_dt(row["due_at"]).hour, 23)  # keeps its time of day
        self.assertEqual(row["progress_current"], 0, "a new day is a clean slate, never a carried count")
        self.assertEqual(row["remind_count"], 0, "the new occurrence starts its nag chain fresh")

    def test_todays_occurrence_is_left_alone(self):
        now = datetime.now(config.TZ)
        later_today = now.replace(hour=23, minute=59, second=0, microsecond=0)
        i = memory.add_item("Pushups", due_at=iso(later_today), recurrence="daily")
        self.assertEqual(memory.roll_forward_recurring(now), [])
        self.assertEqual(memory.get_item(i)["due_at"], iso(later_today))

    def test_weekly_item_keeps_its_weekday(self):
        now = datetime.now(config.TZ)
        three_weeks_ago = (now - timedelta(days=21)).replace(hour=9, minute=0, second=0, microsecond=0)
        i = memory.add_item("Men's Group", due_at=iso(three_weeks_ago), recurrence="weekly")
        memory.roll_forward_recurring(now)
        new_due = memory.parse_dt(memory.get_item(i)["due_at"])
        self.assertEqual(new_due.weekday(), three_weeks_ago.weekday())
        self.assertGreaterEqual(new_due.date(), now.date())

    def test_never_creates_a_reminder_in_the_past(self):
        """The roll must not reintroduce the 2026-07-24 nudge storm: a big
        due->reminder lead (pushups ping in the morning for an 11:59pm due
        time) would land today's reminder hours before "now"."""
        now = datetime.now(config.TZ).replace(hour=14, minute=0, second=0, microsecond=0)
        yesterday = (now - timedelta(days=1)).replace(hour=23, minute=59)
        morning_ping = (now - timedelta(days=1)).replace(hour=8, minute=0)
        i = memory.add_item("Pushups", due_at=iso(yesterday), remind_at=iso(morning_ping),
                            recurrence="daily")
        memory.roll_forward_recurring(now)
        for t in memory.pending_reminder_times(i):
            self.assertGreater(memory.parse_dt(t), now, f"reminder {t} is in the past")

    def test_series_past_its_end_date_is_retired_not_rolled(self):
        now = datetime.now(config.TZ)
        old = (now - timedelta(days=10)).replace(hour=9, minute=0, second=0, microsecond=0)
        i = memory.add_item("Ann Frank exhibit", due_at=iso(old), recurrence="weekly",
                            recurrence_until=(now - timedelta(days=3)).date().isoformat())
        memory.roll_forward_recurring(now)
        self.assertEqual(memory.get_item(i)["status"], "dropped")

    def test_non_recurring_overdue_items_are_untouched(self):
        now = datetime.now(config.TZ)
        i = memory.add_item("Get groceries", due_at=iso(now - timedelta(days=3)))
        self.assertEqual(memory.roll_forward_recurring(now), [])
        self.assertEqual(memory.get_item(i)["status"], "open")


# ------------------------------------------------------- past-dated captures


class TestPastTimesAreRejected(unittest.TestCase):
    """Real incident (jarvis, 2026-07-24 4:18pm): "remind me in an hour to take
    Wednesday the 19th and Saturday the 22nd in PTO portal" was saved with a
    due_at of 1:18pm the SAME day — three hours in the past. It fired
    immediately, then nudged every 30 minutes: 6 pings in 2 hours, a quarter of
    that day's whole notification volume. Nothing checked the date."""

    def setUp(self):
        fresh_db()
        import brain
        self.brain = brain

    def _capture(self, **extra):
        inp = {"title": "Take Wednesday off in PTO portal", "category": "task", "priority": 3}
        inp.update(extra)
        return self.brain._run_tool("capture_item", inp)

    def test_past_due_at_is_refused_with_the_current_time(self):
        past = datetime.now(config.TZ) - timedelta(hours=3)
        result = self._capture(due_at=iso(past))
        self.assertFalse(result.ok)
        self.assertIn("PAST", result.message)
        self.assertIn("future", result.message)
        self.assertEqual(memory.open_items(), [], "nothing may be saved on a rejected capture")

    def test_past_remind_at_is_refused(self):
        past = datetime.now(config.TZ) - timedelta(minutes=30)
        future = datetime.now(config.TZ) + timedelta(days=1)
        self.assertFalse(self._capture(due_at=iso(future), remind_at=iso(past)).ok)

    def test_one_past_time_in_a_multi_reminder_string_is_refused(self):
        future = datetime.now(config.TZ) + timedelta(days=2)
        past = datetime.now(config.TZ) - timedelta(days=1)
        self.assertFalse(self._capture(due_at=iso(future),
                                       remind_at=f"{iso(future)} | {iso(past)}").ok)

    def test_future_times_are_accepted(self):
        future = datetime.now(config.TZ) + timedelta(hours=1)
        self.assertTrue(self._capture(due_at=iso(future)).ok)
        self.assertEqual(len(memory.open_items()), 1)

    def test_a_few_seconds_of_slack_is_tolerated(self):
        """"remind me now"-shaped asks and ordinary thinking latency must not
        trip this — only a real, minutes-or-hours-wrong timestamp does."""
        just_now = datetime.now(config.TZ) - timedelta(seconds=20)
        self.assertTrue(self._capture(due_at=iso(just_now)).ok)

    def test_update_may_move_a_due_date_backwards_but_not_a_ping(self):
        """Asymmetric on purpose: repairing a date that was saved wrong last
        week is legitimate; a *ping* in the past can only fire instantly."""
        i = memory.add_item("Dentist", due_at=iso(datetime.now(config.TZ) + timedelta(days=5)))
        past = datetime.now(config.TZ) - timedelta(days=2)
        self.assertTrue(self.brain._run_tool("update_item", {"item_id": i, "due_at": iso(past)}).ok)
        self.assertFalse(self.brain._run_tool("update_item", {"item_id": i, "remind_at": iso(past)}).ok)


# ------------------------------------------------ guard prose / partial lies


class TestGuardRetryProseIsScrubbed(unittest.TestCase):
    """THE most-visible failure of the 07-13→07-25 window and the reason
    acceptance criterion 6 failed: 23 guard trips on jarvis, and every one
    traced back to the items table showed the action DID succeed on the
    corrective retry. What he saw was "You're right — I didn't actually
    capture that" and "You're right — I need to actually complete that item"
    about work committed to the database that same second. He answered one with
    a bare "What?". The prompt already asks the model not to say this; the code
    knows the retry worked, so the apology is now discarded outright."""

    def setUp(self):
        fresh_db()
        import brain
        self.brain = brain
        memory.set_setting("owner_chat_id", "12345")

    def _respond(self, script, user_text):
        orig_create, orig_judge = self.brain.client.messages.create, self.brain.llm_claims_change
        self.brain.client.messages.create = script
        self.brain.llm_claims_change = lambda text: False
        try:
            return self.brain.respond(user_text)
        finally:
            self.brain.client.messages.create = orig_create
            self.brain.llm_claims_change = orig_judge

    def test_successful_retry_never_reports_a_failure(self):
        # verbatim from jarvis 2026-07-21 3:26pm: the item WAS captured
        script = _Script(
            text_resp("Got it: shop Bills Dolphins tickets tomorrow."),   # claim, zero tools
            _FakeResp(
                _FakeToolBlock("capture_item", {"title": "Shop Bills Dolphins tickets on Gametime",
                                                "category": "task", "priority": 3}),
                _FakeTextBlock("You're right — I didn't actually capture that. Let me do it now."),
            ),
        )
        reply = self._respond(script, "Remind me tomorrow to shop bills dolphins tickets on Gametime")
        self.assertNotIn("You're right", reply)
        self.assertNotIn("didn't actually", reply)
        self.assertIn("Shop Bills Dolphins tickets on Gametime", reply)
        self.assertEqual(len(memory.open_items()), 1)

    def test_check_off_apology_becomes_a_plain_confirmation(self):
        # verbatim from jarvis 2026-07-23 12:03pm: item #94 was completed at
        # that exact second, and the reply read like an unresolved problem
        i = memory.add_item("Code by the pool and tan")
        script = _Script(
            text_resp("Checked off code by the pool."),  # claim, zero tools
            _FakeResp(
                _FakeToolBlock("complete_item", {"item_id": i}),
                _FakeTextBlock("You're right — I need to actually complete that item."),
            ),
        )
        reply = self._respond(script, "Also check off code by the pool")
        self.assertNotIn("need to actually", reply)
        self.assertIn("Code by the pool and tan", reply)
        self.assertEqual(memory.get_item(i)["status"], "done")

    def test_a_clean_retry_reply_is_left_exactly_as_written(self):
        """Scrubbing only ever replaces apologetic prose. A retry that acts and
        explains itself well keeps its own words — this must not flatten every
        reply into a template."""
        i = memory.add_item("Get groceries")
        script = _Script(
            text_resp("Dropped the groceries item."),
            _FakeResp(
                _FakeToolBlock("complete_item", {"item_id": i}),
                _FakeTextBlock("Done — groceries are off the list, and nothing else moved."),
            ),
        )
        reply = self._respond(script, "check off groceries")
        self.assertEqual(reply, "Done — groceries are off the list, and nothing else moved.")


class TestPartialFulfillmentGuard(unittest.TestCase):
    """The empty-promise guard only ever ran when ZERO tools succeeded, so a
    turn that did three real things and fabricated a fourth went unchecked.
    Two live cases:
      - jarvis 2026-07-19: a Jordan reminder and a pushup reset both happened,
        then the reply added "and marked the gym bag done" — an item the
        message never mentioned, never completed.
      - penny 2026-07-24: "store return and the show checked off. I'll
        ping you tomorrow morning at 8 AM about the coffee pod order." Both
        check-offs real; the coffee pods reschedule never happened, and six days
        later that item still carried its original due date."""

    def setUp(self):
        fresh_db()
        import brain
        self.brain = brain
        memory.set_setting("owner_chat_id", "12345")

    def _respond(self, script, user_text):
        orig_create, orig_judge = self.brain.client.messages.create, self.brain.llm_claims_change
        self.brain.client.messages.create = script
        self.brain.llm_claims_change = lambda text: False
        try:
            return self.brain.respond(user_text)
        finally:
            self.brain.client.messages.create = orig_create
            self.brain.llm_claims_change = orig_judge

    def test_promised_ping_with_no_reminder_call_is_challenged(self):
        done = memory.add_item("store return")
        nespresso = memory.add_item("Order more coffee pods",
                                    due_at=iso(datetime.now(config.TZ) - timedelta(days=5)))
        tomorrow = datetime.now(config.TZ) + timedelta(days=1)
        script = _Script(
            _FakeResp(_FakeToolBlock("complete_item", {"item_id": done})),
            text_resp("Done — store return checked off. I'll ping you tomorrow morning at 8 AM "
                      "about the coffee pod order."),
            _FakeResp(
                _FakeToolBlock("update_item", {"item_id": nespresso, "remind_at": iso(tomorrow)}),
                _FakeTextBlock("Done — store return checked off, and the coffee pods ping is set "
                               "for tomorrow at 8am."),
            ),
        )
        reply = self._respond(script, "check off store return but please remind me tomorrow about coffee pods")
        self.assertEqual(script.calls, 3, "the unbacked reminder promise must force one retry")
        self.assertTrue(memory.pending_reminder_times(nespresso), "the promised ping must now exist")
        self.assertIn("coffee pods", reply)

    def test_claimed_completion_with_no_complete_call_is_challenged(self):
        clubs = memory.add_item("Take the gym bag out of the car")
        script = _Script(
            _FakeResp(_FakeToolBlock("capture_item", {"title": "Respond to Jordan", "category": "task",
                                                      "priority": 3})),
            text_resp("Got it: respond to Jordan in an hour, and marked the gym bag done."),
            text_resp("Got it: respond to Jordan in an hour. I left the gym bag on your list — say the "
                      "word and I'll check it off."),
        )
        reply = self._respond(script, "Remind me to respond to Jordan in an hour")
        self.assertEqual(script.calls, 3)
        self.assertEqual(memory.get_item(clubs)["status"], "open",
                         "the guard must not 'fix' a fabricated claim by really completing the item")
        self.assertNotIn("marked the gym bag done", reply)

    def test_an_honest_confirmation_costs_no_extra_round_trip(self):
        """The check is deterministic and must stay off the common path — an
        ordinary capture-with-reminder confirmation triggers nothing."""
        tomorrow = datetime.now(config.TZ) + timedelta(days=1)
        script = _Script(
            _FakeResp(_FakeToolBlock("capture_item", {"title": "Order dinner to work", "category": "task",
                                                      "priority": 3, "remind_at": iso(tomorrow)})),
            text_resp("Got it: order dinner to work. I'll ping you tomorrow at 3:30pm."),
        )
        reply = self._respond(script, "Remind me to order dinner to work at 3:30pm tomorrow")
        self.assertEqual(script.calls, 2, "no corrective retry should have fired")
        self.assertIn("order dinner to work", reply)


class TestQuestionMustNotSwallowTheMessage(unittest.TestCase):
    """Real incident (penny, 2026-07-22 9:02pm): one message held four things —
    "I already returned the store package but remind me next week to check if
    I got a refund... and remind me tomorrow to order more coffee pods or remind
    me on Friday as well." Penny asked which day she meant and saved NOTHING,
    completed NOTHING. She never answered, so the refund reminder has never
    existed, and the the store item kept being listed as due for two more days."""

    def setUp(self):
        fresh_db()
        import brain
        self.brain = brain
        memory.set_setting("owner_chat_id", "12345")

    def _respond(self, script, user_text):
        orig_create, orig_judge = self.brain.client.messages.create, self.brain.llm_claims_change
        orig_missed = self.brain._missed_captures
        self.brain.client.messages.create = script
        self.brain.llm_claims_change = lambda text: False
        # the separate capture-completeness pass has its own tests; stubbed out
        # here so its API call doesn't count toward this test's round-trips
        self.brain._missed_captures = lambda user_text, captured: []
        try:
            return self.brain.respond(user_text)
        finally:
            self.brain.client.messages.create = orig_create
            self.brain.llm_claims_change = orig_judge
            self.brain._missed_captures = orig_missed

    def test_bare_question_with_nothing_saved_forces_a_partial_capture(self):
        script = _Script(
            text_resp("I need to clarify the coffee pod reminders — tomorrow *and* Friday, or just one?"),
            _FakeResp(
                _FakeToolBlock("capture_item", {"title": "Check if the store refund posted",
                                                "category": "task", "priority": 3}),
                _FakeTextBlock("Saved the refund check. On the coffee pod reminder — tomorrow, Friday, "
                               "or both?"),
            ),
        )
        reply = self._respond(script, "I already returned the store package but remind me next week "
                                      "to check if I got a refund, and remind me tomorrow to order "
                                      "more coffee pods or remind me on Friday as well.")
        self.assertEqual(script.calls, 2)
        self.assertEqual(len(memory.open_items()), 1, "the unambiguous ask must be banked")
        self.assertIn("?", reply, "the genuinely ambiguous part is still asked about")

    def test_ordinary_question_with_no_capture_intent_is_left_alone(self):
        script = _Script(text_resp("Which store did you have in mind — Costco or Target?"))
        self._respond(script, "where should I buy a space heater?")
        self.assertEqual(script.calls, 1, "plain conversation must not trigger a corrective retry")


class TestClaimPatternsForPartialFulfillment(unittest.TestCase):
    def setUp(self):
        import brain
        self.brain = brain

    def test_reminder_promises_are_detected(self):
        for text in ["I'll remind you tonight at 7:30.",
                     "I'll ping you tomorrow morning at 8 AM about the coffee pod order.",
                     "Your reminder is now set for 8pm.",
                     "Got it — pinged at 8:00am tomorrow to talk to Riley's dad."]:
            with self.subTest(text=text):
                self.assertTrue(self.brain.PROMISED_REMINDER_RE.search(text))

    def test_reporting_an_existing_reminder_is_not_a_promise(self):
        """Answering "when's my next reminder for that?" describes state that
        already exists — flagging it would fire on plain state questions."""
        for text in ["The flowers ping is next Thursday at 9:00am, since it repeats weekly.",
                     "Your reminder for the dentist is tomorrow at 10am.",
                     "You have three things due today."]:
            with self.subTest(text=text):
                self.assertFalse(self.brain.PROMISED_REMINDER_RE.search(text))

    def test_completion_claims_are_detected(self):
        for text in ["and marked the gym bag done.",
                     "Done — bodyweight workout checked off.",
                     "Dinner order and pushups both checked off.",
                     "Job scout and the recruiter completed."]:
            with self.subTest(text=text):
                self.assertTrue(self.brain.CLAIMED_COMPLETION_RE.search(text))

    def test_self_correction_phrases_from_the_live_transcripts(self):
        for text in ["You're right — I didn't actually capture that. Let me do it now.",
                     "You're right — I need to actually complete that item.",
                     "Looking back, I need to actually complete that item.",
                     "My mistake — I should have just confirmed those three items done.",
                     "That didn't actually save on my end — mind sending it again?"]:
            with self.subTest(text=text):
                self.assertTrue(self.brain.SELF_CORRECTION_RE.search(text))

    def test_normal_confirmations_are_not_self_correcting(self):
        for text in ["Done — Code by the pool and tan.",
                     "Got it: order dinner to work tomorrow at 3:30pm.",
                     "Nice — marked lift done for today."]:
            with self.subTest(text=text):
                self.assertFalse(self.brain.SELF_CORRECTION_RE.search(text))


class TestUpdateConfirmationNamesTheChange(unittest.TestCase):
    """Real incident (jarvis, 2026-07-22 5:00am): "Move dinner on Thursday to
    Friday at 7:15pm" was confirmed as `Done — updated "dinner reservation at
    the bistro"` — and the date it actually wrote was wrong. A confirmation
    that names no value is one the owner cannot check."""

    def setUp(self):
        fresh_db()
        import brain
        self.brain = brain

    def test_new_due_date_appears_in_the_description(self):
        d = datetime.now(config.TZ).replace(hour=19, minute=15) + timedelta(days=3)
        desc = self.brain._describe_update({"item_id": 1, "due_at": iso(d)})
        self.assertIn(d.strftime("%A"), desc)
        self.assertIn("7:15 PM", desc)

    def test_recurrence_and_rename_are_described(self):
        self.assertIn("repeats weekly", self.brain._describe_update({"recurrence": "weekly"}))
        self.assertIn("no longer repeating", self.brain._describe_update({"recurrence": "none"}))
        self.assertIn("renamed", self.brain._describe_update({"title": "New name"}))

    def test_nothing_to_describe_is_empty_not_noise(self):
        self.assertEqual(self.brain._describe_update({"item_id": 4}), "")


# ------------------------------------------------------------ date reasoning


class TestDateTableCoversTodayAndTwoWeeks(unittest.TestCase):
    """Real incident (jarvis, 2026-07-25 12:15pm): with the one-week table
    already live, the model called Sunday 07-26 "next Friday (tomorrow)",
    corrected itself to "next Saturday, August 1st", and then saved a
    Thursday-recurring item to Friday 07-31. The table didn't name TODAY, and
    didn't reach far enough to cover where a weekly item's next occurrence
    lands."""

    def setUp(self):
        import brain
        self.brain = brain

    def test_table_starts_at_today(self):
        now = datetime.now(config.TZ)
        line = self.brain._now_line()
        self.assertIn(now.strftime("%a=%Y-%m-%d"), line)

    def test_table_reaches_a_full_two_weeks(self):
        line = self.brain._now_line()
        for days in (7, 13, 14):
            with self.subTest(days=days):
                d = datetime.now(config.TZ) + timedelta(days=days)
                self.assertIn(d.strftime("%a=%Y-%m-%d"), line)

    def test_every_weekday_name_is_available_to_look_up(self):
        line = self.brain._now_line()
        for name in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"):
            with self.subTest(name=name):
                self.assertIn(f"{name}=", line)


# ------------------------------------------------------------------ digests


class TestDigestEntryLabels(unittest.TestCase):
    """Real incident (penny, 2026-07-23 and 07-24): the Odyssey movie was
    Tuesday 07-21, and both mornings after it the digest called it "tonight at
    7 PM". "Due today" deliberately includes overdue items; handing the model
    bare titles left it to guess when each one was for, and it guessed today."""

    def setUp(self):
        fresh_db()
        import scheduler
        self.scheduler = scheduler

    def test_overdue_item_is_labeled_with_its_real_date(self):
        now = datetime.now(config.TZ)
        past = now - timedelta(days=3)
        i = memory.add_item("See movie at the Odyssey", due_at=iso(past))
        label = self.scheduler.digest_entry_label(memory.get_item(i), now)
        self.assertIn("overdue", label)
        self.assertIn(past.strftime("%B %-d"), label)

    def test_todays_item_is_labeled_with_its_time(self):
        now = datetime.now(config.TZ)
        due = now.replace(hour=15, minute=30)
        i = memory.add_item("Order dinner to work", due_at=iso(due))
        label = self.scheduler.digest_entry_label(memory.get_item(i), now)
        self.assertIn("due today at", label)
        self.assertNotIn("overdue", label)


class TestDigestTidying(unittest.TestCase):
    def setUp(self):
        import brain
        self.brain = brain
        self.now = datetime.now(config.TZ)

    def test_duplicate_date_header_is_stripped(self):
        """scheduler.py already prefixes "Morning! It's Saturday, July 25." —
        the model then wrote its own header under it, so the date shipped
        twice on 3 of 5 jarvis digests and 4 of 6 of penny's."""
        raw = "Morning digest for Saturday, July 25\n\nTake PTO portal off first.\n(35 things safely on the list)"
        out = self.brain._tidy_digest(raw, self.now)
        self.assertTrue(out.startswith("Take PTO portal off first."))
        self.assertIn("35 things safely on the list", out)

    def test_bare_weekday_header_is_stripped(self):
        raw = "Morning of Tuesday, July 21\n\nThe one thing: expand the job search."
        self.assertTrue(self.brain._tidy_digest(raw, self.now).startswith("The one thing"))

    def test_a_long_first_line_is_never_mistaken_for_a_header(self):
        raw = ("Morning starts with the dentist at 10am, then groceries, and the Fable prompt "
               "before lunch if you can manage it.\n\nHeads up: nothing else is urgent.")
        out = self.brain._tidy_digest(raw, self.now)
        self.assertTrue(out.startswith("Morning starts with the dentist"))

    def test_all_lowercase_drift_is_capitalized(self):
        """penny's 2026-07-23 digest arrived entirely in lowercase."""
        raw = "order more nespresso (due 8am) — get this done first thing.\nsee movie tonight."
        out = self.brain._tidy_digest(raw, self.now)
        self.assertTrue(out.startswith("Order more nespresso"))
        self.assertIn("\nSee movie tonight.", out)

    def test_normal_mixed_case_text_is_untouched(self):
        raw = "Take PTO portal off first.\n\nThen the Fable prompt."
        self.assertEqual(self.brain._tidy_digest(raw, self.now), raw)


class TestSpareEnergyRotates(unittest.TestCase):
    """penny's "pick timing for birthday sushi" and "check Partiful
    reminders" led the spare-energy section every single morning for over two
    weeks (nagged 7 and 8 times) because a fixed [:3] slice of a stably-ordered
    list is the same three items forever."""

    def setUp(self):
        fresh_db()
        import scheduler
        self.scheduler = scheduler

    def test_different_days_offer_different_items(self):
        for n in range(8):
            memory.add_item(f"undated backlog {n}")
        items = memory.open_items()
        seen = set()
        for day in range(1, 15):
            now = datetime.now(config.TZ).replace(month=1, day=1) + timedelta(days=day)
            _due, spare = self.scheduler.digest_buckets(items, now)
            self.assertLessEqual(len(spare), self.scheduler.SPARE_ENERGY_SHOWN)
            seen.update(i["title"] for i in spare)
        self.assertGreater(len(seen), self.scheduler.SPARE_ENERGY_SHOWN,
                           "every undated item should get a turn across a fortnight")

    def test_a_short_backlog_is_still_shown_in_full(self):
        memory.add_item("only backlog item")
        _due, spare = self.scheduler.digest_buckets(memory.open_items(), datetime.now(config.TZ))
        self.assertEqual([i["title"] for i in spare], ["only backlog item"])

    def test_dated_items_still_never_reach_spare_energy(self):
        memory.add_item("future dinner", due_at=iso(datetime.now(config.TZ) + timedelta(days=4)))
        for n in range(5):
            memory.add_item(f"undated {n}")
        _due, spare = self.scheduler.digest_buckets(memory.open_items(), datetime.now(config.TZ))
        self.assertNotIn("future dinner", [i["title"] for i in spare])


# ---------------------------------------------------------------- ping shape


class TestBundleIsCapped(unittest.TestCase):
    """8 items with 8 four-button rows went out at 8:00am on 2026-07-25, and 7
    at 3:01pm on 07-21. Bundling to one notification was right; an uncapped
    wall of buttons became its own kind of flood. Buttons are gone entirely as
    of the same day (BUTTONS_ARE_V2 in scheduler.py) — the cap now governs how
    many items are listed individually before the rest go in a named tail."""

    def setUp(self):
        fresh_db()
        import scheduler
        self.scheduler = scheduler
        memory.set_setting("owner_chat_id", "12345")

    def test_overflow_items_are_named_in_the_tail_never_dropped(self):
        entries = []
        for n in range(8):
            i = memory.add_item(f"thing {n}")
            entries.append({"item": memory.get_item(i), "kind": "nag", "reminder_id": None})
        ctx = FakeContext()
        run(self.scheduler._send_bundle(ctx, "12345", entries, show_overdue=True))
        sent = ctx.bot.sent[0]
        self.assertIsNone(sent.get("reply_markup"), "Telegram sends are buttonless")
        self.assertIn("8 things need you right now", sent["text"])
        for n in range(8):
            with self.subTest(n=n):
                self.assertIn(f"thing {n}", sent["text"], "no item may be silently dropped")
        self.assertIn("Also waiting", sent["text"])

    def test_a_small_bundle_has_no_overflow_tail(self):
        entries = []
        for n in range(3):
            i = memory.add_item(f"thing {n}")
            entries.append({"item": memory.get_item(i), "kind": "nag", "reminder_id": None})
        ctx = FakeContext()
        run(self.scheduler._send_bundle(ctx, "12345", entries, show_overdue=True))
        self.assertIsNone(ctx.bot.sent[0].get("reply_markup"))
        self.assertNotIn("Also waiting", ctx.bot.sent[0]["text"])


class TestStaleSweep(unittest.TestCase):
    """penny got the stale sweep as a second ping right behind the digest 7
    mornings out of 7 — the same expired items every day — and its body named
    nothing at all ("These came and went — clear, or give them a new time?"),
    so the titles existed only inside the buttons."""

    def setUp(self):
        fresh_db()
        import scheduler
        self.scheduler = scheduler
        memory.set_setting("owner_chat_id", "12345")
        self.old = iso(datetime.now(config.TZ) - timedelta(days=4))

    def test_the_message_names_the_items(self):
        memory.add_item("Order more coffee pods", due_at=self.old)
        ctx = FakeContext()
        run(self.scheduler._stale_sweep(ctx, "12345"))
        self.assertIn("Order more coffee pods", ctx.bot.sent[0]["text"])

    def test_the_same_item_is_not_swept_again_the_next_morning(self):
        memory.add_item("Order more coffee pods", due_at=self.old)
        first = FakeContext()
        run(self.scheduler._stale_sweep(first, "12345"))
        self.assertEqual(len(first.bot.sent), 1)
        second = FakeContext()
        run(self.scheduler._stale_sweep(second, "12345"))
        self.assertEqual(second.bot.sent, [], "an expired item must wait out its cooldown")

    def test_cooldown_expiry_lets_it_resurface(self):
        memory.add_item("Order more coffee pods", due_at=self.old)
        run(self.scheduler._stale_sweep(FakeContext(), "12345"))
        long_ago = (datetime.now(config.TZ)
                    - timedelta(days=self.scheduler.STALE_SWEEP_COOLDOWN_DAYS + 1)).date().isoformat()
        import json
        seen = json.loads(memory.get_setting("stale_swept_on"))
        memory.set_setting("stale_swept_on", json.dumps({k: long_ago for k in seen}))
        ctx = FakeContext()
        run(self.scheduler._stale_sweep(ctx, "12345"))
        self.assertEqual(len(ctx.bot.sent), 1)

    def test_a_newly_expired_item_is_swept_even_while_another_is_cooling_down(self):
        memory.add_item("Order more coffee pods", due_at=self.old)
        run(self.scheduler._stale_sweep(FakeContext(), "12345"))
        memory.add_item("Check Partiful reminders", due_at=self.old)
        ctx = FakeContext()
        run(self.scheduler._stale_sweep(ctx, "12345"))
        self.assertIn("Check Partiful reminders", ctx.bot.sent[0]["text"])
        self.assertNotIn("coffee pods", ctx.bot.sent[0]["text"])

    def test_corrupt_cooldown_state_fails_open(self):
        memory.set_setting("stale_swept_on", "{not json")
        memory.add_item("Order more coffee pods", due_at=self.old)
        ctx = FakeContext()
        run(self.scheduler._stale_sweep(ctx, "12345"))
        self.assertEqual(len(ctx.bot.sent), 1)


class TestTodaysAgendaIsPrecomputed(unittest.TestCase):
    """Real failure (jarvis, 2026-07-25 5:22pm): "What else is on tap for me
    today?" got the answer "Call Dana at 5:45pm." — ONE item out of six that
    were genuinely open and dated today or earlier. The open-items list in the
    state block is flat, priority-sorted and carries raw ISO timestamps, so
    answering a "today" question meant scanning 30 lines and comparing dates by
    eye. Same re-derivation failure the smart digest already hit; same fix."""

    def setUp(self):
        fresh_db()
        import brain
        self.brain = brain
        self.now = datetime.now(config.TZ)

    def test_every_due_or_overdue_item_is_listed_with_a_count(self):
        memory.add_item("Get groceries", due_at=iso(self.now - timedelta(days=2)))
        memory.add_item("Use Fable to get AIssistant public", due_at=iso(self.now - timedelta(days=1)))
        memory.add_item("Call Dana", due_at=iso(self.now.replace(hour=17, minute=45)))
        memory.add_item("Pushups", due_at=iso(self.now.replace(hour=23, minute=59)))
        memory.add_item("Doctor appointment", due_at=iso(self.now + timedelta(days=2)))

        agenda = self.brain._agenda_text(memory.open_items())
        due_line = [l for l in agenda.splitlines() if "DUE TODAY OR OVERDUE" in l][0]
        self.assertIn("(4)", due_line, "the count must let him see nothing was trimmed")
        for title in ("Get groceries", "Use Fable", "Call Dana", "Pushups"):
            with self.subTest(title=title):
                self.assertIn(title, due_line)
        self.assertNotIn("Doctor appointment", due_line)

    def test_overdue_items_are_marked_overdue_with_their_real_date(self):
        past = self.now - timedelta(days=2)
        memory.add_item("Get groceries", due_at=iso(past))
        agenda = self.brain._agenda_text(memory.open_items())
        self.assertIn("OVERDUE", agenda)
        self.assertIn(past.strftime("%b %-d"), agenda)

    def test_undated_backlog_is_kept_separate_from_today(self):
        memory.add_item("Run the graph article by coworker")
        agenda = self.brain._agenda_text(memory.open_items())
        due_line = [l for l in agenda.splitlines() if "DUE TODAY OR OVERDUE" in l][0]
        no_date_line = [l for l in agenda.splitlines() if "NO DATE" in l][0]
        self.assertNotIn("graph article", due_line)
        self.assertIn("graph article", no_date_line)

    def test_the_agenda_is_stable_within_a_day_so_the_state_block_still_caches(self):
        memory.add_item("Call Dana", due_at=iso(self.now.replace(hour=17, minute=45)))
        self.assertEqual(self.brain._agenda_text(memory.open_items()),
                         self.brain._agenda_text(memory.open_items()))
        self.assertNotIn("Now:", self.brain._agenda_text(memory.open_items()))


class TestPingTodayCountsAsToday(unittest.TestCase):
    """Real hole (jarvis, 2026-07-25 4:21pm): "Remind me to respond to Jordan at
    7pm tonight" saved a 7pm reminder and NO due_at. The ping would have fired
    correctly, but the item was invisible to every "what's due today"
    calculation — including the morning digest — and would have been offered
    under "if there's spare energy" instead. Note the deliberate asymmetry with
    a night-before ping for a later event."""

    def setUp(self):
        fresh_db()
        import brain
        import scheduler
        self.brain = brain
        self.scheduler = scheduler
        self.now = datetime.now(config.TZ)

    def test_undated_item_pinging_today_is_a_today_item(self):
        memory.add_item("Respond to Jordan", remind_at=iso(self.now.replace(hour=19, minute=0)))
        due, spare = self.scheduler.digest_buckets(memory.open_items(), self.now)
        self.assertEqual([i["title"] for i in due], ["Respond to Jordan"])
        self.assertEqual(spare, [], "something with a ping tonight is not spare-energy backlog")

    def test_its_ping_time_is_named_in_the_agenda(self):
        memory.add_item("Respond to Jordan", remind_at=iso(self.now.replace(hour=19, minute=0)))
        self.assertIn("ping 7:00 PM today", self.brain._agenda_text(memory.open_items()))

    def test_a_night_before_ping_for_a_later_event_is_not_today(self):
        """Polo Club, live: pings 9pm tonight, due 10:30am tomorrow. The event
        is tomorrow's — it must not be listed among today's work."""
        tomorrow = (self.now + timedelta(days=1)).replace(hour=10, minute=30)
        memory.add_item("Go to Polo Club for 10:30am appointment",
                        due_at=iso(tomorrow), remind_at=iso(self.now.replace(hour=21, minute=0)))
        due, _spare = self.scheduler.digest_buckets(memory.open_items(), self.now)
        self.assertEqual(due, [])

    def test_undated_item_pinging_a_future_day_is_neither_today_nor_spare_energy(self):
        memory.add_item("Check on the refund", remind_at=iso(self.now + timedelta(days=4)))
        due, spare = self.scheduler.digest_buckets(memory.open_items(), self.now)
        self.assertEqual(due, [])
        self.assertEqual(spare, [], "a scheduled ping means scheduled, not flexible backlog")

    def test_genuinely_undated_backlog_still_reaches_spare_energy(self):
        memory.add_item("Run the graph article by coworker")
        _due, spare = self.scheduler.digest_buckets(memory.open_items(), self.now)
        self.assertEqual([i["title"] for i in spare], ["Run the graph article by coworker"])


class TestGuardNeverLeaksItsOwnMachinery(unittest.TestCase):
    """Real failure (jarvis, 2026-07-25 5:11pm) — the entire reply he received:
    "Looking back at his actual request: 'Whats left today?' — that was a
    question about what's on his list for today, not a request for any change.
    I answered it accurately from the state... No tool call was needed; I
    should have just answered the question plainly... I'll reply correctly if he
    messages again." That is the model's reasoning about the injected system
    check, in the third person, delivered as a reply. The same turn also lost
    the "Responded to Jordan, you can complete that" check-off entirely."""

    def setUp(self):
        fresh_db()
        import brain
        self.brain = brain
        memory.set_setting("owner_chat_id", "12345")

    def _respond(self, script, user_text):
        orig_create, orig_judge = self.brain.client.messages.create, self.brain.llm_claims_change
        orig_missed = self.brain._missed_captures
        self.brain.client.messages.create = script
        self.brain.llm_claims_change = lambda text: False
        self.brain._missed_captures = lambda user_text, captured: []
        try:
            return self.brain.respond(user_text)
        finally:
            self.brain.client.messages.create = orig_create
            self.brain.llm_claims_change = orig_judge
            self.brain._missed_captures = orig_missed

    LEAK = ("Looking back at his actual request: \"Whats left today?\" — that was a question about "
            "what's on his list, not a request for any change. No tool call was needed. I'll reply "
            "correctly if he messages again.")

    def test_the_verbatim_live_leak_is_detected(self):
        self.assertTrue(self.brain._detects_leak(self.LEAK))

    def test_ordinary_replies_are_not_flagged_as_leaks(self):
        for text in ["Call Dana at 5:45pm is what's left today.",
                     "Done — groceries checked off.",
                     "Jordan said he'd get back to you tomorrow.",
                     "You have six things left today: groceries, the PTO portal request, and four more."]:
            with self.subTest(text=text):
                self.assertFalse(self.brain._detects_leak(text))

    def test_a_leaked_reply_is_replaced_by_a_clean_re_answer(self):
        memory.add_item("Call Dana", due_at=iso(datetime.now(config.TZ).replace(hour=17, minute=45)))
        script = _Script(
            text_resp("Checked off the Jordan item — here's what's left today."),  # claim, no tools
            text_resp(self.LEAK),                                             # leak, still no tools
            text_resp("Call Dana at 5:45pm is the only thing left today."),  # clean
        )
        reply = self._respond(script, "Responded to Jordan, you can complete that. Whats left today?")
        self.assertEqual(script.calls, 3)
        self.assertEqual(reply, "Call Dana at 5:45pm is the only thing left today.")
        self.assertNotIn("he messages", reply)

    def test_a_second_leak_falls_back_instead_of_shipping_it(self):
        script = _Script(
            text_resp("Checked off the Jordan item."),
            text_resp(self.LEAK),
            text_resp("As I said, no tool call was needed for his question."),
        )
        reply = self._respond(script, "Responded to Jordan, you can complete that. Whats left today?")
        self.assertNotIn("his question", reply)
        self.assertNotIn("no tool call", reply)


class TestGuardDoesNotOfferAnEscapeHatchWhenActionWasAsked(unittest.TestCase):
    """The "nothing actually needed fixing" outcome was added 2026-07-16 for a
    real case (a diagnostic question whose accurate answer tripped the guard).
    On 2026-07-25 it got abused: given "Responded to Jordan, you can complete
    that. Whats left today?" the model took the hatch, decided he'd only asked
    a question, completed nothing — and that item was still open hours later.
    Whether the message contains a real ask is something code can decide."""

    def setUp(self):
        fresh_db()
        import brain
        self.brain = brain
        memory.set_setting("owner_chat_id", "12345")

    def _corrective_note_for(self, user_text):
        notes = []
        orig_create, orig_judge = self.brain.client.messages.create, self.brain.llm_claims_change

        def capture(**kw):
            for m in kw["messages"]:
                if m["role"] == "user" and isinstance(m["content"], list):
                    for block in m["content"]:
                        if block.get("type") == "text" and "automated system check" in block["text"]:
                            notes.append(block["text"])
            return text_resp("Fine.")

        self.brain.client.messages.create = capture
        self.brain.llm_claims_change = lambda text: True
        try:
            self.brain.respond(user_text)
        finally:
            self.brain.client.messages.create = orig_create
            self.brain.llm_claims_change = orig_judge
        return notes[0] if notes else ""

    def test_hatch_is_withheld_when_he_asked_for_a_completion(self):
        note = self._corrective_note_for("Responded to Jordan, you can complete that. Whats left today?")
        self.assertIn("is NOT available here", note)
        self.assertIn("DID ask you to record", note)

    def test_hatch_is_offered_for_a_plain_diagnostic_question(self):
        note = self._corrective_note_for("why did you nudge me a minute after the initial reminder?")
        self.assertIn("nothing actually needed fixing", note)


if __name__ == "__main__":
    unittest.main()


class TestBrokenGoogleAuthIsNotAnEmptyCalendar(unittest.TestCase):
    """penny's Google grant died 2026-07-16 and for the nine days after it her
    calendar simply looked EMPTY — to the model on every turn, and to anyone
    who asked her what was coming up. upcoming_events() returns [] on any
    failure and _upcoming_text_fresh turned [] into "(no events in the next 7
    days)", which is an affirmative lie rather than a missing feature."""

    def setUp(self):
        import gcal
        self.gcal = gcal
        self.gcal._auth_state["broken"] = False
        self.gcal._cal_cache.update(ts=0.0, text="")
        # test_live.py points gcal.GOOGLE_TOKEN at a nonexistent file at import
        # time, and `unittest discover` imports it — so enabled() is False by
        # the time this class runs in the full suite. Don't depend on either.
        self._orig_enabled = gcal.enabled
        gcal.enabled = lambda: True

    def tearDown(self):
        self.gcal.enabled = self._orig_enabled
        self.gcal._auth_state["broken"] = False
        self.gcal._cal_cache.update(ts=0.0, text="")

    def test_a_dead_grant_is_recorded_not_swallowed(self):
        class _RefreshError(Exception):
            pass
        _RefreshError.__name__ = "RefreshError"

        orig = self.gcal._svc
        self.gcal._svc = lambda: (_ for _ in ()).throw(_RefreshError("invalid_grant: Token expired"))
        try:
            self.assertEqual(self.gcal.upcoming_events(7), [])
            self.assertTrue(self.gcal.auth_broken())
        finally:
            self.gcal._svc = orig

    def test_the_state_block_text_refuses_to_claim_an_empty_calendar(self):
        self.gcal._auth_state["broken"] = True
        orig = self.gcal.upcoming_events
        self.gcal.upcoming_events = lambda days=7: []
        try:
            text = self.gcal._upcoming_text_fresh(7)
        finally:
            self.gcal.upcoming_events = orig
        self.assertIn("UNREADABLE", text)
        self.assertNotIn("no events", text)

    def test_a_genuinely_empty_calendar_still_reads_as_empty(self):
        orig = self.gcal.upcoming_events
        self.gcal.upcoming_events = lambda days=7: []
        try:
            self.assertIn("no events", self.gcal._upcoming_text_fresh(7))
        finally:
            self.gcal.upcoming_events = orig


class TestHeartbeatCatchesDeadGoogleAuth(unittest.TestCase):
    """"Alive but not actually working" is this watchdog's entire reason to
    exist, and a dead Google grant is exactly that shape — nine days silent."""

    def setUp(self):
        import heartbeat
        self.heartbeat = heartbeat

    def _check_with_log(self, lines):
        orig_run, orig_lines = self.heartbeat._process_running, self.heartbeat._log_lines_since
        self.heartbeat._process_running = lambda instance: True
        self.heartbeat._log_lines_since = lambda path, minutes: lines
        try:
            return self.heartbeat.check_instance("penny")
        finally:
            self.heartbeat._process_running = orig_run
            self.heartbeat._log_lines_since = orig_lines

    def test_invalid_grant_in_the_log_raises_an_alert_naming_the_fix(self):
        alert = self._check_with_log([
            "2026-07-25 08:00:40,007 penny.gcal ERROR calendar fetch failed",
            "google.auth.exceptions.RefreshError: ('invalid_grant: Token has been expired or revoked.')",
        ])
        self.assertIsNotNone(alert)
        self.assertIn("Google auth is dead", alert)
        self.assertIn("setup_google.py", alert)

    def test_a_healthy_log_still_reports_healthy(self):
        self.assertIsNone(self._check_with_log([
            "2026-07-25 17:34:12,208 apscheduler INFO Job check_reminders executed successfully",
        ]))


class TestMultiPartMessageGetsAFullAnswer(unittest.TestCase):
    """Brooks: "Why doesn't Jarvis answer when I have multiple items in a
    message and ask what else is on my list?"

    Traced to the guard log. At 2026-07-25 5:22pm he sent "bistro dinner was
    last night, you can complete it. What else is on tap for me today?" and the
    model's FIRST draft was right on both counts — "Done — the bistro dinner.
    Today (Saturday, July 25): Call Dana at 5:45pm is all that's left." — but
    it made zero tool calls, so the empty-promise guard fired. The retry made
    the real complete_item call and replied only "You're right — I need to
    actually complete that item", and _scrub_self_correction replaced that
    wholesale with the deterministic confirmation. Action correct, question
    gone. Same shape at 5:11pm and 5:12pm: three turns in one afternoon.

    Every guard was built to make an ACTION correct; none of them knew the
    message might have a second half."""

    def setUp(self):
        fresh_db()
        import brain
        self.brain = brain
        memory.set_setting("owner_chat_id", "12345")

    def _respond(self, script, user_text):
        orig_create, orig_judge = self.brain.client.messages.create, self.brain.llm_claims_change
        orig_missed = self.brain._missed_captures
        self.brain.client.messages.create = script
        self.brain.llm_claims_change = lambda text: False
        self.brain._missed_captures = lambda user_text, captured: []
        try:
            return self.brain.respond(user_text)
        finally:
            self.brain.client.messages.create = orig_create
            self.brain.llm_claims_change = orig_judge
            self.brain._missed_captures = orig_missed

    ASK = "bistro dinner was last night, you can complete it. What else is on tap for me today?"

    def test_question_detection_on_the_real_message_shapes(self):
        for text in [self.ASK,
                     "Responded to Jordan, you can complete that. Whats left today?",
                     "Done on both, also completed the list of refer-able network\n\nWhat's left?",
                     "What else do I have to do today?"]:
            with self.subTest(text=text):
                self.assertTrue(self.brain._asks_a_question(text))

    def test_plain_capture_is_not_treated_as_a_question(self):
        for text in ["Remind me to call grandma tomorrow at 6:30pm",
                     "Did 25 pushups",
                     "Lifting now, you can complete that for the day"]:
            with self.subTest(text=text):
                self.assertFalse(self.brain._asks_a_question(text))

    def test_the_answer_survives_the_scrub(self):
        """The exact 5:22pm turn: apologetic retry, real tool call, and an
        answer that must not be thrown away with the apology."""
        dinner = memory.add_item("Dinner reservation at the bistro")
        memory.add_item("Call Dana", due_at=iso(datetime.now(config.TZ).replace(hour=17, minute=45)))
        script = _Script(
            text_resp("Done — the bistro dinner.\n\nToday: Call Dana at 5:45pm is all that's left."),
            _FakeResp(
                _FakeToolBlock("complete_item", {"item_id": dinner}),
                _FakeTextBlock("You're right — I need to actually complete that item. "
                               "Today: Call Dana at 5:45pm is all that's left."),
            ),
        )
        reply = self._respond(script, self.ASK)
        self.assertEqual(memory.get_item(dinner)["status"], "done")
        self.assertNotIn("You're right", reply)
        self.assertIn("Call Dana", reply, "the answer to his question must survive")
        self.assertIn("the bistro", reply, "and so must the confirmation")

    def test_an_apology_with_no_answer_costs_one_more_call_to_get_one(self):
        """When nothing survives the scrub and a question is outstanding, a
        confirmation-only reply is exactly the complaint — spend a round-trip."""
        dinner = memory.add_item("Dinner reservation at the bistro")
        script = _Script(
            text_resp("Done — the bistro dinner. Today: Call Dana at 5:45pm."),
            _FakeResp(
                _FakeToolBlock("complete_item", {"item_id": dinner}),
                _FakeTextBlock("You're right — I need to actually complete that item."),
            ),
            text_resp("Call Dana at 5:45pm is the only thing left today."),
        )
        reply = self._respond(script, self.ASK)
        self.assertEqual(script.calls, 3)
        self.assertIn("Call Dana", reply)
        self.assertNotIn("You're right", reply)

    def test_no_extra_call_when_there_was_no_question(self):
        dinner = memory.add_item("Dinner reservation at the bistro")
        script = _Script(
            text_resp("Checked off the the bistro dinner."),
            _FakeResp(
                _FakeToolBlock("complete_item", {"item_id": dinner}),
                _FakeTextBlock("You're right — I need to actually complete that item."),
            ),
        )
        reply = self._respond(script, "bistro dinner was last night, you can complete it.")
        self.assertEqual(script.calls, 2, "no question outstanding — nothing to chase")
        self.assertIn("the bistro", reply)
        self.assertNotIn("You're right", reply)

    def test_the_corrective_note_demands_both_halves(self):
        dinner = memory.add_item("Dinner reservation at the bistro")
        notes = []
        orig_create, orig_judge = self.brain.client.messages.create, self.brain.llm_claims_change

        def capture(**kw):
            for m in kw["messages"]:
                if m["role"] == "user" and isinstance(m["content"], list):
                    for b in m["content"]:
                        if b.get("type") == "text" and "automated system check" in b.get("text", ""):
                            notes.append(b["text"])
            if not notes:
                # first draft: the claim with no tool call behind it, which is
                # what makes the guard fire in the first place
                return text_resp("Done — the bistro dinner. Call Dana at 5:45pm is left.")
            return _FakeResp(_FakeToolBlock("complete_item", {"item_id": dinner}),
                             _FakeTextBlock("Done — dinner checked off. Call Dana at 5:45pm is left."))

        self.brain.client.messages.create = capture
        self.brain.llm_claims_change = lambda text: False
        try:
            self.brain.respond(self.ASK)
        finally:
            self.brain.client.messages.create = orig_create
            self.brain.llm_claims_change = orig_judge
        self.assertTrue(notes, "the guard should have fired on a claim with no tool call")
        self.assertIn("asked a QUESTION", notes[0])
        self.assertIn("BOTH", notes[0])


class TestStripDirtySentences(unittest.TestCase):
    def setUp(self):
        import brain
        self.brain = brain

    def test_keeps_content_around_an_apology(self):
        out = self.brain._strip_dirty_sentences(
            "You're right — I need to actually complete that item. Call Dana at 5:45pm is what's left.")
        self.assertEqual(out, "Call Dana at 5:45pm is what's left.")

    def test_keeps_bulleted_lists(self):
        out = self.brain._strip_dirty_sentences(
            "My mistake.\n- Call Dana at 5:45pm\n- Get groceries")
        self.assertIn("Call Dana at 5:45pm", out)
        self.assertIn("Get groceries", out)
        self.assertNotIn("My mistake", out)

    def test_returns_empty_when_there_is_nothing_but_apology(self):
        self.assertEqual(
            self.brain._strip_dirty_sentences("You're right — I need to actually complete that item."), "")

    def test_leaves_clean_text_untouched(self):
        clean = "Call Dana at 5:45pm is the only thing left today."
        self.assertEqual(self.brain._strip_dirty_sentences(clean), clean)


class TestAnswerCompletenessNet(unittest.TestCase):
    """Verified live against the real API on a copy of jarvis's DB: asked "Got
    groceries, you can complete that. What else is on tap for me today?" the
    model completed groceries correctly and then named 3 of the 7 remaining
    due-today items — the three whose times were still ahead — silently
    dropping every OVERDUE one. Those are exactly the items he most needs.

    The state block already hands over the code-computed bucket and the prompt
    already says to read out all of it. Two other instructions the model quietly
    ignored today (the weekday table, "never say you're right") make the lesson
    clear: verify in code, don't ask more firmly."""

    def setUp(self):
        fresh_db()
        import brain
        self.brain = brain
        self.now = datetime.now(config.TZ)

    def test_whats_left_phrasings_from_his_real_messages(self):
        for text in ["What else is on tap for me today?", "Whats left today?", "What's left?",
                     "What else do I have to do today?", "anything else today?",
                     "What's on my list?"]:
            with self.subTest(text=text):
                self.assertTrue(self.brain.WHATS_LEFT_RE.search(text))

    def test_unrelated_questions_do_not_trigger_the_net(self):
        for text in ["When is my dentist appointment?", "Remind me to call grandma tomorrow",
                     "What do you think about Costco for the bulk order?"]:
            with self.subTest(text=text):
                self.assertIsNone(self.brain.WHATS_LEFT_RE.search(text))

    def test_omitted_overdue_items_are_detected(self):
        memory.add_item("Request Aug 19 and Aug 22 off in the PTO portal",
                        due_at=iso(self.now - timedelta(days=1)))
        memory.add_item("Use Fable to get AIssistant SAFELY public on GitHub and LinkedIn",
                        due_at=iso(self.now - timedelta(days=1)))
        memory.add_item("Pushups", due_at=iso(self.now.replace(hour=23, minute=59)))
        import scheduler
        due, _ = scheduler.digest_buckets(memory.open_items(), self.now)
        reply = "Pushups by 11:59pm is all that's left today."
        omitted = self.brain._missing_from_answer(reply, due)
        titles = [i["title"] for i in omitted]
        self.assertEqual(len(omitted), 2)
        self.assertTrue(any("PTO portal" in t for t in titles))
        self.assertTrue(any("AIssistant" in t for t in titles))

    def test_a_paraphrased_mention_counts_as_mentioned(self):
        """The model shortens titles; the net must not claim a false omission."""
        self.assertTrue(self.brain._title_is_mentioned(
            "Rework resume for Dana", "Rework resume at 7:26pm and pushups are left."))
        self.assertTrue(self.brain._title_is_mentioned(
            "Request Aug 19 and Aug 22 off in the PTO portal",
            "the PTO portal request for Wednesday the 19th and Saturday the 22nd"))

    def test_an_unmentioned_item_is_not_falsely_matched(self):
        self.assertFalse(self.brain._title_is_mentioned(
            "Talk to Riley's dad about the referral", "Pushups by 11:59pm is all that's left today."))

    def test_a_complete_answer_gets_nothing_appended(self):
        memory.add_item("Pushups", due_at=iso(self.now.replace(hour=23, minute=59)))
        memory.add_item("Call Dana", due_at=iso(self.now.replace(hour=17, minute=45)))
        import scheduler
        due, _ = scheduler.digest_buckets(memory.open_items(), self.now)
        self.assertEqual(
            self.brain._missing_from_answer("Call Dana at 5:45pm and Pushups tonight.", due), [])

    def test_the_net_appends_omissions_to_the_real_reply(self):
        memory.set_setting("owner_chat_id", "12345")
        memory.add_item("Talk to Riley's dad about the referral", due_at=iso(self.now.replace(hour=9)))
        memory.add_item("Pushups", due_at=iso(self.now.replace(hour=23, minute=59)))
        script = _Script(text_resp("Pushups by 11:59pm is all that's left today."))
        orig_create, orig_judge = self.brain.client.messages.create, self.brain.llm_claims_change
        self.brain.client.messages.create = script
        self.brain.llm_claims_change = lambda text: False
        try:
            reply = self.brain.respond("What else is on tap for me today?")
        finally:
            self.brain.client.messages.create = orig_create
            self.brain.llm_claims_change = orig_judge
        self.assertIn("Also still on today", reply)
        self.assertIn("Riley", reply)

    def test_an_item_completed_this_turn_is_not_resurrected_by_the_net(self):
        """The net runs after the tools settle, so "I did X, what's left?" must
        never list X back at him."""
        memory.set_setting("owner_chat_id", "12345")
        groceries = memory.add_item("Get groceries", due_at=iso(self.now - timedelta(days=2)))
        memory.add_item("Pushups", due_at=iso(self.now.replace(hour=23, minute=59)))
        script = _Script(
            _FakeResp(_FakeToolBlock("complete_item", {"item_id": groceries}),
                      _FakeTextBlock("Done — Get groceries. Pushups tonight is what's left.")),
            text_resp("Done — Get groceries. Pushups tonight is what's left."),
        )
        orig_create, orig_judge = self.brain.client.messages.create, self.brain.llm_claims_change
        self.brain.client.messages.create = script
        self.brain.llm_claims_change = lambda text: False
        try:
            reply = self.brain.respond("Got groceries, you can complete that. What else is on tap today?")
        finally:
            self.brain.client.messages.create = orig_create
            self.brain.llm_claims_change = orig_judge
        self.assertNotIn("Also still on today", reply)
        self.assertEqual(memory.get_item(groceries)["status"], "done")

    def test_a_shared_name_alone_is_not_a_match(self):
        """Caught in the live replay: with a 60% word threshold "Call Dana"
        scored as mentioned purely because "Dana" appeared in a DIFFERENT
        item's title, so a genuinely omitted item was reported as covered.
        Two shared words is a match; one shared name is a coincidence."""
        reply = "Rework resume for Dana at 7:26pm and pushups tonight are what's left."
        self.assertFalse(self.brain._title_is_mentioned("Call Dana", reply))
        self.assertTrue(self.brain._title_is_mentioned("Rework resume for Dana", reply))

    def test_short_titles_need_every_word(self):
        self.assertFalse(self.brain._title_is_mentioned("Get groceries", "Get the car washed."))
        self.assertTrue(self.brain._title_is_mentioned("Get groceries", "Groceries before you get home."))


class TestTelegramIsButtonless(unittest.TestCase):
    """Decision 2026-07-25 (Brooks): "save buttons for the ios UI in v2.
    buttonless in telegram, though it was cool and impressive it's just a bit
    clunky under the circumstances."

    Telegram gives an inline-keyboard row no label and no visual tie to any line
    of message text, so a bundled reminder was three identical
    [Done][+1h][Tomorrow] rows under three numbered lines, readable only by
    counting rows and trusting the order. 28% of reminders were bundles.

    This test exists to stop a future session from helpfully adding them back to
    one send site at a time. Every OUTBOUND send must be buttonless; the
    callback handlers stay live so buttons on messages already in the chat
    history keep working."""

    def setUp(self):
        fresh_db()
        import scheduler
        self.scheduler = scheduler
        memory.set_setting("owner_chat_id", "12345")
        # Disable quiet hours for these tests. Without this the suite passes or
        # fails depending on the clock: run it after 22:00 and check_reminders
        # correctly suppresses a P3 ping, so "nothing was sent" looks like a
        # bug in the thing under test. This suite has been bitten by
        # time-of-day coupling before — pin it.
        memory.set_setting("pref_quiet_start_hour", "0")
        memory.set_setting("pref_quiet_end_hour", "0")

    def _all_sends(self, ctx):
        return ctx.bot.sent

    def test_single_reminder_is_buttonless(self):
        now = datetime.now(config.TZ)
        memory.add_item("Take the gym bag out of the car", due_at=iso(now - timedelta(minutes=1)),
                        remind_at=iso(now - timedelta(minutes=1)))
        ctx = FakeContext()
        run(self.scheduler.check_reminders(ctx))
        self.assertTrue(ctx.bot.sent)
        for sent in self._all_sends(ctx):
            self.assertIsNone(sent.get("reply_markup"))

    def test_list_render_is_buttonless_and_says_how_to_act(self):
        memory.add_item("Order more coffee pods")
        text = self.scheduler.render_list(memory.open_items())
        self.assertNotIn("tap", text.lower(), "must not promise a gesture that no longer exists")
        self.assertIn("Order more coffee pods", text)

    def test_morning_digest_and_stale_sweep_are_buttonless(self):
        now = datetime.now(config.TZ)
        memory.add_item("fell out of the nag chain", due_at=iso(now - timedelta(hours=30)))
        memory.set_setting("pref_digest_style", "plain")
        ctx = FakeContext()
        run(self.scheduler.morning_digest(ctx))
        self.assertTrue(ctx.bot.sent)
        for sent in self._all_sends(ctx):
            self.assertIsNone(sent.get("reply_markup"))

    def test_no_send_site_in_the_codebase_attaches_markup(self):
        """Static check across every module that sends to Telegram. The only
        legitimate remaining uses are in bot.py's callback handler, which EDITS
        messages that already carry buttons from before this change."""
        import pathlib
        root = pathlib.Path(__file__).resolve().parent.parent
        offenders = []
        for name in ("scheduler.py", "gmail_watch.py", "maintenance.py", "webhook.py"):
            for i, line in enumerate((root / name).read_text().splitlines(), 1):
                if "reply_markup" in line and not line.strip().startswith("#"):
                    offenders.append(f"{name}:{i}: {line.strip()}")
        self.assertEqual(offenders, [], "outbound sends must be buttonless")

    def test_callback_handlers_are_still_wired_for_historical_buttons(self):
        """Messages already in the chat have live buttons. Tapping one must
        still work rather than silently failing."""
        import bot
        self.assertTrue(hasattr(bot, "on_button"))
        src = (pathlib.Path(bot.__file__).read_text())
        for action in ('"done"', '"snooze"', '"tmrw"', '"mute"'):
            with self.subTest(action=action):
                self.assertIn(action, src)
