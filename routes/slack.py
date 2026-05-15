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

        print(
            f"DEBUG /meet "
            f"user={user_id} "
            f"channel={channel_id} "
            f"text={text}"
        )

        return JSONResponse({

            "response_type": "ephemeral",

            "blocks": [

                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"🤝 Meeting with "
                            f"<@{uid}>"
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

                            "value": json.dumps({
                                "user_id": user_id,
                                "team_id": team_id,
                                "channel_id": channel_id,
                                "uid": uid,
                                "uname": uname,
                                "response_url": response_url,
                            })
                        },

                        {
                            "type": "button",

                            "text": {
                                "type": "plain_text",
                                "text": "📅 Schedule Later"
                            },

                            "action_id": "schedule_later",

                            "value": "todo"
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
# Actions Route
# ─────────────────────────────────────────────────────────────────

@router.post("/actions")
async def actions(
    request: Request,
    background_tasks: BackgroundTasks
):

    try:

        form = await request.form()

        payload = json.loads(
            form.get("payload", "{}")
        )

        actions_list = payload.get(
            "actions",
            []
        )

        if not actions_list:
            return JSONResponse({})

        action = actions_list[0]

        action_id = action.get(
            "action_id"
        )

        # ─────────────────────────────────────────────────────────
        # Connect Now
        # ─────────────────────────────────────────────────────────

        if action_id == "connect_now":

            value = json.loads(
                action.get("value")
            )

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

        # ─────────────────────────────────────────────────────────
        # Cancel Meeting
        # ─────────────────────────────────────────────────────────

        if action_id == "cancel_meeting":

            value = json.loads(
                action.get("value")
            )

            event_id = value.get(
                "event_id"
            )

            db = SessionLocal()

            try:

                record = db.query(
                    MeetingRecord
                ).filter(
                    MeetingRecord.event_id == event_id
                ).first()

                if record:

                    db.delete(record)
                    db.commit()

            finally:
                db.close()

            return JSONResponse({

                "replace_original": True,

                "text": "❌ Meeting cancelled"
            })

        return JSONResponse({})

    except Exception as e:

        print(
            "🔥 /actions ERROR:",
            str(e)
        )

        return JSONResponse({})


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

        db.add(
            MeetingRecord(
                event_id=event_id,
                user_id=user_id,
                team_id=team_id,
                title="Instant Meeting",
                meet_link=meet_link,
                start_time="now",
                calendar_event_id=cal_event_id,
            )
        )

        db.commit()

        cancel_value = json.dumps({
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
                        "style": "danger",

                        "text": {
                            "type": "plain_text",
                            "text": "🗑 Cancel Meeting"
                        },

                        "action_id": "cancel_meeting",

                        "value": cancel_value
                    }
                ]
            }
        ]

        await respond_in_channel(
            response_url,
            f"Meeting ready: {meet_link}",
            blocks
        )

    except Exception as e:

        print(
            "🔥 handle_instant_meet ERROR:",
            str(e)
        )

    finally:
        db.close()