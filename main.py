from fastapi import FastAPI, Request
import requests
import os


SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
app = FastAPI()

@app.post("/meet")
async def meet(request: Request):
    form = await request.form()
    
    user_name = form.get("user_name")
    channel_id = form.get("channel_id")

    message = f"{user_name} started a meeting 🚀\n👉 Join: https://meet.google.com/test"

    requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={
            "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
            "Content-Type": "application/json"
        },
        json={
            "channel": channel_id,
            "text": message
        }
    )

    return {
        "response_type": "ephemeral",
        "text": "Meeting created ✅"
    }