FROM python:3.11-slim

# ffmpeg is a runtime dependency (app/audio.py shells out to ffmpeg/ffprobe),
# not just a dev-time tool, so it has to be in the final image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY tests ./tests

# Baked-in default: run without downloading the Whisper model. Override at
# `docker run -e TRANSCRIPTION_ENGINE=whisper` for the real engine - see
# README for why this is the sane default for a container that's about to
# be built/tested many times in CI.
ENV TRANSCRIPTION_ENGINE=mock
ENV STORAGE_ROOT=/srv/storage

EXPOSE 8000

# Single container, single worker process here for simplicity; see README
# "Scaling this to production" for running this as N replicas behind a
# load balancer with a real queue/broker instead of the in-memory one.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
