"""Compare ASR engines on real syntheses — accuracy first, latency second.

Runs inside voz-net so it can reach voz-tts:

    docker run --rm -i --network voz-net -v voz-models:/models python:3.12-slim \
        sh -c 'pip install -q sherpa-onnx==2.0.1 numpy faster-whisper==1.2.1; python -' < tests/asr_compare.py
"""

import io
import json
import statistics
import time
import urllib.request
import wave

import numpy as np
import sherpa_onnx

REFERENCE = 16000
TEXTS = [
    "Hola Javier. Para ir de Madrid a Valencia, el tren es lo más cómodo: unas dos horas y sin tráfico. ¿Quieres que te mire horarios?",
    "Vale, pues apúntame una cosa: mañana tengo que llamar al médico a las nueve.",
    "Oye, ¿me pones un ejemplo de cena rápida sin cocinar?",
    "Ponme la radio, por favor, y sube un poco el volumen.",
    "Mañana por la tarde me paso por la oficina, pero avísame si hay algo urgente.",
]


def synth(text: str) -> np.ndarray:
    request = urllib.request.Request(
        "http://voz-tts:9000/speak",
        data=json.dumps({"text": text, "voice": "es_piper_f"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    pcm = urllib.request.urlopen(request, timeout=120).read()
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    return np.interp(np.arange(0, samples.size, 24000 / REFERENCE),
                     np.arange(samples.size), samples).astype(np.float32)


def load_sherpa() -> "sherpa_onnx.OnlineRecognizer":
    return sherpa_onnx.OnlineRecognizer.from_transducer(
        tokens="/models/sherpa/tokens.txt", encoder="/models/sherpa/encoder.onnx",
        decoder="/models/sherpa/decoder.onnx", joiner="/models/sherpa/joiner.onnx",
        num_threads=2, sample_rate=REFERENCE, feature_dim=80, decoding_method="greedy_search")


def sherpa_run(rec, audio: np.ndarray) -> tuple[str, float]:
    stream = rec.create_stream()
    started = time.time()
    stream.accept_waveform(REFERENCE, audio)
    stream.accept_waveform(REFERENCE, np.zeros(8000, dtype=np.float32))
    stream.input_finished()
    while rec.is_ready(stream):
        rec.decode_stream(stream)
    return rec.get_result(stream), time.time() - started


def whisper_run(model, audio: np.ndarray) -> tuple[str, float]:
    started = time.time()
    segments, _ = model.transcribe(audio, language="es", beam_size=1, vad_filter=True,
                                   condition_on_previous_text=False, without_timestamps=True)
    return " ".join(s.text.strip() for s in segments), time.time() - started


def main() -> None:
    audio = [synth(t) for t in TEXTS]
    rec = load_sherpa()

    from faster_whisper import WhisperModel

    whisper = {}
    for name, local in (("base", "/models/base"), ("small", "/models/small")):
        try:
            whisper[name] = WhisperModel(local, device="cpu", compute_type="int8", cpu_threads=2)
        except Exception as error:  # noqa: BLE001
            print(f"whisper {name} unavailable: {error}")

    for engine, runner in [("sherpa-kroko", lambda a: sherpa_run(rec, a))] + [
        (f"whisper-{n}", (lambda m: lambda a: whisper_run(m, a))(m)) for n, m in whisper.items()
    ]:
        times = []
        print(f"\n## {engine}")
        for text, samples in zip(TEXTS, audio):
            result, seconds = runner(samples)
            times.append(seconds)
            print(f"  {len(samples)/REFERENCE:5.2f}s -> {seconds:5.2f}s | {result}")
            print(f"           ref: {text}")
        print(f"  median {statistics.median(times):.2f}s")


if __name__ == "__main__":
    main()
