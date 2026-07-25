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


def auth_broken() -> bool:
    """True when the stored token exists but Google refuses to refresh it.

    Set by the last failed call, cleared by the next successful one. Exists
    because "[]" from upcoming_events used to mean BOTH "no events" and "I
    cannot read your calendar at all" — see _upcoming_text_fresh."""
    return _auth_state["broken"]


_auth_state = {"broken": False}


def upcoming_events(days=7) -> list:
    """Returns [{id, title, start, end}] for the next N days, [] on any failure.
    Callers that need to tell an empty calendar apart from an unreadable one
    must check auth_broken() — see _upcoming_text_fresh."""
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
        _auth_state["broken"] = False
        return events
    except Exception as e:
        log.exception("calendar fetch failed")
        # RefreshError means the stored grant is dead (revoked, or expired
        # because the OAuth app is still in "Testing" publishing status, where
        # Google expires refresh tokens after 7 days). That is a completely
        # different situation from a transient network blip and must not be
        # reported to the owner as "no events".
        if "invalid_grant" in str(e) or type(e).__name__ == "RefreshError":
            _auth_state["broken"] = True
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
    if not events and auth_broken():
        # This string goes into the state block on EVERY turn. Saying "no
        # events" here is an affirmative lie the model then repeats with
        # confidence: penny's Google grant died on 2026-07-16 and for the nine
        # days after it, her calendar looked simply empty — to the model and to
        # anyone who asked her what was coming up.
        return ("(CALENDAR UNREADABLE — the Google connection has expired and needs re-authorizing. "
                "You do NOT know what is on the calendar right now: never say it is empty or clear, "
                "never answer a calendar question from this. Say the connection needs reconnecting.)")
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
