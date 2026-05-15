"""
routes/slack.py  — MeetNow
──────────────────────────
Privacy model (same as Calendly, Zoom bot, etc):
  • You type /meet @Pranjal in any context
  • MeetNow DMs YOU privately with the link + cancel button
  • MeetNow DMs PRANJAL privately with the invite link
  • Nothing is ever posted publicly
  • Google Calendar invite is emailed to Pranjal automatically
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
# Low-level helpers
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
            headers={
                "Authorization": f"Bearer {bot_token}",
                "Content-Type": "application/json"
            },
            json=payload,
            timeout=15,
        )
    data = resp.json()
    if not data.get("ok"):
        print(f"🔥 Slack {method} error: {data.get('error')} | keys: {list(payload.keys())}")
    return data


async def dm(bot_token: str, user_id: str, text: str, blocks: list = None):
    """
    Send a private DM to any user via the MeetNow bot.
    Always works — bot opens its own DM channel with the user.
    """
    result = await slack_api(bot_token, "conversations.open", {"users": user_id})
    channel = result.get("channel", {}).get("id")
    if not channel:
        print(f"🔥 Cannot open DM with {user_id}: {result.get('error')}")
        return
    payload = {"channel": channel, "text": text}
    if blocks:
        payload["blocks"] = blocks
    await slack_api(bot_token, "chat.postMessage", payload)


async def post_to_channel(bot_token: str, channel_id: str, text: str, blocks: list = None, fallback_user_id: str = None):
    """
    Post to a channel or DM intelligently:
    - Real channels (C...) → post directly
    - User-to-user DMs (D...) or anything else → use conversations.open via dm()
      so the bot always has access, exactly like the original working code.
    """
    # Slack channel IDs starting with C = public/private channel (bot can post if member)
    # D = direct message between users (bot cannot post directly, must use conversations.open)
    if channel_id and channel_id.startswith("C"):
        payload = {"channel": channel_id, "text": text}
        if blocks:
            payload["blocks"] = blocks
        result = await slack_api(bot_token, "chat.postMessage", payload)
        if not result.get("ok") and fallback_user_id:
            print(f"DEBUG channel post failed ({result.get('error')}), falling back to DM with {fallback_user_id}")
            await dm(bot_token, fallback_user_id, text, blocks)
    elif fallback_user_id:
        # DM context — use conversations.open so bot can always reach the user
        await dm(bot_token, fallback_user_id, text, blocks)


async def respond(response_url: str, payload: dict):
    """Ack back to slash command via response_url."""
    async with httpx.AsyncClient() as client:
        r = await client.post(response_url, json=payload, timeout=10)
        print(f"DEBUG respond {r.status_code}")


# ─────────────────────────────────────────────────────────────────
# User lookup helpers
# ─────────────────────────────────────────────────────────────────

# Cache the full members list so we don't call users.list on every request
_members_cache: list = []


async def _get_all_members(bot_token: str) -> list:
    global _members_cache
    if _members_cache:
        return _members_cache
    result = await slack_api(bot_token, "users.list", {})
    _members_cache = [
        m for m in result.get("members", [])
        if not m.get("deleted") and not m.get("is_bot") and m.get("id") != "USLACKBOT"
    ]
    return _members_cache


def extract_mention(text: str) -> tuple[str | None, str | None]:
    """
    Parse /meet text to find who was mentioned.
    Returns (slack_user_id, raw_name).
    Example: '/meet @Pranjal' → (None, 'Pranjal')
    Example: '/meet <@U123|pranjal>' → ('U123', 'pranjal')
    """
    m = re.search(r"<@([A-Z0-9]+)(?:\|([^>]*)?)?>", text)
    if m:
        return m.group(1), (m.group(2) or "").lower()
    m = re.search(r"@([\w.]+)", text)
    if m:
        return None, m.group(1).lower()
    return None, None


async def resolve_invited_user(bot_token: str, uid: str | None, uname: str | None) -> dict | None:
    """
    Returns the full Slack member object for the invited person.
    Includes id, name, real_name, display_name, email.
    """
    members = await _get_all_members(bot_token)

    if uid:
        for m in members:
            if m.get("id") == uid:
                return m
        print(f"🔥 uid {uid} not found in members list")
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

    print(f"🔥 Could not find member with name {uname!r}")
    return None


def member_email(member: dict) -> str | None:
    return member.get("profile", {}).get("email")


def member_display_name(member: dict) -> str:
    p = member.get("profile", {})
    return p.get("display_name") or p.get("real_name") or member.get("name") or "them"


async def get_my_name(bot_token: str, user_id: str) -> str:
    """Get the organiser's display name."""
    members = await _get_all_members(bot_token)
    for m in members:
        if m.get("id") == user_id:
            return member_display_name(m)
    return f"<@{user_id}>"


# ─────────────────────────────────────────────────────────────────
# /meet slash command
# ─────────────────────────────────────────────────────────────────

@router.post("/meet")
async def meet(request: Request, background_tasks: BackgroundTasks):
    try:
        form         = await request.form()
        user_id      = form.get("user_id")
        team_id      = form.get("team_id")
        channel_id   = form.get("channel_id")
        text         = (form.get("text") or "").strip()
        response_url = form.get("response_url")
        trigger_id   = form.get("trigger_id")

        uid, uname = extract_mention(text)
        print(f"DEBUG /meet user={user_id} team={team_id} text={text!r} uid={uid!r} uname={uname!r}")

        if not user_id or not response_url:
            return JSONResponse({"response_type": "ephemeral", "text": "Missing required fields."})

        background_tasks.add_task(
            handle_meet, user_id, team_id, channel_id, text, uid, uname, response_url, trigger_id
        )
        return JSONResponse({"response_type": "ephemeral", "text": "⏳ On it..."})

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
                "text": f"⚠️ MeetNow isn't set up. Install it: {BASE_URL}/slack/install"
            })
            return

        # Resolve who was @mentioned
        invited_member = await resolve_invited_user(bot_token, uid, uname)
        invited_user_id = invited_member.get("id") if invited_member else None
        print(f"DEBUG invited_user_id={invited_user_id!r} name={member_display_name(invited_member) if invited_member else None!r}")

        # Check organiser has Google connected
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
                     "text": "👋 *Connect your Google account to use MeetNow*\n"
                             "You only need to do this once."}},
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
                "text": "⏳ Creating your meeting..."
            })
            await handle_instant_meet(user_id, team_id, channel_id, invited_member, bot_token)
            return

        if any(w in text_lower for w in ["schedule", "later", "plan"]):
            await respond(response_url, {
                "replace_original": True, "response_type": "ephemeral",
                "text": "📅 Opening scheduler..."
            })
            await open_schedule_modal(trigger_id, user_id, team_id, channel_id, invited_user_id, bot_token)
            return

        # No keyword → show two single-use buttons
        session_id = str(uuid.uuid4())
        encoded = f"{user_id}|{team_id}|{channel_id}|{invited_user_id or ''}|{session_id}"
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
         "text": {"type": "mrkdwn", "text": "*What would you like to do?* _(pick one — buttons expire after use)_"}},
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
                values     = view["state"]["values"]
                title    = values["title_block"]["title_input"]["value"]
                date     = values["date_block"]["date_input"]["selected_date"]
                time     = values["time_block"]["time_input"]["selected_time"]
                duration = int(values["duration_block"]["duration_input"]["selected_option"]["value"])
                notes    = (values.get("notes_block", {}).get("notes_input", {}).get("value") or "")
                background_tasks.add_task(
                    handle_scheduled_meet,
                    meta["user_id"], meta["team_id"],
                    meta.get("channel_id") or "",
                    meta.get("invited_user_id") or None,
                    title, date, time, duration, notes
                )
                return JSONResponse({})

            if cb == "cancel_confirm_modal":
                meta = json.loads(view.get("private_metadata", "{}"))
                background_tasks.add_task(
                    handle_cancel_meeting,
                    meta["user_id"], meta["team_id"],
                    meta["event_id"], meta["slack_user_id"],
                    meta["title"], meta.get("channel_id", "")
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
            if len(parts) != 6:
                return JSONResponse({})
            _, user_id, team_id_val, channel_id, invited_uid, sid = parts
            invited_uid = invited_uid or None

            if sid in _used_sessions:
                await respond(response_url, {
                    "replace_original": True, "response_type": "ephemeral",
                    "text": "⚠️ These buttons already used. Type `/meet @user` again for new ones."
                })
                return JSONResponse({})

            _used_sessions.add(sid)

            if action_id == "choice_instant":
                await respond(response_url, {
                    "replace_original": True, "response_type": "ephemeral",
                    "text": "⏳ Creating your meeting..."
                })
                # Re-resolve invited member from stored user_id
                invited_member = None
                if invited_uid:
                    members = await _get_all_members(bot_token)
                    for m in members:
                        if m.get("id") == invited_uid:
                            invited_member = m
                            break
                background_tasks.add_task(
                    handle_instant_meet, user_id, team_id_val, channel_id, invited_member, bot_token
                )
            else:
                await respond(response_url, {
                    "replace_original": True, "response_type": "ephemeral",
                    "text": "📅 Opening scheduler..."
                })
                background_tasks.add_task(
                    open_schedule_modal, trigger_id, user_id, team_id_val, channel_id, invited_uid, bot_token
                )
            return JSONResponse({})

        if action_id == "cancel_meeting":
            ctx        = json.loads(value)
            slack_user = payload.get("user", {}).get("id")
            background_tasks.add_task(
                open_cancel_modal,
                trigger_id, ctx["event_id"], ctx["user_id"],
                ctx["team_id"], ctx["title"], slack_user,
                ctx.get("channel_id", ""), bot_token
            )
            return JSONResponse({})

        return JSONResponse({})

    except Exception as e:
        print("🔥 /actions ERROR:", str(e))
        return JSONResponse({})


# ─────────────────────────────────────────────────────────────────
# Core meeting flows — all private DMs
# ─────────────────────────────────────────────────────────────────

async def handle_instant_meet(
    user_id: str, team_id: str,
    channel_id: str,
    invited_member: dict | None,
    bot_token: str
):
    """
    Creates meeting, then:
    - DMs organiser: link + cancel button (private, only they see it)
    - DMs invited person: join link (private, only they see it)
    - Google Calendar email invite sent automatically
    """
    db = SessionLocal()
    try:
        organiser = get_db_user(db, user_id)
        if not organiser:
            await post_to_channel(bot_token, channel_id,
                     f"⚠️ Connect Google first: {BASE_URL}/auth?user_id={user_id}&team_id={team_id}",
                     fallback_user_id=user_id)
            return

        # Get invited person's email for Google Calendar invite
        invited_email = member_email(invited_member) if invited_member else None
        invited_id    = invited_member.get("id") if invited_member else None
        invited_name  = member_display_name(invited_member) if invited_member else None

        if invited_email:
            print(f"DEBUG calendar invite → {invited_email}")
        elif invited_member:
            print(f"🔥 No email for {invited_name} — calendar invite skipped")

        meet_link, cal_event_id = create_meeting(
            organiser,
            attendee_emails=[invited_email] if invited_email else None
        )
        if not meet_link:
            await post_to_channel(bot_token, channel_id,
                     "❌ Failed to create meeting. Check your Google Calendar access.",
                     fallback_user_id=user_id)
            return

        # Save for cancel
        event_id = str(uuid.uuid4())
        db.add(MeetingRecord(
            event_id=event_id, user_id=user_id, team_id=team_id,
            title="Instant Meeting", meet_link=meet_link,
            start_time="now", calendar_event_id=cal_event_id,
        ))
        db.commit()

        my_name    = await get_my_name(bot_token, user_id)
        cancel_val = json.dumps({
            "event_id": event_id, "user_id": user_id,
            "team_id": team_id, "title": "Instant Meeting",
            "channel_id": channel_id
        })
        cancel_btn = [{
            "type": "button", "style": "danger",
            "text": {"type": "plain_text", "text": "🗑 Cancel Meeting"},
            "action_id": "cancel_meeting", "value": cancel_val
        }]

        # ✅ Post to the channel/DM where /meet was typed
        await post_to_channel(bot_token, channel_id,
            text=f"✅ Meeting live: {meet_link}",
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn",
                 "text": f"✅ *Your meeting is live!*\n"
                         f"📞 {meet_link}\n"
                         + (f"_Invited {invited_name}_ — "
                            + ("calendar invite sent to their email ✉️"
                               if invited_email else "no email found, invite skipped")
                            if invited_member else "_No one invited_")}},
                {"type": "actions", "elements": cancel_btn}
            ],
            fallback_user_id=user_id)

        # Invited person sees the message in the same channel where /meet was typed

    except Exception as e:
        print("🔥 handle_instant_meet ERROR:", str(e))
        await post_to_channel(bot_token, channel_id, "❌ Something went wrong creating the meeting.", fallback_user_id=user_id)
    finally:
        db.close()


async def handle_scheduled_meet(
    user_id: str, team_id: str,
    channel_id: str,
    invited_user_id: str | None,
    title: str, date: str, time: str, duration: int, notes: str
):
    db = SessionLocal()
    bot_token = get_token(team_id)
    try:
        organiser = get_db_user(db, user_id)
        if not organiser:
            await post_to_channel(bot_token, channel_id,
                     f"⚠️ Connect Google first: {BASE_URL}/auth?user_id={user_id}&team_id={team_id}",
                     fallback_user_id=user_id)
            return

        # Look up invited person
        invited_member = None
        if invited_user_id:
            members = await _get_all_members(bot_token)
            for m in members:
                if m.get("id") == invited_user_id:
                    invited_member = m
                    break

        invited_email = member_email(invited_member) if invited_member else None
        invited_name  = member_display_name(invited_member) if invited_member else None

        meet_link, cal_event_id = create_scheduled_meeting(
            organiser, title, date, time, duration, notes,
            attendee_emails=[invited_email] if invited_email else None
        )
        if not meet_link:
            await post_to_channel(bot_token, channel_id, "❌ Failed to schedule meeting.", fallback_user_id=user_id)
            return

        event_id = str(uuid.uuid4())
        db.add(MeetingRecord(
            event_id=event_id, user_id=user_id, team_id=team_id,
            title=title, meet_link=meet_link,
            start_time=f"{date} {time}", calendar_event_id=cal_event_id,
        ))
        db.commit()

        my_name    = await get_my_name(bot_token, user_id)
        cancel_val = json.dumps({
            "event_id": event_id, "user_id": user_id,
            "team_id": team_id, "title": title,
            "channel_id": channel_id
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

        # ✅ Post to channel/DM where /meet was typed
        await post_to_channel(bot_token, channel_id,
            text=f"✅ Meeting scheduled: {meet_link}",
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn",
                 "text": f"✅ *Meeting scheduled!*\n{summary}\n"
                         + (f"_Invited {invited_name}_ — "
                            + ("calendar invite sent ✉️"
                               if invited_email else "no email found, invite skipped")
                            if invited_member else "")}},
                {"type": "actions", "elements": cancel_btn}
            ],
            fallback_user_id=user_id)

        # Invited person sees the message in the same channel where /meet was typed

    except Exception as e:
        print("🔥 handle_scheduled_meet ERROR:", str(e))
        if bot_token:
            await post_to_channel(bot_token, channel_id, "❌ Something went wrong scheduling the meeting.", fallback_user_id=user_id)
    finally:
        db.close()


async def open_schedule_modal(
    trigger_id: str, user_id: str, team_id: str,
    channel_id: str, invited_user_id: str | None, bot_token: str
):
    meta = json.dumps({
        "user_id": user_id, "team_id": team_id,
        "channel_id": channel_id,
        "invited_user_id": invited_user_id or ""
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
    title: str, slack_user_id: str, channel_id: str, bot_token: str
):
    meta = json.dumps({
        "event_id": event_id, "user_id": user_id,
        "team_id": team_id, "slack_user_id": slack_user_id,
        "title": title, "channel_id": channel_id
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
                         "This will delete it from Google Calendar."}}
            ]
        }
    }
    r = await slack_api(bot_token, "views.open", modal)
    if not r.get("ok"):
        print("🔥 Cancel modal failed:", r)


async def handle_cancel_meeting(
    user_id: str, team_id: str,
    event_id: str, slack_user_id: str, title: str, channel_id: str
):
    db = SessionLocal()
    bot_token = get_token(team_id)
    try:
        record = db.query(MeetingRecord).filter(MeetingRecord.event_id == event_id).first()
        if not record:
            await post_to_channel(bot_token, channel_id,
                     "⚠️ Meeting not found — it may already have been cancelled.",
                     fallback_user_id=slack_user_id)
            return

        organiser   = get_db_user(db, user_id)
        cal_deleted = cancel_calendar_event(organiser, record.calendar_event_id) if organiser else False
        db.delete(record)
        db.commit()

        msg = (f"✅ *{title}* cancelled and removed from Google Calendar."
               if cal_deleted else
               f"⚠️ *{title}* removed from MeetNow but couldn't delete from Google Calendar — remove manually.")
        await post_to_channel(bot_token, channel_id, msg, fallback_user_id=slack_user_id)

    except Exception as e:
        print("🔥 handle_cancel_meeting ERROR:", str(e))
        if bot_token:
            await post_to_channel(bot_token, channel_id, "❌ Something went wrong cancelling.", fallback_user_id=slack_user_id)
    finally:
        db.close()