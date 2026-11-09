"""End-to-end turn test: stream a WAV into /ws and record what comes back.

    docker run --rm -i --network voz-net -e COOKIE=voz_session=... \
        -v nowhere:/x python:3.12-slim sh -c 'pip install -q websockets && python -' < tests/turn.py

Prints a latency timeline and writes the returned audio to out.pcm (24 kHz PCM16).
"""

import asyncio
import json
import os
import sys
import time
import wave

import websockets

URL = os.environ.get("URL", "ws://voz:9000/ws")
COOKIE = os.environ.get("COOKIE", "")
WAV = os.environ.get("WAV", "speech.wav")
OUT = os.environ.get("OUT", "out.pcm")
OUT_RATE = 24000


async def main() -> int:
    with wave.open(WAV, "rb") as handle:
        assert handle.getframerate() == 16000, handle.getframerate()
        audio = handle.readframes(handle.getnframes())

    started = time.time()
    events = []
    audio_chunks = []
    first_audio = None
    done = asyncio.Event()

    async with websockets.connect(URL, additional_headers={"Cookie": COOKIE}, max_size=None) as ws:
        ready = json.loads(await ws.recv())
        events.append((time.time() - started, "ready", ready))
        if os.environ.get("VOICE"):
            await ws.send(json.dumps({"t": "voice", "v": os.environ["VOICE"]}))

        async def sender():
            frame = 640                      # 20 ms of 16 kHz PCM
            for offset in range(0, len(audio), frame):
                await ws.send(audio[offset:offset + frame])
                await asyncio.sleep(0.02)
            silence = b"\x00\x00" * 320
            for _ in range(90):              # 1.8 s of silence closes the utterance
                await ws.send(silence)
                await asyncio.sleep(0.02)
            await asyncio.sleep(20)

        async def receiver():
            nonlocal first_audio
            async for message in ws:
                now = time.time() - started
                if isinstance(message, bytes):
                    if first_audio is None:
                        first_audio = now
                        events.append((now, "first_audio", None))
                    audio_chunks.append(message)
                    continue
                payload = json.loads(message)
                events.append((now, payload.get("t"), payload))
                if payload.get("t") == "state" and payload.get("v") == "listen" and first_audio:
                    done.set()
                    return

        sending = asyncio.create_task(sender())
        receiving = asyncio.create_task(receiver())
        try:
            await asyncio.wait_for(done.wait(), timeout=90)
        except asyncio.TimeoutError:
            events.append((time.time() - started, "TIMEOUT", None))
        sending.cancel()

    audio_bytes = b"".join(audio_chunks)
    if audio_bytes:
        with open(OUT, "wb") as handle:
            handle.write(audio_bytes)

    for stamp, kind, payload in events:
        detail = ""
        if isinstance(payload, dict):
            detail = {k: v for k, v in payload.items() if k in ("text", "v", "msg")}
        print(f"{stamp:7.2f}s  {kind:12} {detail}")
    print(f"audio received: {len(audio_bytes)/2/OUT_RATE:.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
