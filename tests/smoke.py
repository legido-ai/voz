"""Smoke test for the two voice services, run inside the voz-net network.

    docker run --rm -i --network voz-net python:3.12-slim python - < tests/smoke.py
"""

import io
import json
import time
import urllib.request
import wave

TTS = "http://voz-tts:9000"
STT = "http://voz-stt:9000"
PHRASE = "¿Qué tiempo hará mañana en Madrid?"


def post_json(url, payload, stream=False):
    body = json.dumps(payload).encode()
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    started = time.time()
    response = urllib.request.urlopen(request, timeout=120)
    first = None
    chunks = []
    while True:
        chunk = response.read(9600)
        if not chunk:
            break
        if first is None:
            first = time.time() - started
        chunks.append(chunk)
    return first, time.time() - started, b"".join(chunks), response.headers


first, total, pcm, headers = post_json(f"{TTS}/speak", {"text": PHRASE, "voice": "es_f"})
rate = int(headers.get("X-Sample-Rate", 24000))
seconds = len(pcm) / 2 / rate
print(f"tts: first={first:.2f}s total={total:.2f}s audio={seconds:.2f}s rtf={total/seconds:.2f} rate={rate}")

buffer = io.BytesIO()
with wave.open(buffer, "wb") as handle:
    handle.setnchannels(1)
    handle.setsampwidth(2)
    handle.setframerate(rate)
    handle.writeframes(pcm)
wav = buffer.getvalue()

request = urllib.request.Request(
    f"{STT}/transcribe?language=es", data=wav, headers={"Content-Type": "audio/wav"}
)
started = time.time()
text = json.loads(urllib.request.urlopen(request, timeout=180).read())
print(f"stt: {time.time()-started:.2f}s -> {text}")
print("match:", PHRASE.strip("¿?").lower() in text["text"].lower())
