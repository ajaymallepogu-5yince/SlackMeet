from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from google_auth_oauthlib.flow import Flow

from core.config import GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, BASE_URL, SCOPES
from storage.tokens import user_tokens
from storage.oauth import flows

router = APIRouter()


def get_flow():
    return Flow.from_client_config(
        {
            "web": {
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=SCOPES,
        redirect_uri=f"{BASE_URL}/callback",
    )


@router.get("/auth")
def auth():
    flow = get_flow()

    auth_url, _ = flow.authorization_url(prompt="consent")

    # ✅ Store flow (fixes "missing code verifier")
    flows["default"] = flow

    return RedirectResponse(auth_url)


@router.get("/callback")
def callback(request: Request):
    code = request.query_params.get("code")

    flow = flows.get("default")

    if not flow:
        return {"error": "Flow not found. Please retry login."}

    flow.fetch_token(code=code)

    credentials = flow.credentials

    user_tokens["default"] = credentials

    return {"message": "Google connected ✅"}