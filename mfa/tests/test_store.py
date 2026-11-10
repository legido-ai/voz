"""Behavioural tests for the MFA store: expiry, single use, attempts, matching."""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import store as S  # noqa: E402


@pytest.fixture()
def st():
    return S.Store(":memory:", ttl=300, attempts=5, session_ttl=900)


def test_challenge_returns_id_but_code_is_stored_hashed(st):
    created = st.create()
    assert len(created.code) == 6 and created.code.isdigit()
    rows = st.db.execute("SELECT code_hash, nonce FROM challenges").fetchall()
    assert len(rows) == 1
    blob = " ".join(rows[0])
    assert created.code not in blob


def test_spoken_code_inside_a_busy_transcript_is_accepted(st):
    created = st.create()
    said = f"necesito autenticarme código {created.code}, enciende la música"
    verdict = st.verify(created.id, said)
    assert verdict.ok and verdict.reason == "ok" and verdict.session


def test_code_digits_must_match_exactly(st):
    created = st.create()
    wrong = str((int(created.code) + 1) % 1000000).zfill(6)
    verdict = st.verify(created.id, f"código {wrong}")
    assert not verdict.ok and verdict.reason == "no_code"


def test_code_is_single_use(st):
    created = st.create()
    assert st.verify(created.id, created.code).ok
    again = st.verify(created.id, created.code)
    assert not again.ok and again.reason == "used"


def test_expired_challenge_is_rejected(st):
    created = st.create()
    st.db.execute("UPDATE challenges SET created=? WHERE id=?",
                  (time.time() - 400, created.id))
    verdict = st.verify(created.id, created.code)
    assert not verdict.ok and verdict.reason == "expired"


def test_five_wrong_attempts_kill_the_challenge(st):
    created = st.create()
    for _ in range(5):
        assert not st.verify(created.id, "000000").ok
    # Even the right code is dead now.
    verdict = st.verify(created.id, created.code)
    assert not verdict.ok and verdict.reason in ("used", "attempts")


def test_session_lives_and_then_expires(st):
    created = st.create()
    token = st.verify(created.id, created.code).session
    assert st.session(token)
    st.db.execute("UPDATE sessions SET touched=? WHERE token=?",
                  (time.time() - 1000, token))
    assert not st.session(token)


def test_verify_without_id_matches_the_newest_live_challenge(st):
    old = st.create()
    new = st.create()
    verdict = st.verify(None, new.code)
    assert verdict.ok
    # The older one is still live and still its own code.
    assert st.verify(old.id, old.code).ok


def test_digits_only_extraction_handles_groups(st):
    assert S.digits("código 4 7 2 9 1 3") == "472913"
    assert S.find_code("el 472913 por favor", "472913")
    # ASR drops the spaces between spoken digits; a split group must still join.
    assert S.find_code("el 472 913", "472913")
