import json
import re
import uuid
import httpx

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

def get_db_user(
    db: Session,
    user_id: str
):
    return db.query(UserToken).filter(
        UserToken.user_id == user_id
    ).first()


def get_token(team_id: str):

    db = SessionLocal()

    try:

        install = db.query(
            WorkspaceInstall
        ).filter(
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

async def slack_api(
    bot_token: str,
    method: str,
    payload: dict
):

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

        print(
            f"🔥 Slack API Error "
            f"{method}: "
            f"{data}"
        )

    return data


# ─────────────────────────────────────────────────────────────────
# Public Response
# ─────────────────────────────────────────────────────────────────

async def respond_in_channel(
    response_url: str,
    text: str,
    blocks: list = None
):

    payload = {
        "response_type": "in_channel",
        "text": text,
    }

    if blocks:
        payload["blocks"] = blocks

    async with httpx.AsyncClient() as client:

        response = await client.post(
            response_url,
            json=payload,
            timeout=20,
        )

    print(
        "DEBUG response_url:",
        response.status_code
    )

    try:
        return response.json()
    except Exception:
        return {}


# ─────────────────────────────────────────────────────────────────
# Private Response
# ─────────────────────────────────────────────────────────────────

async def respond_to_user(
    response_url: str,
    text: str,
    blocks: list = None
):

    payload = {
        "response_type": "ephemeral",
        "text": text,
    }

    if blocks:
        payload["blocks"] = blocks

    async with httpx.AsyncClient() as client:

        response = await client.post(
            response_url,
            json=payload,
            timeout=20,
        )

    print(
        "DEBUG ephemeral:",
        response.status_code
    )


# ─────────────────────────────────────────────────────────────────
# User Cache
# ─────────────────────────────────────────────────────────────────

_members_cache = []
_used_sessions = set()


async def get_all_members(
    bot_token: str
):

    global _members_cache

    if _members_cache:
        return _members_cache

    result = await slack_api(
        bot_token,
        "users.list",
        {}
    )

    _members_cache = [
        m for m in result.get("members", [])
        if not m.get("deleted")
        and not m.get("is_bot")
        and m.get("id") != "USLACKBOT"
    ]

    return _members_cache


# ─────────────────────────────────────────────────────────────────
# Mention Parsing
# ─────────────────────────────────────────────────────────────────

def extract_mention(text: str):

    m = re.search(
        r"<@([A-Z0-9]+)(?:\|([^>]*)?)?>",
        text
    )

    if m:
        return m.group(1), (
            m.group(2) or ""
        ).lower()

    m = re.search(r"@([\w.]+)", text)

    if m:
        return None, m.group(1).lower()

    return None, None


async def resolve_invited_user(
    bot_token: str,
    uid: str | None,
    uname: str | None
):

    members = await get_all_members(
        bot_token
    )

    if uid:

        for m in members:

            if m.get("id") == uid:
                return m

        return None

    if not uname:
        return None

    uname = uname.lower()

    for m in members:

        profile = m.get("profile", {})

        candidates = {

            (
                profile.get("display_name") or ""
            ).lower(),

            (
                profile.get("real_name") or ""
            ).lower(),

            (
                m.get("name") or ""
            ).lower(),
        }

        if uname in candidates:
            return m

    return None


def member_email(member: dict):

    return (
        member.get("profile", {})
        .get("email")
    )


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
async def meet(
    request: Request,
    background_tasks: BackgroundTasks
):

    try:

        form = await request.form()

        user_id = form.get("user_id")
        team_id = form.get("team_id")
        channel_id = form.get("channel_id")

        text = (
            form.get("text") or ""
        ).strip()

        response_url = form.get(
            "response_url"
        )

        uid, uname = extract_mention(
            text
        )

        # ---------------------------------------------------------
        # Smart Instant Meeting Keywords
        # ---------------------------------------------------------

        instant_keywords = [
            "lets connect",
            "let's connect",
            "connect",
            "call",
            "lets meet",
            "let's meet",
            "meet",
            "instant meeting",
            "lets go",
            "let's go",
            "talk",
            "lets talk",
            "let's talk",
        ]

        normalized_text = text.lower()

        should_start_instant = any(
            keyword in normalized_text
            for keyword in instant_keywords
        )

        print(
            f"DEBUG /meet "
            f"user={user_id} "
            f"channel={channel_id} "
            f"text={text}"
        )

        # ---------------------------------------------------------
        # AUTO START INSTANT MEETING
        # ---------------------------------------------------------

        if should_start_instant:
            background_tasks.add_task(
                handle_instant_meet,
                user_id,
                team_id,
                channel_id,
                uid,
                uname,
                response_url,
            )

            return JSONResponse({
                "response_type": "ephemeral",
                "text": "🚀 Starting instant meeting..."
            })

        shared_session_id = str(uuid.uuid4())

        shared_value = json.dumps({
            "session_id": shared_session_id,
            "user_id": user_id,
            "team_id": team_id,
            "channel_id": channel_id,
            "uid": uid,
            "uname": uname,
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
                        )
                    }
                },

                {
                    "type": "actions",

                    "elements": [

                        {
                            "type": "button",

                            "style": "primary",

                            "text": {
                                "type": "plain_text",
                                "text": "⚡ Connect Now"
                            },

                            "action_id": "connect_now",

                            "value": shared_value
                        },

                        {
                            "type": "button",

                            "text": {
                                "type": "plain_text",
                                "text": "📅 Schedule Later"
                            },

                            "action_id": "schedule_meeting",

                            "value": shared_value
                        }
                    ]
                }
            ]
        })

    except Exception as e:

        print(
            "🔥 /meet ERROR:",
            str(e)
        )

        return JSONResponse({
            "response_type": "ephemeral",
            "text": "Something went wrong."
        })


# ─────────────────────────────────────────────────────────────────
# Cancel Meeting Handler
# ─────────────────────────────────────────────────────────────────

async def handle_cancel_meeting(
    event_id: str,
    user_id: str,
):

    db = SessionLocal()

    try:

        record = db.query(
            MeetingRecord
        ).filter(
            MeetingRecord.event_id == event_id
        ).first()

        if not record:
            return

        organiser = get_db_user(
            db,
            user_id
        )

        if (
            organiser
            and record.calendar_event_id
        ):

            cancel_calendar_event(
                organiser,
                record.calendar_event_id
            )

        db.delete(record)

        db.commit()

        print(
            f"✅ Meeting cancelled: {event_id}"
        )

    except Exception as e:

        print(
            "🔥 Cancel Meeting ERROR:",
            str(e)
        )

    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────
# Actions Route
# ─────────────────────────────────────────────────────────────────

@router.post("/actions")
async def slack_actions(
    request: Request,
    background_tasks: BackgroundTasks
):
    try:
        form = await request.form()

        payload = json.loads(
            form.get("payload", "{}")
        )

        payload_type = payload.get("type")

        # =========================================================
        # BUTTON ACTIONS
        # =========================================================

        if payload_type == "block_actions":
            action = payload["actions"][0]
            value = json.loads(
                action.get("value", "{}")
            )
            session_id = value.get(
                "session_id"
            )
            action_id = action.get("action_id")

            # ---------------------------------------------------------
            # DEAD SESSION CHECK
            # ---------------------------------------------------------
            if session_id in _used_sessions:
                return JSONResponse({
                    "response_type": "ephemeral",
                    "text": (
                        "⚠️ This meeting session has expired.\n\n"
                        "Use `/meet` again to start a new meeting."
                    )
                })

            # -----------------------------------------------------
            # CONNECT NOW
            # -----------------------------------------------------
            if action_id == "connect_now":
                _used_sessions.add(session_id)

                background_tasks.add_task(
                    handle_instant_meet,
                    value["user_id"],
                    value["team_id"],
                    value["channel_id"],
                    value["uid"],
                    value["uname"],
                    value["response_url"],
                )

                return JSONResponse({})

            # -----------------------------------------------------
            # SCHEDULE MEETING
            # -----------------------------------------------------
            if action_id == "schedule_meeting":
                _used_sessions.add(session_id)

                await open_schedule_modal(
                    trigger_id=payload["trigger_id"],
                    metadata=value,
                    team_id=value["team_id"],
                )

                return JSONResponse({})

            # -----------------------------------------------------
            # CANCEL MEETING
            # -----------------------------------------------------
            if action_id == "cancel_meeting":

                db = SessionLocal()

                record = db.query(MeetingRecord).filter(
                    MeetingRecord.event_id == value["event_id"]
                ).first()

                if record:
                    bot_token = get_token(value["team_id"])

                    # Step 1: Delete the original meeting message
                    if record.channel_id and record.slack_message_ts:
                        await slack_api(
                            bot_token,
                            "chat.delete",
                            {
                                "channel": record.channel_id,
                                "ts": record.slack_message_ts,
                            }
                        )

                    reconnect_session_id = str(uuid.uuid4())
                    stop_session_id = str(uuid.uuid4())

                    reconnect_value = json.dumps({
                        "session_id": reconnect_session_id,
                        "user_id": value["user_id"],
                        "team_id": value["team_id"],
                        "channel_id": record.channel_id,
                        "uid": value.get("uid"),
                        "uname": value.get("uname"),
                        "response_url": value["response_url"],
                    })

                    # Step 2: Post "what next" as ephemeral — no chat history
                    await slack_api(
                        bot_token,
                        "chat.postEphemeral",
                        {
                            "channel": record.channel_id,
                            "user": value["user_id"],
                            "text": "Meeting Cancelled",
                            "blocks": [
                                {
                                    "type": "section",
                                    "text": {
                                        "type": "mrkdwn",
                                        "text": (
                                            "❌ *Meeting Cancelled*\n\n"
                                            "The meeting and calendar event "
                                            "were cancelled successfully.\n\n"
                                            "*What would you like to do next?*"
                                        )
                                    }
                                },
                                {
                                    "type": "actions",
                                    "elements": [
                                        {
                                            "type": "button",
                                            "style": "primary",
                                            "text": {
                                                "type": "plain_text",
                                                "text": "⚡ Connect Now"
                                            },
                                            "action_id": "connect_now",
                                            "value": reconnect_value
                                        },
                                        {
                                            "type": "button",
                                            "text": {
                                                "type": "plain_text",
                                                "text": "📅 Schedule Later"
                                            },
                                            "action_id": "schedule_meeting",
                                            "value": reconnect_value
                                        },
                                        {
                                            "type": "button",
                                            "style": "danger",
                                            "text": {
                                                "type": "plain_text",
                                                "text": "🛑 Stop"
                                            },
                                            "action_id": "stop_meeting_flow",
                                            "value": json.dumps({
                                                "session_id": stop_session_id,
                                            })
                                        }
                                    ]
                                }
                            ]
                        }
                    )

                    background_tasks.add_task(
                        handle_cancel_meeting,
                        value["event_id"],
                        value["user_id"],
                    )

                db.close()

                return JSONResponse({})

            # -----------------------------------------------------
            # STOP FLOW
            # -----------------------------------------------------
            if action_id == "stop_meeting_flow":
                return JSONResponse({
                    "replace_original": True,
                    "text": "✅ Done.",
                    "blocks": [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": "✅ Done. Use `/meet` anytime to start a new meeting."
                            }
                        }
                    ]
                })

        # =========================================================
        # MODAL SUBMIT
        # =========================================================
        if payload_type == "view_submission":
            view = payload.get("view", {})
            if view.get("callback_id") == "schedule_modal":
                metadata = json.loads(
                    view["private_metadata"]
                )

                values = view["state"]["values"]

                title = values[
                    "title_block"
                ]["title_input"]["value"]

                date = values[
                    "date_block"
                ]["date_input"]["selected_date"]

                time = values[
                    "time_block"
                ]["time_input"]["selected_time"]

                duration = int(
                    values[
                        "duration_block"
                    ]["duration_input"][
                        "selected_option"
                    ]["value"]
                )

                background_tasks.add_task(
                    handle_scheduled_meeting,
                    metadata,
                    title,
                    date,
                    time,
                    duration,
                )

                return JSONResponse({
                    "response_action": "clear"
                })

        return JSONResponse({})

    except Exception as e:
        print(
            "🔥 /actions ERROR:",
            str(e)
        )

        return JSONResponse({
            "response_type": "ephemeral",
            "text": "Something went wrong."
        })


# ─────────────────────────────────────────────────────────────────
# Main Meeting Logic
# ─────────────────────────────────────────────────────────────────

async def handle_instant_meet(
    user_id: str,
    team_id: str,
    channel_id: str,
    uid: str | None,
    uname: str | None,
    response_url: str,
):

    db = SessionLocal()

    try:

        bot_token = get_token(team_id)

        invited_member = await resolve_invited_user(
            bot_token,
            uid,
            uname,
        )

        invited_email = (
            member_email(invited_member)
            if invited_member
            else None
        )

        invited_name = (
            member_display_name(invited_member)
            if invited_member
            else "Guest"
        )

        organiser = get_db_user(
            db,
            user_id
        )

        # ─────────────────────────────────────────────────────────
        # Google Not Connected
        # ─────────────────────────────────────────────────────────

        if not organiser:

            auth_url = (
                f"{BASE_URL}/auth"
                f"?user_id={user_id}"
                f"&team_id={team_id}"
            )

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
                                "MeetNow needs Google Calendar access "
                                "to create meetings."
                            )
                        }
                    },

                    {
                        "type": "actions",
                        "elements": [

                            {
                                "type": "button",
                                "style": "primary",

                                "text": {
                                    "type": "plain_text",
                                    "text": "🔗 Connect Google"
                                },

                                "url": auth_url
                            }
                        ]
                    }
                ]
            )

            return

        # ─────────────────────────────────────────────────────────
        # Create Google Meet
        # ─────────────────────────────────────────────────────────

        meet_link, cal_event_id = create_meeting(
            organiser,
            attendee_emails=[invited_email]
            if invited_email
            else None
        )

        if not meet_link:

            await respond_in_channel(
                response_url,
                "❌ Failed to create meeting."
            )

            return

        # ─────────────────────────────────────────────────────────
        # Save Meeting
        # ─────────────────────────────────────────────────────────

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
     )

        db.add(record)
        db.commit()

        action_value = json.dumps({
            "event_id": event_id,
            "user_id": user_id,
            "team_id": team_id,
            "channel_id": channel_id,
            "uid": uid,
            "uname": uname,
            "response_url": response_url,
        })

        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"🚀 *Meeting Ready!*\n\n"
                        f"👤 Started by <@{user_id}>\n"
                        f"🤝 With: {invited_name}\n"
                        f"📞 {meet_link}"
                    )
                }
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "style": "primary",
                        "text": {
                            "type": "plain_text",
                            "text": "📅 Schedule Later"
                        },
                        "action_id": "schedule_meeting",
                        "value": action_value
                    },
                    {
                        "type": "button",
                        "style": "danger",
                        "text": {
                            "type": "plain_text",
                            "text": "🗑 Cancel Meeting"
                        },
                        "action_id": "cancel_meeting",
                        "value": action_value
                    }
                ]
            }
        ]

        # Post ephemeral — only visible to the user, no chat history
        slack_response = await slack_api(
            bot_token,
            "chat.postEphemeral",
            {
                "channel": channel_id,
                "user": user_id,
                "text": f"Meeting ready: {meet_link}",
                "blocks": blocks,
            }
        )
        message_ts = slack_response.get("message_ts")
        if message_ts:
            record.slack_message_ts = message_ts
            db.commit()

    except Exception as e:

        print(
            "🔥 handle_instant_meet ERROR:",
            str(e)
        )

    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────
# Schedule Modal
# ─────────────────────────────────────────────────────────────────

async def open_schedule_modal(
    trigger_id: str,
    metadata: dict,
    team_id: str,
):

    bot_token = get_token(team_id)

    modal = {

        "trigger_id": trigger_id,

        "view": {

            "type": "modal",

            "callback_id": "schedule_modal",

            "private_metadata": json.dumps(metadata),

            "title": {
                "type": "plain_text",
                "text": "Schedule Meeting"
            },

            "submit": {
                "type": "plain_text",
                "text": "Create"
            },

            "blocks": [

                {
                    "type": "input",

                    "block_id": "title_block",

                    "label": {
                        "type": "plain_text",
                        "text": "Meeting Title"
                    },

                    "element": {
                        "type": "plain_text_input",
                        "action_id": "title_input"
                    }
                },

                {
                    "type": "input",

                    "block_id": "date_block",

                    "label": {
                        "type": "plain_text",
                        "text": "Date"
                    },

                    "element": {
                        "type": "datepicker",
                        "action_id": "date_input"
                    }
                },

                {
                    "type": "input",

                    "block_id": "time_block",

                    "label": {
                        "type": "plain_text",
                        "text": "Time"
                    },

                    "element": {
                        "type": "timepicker",
                        "action_id": "time_input"
                    }
                },

                {
                    "type": "input",

                    "block_id": "duration_block",

                    "label": {
                        "type": "plain_text",
                        "text": "Duration"
                    },

                    "element": {

                        "type": "static_select",

                        "action_id": "duration_input",

                        "options": [

                            {
                                "text": {
                                    "type": "plain_text",
                                    "text": "30 Minutes"
                                },
                                "value": "30"
                            },

                            {
                                "text": {
                                    "type": "plain_text",
                                    "text": "1 Hour"
                                },
                                "value": "60"
                            }
                        ]
                    }
                }
            ]
        }
    }

    result = await slack_api(
        bot_token,
        "views.open",
        modal
    )

    print(
        "DEBUG modal:",
        result
    )


# ─────────────────────────────────────────────────────────────────
# Scheduled Meeting
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

        response_url = metadata["response_url"]

        organiser = get_db_user(
            db,
            user_id
        )

        if not organiser:
            return

        meet_link, calendar_event_id = create_scheduled_meeting(
            organiser,
            title,
            date,
            time,
            duration,
        )

        if not meet_link:
            return

        event_id = str(uuid.uuid4())

        db.add(
            MeetingRecord(
                event_id=event_id,
                user_id=user_id,
                team_id=metadata["team_id"],
                title=title,
                meet_link=meet_link,
                start_time=f"{date} {time}",
                calendar_event_id=calendar_event_id,
            )
        )

        db.commit()

        action_value = json.dumps({
            "event_id": event_id,
            "user_id": metadata["user_id"],
            "team_id": metadata["team_id"],
            "channel_id": metadata["channel_id"],
            "uid": metadata.get("uid"),
            "uname": metadata.get("uname"),
            "response_url": metadata["response_url"],
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

                        "text": {
                            "type": "plain_text",
                            "text": "🗑 Cancel Meeting"
                        },

                        "action_id": "cancel_meeting",

                        "value": action_value
                    }
                ]
            }
        ]

        bot_token = get_token(metadata["team_id"])

        # Post ephemeral — only visible to the user, no chat history
        slack_response = await slack_api(
            bot_token,
            "chat.postEphemeral",
            {
                "channel": metadata["channel_id"],
                "user": metadata["user_id"],
                "text": f"Meeting scheduled: {meet_link}",
                "blocks": blocks,
            }
        )
        message_ts = slack_response.get("message_ts")
        if message_ts:
            record = db.query(MeetingRecord).filter(
                MeetingRecord.event_id == event_id
            ).first()
            if record:
                record.slack_message_ts = message_ts
                db.commit()

    except Exception as e:

        print(
            "🔥 Scheduled Meeting ERROR:",
            str(e)
        )

    finally:
        db.close()