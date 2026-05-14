import json
import uuid
import httpx

from fastapi import APIRouter, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from core.config import BASE_URL, SLACK_BOT_TOKEN as FALLBACK_BOT_TOKEN
from core.database import SessionLocal
from models.user_token import UserToken, WorkspaceInstall, MeetingRecord
from services.google import create_meeting, create_scheduled_meeting, cancel_calendar_event

router = APIRouter()


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def get_db_user(db: Session, user_id: str):
    return db.query(UserToken).filter(UserToken.user_id == user_id).first()


def get_token(team_id: str) -> str | None:
    """Look up bot token from DB, fall back to SLACK_BOT_TOKEN env var."""
    db = SessionLocal()
    try:
        install = db.query(WorkspaceInstall).filter(WorkspaceInstall.team_id == team_id).first()
        if install and install.bot_token:
            return install.bot_token
        return FALLBACK_BOT_TOKEN  # fallback for dev / before OAuth install
    finally:
        db.close()


async def slack_post(bot_token: str, channel: str, text: str = None, blocks: list = None):
    payload = {"channel": channel}
    if text:
        payload["text"] = text
    if blocks:
        payload["blocks"] = blocks
    async with httpx.AsyncClient() as client:
        await client.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {bot_token}"},
            json=payload,
            timeout=10,
        )


async def slack_respond(response_url: str, payload: dict):
    async with httpx.AsyncClient() as client:
        await client.post(response_url, json=payload, timeout=10)


# ─────────────────────────────────────────────
# /meet slash command
# ─────────────────────────────────────────────

@router.post("/meet")
async def meet(request: Request, background_tasks: BackgroundTasks):
    try:
        form = await request.form()
        user_id = form.get("user_id")
        team_id = form.get("team_id")
        text = (form.get("text") or "").lower().strip()
        response_url = form.get("response_url")
        trigger_id = form.get("trigger_id")

        print(f"DEBUG /meet user_id={user_id} team_id={team_id} text={text!r}")

        if not user_id:
            return JSONResponse({"response_type": "ephemeral", "text": "❌ Missing user_id."})

        bot_token = get_token(team_id)
        if not bot_token:
            return JSONResponse({
                "response_type": "ephemeral",
                "text": f"⚠️ MeetNow isn't installed properly. Please reinstall: {BASE_URL}/slack/install"
            })

        db = SessionLocal()
        try:
            user = get_db_user(db, user_id)
            google_auth_url = f"{BASE_URL}/auth?user_id={user_id}&team_id={team_id}"

            if any(w in text for w in ["connect", "now", "instant"]):
                if not user:
                    return _need_google(google_auth_url)
                background_tasks.add_task(handle_instant_meet, user_id, team_id, response_url)
                return JSONResponse({"response_type": "ephemeral", "text": "⏳ Creating your meeting link..."})

            if any(w in text for w in ["schedule", "later", "plan"]):
                if not user:
                    return _need_google(google_auth_url)
                background_tasks.add_task(open_schedule_modal, trigger_id, user_id, team_id, bot_token)
                return JSONResponse({"response_type": "ephemeral", "text": "📅 Opening scheduler..."})

            if not user:
                return _need_google(google_auth_url)

            session_id = str(uuid.uuid4())
            return JSONResponse({
                "response_type": "ephemeral",
                "text": "👋 What would you like to do?",
                "blocks": _choice_blocks(user_id, team_id, session_id),
            })
        finally:
            db.close()

    except Exception as e:
        print("🔥 /meet ERROR:", str(e))
        return JSONResponse({"response_type": "ephemeral", "text": "❌ Something went wrong."})


def _need_google(auth_url: str):
    return JSONResponse({
        "response_type": "ephemeral",
        "text": "👋 Connect Google to use MeetNow",
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": "👋 *Connect Google to use MeetNow*"}},
            {"type": "actions", "elements": [{
                "type": "button",
                "text": {"type": "plain_text", "text": "🔗 Connect Google"},
                "url": auth_url,
                "style": "primary"
            }]}
        ]
    })


def _choice_blocks(user_id: str, team_id: str, session_id: str) -> list:
    now_val = json.dumps({"action": "instant", "user_id": user_id, "team_id": team_id, "sid": session_id})
    sched_val = json.dumps({"action": "schedule", "user_id": user_id, "team_id": team_id, "sid": session_id})
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "👋 What would you like to do? *(pick one)*"}
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "⚡ Connect Now"},
                    "action_id": "choice_button",
                    "value": now_val,
                    "style": "primary"
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "📅 Schedule Later"},
                    "action_id": "choice_button",
                    "value": sched_val,
                }
            ]
        }
    ]


# ─────────────────────────────────────────────
# /actions  (button clicks + modal submissions)
# ─────────────────────────────────────────────

_used_sessions: set[str] = set()


@router.post("/actions")
async def actions(request: Request, background_tasks: BackgroundTasks):
    try:
        form = await request.form()
        payload = json.loads(form.get("payload", "{}"))
        payload_type = payload.get("type")

        # ── Modal submitted ──────────────────────────────
        if payload_type == "view_submission":
            view = payload.get("view", {})
            cb = view.get("callback_id", "")

            if cb == "schedule_modal":
                meta = json.loads(view.get("private_metadata", "{}"))
                user_id = meta["user_id"]
                team_id = meta["team_id"]
                slack_user_id = payload.get("user", {}).get("id")
                values = view["state"]["values"]

                title = values["title_block"]["title_input"]["value"]
                date = values["date_block"]["date_input"]["selected_date"]
                time = values["time_block"]["time_input"]["selected_time"]
                duration = int(values["duration_block"]["duration_input"]["selected_option"]["value"])
                notes = (values.get("notes_block", {}).get("notes_input", {}).get("value") or "")

                background_tasks.add_task(
                    handle_scheduled_meet,
                    user_id, team_id, slack_user_id,
                    title, date, time, duration, notes
                )
                return JSONResponse({})

            if cb == "cancel_confirm_modal":
                meta = json.loads(view.get("private_metadata", "{}"))
                background_tasks.add_task(
                    handle_cancel_meeting,
                    meta["user_id"], meta["team_id"],
                    meta["event_id"], meta["slack_user_id"]
                )
                return JSONResponse({})

        # ── Button clicked ───────────────────────────────
        actions_list = payload.get("actions", [])
        response_url = payload.get("response_url")
        trigger_id = payload.get("trigger_id")
        team_id = payload.get("team", {}).get("id")
        bot_token = get_token(team_id)

        if not actions_list:
            return JSONResponse({})

        action = actions_list[0]
        action_id = action.get("action_id")

        if action_id == "choice_button":
            ctx = json.loads(action.get("value", "{}"))
            sid = ctx.get("sid")
            user_id = ctx.get("user_id")
            chosen = ctx.get("action")

            if sid in _used_sessions:
                await slack_respond(response_url, {
                    "replace_original": True,
                    "response_type": "ephemeral",
                    "text": "⚠️ You already used this. Type `/meet @user` again to get fresh options."
                })
                return JSONResponse({})

            _used_sessions.add(sid)

            if chosen == "instant":
                await slack_respond(response_url, {
                    "replace_original": True,
                    "response_type": "ephemeral",
                    "text": "⏳ Creating your meeting link..."
                })
                background_tasks.add_task(handle_instant_meet, user_id, team_id, response_url)

            elif chosen == "schedule":
                await slack_respond(response_url, {
                    "replace_original": True,
                    "response_type": "ephemeral",
                    "text": "📅 Opening scheduler..."
                })
                background_tasks.add_task(open_schedule_modal, trigger_id, user_id, team_id, bot_token)

            return JSONResponse({})

        if action_id == "cancel_meeting":
            ctx = json.loads(action.get("value", "{}"))
            slack_user_id = payload.get("user", {}).get("id")
            background_tasks.add_task(
                open_cancel_modal,
                trigger_id, ctx["event_id"], ctx["user_id"],
                ctx["team_id"], ctx["title"], slack_user_id, bot_token
            )
            return JSONResponse({})

        return JSONResponse({})

    except Exception as e:
        print("🔥 /actions ERROR:", str(e))
        return JSONResponse({})


# ─────────────────────────────────────────────
# Background tasks
# ─────────────────────────────────────────────

async def handle_instant_meet(user_id: str, team_id: str, response_url: str):
    db = SessionLocal()
    try:
        user = get_db_user(db, user_id)
        if not user:
            await slack_respond(response_url, {
                "replace_original": True,
                "response_type": "ephemeral",
                "text": f"⚠️ Connect Google first: {BASE_URL}/auth?user_id={user_id}&team_id={team_id}"
            })
            return

        meet_link, cal_event_id = create_meeting(user)
        if not meet_link:
            await slack_respond(response_url, {
                "replace_original": True,
                "response_type": "ephemeral",
                "text": "❌ Failed to create meeting. Check Google Calendar access."
            })
            return

        event_id = str(uuid.uuid4())
        record = MeetingRecord(
            event_id=event_id,
            user_id=user_id,
            team_id=team_id,
            title="Instant Slack Meeting",
            meet_link=meet_link,
            start_time="now",
            calendar_event_id=cal_event_id,
        )
        db.add(record)
        db.commit()

        cancel_val = json.dumps({
            "event_id": event_id, "user_id": user_id,
            "team_id": team_id, "title": "Instant Slack Meeting"
        })
        await slack_respond(response_url, {
            "replace_original": True,
            "response_type": "in_channel",
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": f"📞 *Meeting ready!*\n{meet_link}"}},
                {"type": "actions", "elements": [{
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🗑 Cancel Meeting"},
                    "action_id": "cancel_meeting",
                    "value": cancel_val,
                    "style": "danger"
                }]}
            ]
        })
    except Exception as e:
        print("🔥 handle_instant_meet ERROR:", str(e))
    finally:
        db.close()


async def handle_scheduled_meet(
    user_id: str, team_id: str, slack_user_id: str,
    title: str, date: str, time: str, duration: int, notes: str
):
    db = SessionLocal()
    bot_token = get_token(team_id)
    try:
        user = get_db_user(db, user_id)
        if not user:
            await slack_post(bot_token, slack_user_id,
                             text=f"⚠️ Connect Google first: {BASE_URL}/auth?user_id={user_id}&team_id={team_id}")
            return

        meet_link, cal_event_id = create_scheduled_meeting(user, title, date, time, duration, notes)
        if not meet_link:
            await slack_post(bot_token, slack_user_id, text="❌ Failed to schedule meeting.")
            return

        event_id = str(uuid.uuid4())
        record = MeetingRecord(
            event_id=event_id,
            user_id=user_id,
            team_id=team_id,
            title=title,
            meet_link=meet_link,
            start_time=f"{date} {time}",
            calendar_event_id=cal_event_id,
        )
        db.add(record)
        db.commit()

        cancel_val = json.dumps({
            "event_id": event_id, "user_id": user_id,
            "team_id": team_id, "title": title
        })
        await slack_post(bot_token, slack_user_id, blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"✅ *{title}* scheduled!\n"
                        f"📅 {date} at {time} ({duration} min)\n"
                        f"📞 {meet_link}"
                        + (f"\n📝 _{notes}_" if notes else "")
                    )
                }
            },
            {
                "type": "actions",
                "elements": [{
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🗑 Cancel Meeting"},
                    "action_id": "cancel_meeting",
                    "value": cancel_val,
                    "style": "danger"
                }]
            }
        ])
    except Exception as e:
        print("🔥 handle_scheduled_meet ERROR:", str(e))
        if bot_token:
            await slack_post(bot_token, slack_user_id, text="❌ Something went wrong scheduling the meeting.")
    finally:
        db.close()


async def open_schedule_modal(trigger_id: str, user_id: str, team_id: str, bot_token: str):
    meta = json.dumps({"user_id": user_id, "team_id": team_id})
    modal = {
        "trigger_id": trigger_id,
        "view": {
            "type": "modal",
            "callback_id": "schedule_modal",
            "private_metadata": meta,
            "title": {"type": "plain_text", "text": "📅 Schedule a Meeting"},
            "submit": {"type": "plain_text", "text": "Create Meeting"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {"type": "input", "block_id": "title_block",
                 "label": {"type": "plain_text", "text": "Meeting Title"},
                 "element": {
                     "type": "plain_text_input", "action_id": "title_input",
                     "placeholder": {"type": "plain_text", "text": "e.g. Team Sync, Design Review..."},
                     "max_length": 100
                 }},
                {"type": "input", "block_id": "date_block",
                 "label": {"type": "plain_text", "text": "Date"},
                 "element": {"type": "datepicker", "action_id": "date_input",
                             "placeholder": {"type": "plain_text", "text": "Select a date"}}},
                {"type": "input", "block_id": "time_block",
                 "label": {"type": "plain_text", "text": "Start Time"},
                 "element": {"type": "timepicker", "action_id": "time_input",
                             "placeholder": {"type": "plain_text", "text": "Select time"}}},
                {"type": "input", "block_id": "duration_block",
                 "label": {"type": "plain_text", "text": "Duration"},
                 "element": {
                     "type": "static_select", "action_id": "duration_input",
                     "placeholder": {"type": "plain_text", "text": "How long?"},
                     "options": [
                         {"text": {"type": "plain_text", "text": "15 minutes"}, "value": "15"},
                         {"text": {"type": "plain_text", "text": "30 minutes"}, "value": "30"},
                         {"text": {"type": "plain_text", "text": "45 minutes"}, "value": "45"},
                         {"text": {"type": "plain_text", "text": "1 hour"}, "value": "60"},
                         {"text": {"type": "plain_text", "text": "1.5 hours"}, "value": "90"},
                         {"text": {"type": "plain_text", "text": "2 hours"}, "value": "120"},
                     ]
                 }},
                {"type": "input", "block_id": "notes_block",
                 "label": {"type": "plain_text", "text": "Notes (optional)"},
                 "optional": True,
                 "element": {
                     "type": "plain_text_input", "action_id": "notes_input",
                     "multiline": True,
                     "placeholder": {"type": "plain_text", "text": "Agenda, topics..."},
                     "max_length": 500
                 }},
            ]
        }
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://slack.com/api/views.open",
            headers={"Authorization": f"Bearer {bot_token}", "Content-Type": "application/json"},
            json=modal, timeout=10
        )
        if not resp.json().get("ok"):
            print("🔥 Modal open failed:", resp.json())


async def open_cancel_modal(
    trigger_id: str, event_id: str, user_id: str,
    team_id: str, title: str, slack_user_id: str, bot_token: str
):
    meta = json.dumps({
        "event_id": event_id, "user_id": user_id,
        "team_id": team_id, "slack_user_id": slack_user_id
    })
    modal = {
        "trigger_id": trigger_id,
        "view": {
            "type": "modal",
            "callback_id": "cancel_confirm_modal",
            "private_metadata": meta,
            "title": {"type": "plain_text", "text": "Cancel Meeting"},
            "submit": {"type": "plain_text", "text": "Yes, Cancel It"},
            "close": {"type": "plain_text", "text": "Keep It"},
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"Are you sure you want to cancel *{title}*?\n\nThis will delete it from your Google Calendar."
                    }
                }
            ]
        }
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://slack.com/api/views.open",
            headers={"Authorization": f"Bearer {bot_token}", "Content-Type": "application/json"},
            json=modal, timeout=10
        )
        if not resp.json().get("ok"):
            print("🔥 Cancel modal failed:", resp.json())


async def handle_cancel_meeting(user_id: str, team_id: str, event_id: str, slack_user_id: str):
    db = SessionLocal()
    bot_token = get_token(team_id)
    try:
        record = db.query(MeetingRecord).filter(MeetingRecord.event_id == event_id).first()
        if not record:
            await slack_post(bot_token, slack_user_id,
                             text="⚠️ Meeting not found — it may have already been cancelled.")
            return

        user = get_db_user(db, user_id)
        success = cancel_calendar_event(user, record.calendar_event_id) if user else False

        db.delete(record)
        db.commit()

        if success:
            await slack_post(bot_token, slack_user_id,
                             text=f"✅ *{record.title}* cancelled and removed from Google Calendar.")
        else:
            await slack_post(bot_token, slack_user_id,
                             text=f"⚠️ Removed from MeetNow but couldn't delete from Google Calendar — please check there manually.")
    except Exception as e:
        print("🔥 handle_cancel_meeting ERROR:", str(e))
        await slack_post(bot_token, slack_user_id, text="❌ Something went wrong cancelling the meeting.")
    finally:
        db.close()