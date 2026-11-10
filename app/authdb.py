"""Login against the AI Usage accounts database.

The AI dashboards (`ai-usage`, `hermes-usage`) keep their users in
`accounts.db` with scrypt hashes (`scrypt$n$r$p$b64salt$b64key`). voz reads that
database so both share one password: rotating it in the AI app rotates it here.

The database is SQLite in WAL mode and lives on a read-only mount, which SQLite
cannot open directly (it needs to create `-shm`/`-wal`). So it is copied — with
its WAL — into a private temp directory and queried there; the copy is refreshed
when the source changes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import shutil
import sqlite3
import tempfile
import threading

AUTH_DB = os.environ.get("VOZ_AUTH_DB", "").strip()
DEFAULT_USER = os.environ.get("VOZ_AUTH_USER", "usuario")

_lock = threading.Lock()
_snapshot: str | None = None
_stamp: tuple = ()


def _verify_scrypt(password: str, stored: str | None) -> bool:
    if not stored or not isinstance(stored, str):
        return False
    parts = stored.split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        return False
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt = base64.b64decode(parts[4])
        expected = base64.b64decode(parts[5])
    except (ValueError, TypeError):
        return False
    try:
        candidate = hashlib.scrypt((password or "").encode("utf-8"), salt=salt,
                                   n=n, r=r, p=p, dklen=len(expected))
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(candidate, expected)


def available() -> bool:
    return bool(AUTH_DB) and os.path.exists(AUTH_DB)


def _local_copy() -> str | None:
    """A writable copy of the accounts database, refreshed when the source moves."""
    global _snapshot, _stamp
    if not available():
        return None
    sources = [p for p in (AUTH_DB, f"{AUTH_DB}-wal", f"{AUTH_DB}-shm") if os.path.exists(p)]
    try:
        stamp = tuple((os.path.getmtime(p), os.path.getsize(p)) for p in sources)
    except OSError:
        return _snapshot
    with _lock:
        if _snapshot and stamp == _stamp and os.path.exists(_snapshot):
            return _snapshot
        workdir = tempfile.mkdtemp(prefix="voz-auth-")
        target = os.path.join(workdir, "accounts.db")
        try:
            for source in sources:
                suffix = source[len(AUTH_DB):]
                shutil.copyfile(source, target + suffix)
        except OSError:
            return _snapshot
        _snapshot, _stamp = target, stamp
        return _snapshot


def count() -> int:
    """How many accounts the shared database exposes (used by the startup log)."""
    database = _local_copy()
    if not database:
        return 0
    try:
        connection = sqlite3.connect(database, timeout=5)
        try:
            return int(connection.execute("SELECT COUNT(*) FROM users").fetchone()[0])
        finally:
            connection.close()
    except sqlite3.Error:
        return 0


def verify(username: str, password: str) -> bool:
    """True when the pair matches a row in the AI Usage accounts database."""
    database = _local_copy()
    if not database:
        return False
    try:
        connection = sqlite3.connect(database, timeout=5)
        try:
            row = connection.execute(
                "SELECT password_hash FROM users WHERE username = ?",
                ((username or "").strip().lower(),),
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        return False
    return bool(row) and _verify_scrypt(password, row[0])
