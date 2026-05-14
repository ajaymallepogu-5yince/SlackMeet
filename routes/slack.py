"""
routes/slack.py
───────────────
Handles all Slack interactions for MeetNow:
  POST /meet        — slash command
  POST /actions     — button clicks + modal submissions

Key design:
  • /meet acks Slack within 3 s, all real work runs in BackgroundTasks
  • Meeting link is posted to the CHANNEL (visible to everyone) via chat.postMessage
  • The @mentioned user gets a personal DM with the invite + join link
  • Organiser gets a private confirmation DM
  • Single-use buttons: session_id tracked in _used_sessions set
  • Cancel flow: confirmation modal → deletes from Google Calendar + posts status
"""

import json
import re
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


# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────

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


def extract_mentioned_user(text: str) -> str | None:
    """Pull the first <@UXXXXXXX> user ID out of slash command text."""
    match = re.search(r"<@([A-Z0-9]+)(?:\|[^>]*)?>", text)
    return match.group(1) if match else None


async def slack_api(bot_token: str, method: str, payload: dict) -> dict:
    """Call any Slack Web API method. Returns parsed JSON."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://slack.com/api/{method}",
            headers={"Authorization": f"Bearer {bot_token}", "Content-Type": "application/json"},
            json=payload,
            timeout=10,
        )
    data = resp.json()
    if not data.get("ok"):
        print(f"🔥 Slack API {method} failed:", data.get("error"))
    return data


async def post_message(bot_token: str, channel: str, text: str, blocks: list = None):
    """Post a message to a channel or DM."""
    payload = {"channel": channel, "text": text}
    if blocks:
        payload["blocks"] = blocks
    await slack_api(bot_token, "chat.postMessage", payload)


async def post_dm(bot_token: str, user_id: str, text: str, blocks: list = None):
    """Open a DM and post to it."""
    # Open DM channel first
    result = await slack_api(bot_token, "conversations.open", {"users": user_id})
    channel_id = result.get("channel", {}).get("id")
    if channel_id:
        await post_message(bot_token, channel_id, text, blocks)


async def respond(response_url: str, payload: dict):
    """Reply to a slash command via response_url."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(response_url, json=payload, timeout=10)
        print(f"DEBUG respond status={resp.status_code} body={resp.text[:200]}")


async def get_user_name(bot_token: str, user_id: str) -> str:
    """Resolve a Slack user ID to a display name."""
    result = await slack_api(bot_token, "users.info", {"user": user_id})
    return result.get("user", {}).get("profile", {}).get("display_name") or \
           result.get("user", {}).get("real_name") or user_id


# ─────────────────────────────────────────────────────────────────
# /meet  slash command
# ─────────────────────────────────────────────────────────────────

@router.post("/meet")
async def meet(request: Request, background_tasks: BackgroundTasks):
    """
    Slack calls this within the 3-second window.
    We ack immediately and do all work in a background task.
    """
    try:
        form = await request.form()
        user_id     = form.get("user_id")
        team_id     = form.get("team_id")
        channel_id  = form.get("channel_id")
        text        = (form.get("text") or "").strip()
        response_url = form.get("response_url")
        trigger_id  = form.get("trigger_id")

        print(f"DEBUG /meet user_id={user_id} team_id={team_id} text={text!r}")

        if not user_id or not response_url:
            return JSONResponse({"response_type": "ephemeral", "text": "Missing required fields."})

        background_tasks.add_task(
            handle_meet,
            user_id, team_id, channel_id, text, response_url, trigger_id
        )
        # Ack within 3 s — Slack requires this
        return JSONResponse({"response_type": "ephemeral", "text": "One moment... ⏳"})

    except Exception as e:
        print("🔥 /meet ERROR:", str(e))
        return JSONResponse({"response_type": "ephemeral", "text": "Something went wrong."})


async def handle_meet(
    user_id: str, team_id: str, channel_id: str,
    text: str, response_url: str, trigger_id: str
):
    """All real logic runs here after Slack has been acked."""
    google_auth_url = f"{BASE_URL}/auth?user_id={user_id}&team_id={team_id}"

    try:
        bot_token = get_token(team_id)
        if not bot_token:
            await respond(response_url, {
                "replace_original": True, "response_type": "ephemeral",
                "text": f"MeetNow isn't installed properly. Reinstall: {BASE_URL}/slack/install"
            })
            return

        # Who is being @mentioned?
        mentioned_user_id = extract_mentioned_user(text)
        text_lower = text.lower()

        db = SessionLocal()
        try:
            organiser = get_db_user(db, user_id)
        finally:
            db.close()

        if not organiser:
            await respond(response_url, {
                "replace_original": True, "response_type": "ephemeral",
                "blocks": [
                    {"type": "section", "text": {"type": "mrkdwn",
                     "text": "👋 *Connect your Google account to use MeetNow*"}},
                    {"type": "actions", "elements": [{
                        "type": "button", "style": "primary",
                        "text": {"type": "plain_text", "text": "🔗 Connect Google"},
                        "url": google_auth_url
                    }]}
                ]
            })
            return

        # Direct keyword shortcuts
        if any(w in text_lower for w in ["connect", "now", "instant"]):
            await respond(response_url, {
                "replace_original": True, "response_type": "ephemeral",
                "text": "⏳ Creating your meeting link..."
            })
            await handle_instant_meet(user_id, team_id, channel_id, mentioned_user_id, bot_token)
            return

        if any(w in text_lower for w in ["schedule", "later", "plan"]):
            await respond(response_url, {
                "replace_original": True, "response_type": "ephemeral",
                "text": "📅 Opening scheduler..."
            })
            await open_schedule_modal(trigger_id, user_id, team_id, channel_id, mentioned_user_id, bot_token)
            return

        # No keyword → show two single-use buttons
        session_id = str(uuid.uuid4())
        # Encode channel + mentioned user in the session value
        encoded = f"{user_id}|{team_id}|{channel_id}|{mentioned_user_id or ''}|{session_id}"
        await respond(response_url, {
            "replace_original": True, "response_type": "ephemeral",
            "blocks": _choice_blocks(encoded)
        })

    except Exception as e:
        print("🔥 handle_meet ERROR:", str(e))
        try:
            await respond(response_url, {
                "replace_original": True, "response_type": "ephemeral",
                "text": "Something went wrong. Please try again."
            })
        except Exception:
            pass


def _choice_blocks(encoded_value: str) -> list:
    return [
        {"type": "section",
         "text": {"type": "mrkdwn", "text": "*What would you like to do?* _(pick one)_"}},
        {"type": "actions", "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "⚡ Connect Now"},
                "action_id": "choice_instant",
                "value": "instant|" + encoded_value,
                "style": "primary"
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "📅 Schedule Later"},
                "action_id": "choice_schedule",
                "value": "schedule|" + encoded_value,
            }
        ]}
    ]


# ─────────────────────────────────────────────────────────────────
# /actions  button clicks + modal submissions
# ─────────────────────────────────────────────────────────────────

_used_sessions: set[str] = set()


@router.post("/actions")
async def actions(request: Request, background_tasks: BackgroundTasks):
    try:
        form = await request.form()
        payload = json.loads(form.get("payload", "{}"))
        payload_type = payload.get("type")

        # ── Modal submissions ────────────────────────────────────
        if payload_type == "view_submission":
            view = payload.get("view", {})
            cb   = view.get("callback_id", "")

            if cb == "schedule_modal":
                meta         = json.loads(view.get("private_metadata", "{}"))
                slack_user   = payload.get("user", {}).get("id")
                values       = view["state"]["values"]

                title    = values["title_block"]["title_input"]["value"]
                date     = values["date_block"]["date_input"]["selected_date"]
                time     = values["time_block"]["time_input"]["selected_time"]
                duration = int(values["duration_block"]["duration_input"]["selected_option"]["value"])
                notes    = (values.get("notes_block", {}).get("notes_input", {}).get("value") or "")

                background_tasks.add_task(
                    handle_scheduled_meet,
                    meta["user_id"], meta["team_id"],
                    meta["channel_id"], meta.get("mentioned_user_id"),
                    slack_user, title, date, time, duration, notes
                )
                return JSONResponse({})   # closes modal immediately

            if cb == "cancel_confirm_modal":
                meta = json.loads(view.get("private_metadata", "{}"))
                background_tasks.add_task(
                    handle_cancel_meeting,
                    meta["user_id"], meta["team_id"],
                    meta["event_id"], meta["slack_user_id"],
                    meta["channel_id"], meta["meet_link"], meta["title"]
                )
                return JSONResponse({})

        # ── Button clicks ────────────────────────────────────────
        actions_list = payload.get("actions", [])
        response_url = payload.get("response_url")
        trigger_id   = payload.get("trigger_id")
        team_id      = payload.get("team", {}).get("id")
        bot_token    = get_token(team_id)

        if not actions_list:
            return JSONResponse({})

        action    = actions_list[0]
        action_id = action.get("action_id")
        value     = action.get("value", "")

        # ── Choice buttons (Connect Now / Schedule Later) ────────
        if action_id in ("choice_instant", "choice_schedule"):
            parts = value.split("|")
            # format: "instant|user_id|team_id|channel_id|mentioned_uid|session_id"
            if len(parts) != 6:
                return JSONResponse({})
            _, user_id, team_id_val, channel_id, mentioned_uid, sid = parts
            mentioned_uid = mentioned_uid or None

            # Single-use enforcement
            if sid in _used_sessions:
                await respond(response_url, {
                    "replace_original": True, "response_type": "ephemeral",
                    "text": "⚠️ You already used these buttons. Type `/meet @user` again for fresh options."
                })
                return JSONResponse({})

            _used_sessions.add(sid)

            if action_id == "choice_instant":
                await respond(response_url, {
                    "replace_original": True, "response_type": "ephemeral",
                    "text": "⏳ Creating your meeting link..."
                })
                background_tasks.add_task(
                    handle_instant_meet,
                    user_id, team_id_val, channel_id, mentioned_uid, bot_token
                )
            else:
                await respond(response_url, {
                    "replace_original": True, "response_type": "ephemeral",
                    "text": "📅 Opening scheduler..."
                })
                background_tasks.add_task(
                    open_schedule_modal,
                    trigger_id, user_id, team_id_val, channel_id, mentioned_uid, bot_token
                )
            return JSONResponse({})

        # ── Cancel meeting button ────────────────────────────────
        if action_id == "cancel_meeting":
            ctx          = json.loads(value)
            slack_user   = payload.get("user", {}).get("id")
            background_tasks.add_task(
                open_cancel_modal,
                trigger_id, ctx["event_id"], ctx["user_id"],
                ctx["team_id"], ctx["title"], slack_user,
                ctx["channel_id"], ctx["meet_link"], bot_token
            )
            return JSONResponse({})

        return JSONResponse({})

    except Exception as e:
        print("🔥 /actions ERROR:", str(e))
        return JSONResponse({})


# ─────────────────────────────────────────────────────────────────
# Core meeting flows
# ─────────────────────────────────────────────────────────────────

async def handle_instant_meet(
    user_id: str, team_id: str,
    channel_id: str, mentioned_user_id: str | None,
    bot_token: str
):
    """
    Create an instant meeting then:
      1. Post the link to the channel so everyone can see it
      2. DM the @mentioned person with a personal invite
      3. DM the organiser with a confirmation + cancel button
    """
    db = SessionLocal()
    try:
        organiser = get_db_user(db, user_id)
        if not organiser:
            await post_dm(bot_token, user_id, "⚠️ Connect Google first: " +
                          BASE_URL + "/auth?user_id=" + user_id + "&team_id=" + team_id)
            return

        meet_link, cal_event_id = create_meeting(organiser)
        if not meet_link:
            await post_dm(bot_token, user_id, "❌ Failed to create meeting. Check Google Calendar access.")
            return

        # Save record for cancellation
        event_id = str(uuid.uuid4())
        db.add(MeetingRecord(
            event_id=event_id, user_id=user_id, team_id=team_id,
            title="Instant Meeting", meet_link=meet_link,
            start_time="now", calendar_event_id=cal_event_id,
        ))
        db.commit()

        organiser_name = await get_user_name(bot_token, user_id)
        cancel_val = json.dumps({
            "event_id": event_id, "user_id": user_id, "team_id": team_id,
            "title": "Instant Meeting", "channel_id": channel_id, "meet_link": meet_link
        })
        cancel_btn = [{
            "type": "button", "style": "danger",
            "text": {"type": "plain_text", "text": "🗑 Cancel Meeting"},
            "action_id": "cancel_meeting", "value": cancel_val
        }]

        # 1️⃣  Post to channel — visible to everyone
        channel_blocks = [
            {"type": "section", "text": {"type": "mrkdwn",
             "text": f"📞 *{organiser_name} started a meeting!*\n"
                     + (f"<@{mentioned_user_id}> you've been invited.\n" if mentioned_user_id else "")
                     + f"👉 *Join here:* {meet_link}"}},
            {"type": "actions", "elements": cancel_btn}
        ]
        await post_message(bot_token, channel_id,
                           text=f"Meeting started by {organiser_name}: {meet_link}",
                           blocks=channel_blocks)

        # 2️⃣  DM the invited person
        if mentioned_user_id and mentioned_user_id != user_id:
            await post_dm(bot_token, mentioned_user_id,
                text=f"You've been invited to a meeting by <@{user_id}>.",
                blocks=[
                    {"type": "section", "text": {"type": "mrkdwn",
                     "text": f"👋 *<@{user_id}> invited you to a meeting!*\n"
                             f"📞 *Join here:* {meet_link}"}}
                ])

        # 3️⃣  DM organiser with confirmation + cancel
        await post_dm(bot_token, user_id,
            text=f"✅ Meeting created: {meet_link}",
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn",
                 "text": f"✅ *Your meeting is live!*\n📞 {meet_link}\n"
                         + (f"_Invited: <@{mentioned_user_id}>_" if mentioned_user_id else "")}},
                {"type": "actions", "elements": cancel_btn}
            ])

    except Exception as e:
        print("🔥 handle_instant_meet ERROR:", str(e))
        await post_dm(bot_token, user_id, "❌ Something went wrong creating the meeting.")
    finally:
        db.close()


async def handle_scheduled_meet(
    user_id: str, team_id: str, channel_id: str,
    mentioned_user_id: str | None, slack_user_id: str,
    title: str, date: str, time: str, duration: int, notes: str
):
    """
    Create a scheduled meeting then:
      1. Post to channel
      2. DM the invited person
      3. DM organiser with confirmation + cancel button
    """
    db = SessionLocal()
    bot_token = get_token(team_id)
    try:
        organiser = get_db_user(db, user_id)
        if not organiser:
            await post_dm(bot_token, user_id, "⚠️ Connect Google first: " +
                          BASE_URL + "/auth?user_id=" + user_id + "&team_id=" + team_id)
            return

        meet_link, cal_event_id = create_scheduled_meeting(organiser, title, date, time, duration, notes)
        if not meet_link:
            await post_dm(bot_token, user_id, "❌ Failed to schedule meeting.")
            return

        event_id = str(uuid.uuid4())
        db.add(MeetingRecord(
            event_id=event_id, user_id=user_id, team_id=team_id,
            title=title, meet_link=meet_link,
            start_time=f"{date} {time}", calendar_event_id=cal_event_id,
        ))
        db.commit()

        organiser_name = await get_user_name(bot_token, user_id)
        cancel_val = json.dumps({
            "event_id": event_id, "user_id": user_id, "team_id": team_id,
            "title": title, "channel_id": channel_id, "meet_link": meet_link
        })
        cancel_btn = [{
            "type": "button", "style": "danger",
            "text": {"type": "plain_text", "text": "🗑 Cancel Meeting"},
            "action_id": "cancel_meeting", "value": cancel_val
        }]

        summary = (
            f"*{title}*\n"
            f"📅 {date} at {time} ({duration} min)\n"
            f"📞 {meet_link}"
            + (f"\n📝 _{notes}_" if notes else "")
        )

        # 1️⃣  Post to channel
        channel_blocks = [
            {"type": "section", "text": {"type": "mrkdwn",
             "text": f"📅 *{organiser_name} scheduled a meeting!*\n"
                     + (f"<@{mentioned_user_id}> you've been invited.\n" if mentioned_user_id else "")
                     + summary}},
            {"type": "actions", "elements": cancel_btn}
        ]
        await post_message(bot_token, channel_id,
                           text=f"Meeting scheduled by {organiser_name}: {meet_link}",
                           blocks=channel_blocks)

        # 2️⃣  DM invited person
        if mentioned_user_id and mentioned_user_id != user_id:
            await post_dm(bot_token, mentioned_user_id,
                text=f"<@{user_id}> scheduled a meeting with you.",
                blocks=[
                    {"type": "section", "text": {"type": "mrkdwn",
                     "text": f"👋 *<@{user_id}> scheduled a meeting with you!*\n{summary}"}}
                ])

        # 3️⃣  DM organiser confirmation
        await post_dm(bot_token, user_id,
            text=f"✅ Meeting scheduled: {meet_link}",
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn",
                 "text": f"✅ *Meeting scheduled!*\n{summary}\n"
                         + (f"_Invited: <@{mentioned_user_id}>_" if mentioned_user_id else "")}},
                {"type": "actions", "elements": cancel_btn}
            ])

    except Exception as e:
        print("🔥 handle_scheduled_meet ERROR:", str(e))
        if bot_token:
            await post_dm(bot_token, user_id, "❌ Something went wrong scheduling the meeting.")
    finally:
        db.close()


async def open_schedule_modal(
    trigger_id: str, user_id: str, team_id: str,
    channel_id: str, mentioned_user_id: str | None, bot_token: str
):
    meta = json.dumps({
        "user_id": user_id, "team_id": team_id,
        "channel_id": channel_id, "mentioned_user_id": mentioned_user_id or ""
    })
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
                 "element": {"type": "plain_text_input", "action_id": "title_input",
                             "placeholder": {"type": "plain_text", "text": "e.g. Team Sync, Design Review..."},
                             "max_length": 100}},
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
                         {"text": {"type": "plain_text", "text": "1 hour"},     "value": "60"},
                         {"text": {"type": "plain_text", "text": "1.5 hours"},  "value": "90"},
                         {"text": {"type": "plain_text", "text": "2 hours"},    "value": "120"},
                     ]
                 }},
                {"type": "input", "block_id": "notes_block",
                 "label": {"type": "plain_text", "text": "Notes (optional)"},
                 "optional": True,
                 "element": {"type": "plain_text_input", "action_id": "notes_input",
                             "multiline": True,
                             "placeholder": {"type": "plain_text", "text": "Agenda, topics..."},
                             "max_length": 500}},
            ]
        }
    }
    resp_data = await slack_api(bot_token, "views.open", modal)
    if not resp_data.get("ok"):
        print("🔥 Modal open failed:", resp_data)


async def open_cancel_modal(
    trigger_id: str, event_id: str, user_id: str, team_id: str,
    title: str, slack_user_id: str, channel_id: str, meet_link: str, bot_token: str
):
    meta = json.dumps({
        "event_id": event_id, "user_id": user_id, "team_id": team_id,
        "slack_user_id": slack_user_id, "channel_id": channel_id,
        "meet_link": meet_link, "title": title
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
                {"type": "section", "text": {"type": "mrkdwn",
                 "text": f"Are you sure you want to cancel *{title}*?\n\n"
                         "This will delete the event from your Google Calendar."}}
            ]
        }
    }
    resp_data = await slack_api(bot_token, "views.open", modal)
    if not resp_data.get("ok"):
        print("🔥 Cancel modal failed:", resp_data)


async def handle_cancel_meeting(
    user_id: str, team_id: str, event_id: str,
    slack_user_id: str, channel_id: str, meet_link: str, title: str
):
    """
    Cancel a meeting:
      1. Delete from Google Calendar
      2. Remove DB record
      3. Post cancellation notice to the channel
      4. DM the organiser confirmation
    """
    db = SessionLocal()
    bot_token = get_token(team_id)
    try:
        record = db.query(MeetingRecord).filter(MeetingRecord.event_id == event_id).first()
        if not record:
            await post_dm(bot_token, slack_user_id,
                          "⚠️ Meeting not found — it may already have been cancelled.")
            return

        organiser = get_db_user(db, user_id)
        cal_deleted = cancel_calendar_event(organiser, record.calendar_event_id) if organiser else False

        db.delete(record)
        db.commit()

        organiser_name = await get_user_name(bot_token, user_id)

        # 1️⃣  Notify the channel
        await post_message(
            bot_token, channel_id,
            text=f"❌ {title} cancelled by {organiser_name}.",
            blocks=[{"type": "section", "text": {"type": "mrkdwn",
                     "text": f"❌ *{title}* has been cancelled by <@{user_id}>.\n"
                             f"_The meeting link {meet_link} is no longer active._"}}]
        )

        # 2️⃣  DM organiser
        if cal_deleted:
            msg = f"✅ *{title}* cancelled and removed from Google Calendar."
        else:
            msg = f"⚠️ *{title}* removed from MeetNow but couldn't delete from Google Calendar — please remove it manually."
        await post_dm(bot_token, slack_user_id, msg)

    except Exception as e:
        print("🔥 handle_cancel_meeting ERROR:", str(e))
        if bot_token:
            await post_dm(bot_token, slack_user_id, "❌ Something went wrong cancelling the meeting.")
    finally:
        db.close()