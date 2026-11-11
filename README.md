# voz

Conversación por voz, sin texto: abres la web en el móvil, dices el código que te
llega al Telegram y hablas; una voz natural te responde con un agente con
herramientas detrás. La voz es open source y corre en esta caja; la parte de
agente la sirve un perfil Hermes (`voz`) al que este servicio solo hace de relay.

- Web app (PWA instalable en iPhone). Nada de modelo en el teléfono: el móvil es
  micrófono y altavoz.
- ASR: sherpa-onnx, zipformer `es-kroko` en streaming, dentro del proceso `voz`.
- TTS: Kokoro-82M int8 (Apache-2.0) y Piper `es_ES-sharvard-medium` a elegir.
- Orquestación: FastAPI + WebSocket en `voz`.

## Cómo funciona una frase

```
iPhone ──PCM16 16 kHz por WebSocket──▶ voz ──┬─▶ sherpa-onnx (streaming, en proceso)
                                             │
                                             ├─▶ Hermes API server (perfil `voz`, SSE)
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
| `voz-mfa` | `ghcr.io/legido-ai/voz-mfa:current` | `voz-net` | 8000 |
| `voz-tts` | `ghcr.io/legido-ai/voz-tts:current` | `voz-net` | 9000 |
| `voz` | `ghcr.io/legido-ai/voz:current` | `voz-net`, `edge` | 9000 |

Solo `voz` está en `edge`, con las etiquetas de Traefik para
`voz.oci2.legido.com`. Los pesos (Kokoro, Piper, zipformer) y las muestras de
prueba viven en el volumen `voz-models`.

`stt/` contiene el servicio faster-whisper heredado: no se despliega, queda como
alternativa (`STT_URL=http://voz-stt:9000`) si alguna vez falla el streaming.

## Desplegar

Las imágenes se publican en **GHCR** (públicas, `linux/amd64` + `linux/arm64`) desde
`.github/workflows/publish.yml` en cada push a `main`: `ghcr.io/legido-ai/voz` y
`ghcr.io/legido-ai/voz-mfa`. En esta caja se despliegan con `docker run` contra el
Engine (ver `compose.yml` para el cableado completo).

```sh
docker network create voz-net        # una vez
docker volume create voz-models      # una vez
docker volume create voz-mfa-data    # una vez

docker run -d --name voz-mfa --restart unless-stopped --network voz-net \
  -v voz-mfa-data:/data \
  -e MFA_BOT_TOKEN -e MFA_CHAT_ID=21442158 -e MFA_OUTBOX=1 \
  ghcr.io/legido-ai/voz-mfa:current

docker run -d --name voz --restart unless-stopped \
  --network voz-net --network edge \
  -v voz-models:/models:ro --add-host host.docker.internal:host-gateway \
  -e MFA_URL=http://voz-mfa:8000 \
  -e TTS_URL=http://voz-tts:9000 \
  --network box_default \
  -e HERMES_BASE=http://hermes:8642/p/voz/v1 -e HERMES_MODEL=voz \
  -e HERMES_API_KEY \
  -l traefik.enable=true \
  -l 'traefik.http.routers.voz.rule=Host(`voz.oci2.legido.com`)' \
  -l traefik.http.routers.voz.entrypoints=websecure \
  -l traefik.http.routers.voz.tls.certresolver=le \
  -l traefik.http.services.voz.loadbalancer.server.port=9000 \
  ghcr.io/legido-ai/voz:current
```

Pruebas: `pytest app/tests mfa/tests` (sin red) y `python app/tests/e2e_turn.py`
(arranca `voz-mfa` + `voz` con relays falsos y recorre desafío → código hablado →
sesión → turno).

Contenido del volumen `voz-models`:

```
/models/kokoro-v1.0.int8.onnx   /models/voices-v1.0.bin
/models/piper/es_ES-sharvard-medium.onnx(+.json)
/models/sherpa/{encoder,decoder,joiner}.onnx, tokens.txt
```

## Acceso: desafío fuera de banda (`voz-mfa`)

No hay contraseña ni pantalla de login. Al abrir la web se pide un desafío a
`voz-mfa`; ese servicio genera un número de **6 cifras**, lo envía por Telegram
(chat configurado en `MFA_CHAT_ID`) y guarda solo `sha256(nonce:código)`. La web
recibe el **id del desafío, nunca el número**.

El desafío **no se genera al cargar la página**: se pide en el primer toque del
micrófono, que es intención deliberada. Cualquiera puede hacer `GET /` sin provocar
un mensaje en el teléfono. Reglas de emisión:

- Por cliente: máximo 3 desafíos cada 10 min (`VOZ_CHALLENGE_MAX`,
  `VOZ_CHALLENGE_WINDOW`) y, dentro de los 4 min siguientes, se **reutiliza** el
  vivo en vez de volver a empujar (`VOZ_CHALLENGE_REUSE`).
- En `voz-mfa`, además, un cooldown global de 20 s entre envíos y 6 por hora
  (`MFA_SEND_COOLDOWN`, `MFA_SEND_HOURLY`): si se supera, el desafío existe pero no
  se envía. Así ni un escáner ni un bucle pueden inundar Telegram.
- Cada emisión registra la IP del cliente (`challenge: minted for <ip>`), que es la
  única atribución disponible: el Traefik del borde no guarda log de accesos.

El usuario lo dice en voz alta en la misma frase que su petición (*"Necesitas
autenticarte… código 4 7 2 9 1 3, enciende la música"*); el transcript completo
se manda a `POST /api/auth/verify`, que busca un tramo de 6 dígitos válido. Con
acierto, el desafío se quema y se emite una sesión con **TTL deslizante de 15
minutos** (`VOZ_IDLE_MINUTES`), renovada en cada turno. Si el código falla, el
desafío muere y la app genera otro sin recargar.

La app solo dice dos frases: **«Necesitas autenticarte.»** y **«Error en la
autenticación.»**. Nada más: no explica dónde está el número ni qué hacer.

Límites: 5 intentos por desafío (`MFA_MAX_ATTEMPTS`), 5 minutos de vida
(`MFA_TTL_SECONDS`), un solo uso. Si Telegram no está configurado, el desafío se
crea y el outbox registra `sent=0` (modo prueba, falla cerrado).

### Relay al perfil Hermes `voz`

`voz` no guarda ninguna clave de modelo. Habla con el API server del gateway de
Hermes en `HERMES_BASE` (`http://hermes:8642/p/voz/v1`, modelo `voz`), con
`HERMES_API_KEY` = `API_SERVER_KEY` del perfil. Sin esa clave el contenedor **no
arranca**: un relay sin credencial fallaría dentro de un turno de voz, que es
mucho más difícil de diagnosticar.

Dos condiciones de red, ambas verificadas a mano:

1. El API server escucha en `127.0.0.1` por defecto, así que hay que fijar
   `platforms.api_server.host=0.0.0.0` y reiniciar el gateway (el bind ocurre al
   arrancar). `host.docker.internal` **no** sirve aquí: resuelve al gateway de
   Docker, no al contenedor de Hermes.
2. Docker aísla las redes bridge entre sí: `voz` no alcanza `box_default` desde
   `voz-net`. El contenedor se conecta también a `box_default` y llama por el
   nombre `hermes`.

## Ajustes de la app

- Voz: Dora, Álex, Santa (Kokoro, naturales) o Piper (femenina/masculina, rápidas).
- Ritmo 0,8-1,2 y transcripción opcional (apagada por defecto: la app es voz).

## Marca

`brand/logo.py` es la fuente única del logo: dibuja la marca desde geometría (no
hay un PNG suelto que alguien tenga que rehacer). Misma identidad que el icono de
la PWA —cuatro barras redondeadas en `#ff8a3d` / `#ffb37a` sobre `#08090c`, con el
anillo tenue `#2e241e`— pero con las barras al 55 % del lienzo en lugar del 32 %:
a 40 px (la lista de chats de Telegram) la versión pequeña se fundía en una mancha.

```sh
python3 brand/logo.py                              # brand/voz-mark-1024.png, lockup, avatar
python3 brand/logo.py --icons app/static/icons     # además los iconos de la PWA
```

- `brand/voz-mark.svg` — maestro vectorial, mismas coordenadas que el generador.
- `brand/voz-avatar-256.jpg` — el avatar del bot `@legido_voz_bot`.
- `brand/voz-mark-glow.png` — variante con halo, para fondos claros.

## Tests

```sh
# servicios sueltos: TTS + ASR
docker run --rm -i --network voz-net python:3.12-slim python - < tests/smoke.py

# turno completo por WebSocket (WAV dentro del volumen voz-models)
docker run --rm -i --network voz-net -v voz-models:/models \
  -e COOKIE="voz_session=<token>" -e WAV=/models/tests/turn-long.wav \
  python:3.12-slim sh -c 'pip install -q websockets; python -' < tests/turn.py
```
