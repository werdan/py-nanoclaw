# Build from repo root: docker build -f container/Dockerfile -t nanoclaw-agent .
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --system app && useradd --system --gid app --create-home app

WORKDIR /app

COPY pyproject.toml .
COPY nanoclaw ./nanoclaw

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir .

COPY container/agent_runner.py /app/agent_runner.py

USER app

ENTRYPOINT ["python", "/app/agent_runner.py"]
