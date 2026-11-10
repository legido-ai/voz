"""Client for the voz-mfa service: ask for a challenge, verify a spoken token, keep
the session alive.

The app never sees the code. It asks voz-mfa for a challenge, voz-mfa sends the
number down Telegram, and this module only learns whether the transcript the user
spoke happens to contain it.
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from typing import Optional

import httpx

MFA_URL = os.environ.get("MFA_URL", "http://voz-mfa:8000").rstrip("/")
TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=5.0)

_client: httpx.AsyncClient | None = None
_client_loop: object | None = None


def client() -> httpx.AsyncClient:
    """One pooled client per event loop.

    An AsyncClient holds asyncio primitives bound to the loop that created them, so a
    module-level singleton breaks as soon as the module is used from a second loop
    (a test harness, a worker thread): the symptom is a bare
    "Event is bound to a different event loop", not a network error.
    """
    global _client, _client_loop
    loop = asyncio.get_running_loop()
    if _client is None or _client_loop is not loop or _client.is_closed:
        _client = httpx.AsyncClient(timeout=TIMEOUT)
        _client_loop = loop
    return _client


async def aclose() -> None:
    global _client, _client_loop
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None
    _client_loop = None


@dataclass
class Challenge:
    id: str
    expires_in: int
    code_length: int
    sent: bool


@dataclass
class Verdict:
    ok: bool
    reason: str
    session: Optional[str] = None
    expires_in: int = 0


async def new_challenge(client_ip: str = "") -> Challenge | None:
    """Ask voz-mfa for a fresh challenge. None when the service is unreachable."""
    try:
        headers = {"x-client-ip": client_ip} if client_ip else {}
        response = await client().get(f"{os.environ.get('MFA_URL', MFA_URL).rstrip('/')}"
                                       "/api/auth/challenge", headers=headers)
        response.raise_for_status()
        body = response.json()
    except Exception as error:  # noqa: BLE001 - fail closed, never crash a turn
        print(f"mfa: challenge failed: {error!r}", file=sys.stderr, flush=True)
        return None
    return Challenge(
        id=str(body.get("id", "")),
        expires_in=int(body.get("expires_in", 0)),
        code_length=int(body.get("code_length", 6)),
        sent=bool(body.get("sent", False)),
    )


async def verify(token: str, challenge_id: str | None = None) -> Verdict:
    """Check a transcript. A failure is a normal outcome, not an error."""
    try:
        response = await client().post(
            f"{os.environ.get('MFA_URL', MFA_URL).rstrip('/')}/api/auth/verify",
            json={"token": token, "id": challenge_id},
        )
    except Exception as error:  # noqa: BLE001
        print(f"mfa: verify failed: {error!r}", file=sys.stderr, flush=True)
        return Verdict(False, "unreachable")
    if response.status_code == 200:
        body = response.json()
        return Verdict(True, "ok", body.get("session"), int(body.get("expires_in", 0)))
    try:
        reason = response.json().get("reason", "rejected")
    except Exception:  # noqa: BLE001
        reason = "rejected"
    return Verdict(False, str(reason))


async def session_ok(session: str | None, *, touch: bool = True) -> bool:
    if not session:
        return False
    headers = {"x-voz-session": session}
    if touch:
        headers["x-touch"] = "1"
    # The URL is read at call time, not at import time, so a changed MFA endpoint
    # (or a test harness pointing at a local instance) takes effect immediately.
    try:
        response = await client().post(f"{os.environ.get('MFA_URL', MFA_URL).rstrip('/')}"
                                       "/api/session/check", headers=headers)
        return response.status_code == 200
    except Exception as error:  # noqa: BLE001
        print(f"mfa: session check failed: {error!r}", file=sys.stderr, flush=True)
        return False
