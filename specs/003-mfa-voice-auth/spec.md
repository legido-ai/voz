# 003 — Autenticación del acceso a voz por desafío fuera de banda

## Goal

Abrir `voz`, hablar, y que el asistente ejecute de verdad (con herramientas, vía el
perfil `voz` de Hermes) sin ninguna pantalla de login: la autenticación ocurre una
vez, con un número de seis cifras que llega al bot de Telegram del perfil `voz` y se
dice en voz alta junto a la petición.

## Background

Hoy `voz` se autentica con usuario/contraseña contra la base `accounts.db` de AI
Usage (`app/authdb.py`) y guarda una cookie firmada de 60 días. Eso obliga a una
pantalla de login en el móvil y deja la contraseña como único factor.

La alternativa evaluada y descartada fue un TOTP generado y validado por el propio
servicio: no es un segundo factor, porque quien guarda la semilla puede fabricar
códigos. Lo que sí aporta es un **desafío de aprobación fuera de banda**: el número
no lo ve la web (solo viaja por Telegram), es de un solo uso, caduca y tiene
intentos limitados.

### Amendment A1 — texto exacto de los mensajes de voz (usuario, tras aprobar el flujo)

El usuario fija que la app no explique nada: sólo dos frases.

- Necesita autenticación → **"Necesitas autenticarte."**
- El código se usa mal (o caduca, o no queda desafío vivo) → **"Error en la autenticación."**

No hay pistas sobre Telegram, ni sobre el código, ni sobre qué hacer después. El
usuario ya sabe que el número llega a su Telegram. Se retira por tanto cualquier
mensaje del tipo "mira tu Telegram" o "dime el código". Constraint vinculante: la
app **no** guía; sólo informa del estado en esas dos palabras.

### Amendment A2 — el destino deja de ser OpenCode Go (usuario)

El usuario decide que `voz` no hable con el proveedor del modelo sino con un perfil
Hermes **`voz`**, para tener herramientas y no un chat pelado. Constraint vinculante:
`voz` deja de holding credenciales de proveedor; la única credencial saliente es la
del API server de Hermes. El perfil `voz` se crea con las herramientas por defecto y
se ajustará después ("iremos viendo qué herramientas tiene").

## Functional Requirements

- **FR-001** La página carga un desafío al abrirse (`GET /api/auth/challenge`), no al
  hablar. La respuesta incluye sólo `id`, `expires_in` y `code_length`; **nunca** el
  número.
- **FR-002** El servicio MFA publica el número por Telegram con un token de bot
  propio (`MFA_BOT_TOKEN`), independiente del bot de Hermes, y con el chat fijado por
  `MFA_CHAT_ID`. Sin `MFA_BOT_TOKEN` el desafío se crea igualmente y el outbox lo
  registra como no enviado (modo prueba).
- **FR-003** El número se guarda como hash SHA-256 con un nonce por desafío; nunca en
  claro, ni en logs.
- **FR-004** Validación **antes** de cualquier ejecución. `POST /api/auth/verify`
  recibe `{token}`, donde `token` es el transcript completo de la frase
  (`"Necesitas autenticarte… no, código cuatro siete dos nueve uno tres, apaga la
  música"`), y localiza en él un tramo de 6 dígitos válido. Si no hay desafío vivo,
  o el código no coincide, o se agotan los intentos → 401 y **FR-005**.
- **FR-005** Un código válido consume el desafío inmediatamente y emite una sesión:
  `{session, expires_in}`, con TTL deslizante de 15 min (`VOZ_IDLE_MINUTES`) que se
  renueva en cada turno ejecutado.
- **FR-006** Intentos limitados: 5 por desafío (`MFA_MAX_ATTEMPTS`). Agotados, el
  desafío muere; el cliente debe pedir uno nuevo (la UI lo hace sola).
- **FR-007** El relay enruta al perfil `voz` de Hermes: `POST {HERMES_BASE}/chat/completions`
  con `Authorization: Bearer $HERMES_API_KEY`, `model: $HERMES_MODEL`, streaming SSE.
  Sin `HERMES_API_KEY` configurada, el proceso **no arranca** (fail closed).
- **FR-008** La frase de error por voz es literalmente "Error en la autenticación."
  tras un fallo, y la app pide un desafío nuevo automáticamente sin recargar.
- **FR-009** La bienvenida al cargar la página es "Necesitas autenticarte." si no hay
  sesión válida; si la cookie de sesión es válida, no se crea desafío ni se envía
  número a Telegram.
- **FR-010** Ambos servicios son imágenes públicas: `ghcr.io/legido-ai/voz` y
  `ghcr.io/legido-ai/voz-mfa`, y el repositorio es público.
- **FR-011** La UI no muestra ninguna pista de cómo autenticarse (A1).

## Non-Goals

- TOTP/WebAuthn reales. El diseño es aprobación fuera de banda, con caducidad.
- Restringir por IP. Descartado: la IP del móvil es dinámica y el operador no garantiza fija.
- Elevar el perfil `voz` a herramientas de infraestructura en esta entrega.
- Multi-usuario: un único destinatario de Telegram por despliegue.

## Acceptance Criteria

- `GET /api/auth/challenge` → 200 con `id`, `expires_in`, `code_length`; el log del
  servicio MFA muestra `sent=1` cuando hay token y `sent=0` cuando no lo hay.
- `POST /api/auth/verify` con el mensaje del outbox → 200 y `session` no vacío.
- El mismo mensaje otra vez → 401 (`used`).
- Un código con dígitos cambiados 5 veces → 401 y el 6.º intento también 401.
- Desafío de hace 6 minutos → 401 (`expired`).
- Con sesión válida, `POST /api/chat` sin cabecera `X-Voz-Session` → 401.
- Con sesión válida y credencial de Hermes inválida → 502 con un error controlado, no
  una traza.
- `pytest` verde en ambos paquetes, sin red.

## Edge cases

- El usuario dice el código troceado ("cuatro siete dos nueve, uno tres, hmm, nueve uno
  tres"): la búsqueda escanea todas las ventanas de 6 dígitos (`REF-001`).
- El usuario dice un código mientras el desafío ya caducó: 401 y desafío nuevo.
- Llega una petición concurrente con el mismo código: el primero gana, el segundo ve
  `used` (bloqueo en la base).
- Telegram caído: el flujo falla cerrado (nadie recibe el número) y se dice "Error en
  la autenticación." al intentar.
