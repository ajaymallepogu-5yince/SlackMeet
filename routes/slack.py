import json
import re
import uuid
import httpx
from datetime import datetime

from fastapi import APIRouter, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from core.config import (
    BASE_URL,
    SLACK_BOT_TOKEN as FALLBACK_BOT_TOKEN,
)

from core.database import SessionLocal

from models.user_token import (
    UserToken,
    WorkspaceInstall,
    MeetingRecord,
)

from services.google import (
    create_meeting,
    cancel_calendar_event,
    create_scheduled_meeting,
)

router = APIRouter()


# ─────────────────────────────────────────────────────────────────
# Database Helpers
# ─────────────────────────────────────────────────────────────────

def get_db_user(db: Session, user_id: str):
    return db.query(UserToken).filter(
        UserToken.user_id == user_id
    ).first()


def get_token(team_id: str):
    db = SessionLocal()
    try:
        install = db.query(WorkspaceInstall).filter(
            WorkspaceInstall.team_id == team_id
        ).first()
        if install and install.bot_token:
            return install.bot_token
        return FALLBACK_BOT_TOKEN
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────
# Slack API
# ─────────────────────────────────────────────────────────────────

async def slack_api(bot_token: str, method: str, payload: dict):
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"https://slack.com/api/{method}",
            headers={
                "Authorization": f"Bearer {bot_token}",
                "Content-Type": "application/json"
            },
            json=payload,
            timeout=20
        )
    data = response.json()
    if not data.get("ok"):
        print(f"🔥 Slack API Error {method}: {data}")
    return data


# ─────────────────────────────────────────────────────────────────
# Public Response (via response_url)
# ─────────────────────────────────────────────────────────────────

async def respond_in_channel(
    response_url: str,
    text: str,
    blocks: list = None
):
    payload = {"response_type": "in_channel", "text": text}
    if blocks:
        payload["blocks"] = blocks

    async with httpx.AsyncClient() as client:
        response = await client.post(response_url, json=payload, timeout=20)

    print("DEBUG response_url:", response.status_code)
    print("DEBUG response_url body:", response.text)

    try:
        return response.json()
    except Exception:
        return {}


# ─────────────────────────────────────────────────────────────────
# Private Response (ephemeral)
# ─────────────────────────────────────────────────────────────────

async def respond_to_user(
    response_url: str,
    text: str,
    blocks: list = None
):
    payload = {"response_type": "ephemeral", "text": text}
    if blocks:
        payload["blocks"] = blocks

    async with httpx.AsyncClient() as client:
        response = await client.post(response_url, json=payload, timeout=20)

    print("DEBUG ephemeral:", response.status_code)


# ─────────────────────────────────────────────────────────────────
# Post Meeting Message
# ─────────────────────────────────────────────────────────────────

async def post_meeting_message(
    response_url: str,
    text: str,
    blocks: list,
) -> str | None:
    result = await respond_in_channel(response_url, text, blocks=blocks)
    print(f"DEBUG post_meeting_message full response: {result}")
    return result.get("ts") or result.get("message", {}).get("ts")


# ─────────────────────────────────────────────────────────────────
# User Cache — refreshes every 1 hour
# ─────────────────────────────────────────────────────────────────

_members_cache = []
_members_cache_time = None
_used_sessions = set()


async def get_all_members(bot_token: str):
    global _members_cache, _members_cache_time

    if _members_cache and _members_cache_time:
        if (datetime.utcnow() - _members_cache_time).seconds < 3600:
            return _members_cache

    result = await slack_api(bot_token, "users.list", {})

    _members_cache = [
        m for m in result.get("members", [])
        if not m.get("deleted")
        and not m.get("is_bot")
        and m.get("id") != "USLACKBOT"
    ]
    _members_cache_time = datetime.utcnow()

    return _members_cache


# ─────────────────────────────────────────────────────────────────
# Mention Parsing — supports multiple @mentions
# ─────────────────────────────────────────────────────────────────

def extract_mentions(text: str) -> list[tuple[str | None, str | None]]:
    """Extract ALL @mentions. Returns list of (uid, uname) tuples."""
    results = []

    formatted = re.findall(r"<@([A-Z0-9]+)(?:\|([^>]*)?)?>", text)
    for uid, uname in formatted:
        results.append((uid, uname.lower() if uname else ""))

    if not results:
        plain = re.findall(r"@([\w.]+)", text)
        for uname in plain:
            results.append((None, uname.lower()))

    return results


async def resolve_invited_users(
    bot_token: str,
    mentions: list[tuple[str | None, str | None]]
) -> list[dict]:
    """Resolve ALL mentioned users. Returns list of member dicts."""
    members = await get_all_members(bot_token)
    resolved = []

    for uid, uname in mentions:
        if uid:
            for m in members:
                if m.get("id") == uid:
                    resolved.append(m)
                    break
        elif uname:
            uname_lower = uname.lower()
            for m in members:
                profile = m.get("profile", {})
                candidates = {
                    (profile.get("display_name") or "").lower(),
                    (profile.get("real_name") or "").lower(),
                    (m.get("name") or "").lower(),
                }
                if uname_lower in candidates:
                    resolved.append(m)
                    break

    return resolved


def member_email(member: dict):
    return member.get("profile", {}).get("email")


def member_display_name(member: dict):
    profile = member.get("profile", {})
    return (
        profile.get("display_name")
        or profile.get("real_name")
        or member.get("name")
        or "Guest"
    )


# ─────────────────────────────────────────────────────────────────
# Slash Command
# ─────────────────────────────────────────────────────────────────

@router.post("/meet")
async def meet(request: Request, background_tasks: BackgroundTasks):
    try:
        form = await request.form()

        user_id = form.get("user_id")
        team_id = form.get("team_id")
        channel_id = form.get("channel_id")
        text = (form.get("text") or "").strip()
        response_url = form.get("response_url")

        mentions = extract_mentions(text)
        uid = mentions[0][0] if mentions else None
        uname = mentions[0][1] if mentions else None

        print(f"DEBUG /meet user={user_id} channel={channel_id} text={text}")

        # ── Instant keywords ──
        instant_keywords = [
            "lets connect", "let's connect", "connect", "call",
            "lets meet", "let's meet", "meet", "instant meeting",
            "lets go", "let's go", "talk", "lets talk", "let's talk",
        ]
        normalized_text = text.lower()
        should_start_instant = any(k in normalized_text for k in instant_keywords)

        if should_start_instant:
            background_tasks.add_task(
                handle_instant_meet,
                user_id,
                team_id,
                channel_id,
                mentions,       # ← full mentions list
                response_url,
            )
            return JSONResponse({
                "response_type": "ephemeral",
                "text": "🚀 Starting instant meeting..."
            })

        # ── Show Connect Now / Schedule Later buttons ──
        shared_session_id = str(uuid.uuid4())   # ← defined BEFORE use

        shared_value = json.dumps({
            "session_id": shared_session_id,
            "user_id": user_id,
            "team_id": team_id,
            "channel_id": channel_id,
            "uid": uid,
            "uname": uname,
            "mentions": mentions,       # ← all mentions saved
            "response_url": response_url,
        })

        return JSONResponse({
            "response_type": "ephemeral",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"🤝 Meeting with <@{uid}>"
                            if uid
                            else "🤝 Start a meeting"
                        )
                    }
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "style": "primary",
                            "text": {"type": "plain_text", "text": "⚡ Connect Now"},
                            "action_id": "connect_now",
                            "value": shared_value
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "📅 Schedule Later"},
                            "action_id": "schedule_meeting",
                            "value": shared_value
                        }
                    ]
                }
            ]
        })

    except Exception as e:
        print("🔥 /meet ERROR:", str(e))
        return JSONResponse({"response_type": "ephemeral", "text": "Something went wrong."})


# ─────────────────────────────────────────────────────────────────
# Cancel Meeting Handler
# ─────────────────────────────────────────────────────────────────

async def handle_cancel_meeting(event_id: str, user_id: str, team_id: str):
    db = SessionLocal()
    try:
        record = db.query(MeetingRecord).filter(
            MeetingRecord.event_id == event_id
        ).first()

        if not record:
            print(f"⚠️ Meeting {event_id} already cancelled")
            return

        # ── Replace meeting card with cancelled notice ──
        if record.response_url:
            payload = {
                "replace_original": "true",
                "text": "🗑 Meeting cancelled.",
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "🗑 *Meeting cancelled.*"
                        }
                    }
                ]
            }
            async with httpx.AsyncClient() as client:
                resp = await client.post(record.response_url, json=payload, timeout=20)
            print(f"DEBUG replace_original: {resp.status_code} {resp.text}")

        # ── Cancel Google Calendar event ──
        organiser = get_db_user(db, user_id)
        if organiser and record.calendar_event_id:
            cancel_calendar_event(organiser, record.calendar_event_id)

        db.delete(record)
        db.commit()
        print(f"✅ Meeting cancelled: {event_id}")

    except Exception as e:
        print(f"🔥 Cancel Meeting ERROR: {e}")
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────
# Actions Route
# ─────────────────────────────────────────────────────────────────

@router.post("/actions")
async def slack_actions(request: Request, background_tasks: BackgroundTasks):
    try:
        form = await request.form()
        payload = json.loads(form.get("payload", "{}"))
        payload_type = payload.get("type")

        if payload_type == "block_actions":
            action = payload["actions"][0]
            value = json.loads(action.get("value", "{}"))
            session_id = value.get("session_id")
            action_id = action.get("action_id")

            # ── Dead session check ──
            if (
                action_id in ("connect_now", "schedule_meeting")
                and session_id in _used_sessions
            ):
                return JSONResponse({
                    "response_type": "ephemeral",
                    "replace_original": True,
                    "blocks": [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": (
                                    "⚠️ *These buttons have expired.*\n\n"
                                    "Use `/meet @user` again to start a new meeting."
                                )
                            }
                        }
                    ]
                })

            # ── Connect Now ──
            if action_id == "connect_now":
                _used_sessions.add(session_id)
                background_tasks.add_task(
                    handle_instant_meet,
                    value["user_id"],
                    value["team_id"],
                    value["channel_id"],
                    value.get("mentions", []),  # ← full mentions list
                    value["response_url"],
                )
                return JSONResponse({
                    "response_type": "ephemeral",
                    "replace_original": True,
                    "blocks": [
                        {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": "🚀 Starting instant meeting..."}
                        }
                    ]
                })

            # ── Schedule Meeting ──
            if action_id == "schedule_meeting":
                _used_sessions.add(session_id)
                await open_schedule_modal(
                    trigger_id=payload["trigger_id"],
                    metadata=value,
                    team_id=value["team_id"],
                )
                return JSONResponse({
                    "response_type": "ephemeral",
                    "replace_original": True,
                    "blocks": [
                        {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": "📅 Opening schedule form..."}
                        }
                    ]
                })

            # ── Cancel Meeting ──
            if action_id == "cancel_meeting":
                event_id = value.get("event_id")
                if not event_id:
                    return JSONResponse({})

                background_tasks.add_task(
                    handle_cancel_meeting,
                    event_id,
                    value["user_id"],
                    value["team_id"],
                )
                return JSONResponse({})

        # ── Modal Submit ──
        if payload_type == "view_submission":
            view = payload.get("view", {})
            if view.get("callback_id") == "schedule_modal":
                metadata = json.loads(view["private_metadata"])
                values = view["state"]["values"]

                title = values["title_block"]["title_input"]["value"]
                date = values["date_block"]["date_input"]["selected_date"]
                time = values["time_block"]["time_input"]["selected_time"]
                duration = int(
                    values["duration_block"]["duration_input"]["selected_option"]["value"]
                )

                background_tasks.add_task(
                    handle_scheduled_meeting,
                    metadata,
                    title,
                    date,
                    time,
                    duration,
                )
                return JSONResponse({"response_action": "clear"})

        return JSONResponse({})

    except Exception as e:
        print("🔥 /actions ERROR:", str(e))
        return JSONResponse({"response_type": "ephemeral", "text": "Something went wrong."})


# ─────────────────────────────────────────────────────────────────
# Instant Meet Handler
# ─────────────────────────────────────────────────────────────────

async def handle_instant_meet(
    user_id: str,
    team_id: str,
    channel_id: str,
    mentions: list,
    response_url: str,
):
    db = SessionLocal()
    try:
        bot_token = get_token(team_id)

        # ── Resolve ALL mentioned users ──
        invited_members = await resolve_invited_users(bot_token, mentions)
        invited_emails = [member_email(m) for m in invited_members if member_email(m)]
        invited_names = [member_display_name(m) for m in invited_members] or ["Guest"]
        names_display = ", ".join(invited_names)

        organiser = get_db_user(db, user_id)

        if not organiser:
            auth_url = f"{BASE_URL}/auth?user_id={user_id}&team_id={team_id}"
            await respond_to_user(
                response_url=response_url,
                text="Google connection required",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                "🔐 *Connect your Google account*\n\n"
                                "MeetNow needs Google Calendar access to create meetings."
                            )
                        }
                    },
                    {
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "style": "primary",
                                "text": {"type": "plain_text", "text": "🔗 Connect Google"},
                                "url": auth_url
                            }
                        ]
                    }
                ]
            )
            return

        # ── Create Google Meet ──
        meet_link, cal_event_id = create_meeting(
            organiser,
            attendee_emails=invited_emails if invited_emails else None
        )

        if not meet_link:
            await respond_in_channel(response_url, "❌ Failed to create meeting.")
            return

        event_id = str(uuid.uuid4())

        record = MeetingRecord(
            event_id=event_id,
            user_id=user_id,
            team_id=team_id,
            title="Instant Meeting",
            meet_link=meet_link,
            start_time="now",
            calendar_event_id=cal_event_id,
            channel_id=channel_id,
            response_url=response_url,  # ← saved at creation
        )
        db.add(record)
        db.commit()

        action_value = json.dumps({
            "event_id": event_id,
            "user_id": user_id,
            "team_id": team_id,
            "channel_id": channel_id,
        })

        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"🚀 *Meeting Ready!*\n\n"
                        f"👤 Started by <@{user_id}>\n"
                        f"🤝 With: {names_display}\n"
                        f"📞 {meet_link}"
                    )
                }
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "style": "danger",
                        "text": {"type": "plain_text", "text": "🗑 Cancel Meeting"},
                        "action_id": "cancel_meeting",
                        "value": action_value
                    }
                ]
            }
        ]

        await post_meeting_message(response_url, f"Meeting ready: {meet_link}", blocks)
         # ── Explicitly save response_url after posting ──
        record.response_url = response_url
        db.commit()

    except Exception as e:
        print(f"🔥 handle_instant_meet ERROR: {e}")
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────
# Schedule Modal
# ─────────────────────────────────────────────────────────────────

async def open_schedule_modal(trigger_id: str, metadata: dict, team_id: str):
    bot_token = get_token(team_id)
    modal = {
        "trigger_id": trigger_id,
        "view": {
            "type": "modal",
            "callback_id": "schedule_modal",
            "private_metadata": json.dumps(metadata),
            "title": {"type": "plain_text", "text": "Schedule Meeting"},
            "submit": {"type": "plain_text", "text": "Create"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "title_block",
                    "label": {"type": "plain_text", "text": "Meeting Title"},
                    "element": {"type": "plain_text_input", "action_id": "title_input"}
                },
                {
                    "type": "input",
                    "block_id": "date_block",
                    "label": {"type": "plain_text", "text": "Date"},
                    "element": {"type": "datepicker", "action_id": "date_input"}
                },
                {
                    "type": "input",
                    "block_id": "time_block",
                    "label": {"type": "plain_text", "text": "Time"},
                    "element": {"type": "timepicker", "action_id": "time_input"}
                },
                {
                    "type": "input",
                    "block_id": "duration_block",
                    "label": {"type": "plain_text", "text": "Duration"},
                    "element": {
                        "type": "static_select",
                        "action_id": "duration_input",
                        "options": [
                            {"text": {"type": "plain_text", "text": "30 Minutes"}, "value": "30"},
                            {"text": {"type": "plain_text", "text": "1 Hour"}, "value": "60"}
                        ]
                    }
                }
            ]
        }
    }
    result = await slack_api(bot_token, "views.open", modal)
    print("DEBUG modal:", result)


# ─────────────────────────────────────────────────────────────────
# Scheduled Meeting Handler
# ─────────────────────────────────────────────────────────────────

async def handle_scheduled_meeting(
    metadata: dict,
    title: str,
    date: str,
    time: str,
    duration: int,
):
    db = SessionLocal()
    try:
        user_id = metadata["user_id"]
        team_id = metadata["team_id"]
        channel_id = metadata["channel_id"]
        response_url = metadata["response_url"]
        mentions = metadata.get("mentions", [])  # ← get all mentions

        organiser = get_db_user(db, user_id)
        if not organiser:
            return

        bot_token = get_token(team_id)

        # ── Resolve ALL mentioned users ──
        invited_members = await resolve_invited_users(bot_token, mentions)
        invited_emails = [member_email(m) for m in invited_members if member_email(m)]
        invited_names = [member_display_name(m) for m in invited_members] or ["Guest"]
        names_display = ", ".join(invited_names)

        meet_link, calendar_event_id = create_scheduled_meeting(
            organiser,
            title,
            date,
            time,
            duration,
            attendee_emails=invited_emails if invited_emails else None  # ← send invites
        )

        if not meet_link:
            return

        event_id = str(uuid.uuid4())

        record = MeetingRecord(
            event_id=event_id,
            user_id=user_id,
            team_id=team_id,
            title=title,
            meet_link=meet_link,
            start_time=f"{date} {time}",
            calendar_event_id=calendar_event_id,
            channel_id=channel_id,
            response_url=response_url,  # ← saved at creation
        )
        db.add(record)
        db.commit()

        action_value = json.dumps({
            "event_id": event_id,
            "user_id": user_id,
            "team_id": team_id,
            "channel_id": channel_id,
        })

        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"📅 *Meeting Scheduled*\n\n"
                        f"📌 {title}\n"
                        f"🗓 {date}\n"
                        f"⏰ {time}\n"
                        f"🤝 With: {names_display}\n"
                        f"📞 {meet_link}"
                    )
                }
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "style": "danger",
                        "text": {"type": "plain_text", "text": "🗑 Cancel Meeting"},
                        "action_id": "cancel_meeting",
                        "value": action_value
                    }
                ]
            }
        ]

        await post_meeting_message(response_url, f"Meeting scheduled: {meet_link}", blocks)
        # ── Explicitly save response_url after posting ──
        record.response_url = response_url
        db.commit()

    except Exception as e:
        print(f"🔥 Scheduled Meeting ERROR: {e}")
    finally:
        db.close()