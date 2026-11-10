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
    app_module = load("app")
    app_module.STORE = load("store").Store(":memory:", ttl=300, attempts=5, session_ttl=900)
    app_module._sent_at.clear()
    sent = []

    async def fake_send(code, chat_id=None):
        sent.append(code)
        return True

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
