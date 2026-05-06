from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from google_auth_oauthlib.flow import Flow

from core.config import GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, BASE_URL, SCOPES
from core.database import SessionLocal
from models.user_token import UserToken

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
def auth(user_id: str):
    flow = get_flow()

    auth_url, _ = flow.authorization_url(
        prompt="consent",
        state=user_id  # pass user_id safely
    )

    return RedirectResponse(auth_url)


@router.get("/callback")
def callback(request: Request):
    code = request.query_params.get("code")
    user_id = request.query_params.get("state")

    flow = get_flow()
    flow.fetch_token(code=code)

    credentials = flow.credentials

    db = SessionLocal()

    user = db.query(UserToken).filter(UserToken.user_id == user_id).first()

    if not user:
        user = UserToken(user_id=user_id)

    user.access_token = credentials.token
    user.refresh_token = credentials.refresh_token
    user.token_uri = credentials.token_uri
    user.client_id = credentials.client_id
    user.client_secret = credentials.client_secret
    user.scopes = ",".join(credentials.scopes)

    db.add(user)
    db.commit()
    db.close()

    return {"message": "Google connected ✅"}