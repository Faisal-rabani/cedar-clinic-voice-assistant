import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket
from fastapi.staticfiles import StaticFiles

from cedar_clinic.store import Store

ROOT = Path(__file__).resolve().parent
logger = logging.getLogger(__name__)
load_dotenv(ROOT / ".env")


@asynccontextmanager
async def lifespan(app):
    app.state.store = Store(os.getenv("CLINIC_DB", str(ROOT / "clinic.sqlite3")))
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/api/dashboard")
async def dashboard():
    return app.state.store.snapshot()


@app.websocket("/ws/call")
async def call(socket: WebSocket):
    origin = urlsplit(socket.headers.get("origin", ""))
    if origin.scheme not in {"http", "https"} or origin.netloc != socket.headers.get("host"):
        await socket.close(code=1008)
        return
    await socket.accept()
    if not all(os.getenv(key) for key in ("ELEVENLABS_API_KEY", "GOOGLE_API_KEY")):
        await socket.send_json({"type": "error", "text": "Set the ElevenLabs and Google API keys in .env."})
        await socket.close(code=1011)
        return
    try:
        from cedar_clinic.voice import run_call
        await run_call(socket, app.state.store)
    except Exception as error:
        logger.error("Call failed (%s)", type(error).__name__)
        if socket.application_state.name == "CONNECTED":
            try:
                await socket.send_json({"type": "error", "text": "Voice service unavailable. Check dependencies and provider configuration."})
                await socket.close(code=1011)
            except (RuntimeError, OSError):
                pass


app.mount("/", StaticFiles(directory=ROOT / "static", html=True), name="dashboard")
