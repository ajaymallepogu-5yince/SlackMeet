from fastapi import FastAPI
from routes import slack, auth

app = FastAPI()

app.include_router(slack.router)
app.include_router(auth.router)