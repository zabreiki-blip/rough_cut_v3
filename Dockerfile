# Tag is kept for readability; for a fully reproducible base pin by digest:
#   docker build --build-arg PYTHON_IMAGE=python:3.11-slim@sha256:0dd364ba7e10242f07755449e3a3d0e35f9efd987952737b90def6709ab0c5ce .
# (multi-arch index digest of python:3.11-slim obtained via
#  `docker buildx imagetools inspect python:3.11-slim` on 2026-10-08; refresh periodically).
ARG PYTHON_IMAGE=python:3.11-slim
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
WORKDIR /app

# catdoc is used to extract legacy .doc files.
RUN apt-get update \
    && apt-get install -y --no-install-recommends antiword catdoc curl fonts-dejavu-core ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Versions are pinned by requirements-lock.txt used as a constraints file
# (it also lists dev packages; constraints only apply to what gets installed).
COPY requirements.txt requirements-lock.txt ./
RUN pip install --no-cache-dir -r requirements.txt -c requirements-lock.txt

COPY requirements-whisper.txt .
RUN pip install --no-cache-dir -r requirements-whisper.txt -c requirements-lock.txt

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
CMD ["gunicorn", "-b", "0.0.0.0:8000", "--workers", "1", "--threads", "4", "--timeout", "3600", "api:app"]
