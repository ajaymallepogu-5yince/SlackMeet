from fastapi import FastAPI
from core.database import init_db
from routes.auth import router as auth_router
from routes.slack import router as slack_router
from routes.slack_install import router as install_router

app = FastAPI(title="MeetNow")


@app.on_event("startup")
def on_startup():
    init_db()


app.include_router(auth_router)
app.include_router(slack_router)
app.include_router(install_router)


@app.get("/")
def root():
    return {
        "app": "MeetNow",
        "install": "/slack/install",
        "status": "running"
    }