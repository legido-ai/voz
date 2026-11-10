"""voz — talk to OpenCode Go with a natural voice, over a phone browser.

Browser  ──WebSocket (PCM16)──▶  this app  ──▶  OpenCode Go / deepseek-v4.1-flash
                                    │
                                    ├── sherpa-onnx streaming ASR (in process)
                                    └── voz-tts (Kokoro-82M) ──▶ PCM16 back

The phone only captures and plays audio: no model runs on it, and the only
outbound call is the subscription LLM.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
from pathlib import Path

import numpy as np
from fastapi import FastAPI, Form, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

import asr as A
import authdb
import pipeline as P

HERE = Path(__file__).parent
STATIC = HERE / "static"

COOKIE = "voz_session"
COOKIE_TTL = 60 * 86400
SECRET = os.environ.get("VOZ_COOKIE_SECRET") or hashlib.sha256(os.urandom(32)).hexdigest()
PASS_HASH = os.environ.get("VOZ_PASS_HASH", "").strip().lower()
MAX_TURNS = int(os.environ.get("VOZ_MAX_TURNS", "80"))
MIN_UTTERANCE = float(os.environ.get("VOZ_MIN_UTTERANCE", "0.35"))
VOICE_IDS = {"es_f", "es_m", "es_m2", "es_piper_f", "es_piper_m",
             "es_carlfm", "es_davefx", "es_mls10246", "es_mls9972"}
DECODE_EVERY = 8                      # frames of 20 ms fed to the ASR before decoding

app = FastAPI(title="voz", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

# Per-IP login protection: N consecutive wrong codes ban the IP for a while.
# Behind Traefik the socket peer is the proxy, so the client is taken from
# X-Forwarded-For, which Traefik sets itself (forwardedHeaders.insecure=false,
# no trustedIPs => it discards whatever the client sent and writes the real peer).
BAN_AFTER = int(os.environ.get("VOZ_BAN_AFTER", "3"))
BAN_SECONDS = int(os.environ.get("VOZ_BAN_SECONDS", "300"))
FAIL_WINDOW = int(os.environ.get("VOZ_FAIL_WINDOW", "600"))

_failures: dict[str, tuple[int, float]] = {}   # ip -> (count, last failure)
_bans: dict[str, float] = {}                   # ip -> banned until


# ------------------------------------------------------------------------- auth


def _token() -> str:
    stamp = str(int(time.time()))
    sig = hmac.new(SECRET.encode(), stamp.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{stamp}.{sig}"


def _valid(token: str | None) -> bool:
    if not PASS_HASH:
        return True                                    # no code configured: open (dev)
    if not token or "." not in token:
        return False
    stamp, _, sig = token.partition(".")
    expect = hmac.new(SECRET.encode(), stamp.encode(), hashlib.sha256).hexdigest()[:32]
    try:
        fresh = time.time() - int(stamp) < COOKIE_TTL
    except ValueError:
        return False
    return fresh and hmac.compare_digest(sig, expect)


def _authed(request: Request) -> bool:
    return _valid(request.cookies.get(COOKIE))


def client_ip(request: Request) -> str:
    """The real peer: rightmost X-Forwarded-For entry, else the socket peer."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        candidate = forwarded.split(",")[-1].strip()
        if candidate:
            return candidate
    return request.client.host if request.client else "?"


def _banned(ip: str) -> int:
    """Seconds left on this IP's ban, 0 when it is not banned."""
    until = _bans.get(ip)
    if not until:
        return 0
    left = int(until - time.time())
    if left <= 0:
        _bans.pop(ip, None)
        return 0
    return left


def _note_failure(ip: str) -> int:
    """Count a wrong code; return the ban length applied, 0 if not banned yet."""
    now = time.time()
    count, last = _failures.get(ip, (0, now))
    if now - last > FAIL_WINDOW:
        count = 0
    count += 1
    _failures[ip] = (count, now)
    if count >= BAN_AFTER:
        _failures.pop(ip, None)
        _bans[ip] = now + BAN_SECONDS
        return BAN_SECONDS
    return 0


def _note_success(ip: str) -> None:
    _failures.pop(ip, None)
    _bans.pop(ip, None)


LOGIN_PAGE = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Voz</title><link rel="stylesheet" href="/static/styles.css"></head>
<body class="login-body"><main class="login">
<h1>Voz</h1><p class="muted">Habla con tu asistente.</p>
<form method="post" action="/login">
<input type="text" name="user" value="{user}" autocapitalize="none" autocorrect="off"
 spellcheck="false" autocomplete="username" placeholder="Usuario"{user_disabled}>
<input type="password" name="password" inputmode="text" autocomplete="current-password"
 placeholder="Contraseña"{focus}>
<button type="submit">Entrar</button></form>
{error}</main></body></html>"""


def _login_page(error: str = "") -> str:
    shared = authdb.available()
    return LOGIN_PAGE.format(
        error=error,
        user=authdb.DEFAULT_USER if shared else "",
        user_disabled="" if shared else ' style="display:none"',
        focus="" if shared else " autofocus",
    )


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    if _authed(request):
        return RedirectResponse("/", status_code=303)
    return HTMLResponse(_login_page())


@app.post("/login")
async def login_submit(request: Request, password: str = Form(""),
                       user: str = Form(""), code: str = Form("")):
    ip = client_ip(request)
    left = _banned(ip)
    if left:
        return HTMLResponse(
            _login_page(
                f'<p class="error">Demasiados intentos. Prueba en {left // 60 + 1} min.</p>'
            ),
            status_code=429, headers={"Retry-After": str(left)},
        )
    secret = password or code
    if authdb.available():
        ok = authdb.verify(user or authdb.DEFAULT_USER, secret)
    else:
        ok = bool(PASS_HASH) and hmac.compare_digest(
            hashlib.sha256(secret.strip().encode()).hexdigest(), PASS_HASH
        )
    if ok:
        _note_success(ip)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(COOKIE, _token(), max_age=COOKIE_TTL, httponly=True,
                            secure=True, samesite="lax", path="/")
        return response
    banned_for = _note_failure(ip)
    print(f"login: wrong credentials from {ip} (user={user or '-'})"
          + (f" - banned {banned_for}s" if banned_for else ""), flush=True)
    if banned_for:
        return HTMLResponse(
            _login_page('<p class="error">Demasiados intentos. Bloqueado 5 minutos.</p>'),
            status_code=429, headers={"Retry-After": str(banned_for)},
        )
    return HTMLResponse(
        _login_page('<p class="error">Usuario o contraseña incorrectos.</p>'), status_code=401
    )


@app.get("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE, path="/")
    return response


# ------------------------------------------------------------------------ pages


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if not _authed(request):
        return RedirectResponse("/login", status_code=303)
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})


@app.get("/healthz")
async def healthz():
    tts = await P.healthy(P.TTS_URL)
    stt = await P.healthy(P.STT_URL) if P.STT_URL else None
    model = A.instance()
    return JSONResponse({
        "ok": bool(tts and P.LLM_KEY and model),
        "auth": "ai-usage accounts" if authdb.available() else "local code",
        "asr": "sherpa-streaming" if model else "unavailable",
        "tts": tts,
        "whisper_fallback": stt,
        "llm_key": bool(P.LLM_KEY),
        "model": P.LLM_MODEL,
        "voice": P.VOICE,
    })


@app.get("/manifest.webmanifest")
async def manifest():
    return JSONResponse(json.loads((STATIC / "manifest.webmanifest").read_text()),
                        media_type="application/manifest+json")


@app.get("/apple-touch-icon.png")
async def apple_icon():
    return FileResponse(STATIC / "icons" / "apple-touch-icon.png")


# --------------------------------------------------------------- voice session


class Session:
    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self.vad = P.VAD()
        self.asr = A.instance()
        self.stream = self.asr.stream() if self.asr else None
        self.state = "listen"
        self.gen = 0
        self.task: asyncio.Task | None = None
        self.history: list[dict] = []
        self.turns = 0
        self.voice = P.VOICE
        self.speed = 1.0
        self.send_lock = asyncio.Lock()
        self.alive = True
        self.fed = 0
        self.last_partial = 0.0
        self.last_text = ""

    # -- transport ---------------------------------------------------------

    async def send_json(self, payload: dict) -> None:
        async with self.send_lock:
            await self.ws.send_json(payload)

    async def send_bytes(self, payload: bytes) -> None:
        async with self.send_lock:
            await self.ws.send_bytes(payload)

    async def state_to(self, state: str) -> None:
        self.state = state
        await self.send_json({"t": "state", "v": state})

    # -- turn control ------------------------------------------------------

    def reset_asr(self) -> None:
        if self.asr:
            self.stream = self.asr.stream()
        self.fed = 0

    async def interrupt(self, tell_client: bool = True) -> None:
        self.gen += 1
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self.task = None
        self.reset_asr()
        if tell_client:
            await self.send_json({"t": "flush"})
        await self.state_to("listen")

    async def on_audio(self, frame: np.ndarray) -> None:
        """One 20 ms frame: VAD decides the turn, the ASR decodes as it goes."""
        event = self.vad.push(frame, speaking=self.state == "speak")

        if self.asr:
            self.asr.feed(self.stream, frame)
            self.fed += 1
            if self.fed >= DECODE_EVERY:
                self.fed = 0
                now = time.time()
                if now - self.last_partial > 0.55 and self.state != "speak":
                    partial = self.asr.partial(self.stream)
                    if partial and partial != self.last_text:
                        self.last_text = partial
                        self.last_partial = now
                        await self.send_json({"t": "partial", "text": partial})

        if event == "start":
            if self.state in ("speak", "think"):
                await self.interrupt(tell_client=True)
        elif event == "end":
            audio = self.vad.close()
            if len(audio) / P.IN_RATE >= MIN_UTTERANCE:
                text = ""
                if self.asr:
                    text = self.asr.finish(self.stream)
                    self.stream = self.asr.stream()
                    self.fed = 0
                await self.start_turn(text, audio)
            else:
                self.reset_asr()
                await self.state_to("listen")

    async def start_turn(self, text: str, audio: np.ndarray) -> None:
        await self.interrupt(tell_client=True)
        if self.turns >= MAX_TURNS:
            await self.send_json({"t": "error", "msg": "Límite de turnos de esta sesión alcanzado."})
            await self.state_to("listen")
            return
        self.turns += 1
        self.gen += 1
        gen = self.gen
        await self.state_to("think")
        self.task = asyncio.create_task(self.respond(text, audio, gen))

    async def respond(self, text: str, audio: np.ndarray, gen: int) -> None:
        try:
            said = P.speakable(text)
            if not said and P.STT_URL:
                try:
                    said = await asyncio.wait_for(P.transcribe(audio), timeout=60)
                except Exception as error:  # noqa: BLE001
                    print(f"fallback stt failed: {error!r}", flush=True)
            if gen != self.gen:
                return
            if not said:
                await self.send_json({"t": "error", "msg": "No te he entendido."})
                await self.state_to("listen")
                return

            await self.send_json({"t": "heard", "text": said})
            self.history.append({"role": "user", "content": said})
            buffer = ""
            held = ""
            spoken: list[str] = []
            first = True

            async def speak(sentence: str, final: bool) -> bool:
                """Speak a fragment, holding back ones too short to sound right."""
                nonlocal held, first
                held = f"{held} {sentence}".strip() if held else sentence
                if not final and len(P.speakable(held)) < 14:
                    return True
                if gen != self.gen:
                    return False
                await self.say(held, gen, first)
                first = False
                spoken.append(held)
                held = ""
                return True

            async for delta in P.stream_reply(self.history):
                if gen != self.gen:
                    return
                buffer += delta
                sentences, buffer = P.split_sentences(buffer, first=first and len(buffer) < 90)
                for sentence in sentences:
                    if not await speak(sentence, False):
                        return
            sentences, buffer = P.split_sentences(buffer, flush=True)
            for sentence in sentences:
                if not await speak(sentence, True):
                    return
            if held:
                if not await speak("", True):
                    return

            if not spoken:
                await self.send_json({"t": "error", "msg": "El modelo no ha devuelto texto."})
            else:
                self.history.append({"role": "assistant", "content": " ".join(spoken)})
                if len(self.history) > 16:
                    self.history = self.history[-16:]
            if gen == self.gen:
                await self.state_to("listen")
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - a turn must never kill the session
            print(f"turn failed: {error!r}", flush=True)
            try:
                await self.send_json({"t": "error", "msg": "Algo ha fallado en ese turno."})
                await self.state_to("listen")
            except Exception:  # noqa: BLE001
                pass

    async def say(self, sentence: str, gen: int, first: bool) -> None:
        text = P.speakable(sentence)
        if len(text) < 2:
            return
        await self.send_json({"t": "say", "text": text})
        announced = False
        async for chunk in P.synth(text, voice=self.voice, speed=self.speed):
            if gen != self.gen:
                return
            if not announced:
                await self.state_to("speak")
                announced = True
            await self.send_bytes(chunk)


@app.on_event("startup")
async def _startup() -> None:
    if authdb.available():
        print(f"auth: {authdb.count()} accounts from {authdb.AUTH_DB} "
              f"(default user {authdb.DEFAULT_USER})", flush=True)
    elif PASS_HASH:
        print("auth: local access code", flush=True)
    else:
        print("auth: DISABLED - no code and no accounts database", flush=True)


@app.websocket("/ws")
async def voice_socket(ws: WebSocket):
    if not _valid(ws.cookies.get(COOKIE)):
        await ws.close(code=4401)
        return
    await ws.accept()
    session = Session(ws)
    await session.send_json({"t": "ready", "in_rate": P.IN_RATE, "out_rate": 24000,
                             "voice": session.voice, "asr": "sherpa" if session.asr else "whisper"})
    pending = bytearray()
    frame_bytes = P.FRAME_SAMPLES * 2
    try:
        while session.alive:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                break
            payload = message.get("bytes")
            if payload is not None:
                pending.extend(payload)
                while len(pending) >= frame_bytes:
                    raw = bytes(pending[:frame_bytes])
                    del pending[:frame_bytes]
                    frame = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
                    await session.on_audio(frame)
                continue

            try:
                control = json.loads(message.get("text") or "{}")
            except json.JSONDecodeError:
                continue
            kind = control.get("t")
            if kind == "stop":
                session.vad.reset()
                await session.interrupt(tell_client=True)
            elif kind == "voice" and control.get("v") in VOICE_IDS:
                session.voice = control["v"]
                await session.send_json({"t": "voice", "v": session.voice})
            elif kind == "speed":
                try:
                    session.speed = min(1.4, max(0.7, float(control.get("v", 1.0))))
                except (TypeError, ValueError):
                    pass
            elif kind == "ping":
                await session.send_json({"t": "pong"})
            elif kind == "close":
                break
    except WebSocketDisconnect:
        pass
    finally:
        session.alive = False
        session.vad.reset()


@app.on_event("shutdown")
async def _shutdown() -> None:
    await P.aclose()
