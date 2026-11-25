# syntax=docker/dockerfile:1
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

WORKDIR /app

# Create non-root user for Cloud Run security hardening
RUN groupadd --system shiftpilot && useradd --system --gid shiftpilot shiftpilot

COPY requirements.txt pyproject.toml ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY shiftpilot_agent/ ./shiftpilot_agent/
COPY evals/ ./evals/

RUN mkdir -p /app/.shiftpilot_data && chown -R shiftpilot:shiftpilot /app
USER shiftpilot

EXPOSE 8080

# Run ADK API server for Cloud Run
CMD ["sh", "-c", "adk api_server --host 0.0.0.0 --port ${PORT:-8080} ."]
