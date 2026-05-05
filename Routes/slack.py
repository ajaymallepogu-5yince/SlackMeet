from fastapi import APIRouter
from storage.tokens import user_tokens
from services.google import create_meet
from core.config import BASE_URL

router = APIRouter()

@router.post("/meet")
def meet():
    # If not connected
    if "default" not in user_tokens:
        return {
            "response_type": "ephemeral",
            "blocks": [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "Connect Google first"}
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Connect Google"},
                            "url": f"{BASE_URL}/auth"
                        }
                    ]
                }
            ]
        }

    # Create meeting
    link = create_meet(user_tokens["default"])

    return {
        "response_type": "in_channel",
        "text": f"Meeting started 🚀\n👉 {link}"
    }