from googleapiclient.discovery import build
from datetime import datetime, timedelta
from storage.tokens import user_tokens


def create_meeting(user_id: str):
    credentials = user_tokens.get(user_id)

    if not credentials:
        return None

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