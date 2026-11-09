# voz

Conversación por voz, sin texto: tocas un botón en el móvil, hablas, y una voz
natural te responde. Toda la voz es open source y corre en esta caja; la única
llamada externa es el LLM (OpenCode Go, `deepseek-v4.1-flash`).

- Web app (PWA instalable en iPhone). Nada de modelo en el teléfono: el móvil es
  micrófono y altavoz.
- ASR: sherpa-onnx, zipformer `es-kroko` en streaming, dentro del proceso `voz`.
- TTS: Kokoro-82M int8 (Apache-2.0) y Piper `es_ES-sharvard-medium` a elegir.
- Orquestación: FastAPI + WebSocket en `voz`.

## Cómo funciona una frase

```
iPhone ──PCM16 16 kHz por WebSocket──▶ voz ──┬─▶ sherpa-onnx (streaming, en proceso)
                                             │
                                             ├─▶ OpenCode Go / deepseek-v4.1-flash (SSE)
                                             │
                                             └─▶ voz-tts (Kokoro / Piper) ──PCM16 24 kHz──▶ iPhone
```

- El VAD es de energía, con suelo de ruido adaptativo: 140 ms para abrir turno,
  500 ms de silencio para cerrarlo, pre-roll de 200 ms.
- El ASR decodifica mientras hablas; al cerrar el turno sólo queda la cola.
- El LLM se consume en streaming y se corta en frases: la primera se sintetiza
  mientras el resto todavía se está generando.
- Interrumpir hablando: si el VAD detecta voz mientras el asistente habla (con
  umbral más alto), se cancela el turno y se vacía la cola de audio del cliente.

## Medidas en esta caja (aarch64, 2 vCPU, sin GPU)

Tabla completa y metodología en [`docs/medidas.md`](docs/medidas.md).

| etapa | medida |
|---|---|
| ASR sherpa streaming | 0,10-0,51 s por frase (RTF 0,05-0,11) |
| ASR faster-whisper `small` int8 | 4,97 s por frase → descartado |
| TTS Kokoro int8 | RTF 0,47-0,73 · primera frase 1,4 s |
| TTS Kokoro fp32 | RTF 1,04-1,27 → descartado |
| TTS Piper | RTF 0,11-0,13 · primera frase 0,3 s |
| LLM TTFT (`thinking=false`) | 0,8-1,7 s (2,2-2,5 s si razona) |
| **fin de voz → primer audio** | **1,8-1,9 s (Piper) · 2,8 s (Kokoro)** |

## Servicios

| contenedor | imagen | red | puerto interno |
|---|---|---|---|
| `voz-tts` | `voz-tts:current` | `voz-net` | 9000 |
| `voz` | `voz-app:current` | `voz-net`, `edge` | 9000 |

Solo `voz` está en `edge`, con las etiquetas de Traefik para
`voz.oci2.legido.com`. Los pesos (Kokoro, Piper, zipformer) y las muestras de
prueba viven en el volumen `voz-models`.

`stt/` contiene el servicio faster-whisper heredado: no se despliega, queda como
alternativa (`STT_URL=http://voz-stt:9000`) si alguna vez falla el streaming.

## Desplegar

```sh
docker network create voz-net docker network create voz-net voz-models        # una vezdocker network create voz-net voz-models        # una vez docker volume create voz-models   # una vez

docker build -t voz-tts:current ./tts
docker build -t voz-app:current ./app

docker run -d --name voz-tts --restart unless-stopped --network voz-net \
  -v voz-models:/models:ro -e OMP_NUM_THREADS=2 voz-tts:current

docker run -d --name voz --restart unless-stopped \
  --network voz-net --network edge \
  -v voz-models:/models:ro \
  -e STT_URL= -e TTS_URL=http://voz-tts:9000 \
  -e LLM_BASE=https://opencode.ai/zen/go/v1 -e LLM_MODEL=deepseek-v4.1-flash \
  -e OPENCODE_GO_API_KEY -e VOZ_COOKIE_SECRET -e VOZ_PASS_HASH \
  -l traefik.enable=true \
  -l 'traefik.http.routers.voz.rule=Host(`voz.oci2.legido.com`)' \
  -l traefik.http.routers.voz.entrypoints=websecure \
  -l traefik.http.routers.voz.tls.certresolver=le \
  -l traefik.http.services.voz.loadbalancer.server.port=9000 \
  voz-app:current
```

Contenido del volumen `voz-models`:

```
/models/kokoro-v1.0.int8.onnx   /models/voices-v1.0.bin
/models/piper/es_ES-sharvard-medium.onnx(+.json)
/models/sherpa/{encoder,decoder,joiner}.onnx, tokens.txt
```

## Acceso

Un solo usuario: `VOZ_PASS_HASH` es el SHA-256 del código de acceso. Sin ese
código configurado la app queda abierta (solo para desarrollo). La cookie de
sesión va firmada con `VOZ_COOKIE_SECRET` y caduca a los 60 días. Hay un límite
de 8 intentos fallidos por IP cada 10 minutos y un tope de 80 turnos por sesión.
La clave del LLM vive solo en el entorno del contenedor: nunca llega al navegador.

## Ajustes de la app

- Voz: Dora, Álex, Santa (Kokoro, naturales) o Piper (femenina/masculina, rápidas).
- Ritmo 0,8-1,2 y transcripción opcional (apagada por defecto: la app es voz).

## Tests

```sh
# servicios sueltos: TTS + ASR
docker run --rm -i --network voz-net python:3.12-slim python - < tests/smoke.py

# turno completo por WebSocket (WAV dentro del volumen voz-models)
docker run --rm -i --network voz-net -v voz-models:/models \
  -e COOKIE="voz_session=<token>" -e WAV=/models/tests/turn-long.wav \
  python:3.12-slim sh -c 'pip install -q websockets; python -' < tests/turn.py
```
