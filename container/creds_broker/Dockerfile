# Build from repo root: docker build -f container/creds_broker/Dockerfile -t nanoclaw-creds-broker .
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Distinct UID for compartmentalization. Bot/agent UIDs differ; secrets file
# is owned by `broker` and only readable here.
RUN groupadd --system --gid 10010 broker \
    && useradd --system --uid 10010 --gid broker --create-home broker

WORKDIR /app

# Only the libs the broker actually needs — no telegram, no openai, no SDK.
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir \
        "google-auth>=2.30" \
        "google-auth-httplib2>=0.2" \
        "requests>=2.32"

COPY container/creds_broker/server.py /app/server.py

USER broker

ENTRYPOINT ["python", "/app/server.py"]
