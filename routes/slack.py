"""
routes/slack.py  — MeetNow
──────────────────────────
Privacy-first design:
  • Meeting links are NEVER posted publicly
  • Organiser gets a private DM confirmation + cancel button
  • Invited person gets a private DM with the join link
  • Cancel confirmation is also private DM only
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


async def slack_api(bot_token: str, method: str, payload: dict) -> dict:
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


async def post_dm(bot_token: str, user_id: str, text: str, blocks: list = None):
    """Always opens a fresh DM channel and posts privately to the user."""
    result = await slack_api(bot_token, "conversations.open", {"users": user_id})
    channel_id = result.get("channel", {}).get("id")
    if not channel_id:
        print(f"🔥 Could not open DM with {user_id}")
        return
    payload = {"channel": channel_id, "text": text}
    if blocks:
        payload["blocks"] = blocks
    await slack_api(bot_token, "chat.postMessage", payload)


async def respond(response_url: str, payload: dict):
    async with httpx.AsyncClient() as client:
        resp = await client.post(response_url, json=payload, timeout=10)
        print(f"DEBUG respond status={resp.status_code}")


async def get_user_name(bot_token: str, user_id: str) -> str:
    result = await slack_api(bot_token, "users.info", {"user": user_id})
    if not result.get("ok"):
        return f"<@{user_id}>"
    profile = result.get("user", {}).get("profile", {})
    return profile.get("display_name") or result["user"].get("real_name") or f"<@{user_id}>"


def extract_mention(text: str) -> tuple[str | None, str | None]:
    """
    Returns (user_id, raw_username).
    Proper Slack mention <@U123|name>  → (U123, name)
    Plain typed          @username     → (None, username)
    """
    m = re.search(r"<@([A-Z0-9]+)(?:\|([^>]*)?)?>", text)
    if m:
        return m.group(1), m.group(2) or ""
    m = re.search(r"@([\w.]+)", text)
    if m:
        return None, m.group(1)
    return None, None


async def resolve_user_id(bot_token: str, user_id: str | None, username: str | None) -> str | None:
    """Resolve to a real Slack user ID — handles both proper mentions and plain @name."""
    if user_id:
        # Already have an ID — verify it exists
        r = await slack_api(bot_token, "users.info", {"user": user_id})
        return user_id if r.get("ok") else None
    if not username:
        return None
    # Search by name
    result = await slack_api(bot_token, "users.list", {})
    uname = username.lower()
    for member in result.get("members", []):
        if member.get("deleted") or member.get("is_bot"):
            continue
        p = member.get("profile", {})
        names = {
            (p.get("display_name") or "").lower(),
            (p.get("real_name") or "").lower(),
            (member.get("name") or "").lower(),
        }
        if uname in names:
            return member["id"]
    print(f"🔥 Could not resolve username: {username!r}")
    return None


# ─────────────────────────────────────────────────────────────────
# /meet  slash command
# ─────────────────────────────────────────────────────────────────

@router.post("/meet")
async def meet(request: Request, background_tasks: BackgroundTasks):
    try:
        form         = await request.form()
        user_id      = form.get("user_id")
        team_id      = form.get("team_id")
        channel_id   = form.get("channel_id")   # kept for context only
        text         = (form.get("text") or "").strip()
        response_url = form.get("response_url")
        trigger_id   = form.get("trigger_id")

        uid, uname = extract_mention(text)
        print(f"DEBUG /meet user_id={user_id} team_id={team_id} text={text!r} uid={uid!r} uname={uname!r}")

        if not user_id or not response_url:
            return JSONResponse({"response_type": "ephemeral", "text": "Missing required fields."})

        background_tasks.add_task(
            handle_meet, user_id, team_id, channel_id, text, uid, uname, response_url, trigger_id
        )
        return JSONResponse({"response_type": "ephemeral", "text": "One moment... ⏳"})

    except Exception as e:
        print("🔥 /meet ERROR:", str(e))
        return JSONResponse({"response_type": "ephemeral", "text": "Something went wrong."})


async def handle_meet(
    user_id: str, team_id: str, channel_id: str, text: str,
    uid: str | None, uname: str | None,
    response_url: str, trigger_id: str
):
    google_auth_url = f"{BASE_URL}/auth?user_id={user_id}&team_id={team_id}"
    try:
        bot_token = get_token(team_id)
        if not bot_token:
            await respond(response_url, {
                "replace_original": True, "response_type": "ephemeral",
                "text": f"MeetNow isn't installed properly. Reinstall: {BASE_URL}/slack/install"
            })
            return

        # Resolve the @mentioned person's Slack user ID
        mentioned_user_id = await resolve_user_id(bot_token, uid, uname)
        print(f"DEBUG resolved mentioned_user_id={mentioned_user_id!r}")

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

        text_lower = text.lower()

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
        form         = await request.form()
        payload      = json.loads(form.get("payload", "{}"))
        payload_type = payload.get("type")

        # ── Modal submissions ──────────────────────────────────
        if payload_type == "view_submission":
            view = payload.get("view", {})
            cb   = view.get("callback_id", "")

            if cb == "schedule_modal":
                meta       = json.loads(view.get("private_metadata", "{}"))
                slack_user = payload.get("user", {}).get("id")
                values     = view["state"]["values"]

                title    = values["title_block"]["title_input"]["value"]
                date     = values["date_block"]["date_input"]["selected_date"]
                time     = values["time_block"]["time_input"]["selected_time"]
                duration = int(values["duration_block"]["duration_input"]["selected_option"]["value"])
                notes    = (values.get("notes_block", {}).get("notes_input", {}).get("value") or "")

                background_tasks.add_task(
                    handle_scheduled_meet,
                    meta["user_id"], meta["team_id"], meta["channel_id"],
                    meta.get("mentioned_user_id") or None,
                    slack_user, title, date, time, duration, notes
                )
                return JSONResponse({})

            if cb == "cancel_confirm_modal":
                meta = json.loads(view.get("private_metadata", "{}"))
                background_tasks.add_task(
                    handle_cancel_meeting,
                    meta["user_id"], meta["team_id"],
                    meta["event_id"], meta["slack_user_id"], meta["title"]
                )
                return JSONResponse({})

        # ── Button clicks ──────────────────────────────────────
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

        if action_id in ("choice_instant", "choice_schedule"):
            parts = value.split("|")
            # format: "instant|user_id|team_id|channel_id|mentioned_uid|session_id"
            if len(parts) != 6:
                return JSONResponse({})
            _, user_id, team_id_val, channel_id, mentioned_uid, sid = parts
            mentioned_uid = mentioned_uid or None

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
                    handle_instant_meet, user_id, team_id_val, channel_id, mentioned_uid, bot_token
                )
            else:
                await respond(response_url, {
                    "replace_original": True, "response_type": "ephemeral",
                    "text": "📅 Opening scheduler..."
                })
                background_tasks.add_task(
                    open_schedule_modal, trigger_id, user_id, team_id_val, channel_id, mentioned_uid, bot_token
                )
            return JSONResponse({})

        if action_id == "cancel_meeting":
            ctx        = json.loads(value)
            slack_user = payload.get("user", {}).get("id")
            background_tasks.add_task(
                open_cancel_modal,
                trigger_id, ctx["event_id"], ctx["user_id"],
                ctx["team_id"], ctx["title"], slack_user, bot_token
            )
            return JSONResponse({})

        return JSONResponse({})

    except Exception as e:
        print("🔥 /actions ERROR:", str(e))
        return JSONResponse({})


# ─────────────────────────────────────────────────────────────────
# Core meeting flows — ALL private DMs, nothing public
# ─────────────────────────────────────────────────────────────────

async def handle_instant_meet(
    user_id: str, team_id: str,
    channel_id: str, mentioned_user_id: str | None,
    bot_token: str
):
    db = SessionLocal()
    try:
        organiser = get_db_user(db, user_id)
        if not organiser:
            await post_dm(bot_token, user_id,
                          f"⚠️ Connect Google first: {BASE_URL}/auth?user_id={user_id}&team_id={team_id}")
            return

        meet_link, cal_event_id = create_meeting(organiser)
        if not meet_link:
            await post_dm(bot_token, user_id, "❌ Failed to create meeting. Check Google Calendar access.")
            return

        event_id = str(uuid.uuid4())
        db.add(MeetingRecord(
            event_id=event_id, user_id=user_id, team_id=team_id,
            title="Instant Meeting", meet_link=meet_link,
            start_time="now", calendar_event_id=cal_event_id,
        ))
        db.commit()

        organiser_name = await get_user_name(bot_token, user_id)
        cancel_val = json.dumps({
            "event_id": event_id, "user_id": user_id,
            "team_id": team_id, "title": "Instant Meeting"
        })
        cancel_btn = [{
            "type": "button", "style": "danger",
            "text": {"type": "plain_text", "text": "🗑 Cancel Meeting"},
            "action_id": "cancel_meeting", "value": cancel_val
        }]

        # DM the invited person
        if mentioned_user_id and mentioned_user_id != user_id:
            await post_dm(bot_token, mentioned_user_id,
                text=f"📞 {organiser_name} invited you to a meeting: {meet_link}",
                blocks=[
                    {"type": "section", "text": {"type": "mrkdwn",
                     "text": f"👋 *<@{user_id}> invited you to a meeting!*\n"
                             f"📞 *Join here:* {meet_link}"}},
                    {"type": "context", "elements": [
                        {"type": "mrkdwn", "text": "Click the link above to join instantly."}
                    ]}
                ])

        # DM organiser — private confirmation + cancel
        await post_dm(bot_token, user_id,
            text=f"✅ Your meeting is live: {meet_link}",
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn",
                 "text": f"✅ *Your meeting is live!*\n"
                         f"📞 {meet_link}\n"
                         + (f"_Invited: <@{mentioned_user_id}>_" if mentioned_user_id else "_No one invited yet._")}},
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
    db = SessionLocal()
    bot_token = get_token(team_id)
    try:
        organiser = get_db_user(db, user_id)
        if not organiser:
            await post_dm(bot_token, user_id,
                          f"⚠️ Connect Google first: {BASE_URL}/auth?user_id={user_id}&team_id={team_id}")
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
            "event_id": event_id, "user_id": user_id,
            "team_id": team_id, "title": title
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

        # DM the invited person
        if mentioned_user_id and mentioned_user_id != user_id:
            await post_dm(bot_token, mentioned_user_id,
                text=f"📅 {organiser_name} scheduled a meeting with you: {meet_link}",
                blocks=[
                    {"type": "section", "text": {"type": "mrkdwn",
                     "text": f"👋 *<@{user_id}> scheduled a meeting with you!*\n{summary}"}},
                    {"type": "context", "elements": [
                        {"type": "mrkdwn", "text": "Click the link above to join at the scheduled time."}
                    ]}
                ])

        # DM organiser — private confirmation + cancel
        await post_dm(bot_token, user_id,
            text=f"✅ Meeting scheduled: {meet_link}",
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn",
                 "text": f"✅ *Meeting scheduled!*\n{summary}\n"
                         + (f"_Invited: <@{mentioned_user_id}>_" if mentioned_user_id else "_No one invited yet._")}},
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
        "channel_id": channel_id,
        "mentioned_user_id": mentioned_user_id or ""
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
    r = await slack_api(bot_token, "views.open", modal)
    if not r.get("ok"):
        print("🔥 Modal open failed:", r)


async def open_cancel_modal(
    trigger_id: str, event_id: str, user_id: str, team_id: str,
    title: str, slack_user_id: str, bot_token: str
):
    meta = json.dumps({
        "event_id": event_id, "user_id": user_id,
        "team_id": team_id, "slack_user_id": slack_user_id,
        "title": title
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
                         "This will delete it from your Google Calendar."}}
            ]
        }
    }
    r = await slack_api(bot_token, "views.open", modal)
    if not r.get("ok"):
        print("🔥 Cancel modal failed:", r)


async def handle_cancel_meeting(
    user_id: str, team_id: str,
    event_id: str, slack_user_id: str, title: str
):
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

        if cal_deleted:
            msg = f"✅ *{title}* cancelled and removed from Google Calendar."
        else:
            msg = (f"⚠️ *{title}* removed from MeetNow but couldn't delete from Google Calendar "
                   "— please remove it manually.")
        await post_dm(bot_token, slack_user_id, msg)

    except Exception as e:
        print("🔥 handle_cancel_meeting ERROR:", str(e))
        if bot_token:
            await post_dm(bot_token, slack_user_id, "❌ Something went wrong cancelling the meeting.")
    finally:
        db.close()