"""Clients for the Lab 11 console: BigQuery as the runner identity, and the two agents.

Ported from the previous lab runner unchanged in behaviour, minus pandas: queries
return plain column names and row lists, which is all the console renders.
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
TRANSACT_URL = os.getenv("TRANSACT_URL", "").rstrip("/")
SOURCE_DATASET = os.getenv("BQ_SOURCE_DATASET_ID", "ecommerce_lab")
ZEROETL_DATASET = os.getenv("BQ_ZEROETL_DATASET_ID", "ecommerce_zeroetl")
SPANNER_DATASET = os.getenv("BQ_SPANNER_DATASET_ID", "spanner_federated")
SPANNER_INSTANCE = os.getenv("SPANNER_INSTANCE", "zeroetl-ops")
SPANNER_DATABASE = os.getenv("SPANNER_DATABASE", "ecommerce-operational")
ALLOYDB_CLUSTER = os.getenv("ALLOYDB_CLUSTER", "zeroetl-cluster")
ALLOYDB_ENABLED = os.getenv("ALLOYDB_ENABLED", "true").lower() == "true"
DISPATCH_SA = os.getenv("DISPATCH_SA", "(unset)")
TRANSACT_SA = os.getenv("TRANSACT_SA", "(unset)")
RUNNER_SA = os.getenv("RUNNER_SA", "(unset)")

# Lab switcher: Lab 10's console URL, set by either lab's deploy.sh when that
# console exists. Empty renders "not deployed".
LAB10_CONSOLE_URL = os.getenv("LAB10_CONSOLE_URL", "").rstrip("/")

# Lab 12 publisher console for the lab switcher. Set by Lab 12's deploy.sh (and
# re-read by this lab's deploy.sh); empty renders "not deployed".
GE_PUBLISHER_URL = os.getenv("GE_PUBLISHER_URL", "").rstrip("/")

UNIFIED_GRAPH = f"{PROJECT_ID}.{ZEROETL_DATASET}.UnifiedEcommerceGraph"

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

    def column(self, name: str) -> list[Any]:
        """Returns every value of one column."""
        idx = self.columns.index(name)
        return [row[idx] for row in self.rows]

    def first(self, name: str) -> Any:
        """Returns the value of `name` in the first row, or None if empty."""
        return self.rows[0][self.columns.index(name)] if self.rows else None


def bq() -> bigquery.Client:
    """Returns a process-wide BigQuery client for the runner identity."""
    global _BQ_CLIENT
    if _BQ_CLIENT is None:
        _BQ_CLIENT = bigquery.Client(project=PROJECT_ID)
    return _BQ_CLIENT


def run_query(
    sql: str,
    params: list[bigquery.ScalarQueryParameter | bigquery.ArrayQueryParameter] | None = None,
    use_cache: bool = True,
) -> QueryResult:
    """Runs SQL as the LAB RUNNER identity, in the US multi-region.

    location="US" is not optional. The unified view spans a US multi-region
    dataset, a federated Spanner dataset and an EXTERNAL_QUERY against AlloyDB in
    us-central1; the job must run in US for BigQuery to consider them co-located.

    Args:
        sql: A fixed statement from actions.py. Never user-supplied text.
        params: Optional named query parameters for validated user input.
        use_cache: False for latency measurements.

    Returns:
        The result reduced to columns and rows.
    """
    config = bigquery.QueryJobConfig(query_parameters=params or [], use_query_cache=use_cache)
    started = time.perf_counter()
    result = bq().query(sql, location="US", job_config=config).result()
    columns = [field.name for field in result.schema]
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

    Same contract as Lab 10's clients.run_query_with_stats: a cached run reports
    ~0 slot-ms, and a comparison would then measure the cache, not the query.

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


_SPANNER_DB: Any = None


def spanner_query(sql: str, params: dict[str, str]) -> QueryResult:
    """Runs one read-only statement on the operational Spanner database.

    Runs as the runner identity (roles/spanner.databaseReader) in a read-only
    snapshot, so it cannot write. Used for the Step 10 operational graph read:
    a GQL traversal of EcommerceOpsGraph for one validated customer.

    Args:
        sql: A fixed statement from actions.py. Never user-supplied text.
        params: Named STRING parameters, already validated by the caller.

    Returns:
        The result; `seconds` is wall-clock time around the read, which on the
        first click also includes opening a Spanner session.
    """
    global _SPANNER_DB
    from google.cloud import spanner  # heavy import: only when the button is pressed
    from google.cloud.spanner_v1 import param_types

    if _SPANNER_DB is None:
        _SPANNER_DB = spanner.Client(project=PROJECT_ID).instance(SPANNER_INSTANCE).database(SPANNER_DATABASE)
    started = time.perf_counter()
    with _SPANNER_DB.snapshot() as snapshot:
        result = snapshot.execute_sql(sql, params=params, param_types={k: param_types.STRING for k in params})
        rows = [list(row) for row in result]
        columns = [field.name for field in result.fields]
    return QueryResult(columns=columns, rows=rows, seconds=time.perf_counter() - started)


def timed_query(sql: str, runs: int = 3) -> float:
    """Returns the MEDIAN wall-clock seconds of `runs` executions, cache off.

    Median of three, not a single run: one measurement of a ~2s cloud query
    carries enough jitter to invert the comparisons the Step 9 breakdown makes.

    Args:
        sql: A fixed statement.
        runs: Number of executions.

    Returns:
        Median seconds.
    """
    times = sorted(run_query(sql, use_cache=False).seconds for _ in range(runs))
    return times[len(times) // 2]


def _id_token(audience: str) -> str:
    """Mints a Cloud Run identity token whose audience is the receiving service."""
    return google_id_token.fetch_id_token(google.auth.transport.requests.Request(), audience)


def _call(method: str, url: str, audience: str, timeout: int, body: dict | None = None) -> dict[str, Any]:
    """Calls a Cloud Run service with an ID token; returns JSON or {"error": ...}.

    An access token is rejected by Cloud Run with a bare 401, so the audience
    must be the receiving service's URL.
    """
    try:
        resp = requests.request(
            method,
            url,
            json=body,
            headers={"Authorization": f"Bearer {_id_token(audience)}"},
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError, google.auth.exceptions.GoogleAuthError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def ask_agent(message: str, session_id: str) -> dict[str, Any]:
    """Asks the dispatch agent a question over HTTP.

    Args:
        message: A fixed or validated question built in actions.py.
        session_id: A fresh id per click, so no answer comes from session history.

    Returns:
        The agent's JSON response, or {"error": ...}.
    """
    if not AGENT_URL:
        return {"error": "AGENT_URL is not set. Deploy the agents: bash deploy.sh"}
    return _call("POST", f"{AGENT_URL}/chat", AGENT_URL, 240, {"message": message, "session_id": session_id})


def route_only(message: str) -> dict[str, Any]:
    """Asks the dispatch service how it WOULD classify a query, running nothing."""
    if not AGENT_URL:
        return {"error": "AGENT_URL is not set."}
    return _call("POST", f"{AGENT_URL}/route", AGENT_URL, 90, {"message": message})


def service_health(url: str) -> dict[str, Any]:
    """Returns the /health JSON of a Cloud Run service, or {"error": ...}."""
    if not url:
        return {"error": "not deployed"}
    return _call("GET", f"{url}/health", url, 30)
