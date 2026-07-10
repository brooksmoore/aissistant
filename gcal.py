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


def _svc():
    global _service
    if _service is None:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build

        creds = Credentials.from_authorized_user_file(str(GOOGLE_TOKEN), SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            GOOGLE_TOKEN.write_text(creds.to_json())
        _service = build("calendar", "v3", credentials=creds, cache_discovery=False)
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


def create_event(title, start_iso, end_iso=None, notes="") -> str:
    start = datetime.fromisoformat(start_iso)
    if start.tzinfo is None:
        start = start.replace(tzinfo=TZ)
    end = datetime.fromisoformat(end_iso) if end_iso else start + timedelta(hours=1)
    if end.tzinfo is None:
        end = end.replace(tzinfo=TZ)
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
        start = datetime.fromisoformat(start_iso)
        if start.tzinfo is None:
            start = start.replace(tzinfo=TZ)
        e["start"] = {"dateTime": start.isoformat(), "timeZone": TIMEZONE}
        if not end_iso:
            end_iso = (start + timedelta(hours=1)).isoformat()
    if end_iso:
        end = datetime.fromisoformat(end_iso)
        if end.tzinfo is None:
            end = end.replace(tzinfo=TZ)
        e["end"] = {"dateTime": end.isoformat(), "timeZone": TIMEZONE}
    svc.events().update(calendarId="primary", eventId=event_id, body=e).execute()
    _cal_cache["ts"] = 0.0


def delete_event(event_id):
    _svc().events().delete(calendarId="primary", eventId=event_id).execute()
    _cal_cache["ts"] = 0.0
