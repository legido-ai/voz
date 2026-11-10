"""Deliver the challenge code to the user's Telegram.

The service owns its bot token; it does not borrow Hermes'. The message is the code
and nothing else: the user already knows where it comes from (spec A1).
"""

from __future__ import annotations

import os
import sys

import httpx

TOKEN = os.environ.get("MFA_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("MFA_CHAT_ID", "").strip()
API = os.environ.get("MFA_TELEGRAM_API", "https://api.telegram.org").rstrip("/")


def _text(code: str) -> str:
    return f"{code[:3]} {code[3:]}"


async def send_code(code: str, *, chat_id: str | None = None) -> dict:
    """Deliver the code; returns {"ok": bool, "message_id": int | None}. Never raises."""
    target = (chat_id or CHAT_ID).strip()
    if not TOKEN or not target:
        print("mfa: telegram not configured (MFA_BOT_TOKEN/MFA_CHAT_ID)", file=sys.stderr, flush=True)
        return {"ok": False, "message_id": None}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{API}/bot{TOKEN}/sendMessage",
                json={
                    "chat_id": target,
                    "text": _text(code),
                    "disable_notification": False,
                    "protect_content": True,
                },
            )
    except Exception as error:  # noqa: BLE001 - delivery must not crash a request
        print(f"mfa: telegram send failed: {error!r}", file=sys.stderr, flush=True)
        return {"ok": False, "message_id": None}
    if response.status_code != 200:
        # Never print the token or the URL: status plus a truncated body only.
        print(f"mfa: telegram {response.status_code}: {response.text[:120]}",
              file=sys.stderr, flush=True)
        return {"ok": False, "message_id": None}
    body = response.json()
    return {"ok": True, "message_id": (body.get("result") or {}).get("message_id")}
