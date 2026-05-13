from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from datetime import datetime, timedelta

from models.user_token import UserToken


def create_meeting(user: UserToken):
    if not user:
        return None

    credentials = Credentials(
        token=user.access_token,
        refresh_token=user.refresh_token,
        token_uri=user.token_uri,
        client_id=user.client_id,
        client_secret=user.client_secret,
        scopes=user.scopes.split(","),
    )

    # Refresh token if expired
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())

    service = build("calendar", "v3", credentials=credentials)

    event = {
        "summary": "Slack Meeting",
        "start": {
            "dateTime": datetime.utcnow().isoformat() + "Z",
        },
        "end": {
            "dateTime": (datetime.utcnow() + timedelta(hours=1)).isoformat() + "Z",
        },
        "conferenceData": {
            "createRequest": {
                "requestId": f"meet-{user.user_id}-{int(datetime.utcnow().timestamp())}",
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        },
    }

    event = service.events().insert(
        calendarId="primary",
        body=event,
        conferenceDataVersion=1,
    ).execute()

    return event.get("hangoutLink")