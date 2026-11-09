"""Text-to-speech over HTTP: Kokoro-82M (natural) and Piper (fast), CPU only.

POST /speak   {"text": "...", "voice": "es_f", "speed": 1.0}
              -> 200, chunked raw PCM16LE mono at 24 kHz (X-Sample-Rate header)
GET  /healthz -> {"ok": true, "voices": [...], "engines": {...}}
GET  /voices  -> the voice table

Both engines are open source and run inside this container: no vendor API and no
network at request time. Kokoro-82M is Apache-2.0; Piper voices carry their own
licence (es_ES-sharvard-medium is CC BY 4.0) and the engine is GPL-3.0.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

OUT_RATE = 24000

KOKORO_MODEL = os.environ.get("TTS_MODEL_PATH", "/models/kokoro-v1.0.int8.onnx")
KOKORO_VOICES = os.environ.get("TTS_VOICES_PATH", "/models/voices-v1.0.bin")
PIPER_DIR = os.environ.get("TTS_PIPER_DIR", "/models/piper")
PORT = int(os.environ.get("TTS_PORT", "9000"))
DEFAULT_VOICE = os.environ.get("TTS_DEFAULT_VOICE", "es_f")
PRELOAD = os.environ.get("TTS_PRELOAD", "true").lower() in ("1", "true", "yes")
MAX_INPUT = int(os.environ.get("TTS_MAX_INPUT", "1200"))
SPEED = float(os.environ.get("TTS_SPEED", "1.0"))

# voice id -> (engine, model, kokoro language | piper speaker id)
VOICES: dict[str, tuple[str, str, object]] = {
    "es_f": ("kokoro", "ef_dora", "es"),
    "es_m": ("kokoro", "em_alex", "es"),
    "es_m2": ("kokoro", "em_santa", "es"),
    "es_piper_f": ("piper", "es_ES-sharvard-medium", 1),
    "es_piper_m": ("piper", "es_ES-sharvard-medium", 0),
}

_kokoro = None
_piper: dict[str, object] = {}
_model_lock = threading.Lock()
_synth_lock = threading.Lock()

_JUNK = re.compile(r"[*_#`~|]+|<[^>]+>")
_EMOJI = re.compile(
    "[" "\U0001F300-\U0001FAFF" "\U00002600-\U000027BF" "\U0001F000-\U0001F2FF" "\uFE0F" "]+",
    flags=re.UNICODE,
)


def clean(text: str) -> str:
    text = _EMOJI.sub(" ", text or "")
    text = _JUNK.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:MAX_INPUT]


def _trim(samples: np.ndarray, margin: int = 1200) -> np.ndarray:
    """Strip leading/trailing silence, keeping a small margin around the speech."""
    if samples.size == 0:
        return samples
    loud = np.flatnonzero(np.abs(samples) > 0.006)
    if loud.size == 0:
        return samples
    start = max(0, int(loud[0]) - margin)
    end = min(samples.size, int(loud[-1]) + margin)
    return samples[start:end]


def _resample(samples: np.ndarray, rate: int, target: int = OUT_RATE) -> np.ndarray:
    if rate == target or samples.size == 0:
        return samples
    positions = np.arange(0, samples.size, rate / target)
    return np.interp(positions, np.arange(samples.size), samples).astype(np.float32)


def resolve(voice: str | None) -> tuple[str, str, object]:
    return VOICES.get((voice or DEFAULT_VOICE).strip(), VOICES[DEFAULT_VOICE])


def load_kokoro():
    global _kokoro
    if _kokoro is None:
        from kokoro_onnx import Kokoro

        with _model_lock:
            if _kokoro is None:
                started = time.time()
                _kokoro = Kokoro(KOKORO_MODEL, KOKORO_VOICES)
                sys.stderr.write(f"tts: kokoro {os.path.basename(KOKORO_MODEL)} "
                                 f"loaded in {time.time()-started:.1f}s\n")
    return _kokoro


def load_piper(model: str):
    if model not in _piper:
        from piper.voice import PiperVoice

        with _model_lock:
            if model not in _piper:
                started = time.time()
                path = os.path.join(PIPER_DIR, f"{model}.onnx")
                if not os.path.exists(path):
                    raise RuntimeError(f"piper voice is missing: {path}")
                _piper[model] = PiperVoice.load(path, config_path=path + ".json")
                sys.stderr.write(f"tts: piper {model} loaded in {time.time()-started:.1f}s\n")
    return _piper[model]


def synth(text: str, voice: str | None, speed: float) -> np.ndarray:
    """Text -> float32 mono samples at OUT_RATE."""
    engine, model, extra = resolve(voice)
    if engine == "kokoro":
        kokoro = load_kokoro()
        try:
            samples, rate = kokoro.create(text, voice=model, speed=speed, lang=str(extra))
        except ValueError:
            # kokoro-onnx 0.6.1 crashes in its own trimmer on very short
            # utterances ("Vale."): synthesize untrimmed and trim here instead.
            samples, rate = kokoro.create(text, voice=model, speed=speed, lang=str(extra),
                                          trim=False)
            samples = _trim(np.asarray(samples, dtype=np.float32))
    else:
        from piper.config import SynthesisConfig

        piper = load_piper(model)
        try:
            config = SynthesisConfig(speaker_id=int(extra), length_scale=1.0 / max(0.5, speed))
        except TypeError:                     # older SynthesisConfig without length_scale
            config = SynthesisConfig(speaker_id=int(extra))
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as handle:
            piper.synthesize_wav(text, handle, syn_config=config)
        buffer.seek(0)
        with wave.open(buffer, "rb") as handle:
            rate = handle.getframerate()
            frames = handle.readframes(handle.getnframes())
        samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    samples = np.asarray(samples, dtype=np.float32)
    if samples.size == 0:
        raise ValueError("the model produced no audio")
    return np.clip(_resample(samples, rate), -1.0, 1.0)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "voz-tts/1.1"

    def log_message(self, fmt, *args):  # noqa: A002 - stdlib signature
        sys.stderr.write("tts: " + (fmt % args) + "\n")

    def json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 - stdlib naming
        route = urllib.parse.urlparse(self.path).path
        if route == "/healthz":
            return self.json(200, {
                "ok": True,
                "voices": sorted(VOICES),
                "default_voice": DEFAULT_VOICE,
                "engines": {"kokoro": _kokoro is not None, "piper": sorted(_piper)},
            })
        if route == "/voices":
            return self.json(200, {"voices": sorted(VOICES), "default": DEFAULT_VOICE})
        return self.json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802 - stdlib naming
        route = urllib.parse.urlparse(self.path).path
        if route != "/speak":
            return self.json(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception as error:  # noqa: BLE001
            return self.json(400, {"error": f"bad json: {error}"})

        text = clean(payload.get("text", ""))
        if not text:
            return self.json(400, {"error": "empty text"})
        voice_name, speed = payload.get("voice"), float(payload.get("speed") or SPEED)

        try:
            started = time.time()
            with _synth_lock:
                pcm = (synth(text, voice_name, speed) * 32767.0).astype("<i2").tobytes()
        except Exception as error:  # noqa: BLE001 - never kill the service
            self.log_message("synthesis failed: %r", error)
            return self.json(500, {"error": f"synthesis failed: {error}"})

        sys.stderr.write(
            f"tts: {len(text)} chars -> {len(pcm)/2/OUT_RATE:.2f}s audio "
            f"in {time.time()-started:.2f}s voice={resolve(voice_name)[1]}\n"
        )
        self.send_response(200)
        self.send_header("Content-Type", "audio/L16")
        self.send_header("X-Sample-Rate", str(OUT_RATE))
        self.send_header("X-Voice", str(resolve(voice_name)[1]))
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        step = 4800 * 2                       # 200 ms of 24 kHz PCM per chunk
        try:
            for offset in range(0, len(pcm), step):
                chunk = pcm[offset:offset + step]
                self.wfile.write(b"%X\r\n" % len(chunk) + chunk + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass                              # the client hung up: the user interrupted


def main() -> int:
    if PRELOAD:
        load_kokoro()
        load_piper("es_ES-sharvard-medium")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)  # noqa: S104 - container-internal
    sys.stderr.write(f"tts: listening on {PORT} with {len(VOICES)} voices\n")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
