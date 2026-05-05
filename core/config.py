import os

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")

BASE_URL = "https://slackmeet-production.up.railway.app"
SCOPES = ["https://www.googleapis.com/auth/calendar"]