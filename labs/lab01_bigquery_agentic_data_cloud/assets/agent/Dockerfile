# ==============================================================================
# Lab 10 — agent container
#
# Packages the agent.py the participant completed. Nothing in here decides which
# identity the agent runs as; service.yaml does, via serviceAccountName.
# ==============================================================================
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Dependencies first so an edit to agent.py -- which is every rebuild during the
# lab -- does not reinstall ADK.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent.py main.py a2a_surface.py __init__.py ./

# Cloud Run sets $PORT and it is not always 8080.
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]
