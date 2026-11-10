"""voz-mfa — out-of-band approval for the voice app.

    voz ──POST /api/auth/verify──▶ voz-mfa ──Bot API──▶ Telegram (perfil voz)

A challenge holds sha256(nonce:code); the code is generated here and leaves only
down the Telegram channel. The web app gets an id, never the code, so a compromised
web container cannot approve itself. Codes are single-use, 5 minutes, 5 attempts.
"""

from __future__ import annotations

import os
import sys
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import store as S
import telegram as TG

app = FastAPI(title="voz-mfa", docs_url=None, redoc_url=None, openapi_url=None)

DB = os.environ.get("MFA_DB", "/data/mfa.db")
STORE = S.Store(DB)

# A challenge is a push notification to a real phone: cap the rate globally as well as
# per caller, so nothing upstream (a scanner, a buggy client, a loop) can flood it.
SEND_COOLDOWN = int(os.environ.get("MFA_SEND_COOLDOWN", "20"))     # seconds between sends
SEND_HOURLY = int(os.environ.get("MFA_SEND_HOURLY", "6"))          # per rolling hour
_sent_at: list[float] = []


def _may_send() -> tuple[bool, int]:
    now = time.time()
    _sent_at[:] = [t for t in _sent_at if now - t < 3600]
    if _sent_at and now - _sent_at[-1] < SEND_COOLDOWN:
        return False, int(SEND_COOLDOWN - (now - _sent_at[-1]))
    if len(_sent_at) >= SEND_HOURLY:
        return False, int(3600 - (now - _sent_at[0]))
    return True, 0


@app.on_event("startup")
async def _startup() -> None:
    print(
        f"mfa: ready db={DB} ttl={STORE.ttl}s attempts={STORE.attempts} "
        f"session={STORE.session_ttl}s telegram={'on' if TG.TOKEN and TG.CHAT_ID else 'off'}",
        file=sys.stderr, flush=True,
    )


@app.get("/healthz")
async def healthz():
    return {"ok": True, "telegram": bool(TG.TOKEN and TG.CHAT_ID),
            "ttl": STORE.ttl, "code_len": S.CODE_LEN}


@app.get("/api/auth/challenge")
async def challenge(request: Request):
    caller = request.headers.get("x-client-ip", "?")
    created = STORE.create()
    allowed, wait = _may_send()
    sent = False
    if allowed:
        sent = await TG.send_code(created.code)
        if sent:
            _sent_at.append(time.time())
    else:
        print(f"mfa: push suppressed (cooldown {wait}s) caller={caller}", flush=True)
    STORE.record(created.id, created.code, sent)
    print(f"mfa: challenge {created.id[:8]} sent={1 if sent else 0} caller={caller}",
          flush=True)
    # The number itself never crosses this boundary.
    return {"id": created.id, "expires_in": created.expires_in,
            "code_length": S.CODE_LEN, "sent": sent, "retry_after": wait}


@app.post("/api/auth/verify")
async def verify(request: Request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    text = str(body.get("token") or body.get("text") or "")
    ident = body.get("id") or request.headers.get("x-challenge")
    verdict = STORE.verify(ident if isinstance(ident, str) else None, text)
    if not verdict.ok:
        print(f"mfa: verify failed reason={verdict.reason} "
              f"caller={request.headers.get('x-client-ip', '?')}", flush=True)
        return JSONResponse({"ok": False, "reason": verdict.reason}, status_code=401)
    return {"ok": True, "session": verdict.session, "expires_in": verdict.expires_in}


@app.post("/api/session/check")
async def check(request: Request):
    token = request.headers.get("x-voz-session") or ""
    ok = STORE.session(token, touch=bool(request.headers.get("x-touch")))
    return JSONResponse({"ok": ok}, status_code=200 if ok else 401)


@app.post("/api/session/revoke")
async def revoke(request: Request):
    token = request.headers.get("x-voz-session") or ""
    STORE.revoke(token)
    return {"ok": True}
