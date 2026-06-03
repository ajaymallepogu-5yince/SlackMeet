import httpx
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, HTMLResponse
from google_auth_oauthlib.flow import Flow

from core.config import GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, BASE_URL, GOOGLE_SCOPES
from core.database import SessionLocal
from models.user_token import UserToken

router = APIRouter()

_flows: dict[str, dict] = {}   


def make_flow() -> Flow:
    return Flow.from_client_config(
        {
            "web": {
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=GOOGLE_SCOPES,
        redirect_uri=f"{BASE_URL}/callback",
    )


@router.get("/auth")
def auth(user_id: str, team_id: str = "", response_url: str = ""):
    flow = make_flow()
    state = f"{team_id}:{user_id}"
    auth_url, _ = flow.authorization_url(
        prompt="consent",
        access_type="offline",
        state=state,
    )
    _flows[state] = {"flow": flow, "response_url": response_url}
    return RedirectResponse(auth_url)


@router.get("/callback")
async def callback(request: Request):
    code = request.query_params.get("code")
    state = request.query_params.get("state", ":")

    try:
        team_id, user_id = state.split(":", 1)
    except ValueError:
        return HTMLResponse("<h2>❌ Invalid state.</h2>", status_code=400)

    if not user_id or not code:
        return HTMLResponse("<h2>❌ Missing code or state.</h2>", status_code=400)

    stored = _flows.pop(state, None)
    if stored is None:
        return HTMLResponse(
            "<h2>❌ Session expired. Please click Connect Google again in Slack.</h2>",
            status_code=400,
        )

    flow = stored["flow"]
    response_url = stored.get("response_url", "")

    flow.fetch_token(code=code)
    credentials = flow.credentials

    db = SessionLocal()
    try:
        user = db.query(UserToken).filter(UserToken.user_id == user_id).first()
        if not user:
            user = UserToken(user_id=user_id)

        user.team_id = team_id
        user.access_token = credentials.token
        user.refresh_token = credentials.refresh_token
        user.token_uri = credentials.token_uri
        user.client_id = credentials.client_id
        user.client_secret = credentials.client_secret
        user.scopes = ",".join(credentials.scopes)

        db.add(user)
        db.commit()
    finally:
        db.close()

    # Notify user in Slack if we have a successful auth 
    if response_url:
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    response_url,
                    json={
                        "response_type": "ephemeral", # 👈 Ensures it targets the hidden message
                        "replace_original": True,     # 👈 Deletes the Connect Google button
                        "text": "✅ Google account connected!",
                        "blocks": [
                            {
                                "type": "section",
                                "text": {
                                    "type": "mrkdwn",
                                    "text": (
                                        "✅ *Google account connected!*\n\n"
                                        "You're all set. Try `/meet` again to start your meeting. 🚀"
                                    )
                                }
                            }
                        ]
                    },
                    timeout=10,
                )
        except Exception as e:
            print(f"⚠️ Failed to post auth success to Slack: {e}")

    return HTMLResponse("<script>window.close();</script>")