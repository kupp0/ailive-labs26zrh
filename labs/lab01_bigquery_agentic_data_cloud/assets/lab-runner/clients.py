"""Clients for the Lab 10 console: BigQuery and Dataplex as the runner identity, and the agent.

Ported from the previous lab runner unchanged in behaviour, minus pandas: queries
return plain column names and row lists, which is all the console renders.

The identity split that Steps 7-8 demonstrate is not implemented here. It is a
property of the deployment: this process runs as lab-runner-sa (Fine-Grained
Reader) and the agent runs as ecommerce-analyst-agent (Masked Reader). Nothing
in this file could grant or remove either.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

import google.auth.exceptions
import google.auth.transport.requests
import requests
from google.cloud import bigquery
from google.oauth2 import id_token as google_id_token

# --------------------------------------------------------------------------
# Configuration (all injected by service.yaml)
# --------------------------------------------------------------------------
PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "")
AGENT_URL = os.getenv("AGENT_URL", "").rstrip("/")
DATASET = os.getenv("BQ_DATASET_ID", "ecommerce_lab")
GRAPH_DATASET = os.getenv("BQ_GRAPH_DATASET_ID", "ecommerce_graph_lab")
GLOSSARY_LOCATION = os.getenv("GLOSSARY_LOCATION", "us-central1")
AGENT_SA = os.getenv("AGENT_SA", "(unset)")
RUNNER_SA = os.getenv("RUNNER_SA", "(unset)")
GLOSSARY_ID = "ecommerce-business-terms"

# Sibling lab consoles for the lab switcher. Set by deploy.sh (either lab's)
# when the other console exists; empty means "not deployed".
LAB11_CONSOLE_URL = os.getenv("LAB11_CONSOLE_URL", "").rstrip("/")

# Lab 12 publisher console for the lab switcher. Set by Lab 12's deploy.sh (and
# re-read by this lab's deploy.sh); empty renders "not deployed".
GE_PUBLISHER_URL = os.getenv("GE_PUBLISHER_URL", "").rstrip("/")

GRAPH = f"{PROJECT_ID}.{GRAPH_DATASET}.ecommerce"

_BQ_CLIENT: bigquery.Client | None = None


@dataclass
class QueryResult:
    """A BigQuery result reduced to what the console renders.

    Attributes:
        columns: Column names in result order.
        rows: One list of cell values per row.
        seconds: Wall-clock time of the query, including result fetch.
    """

    columns: list[str]
    rows: list[list[Any]]
    seconds: float

    def first(self, name: str) -> Any:
        """Returns the value of `name` in the first row, or None if empty."""
        return self.rows[0][self.columns.index(name)] if self.rows else None


def bq() -> bigquery.Client:
    """Returns a process-wide BigQuery client for the runner identity."""
    global _BQ_CLIENT
    if _BQ_CLIENT is None:
        _BQ_CLIENT = bigquery.Client(project=PROJECT_ID)
    return _BQ_CLIENT


def run_query(sql: str, params: list[bigquery.ScalarQueryParameter | bigquery.ArrayQueryParameter] | None = None) -> QueryResult:
    """Runs SQL as the LAB RUNNER identity, in the US multi-region.

    Step 8 depends on this being the runner identity: it is the Fine-Grained
    Reader side of the masking comparison.

    Args:
        sql: A fixed statement from actions.py. Never user-supplied text.
        params: Query parameters; the only way a request value reaches SQL.

    Returns:
        The result reduced to columns and rows.
    """
    started = time.perf_counter()
    config = bigquery.QueryJobConfig(query_parameters=params or [])
    result = bq().query(sql, location="US", job_config=config).result()
    columns = [field.name for field in result.schema] if result.schema else []
    rows = [list(row.values()) for row in result]
    return QueryResult(columns=columns, rows=rows, seconds=time.perf_counter() - started)


@dataclass
class JobStats:
    """What BigQuery reports about one job, for side-by-side comparison.

    Attributes:
        elapsed_ms: job.ended - job.started, server side.
        slot_ms: Total slot milliseconds consumed.
        bytes_processed: Total bytes processed.
    """

    elapsed_ms: int
    slot_ms: int
    bytes_processed: int


def run_query_with_stats(
    sql: str, params: list[bigquery.ScalarQueryParameter | bigquery.ArrayQueryParameter] | None = None
) -> tuple[QueryResult, JobStats]:
    """Runs SQL with the result cache OFF and returns the result plus job statistics.

    Cache off matters: a cached run reports ~0 slot-ms, and the comparison
    would then measure the cache rather than the query.

    Args:
        sql: A fixed statement from actions.py.
        params: Query parameters.

    Returns:
        (result, stats).
    """
    started = time.perf_counter()
    config = bigquery.QueryJobConfig(query_parameters=params or [], use_query_cache=False)
    job = bq().query(sql, location="US", job_config=config)
    result = job.result()
    columns = [field.name for field in result.schema] if result.schema else []
    rows = [list(row.values()) for row in result]
    elapsed = int((job.ended - job.started).total_seconds() * 1000) if job.ended and job.started else 0
    stats = JobStats(
        elapsed_ms=elapsed,
        slot_ms=int(job.slot_millis or 0),
        bytes_processed=int(job.total_bytes_processed or 0),
    )
    return QueryResult(columns=columns, rows=rows, seconds=time.perf_counter() - started), stats


def glossary_terms() -> list[tuple[str, str, str]]:
    """Lists the business glossary terms as (term_id, display_name, description).

    Glossary terms live on BusinessGlossaryServiceClient. CatalogServiceClient
    carries only the glossary_path helpers, so list_glossary_terms on it raises
    AttributeError at run time.
    """
    from google.cloud import dataplex_v1  # heavy import: only when the button is pressed

    client = dataplex_v1.BusinessGlossaryServiceClient()
    parent = f"projects/{PROJECT_ID}/locations/{GLOSSARY_LOCATION}/glossaries/{GLOSSARY_ID}"
    return [
        (t.name.rsplit("/", 1)[-1], t.display_name, t.description)
        for t in client.list_glossary_terms(parent=parent)
    ]


def _id_token(audience: str) -> str:
    """Mints a Cloud Run identity token whose audience is the receiving service."""
    return google_id_token.fetch_id_token(google.auth.transport.requests.Request(), audience)


def _call(method: str, url: str, timeout: int, body: dict | None = None) -> dict[str, Any]:
    """Calls the agent with an ID token; returns JSON or {"error": ...}.

    Cloud Run authenticates service-to-service with an identity token whose
    audience is the receiving service's URL. An access token is rejected with a
    bare 401.
    """
    try:
        resp = requests.request(
            method,
            url,
            json=body,
            headers={"Authorization": f"Bearer {_id_token(AGENT_URL)}"},
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError, google.auth.exceptions.GoogleAuthError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def ask_agent(message: str, session_id: str) -> dict[str, Any]:
    """Asks the deployed agent a question over HTTP.

    Args:
        message: A fixed question from actions.py.
        session_id: A fresh id per click, so no answer comes from session history.

    Returns:
        The agent's JSON response ({response, tools_called, sql}) or {"error": ...}.
    """
    if not AGENT_URL:
        return {"error": "AGENT_URL is not set. Deploy the agent: bash deploy.sh"}
    return _call("POST", f"{AGENT_URL}/chat", 180, {"message": message, "session_id": session_id})


def agent_health() -> dict[str, Any]:
    """Returns the agent's /health JSON, or {"error": ...}."""
    if not AGENT_URL:
        return {"error": "AGENT_URL is not set"}
    return _call("GET", f"{AGENT_URL}/health", 30)
