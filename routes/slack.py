"""
routes/slack.py — MeetNow
─────────────────────────
New behavior:
• /meet posts meeting links back into the SAME Slack conversation
• Works in:
    - Channels
    - Private channels
    - Group DMs
    - Supported 1-on-1 DMs
• Google Calendar invite still gets sent automatically
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
from services.google import (
    create_meeting,
    create_scheduled_meeting,
    cancel_calendar_event,
)

router = APIRouter()

# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────


def get_db_user(db: Session, user_id: str):
    return db.query(UserToken).filter(
        UserToken.user_id == user_id
    ).first()


def get_token(team_id: str) -> str | None:
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


async def slack_api(
    bot_token: str,
    method: str,
    payload: dict
) -> dict:

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://slack.com/api/{method}",
            headers={
                "Authorization": f"Bearer {bot_token}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=15,
        )

    data = resp.json()

    if not data.get("ok"):
        print(
            f"🔥 Slack {method} error: "
            f"{data.get('error')} | payload={payload}"
        )

    return data


async def post_to_channel(
    bot_token: str,
    channel_id: str,
    text: str,
    blocks: list = None,
):
    """
    Posts directly into the SAME Slack conversation
    where the slash command was used.
    """

    payload = {
        "channel": channel_id,
        "text": text,
    }

    if blocks:
        payload["blocks"] = blocks

    result = await slack_api(
        bot_token,
        "chat.postMessage",
        payload,
    )

    if not result.get("ok"):
        print(f"🔥 Failed posting to {channel_id}: {result}")


async def respond(response_url: str, payload: dict):
    """
    Respond to slash command
    """

    async with httpx.AsyncClient() as client:
        r = await client.post(
            response_url,
            json=payload,
            timeout=10,
        )

        print(f"DEBUG respond {r.status_code}")


# ─────────────────────────────────────────────────────────────────
# User Lookup
# ─────────────────────────────────────────────────────────────────

_members_cache: list = []


async def _get_all_members(bot_token: str) -> list:
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


def extract_mention(text: str):
    """
    Extract Slack mention from slash command
    """

    m = re.search(r"<@([A-Z0-9]+)(?:\|([^>]*)?)?>", text)

    if m:
        return m.group(1), (m.group(2) or "").lower()

    m = re.search(r"@([\w.]+)", text)

    if m:
        return None, m.group(1).lower()

    return None, None


async def resolve_invited_user(
    bot_token: str,
    uid: str | None,
    uname: str | None
):

    members = await _get_all_members(bot_token)

    if uid:
        for m in members:
            if m.get("id") == uid:
                return m

        return None

    if not uname:
        return None

    uname_lower = uname.lower()

    for m in members:

        p = m.get("profile", {})

        candidates = {
            (p.get("display_name") or "").lower(),
            (p.get("real_name") or "").lower(),
            (m.get("name") or "").lower(),
            (p.get("display_name_normalized") or "").lower(),
            (p.get("real_name_normalized") or "").lower(),
        }

        if uname_lower in candidates:
            return m

    return None


def member_email(member: dict) -> str | None:
    return member.get("profile", {}).get("email")


def member_display_name(member: dict) -> str:
    p = member.get("profile", {})

    return (
        p.get("display_name")
        or p.get("real_name")
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
        text = (form.get("text") or "").strip()
        response_url = form.get("response_url")
        trigger_id = form.get("trigger_id")

        uid, uname = extract_mention(text)

        print(
            f"DEBUG /meet "
            f"user={user_id} "
            f"channel={channel_id} "
            f"text={text}"
        )

        if not user_id or not response_url:
            return JSONResponse({
                "response_type": "ephemeral",
                "text": "Missing required fields."
            })

        background_tasks.add_task(
            handle_meet,
            user_id,
            team_id,
            channel_id,
            text,
            uid,
            uname,
            response_url,
            trigger_id,
        )

        return JSONResponse({
            "response_type": "ephemeral",
            "text": ""
        })

    except Exception as e:
        print("🔥 /meet ERROR:", str(e))

        return JSONResponse({
            "response_type": "ephemeral",
            "text": "Something went wrong."
        })


# ─────────────────────────────────────────────────────────────────
# Main Meet Logic
# ─────────────────────────────────────────────────────────────────

async def handle_meet(
    user_id: str,
    team_id: str,
    channel_id: str,
    text: str,
    uid: str | None,
    uname: str | None,
    response_url: str,
    trigger_id: str,
):

    google_auth_url = (
        f"{BASE_URL}/auth"
        f"?user_id={user_id}"
        f"&team_id={team_id}"
    )

    try:
        bot_token = get_token(team_id)

        if not bot_token:
            await respond(response_url, {
                "replace_original": True,
                "response_type": "ephemeral",
                "text": "⚠️ MeetNow is not installed."
            })
            return

        invited_member = await resolve_invited_user(
            bot_token,
            uid,
            uname,
        )

        db = SessionLocal()

        try:
            organiser = get_db_user(db, user_id)

        finally:
            db.close()

        if not organiser:
            await respond(response_url, {
                "replace_original": True,
                "response_type": "ephemeral",
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text":
                                "🔗 Connect Google to use MeetNow"
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
                                    "text": "Connect Google"
                                },
                                "url": google_auth_url
                            }
                        ]
                    }
                ]
            })

            return

        text_lower = text.lower()

        mention_stripped = re.sub(
            r"<@[A-Z0-9]+(?:\|[^>]*)?>",
            "",
            text,
        ).strip()

        mention_stripped = re.sub(
            r"@[\w.]+",
            "",
            mention_stripped,
        ).strip()

        has_extra_text = bool(mention_stripped)

        if any(w in text_lower for w in [
            "schedule",
            "later",
            "plan"
        ]):
            await open_schedule_modal(
                trigger_id,
                user_id,
                team_id,
                channel_id,
                invited_member.get("id") if invited_member else None,
                bot_token,
            )
            return

        if (
            has_extra_text
            or any(w in text_lower for w in [
                "now",
                "instant",
                "connect"
            ])
        ):
            await handle_instant_meet(
                user_id,
                team_id,
                channel_id,
                invited_member,
                bot_token,
            )
            return

        session_id = str(uuid.uuid4())

        encoded = (
            f"{user_id}|"
            f"{team_id}|"
            f"{channel_id}|"
            f"{invited_member.get('id') if invited_member else ''}|"
            f"{session_id}"
        )

        await respond(response_url, {
            "replace_original": True,
            "response_type": "ephemeral",
            "blocks": _choice_blocks(encoded)
        })

    except Exception as e:
        print("🔥 handle_meet ERROR:", str(e))


def _choice_blocks(encoded_value: str):

    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text":
                    "*What would you like to do?*"
            }
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "⚡ Connect Now"
                    },
                    "action_id": "choice_instant",
                    "value": "instant|" + encoded_value,
                    "style": "primary"
                },
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "📅 Schedule Later"
                    },
                    "action_id": "choice_schedule",
                    "value": "schedule|" + encoded_value,
                }
            ]
        }
    ]


# ─────────────────────────────────────────────────────────────────
# Instant Meet
# ─────────────────────────────────────────────────────────────────

async def handle_instant_meet(
    user_id: str,
    team_id: str,
    channel_id: str,
    invited_member: dict | None,
    bot_token: str,
):

    db = SessionLocal()

    try:
        organiser = get_db_user(db, user_id)

        if not organiser:
            await post_to_channel(
                bot_token,
                channel_id,
                "⚠️ Please connect Google first."
            )
            return

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

        meet_link, cal_event_id = create_meeting(
            organiser,
            attendee_emails=[invited_email]
            if invited_email
            else None,
        )

        if not meet_link:
            await post_to_channel(
                bot_token,
                channel_id,
                "❌ Failed to create meeting."
            )
            return

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

        cancel_val = json.dumps({
            "event_id": event_id,
            "user_id": user_id,
            "team_id": team_id,
            "title": "Instant Meeting",
            "channel_id": channel_id,
        })

        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text":
                        f"🚀 *Meeting Ready!*\n\n"
                        f"👤 Started by <@{user_id}>\n"
                        f"🤝 With: {invited_name}\n"
                        f"📞 {meet_link}"
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
                        "value": cancel_val
                    }
                ]
            }
        ]

        await post_to_channel(
            bot_token,
            channel_id,
            f"Meeting ready: {meet_link}",
            blocks,
        )

    except Exception as e:
        print("🔥 handle_instant_meet ERROR:", str(e))

    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────
# Schedule Modal
# ─────────────────────────────────────────────────────────────────

async def open_schedule_modal(
    trigger_id: str,
    user_id: str,
    team_id: str,
    channel_id: str,
    invited_user_id: str | None,
    bot_token: str,
):

    meta = json.dumps({
        "user_id": user_id,
        "team_id": team_id,
        "channel_id": channel_id,
        "invited_user_id": invited_user_id or "",
    })

    modal = {
        "trigger_id": trigger_id,
        "view": {
            "type": "modal",
            "callback_id": "schedule_modal",
            "private_metadata": meta,
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
                }
            ]
        }
    }

    await slack_api(
        bot_token,
        "views.open",
        modal
    )