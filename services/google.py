from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from datetime import datetime, timedelta

from core.database import SessionLocal
from models.user_token import UserToken


def create_meeting(user_id: str):
    db = SessionLocal()

    user = db.query(UserToken).filter(UserToken.user_id == user_id).first()
    db.close()

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
                "requestId": "random-string",
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