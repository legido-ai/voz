# Plan — 003 MFA fuera de banda + relay al perfil `voz` de Hermes

## Topología

```
iPhone ──WS PCM16──▶ voz (app)  ──▶ sherpa ASR (en proceso)
                         │
                         ├─ HTTP ──▶ voz-mfa  ──▶ Telegram bot del perfil voz
                         │              (desafío: id + hash( código ))
                         └─ SSE ───▶ Hermes API server :8642 (perfil voz)
```

`voz` ya no habla con OpenCode Go. `voz-mfa` es un segundo contenedor con su propia
base SQLite sobre un volumen nombrado.

## Decisiones

| Decisión | Valor | Motivo |
|---|---|---|
| Generación del código | 6 dígitos aleatorios (`secrets.randbelow`) | Lectura en voz alta sin ambigüedad |
| Almacenamiento | `sha256(nonce:código)` + nonce por desafío | Ni en claro ni reversible; sin sal compartida |
| TTL del desafío | 300 s (`MFA_TTL_SECONDS`) | 5 min es lo bastante cómodo y caduca rápido |
| Intentos | 5 (`MFA_MAX_ATTEMPTS`) | Un desafío muere por fuerza bruta en segundos, no en minutos |
| Sesión | token opaco, TTL deslizante 15 min (`VOZ_IDLE_MINUTES`) | El usuario lo fijó |
| Verificación | el cliente manda el transcript entero; el servidor busca ventanas de 6 dígitos | Así el número se dice en la misma frase que la petición |
| Envío del número | Bot API `sendMessage` directo desde `voz-mfa` | Cumple A1: no explica nada; el número llega a Telegram |

## Ficheros

- `mfa/app.py` — FastAPI: `GET /api/auth/challenge`, `POST /api/auth/verify`,
  `GET /healthz`; SQLite en `MFA_DB` (`/data/mfa.db`).
- `mfa/telegram.py` — `send_code(chat_id, código)` con `httpx`; registra `sent`.
- `mfa/Dockerfile`, `mfa/requirements.txt`, `mfa/tests/`.
- `app/main.py` — sustituye `authdb`/contraseña por `authorize(token)` contra `voz-mfa`;
  `/login` y `POST /login` desaparecen; `/api/chat` nuevo; `ws` exige sesión.
- `app/pipeline.py` — `stream_reply` apunta a `HERMES_BASE` con `HERMES_API_KEY`.
- `app/static/*` — al cargar pide desafío; el audio de bienvenida dice
  "Necesitas autenticarte."; el transcript se manda a `/api/auth/verify` antes de hablar.
- `compose.yml`, `README.md`, `CHANGELOG.md`.

## Estrategia de tests

`pytest` puro, sin red ni contenedores:

- `mfa/tests/test_mfa.py`: expiración, un solo uso, 5 intentos, localización del código
  en un transcript con ruido, hash no reversible (la tabla no contiene el número).
- `app/tests/test_auth_flow.py`: el relay exige `X-Voz-Session`, mapea `auth_expired`
  a 401 y la llamada al API de Hermes usa `HERMES_API_KEY`; el transcript sin código
  válido devuelve el error literal.

## Riesgos

- **Telegram como punto único de entrega.** Si el móvil no está, no entra. Aceptado por
  el usuario: es el móvil el que valida.
- **El bot del perfil voz debe existir** con `MFA_CHAT_ID` = chat del usuario. Es un
  paso de despliegue, y el primer smoke test es manda-y-comprueba (canal directo al
  perfil, sin `voz` en medio).
- **El API server de Hermes** debe estar habilitado con `API_SERVER_KEY`; el perfil
  `voz` debe existir antes de arrancar `voz`.
