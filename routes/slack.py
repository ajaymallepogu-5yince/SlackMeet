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

    # 🔥 keywords for instant meeting
    instant_keywords = ["connect", "meet", "call", "phone"]

    # ================================
    # ✅ SCENARIO 2 → INSTANT MEETING
    # ================================
    if any(word in text for word in instant_keywords):

        # ❌ not connected → force auth
        if user_id not in user_tokens:
            return JSONResponse({
                "response_type": "ephemeral",
                "text": "⚠️ Please connect Google first: https://slackmeet-production.up.railway.app/auth"
            })

        # ✅ create meeting directly
        credentials = user_tokens[user_id]
        meet_link = create_meeting(credentials)

        return JSONResponse({
            "response_type": "in_channel",
            "text": f"📞 Instant meeting ready: {meet_link}"
        })

    # ======================================
    # ✅ SCENARIO 1 → SHOW BUTTONS ALWAYS
    # ======================================

    # ❌ Not connected → show connect button first
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
                            "url": "https://slackmeet-production.up.railway.app/auth"
                        }
                    ]
                }
            ]
        })

    # ✅ Already connected → show action buttons
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
                        "url": "https://slackmeet-production.up.railway.app/instant-meet"
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