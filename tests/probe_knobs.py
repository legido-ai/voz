"""Which knob turns off deepseek's thinking on OpenCode Go, and what it costs in TTFT."""

import json
import os
import time
import urllib.request
import urllib.error

BASE = os.environ.get("LLM_BASE", "https://opencode.ai/zen/go/v1")
MODEL = os.environ.get("LLM_MODEL", "deepseek-v4.1-flash")
KEY = os.environ.get("OPENCODE_GO_API_KEY", "")

SYSTEM = "Asistente de voz en español. Responde en una frase corta, sin markdown."

VARIANTS = {
    "baseline": {},
    "reasoning_effort=low": {"reasoning_effort": "low"},
    "reasoning_effort=minimal": {"reasoning_effort": "minimal"},
    "reasoning_effort=none": {"reasoning_effort": "none"},
    "thinking_disabled": {"thinking": {"type": "disabled"}},
    "enable_thinking=false": {"enable_thinking": False},
    "chat_template_kwargs": {"chat_template_kwargs": {"thinking": False}},
}


def probe(extra: dict) -> str:
    body = json.dumps({
        "model": MODEL,
        "stream": True,
        "max_tokens": 900,
        "temperature": 0.6,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": "Dame una idea rápida para cenar sin cocinar."},
        ],
        **extra,
    }).encode()
    request = urllib.request.Request(
        f"{BASE}/chat/completions", data=body,
        headers={
            "Authorization": f"Bearer {KEY}",
            "Content-Type": "application/json",
            "x-opencode-session": "voz-probe-knobs",
            "User-Agent": "voz/1.0 (OpenAI/Python 1.40.0)",
            "Accept": "text/event-stream",
        },
    )
    started = time.time()
    first = None
    reasoning = 0
    chars = []
    try:
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
                    chars.append(delta["content"])
    except urllib.error.HTTPError as error:
        return f"HTTP {error.code}: {error.read()[:160].decode('utf-8', 'replace')}"
    total = time.time() - started
    return f"ttft={first:.2f}s total={total:.2f}s reasoning={reasoning} chars={len(''.join(chars))}"


for name, extra in VARIANTS.items():
    print(f"{name:26} {probe(extra)}", flush=True)
