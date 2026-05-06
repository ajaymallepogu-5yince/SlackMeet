from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from storage.tokens import user_tokens
from services.google import create_meeting

router = APIRouter()


@router.post("/meet")
async def meet(request: Request):
    form = await request.form()

    user_id = form.get("user_id")
    text = (form.get("text") or "").lower()

    BASE_URL = "https://slackmeet-production.up.railway.app"

    # 🔥 instant keywords
    instant_keywords = ["connect", "meet", "call", "phone"]

    # ================================
    # 🚀 SCENARIO 2 → INSTANT MEETING
    # ================================
    if any(word in text for word in instant_keywords):

        if user_id not in user_tokens:
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

    # ======================================
    # 🎯 SCENARIO 1 → SHOW BUTTONS
    # ======================================

    if user_id not in user_tokens:
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