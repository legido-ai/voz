"""voz — talk to the Hermes `voz` profile with a natural voice, over a phone browser.

Browser  ──WebSocket (PCM16)──▶  this app  ──▶  Hermes API server (profile `voz`)
                                    │
                                    ├── sherpa-onnx streaming ASR (in process)
                                    ├── voz-tts (Kokoro / Piper) ──▶ PCM16 back
                                    └── voz-mfa (challenge ──▶ Telegram)

The phone only captures and plays audio, and the app holds no model credential:
the only outbound authorisation is the API-server key of the `voz` profile.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path

import numpy as np
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

import asr as A
import mfa as MFA
import pipeline as P

HERE = Path(__file__).parent
STATIC = HERE / "static"

COOKIE = "voz_session"
IDLE_MINUTES = int(os.environ.get("VOZ_IDLE_MINUTES", "15"))
MAX_TURNS = int(os.environ.get("VOZ_MAX_TURNS", "80"))
MIN_UTTERANCE = float(os.environ.get("VOZ_MIN_UTTERANCE", "0.35"))
VOICE_IDS = {"es_f", "es_m", "es_m2", "es_piper_f", "es_piper_m",
             "es_carlfm", "es_davefx", "es_mls10246", "es_mls9972"}
DECODE_EVERY = 8                      # frames of 20 ms fed to the ASR before decoding
_DIGITS = re.compile(r"\d")

# Spec A1: two lines, and nothing else. No hint about Telegram, the code, or what to do.
MSG_AUTH = "Necesitas autenticarte."
MSG_FAIL = "Error en la autenticación."

app = FastAPI(title="voz", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

# A challenge pushes a Telegram message, so minting is rate limited and a live one is
# reused instead of re-pushed. Nothing here mints on page load: the client asks on the
# first mic tap, because an anonymous GET must not be able to ring a phone.
CHALLENGE_MAX = int(os.environ.get("VOZ_CHALLENGE_MAX", "3"))
CHALLENGE_WINDOW = int(os.environ.get("VOZ_CHALLENGE_WINDOW", "600"))
CHALLENGE_REUSE = int(os.environ.get("VOZ_CHALLENGE_REUSE", "240"))

_challenges: dict[str, tuple[str, float]] = {}   # client key -> (challenge id, minted at)
_minted: dict[str, list[float]] = {}             # client key -> mint timestamps


def _client_key(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    peer = forwarded.split(",")[-1].strip() if forwarded else (
        request.client.host if request.client else "?")
    return peer or "?"


async def _session_from(request: Request) -> str | None:
    token = request.cookies.get(COOKIE)
    if token and await MFA.session_ok(token, touch=False):
        return token
    return None


def _recent(key: str, now: float) -> list[float]:
    """Mint timestamps for this client inside the window (old ones dropped)."""
    stamps = [t for t in _minted.get(key, []) if now - t < CHALLENGE_WINDOW]
    if stamps:
        _minted[key] = stamps
    else:
        _minted.pop(key, None)
    return stamps


# ------------------------------------------------------------------------- auth


@app.get("/api/auth/challenge")
async def auth_challenge(request: Request):
    """Mint a challenge on intent, rate limited per client.

    Reuses a challenge minted for this client in the last CHALLENGE_REUSE seconds so a
    reload or a double tap cannot ring the phone twice; refuses beyond
    CHALLENGE_MAX mints per CHALLENGE_WINDOW.
    """
    if await _session_from(request):
        return {"needs_auth": False, "message": ""}
    key = _client_key(request)
    now = time.time()
    live = _challenges.get(key)
    if live and now - live[1] < CHALLENGE_REUSE and await MFA.is_live(live[0]):
        print(f"challenge: reused for {key}", flush=True)
        return {"needs_auth": True, "message": MSG_AUTH, "reused": True,
                "challenge": {"id": live[0], "expires_in": 0, "code_length": 6,
                              "sent": True}}
    stamps = _recent(key, now)
    if len(stamps) >= CHALLENGE_MAX:
        print(f"challenge: rate limited {key} ({len(stamps)} in window)", flush=True)
        return JSONResponse({"needs_auth": True, "message": MSG_FAIL, "retry_after":
                             int(CHALLENGE_WINDOW - (now - stamps[0]))}, status_code=429)
    created = await MFA.new_challenge(client_ip=key)
    if created is None:
        return JSONResponse({"needs_auth": True, "message": MSG_FAIL, "challenge": None},
                            status_code=503)
    stamps.append(now)
    _minted[key] = stamps
    # Only cache as reusable when the number actually left: otherwise the client would
    # sit on a challenge the user cannot answer until the reuse window expires.
    if created.sent:
        _challenges[key] = (created.id, now)
    print(f"challenge: minted for {key} sent={1 if created.sent else 0}", flush=True)
    return {
        "needs_auth": True,
        "message": MSG_AUTH,
        "challenge": {"id": created.id, "expires_in": created.expires_in,
                      "code_length": created.code_length, "sent": created.sent},
    }


@app.post("/api/auth/verify")
async def auth_verify(request: Request):
    """Check a spoken transcript. Success mints a session cookie (idle TTL)."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    text = str(body.get("token") or "")
    stored = _challenges.get(_client_key(request))
    challenge_id = body.get("challenge") or (stored[0] if stored else None)
    verdict = await MFA.verify(text, challenge_id)
    if not verdict.ok:
        # The dead challenge cannot be reused: the client must ask for a new one.
        _challenges.pop(_client_key(request), None)
        return JSONResponse({"ok": False, "message": MSG_FAIL}, status_code=401)
    _challenges.pop(_client_key(request), None)
    response = JSONResponse({"ok": True, "expires_in": verdict.expires_in})
    response.set_cookie(COOKIE, verdict.session or "", max_age=IDLE_MINUTES * 60,
                        httponly=True, secure=True, samesite="lax", path="/")
    return response


@app.get("/api/session")
async def session_state(request: Request):
    ok = await _session_from(request) is not None
    return JSONResponse({"ok": ok}, status_code=200 if ok else 401)


@app.get("/logout")
async def logout():
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(COOKIE, path="/")
    return response


# ------------------------------------------------------------------------ pages


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    # iOS Safari caches /static aggressively: a stale app.js is invisible to the user
    # and behaves exactly like a broken microphone. The bust makes every deploy reach
    # the phone on the next load.
    for asset in ("app.js", "styles.css"):
        stamp = int((STATIC / asset).stat().st_mtime)
        page = page.replace(f"/static/{asset}", f"/static/{asset}?v={stamp}")
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})


@app.get("/healthz")
async def healthz():
    tts = await P.healthy(P.TTS_URL)
    stt = await P.healthy(P.STT_URL) if P.STT_URL else None
    model = A.instance()
    return JSONResponse({
        "ok": bool(tts and P.LLM_KEY and model),
        "auth": "voz-mfa",
        "asr": "sherpa-streaming" if model else "unavailable",
        "tts": tts,
        "whisper_fallback": stt,
        "relay_key": bool(P.LLM_KEY),
        "relay": P.LLM_BASE,
        "model": P.LLM_MODEL,
        "voice": P.VOICE,
        "idle_minutes": IDLE_MINUTES,
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
    def __init__(self, ws: WebSocket, session_token: str = "") -> None:
        self.ws = ws
        # The relay presents the MFA session, not a password: every turn re-checks it.
        self.session = session_token or getattr(ws, "cookies", {}).get(COOKIE, "") or ""
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
        self.seen = 0
        self.expires = time.time() + IDLE_MINUTES * 60

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
        self.seen += 1
        if self.seen == 50:      # ~1 s of audio: proves the phone is actually feeding us
            print(f"audio: 1s received, rms={self.vad.level:.4f}", flush=True)

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
        # Every executed turn renews the idle clock; an expired session ends the call.
        if not await MFA.session_ok(self.session, touch=True):
            await self.interrupt(tell_client=True)
            # The client verifies the code from the transcript, so an unauthenticated
            # turn must still report what was heard — otherwise the code can never be
            # checked and the session never starts. The streaming model writes numbers
            # as words, so a one-shot pass over the same audio is the authority here.
            text = await self._auth_transcript(text, audio)
            if text:
                await self.send_json({"t": "heard", "text": text})
            # The greeting already said the one allowed line; do not repeat it aloud.
            await self.send_json({"t": "auth_expired", "msg": MSG_AUTH})
            await self.state_to("listen")
            return
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

    async def _auth_transcript(self, text: str, audio: np.ndarray) -> str:
        """Best transcript for authentication: prefer the one that yields six digits."""
        if len(_DIGITS.findall(text or "")) >= 6 or not P.STT_URL:
            return text
        try:
            alt = await asyncio.wait_for(P.transcribe(audio), timeout=90)
        except Exception as error:  # noqa: BLE001 - a slow STT must not hang a turn
            print(f"auth stt failed: {error!r}", flush=True)
            return text
        if len(_DIGITS.findall(alt)) >= 6:
            return alt
        return f"{text} {alt}".strip()

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
    if not P.LLM_KEY:
        # Fail closed: a relay with no credential would answer "authentication" errors
        # inside a voice turn, which is far harder to diagnose than refusing to boot.
        raise RuntimeError("HERMES_API_KEY is not set: voz relays to the Hermes `voz` profile")
    print(f"relay: {P.LLM_BASE} model={P.LLM_MODEL} idle={IDLE_MINUTES}min mfa={MFA.MFA_URL}",
          flush=True)


@app.websocket("/ws")
async def voice_socket(ws: WebSocket):
    # The socket is opened BEFORE authentication: the code has to be spoken, and the
    # microphone is only reachable through this socket. Nothing is executed until a
    # session exists — start_turn() re-checks it on every turn.
    token = ws.cookies.get(COOKIE) or ""
    authorised = await MFA.session_ok(token, touch=False)
    await ws.accept()
    print(f"ws: {ws.client.host if ws.client else '?'} connected "
          f"(session={'yes' if authorised else 'no'})", flush=True)
    session = Session(ws, token or "")
    await session.send_json({"t": "ready", "in_rate": P.IN_RATE, "out_rate": 24000,
                             "idle_minutes": IDLE_MINUTES,
                             "voice": session.voice, "asr": "sherpa" if session.asr else "whisper"})
    # Until a spoken code matches a live challenge, say one line and nothing else
    # (spec A1). An already-authenticated client is greeted with silence.
    if not authorised:
        await session.say(MSG_AUTH, session.gen, True)
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
            elif kind == "text" and isinstance(control.get("v"), str):
                # Authenticated client resends the request that rode along with the code,
                # so one sentence can both approve and ask (no duplicate dictation).
                request_text = control["v"].strip()
                if request_text:
                    await session.start_turn(request_text, np.zeros(0, dtype=np.float32))
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
    await MFA.aclose()
