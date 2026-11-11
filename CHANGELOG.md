# Changelog

## 0.2.2 — 2026-11-11

### Añadido
- **La verificación vive en el relay**, no en el navegador: el mismo servidor que ya
  tiene el transcript canjea el código y adopta la sesión. Antes el navegador hacía un
  `POST /api/auth/verify` que fallaba en silencio y el código dictado se perdía.
- **Un código por episodio**: `voz-mfa` mantiene un único desafío vivo por cliente y
  devuelve el mismo `id` en lugar de empujar otro mensaje.
- **«Autenticado.»**: si la frase es solo el código, se responde una palabra y no se
  consulta al perfil (antes el agente contestaba «¿Qué necesitas?» y se perdía un turno).
- **Sesión reanudable**: el token se guarda en el móvil y se presenta como `?s=`; una
  reconexión renueva el reloj de inactividad en vez de juzgarse como caducada.
- **«Cerrar sesión»** (arriba a la derecha, solo con sesión activa) revoca el token en
  `voz-mfa`. Antes el enlace borraba una cookie que la app ya no usaba: el token seguía
  válido y «cerrar sesión» era cosmético.

### Corregido
- **Colgar vuelve a ser colgar**: cierra socket, micro y reproducción, y conserva la
  sesión, para poder callar la respuesta sin volver a autenticarse.
- Captura de audio con `ScriptProcessor` en vez de `AudioWorklet`: en WebKit/iOS el
  worklet entregaba buffers a cero y el ASR recibía silencio.

## 0.2.1 — 2026-11-11

### Añadido

- **`brand/` — la marca en un solo sitio.** `logo.py` dibuja el logo desde
  geometría y emite el maestro vectorial (`voz-mark.svg`), el PNG de 1024, el
  lockup con la palabra y el avatar de 256 del bot. Paleta e identidad las del
  icono que ya servía la PWA; lo que cambia es el aire: las barras ocupan el 55 %
  del lienzo en vez del 32 %, porque a 40 px —el tamaño real de un avatar en la
  lista de chats— la versión pequeña se leía como una mancha naranja.
- `python3 brand/logo.py --icons app/static/icons` regenera el juego de iconos de
  la PWA desde la misma fuente, para que app y bot no se separen con el tiempo.
- `@legido_voz_bot` ya lleva `brand/voz-avatar-256.jpg` como foto de perfil
  (`setMyProfilePhoto`, leída de vuelta con `getUserProfilePhotos`).

## 0.2.0 — 2026-11-10

### Added
- **`voz-mfa`, servicio de autenticación fuera de banda** (nuevo contenedor): genera
  un número de 6 cifras por desafío, lo entrega por Telegram y guarda solo
  `sha256(nonce:código)`. El número nunca cruza hacia la web. 5 minutos de vida,
  10 intentos, un solo uso (FR-001..FR-006).
- Desafío creado en el primer toque del micrófono (no al abrir la página); el
  transcript hablado se canjea en el relay y un acierto emite una sesión con TTL
  deslizante de 15 minutos renovada en cada turno (FR-005, FR-009).
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
- **Un `GET` anónimo ya no puede hacer sonar el teléfono.** El desafío se genera en el
  primer toque del micrófono, no al cargar la página (FR-009 revisado).
- Emisión limitada por cliente (3/10 min) con reutilización del desafío vivo durante
  4 min, y en `voz-mfa` cooldown global de 20 s / 6 por hora; una emisión suprimida
  no se cachea como reutilizable.
- Cada emisión y cada fallo de verificación registran la IP del cliente.

### Added
- La sesión se guarda en el móvil y se reanuda en la siguiente conexión (`?s=<token>`):
  recargar la página ya no pide un código nuevo mientras la sesión siga viva.

### Fixed
- El relay apuntaba a `host.docker.internal`, que resuelve al gateway de Docker y no
  al contenedor de Hermes; ahora usa `http://hermes:8642/p/voz/v1` y el contenedor se
  conecta a `box_default`, porque Docker aísla las redes bridge entre sí.
- El perfil `voz` heredó la regla de cierre «✅ I'm done», que se leía en voz alta: se
  retiró de su memoria y `speakable()` descarta ese marcador si vuelve a aparecer.

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
