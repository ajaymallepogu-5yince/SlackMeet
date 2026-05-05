from googleapiclient.discovery import build
from datetime import datetime, timedelta

def create_meet(credentials):
    service = build('calendar', 'v3', credentials=credentials)

    event = {
        'summary': 'Slack Meeting',
        'start': {
            'dateTime': datetime.utcnow().isoformat(),
            'timeZone': 'UTC',
        },
        'end': {
            'dateTime': (datetime.utcnow() + timedelta(minutes=30)).isoformat(),
            'timeZone': 'UTC',
        },
        'conferenceData': {
            'createRequest': {
                'requestId': 'random123',
                'conferenceSolutionKey': {'type': 'hangoutsMeet'}
            }
        }
    }

    event = service.events().insert(
        calendarId='primary',
        body=event,
        conferenceDataVersion=1
    ).execute()

    return event.get("hangoutLink")