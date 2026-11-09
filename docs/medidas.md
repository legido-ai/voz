# Medidas en la caja (aarch64, 2 vCPU, sin GPU, 4 GB libres)

Todo medido con el código de este repo el 2026-11-09, con TTS y ASR calientes.
RTF = tiempo de cálculo / duración del audio (menos es mejor; por debajo de 1 el
motor produce audio más rápido de lo que se reproduce).

## TTS

| motor | texto | audio | tiempo | RTF |
|---|---|---|---|---|
| Kokoro-82M int8 (`ef_dora`) | 37 caracteres | 2,39 s | 1,45 s | 0,61 |
| Kokoro-82M int8 (`ef_dora`) | 81 caracteres | 4,90 s | 2,31-3,22 s | 0,47-0,66 |
| Kokoro-82M int8 (`ef_dora`) | párrafo | 12,50 s | 5,90-9,07 s | 0,47-0,73 |
| Kokoro-82M fp32 | párrafo | 12,50 s | 12,98-15,91 s | 1,04-1,27 |
| Piper `es_ES-sharvard-medium` | 37 caracteres | 2,28 s | 0,29 s | 0,13 |
| Piper `es_ES-sharvard-medium` | 81 caracteres | 4,74 s | 0,52 s | 0,11 |

- fp16 de Kokoro no llega a sintetizar con `kokoro-onnx` 0.6.1 (falla en su
  propio recorte de silencio), así que la comparación real es int8 contra fp32.
- `kokoro-onnx` 0.6.1 revienta con frases de menos de ~10 caracteres al recortar
  silencio (`zero-size array`); el servicio reintenta sin `trim` y recorta aquí.

## ASR (español)

| motor | audio | tiempo | RTF |
|---|---|---|---|
| sherpa-onnx zipformer `es-kroko` (streaming, 2 hilos) | 2,1-4,8 s | 0,10-0,51 s | 0,05-0,11 |
| faster-whisper `base` int8 | 2,1-4,8 s | 1,57-1,78 s (mediana) | ~0,55 |
| faster-whisper `small` int8 | 2,1-4,8 s | 4,97 s (mediana) | ~1,8 |

- El streaming de sherpa decodifica mientras se habla: al terminar la frase sólo
  queda la cola, 0,1-0,2 s.
- Precisión: `small` transcribe mejor que `base` ("¿Qué tiempo hará mañana…?"
  frente a "que tiempo era manjana…"), pero 5 s por frase lo descartan para
  conversación. El zipformer streaming acierta las cuatro frases de prueba
  (un fallo: "cena" → "escena").

## LLM (OpenCode Go, `deepseek-v4.1-flash`, streaming)

| configuración | TTFT | notas |
|---|---|---|
| por defecto | 2,24-2,49 s | 30-40 deltas de `reasoning_content` antes del primer token visible |
| `reasoning_effort: none` | 1,48 s | sin razonamiento |
| `chat_template_kwargs.thinking=false` | 0,82-1,71 s | sin razonamiento; es lo que usa la app |
| `chat_template_kwargs` + `reasoning_effort` | 1,25-1,61 s | estable, también aceptado |

## Turno completo (fin de voz → primer audio en el navegador)

| voz | latencia |
|---|---|
| Piper | 1,8-1,9 s |
| Kokoro (`ef_dora`) | 2,8 s |

Desglose con Piper: VAD 0,5 s + cola del ASR 0,1 s + TTFT 1,2-1,6 s + primera
frase 0,3 s. Con Kokoro la primera frase cuesta 1,4 s más.
