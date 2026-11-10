# Changelog

## 0.2.0 — 2026-11-10

### Added
- **`voz-mfa`, servicio de autenticación fuera de banda** (nuevo contenedor): genera
  un número de 6 cifras por desafío, lo entrega por Telegram y guarda solo
  `sha256(nonce:código)`. El número nunca cruza hacia la web. 5 minutos de vida,
  5 intentos, un solo uso (FR-001..FR-006).
- Desafío creado al abrir la página; el transcript hablado se valida en
  `POST /api/auth/verify` y un acierto emite una sesión con TTL deslizante de 15
  minutos renovada en cada turno (FR-005, FR-009).
- Relay al **perfil Hermes `voz`**: `voz` habla con el API server del gateway
  (`/p/voz/v1`) en lugar del proveedor del modelo, así que puede ejecutar
  herramientas y no guarda ninguna clave de modelo (FR-007).
- Imágenes públicas en GHCR (`ghcr.io/legido-ai/voz`, `ghcr.io/legido-ai/voz-mfa`)
  construidas para amd64 y arm64 por el workflow de publicación (FR-010).
- `app/tests/e2e_turn.py`: recorrido completo sin red (desafío → código → sesión →
  turno → relay) con relays falsos.

### Changed
- La app pasa a decir **solo dos frases** por voz: «Necesitas autenticarte.» y
  «Error en la autenticación.»; no explica dónde está el número ni qué hacer
  (enmienda A1 de la especificación).
- La sesión ya no dura 60 días: 15 minutos de inactividad, renovados por uso.

### Removed
- Login por usuario/contraseña contra `accounts.db` y el hash `VOZ_PASS_HASH`;
  `authdb.py`, `VOZ_COOKIE_SECRET` y el bloqueo por IP del formulario, que ya no
  tiene formulario donde aplicarse.
- La credencial `OPENCODE_GO_API_KEY` en el contenedor `voz`.

### Security
- El número de aprobación es de un uso, caduca y tiene intentos limitados; una
  reutilización responde `used`, un código viejo `expired`.
- El desafío caducado o agotado se quema y la app pide uno nuevo sin recargar, para
  que un fallo no deje la sesión en un estado que parezca roto.
- `voz` se niega a arrancar sin `HERMES_API_KEY` (fallo cerrado) en lugar de
  fallar dentro de un turno de voz.

## 0.1.3 — 2026-11-09

- Interfaz: círculo de grabación al máximo que cabe (337 px en un iPhone de
  390 px, 460 px en pantallas grandes) y controles de cabecera más grandes
  (54 px de alto). Eliminado el rótulo "voz".
- El menú se cierra al tocar fuera; añadido "Cerrar sesión".
- Corregido: `[hidden]` no funcionaba en los botones con `display` propio (el
  botón "Colgar" se veía siempre).
- Campo de usuario vacío en el login (por defecto `usuario` en el servidor).

## 0.1.2 — 2026-11-09

- Login con las credenciales de AI Usage (misma contraseña, una sola fuente):
  `accounts.db` leído en solo lectura y copiado a temporal por el modo WAL.
- Formulario con usuario (por defecto `usuario`) y contraseña; el código suelto
  queda solo como recuperación.

## 0.1.1 — 2026-11-09

- Voz por defecto: Piper `es_ES-sharvard-medium` voz 1 (castellana, femenina),
  elegible en el menú junto a Kokoro y las demás Piper.
- Bloqueo de login por IP: 3 fallos consecutivos → 5 minutos, con la IP real
  detrás de Traefik (`X-Forwarded-For`). Contador con ventana de 10 minutos.
- Código de acceso rotado.

## 0.1.0 — 2026-11-09

Primera versión: conversación por voz con OpenCode Go, todo autohospedado.

- Web app PWA instalable en iPhone (`voz.oci2.legido.com`): un botón, sin chat de
  texto, interrupción hablando y corte al tocar.
- ASR en streaming con sherpa-onnx (zipformer `es-kroko`, greedy, `enable_endpoint_detection=false`
  para que el turno lo decida el VAD propio).
- TTS con dos motores seleccionables: Kokoro-82M int8 (`ef_dora`/`ef_alex`/`em_santa`)
  y Piper `es_ES-sharvard-medium` (locutora y locutor) para respuestas rápidas.
- Proxy LLM en el servidor: la clave nunca llega al navegador; SSE con
  `deepseek-v4.1-flash`, `chat_template_kwargs.thinking=false` (TTFT medido
  1,0-1,7 s frente a 2,2-2,5 s con razonamiento).
- VAD de energía con suelo de ruido adaptativo, pre-roll de 200 ms y umbral
  reforzado mientras el asistente habla (barge-in).
- Acceso con código (hash SHA-256 en el entorno), cookie firmada HMAC, límite de
  intentos por IP y tope de turnos por sesión.
- Medido en la caja (2 vCPU ARM, sin GPU): Kokoro int8 RTF 0,47-0,73;
  fp32 RTF 0,98-1,27 (descartado); whisper `small` int8 4,3-5,0 s por frase
  (descartado); whisper `base` 1,6-1,8 s; sherpa streaming 0,1-0,5 s.
- Corrección sobre `kokoro-onnx` 0.6.1: su recorte de silencio revienta con
  frases muy cortas; se reintenta sin `trim` y se recorta aquí.
