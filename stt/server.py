"""Local speech-to-text: faster-whisper on the CPU, Spanish, no key, no network.

    POST /transcribe?language=es&model=base   body = 16 kHz mono PCM WAV
    GET  /healthz                             -> {"ok": true, "models": [...]}
    GET  /models

The client in this stack always sends 16 kHz mono 16-bit WAV, so the fast path is
a plain `wave` read. Anything else falls back to ffmpeg.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

MODEL_DIR = os.environ.get("STT_MODEL_DIR", "/models")
MODELS = [m.strip() for m in os.environ.get("STT_MODELS", "base,small").split(",") if m.strip()]
DEFAULT_MODEL = os.environ.get("STT_MODEL", MODELS[0] if MODELS else "base")
DEFAULT_LANGUAGE = os.environ.get("STT_LANGUAGE", "es")
COMPUTE = os.environ.get("STT_COMPUTE_TYPE", "int8")
PORT = int(os.environ.get("STT_PORT", "9000"))
PRELOAD = os.environ.get("STT_PRELOAD", "true").lower() in ("1", "true", "yes")
MAX_BYTES = int(os.environ.get("STT_MAX_BYTES", str(24 * 1024 * 1024)))
FFMPEG = os.environ.get("STT_FFMPEG", "ffmpeg")

_models: dict[str, object] = {}
_load_lock = threading.Lock()
_run_lock = threading.Lock()


def load(name: str):
    from faster_whisper import WhisperModel

    if name not in _models:
        with _load_lock:
            if name not in _models:
                started = time.time()
                local = os.path.join(MODEL_DIR, name)
                source = local if os.path.isdir(local) else name
                _models[name] = WhisperModel(source, device="cpu", compute_type=COMPUTE,
                                             download_root=MODEL_DIR, cpu_threads=2,
                                             local_files_only=os.path.isdir(local))
                sys.stderr.write(f"stt: loaded {name} in {time.time()-started:.1f}s\n")
    return _models[name]


def clean_language(value: str | None) -> str:
    candidate = str(value or "").strip().lower()
    return candidate if re.fullmatch(r"[a-z]{2,3}(-[a-z]{2,4})?", candidate) else DEFAULT_LANGUAGE


def decode(body: bytes, content_type: str) -> np.ndarray:
    """Bytes -> float32 mono samples at 16 kHz."""
    if body[:4] == b"RIFF":
        with tempfile.TemporaryDirectory(prefix="stt-") as workdir:
            path = os.path.join(workdir, "in.wav")
            with open(path, "wb") as handle:
                handle.write(body)
            try:
                return read_wav(path)
            except ValueError:
                target = os.path.join(workdir, "out.wav")
                convert(path, target)
                return read_wav(target)
    with tempfile.TemporaryDirectory(prefix="stt-") as workdir:
        path = os.path.join(workdir, "in.bin")
        with open(path, "wb") as handle:
            handle.write(body)
        target = os.path.join(workdir, "out.wav")
        convert(path, target)
        return read_wav(target)


def convert(source: str, target: str) -> None:
    subprocess.run(
        [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", source,
         "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", "-f", "wav", target],
        check=True, capture_output=True, timeout=120,
    )


def read_wav(path: str) -> np.ndarray:
    with wave.open(path, "rb") as handle:
        if handle.getsampwidth() != 2:
            raise ValueError("expected 16-bit audio")
        if handle.getframerate() != 16000:
            raise ValueError(f"expected 16 kHz audio, got {handle.getframerate()} Hz")
        channels = handle.getnchannels()
        frames = handle.readframes(handle.getnframes())
    samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    if samples.size == 0:
        raise ValueError("the recording is silent")
    return samples


def transcribe(body: bytes, content_type: str, language: str, name: str,
               prompt: str | None = None, beam: int = 1) -> dict:
    if not body:
        raise ValueError("no audio was uploaded")
    if len(body) > MAX_BYTES:
        raise ValueError("the recording is too large")
    name = name if name in MODELS else DEFAULT_MODEL
    started = time.time()
    audio = decode(body, content_type)
    duration = len(audio) / 16000.0
    model = load(name)
    with _run_lock:
        segments, _info = model.transcribe(
            audio, language=clean_language(language), beam_size=max(1, min(5, beam)),
            vad_filter=True, condition_on_previous_text=False,
            without_timestamps=True, initial_prompt=prompt or None,
        )
        text = " ".join(segment.text.strip() for segment in segments).strip()
    return {"text": text, "model": name, "duration": round(duration, 2),
            "seconds": round(time.time() - started, 2)}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "voz-stt/1.0"

    def log_message(self, fmt, *args):  # noqa: A002
        sys.stderr.write("stt: " + (fmt % args) + "\n")

    def reply(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        route = urllib.parse.urlparse(self.path).path
        if route == "/healthz":
            return self.reply(200, {"ok": True, "models": MODELS, "loaded": list(_models),
                                    "default": DEFAULT_MODEL})
        if route == "/models":
            return self.reply(200, {"models": MODELS, "default": DEFAULT_MODEL})
        return self.reply(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        route = urllib.parse.urlparse(self.path).path
        if route not in ("/transcribe", "/v1/audio/transcriptions"):
            return self.reply(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            language = query.get("language", [DEFAULT_LANGUAGE])[0]
            name = query.get("model", [DEFAULT_MODEL])[0]
            prompt = query.get("prompt", [None])[0]
            beam = int(query.get("beam", ["1"])[0])
            result = transcribe(body, self.headers.get("Content-Type", ""), language, name,
                                prompt=prompt, beam=beam)
            sys.stderr.write(f"stt: {result['model']} {result['duration']}s audio in "
                             f"{result['seconds']}s rtf={result['seconds']/max(result['duration'],0.01):.2f}\n")
            return self.reply(200, result)
        except ValueError as error:
            return self.reply(400, {"error": str(error)})
        except Exception as error:  # noqa: BLE001
            self.log_message("transcription failed: %r", error)
            return self.reply(500, {"error": f"transcription failed: {error}"})


def main() -> int:
    if PRELOAD:
        for name in MODELS:
            load(name)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)  # noqa: S104
    sys.stderr.write(f"stt: listening on {PORT} with {MODELS} ({COMPUTE})\n")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
