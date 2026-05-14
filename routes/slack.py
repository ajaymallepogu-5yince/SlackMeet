import json
import httpx
import asyncio

from fastapi import APIRouter, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from core.config import BASE_URL
from core.database import SessionLocal
from models.user_token import UserToken
from services.google import create_meeting

router = APIRouter()


def get_user(db: Session, user_id: str):
    return db.query(UserToken).filter(UserToken.user_id == user_id).first()


async def send_to_slack(response_url: str, message: dict):
    """Send a delayed response back to Slack via response_url."""
    async with httpx.AsyncClient() as client:
        await client.post(response_url, json=message, timeout=10)


async def handle_instant_meet(user_id: str, response_url: str):
    """Background task: create meeting and post result to Slack."""
    db: Session = SessionLocal()
    try:
        user = get_user(db, user_id)

        if not user:
            await send_to_slack(response_url, {
                "response_type": "ephemeral",
                "replace_original": False,
                "text": f"⚠️ Please connect Google first:\n{BASE_URL}/auth?user_id={user_id}"
            })
            return

        meet_link = create_meeting(user)

        if not meet_link:
            await send_to_slack(response_url, {
                "response_type": "ephemeral",
                "replace_original": False,
                "text": "❌ Failed to create meeting. Make sure Google Calendar access is granted."
            })
            return

        await send_to_slack(response_url, {
            "response_type": "in_channel",
            "replace_original": False,
            "text": f"📞 Meeting ready: {meet_link}"
        })

    except Exception as e:
        print("🔥 BACKGROUND MEET ERROR:", str(e))
        await send_to_slack(response_url, {
            "response_type": "ephemeral",
            "replace_original": False,
            "text": "❌ Something went wrong while creating the meeting."
        })
    finally:
        db.close()


@router.post("/meet")
async def meet(request: Request, background_tasks: BackgroundTasks):
    try:
        form = await request.form()
        user_id = form.get("user_id")
        text = (form.get("text") or "").lower()
        response_url = form.get("response_url")

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

            if any(word in text for word in instant_keywords):
                if not user:
                    return JSONResponse({
                        "response_type": "ephemeral",
                        "text": f"⚠️ Please connect Google first:\n{BASE_URL}/auth?user_id={user_id}"
                    })

                background_tasks.add_task(handle_instant_meet, user_id, response_url)
                return JSONResponse({
                    "response_type": "ephemeral",
                    "text": "⏳ Creating your meeting link..."
                })

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
async def actions(request: Request, background_tasks: BackgroundTasks):
    try:
        form = await request.form()
        payload = json.loads(form.get("payload", "{}"))

        actions_list = payload.get("actions", [])
        response_url = payload.get("response_url")

        if not actions_list:
            return JSONResponse({"text": "❌ No action found."})

        action = actions_list[0]
        action_id = action.get("action_id")
        user_id = action.get("value")

        if action_id == "instant_meet":
            background_tasks.add_task(handle_instant_meet, user_id, response_url)
            return JSONResponse({
                "response_type": "ephemeral",
                "text": "⏳ Creating your meeting link..."
            })

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