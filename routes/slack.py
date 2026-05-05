from fastapi import APIRouter, Form
from fastapi.responses import JSONResponse

from services.google import create_meeting

router = APIRouter()


@router.post("/meet")
async def meet(command: str = Form(...), text: str = Form(...)):
    meet_link = create_meeting()

    if not meet_link: 
        return JSONResponse(
            content={"text": "⚠️ Please connect Google : /auth"}
        )

    return JSONResponse(
        content={
            "response_type": "in_channel",
            "text": f"Meeting created ✅\n{meet_link}",
        }
    )