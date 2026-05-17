from sqlalchemy import Column, String, Text
from core.database import Base


class UserToken(Base):
    """Stores Google OAuth tokens per Slack user."""
    __tablename__ = "user_tokens"

    user_id = Column(String, primary_key=True, index=True)
    team_id = Column(String, index=True)          # which Slack workspace
    access_token = Column(String)
    refresh_token = Column(String)
    token_uri = Column(String)
    client_id = Column(String)
    client_secret = Column(String)
    scopes = Column(String)


class WorkspaceInstall(Base):
    """Stores Slack bot tokens per workspace (multi-workspace support)."""
    __tablename__ = "workspace_installs"

    team_id = Column(String, primary_key=True, index=True)
    team_name = Column(String)
    bot_token = Column(String)       # xoxb-...
    bot_user_id = Column(String)


class MeetingRecord(Base):
    """Tracks meetings so users can cancel them."""
    __tablename__ = "meeting_records"

    event_id = Column(String, primary_key=True, index=True)
    user_id = Column(String, index=True)
    team_id = Column(String)
    title = Column(String)
    meet_link = Column(String)
    start_time = Column(String)
    calendar_event_id = Column(String)   # Google Calendar event id for deletion
    slack_message_ts = Column(String)    # Slack message timestamp for updating
    channel_id = Column(String)          # Slack channel for updating message
    response_url = Column(Text)  # Slack response_url for replacing message