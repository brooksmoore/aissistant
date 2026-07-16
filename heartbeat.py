"""Independent liveness watchdog for both aissistant instances. Runs as its
OWN launchd job (com.aissistant.heartbeat), completely separate from the bot
processes it watches — the whole point is that this keeps working even if a
bot's own internal loop wedges. Confirmed incident (2026-07-14): both jarvis
and penny sat silently stuck in a Telegram long-poll network-error loop for
~6 hours — process alive (launchd showed a PID, KeepAlive never triggered),
scheduler jobs still firing, but Telegram unreachable — before Brooks
happened to message Jarvis and notice. Jordan never noticed Penny was down
at all. Alerts via a macOS local notification, not Telegram: alerting
through the exact channel that might be the thing that's broken would defeat
the purpose. Zero model calls, zero API cost, does not import
bot.py/brain.py/scheduler.py — nothing in this file can be affected by
whatever is wrong with the thing it's checking on."""
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent
INSTANCES = ("jarvis", "penny")
LOG_STALE_MINUTES = 15       # zero log activity in this window -> process looks hung
ERROR_WINDOW_MINUTES = 10    # look at this recent a slice of the log for network errors
ERROR_THRESHOLD = 5          # this many NetworkError/TimedOut lines in the window -> stuck poller
ALERT_COOLDOWN_MINUTES = 60  # don't re-alert on the same ongoing incident more than hourly
TAIL_BYTES = 2_000_000       # logs get large; the last ~2MB comfortably covers a 15-min window

STATE_FILE = BASE / "heartbeat_state.txt"  # small, local, gitignored alongside instances/


def _process_running(instance: str) -> bool:
    out = subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        parts = line.split()
        if parts and parts[-1] == f"com.aissistant.{instance}":
            return parts[0].isdigit() and int(parts[0]) > 0
    return False


def _log_lines_since(log_path: Path, minutes: int) -> list:
    if not log_path.exists():
        return []
    cutoff = datetime.now() - timedelta(minutes=minutes)
    with log_path.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - TAIL_BYTES))
        text = f.read().decode(errors="ignore")
    out = []
    for line in text.splitlines():
        m = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
        if not m:
            continue
        try:
            ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if ts >= cutoff:
            out.append(line)
    return out


def check_instance(instance: str) -> str | None:
    """Returns an alert message, or None if healthy."""
    if not _process_running(instance):
        return f"{instance}: process is not running (launchd shows no PID)."
    log_path = BASE / "instances" / instance / "assistant.log"
    recent = _log_lines_since(log_path, LOG_STALE_MINUTES)
    if not recent:
        return f"{instance}: no log activity in {LOG_STALE_MINUTES} min — may be hung."
    errors = [l for l in _log_lines_since(log_path, ERROR_WINDOW_MINUTES)
              if "NetworkError" in l or "TimedOut" in l]
    if len(errors) >= ERROR_THRESHOLD:
        return (f"{instance}: {len(errors)} Telegram network errors in the last "
                f"{ERROR_WINDOW_MINUTES} min — likely stuck (2026-07-14-class outage).")
    return None


def _build_script(title: str, message: str) -> str:
    # AppleScript string literals need double quotes, not Python's !r (single-
    # quoted) repr — using !r here produced a silent AppleScript syntax error
    # that never surfaced anywhere (subprocess.run doesn't raise on nonzero
    # exit unless check=True), caught only by manually firing a test alert.
    # Split out from _notify so the escaping logic is unit-testable without
    # actually popping a real notification on every test run.
    def _esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')
    return f'display notification "{_esc(message)}" with title "{_esc(title)}" sound name "Basso"'


def _notify(title: str, message: str):
    # osascript ships with macOS and doesn't depend on network/Telegram at all
    subprocess.run(["osascript", "-e", _build_script(title, message)], check=True)


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    out = {}
    for line in STATE_FILE.read_text().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


def _save_state(state: dict):
    STATE_FILE.write_text("\n".join(f"{k}={v}" for k, v in state.items()))


def main():
    state = _load_state()
    now = datetime.now()
    for instance in INSTANCES:
        alert = check_instance(instance)
        key = f"last_alert_{instance}"
        if alert:
            last = state.get(key)
            if last and now - datetime.fromisoformat(last) < timedelta(minutes=ALERT_COOLDOWN_MINUTES):
                continue  # already alerted for this ongoing incident within the cooldown
            try:
                _notify("aissistant heartbeat", alert)
            except Exception:
                # a failed notification for one instance must not stop the
                # other instance's check or the state save at the end
                print(f"heartbeat: notify failed for {instance}: {alert}")
            state[key] = now.isoformat()
        else:
            state.pop(key, None)  # healthy again -> the next real incident alerts fresh
    _save_state(state)


if __name__ == "__main__":
    main()
