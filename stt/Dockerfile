FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir faster-whisper==1.2.1 numpy==2.2.6

# Weights are baked in so the container never reaches the network at run time.
ENV STT_MODEL_DIR=/models \
    STT_MODELS=base,small \
    STT_MODEL=base \
    STT_COMPUTE_TYPE=int8 \
    STT_LANGUAGE=es \
    OMP_NUM_THREADS=2
RUN python -c "\
from huggingface_hub import snapshot_download as d;\
d('Systran/faster-whisper-base', local_dir='/models/base');\
d('Systran/faster-whisper-small', local_dir='/models/small')"

WORKDIR /srv
COPY server.py /srv/server.py
EXPOSE 9000
CMD ["python", "-u", "/srv/server.py"]
