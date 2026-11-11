# Tasks — 003 MFA fuera de banda + relay al perfil `voz`

## Fase 1 · Especificación y plan (completada)

- [x] T001 `spec.md` con FR-001..FR-011, enmiendas A1/A2
- [x] T002 `plan.md` con topología, decisiones y estrategia de tests

## Fase 2 · Servicio MFA (nuevo paquete `mfa/`)

- [x] T003 Tests primero: expiración, un solo uso, 5 intentos, búsqueda en transcript
- [x] T004 `mfa/store.py`: SQLite, hash con nonce, `create`, `verify`, `session`
- [x] T005 `mfa/telegram.py`: envío del número por Bot API, `sent=0/1`
- [x] T006 `mfa/app.py`: `GET /api/auth/challenge`, `POST /api/auth/verify`, `GET /healthz`
- [x] T007 `mfa/Dockerfile` + `requirements.txt`

## Fase 3 · Relay `voz` → Hermes perfil `voz`

- [x] T008 Tests: `/api/chat` exige sesión y propaga `auth_expired` como 401
- [x] T009 `app/pipeline.py`: `stream_reply` contra `HERMES_BASE`/`HERMES_API_KEY`
- [x] T010 `app/main.py`: `authorize()` contra `voz-mfa`, `/api/chat`, WS con sesión
- [x] T011 Eliminar `authdb.py` y las rutas `/login`, `/logout`

## Fase 4 · UI

- [x] T012 Pedir desafío al cargar (solo sin sesión válida)
- [x] T013 Audio de bienvenida: «Necesitas autenticarte.»
- [x] T014 Tras fallo: «Error en la autenticación.» + desafío nuevo automático

## Fase 5 · Publicación y despliegue

- [x] T015 Workflow GHCR: `ghcr.io/legido-ai/voz` y `ghcr.io/legido-ai/voz-mfa` (públicas)
- [x] T016 Repositorio público
- [x] T017 Perfil `voz` en Hermes + `API_SERVER_KEY`
- [x] T018 Bot de Telegram del perfil `voz` + `MFA_CHAT_ID`
- [x] T019 Smoke del canal directo: nodo despliega y dice qué número llega
- [ ] T020 Vuelta completa: abrir, hablar, decir el número, ejecutar (pendiente de T018)
- [ ] T021 Apagar el bot antiguo del perfil `voz` y rotar credenciales
