FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OMP_NUM_THREADS=1 \
    ASR_DIR=/models/sherpa

WORKDIR /srv
COPY requirements.txt /srv/requirements.txt
RUN pip install --no-cache-dir -r /srv/requirements.txt

COPY main.py pipeline.py asr.py /srv/
COPY static /srv/static

EXPOSE 9000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9000", "--ws-ping-interval", "25", "--ws-ping-timeout", "60", "--no-access-log"]
