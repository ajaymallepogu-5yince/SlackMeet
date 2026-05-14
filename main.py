from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, JSONResponse
from google_auth_oauthlib.flow import Flow

from core.config import GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, BASE_URL, GOOGLE_SCOPES
from core.database import SessionLocal
from models.user_token import UserToken

router = APIRouter()

# In-memory store of active flows keyed by "{team_id}:{user_id}"
_flows: dict[str, Flow] = {}


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
def auth(user_id: str, team_id: str = ""):
    flow = make_flow()
    state = f"{team_id}:{user_id}"
    auth_url, _ = flow.authorization_url(
        prompt="consent",
        access_type="offline",
        state=state,
    )
    _flows[state] = flow
    return RedirectResponse(auth_url)


@router.get("/callback")
def callback(request: Request):
    code = request.query_params.get("code")
    state = request.query_params.get("state", ":")

    try:
        team_id, user_id = state.split(":", 1)
    except ValueError:
        return JSONResponse({"error": "Invalid state"}, status_code=400)

    if not user_id or not code:
        return JSONResponse({"error": "Missing code or state"}, status_code=400)

    flow = _flows.pop(state, None)
    if flow is None:
        return JSONResponse(
            {"error": "OAuth session expired. Please try /auth again."},
            status_code=400,
        )

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

    return JSONResponse({"message": "✅ Google connected! You can close this tab and return to Slack."})