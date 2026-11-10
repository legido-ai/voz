"""Behavioural tests for the relay: session gating and Hermes routing (no network)."""

import importlib.util
import os
import sys

import pytest

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)

# pipeline imports numpy/httpx only; that is enough to prove the routing decision.
spec = importlib.util.spec_from_file_location("voz_pipeline", os.path.join(APP, "pipeline.py"))
pipeline = importlib.util.module_from_spec(spec)
sys.modules["voz_pipeline"] = pipeline
spec.loader.exec_module(pipeline)


def test_relay_points_at_hermes_not_a_model_provider(monkeypatch):
    assert "opencode.ai" not in pipeline.LLM_BASE
    assert "/v1" in pipeline.LLM_BASE or "8642" in pipeline.LLM_BASE


def test_missing_hermes_key_fails_loudly(monkeypatch):
    monkeypatch.setattr(pipeline, "LLM_KEY", "")
    with pytest.raises(RuntimeError) as error:
        import asyncio
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            pipeline.stream_reply([{"role": "user", "content": "hola"}]).__anext__()
        )
    assert "HERMES_API_KEY" in str(error.value)


def test_auth_messages_are_exactly_the_two_lines():
    source = open(os.path.join(APP, "main.py"), encoding="utf-8").read()
    assert 'MSG_AUTH = "Necesitas autenticarte."' in source
    assert 'MSG_FAIL = "Error en la autenticación."' in source
    # A1: no hint about Telegram or the code in what the app says.
    for forbidden in ("Telegram", "código de seis", "mira tu"):
        assert forbidden not in source.split("MSG_FAIL")[0].split("MSG_AUTH")[-1]


def test_password_auth_is_gone():
    source = open(os.path.join(APP, "main.py"), encoding="utf-8").read()
    for gone in ("authdb", "PASS_HASH", "VOZ_PASS_HASH", "def _valid", "def _token"):
        assert gone not in source
    assert "MFA.verify" in source and "MFA.session_ok" in source


def test_websocket_requires_a_live_session():
    source = open(os.path.join(APP, "main.py"), encoding="utf-8").read()
    ws = source.split('@app.websocket("/ws")', 1)[1].split("@app.on_event", 1)[0]
    assert "MFA.session_ok" in ws and "4401" in ws


def test_verify_maps_rejections_to_401():
    source = open(os.path.join(APP, "main.py"), encoding="utf-8").read()
    body = source.split("async def auth_verify", 1)[1].split("@app.get", 1)[0]
    assert "status_code=401" in body
    assert "set_cookie" in body                      # a success mints the session


def test_a_wrong_code_reports_the_exact_failure_line():
    source = open(os.path.join(APP, "main.py"), encoding="utf-8").read()
    body = source.split("async def auth_verify", 1)[1].split("@app.get", 1)[0]
    assert '"message": MSG_FAIL' in body
    assert 'MSG_FAIL = "Error en la autenticación."' in source


def test_sign_off_marker_is_never_spoken():
    assert pipeline.speakable("Cadena de voz operativa. ✅ I'm done") == "Cadena de voz operativa."
    assert pipeline.speakable("Listo, he terminado.") == "Listo, he terminado."


def test_challenge_is_rate_limited_and_reused_per_client():
    source = open(os.path.join(APP, "main.py"), encoding="utf-8").read()
    body = source.split("async def auth_challenge", 1)[1].split('@app.post("/api/auth/verify"', 1)[0]
    assert "CHALLENGE_MAX" in body and "429" in body
    # a repeat inside the reuse window must not mint or push again
    assert "reused" in body and "CHALLENGE_REUSE" in body


def test_client_does_not_mint_a_challenge_on_load():
    js = open(os.path.join(APP, "static", "app.js"), encoding="utf-8").read()
    boot = js.rsplit("})();", 2)[0][-600:]
    assert "needChallenge()" not in boot
    assert "checkSession()" in boot


def test_a_suppressed_push_is_not_cached_as_reusable():
    source = open(os.path.join(APP, "main.py"), encoding="utf-8").read()
    body = source.split("async def auth_challenge", 1)[1].split('@app.post("/api/auth/verify"', 1)[0]
    assert "if created.sent:" in body
