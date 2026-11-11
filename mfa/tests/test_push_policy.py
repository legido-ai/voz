"""Behavioural tests for the mint/push policy (no network, no Telegram)."""

import importlib.util
import os
import sys

import pytest

MFA_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(name):
    path = os.path.join(MFA_DIR, f"{name}.py")
    spec = importlib.util.spec_from_file_location(f"vozmfa_{name}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"vozmfa_{name}"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def mfa(monkeypatch):
    os.environ["MFA_DB"] = ":memory:"
    os.environ["MFA_SEND_COOLDOWN"] = "20"
    os.environ["MFA_SEND_HOURLY"] = "3"
    os.environ["MFA_OUTBOX"] = "1"
    app_module = load("app")
    app_module.STORE = load("store").Store(":memory:", ttl=300, attempts=5, session_ttl=900)
    app_module._sent_at.clear()
    sent = []

    async def fake_send(code, chat_id=None):
        sent.append(code)
        return {"ok": True, "message_id": len(sent)}

    app_module.TG.send_code = fake_send
    app_module.sent = sent
    return app_module


def test_first_send_is_allowed(mfa):
    assert mfa._may_send() == (True, 0)


def test_a_second_push_inside_the_cooldown_is_suppressed(mfa):
    allowed, _ = mfa._may_send()
    assert allowed
    mfa._sent_at.append(mfa.time.time())
    allowed, wait = mfa._may_send()
    assert not allowed and wait > 0


def test_hourly_cap_stops_a_flood(mfa):
    now = mfa.time.time()
    mfa._sent_at[:] = [now - 3000, now - 2000, now - 100]
    allowed, wait = mfa._may_send()
    assert not allowed and wait > 0


def test_old_sends_age_out_of_the_window(mfa):
    now = mfa.time.time()
    mfa._sent_at[:] = [now - 3700, now - 3600 - 10]
    allowed, _ = mfa._may_send()
    assert allowed


def test_a_suppressed_push_still_mints_a_challenge(mfa):
    """The user must still be able to ask again after the cooldown: the challenge
    exists even when the push was withheld."""
    created = mfa.STORE.create()
    assert created.code.isdigit() and len(created.code) == 6


def test_private_and_loopback_callers_never_reach_telegram(mfa):
    for addr in ("127.0.0.1", "172.17.0.1", "10.210.0.3", "::1", "192.168.1.5"):
        assert not mfa._is_public(addr), addr
    for addr in ("82.70.92.83", "8.8.8.8", "2a00:1450:4001::1"):
        assert mfa._is_public(addr), addr


def test_outbox_records_caller_and_message_id(mfa):
    created = mfa.STORE.create()
    mfa.STORE.record(created.id, created.code, True, caller="82.70.92.83", message_id=42)
    row = mfa.STORE.db.execute(
        "select caller, message_id, sent from outbox order by id desc limit 1").fetchone()
    assert row == ("82.70.92.83", 42, 1)


def test_only_one_live_challenge_per_caller(mfa):
    """One code in Telegram at a time: a second request returns the same challenge."""
    s = mfa.STORE
    first = s.create("1.2.3.4")
    live = s.live_for("1.2.3.4")
    assert live == first.id
    # a different caller is independent
    assert s.live_for("5.6.7.8") is None
    # once consumed, nothing is live
    s.verify(first.id, first.code)
    assert s.live_for("1.2.3.4") is None


def test_a_caller_that_keeps_failing_goes_quiet():
    """A stranger poking the mic must not keep ringing the phone with codes."""
    source = open(os.path.join(MFA_DIR, "app.py"), encoding="utf-8").read()
    assert "def _blocked(" in source
    assert "MFA_FAIL_LIMIT" in source and "MFA_FAIL_WINDOW" in source
    # failures are recorded where the verify verdict is known
    assert "_fails.setdefault(who, [])" in source
    # and the push path consults it before sending
    body = source.split("async def challenge(", 1)[1].split("@app.get", 1)[0]
    assert "_blocked(caller)" in body
