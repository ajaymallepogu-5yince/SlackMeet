import os

# Google OAuth
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")

# Slack App credentials (from api.slack.com/apps)
SLACK_CLIENT_ID = os.getenv("SLACK_CLIENT_ID")
SLACK_CLIENT_SECRET = os.getenv("SLACK_CLIENT_SECRET")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")  # fallback for single-workspace dev

BASE_URL = os.getenv("BASE_URL", "https://slackmeet-production.up.railway.app")

GOOGLE_SCOPES = ["https://www.googleapis.com/auth/calendar"]

SLACK_SCOPES = [
    "commands",
    "chat:write",
    "im:write",
    "users:read",
]