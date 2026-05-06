from fastapi import APIRouter
from fastapi.responses import JSONResponse
from storage.tokens import user_tokens

router = APIRouter()

@router.post("/meet")
async def meet():
    # ✅ Check if user already connected
    if "default" not in user_tokens:
        return JSONResponse({
            "response_type": "ephemeral",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "⚠️ Please connect your Google account first"
                    }
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "Connect Google"
                            },
                            "url": "https://slackmeet-production.up.railway.app/auth"
                        }
                    ]
                }
            ]
        })

    # ✅ If connected → create meeting
    return JSONResponse({
        "response_type": "ephemeral",
        "text": "✅ Google already connected. Creating meeting..."
    })