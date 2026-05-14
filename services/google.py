from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from datetime import datetime, timedelta
import uuid

from models.user_token import UserToken


def _get_service(user: UserToken):
    credentials = Credentials(
        token=user.access_token,
        refresh_token=user.refresh_token,
        token_uri=user.token_uri,
        client_id=user.client_id,
        client_secret=user.client_secret,
        scopes=user.scopes.split(","),
    )
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
    return build("calendar", "v3", credentials=credentials)


def create_meeting(user: UserToken) -> tuple[str, str] | tuple[None, None]:
    """Create an instant meeting. Returns (meet_link, calendar_event_id)."""
    if not user:
        return None, None

    service = _get_service(user)
    now = datetime.utcnow()

    event = {
        "summary": "Instant Slack Meeting",
        "start": {"dateTime": now.isoformat() + "Z"},
        "end": {"dateTime": (now + timedelta(hours=1)).isoformat() + "Z"},
        "conferenceData": {
            "createRequest": {
                "requestId": str(uuid.uuid4()),
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        },
    }

    result = service.events().insert(
        calendarId="primary",
        body=event,
        conferenceDataVersion=1,
    ).execute()

    return result.get("hangoutLink"), result.get("id")


def create_scheduled_meeting(
    user: UserToken,
    title: str,
    date: str,
    time: str,
    duration: int,
    notes: str = "",
) -> tuple[str, str] | tuple[None, None]:
    """Create a scheduled meeting. Returns (meet_link, calendar_event_id)."""
    if not user:
        return None, None

    service = _get_service(user)
    start_dt = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
    end_dt = start_dt + timedelta(minutes=duration)

    event = {
        "summary": title,
        "description": notes,
        "start": {"dateTime": start_dt.isoformat() + "Z"},
        "end": {"dateTime": end_dt.isoformat() + "Z"},
        "conferenceData": {
            "createRequest": {
                "requestId": str(uuid.uuid4()),
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        },
    }

    result = service.events().insert(
        calendarId="primary",
        body=event,
        conferenceDataVersion=1,
    ).execute()

    return result.get("hangoutLink"), result.get("id")


def cancel_calendar_event(user: UserToken, calendar_event_id: str) -> bool:
    """Delete a Google Calendar event. Returns True on success."""
    try:
        service = _get_service(user)
        service.events().delete(calendarId="primary", eventId=calendar_event_id).execute()
        return True
    except Exception as e:
        print("🔥 Cancel calendar error:", str(e))
        return False