"""Google Calendar integration. Activates automatically once google_token.json exists
(created by running setup_google.py). All functions fail soft — the bot works without it."""
import logging
from datetime import datetime, timedelta

from config import GOOGLE_TOKEN, TZ, TIMEZONE

log = logging.getLogger("penny.gcal")
SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.readonly",
]

_service = None


def enabled() -> bool:
    return GOOGLE_TOKEN.exists()


def service(api: str, version: str):
    """Shared Google auth: load, refresh, and persist the instance token, then
    build a client. Calendar and Gmail both go through here so auth fixes land once."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    creds = Credentials.from_authorized_user_file(str(GOOGLE_TOKEN), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        GOOGLE_TOKEN.write_text(creds.to_json())
    return build(api, version, credentials=creds, cache_discovery=False)


def _svc():
    global _service
    if _service is None:
        _service = service("calendar", "v3")
    return _service


def upcoming_events(days=7) -> list:
    """Returns [{id, title, start, end}] for the next N days, [] on any failure."""
    if not enabled():
        return []
    try:
        now = datetime.now(TZ)
        result = _svc().events().list(
            calendarId="primary",
            timeMin=now.isoformat(),
            timeMax=(now + timedelta(days=days)).isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=40,
        ).execute()
        events = []
        for e in result.get("items", []):
            start = e["start"].get("dateTime", e["start"].get("date", ""))
            end = e["end"].get("dateTime", e["end"].get("date", ""))
            events.append({"id": e["id"], "title": e.get("summary", "(untitled)"), "start": start, "end": end})
        return events
    except Exception:
        log.exception("calendar fetch failed")
        return []


_cal_cache = {"ts": 0.0, "text": ""}


def upcoming_text(days=7) -> str:
    """Cached 5 min: this gets injected into every brain turn — don't hit the API each time."""
    import time
    if time.time() - _cal_cache["ts"] < 300:
        return _cal_cache["text"]
    text = _upcoming_text_fresh(days)
    _cal_cache.update(ts=time.time(), text=text)
    return text


def _upcoming_text_fresh(days=7) -> str:
    events = upcoming_events(days)
    if not events:
        return "(no events in the next %d days)" % days
    lines = []
    for e in events:
        lines.append(f"- [{e['id']}] {e['start']} — {e['title']}")
    return "\n".join(lines)


def _local(iso: str) -> datetime:
    """Parse ISO; naive values are the owner's local time."""
    dt = datetime.fromisoformat(iso)
    return dt if dt.tzinfo else dt.replace(tzinfo=TZ)


def create_event(title, start_iso, end_iso=None, notes="") -> str:
    start = _local(start_iso)
    end = _local(end_iso) if end_iso else start + timedelta(hours=1)
    body = {
        "summary": title,
        "description": notes,
        "start": {"dateTime": start.isoformat(), "timeZone": TIMEZONE},
        "end": {"dateTime": end.isoformat(), "timeZone": TIMEZONE},
    }
    created = _svc().events().insert(calendarId="primary", body=body).execute()
    _cal_cache["ts"] = 0.0
    return created["id"]


def update_event(event_id, title=None, start_iso=None, end_iso=None, notes=None):
    svc = _svc()
    e = svc.events().get(calendarId="primary", eventId=event_id).execute()
    if title:
        e["summary"] = title
    if notes is not None:
        e["description"] = notes
    if start_iso:
        start = _local(start_iso)
        e["start"] = {"dateTime": start.isoformat(), "timeZone": TIMEZONE}
        if not end_iso:
            end_iso = (start + timedelta(hours=1)).isoformat()
    if end_iso:
        end = _local(end_iso)
        e["end"] = {"dateTime": end.isoformat(), "timeZone": TIMEZONE}
    svc.events().update(calendarId="primary", eventId=event_id, body=e).execute()
    _cal_cache["ts"] = 0.0


def delete_event(event_id):
    _svc().events().delete(calendarId="primary", eventId=event_id).execute()
    _cal_cache["ts"] = 0.0
