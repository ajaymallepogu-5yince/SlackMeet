import os

# Google OAuth
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")

# Slack App credentials (from api.slack.com/apps → Basic Information)
SLACK_CLIENT_ID = os.getenv("SLACK_CLIENT_ID")
SLACK_CLIENT_SECRET = os.getenv("SLACK_CLIENT_SECRET")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")

# Fallback bot token for single-workspace dev/testing
# Get from: api.slack.com/apps → OAuth & Permissions → Bot User OAuth Token
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")

BASE_URL = os.getenv("BASE_URL", "https://slackmeet-production.up.railway.app")

GOOGLE_SCOPES = ["https://www.googleapis.com/auth/calendar"]

SLACK_SCOPES = [
    "commands",
    "chat:write",
    "im:write",
    "users:read",
]