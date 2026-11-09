FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      espeak-ng libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir kokoro-onnx==0.6.1 piper-tts==1.8.0 numpy==2.2.6

WORKDIR /srv
COPY server.py /srv/server.py

ENV TTS_PORT=9000 \
    TTS_MODEL_PATH=/models/kokoro-v1.0.int8.onnx \
    TTS_VOICES_PATH=/models/voices-v1.0.bin \
    TTS_PIPER_DIR=/models/piper \
    OMP_NUM_THREADS=2

EXPOSE 9000
CMD ["python", "-u", "/srv/server.py"]
