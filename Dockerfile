FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
WORKDIR /app

# catdoc is used to extract legacy .doc files.
RUN apt-get update \
    && apt-get install -y --no-install-recommends antiword catdoc curl fonts-dejavu-core ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Reproducible runtime install: requirements-lock.txt is a universal uv lock
# (requirements.txt + requirements-whisper.txt, Python 3.11, hashes for linux
# x86_64 and aarch64 wheels). Regenerate it with scripts/lock-deps.
COPY requirements-lock.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements-lock.txt

COPY src ./src
COPY scripts ./scripts

RUN chmod +x ./scripts/analyze-script ./scripts/synthesize-tts ./scripts/validate-stage1 \
    && mkdir -p /app/projects /app/data /app/.cache/huggingface \
    && useradd --system --uid 10001 --home-dir /app --no-create-home roughcut \
    && chown -R roughcut:roughcut /app

ENV PYTHONPATH=/app/src
EXPOSE 8000

USER roughcut

# Single worker by design: revision activation and normalizer state use
# in-process locks (see src/api.py, src/stage4/versioning.py), not
# cross-process locks. Do not raise --workers without adding an
# inter-process file lock (e.g. fcntl/filelock) around project writes.
#
# Network note: binds 0.0.0.0:8000 with no TLS, only token auth
# (ROUGH_CUT_API_TOKEN). Do not expose this port to the internet directly;
# publish it as 127.0.0.1:8000 on the host, or put a TLS-terminating
# reverse proxy in front of it.
CMD ["gunicorn", "-b", "0.0.0.0:8000", "--workers", "1", "--threads", "4", "--timeout", "3600", "--no-control-socket", "api:app"]
