# voz

Conversación por voz, sin texto: tocas un botón en el móvil, hablas, y una voz
natural te responde. Toda la voz es open source y corre en esta caja; la única
llamada externa es el LLM (OpenCode Go, `deepseek-v4.1-flash`).

- Web app (PWA instalable en iPhone). Nada de modelo en el teléfono: el móvil es
  micrófono y altavoz.
- STT: faster-whisper `base`/`small` int8, español, en `voz-stt`.
- TTS: Kokoro-82M int8 (Apache-2.0), voz `ef_dora`, en `voz-tts`.
- Orquestación: FastAPI + WebSocket en `voz`.

## Cómo funciona una frase

```
iPhone ──PCM16 16 kHz por WebSocket──▶ voz (VAD) ──WAV──▶ voz-stt (whisper)
                                          │
                                          ├──▶ OpenCode Go / deepseek-v4.1-flash (SSE, solo texto)
                                          │
                                          └──▶ voz-tts (Kokoro) ──PCM16 24 kHz──▶ iPhone
```

- El VAD es de energía, con suelo de ruido adaptativo: 140 ms para abrir turno,
  620 ms de silencio para cerrarlo.
- El LLM se consume en streaming y se corta en frases: la primera se sintetiza
  mientras el resto todavía se está generando.
- Interrumpir hablando: si el VAD detecta voz mientras el asistente habla (con
  umbral más alto), se cancela el turno y se vacía la cola de audio del cliente.

## Medidas en esta caja (aarch64, 2 vCPU, sin GPU)

| etapa | medida |
|---|---|
| Kokoro int8 (`ef_dora`), frase de 34 caracteres | 2,13 s de audio en 1,10 s → RTF 0,52 |
| Kokoro int8, párrafo de 12,5 s | RTF 0,47-0,73 |
| Kokoro fp32 (mismo texto) | RTF 0,98-1,27 → descartado |
| faster-whisper small int8, 2,1 s de audio | ~6 s → descartado para conversación |
| faster-whisper base int8 | ver `docs/medidas.md` |

El fp32 y el `small` se descartaron por medida, no por intuición: en dos núcleos
ARM no llegan al ritmo de una conversación.

## Servicios

| contenedor | imagen | red | puerto interno |
|---|---|---|---|
| `voz-stt` | `voz-stt:current` | `voz-net` | 9000 |
| `voz-tts` | `voz-tts:current` | `voz-net` | 9000 |
| `voz` | `voz-app:current` | `voz-net`, `edge` | 9000 |

Solo `voz` está en `edge`, con las etiquetas de Traefik para
`voz.oci2.legido.com`. Los pesos viven en el volumen `voz-models`.

## Desplegar

```sh
docker network create voz-net                     # una vez

docker build -t voz-stt:current ./stt
docker build -t voz-tts:current ./tts
docker build -t voz-app:current ./app

docker run -d --name voz-stt --restart unless-stopped --network voz-net voz-stt:current
docker run -d --name voz-tts --restart unless-stopped --network voz-net \
  -v voz-models:/models:ro voz-tts:current

docker run -d --name voz --restart unless-stopped \
  --network voz-net --network edge \
  -e STT_URL=http://voz-stt:9000 -e TTS_URL=http://voz-tts:9000 \
  -e LLM_BASE=https://opencode.ai/zen/go/v1 -e LLM_MODEL=deepseek-v4.1-flash \
  -e OPENCODE_GO_API_KEY -e VOZ_COOKIE_SECRET -e VOZ_PASS_HASH \
  -l traefik.enable=true \
  -l 'traefik.http.routers.voz.rule=Host(`voz.oci2.legido.com`)' \
  -l traefik.http.routers.voz.entrypoints=websecure \
  -l traefik.http.routers.voz.tls.certresolver=le \
  -l traefik.http.services.voz.loadbalancer.server.port=9000 \
  voz-app:current
```

## Acceso

Un solo usuario: `VOZ_PASS_HASH` es el SHA-256 del código de acceso. Sin ese
código configurado la app queda abierta (solo para desarrollo). La cookie de
sesión va firmada con `VOZ_COOKIE_SECRET` y caduca a los 60 días. Hay un límite
de 8 intentos fallidos por IP cada 10 minutos y un tope de 80 turnos por sesión.

## Ajustes de la app

Voz (Dora, Álex, Santa), ritmo (0,8-1,2) y transcripción opcional. La
transcripción está apagada por defecto: la app es voz, no chat.

## Tests

```sh
docker run --rm -i --network voz-net python:3.12-slim python - < tests/smoke.py
```

`tests/turn.py` hace un turno completo por WebSocket y escribe el audio devuelto.
