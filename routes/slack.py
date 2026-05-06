from fastapi import APIRouter, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
import requests
from models.user_token import UserToken

from core.database import SessionLocal
from services.google import create_meeting

router = APIRouter()


# 🔥 KEYWORDS → instant meeting (no buttons)
INSTANT_KEYWORDS = ["connect", "meet", "call", "phone"]


# =========================
# MAIN SLASH COMMAND
# =========================
@router.post("/meet")
async def meet(request: Request, background_tasks: BackgroundTasks):
    form = await request.form()

    user_id = form.get("user_id")
    text = (form.get("text") or "").lower()
    response_url = form.get("response_url")

    # =========================
    # ✅ SCENARIO 2 → INSTANT
    # =========================
    if any(word in text for word in INSTANT_KEYWORDS):
        background_tasks.add_task(process_instant_meeting, user_id, response_url)

        return JSONResponse({
            "response_type": "ephemeral",
            "text": "⏳ Creating instant meeting..."
        })

    # =========================
    # ✅ SCENARIO 1 → SHOW BUTTONS
    # =========================
    db: Session = SessionLocal()

    token = db.query(UserToken).filter(UserToken.user_id == user_id).first()
    db.close()

    # ❌ NOT CONNECTED
    if not token:
        return JSONResponse({
            "response_type": "ephemeral",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "⚠️ Connect your Google account first"
                    }
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "🔗 Connect Google"},
                            "url": "https://slackmeet-production.up.railway.app/auth"
                        }
                    ]
                }
            ]
        })

    # ✅ CONNECTED → SHOW OPTIONS
    return JSONResponse({
        "response_type": "ephemeral",
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "What would you like to do?"
                }
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "⚡ Connect Now"},
                        "url": f"https://slackmeet-production.up.railway.app/instant-meet?user_id={user_id}"
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "📅 Schedule Later"},
                        "value": "schedule_later"
                    }
                ]
            }
        ]
    })


# =========================
# 🔥 BACKGROUND TASK
# =========================
def process_instant_meeting(user_id: str, response_url: str):
    db: Session = SessionLocal()

    token = db.query(UserToken).filter(UserToken.user_id == user_id).first()

    # ❌ NOT CONNECTED
    if not token:
        db.close()
        requests.post(response_url, json={
            "response_type": "ephemeral",
            "text": "⚠️ Please connect Google first: https://slackmeet-production.up.railway.app/auth"
        })
        return

    # ✅ CREATE MEETING
    meet_link = create_meeting(token)

    db.close()

    # ❌ FAILED
    if not meet_link:
        requests.post(response_url, json={
            "response_type": "ephemeral",
            "text": "❌ Failed to create meeting"
        })
        return

    # ✅ SUCCESS
    requests.post(response_url, json={
        "response_type": "in_channel",
        "text": f"📞 Instant meeting ready: {meet_link}"
    })


# =========================
# ⚡ BUTTON HANDLER (INSTANT)
# =========================
@router.get("/instant-meet")
def instant_meet(user_id: str):
    db: Session = SessionLocal()

    token = db.query(UserToken).filter(UserToken.user_id == user_id).first()

    if not token:
        db.close()
        return {"error": "Please connect Google first"}

    meet_link = create_meeting(token)

    db.close()

    return {
        "message": f"Meeting created: {meet_link}"
    }