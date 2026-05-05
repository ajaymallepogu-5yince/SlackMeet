from fastapi import FastAPI
from routes import slack, auth
import os

print("CURRENT DIR:", os.getcwd())
print("FILES:", os.listdir())

app = FastAPI()

app.include_router(slack.router)
app.include_router(auth.router)