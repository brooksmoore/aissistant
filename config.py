"""Central configuration. Everything adjustable lives in .env — see .env.example."""
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

# Which install this process is: "penny", "jarvis", ... Each instance has its own
# .env, database, Google tokens, backups, and log under instances/<name>/ —
# fully independent brains sharing one codebase.
INSTANCE = os.environ.get("AISSISTANT_INSTANCE", "penny")
INSTANCE_DIR = BASE_DIR / "instances" / INSTANCE
INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
load_dotenv(INSTANCE_DIR / ".env")

# --- required ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# --- identity / behavior ---
ASSISTANT_NAME = os.getenv("ASSISTANT_NAME", "Penny")
PAIRING_CODE = os.getenv("PAIRING_CODE", "letmein")
def _system_tz() -> str:
    """Use this Mac's own timezone unless TIMEZONE is set explicitly in .env."""
    try:
        link = os.readlink("/etc/localtime")
        return link.split("zoneinfo/")[-1]
    except OSError:
        return "America/Chicago"


TIMEZONE = os.getenv("TIMEZONE") or _system_tz()
TZ = ZoneInfo(TIMEZONE)

# --- models & cost control ---
BRAIN_MODEL = os.getenv("BRAIN_MODEL", "claude-sonnet-5")          # deep-thinking turns
CLASSIFIER_MODEL = os.getenv("CLASSIFIER_MODEL", "claude-haiku-4-5-20251001")  # everyday turns + email triage
SMART_ROUTING = os.getenv("SMART_ROUTING", "1") == "1"  # Haiku for routine turns, Sonnet only when thinking is needed
DAILY_BUDGET_USD = float(os.getenv("DAILY_BUDGET_USD", "0.20"))  # soft breaker: past this, everything runs on Haiku
# Hard stop: past this, NOTHING calls the API until midnight (reminders/buttons/
# digest-fallbacks keep working free). Its own knob, not a multiple of the soft
# breaker — raising one must not silently move the other.
HARD_CAP_USD = float(os.getenv("HARD_CAP_USD", "0.25"))

# --- schedule (24h HH:MM, local time) ---
MORNING_DIGEST = os.getenv("MORNING_DIGEST", "08:00")
EVENING_DIGEST = os.getenv("EVENING_DIGEST", "20:30")
QUIET_START_HOUR = int(os.getenv("QUIET_START_HOUR", "22"))  # no pings after this...
QUIET_END_HOUR = int(os.getenv("QUIET_END_HOUR", "8"))       # ...until this (priority-5 excepted)
EMAIL_POLL_MINUTES = int(os.getenv("EMAIL_POLL_MINUTES", "15"))

# --- Siri Shortcut / LAN capture webhook ---
# OFF by default: the listener only starts if WEBHOOK_SECRET is set, so an
# existing install never gains a new network surface without opting in.
# Two instances on the same Mac need distinct ports — set WEBHOOK_PORT
# explicitly per instance if you enable this on more than one.
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "8765"))

# --- files ---
DB_PATH = INSTANCE_DIR / "assistant.db"
GOOGLE_CREDS = INSTANCE_DIR / "google_credentials.json"
GOOGLE_TOKEN = INSTANCE_DIR / "google_token.json"
LOG_PATH = INSTANCE_DIR / "assistant.log"
