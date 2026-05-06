from fastapi import FastAPI
from routes import slack, auth
from core.database import Base, engine

app = FastAPI()

# create tables
Base.metadata.create_all(bind=engine)

app.include_router(slack.router)
app.include_router(auth.router)