"""Liveness watchdog — zero API cost, no model involvement.
Run: ./venv/bin/python -m unittest discover tests -v"""
import pathlib
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import heartbeat  # noqa: E402

TEST_STATE = pathlib.Path("/tmp/heartbeat_test_state.txt")
TEST_LOG = pathlib.Path("/tmp/heartbeat_test.log")


def _write_log(lines_with_offsets_minutes):
    """lines_with_offsets_minutes: list of (minutes_ago, text) -> writes a
    fake log file with real-looking timestamps."""
    now = datetime.now()
    lines = []
    for mins_ago, text in lines_with_offsets_minutes:
        ts = (now - timedelta(minutes=mins_ago)).strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"{ts},000 {text}")
    TEST_LOG.write_text("\n".join(lines) + "\n")


class TestLogParsing(unittest.TestCase):
    def tearDown(self):
        TEST_LOG.unlink(missing_ok=True)

    def test_missing_log_file_returns_empty(self):
        self.assertEqual(heartbeat._log_lines_since(pathlib.Path("/tmp/does_not_exist.log"), 15), [])

    def test_only_lines_within_window_are_returned(self):
        _write_log([(30, "old line"), (5, "recent line"), (1, "very recent line")])
        lines = heartbeat._log_lines_since(TEST_LOG, 15)
        self.assertEqual(len(lines), 2)
        self.assertTrue(any("recent line" in l for l in lines))
        self.assertFalse(any("old line" in l for l in lines))

    def test_malformed_lines_are_skipped_not_raised(self):
        TEST_LOG.write_text("not a timestamped line at all\nalso garbage\n")
        self.assertEqual(heartbeat._log_lines_since(TEST_LOG, 15), [])


class TestCheckInstance(unittest.TestCase):
    def tearDown(self):
        TEST_LOG.unlink(missing_ok=True)

    @patch("heartbeat._process_running", return_value=False)
    def test_process_not_running_is_flagged(self, _mock):
        result = heartbeat.check_instance("jarvis")
        self.assertIsNotNone(result)
        self.assertIn("not running", result)

    @patch("heartbeat._process_running", return_value=True)
    def test_stale_log_is_flagged(self, _mock):
        with patch.object(heartbeat, "BASE", pathlib.Path("/tmp/heartbeat_nonexistent_base")):
            result = heartbeat.check_instance("jarvis")
        self.assertIsNotNone(result)
        self.assertIn("no log activity", result)

    @patch("heartbeat._process_running", return_value=True)
    def test_sustained_network_errors_are_flagged(self, _mock):
        # real incident shape (2026-07-14): a burst of NetworkError/TimedOut
        # lines while the process itself stays up
        lines = [(1, "penny.bot ERROR telegram.error.NetworkError: httpx.ReadError")] * 8
        with patch("heartbeat._log_lines_since") as mock_lines:
            mock_lines.side_effect = lambda path, minutes: (
                ["line"] if minutes == heartbeat.LOG_STALE_MINUTES
                else [t for _, t in lines]
            )
            result = heartbeat.check_instance("jarvis")
        self.assertIsNotNone(result)
        self.assertIn("network errors", result)

    @patch("heartbeat._process_running", return_value=True)
    def test_healthy_instance_returns_none(self, _mock):
        with patch("heartbeat._log_lines_since") as mock_lines:
            mock_lines.side_effect = lambda path, minutes: ["a normal log line"]
            result = heartbeat.check_instance("jarvis")
        self.assertIsNone(result)

    @patch("heartbeat._process_running", return_value=True)
    def test_a_few_scattered_errors_below_threshold_do_not_alert(self, _mock):
        with patch("heartbeat._log_lines_since") as mock_lines:
            mock_lines.side_effect = lambda path, minutes: (
                ["a normal line"] if minutes == heartbeat.LOG_STALE_MINUTES
                else ["one NetworkError here", "a normal line", "another normal line"]
            )
            result = heartbeat.check_instance("jarvis")
        self.assertIsNone(result)


class TestNotifyScript(unittest.TestCase):
    """Real bug caught during manual verification (not by any unit test):
    using Python's !r repr for the AppleScript string literal produces
    single-quoted output, but AppleScript requires double quotes — osascript
    failed with a syntax error that subprocess.run() (no check=True)
    swallowed completely silently. Split into a pure _build_script so the
    escaping is testable without popping a real notification every test run;
    the full pipe (_notify -> osascript) was manually fired once and confirmed
    live during this session."""

    def test_uses_double_quotes_not_python_repr(self):
        script = heartbeat._build_script("a title", "a message")
        self.assertIn('"a message"', script)
        self.assertIn('"a title"', script)
        self.assertNotIn("'a message'", script)

    def test_embedded_double_quotes_are_escaped(self):
        script = heartbeat._build_script("title", 'message with "quotes" inside')
        self.assertIn('\\"quotes\\"', script)

    def test_embedded_backslash_is_escaped(self):
        one_backslash = "a" + chr(92) + "b"  # "a\b" — one literal backslash
        script = heartbeat._build_script("title", one_backslash)
        self.assertIn(chr(92) * 2, script)  # must appear doubled in the output


class TestAlertCooldown(unittest.TestCase):
    def setUp(self):
        heartbeat.STATE_FILE = TEST_STATE
        TEST_STATE.unlink(missing_ok=True)

    def tearDown(self):
        TEST_STATE.unlink(missing_ok=True)

    @patch("heartbeat._notify")
    @patch("heartbeat.check_instance")
    def test_first_alert_fires_and_is_recorded(self, mock_check, mock_notify):
        mock_check.side_effect = lambda i: "jarvis: down" if i == "jarvis" else None
        heartbeat.main()
        mock_notify.assert_called_once()
        state = heartbeat._load_state()
        self.assertIn("last_alert_jarvis", state)

    @patch("heartbeat._notify")
    @patch("heartbeat.check_instance")
    def test_repeat_alert_within_cooldown_is_suppressed(self, mock_check, mock_notify):
        mock_check.side_effect = lambda i: "jarvis: down" if i == "jarvis" else None
        heartbeat.main()  # first alert
        mock_notify.reset_mock()
        heartbeat.main()  # same ongoing incident, should NOT re-alert
        mock_notify.assert_not_called()

    @patch("heartbeat._notify")
    @patch("heartbeat.check_instance")
    def test_recovery_clears_state_and_next_incident_alerts_fresh(self, mock_check, mock_notify):
        mock_check.side_effect = lambda i: "jarvis: down" if i == "jarvis" else None
        heartbeat.main()  # incident 1
        mock_check.side_effect = lambda i: None  # recovered
        heartbeat.main()
        state = heartbeat._load_state()
        self.assertNotIn("last_alert_jarvis", state)
        mock_notify.reset_mock()
        mock_check.side_effect = lambda i: "jarvis: down again" if i == "jarvis" else None
        heartbeat.main()  # incident 2 -> must alert again, not suppressed
        mock_notify.assert_called_once()


if __name__ == "__main__":
    unittest.main()
