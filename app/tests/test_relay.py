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
    tail = js[-400:]                     # the boot block runs last
    assert "needChallenge()" not in tail.split("setState('idle'", 1)[0]
    assert "setState('idle', 'Toca y habla')" in tail


def test_a_suppressed_push_is_not_cached_as_reusable():
    source = open(os.path.join(APP, "main.py"), encoding="utf-8").read()
    body = source.split("async def auth_challenge", 1)[1].split('@app.post("/api/auth/verify"', 1)[0]
    assert "if created.sent:" in body


def test_authenticated_client_can_resend_the_request_as_text():
    source = open(os.path.join(APP, "main.py"), encoding="utf-8").read()
    assert 'kind == "text"' in source
    js = open(os.path.join(APP, "static", "app.js"), encoding="utf-8").read()
    assert "function stripCode" in js and "t: 'text'" in js


def test_websocket_accepts_before_authentication_but_turns_are_gated():
    source = open(os.path.join(APP, "main.py"), encoding="utf-8").read()
    ws = source.split('@app.websocket("/ws")', 1)[1].split("@app.on_event", 1)[0]
    # The socket must open without a session (the code is spoken through it)...
    assert "await ws.accept()" in ws
    assert "ws.close(code=4401)" not in ws
    # ...while every turn still re-checks the session.
    turn = source.split("async def start_turn", 1)[1].split("async def respond", 1)[0]
    assert "MFA.session_ok" in turn


def test_vad_close_pause_is_long_enough_for_a_natural_break():
    source = open(os.path.join(APP, "pipeline.py"), encoding="utf-8").read()
    assert "end_ms: int = 900" in source or 'VOZ_END_MS' in source


def test_auth_expired_keeps_the_microphone_open():
    js = open(os.path.join(APP, "static", "app.js"), encoding="utf-8").read()
    block = js.split("case 'auth_expired':", 1)[1].split("case 'voice'", 1)[0]
    assert "stop(" not in block
    assert "setState('listen'" in block


def test_unauthenticated_turn_still_reports_the_transcript():
    source = open(os.path.join(APP, "main.py"), encoding="utf-8").read()
    turn = source.split("async def start_turn", 1)[1].split("async def respond", 1)[0]
    assert '"t": "heard"' in turn          # the client needs it to verify the code
    assert "auth_expired" in turn


def test_visibility_change_does_not_hang_up_and_socket_reconnects():
    js = open(os.path.join(APP, "static", "app.js"), encoding="utf-8").read()
    vis = js.split("'visibilitychange'", 1)[1].split("});", 1)[0]
    assert "stop(" not in vis                    # switching to Telegram must not end the call
    assert "reconnect()" in vis
    assert "function openSocket" in js and "pong" in js


def test_static_assets_are_cache_busted():
    source = open(os.path.join(APP, "main.py"), encoding="utf-8").read()
    assert "?v=" in source and "st_mtime" in source


def test_audio_context_is_created_before_any_await():
    js = open(os.path.join(APP, "static", "app.js"), encoding="utf-8").read()
    start = js.split("async function start()", 1)[1]
    ctx_at = start.index("new (window.AudioContext")
    # Nothing may be awaited before the AudioContext exists, or iOS mutes the mic
    # (checking statements, not the prose in the comment above them).
    assert not [l for l in start[:ctx_at].splitlines() if l.strip().startswith("await")]
    # The code is requested from the server's own verdict, never before the microphone
    # exists: 'ready' with authorised=false triggers it.
    js_all = js
    ready = js_all.split("case 'ready':", 1)[1].split("case 'state'", 1)[0]
    assert "ensureChallenge()" in ready
    assert "openSocket();" in start


def test_context_is_resumed_again_after_the_permission_prompt():
    js = open(os.path.join(APP, "static", "app.js"), encoding="utf-8").read()
    after_gum = js.split("getUserMedia({ audio: true", 1)[1].split("micSource =", 1)[0]
    assert "ctx.resume()" in after_gum      # iOS suspends it behind the prompt


def test_capture_uses_script_processor_not_the_worklet():
    js = open(os.path.join(APP, "static", "app.js"), encoding="utf-8").read()
    # WebKit hands zeros to the worklet while the analyser sees signal.
    assert "createScriptProcessor" in js
    assert "audioWorklet" not in js
    assert "function sendBlock" in js


def test_the_relay_verifies_the_spoken_code_itself():
    source = open(os.path.join(APP, "main.py"), encoding="utf-8").read()
    turn = source.split("async def start_turn", 1)[1].split("async def respond", 1)[0]
    assert "MFA.verify(text" in turn          # server-side, not a client round trip
    assert '"t": "session"' in turn
    js = open(os.path.join(APP, "static", "app.js"), encoding="utf-8").read()
    assert "case 'session':" in js
    assert "verify(message" not in js         # the client no longer posts the transcript


def test_a_reconnect_renews_the_idle_clock():
    source = open(os.path.join(APP, "main.py"), encoding="utf-8").read()
    ws = source.split('@app.websocket("/ws")', 1)[1].split("session = Session", 1)[0]
    assert "session_ok(token, touch=True)" in ws


def test_logout_revokes_the_session_server_side():
    source = open(os.path.join(APP, "main.py"), encoding="utf-8").read()
    body = source.split("async def logout", 1)[1].split("@app.get(\"/\"", 1)[0]
    assert "MFA.revoke" in body            # not just a cleared cookie


def test_hangup_keeps_the_session_and_logout_ends_it():
    js = open(os.path.join(APP, "static", "app.js"), encoding="utf-8").read()
    hangup = js.split("hangup.addEventListener", 1)[1].split("});", 1)[0]
    assert "stop(false)" in hangup            # ends the call...
    assert "endSession" not in hangup         # ...and keeps the session
    end = js.split("async function endSession", 1)[1].split("\n  }", 1)[0]
    assert "/logout" in end and "localStorage.removeItem" in end

    html = open(os.path.join(APP, "static", "index.html"), encoding="utf-8").read()
    assert 'id="logoutTop"' in html and 'hidden' in html.split('id="logoutTop"')[1].split(">")[0]


def test_a_code_only_utterance_is_acknowledged_with_one_word():
    js = open(os.path.join(APP, "static", "app.js"), encoding="utf-8").read()
    assert "t: 'ack'" in js                 # no assistant turn for a bare code
    source = open(os.path.join(APP, "main.py"), encoding="utf-8").read()
    assert '"Autenticado."' in source
    # the client-side stripper must know spoken digits, not just literal ones
    assert "cero|uno|una|dos|tres" in js


def test_code_only_detection():
    source = open(os.path.join(APP, "main.py"), encoding="utf-8").read()
    ns = {}
    start = source.index("_DIGITS = re.compile")
    end = source.index("app = FastAPI(", start) if source.index("app = FastAPI(", start) > start else source.index("@app.get", start)
    exec("import re\n" + source[start:end], ns)
    only = ns["_is_code_only"]
    assert only("cero, tres, tres, siete, cinco, ocho")
    assert only("codigo 033758")
    assert only("es 033 758")
    assert not only("cero, tres, tres, siete, cinco, ocho, dime la capital de España")
    assert not only("¿qué necesitas?")


def test_a_stalled_stream_never_hangs_the_turn():
    source = open(os.path.join(APP, "main.py"), encoding="utf-8").read()
    assert "FIRST_TOKEN_WAIT" in source
    assert "asyncio.create_task(pump())" in source   # cancel-free: wait_for on the generator closed it
    assert "Un momento." in source          # fills the silence of a slow tool turn
    assert "Esto está tardando demasiado." in source


def test_a_stalled_stream_falls_back_to_a_whole_answer():
    source = open(os.path.join(APP, "main.py"), encoding="utf-8").read()
    assert "P.complete(self.history)" in source
    pipeline = open(os.path.join(APP, "pipeline.py"), encoding="utf-8").read()
    assert "async def complete(" in pipeline
