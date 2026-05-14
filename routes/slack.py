from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from core.config import BASE_URL
from core.database import SessionLocal
from models.user_token import UserToken
from services.google import create_meeting

router = APIRouter()


def get_user(db: Session, user_id: str):
    return db.query(UserToken).filter(UserToken.user_id == user_id).first()


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
        try:
            user = get_user(db, user_id)

            # =========================
            # 🔥 INSTANT MEETING
            # =========================
            if any(word in text for word in instant_keywords):
                if not user:
                    return JSONResponse({
                        "response_type": "ephemeral",
                        "text": f"⚠️ Please connect Google first:\n{BASE_URL}/auth?user_id={user_id}"
                    })

                meet_link = create_meeting(user)

                if not meet_link:
                    return JSONResponse({
                        "response_type": "ephemeral",
                        "text": "❌ Failed to create meeting. Make sure Google Calendar is connected."
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
                                    "url": f"{BASE_URL}/auth?user_id={user_id}"
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
                                "action_id": "instant_meet",
                                "value": user_id,
                                "style": "primary"
                            },
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": "📅 Schedule Later"},
                                "action_id": "schedule_meet",
                                "value": user_id
                            }
                        ]
                    }
                ]
            })
        finally:
            db.close()

    except Exception as e:
        print("🔥 ERROR:", str(e))
        return JSONResponse({
            "response_type": "ephemeral",
            "text": "❌ Something went wrong. Check server logs."
        })


@router.post("/actions")
async def actions(request: Request):
    """Handles Slack interactive button clicks (action payloads)."""
    import json
    try:
        form = await request.form()
        payload = json.loads(form.get("payload", "{}"))

        actions_list = payload.get("actions", [])
        if not actions_list:
            return JSONResponse({"text": "❌ No action found."})

        action = actions_list[0]
        action_id = action.get("action_id")
        user_id = action.get("value")

        if action_id == "instant_meet":
            db: Session = SessionLocal()
            try:
                user = get_user(db, user_id)
                if not user:
                    return JSONResponse({
                        "response_type": "ephemeral",
                        "text": f"⚠️ Please connect Google first:\n{BASE_URL}/auth?user_id={user_id}"
                    })

                meet_link = create_meeting(user)

                if not meet_link:
                    return JSONResponse({
                        "response_type": "ephemeral",
                        "text": "❌ Failed to create meeting."
                    })

                return JSONResponse({
                    "response_type": "in_channel",
                    "text": f"📞 Meeting ready: {meet_link}"
                })
            finally:
                db.close()

        elif action_id == "schedule_meet":
            return JSONResponse({
                "response_type": "ephemeral",
                "text": "📅 Scheduling is coming soon!"
            })

        return JSONResponse({"text": "Unknown action."})

    except Exception as e:
        print("🔥 ACTIONS ERROR:", str(e))
        return JSONResponse({
            "response_type": "ephemeral",
            "text": "❌ Something went wrong."
        })