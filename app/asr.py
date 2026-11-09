"""Streaming Spanish ASR with sherpa-onnx (k2-fsa zipformer transducer).

The model decodes while the user is still talking, so at the end of an utterance
only the tail is left: measured on this box, 0.1-0.2 s after the last frame and
an RTF near 0.05-0.1 while speaking, against 1.6-5.0 s for a batch whisper pass.
"""

from __future__ import annotations

import os
import sys
import threading

import numpy as np

SAMPLE_RATE = 16000
MODEL_DIR = os.environ.get("ASR_DIR", "/models/sherpa")
TAIL_SECONDS = float(os.environ.get("ASR_TAIL", "0.5"))

_instance: "Asr | None" = None
_lock = threading.Lock()


class Asr:
    def __init__(self, model_dir: str = MODEL_DIR) -> None:
        import sherpa_onnx

        tokens = os.path.join(model_dir, "tokens.txt")
        encoder = os.path.join(model_dir, "encoder.onnx")
        decoder = os.path.join(model_dir, "decoder.onnx")
        joiner = os.path.join(model_dir, "joiner.onnx")
        for path in (tokens, encoder, decoder, joiner):
            if not os.path.exists(path):
                raise RuntimeError(f"missing ASR file: {path}")
        self.recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=tokens, encoder=encoder, decoder=decoder, joiner=joiner,
            num_threads=int(os.environ.get("ASR_THREADS", "2")),
            sample_rate=SAMPLE_RATE, feature_dim=80,
            decoding_method="greedy_search",
            enable_endpoint_detection=False,     # this stack owns the turn boundary
        )
        sys.stderr.write(f"asr: streaming model ready from {model_dir}\n")

    def stream(self):
        return self.recognizer.create_stream()

    def feed(self, stream, samples: np.ndarray) -> None:
        if samples.size == 0:
            return
        stream.accept_waveform(SAMPLE_RATE, samples.astype(np.float32))
        while self.recognizer.is_ready(stream):
            self.recognizer.decode_stream(stream)

    def partial(self, stream) -> str:
        return (self.recognizer.get_result(stream) or "").strip()

    def finish(self, stream) -> str:
        tail = np.zeros(int(SAMPLE_RATE * TAIL_SECONDS), dtype=np.float32)
        stream.accept_waveform(SAMPLE_RATE, tail)
        stream.input_finished()
        while self.recognizer.is_ready(stream):
            self.recognizer.decode_stream(stream)
        return (self.recognizer.get_result(stream) or "").strip()


def instance() -> "Asr | None":
    """The shared recognizer, or None when the model is not installed."""
    global _instance
    if _instance is None:
        with _lock:
            if _instance is None:
                try:
                    _instance = Asr()
                except Exception as error:  # noqa: BLE001 - optional accelerator
                    sys.stderr.write(f"asr: disabled ({error})\n")
                    return None
    return _instance
