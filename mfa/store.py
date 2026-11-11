"""MFA store: challenges and sessions, SQLite, stdlib only.

A challenge holds `sha256(nonce:code)`, never the code. The code is generated here
and leaves this process exactly once, down the Telegram channel; the web app never
sees it, which is what makes the approval meaningful.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Optional

CODE_LEN = int(os.environ.get("MFA_CODE_LEN", "6"))
TTL = int(os.environ.get("MFA_TTL_SECONDS", "300"))
MAX_ATTEMPTS = int(os.environ.get("MFA_MAX_ATTEMPTS", "5"))
SESSION_TTL = int(os.environ.get("VOZ_IDLE_MINUTES", "15")) * 60

# A transcript like "código cuatro siete dos nueve uno tres, apaga la música" is read
# in words while the phone screen shows digits; ASR often returns "NECESITAS 4 7 2 9
# 1 3" or "cuarenta y siete...". All we can do reliably is find every run of six
# digits that survived transcription, so digits are extracted, then windows taken.
import re

from words import words_to_digits

_DIGITS = re.compile(r"\d")


def _hash(nonce: str, code: str) -> str:
    return hashlib.sha256(f"{nonce}:{code}".encode()).hexdigest()


def digits(text: str) -> str:
    return "".join(_DIGITS.findall(text or ""))


def find_code(text: str, code: str) -> bool:
    """Is `code` present as a contiguous digit run in the transcript?"""
    stream = digits(text)
    return bool(stream) and code in stream


@dataclass
class Created:
    id: str
    code: str
    expires_in: int


@dataclass
class Verdict:
    ok: bool
    reason: str            # ok | unknown | expired | used | no_code | attempts
    session: Optional[str] = None
    expires_in: int = 0


class Store:
    def __init__(self, path: str, *, ttl: int = TTL, attempts: int = MAX_ATTEMPTS,
                 session_ttl: int = SESSION_TTL) -> None:
        self.ttl = ttl
        self.attempts = attempts
        self.session_ttl = session_ttl
        self._lock = threading.Lock()
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS challenges (
            id TEXT PRIMARY KEY, nonce TEXT NOT NULL, code_hash TEXT NOT NULL,
            created REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            used INTEGER NOT NULL DEFAULT 0, caller TEXT)""")
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(challenges)")}
        if "caller" not in columns:
            self.db.execute("ALTER TABLE challenges ADD COLUMN caller TEXT")
        self.db.execute("""CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY, created REAL NOT NULL, touched REAL NOT NULL)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT, challenge TEXT NOT NULL,
            code TEXT NOT NULL, sent INTEGER NOT NULL, at REAL NOT NULL,
            caller TEXT, message_id INTEGER)""")
        # Older volumes predate the audit columns: add them in place.
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(outbox)")}
        for name, kind in (("caller", "TEXT"), ("message_id", "INTEGER")):
            if name not in columns:
                self.db.execute(f"ALTER TABLE outbox ADD COLUMN {name} {kind}")

    # -- challenges -------------------------------------------------------

    def live_for(self, caller: str) -> str | None:
        """The caller's live challenge, if any: one code in Telegram at a time."""
        if not caller:
            return None
        with self._lock:
            row = self.db.execute(
                "SELECT id FROM challenges WHERE caller=? AND used=0 AND created > ? "
                "ORDER BY created DESC LIMIT 1",
                (caller, time.time() - self.ttl),
            ).fetchone()
        return row[0] if row else None

    def create(self, caller: str = "") -> Created:
        code = "".join(str(secrets.randbelow(10)) for _ in range(CODE_LEN))
        nonce = secrets.token_hex(16)
        ident = secrets.token_urlsafe(16)
        now = time.time()
        with self._lock:
            self.db.execute(
                "INSERT INTO challenges (id, nonce, code_hash, created, caller) VALUES (?,?,?,?,?)",
                (ident, nonce, _hash(nonce, code), now, caller),
            )
            self.db.execute("DELETE FROM challenges WHERE created < ?", (now - 86400,))
        return Created(id=ident, code=code, expires_in=self.ttl)

    def record(self, ident: str, code: str, sent: bool, *, caller: str = "",
               message_id: int | None = None) -> None:
        """Diagnostic trail: who asked, and which Telegram message carried the code."""
        if os.environ.get("MFA_OUTBOX", "").strip() in ("1", "true"):
            with self._lock:
                self.db.execute(
                    "INSERT INTO outbox (challenge, code, sent, at, caller, message_id) "
                    "VALUES (?,?,?,?,?,?)",
                    (ident, code, 1 if sent else 0, time.time(), caller, message_id),
                )

    def live(self, ident: str) -> bool:
        with self._lock:
            row = self.db.execute(
                "SELECT created, used FROM challenges WHERE id=?", (ident,)
            ).fetchone()
        if row is None:
            return False
        created, used = row
        return not used and (time.time() - created) <= self.ttl

    def verify(self, ident: Optional[str], text: str) -> Verdict:
        """Check a spoken transcript against the live challenge."""
        with self._lock:
            row = None
            if ident:
                row = self.db.execute(
                    "SELECT nonce, code_hash, created, attempts, used FROM challenges WHERE id=?",
                    (ident,),
                ).fetchone()
            if row is None:
                # An unknown or stale id must not dead-end the user: the code they read
                # came from the newest challenge pushed to them, so fall back to it.
                row = self.db.execute(
                    "SELECT nonce, code_hash, created, attempts, used, id FROM challenges "
                    "WHERE used=0 AND created > ? ORDER BY created DESC LIMIT 1",
                    (time.time() - self.ttl,),
                ).fetchone()
            if row is None:
                return Verdict(False, "unknown")
            nonce, code_hash, created, attempts, used = row[:5]
            live_id = ident or row[5]
            if used:
                return Verdict(False, "used")
            if time.time() - created > self.ttl:
                return Verdict(False, "expired")
            if attempts >= self.attempts:
                self.db.execute("UPDATE challenges SET used=1 WHERE id=?", (live_id,))
                return Verdict(False, "attempts")
            # Two streams: literal digits and number words read aloud. A code dictated
            # as "quinientos diez mil ochocientos veintinueve" must match too.
            streams = [digits(text), words_to_digits(text)]
            matched = None
            for stream in streams:
                if len(stream) < CODE_LEN:
                    continue
                for start in range(len(stream) - CODE_LEN + 1):
                    candidate = stream[start:start + CODE_LEN]
                    if hmac.compare_digest(_hash(nonce, candidate), code_hash):
                        matched = candidate
                        break
                if matched:
                    break
            if matched is None:
                self.db.execute(
                    "UPDATE challenges SET attempts=attempts+1 WHERE id=?", (live_id,)
                )
                exhausted = attempts + 1 >= self.attempts
                if exhausted:
                    self.db.execute("UPDATE challenges SET used=1 WHERE id=?", (live_id,))
                return Verdict(False, "attempts" if exhausted else "no_code")
            self.db.execute("UPDATE challenges SET used=1 WHERE id=?", (live_id,))
            token = secrets.token_urlsafe(32)
            now = time.time()
            self.db.execute(
                "INSERT INTO sessions (token, created, touched) VALUES (?,?,?)",
                (token, now, now),
            )
            self.db.execute("DELETE FROM sessions WHERE touched < ?", (now - 86400,))
            return Verdict(True, "ok", session=token, expires_in=self.session_ttl)

    # -- sessions ---------------------------------------------------------

    def session(self, token: Optional[str], *, touch: bool = False) -> bool:
        if not token:
            return False
        with self._lock:
            row = self.db.execute(
                "SELECT touched FROM sessions WHERE token=?", (token,)
            ).fetchone()
            if row is None:
                return False
            now = time.time()
            if now - row[0] > self.session_ttl:
                self.db.execute("DELETE FROM sessions WHERE token=?", (token,))
                return False
            if touch:
                self.db.execute("UPDATE sessions SET touched=? WHERE token=?", (now, token))
            return True

    def revoke(self, token: Optional[str]) -> None:
        if not token:
            return
        with self._lock:
            self.db.execute("DELETE FROM sessions WHERE token=?", (token,))
