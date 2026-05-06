from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from core.database import SessionLocal
from models.user import UserToken
from services.google import create_meeting

router = APIRouter()

BASE_URL = "https://slackmeet-production.up.railway.app"


@router.post("/meet")
async def meet(request: Request):
    form = await request.form()

    user_id = form.get("user_id")
    text = (form.get("text") or "").lower()

    db = SessionLocal()
    user = db.query(UserToken).filter(UserToken.user_id == user_id).first()
    db.close()

    instant_keywords = ["connect", "meet", "call", "phone"]

    # 🚀 INSTANT MEETING
    if any(word in text for word in instant_keywords):

        if not user:
            return JSONResponse({
                "response_type": "ephemeral",
                "blocks": [
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": "⚠️ Connect Google first"}
                    },
                    {
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": "Connect Google"},
                                "url": f"{BASE_URL}/auth?user_id={user_id}"
                            }
                        ]
                    }
                ]
            })

        meet_link = create_meeting(user_id)

        return JSONResponse({
            "response_type": "in_channel",
            "text": f"📞 Instant meeting ready: {meet_link}"
        })

    # 🎯 SHOW BUTTONS
    if not user:
        return JSONResponse({
            "response_type": "ephemeral",
            "blocks": [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "⚠️ Connect your Google account first"}
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Connect Google"},
                            "url": f"{BASE_URL}/auth?user_id={user_id}"
                        }
                    ]
                }
            ]
        })

    return JSONResponse({
        "response_type": "ephemeral",
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "What would you like to do?"}
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "⚡ Connect Now"},
                        "url": f"{BASE_URL}/instant-meet?user_id={user_id}"
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