# ==============================================================================
# Lab 11 — lab console container (FastAPI + Jinja, Python only)
# ==============================================================================
# Every participant builds this image in their own project, so it must build
# with pip alone: no Node, no npm, no CDN fetch at build time.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py actions.py clients.py validation.py graph_story.py diagrams.html ./
COPY templates/ ./templates/
COPY static/ ./static/

CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips='*'"]
