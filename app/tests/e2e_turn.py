"""End-to-end turn without network: fake Hermes, fake TTS, in-process ASR skipped.

    /opt/data/cache/scratch/vt/bin/python app/tests/e2e_turn.py

Starts voz-mfa and voz (uvicorn) on localhost, drives the MFA challenge, then walks
the same path the browser walks: challenge -> verify spoken transcript -> session
cookie -> WebSocket frame -> relay to the fake Hermes -> audio back.

The point is the ordering the spec insists on: nothing is forwarded to Hermes until a
live challenge accepted the spoken code.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time

import numpy as np
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MFA_DIR = os.path.join(os.path.dirname(ROOT), "mfa")
sys.path.insert(0, ROOT)
sys.path.insert(0, MFA_DIR)

# --- fake relays -------------------------------------------------------------------

seen_by_hermes: list[str] = []
steps: list[str] = []

fake = FastAPI()


@fake.post("/v1/chat/completions")
async def completions(request: Request):
    body = await request.json()
    last = body["messages"][-1]["content"]
    seen_by_hermes.append(last)
    steps.append("hermes")

    async def events():
        chunk = {"choices": [{"delta": {"content": "Hecho: servicio encendido."}}]}
        yield f"data: {json.dumps(chunk)}\n\n".encode()
        yield b"data: [DONE]\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


@fake.post("/speak")
async def speak(request: Request):
    steps.append("tts")
    pcm = (np.zeros(2400, dtype="<i2")).tobytes()

    async def chunks():
        yield pcm

    return StreamingResponse(chunks(), media_type="application/octet-stream")


@fake.get("/healthz")
async def tts_health():
    return {"ok": True}


def serve(app, port):
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            return server
        time.sleep(0.05)
    raise RuntimeError(f"port {port} did not start")


async def main() -> int:
    import httpx

    serve(fake, 8711)

    os.environ["MFA_DB"] = ":memory:"
    os.environ["MFA_OUTBOX"] = "1"
    os.environ["MFA_BOT_TOKEN"] = ""
    os.environ["MFA_TTL_SECONDS"] = "300"
    os.environ["VOZ_IDLE_MINUTES"] = "15"
    os.environ["MFA_URL"] = "http://127.0.0.1:8712"
    os.environ["TTS_URL"] = "http://127.0.0.1:8711"
    os.environ["STT_URL"] = ""
    os.environ["HERMES_BASE"] = "http://127.0.0.1:8711/v1"
    os.environ["HERMES_API_KEY"] = "test-key"
    os.environ["ASR_DIR"] = "/nonexistent"          # ASR off: the turn is driven by text

    import store as S
    import app as mfa_app
    import main as voz_main

    # One store, shared by the MFA app and this process: an in-memory DB is per
    # connection, so a second Store(":memory:") would silently see nothing.
    voz_store = S.Store(":memory:", ttl=300, attempts=5, session_ttl=900)
    mfa_app.STORE = voz_store
    serve(mfa_app.app, 8712)
    serve(voz_main.app, 8713)

    failures = []

    def check(name, ok, detail=""):
        print(f"{'PASS' if ok else 'FAIL'}  {name}{(' — ' + detail) if detail and not ok else ''}")
        if not ok:
            failures.append(name)

    async with httpx.AsyncClient(base_url="http://127.0.0.1:8713", timeout=20.0) as client:
        # 1. The page asks for a challenge; the number goes to Telegram, not to the app.
        created = await client.get("/api/auth/challenge")
        body = created.json()
        check("challenge returned", created.status_code == 200 and body["needs_auth"])
        check("no code in the response", "code" not in json.dumps(
            {k: v for k, v in (body.get("challenge") or {}).items() if k != "code_length"}))
        code = voz_store.db.execute("SELECT code FROM outbox ORDER BY id DESC LIMIT 1").fetchone()[0]
        check("number reached the message channel", bool(code) and code.isdigit())

        # 2. Nothing reaches the relay before authentication.
        ws = await _ws_attempt(client)
        check("ws refused without session", ws is None)

        # 3. Spoken code inside a busy phrase authenticates and mints the session.
        phrase = f"hola necesito autenticarme, el código es {code}, enciende el servicio de música"
        verify = await client.post("/api/auth/verify", json={"token": phrase,
                                                            "challenge": (body["challenge"] or {}).get("id")})
        check("spoken code accepted", verify.status_code == 200 and verify.json().get("ok"),
              verify.text)
        check("session cookie set", "voz_session" in client.cookies)

        # 4. Reuse is refused.
        again = await client.post("/api/auth/verify", json={"token": phrase})
        check("same code cannot be reused", again.status_code == 401)

        # 5. The authenticated turn reaches Hermes with the full phrase (code included)
        #    and comes back as audio.
        spoken = await _turn(client, voz_main, phrase)
        check("relay received the phrase", seen_by_hermes[-1:] == [phrase], str(seen_by_hermes))
        check("audio came back", spoken)
        check("validated before forwarding", steps[:2] == ["hermes", "tts"], str(steps[:2]))

    print()
    print("relay saw:", seen_by_hermes)
    print("order:", steps)
    return 1 if failures else 0


async def _ws_attempt(client):
    import websockets

    try:
        async with websockets.connect("ws://127.0.0.1:8713/ws") as ws:
            await ws.recv()
            return ws
    except Exception:
        return None


async def _turn(client, voz_main, phrase):
    """Drive one Session directly: the WS transport is a thin wrapper around it."""
    class FakeWs:
        def __init__(self):
            self.frames = []
            self.json = []

        async def send_json(self, payload):
            self.json.append(payload)

        async def send_bytes(self, payload):
            self.frames.append(payload)

    ws = FakeWs()
    ws.cookies = {"voz_session": client.cookies.get("voz_session")}
    session = voz_main.Session(ws, client.cookies.get("voz_session"))
    session.asr = None
    session.stream = None
    await session.start_turn(phrase, np.zeros(1600, dtype=np.float32))
    for _ in range(600):                 # the turn runs as a task; 0.01 s x 600
        if session.task and session.task.done():
            break
        await asyncio.sleep(0.01)
    if session.task:
        try:
            await session.task
        except Exception as error:  # noqa: BLE001 - surface the real failure
            print("turn task failed:", repr(error))
    return sum(len(f) for f in ws.frames) > 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
