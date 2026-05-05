from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter()

@router.post("/meet")
async def meet():
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