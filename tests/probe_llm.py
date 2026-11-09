"""Measure the LLM leg: TTFT, visible deltas, reasoning noise, total time."""

import json
import os
import time
import urllib.request

BASE = os.environ.get("LLM_BASE", "https://opencode.ai/zen/go/v1")
MODEL = os.environ.get("LLM_MODEL", "deepseek-v4.1-flash")
KEY = os.environ.get("OPENCODE_GO_API_KEY", "")

SYSTEM = (
    "Eres un asistente de voz en español de España. Hablas, no escribes: nada de markdown, "
    "listas, asteriscos ni emojis. Responde en una o dos frases cortas."
)

body = json.dumps({
    "model": MODEL,
    "stream": True,
    "max_tokens": 900,
    "temperature": 0.6,
    "messages": [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": "¿Qué me recomiendas cenar si tengo prisa y no quiero cocinar?"},
    ],
}).encode()

request = urllib.request.Request(
    f"{BASE}/chat/completions",
    data=body,
    headers={
        "Authorization": f"Bearer {KEY}",
        "Content-Type": "application/json",
        "x-opencode-session": "voz-probe-0001",
        "User-Agent": "voz/1.0 (OpenAI/Python 1.40.0)",
        "Accept": "text/event-stream",
    },
)

started = time.time()
first = None
visible = []
reasoning = 0
with urllib.request.urlopen(request, timeout=120) as response:
    for raw in response:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        choices = event.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        if delta.get("reasoning_content"):
            reasoning += 1
        if delta.get("content"):
            if first is None:
                first = time.time() - started
            visible.append(delta["content"])

total = time.time() - started
text = "".join(visible)
print(f"http: 200  ttft={first:.2f}s total={total:.2f}s reasoning_deltas={reasoning}")
print(f"chars={len(text)} words={len(text.split())}")
print("answer:", text[:400])
