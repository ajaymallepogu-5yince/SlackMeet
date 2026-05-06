from sqlalchemy import Column, String
from core.database import Base


class UserToken(Base):
    __tablename__ = "user_tokens"

    user_id = Column(String, primary_key=True, index=True)
    access_token = Column(String)
    refresh_token = Column(String)
    token_uri = Column(String)
    client_id = Column(String)
    client_secret = Column(String)
    scopes = Column(String)