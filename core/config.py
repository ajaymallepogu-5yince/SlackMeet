import os

GOOGLE_CLIENT_ID     = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")

SLACK_CLIENT_ID      = os.getenv("SLACK_CLIENT_ID")
SLACK_CLIENT_SECRET  = os.getenv("SLACK_CLIENT_SECRET")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")
SLACK_BOT_TOKEN      = os.getenv("SLACK_BOT_TOKEN")   # fallback for dev

BASE_URL = os.getenv("BASE_URL", "https://slackmeet-production.up.railway.app")

GOOGLE_SCOPES = ["https://www.googleapis.com/auth/calendar"]

SLACK_SCOPES = [
    "commands",           # slash commands
    "chat:write",         # post messages to channels
    "im:write",           # open DMs
    "users:read",         # resolve user names
    "users:read.email",   # get user emails for Google Calendar invites
]