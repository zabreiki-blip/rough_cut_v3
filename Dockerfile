FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
WORKDIR /app

# catdoc is used to extract legacy .doc files.
RUN apt-get update \
    && apt-get install -y --no-install-recommends antiword catdoc curl fonts-dejavu-core ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY requirements-whisper.txt .
RUN pip install --no-cache-dir -r requirements-whisper.txt

COPY src ./src
COPY scripts ./scripts
COPY STAGE_1_PLAN.md ./STAGE_1_PLAN.md

RUN chmod +x ./scripts/analyze-script ./scripts/synthesize-tts ./scripts/validate-stage1 \
    && mkdir -p /app/projects /app/data /app/.cache/huggingface \
    && useradd --system --uid 10001 --home-dir /app --no-create-home roughcut \
    && chown -R roughcut:roughcut /app

ENV PYTHONPATH=/app/src
EXPOSE 8000

USER roughcut

CMD ["gunicorn", "-b", "0.0.0.0:8000", "--workers", "1", "--threads", "4", "--timeout", "3600", "api:app"]
