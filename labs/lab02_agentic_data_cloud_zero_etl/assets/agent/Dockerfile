# ==============================================================================
# Lab 11 — dispatch agent container
#
# Nothing in here decides which identity the agent runs as; service.yaml does,
# via serviceAccountName. That separation is the point: this image is read-only
# over the data no matter who builds it.
# ==============================================================================
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Dependencies first, so editing router.py or orchestrator.py -- which is every
# rebuild during the lab -- does not reinstall ADK.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY __init__.py main.py orchestrator.py router.py dispatch.py \
     bq_analyst.py catalog_specialist.py graph_specialist.py \
     remote_transact.py a2a_surface.py ./

# Cloud Run sets $PORT and it is not always 8080.
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]
