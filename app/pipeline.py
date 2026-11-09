"""Voice pipeline primitives: VAD, sentence splitting, LLM and TTS clients.

Everything here is CPU-only and self-hosted. The single metered dependency is
the OpenCode Go subscription behind an OpenAI-compatible endpoint.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from typing import AsyncIterator

import httpx
import numpy as np

IN_RATE = 16000
FRAME_MS = 20
FRAME_SAMPLES = IN_RATE * FRAME_MS // 1000

STT_URL = os.environ.get("STT_URL", "").rstrip("/")   # whisper fallback, optional
TTS_URL = os.environ.get("TTS_URL", "http://voz-tts:9000").rstrip("/")

LLM_BASE = os.environ.get("LLM_BASE", "https://opencode.ai/zen/go/v1").rstrip("/")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-v4.1-flash")
LLM_KEY = os.environ.get("OPENCODE_GO_API_KEY", "")
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "900"))
LLM_SESSION = os.environ.get("LLM_SESSION") or f"voz-{uuid.uuid4().hex[:12]}"

VOICE = os.environ.get("VOZ_VOICE", "es_f")

SYSTEM_PROMPT = (
    "Eres un asistente de voz en español de España. Hablas, no escribes: todo lo que dices "
    "se convierte en audio, así que no uses markdown, ni listas, ni asteriscos, ni emojis, "
    "ni tablas, ni enlaces, ni código. No empieces diciendo 'claro' o 'por supuesto' en cada "
    "respuesta, varía. Responde en una o dos frases cortas, como mucho tres, y no repitas la "
    "pregunta del usuario. Si un dato no lo sabes con seguridad, dilo en una frase. Si te piden "
    "algo largo, da primero lo esencial y ofrece ampliar. Tono cercano, natural y directo. "
    "Nunca menciones que eres un modelo ni que estás hecho de texto."
)


# --------------------------------------------------------------------------- VAD


class VAD:
    """Energy VAD with a noise floor that adapts to the room.

    Deliberately simple: it runs on 20 ms frames of 16 kHz PCM and only has to
    answer "is this speech, and has it stopped?".
    """

    def __init__(
        self,
        threshold: float = 0.011,
        barge_threshold: float = 0.028,
        start_ms: int = 140,
        end_ms: int = 500,
        max_ms: int = 25000,
        preroll_ms: int = 200,
    ) -> None:
        self.threshold = threshold
        self.barge_threshold = barge_threshold
        self.start_frames = max(1, start_ms // FRAME_MS)
        self.end_frames = max(1, end_ms // FRAME_MS)
        self.max_frames = max_ms // FRAME_MS
        self.preroll = max(1, preroll_ms // FRAME_MS)
        self.floor = 0.0035
        self.reset()

    def reset(self) -> None:
        self.frames: list[np.ndarray] = []
        self.rolling: list[np.ndarray] = []
        self.in_speech = False
        self.speech_run = 0
        self.silent_run = 0
        self.level = 0.0

    def push(self, frame: np.ndarray, speaking: bool) -> str | None:
        """Feed one 20 ms frame. Returns 'start', 'end', or None."""
        rms = float(np.sqrt(np.mean(np.square(frame, dtype=np.float32))))
        self.level = rms
        if not self.in_speech:
            # Track the quiet floor slowly, and only from frames that are quiet.
            if rms < self.floor * 2.5 or rms < self.threshold * 0.6:
                self.floor = 0.995 * self.floor + 0.005 * rms
            self.rolling.append(frame)
            if len(self.rolling) > self.preroll:
                self.rolling.pop(0)
            gate = max(self.barge_threshold if speaking else self.threshold,
                       self.floor * (5.0 if speaking else 3.2))
            if rms > gate:
                self.speech_run += 1
                if self.speech_run >= self.start_frames:
                    self.in_speech = True
                    self.frames = list(self.rolling)
                    self.silent_run = 0
                    self.rolling = []
                    return "start"
            else:
                self.speech_run = 0
            return None

        self.frames.append(frame)
        release = max(self.threshold * 0.55, self.floor * 2.0)
        if rms > release:
            self.silent_run = 0
        else:
            self.silent_run += 1
        if self.silent_run >= self.end_frames or len(self.frames) >= self.max_frames:
            return "end"
        return None

    def close(self) -> np.ndarray:
        """End the utterance now and hand back the audio collected so far."""
        frames = self.frames
        if self.silent_run:
            frames = frames[: max(0, len(frames) - self.silent_run)]
        self.reset()
        if not frames:
            return np.zeros(0, dtype=np.float32)
        audio = np.concatenate(frames)
        return audio


# ------------------------------------------------------------------- text helpers

_MD = re.compile(r"(\*\*|__|\*|_|`|#+|>|~~|\|)")
_EMOJI = re.compile(
    "[" "\U0001F300-\U0001FAFF" "\U00002600-\U000027BF" "\U0001F000-\U0001F2FF" "\uFE0F" "]+"
)
_SENT_END = re.compile(r"[.!?…]+(?=\s|$)")


def speakable(text: str) -> str:
    text = _EMOJI.sub(" ", text or "")
    text = _MD.sub(" ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"^\s*[-•]\s*", "", text, flags=re.M)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def split_sentences(buffer: str, flush: bool = False, first: bool = False) -> tuple[list[str], str]:
    """Cut a streaming answer into speakable sentences as they complete.

    `first` makes the very first cut sooner (at a comma) so the voice starts
    talking while the rest of the answer is still being generated.
    """
    out: list[str] = []
    if first and not _SENT_END.search(buffer) and len(buffer) > 34:
        cut = max(buffer.rfind(",", 12, 44), buffer.rfind(":", 12, 44), buffer.rfind(";", 12, 44))
        if cut > 0:
            out.append(buffer[: cut + 1].strip())
            buffer = buffer[cut + 1:].lstrip()
    while True:
        match = _SENT_END.search(buffer)
        if not match:
            break
        sentence = buffer[: match.end()].strip()
        buffer = buffer[match.end():].lstrip()
        if len(speakable(sentence)) >= 2:
            out.append(sentence)
    # A long clause with no full stop yet: speak it at a comma rather than wait.
    while len(buffer) > 180:
        cut = buffer.rfind(",", 60, 180)
        if cut < 0:
            cut = buffer.rfind(" ", 60, 180)
        if cut < 0:
            break
        out.append(buffer[: cut + 1].strip())
        buffer = buffer[cut + 1:].lstrip()
    if flush and buffer.strip():
        out.append(buffer.strip())
        buffer = ""
    return out, buffer


# --------------------------------------------------------------------------- HTTP

_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0),
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
        )
    return _client


async def aclose() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def pcm_to_wav(audio: np.ndarray, rate: int = IN_RATE) -> bytes:
    import io
    import wave

    pcm = np.clip(audio, -1.0, 1.0)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes((pcm * 32767.0).astype("<i2").tobytes())
    return buffer.getvalue()


async def transcribe(audio: np.ndarray) -> str:
    """Audio in, Spanish text out, via the local faster-whisper service."""
    body = pcm_to_wav(audio)
    response = await client().post(
        f"{STT_URL}/transcribe",
        params={"language": "es"},
        content=body,
        headers={"Content-Type": "audio/wav"},
    )
    response.raise_for_status()
    return speakable(response.json().get("text", ""))


async def stream_reply(messages: list[dict]) -> AsyncIterator[str]:
    """Stream visible answer text from OpenCode Go, dropping reasoning deltas."""
    if not LLM_KEY:
        raise RuntimeError("OPENCODE_GO_API_KEY is not set")
    payload = {
        "model": LLM_MODEL,
        "stream": True,
        "max_tokens": LLM_MAX_TOKENS,
        "temperature": 0.6,
        # Measured on this relay: without these the model spends 0.7-1.0 s on
        # reasoning deltas before the first visible token (TTFT 2.2-2.5 s);
        # with them TTFT is 1.0-1.7 s and no reasoning is emitted.
        "chat_template_kwargs": {"thinking": False},
        "reasoning_effort": "none",
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}, *messages[-12:]],
    }
    headers = {
        "Authorization": f"Bearer {LLM_KEY}",
        "Content-Type": "application/json",
        "x-opencode-session": LLM_SESSION,
        "User-Agent": "voz/1.0 (OpenAI/Python 1.40.0)",
        "Accept": "text/event-stream",
    }
    async with client().stream(
        "POST", f"{LLM_BASE}/chat/completions", json=payload, headers=headers
    ) as response:
        if response.status_code != 200:
            detail = (await response.aread()).decode("utf-8", "replace")[:300]
            raise RuntimeError(f"llm {response.status_code}: {detail}")
        async for line in response.aiter_lines():
            if not line or not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if chunk in ("[DONE]", ""):
                if chunk == "[DONE]":
                    break
                continue
            try:
                event = json.loads(chunk)
            except json.JSONDecodeError:
                continue
            choices = event.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            text = delta.get("content")
            if text:
                yield text


async def synth(text: str, voice: str = VOICE, speed: float = 1.0) -> AsyncIterator[bytes]:
    """One sentence in, chunks of 24 kHz PCM16 out, as fast as they are made."""
    async with client().stream(
        "POST", f"{TTS_URL}/speak", json={"text": text, "voice": voice, "speed": speed}
    ) as response:
        if response.status_code != 200:
            detail = (await response.aread()).decode("utf-8", "replace")[:200]
            raise RuntimeError(f"tts {response.status_code}: {detail}")
        async for chunk in response.aiter_bytes(9600):
            if chunk:
                yield chunk


async def healthy(url: str) -> bool:
    try:
        response = await client().get(f"{url}/healthz", timeout=3.0)
        return response.status_code == 200
    except Exception:  # noqa: BLE001 - a probe
        return False
