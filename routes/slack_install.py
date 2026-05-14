from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, HTMLResponse
import httpx

from core.config import SLACK_CLIENT_ID, SLACK_CLIENT_SECRET, SLACK_SCOPES, BASE_URL
from core.database import SessionLocal
from models.user_token import WorkspaceInstall

router = APIRouter()


@router.get("/slack/install")
def slack_install():
    """Entry point — redirect user to Slack's OAuth consent page."""
    scopes = ",".join(SLACK_SCOPES)
    url = (
        f"https://slack.com/oauth/v2/authorize"
        f"?client_id={SLACK_CLIENT_ID}"
        f"&scope={scopes}"
        f"&redirect_uri={BASE_URL}/slack/callback"
    )
    return RedirectResponse(url)


@router.get("/slack/callback")
async def slack_callback(request: Request):
    """Slack redirects here after user approves the install."""
    code = request.query_params.get("code")
    error = request.query_params.get("error")

    if error or not code:
        return HTMLResponse(_page("❌ Installation cancelled", "You can close this tab and try again.", error=True))

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://slack.com/api/oauth.v2.access",
            data={
                "client_id": SLACK_CLIENT_ID,
                "client_secret": SLACK_CLIENT_SECRET,
                "code": code,
                "redirect_uri": f"{BASE_URL}/slack/callback",
            },
        )

    data = resp.json()

    if not data.get("ok"):
        return HTMLResponse(_page(
            "❌ Installation failed",
            f"Slack returned: {data.get('error', 'unknown error')}",
            error=True
        ))

    team_id = data["team"]["id"]
    team_name = data["team"]["name"]
    bot_token = data["access_token"]
    bot_user_id = data["bot_user_id"]

    db = SessionLocal()
    try:
        install = db.query(WorkspaceInstall).filter(WorkspaceInstall.team_id == team_id).first()
        if not install:
            install = WorkspaceInstall(team_id=team_id)

        install.team_name = team_name
        install.bot_token = bot_token
        install.bot_user_id = bot_user_id

        db.add(install)
        db.commit()
    finally:
        db.close()

    return HTMLResponse(_page(
        "✅ MeetNow installed!",
        f"<strong>MeetNow</strong> has been added to <strong>{team_name}</strong>.<br><br>"
        "Type <code>/meet @someone</code> in any channel to get started.",
    ))


def get_bot_token(team_id: str) -> str | None:
    """Look up the bot token for a workspace."""
    db = SessionLocal()
    try:
        install = db.query(WorkspaceInstall).filter(WorkspaceInstall.team_id == team_id).first()
        return install.bot_token if install else None
    finally:
        db.close()


def _page(title: str, body: str, error: bool = False) -> str:
    color = "#e53e3e" if error else "#38a169"
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>{title} — MeetNow</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: #f7f8fc; display: flex; align-items: center;
            justify-content: center; min-height: 100vh; }}
    .card {{ background: #fff; border-radius: 16px; padding: 48px 40px;
             max-width: 440px; width: 100%; text-align: center;
             box-shadow: 0 4px 24px rgba(0,0,0,0.08); }}
    .icon {{ font-size: 48px; margin-bottom: 16px; }}
    h1 {{ font-size: 22px; font-weight: 600; color: {color}; margin-bottom: 12px; }}
    p {{ font-size: 15px; color: #555; line-height: 1.6; }}
    code {{ background: #f0f0f0; padding: 2px 8px; border-radius: 4px;
            font-size: 14px; color: #333; }}
    .btn {{ display: inline-block; margin-top: 28px; padding: 12px 28px;
            background: #4A154B; color: #fff; border-radius: 8px;
            text-decoration: none; font-size: 15px; font-weight: 500; }}
    .btn:hover {{ background: #611f69; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">{"❌" if error else "🚀"}</div>
    <h1>{title}</h1>
    <p>{body}</p>
    <a class="btn" href="slack://open">Open Slack</a>
  </div>
</body>
</html>"""