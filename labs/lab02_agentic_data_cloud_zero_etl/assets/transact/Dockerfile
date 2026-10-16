# ==============================================================================
# Lab 11 — transactional agent container
#
# Identical in shape to the dispatch agent's image, and deliberately so: the two
# services differ ONLY in which service account Cloud Run assigns them and which
# code they carry. Nothing in this file grants a write.
# ==============================================================================
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY __init__.py main.py alloydb_specialist.py spanner_specialist.py ./

CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]
