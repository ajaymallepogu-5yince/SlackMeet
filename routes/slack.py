from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from core.database import SessionLocal
from models.user_token import UserToken
from services.google import create_meeting

router = APIRouter()


@router.post("/meet")
async def meet(request: Request):
    try:
        form = await request.form()

        user_id = form.get("user_id")
        text = (form.get("text") or "").lower()

        print("DEBUG user_id:", user_id)
        print("DEBUG text:", text)

        if not user_id:
            return JSONResponse({
                "response_type": "ephemeral",
                "text": "❌ user_id missing"
            })

        instant_keywords = ["connect", "meet", "call", "phone"]

        db: Session = SessionLocal()

        user = db.query(UserToken).filter(UserToken.user_id == user_id).first()

        # =========================
        # 🔥 INSTANT MEETING
        # =========================
        if any(word in text for word in instant_keywords):

            if not user:
                return JSONResponse({
                    "response_type": "ephemeral",
                    "text": f"⚠️ Please connect Google first:\nhttps://slackmeet-production.up.railway.app/auth?user_id={user_id}"
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
                            "text": "⚠️ Connect your Google account first"
                        }
                    },
                    {
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": "Connect Google"},
                                "url": f"https://slackmeet-production.up.railway.app/auth?user_id={user_id}"
                            }
                        ]
                    }
                ]
            })

        # ✅ Connected → show options
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
                            "value": "schedule"
                        }
                    ]
                }
            ]
        })

    except Exception as e:
        print("🔥 ERROR:", str(e))

        return JSONResponse({
            "response_type": "ephemeral",
            "text": "❌ Something went wrong. Check server logs."
        })