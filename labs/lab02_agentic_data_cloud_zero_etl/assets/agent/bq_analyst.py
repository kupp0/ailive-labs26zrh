# Author: Wissem Khlifi - August 2026
"""BigQuery analyst: the read-only specialist over the unified layer.

Adapted from Lab 10 `src/agent/agent.py`. Lab 10's agent had one dataset and one
job; this one has three engines behind a view and a graph on top of them, but
the guardrail is unchanged and lives in exactly the same two places:

  1. tool_filter. `execute_sql` -- the BigQuery MCP tool that can write -- is
     never advertised, so no prompt can invoke what was never on the menu.
  2. IAM. This service runs as lab11-dispatch-sa, which holds
     roles/bigquery.dataViewer and roles/bigquery.jobUser. Neither can modify
     data, so even a hand-crafted DML statement is refused by BigQuery itself.

Belt and braces: (1) stops the model asking, (2) stops the request succeeding.
"""

from __future__ import annotations

import os

import google.auth
import google.auth.transport.requests
from google.adk import Agent
from google.adk.tools.mcp_tool import McpToolset, StreamableHTTPConnectionParams

from catalog_specialist import search_entries

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
SOURCE_DATASET = os.environ.get("BQ_SOURCE_DATASET_ID", "ecommerce_lab")
ZEROETL_DATASET = os.environ.get("BQ_ZEROETL_DATASET_ID", "ecommerce_zeroetl")

BIGQUERY_MCP_URL = "https://bigquery.googleapis.com/mcp"

# The read-only subset. `execute_sql` is deliberately absent -- see the module
# docstring. Widening this list is not a tidiness decision; it is the guardrail.
BIGQUERY_READONLY_TOOLS = [
    "list_dataset_ids",
    "get_dataset_info",
    "list_table_ids",
    "get_table_info",
    "execute_sql_readonly",
]

_CREDENTIALS, _ = google.auth.default(
    scopes=["https://www.googleapis.com/auth/cloud-platform"]
)


def google_auth_headers(readonly_context=None) -> dict:
    """Return a freshly-refreshed ADC bearer token for each MCP request.

    Called per request on purpose: a token minted once at import expires after an
    hour, and every tool call after that fails with a 401 that looks like a
    permissions problem.
    """
    if not _CREDENTIALS.valid:
        _CREDENTIALS.refresh(google.auth.transport.requests.Request())
    return {"Authorization": f"Bearer {_CREDENTIALS.token}"}


def mcp_toolset(url: str, tool_filter: list[str]) -> McpToolset:
    """Build an ADK toolset bound to a remote, ADC-authenticated MCP server.

    Args:
        url: The MCP endpoint.
        tool_filter: The exact tool names to expose. Anything not named here is
            never advertised to the model, and therefore cannot be called.

    Returns:
        An McpToolset ready to pass to Agent(tools=[...]).
    """
    return McpToolset(
        connection_params=StreamableHTTPConnectionParams(url=url, timeout=30.0),
        header_provider=google_auth_headers,
        tool_filter=tool_filter,
    )


bq_analyst_specialist = Agent(
    name="bq_analyst_specialist",
    model=os.environ.get("ANALYST_MODEL", "gemini-3.8-flash"),
    description="Answers analytical questions over the unified three-engine view, grounded in the catalog.",
    instruction=f"""You are the analytical specialist for a zero-ETL data platform in
project {PROJECT_ID}.

WHAT MAKES THIS PLATFORM UNUSUAL
The same ecommerce rows live in three engines. BigQuery holds the warehouse copy;
AlloyDB and Spanner hold operational copies that BigQuery EXPORT DATA wrote and
that federation reads back live. You do not need to know which engine a row is
in — `{ZEROETL_DATASET}.unified_orders` reads all three in one query and tags
every row with an `origin` column: 'BigQuery', 'AlloyDB' or 'Spanner'.

TABLES YOU MAY QUERY (fully qualify every one)
  `{PROJECT_ID}.{ZEROETL_DATASET}.unified_orders`      all three engines, live
  `{PROJECT_ID}.{ZEROETL_DATASET}.unified_customers`   customer entities
  `{PROJECT_ID}.{ZEROETL_DATASET}.unified_products`    product entities
  `{PROJECT_ID}.{SOURCE_DATASET}.orders|customers|products`  the BigQuery copy alone

Refuse questions that need data outside these.

PROTOCOL
1. If the question contains a business term — "revenue", "high value customer",
   "churn risk", anything whose meaning is a business decision rather than a
   column name — call search_entries FIRST and take the definition from what it
   returns. Never invent a threshold, a status filter or a column.
2. If search_entries returns nothing for a term, STOP. Say the term has no
   governed definition and ask the user to confirm one. Do not guess.
3. Call get_table_info before writing SQL against a table you have not read this
   session. It is cheaper than a failed query and the schema it returns is
   authoritative.
4. Run every query with execute_sql_readonly. Aggregate in SQL; never pull raw
   rows back and total them yourself.
5. When a question is about WHERE data lives or whether the engines agree,
   GROUP BY origin. That column is the whole point of the unified view.

RULES
- Every answer states which catalog term you relied on and how that definition
  became a filter in the SQL you ran.
- `customers.email` is protected by column-level security and you receive a
  MASKED value. Say so when you surface it. Never present a mask as a real
  address.
- Surface tool errors verbatim rather than retrying blindly. An error naming a
  permission is information, not an obstacle.
- You have no write tool and no write permission. If asked to insert or update,
  say plainly that this agent cannot write and that the request must go to the
  transactional agent — then stop.
""",
    tools=[
        mcp_toolset(BIGQUERY_MCP_URL, BIGQUERY_READONLY_TOOLS),
        # The scoped function, not the MCP tool of the same name. See
        # catalog_specialist.py for why that distinction is load-bearing.
        search_entries,
    ],
)
