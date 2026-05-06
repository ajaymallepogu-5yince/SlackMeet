from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from core.database import SessionLocal
from models.user_token import UserToken
from services.google import create_meeting

router = APIRouter()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.post("/meet")
async def meet(request: Request):
    try:
        form = await request.form()

        user_id = form.get("user_id")
        text = (form.get("text") or "").lower()

        instant_keywords = ["connect", "meet", "call", "phone"]

        db: Session = next(get_db())

        user = db.query(UserToken).filter(UserToken.user_id == user_id).first()

        # =========================
        # 🔥 INSTANT MEETING
        # =========================
        if any(word in text for word in instant_keywords):

            if not user:
                return JSONResponse({
                    "response_type": "ephemeral",
                    "text": "⚠️ Please connect Google first"
                })

            meet_link = create_meeting(user)

            if not meet_link:
                return JSONResponse({
                    "response_type": "ephemeral",
                    "text": "❌ Failed to create meeting"
                })

            return JSONResponse({
                "response_type": "in_channel",
                "text": f"📞 Meeting ready: {meet_link}"
            })

        # =========================
        # 📌 SHOW BUTTONS
        # =========================
        if not user:
            return JSONResponse({
                "response_type": "ephemeral",
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "⚠️ Connect Google first"
                        }
                    },
                    {
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": "Connect Google"},
                                "url": "https://slackmeet-production.up.railway.app/auth"
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
                    "text": {
                        "type": "mrkdwn",
                        "text": "What do you want to do?"
                    }
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "⚡ Connect Now"},
                            "url": "https://slackmeet-production.up.railway.app/instant-meet"
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "📅 Schedule Later"},
                            "value": "schedule"
                        }
                    ]
                }
            ]
        })

    except Exception as e:
        print("🔥 ERROR IN /meet:", str(e))

        return JSONResponse({
            "response_type": "ephemeral",
            "text": "❌ Something went wrong. Check server logs."
        })