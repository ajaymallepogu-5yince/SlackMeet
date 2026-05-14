import json
import uuid
import httpx

from fastapi import APIRouter, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from datetime import datetime, timezone

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
    db = SessionLocal()
    try:
        install = db.query(WorkspaceInstall).filter(WorkspaceInstall.team_id == team_id).first()
        if install and install.bot_token:
            return install.bot_token
        return FALLBACK_BOT_TOKEN
    finally:
        db.close()


async def slack_post(bot_token: str, channel: str, text: str = None, blocks: list = None):
    payload = {"channel": channel}
    if text:
        payload["text"] = text
    if blocks:
        payload["blocks"] = blocks
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": "Bearer " + bot_token},
            json=payload,
            timeout=10,
        )
        data = resp.json()
        if not data.get("ok"):
            print("🔥 slack_post error:", data.get("error"))
        return data


async def slack_update(bot_token: str, channel: str, ts: str, text: str, blocks: list = None):
    """Edit an existing message in place."""
    payload = {"channel": channel, "ts": ts, "text": text}
    if blocks:
        payload["blocks"] = blocks
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://slack.com/api/chat.update",
            headers={"Authorization": "Bearer " + bot_token},
            json=payload,
            timeout=10,
        )
        data = resp.json()
        if not data.get("ok"):
            print("🔥 slack_update error:", data.get("error"))
        return data


async def slack_respond(response_url: str, payload: dict):
    async with httpx.AsyncClient() as client:
        resp = await client.post(response_url, json=payload, timeout=10)
        if resp.status_code != 200 or resp.text.strip() != "ok":
            print("🔥 slack_respond status=" + str(resp.status_code) + " body=" + resp.text[:300])


async def get_slack_user_tz(bot_token: str, user_id: str) -> str:
    """Returns IANA timezone string e.g. 'America/New_York', fallback UTC."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://slack.com/api/users.info",
            headers={"Authorization": "Bearer " + bot_token},
            params={"user": user_id},
            timeout=10,
        )
        data = resp.json()
        if data.get("ok"):
            return data["user"].get("tz", "UTC")
        return "UTC"


# ─────────────────────────────────────────────
# /meet slash command
# ─────────────────────────────────────────────

@router.post("/meet")
async def meet(request: Request, background_tasks: BackgroundTasks):
    # Ack Slack immediately (must respond within 3s), do all work in background.
    try:
        form = await request.form()
        user_id = form.get("user_id")
        team_id = form.get("team_id")
        text = (form.get("text") or "").lower().strip()
        response_url = form.get("response_url")
        trigger_id = form.get("trigger_id")

        print("DEBUG /meet user_id=" + str(user_id) + " team_id=" + str(team_id) + " text=" + repr(text))

        if not user_id or not response_url:
            return JSONResponse({"response_type": "ephemeral", "text": "Missing required fields."})

        background_tasks.add_task(
            handle_meet_background, user_id, team_id, text, response_url, trigger_id
        )
        return JSONResponse({"response_type": "ephemeral", "text": "One moment..."})

    except Exception as e:
        print("🔥 /meet ERROR:", str(e))
        return JSONResponse({"response_type": "ephemeral", "text": "Something went wrong."})


async def handle_meet_background(
    user_id: str, team_id: str, text: str, response_url: str, trigger_id: str
):
    google_auth_url = BASE_URL + "/auth?user_id=" + user_id + "&team_id=" + team_id
    need_google_msg = "👋 *Connect Google to use MeetNow*\n<" + google_auth_url + "|Click here to connect your Google account>"

    try:
        print("DEBUG handle_meet_background start user_id=" + user_id)
        bot_token = get_token(team_id)
        print("DEBUG bot_token found=" + str(bool(bot_token)))

        if not bot_token:
            await slack_respond(response_url, {
                "replace_original": True,
                "response_type": "ephemeral",
                "text": "MeetNow is not installed properly. Please reinstall: " + BASE_URL + "/slack/install",
            })
            return

        db = SessionLocal()
        try:
            user = get_db_user(db, user_id)
        finally:
            db.close()

        print("DEBUG user found=" + str(bool(user)))

        if any(w in text for w in ["connect", "now", "instant"]):
            if not user:
                await slack_respond(response_url, {"replace_original": True, "response_type": "ephemeral", "text": need_google_msg})
                return
            await slack_respond(response_url, {"replace_original": True, "response_type": "ephemeral", "text": "⚡ Creating your meeting link..."})
            await handle_instant_meet(user_id, team_id, response_url, bot_token)
            return

        if any(w in text for w in ["schedule", "later", "plan"]):
            if not user:
                await slack_respond(response_url, {"replace_original": True, "response_type": "ephemeral", "text": need_google_msg})
                return
            await open_schedule_modal(trigger_id, user_id, team_id, bot_token)
            return

        if not user:
            await slack_respond(response_url, {"replace_original": True, "response_type": "ephemeral", "text": need_google_msg})
            return

        # Show instant/schedule choice buttons
        session_id = str(uuid.uuid4())
        now_val = "instant|" + user_id + "|" + team_id + "|" + session_id
        sched_val = "schedule|" + user_id + "|" + team_id + "|" + session_id
        await slack_respond(response_url, {
            "replace_original": True,
            "response_type": "ephemeral",
            "text": "What would you like to do?",
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": "👋 *What would you like to do?*"}},
                {"type": "actions", "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": "⚡ Meet Now", "emoji": True},
                     "action_id": "choice_instant", "value": now_val, "style": "primary"},
                    {"type": "button", "text": {"type": "plain_text", "text": "📅 Schedule Later", "emoji": True},
                     "action_id": "choice_schedule", "value": sched_val},
                ]}
            ],
        })

    except Exception as e:
        print("🔥 handle_meet_background ERROR:", str(e))
        try:
            await slack_respond(response_url, {"replace_original": True, "response_type": "ephemeral", "text": "Something went wrong. Please try again."})
        except Exception:
            pass


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
                    meta["event_id"], meta["slack_user_id"],
                    meta.get("channel_id", ""), meta.get("message_ts", "")
                )
                return JSONResponse({})

        # ── Button clicked ───────────────────────────────
        actions_list = payload.get("actions", [])
        response_url = payload.get("response_url")
        trigger_id = payload.get("trigger_id")
        team_id = payload.get("team", {}).get("id")
        bot_token = get_token(team_id)
        channel_id = payload.get("channel", {}).get("id", "")
        message_ts = payload.get("message", {}).get("ts", "")

        if not actions_list:
            return JSONResponse({})

        action = actions_list[0]
        action_id = action.get("action_id")

        # ── Meet Now / Schedule Later choice ─────────────
        if action_id in ("choice_instant", "choice_schedule"):
            val = action.get("value", "")
            parts = val.split("|")
            if len(parts) != 4:
                return JSONResponse({})
            chosen, user_id, team_id_val, sid = parts

            if sid in _used_sessions:
                await slack_respond(response_url, {
                    "replace_original": True, "response_type": "ephemeral",
                    "text": "You already used this. Type /meet again to get fresh options."
                })
                return JSONResponse({})

            _used_sessions.add(sid)

            if action_id == "choice_instant":
                await slack_respond(response_url, {"replace_original": True, "response_type": "ephemeral", "text": "⚡ Creating your meeting link..."})
                background_tasks.add_task(handle_instant_meet, user_id, team_id_val, response_url, bot_token)
            else:
                await slack_respond(response_url, {"replace_original": True, "response_type": "ephemeral", "text": "📅 Opening scheduler..."})
                background_tasks.add_task(open_schedule_modal, trigger_id, user_id, team_id_val, bot_token)

            return JSONResponse({})

        # ── Cancel meeting button ────────────────────────
        if action_id == "cancel_meeting":
            val = action.get("value", "")
            parts = val.split("|", 3)  # split max 3 times so title can contain pipes
            if len(parts) != 4:
                return JSONResponse({})
            event_id, user_id, team_id_val, title = parts
            slack_user_id = payload.get("user", {}).get("id")
            background_tasks.add_task(
                handle_cancel_meeting,
                user_id, team_id_val, event_id, slack_user_id,
                channel_id, message_ts
            )
            return JSONResponse({})

        return JSONResponse({})

    except Exception as e:
        print("🔥 /actions ERROR:", str(e))
        return JSONResponse({})


# ─────────────────────────────────────────────
# Background tasks
# ─────────────────────────────────────────────

async def handle_instant_meet(user_id: str, team_id: str, response_url: str, bot_token: str):
    db = SessionLocal()
    try:
        user = get_db_user(db, user_id)
        if not user:
            await slack_respond(response_url, {
                "replace_original": True, "response_type": "ephemeral",
                "text": "Connect Google first: " + BASE_URL + "/auth?user_id=" + user_id + "&team_id=" + team_id
            })
            return

        # Get user's timezone so the Google event uses their local time
        user_tz = await get_slack_user_tz(bot_token, user_id)
        meet_link, cal_event_id = create_meeting(user, user_tz)
        if not meet_link:
            await slack_respond(response_url, {
                "replace_original": True, "response_type": "ephemeral",
                "text": "Failed to create meeting. Check your Google Calendar access."
            })
            return

        event_id = str(uuid.uuid4())
        # Friendly local time label
        now_label = datetime.now(timezone.utc).strftime("%b %d, %Y at %I:%M %p UTC")

        record = MeetingRecord(
            event_id=event_id, user_id=user_id, team_id=team_id,
            title="Instant Meeting", meet_link=meet_link,
            start_time=now_label, calendar_event_id=cal_event_id,
        )
        db.add(record)
        db.commit()

        # Cancel button value: pipe-delimited to avoid JSON block issues
        cancel_val = event_id + "|" + user_id + "|" + team_id + "|Instant Meeting"

        await slack_respond(response_url, {
            "replace_original": True,
            "response_type": "in_channel",
            "text": "Meeting ready! " + meet_link,
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": (
                    "🚀 *Your meeting is ready!*\n\n"
                    "📅 *" + now_label + "*\n"
                    "🔗 *Join:* " + meet_link
                )}},
                {"type": "divider"},
                {"type": "actions", "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": "🗑 Cancel Meeting", "emoji": True},
                     "action_id": "cancel_meeting", "value": cancel_val, "style": "danger"}
                ]}
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
                             text="Connect Google first: " + BASE_URL + "/auth?user_id=" + user_id + "&team_id=" + team_id)
            return

        meet_link, cal_event_id = create_scheduled_meeting(user, title, date, time, duration, notes)
        if not meet_link:
            await slack_post(bot_token, slack_user_id, text="Failed to schedule meeting. Please try again.")
            return

        event_id = str(uuid.uuid4())
        record = MeetingRecord(
            event_id=event_id, user_id=user_id, team_id=team_id,
            title=title, meet_link=meet_link,
            start_time=date + " " + time, calendar_event_id=cal_event_id,
        )
        db.add(record)
        db.commit()

        # Duration label
        if duration < 60:
            dur_label = str(duration) + " min"
        elif duration == 60:
            dur_label = "1 hour"
        else:
            dur_label = str(duration // 60) + "h " + (str(duration % 60) + "m" if duration % 60 else "")

        # Format date nicely: 2025-05-14 → May 14, 2025
        try:
            dt = datetime.strptime(date, "%Y-%m-%d")
            date_label = dt.strftime("%B %d, %Y")
        except Exception:
            date_label = date

        # Format time nicely: 14:30 → 2:30 PM
        try:
            t = datetime.strptime(time, "%H:%M")
            time_label = t.strftime("%I:%M %p").lstrip("0")
        except Exception:
            time_label = time

        cancel_val = event_id + "|" + user_id + "|" + team_id + "|" + title

        msg_text = "✅ *" + title + "* has been scheduled!"
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": (
                "✅ *" + title + "* has been scheduled!\n\n"
                "📅 *Date:* " + date_label + "\n"
                "🕐 *Time:* " + time_label + "\n"
                "⏱ *Duration:* " + dur_label + "\n"
                "🔗 *Join:* " + meet_link +
                ("\n📝 *Notes:* " + notes if notes else "")
            )}},
            {"type": "divider"},
            {"type": "actions", "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "🗑 Cancel Meeting", "emoji": True},
                 "action_id": "cancel_meeting", "value": cancel_val, "style": "danger"}
            ]}
        ]
        await slack_post(bot_token, slack_user_id, text=msg_text, blocks=blocks)

    except Exception as e:
        print("🔥 handle_scheduled_meet ERROR:", str(e))
        if bot_token:
            await slack_post(bot_token, slack_user_id, text="Something went wrong scheduling the meeting.")
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
            "title": {"type": "plain_text", "text": "Schedule a Meeting"},
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
                     "placeholder": {"type": "plain_text", "text": "Agenda, topics, attendees..."},
                     "max_length": 500
                 }},
            ]
        }
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://slack.com/api/views.open",
            headers={"Authorization": "Bearer " + bot_token, "Content-Type": "application/json"},
            json=modal, timeout=10
        )
        if not resp.json().get("ok"):
            print("🔥 Modal open failed:", resp.json())


async def handle_cancel_meeting(
    user_id: str, team_id: str, event_id: str, slack_user_id: str,
    channel_id: str = "", message_ts: str = ""
):
    db = SessionLocal()
    bot_token = get_token(team_id)
    try:
        record = db.query(MeetingRecord).filter(MeetingRecord.event_id == event_id).first()
        if not record:
            await slack_post(bot_token, slack_user_id, text="Meeting not found — it may have already been cancelled.")
            return

        title = record.title
        user = get_db_user(db, user_id)
        success = cancel_calendar_event(user, record.calendar_event_id) if user else False

        db.delete(record)
        db.commit()

        # Edit the original message to remove the meeting link and button
        if channel_id and message_ts and bot_token:
            status = "✅ *" + title + "* has been cancelled and removed from Google Calendar." if success else "⚠️ *" + title + "* removed from MeetNow (could not delete from Google Calendar — check there manually)."
            await slack_update(bot_token, channel_id, message_ts,
                text=title + " cancelled.",
                blocks=[
                    {"type": "section", "text": {"type": "mrkdwn", "text": status}}
                ]
            )
        else:
            # Fallback: just post a new message
            msg = "✅ *" + title + "* cancelled and removed from Google Calendar." if success else "⚠️ Removed from MeetNow but could not delete from Google Calendar."
            await slack_post(bot_token, slack_user_id, text=msg)

    except Exception as e:
        print("🔥 handle_cancel_meeting ERROR:", str(e))
        await slack_post(bot_token, slack_user_id, text="Something went wrong cancelling the meeting.")
    finally:
        db.close()