# Author: Wissem Khlifi - August 2026
"""
==============================================================================
Lab 10: From Data to Agent (BigQuery & Knowledge Catalog)
Starter template for the governed ecommerce analyst agent.
Author: Wissem Khlifi
==============================================================================

This agent talks to two REMOTE MCP SERVERS instead of calling the Python client
libraries directly. It writes no BigQuery or Dataplex API code at all:

  * BigQuery MCP Server        https://bigquery.googleapis.com/mcp
      Examines datasets and tables, generates and executes SQL, returns results.
      Tools: list_dataset_ids, get_dataset_info, list_table_ids, get_table_info,
             execute_sql_readonly, execute_sql

  * Knowledge Catalog MCP      https://dataplex.googleapis.com/mcp
      Searches governed metadata and retrieves glossary term definitions.
      Tools: search_entries, lookup_entry, lookup_context

  A few features in this architecture are pre-GA. Check the official product
  documentation and release notes for current status before you recommend it.

You will complete three TODOs during this lab:

  TODO_1  Point the agent at your ephemeral project.
  TODO_2  Configure the two MCP server connections.
  TODO_3  Write the agent instruction that forces catalog-first SQL.

WHERE THE GUARDRAIL LIVES NOW
-----------------------------
The earlier version of this lab enforced read-only access with a regex inside a
Python tool. That code is gone -- the MCP server owns the query path now -- but
the principle has not changed: the guardrail still must not live in the prompt.
It now sits in two places, both outside the model's reach:

  1. `tool_filter` below. The BigQuery MCP server exposes `execute_sql`, which
     can write. Filtering it out means the tool is never advertised to the
     model, so no prompt can invoke what was never on the menu.
  2. IAM. The agent's service account holds roles/bigquery.dataViewer and
     roles/bigquery.jobUser -- neither of which can modify data. Even a
     hand-crafted DML statement is refused by BigQuery itself.

Belt and braces: 1 stops the model asking, 2 stops the request succeeding.
"""

import os

import google.auth
import google.auth.transport.requests
from google.adk import Agent
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool import StreamableHTTPConnectionParams

# ------------------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------------------
# TODO_1: Replace "TODO_1" with your ephemeral lab project ID, or export
#         GOOGLE_CLOUD_PROJECT in the Workstation terminal before running:
#             export GOOGLE_CLOUD_PROJECT=$(gcloud config get-value project)
PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "TODO_1")

DATASET_ID = "ecommerce_lab"

# Remote MCP endpoints.
BIGQUERY_MCP_URL = "https://bigquery.googleapis.com/mcp"
CATALOG_MCP_URL = "https://dataplex.googleapis.com/mcp"

# The read-only subset of the BigQuery MCP toolset. `execute_sql` is deliberately
# absent -- see "WHERE THE GUARDRAIL LIVES NOW" above.
BIGQUERY_READONLY_TOOLS = [
    "list_dataset_ids",
    "get_dataset_info",
    "list_table_ids",
    "get_table_info",
    "execute_sql_readonly",
]

# `search_entries` is deliberately ABSENT from this list. The MCP server's
# version searches every project the caller can read: an unscoped call from this
# lab returns glossary terms from four unrelated projects, so deleting the
# Revenue term here changes nothing -- the agent finds someone else's Revenue and
# answers from it, with search_entries in the trace, looking perfectly grounded.
#
# The MCP tool CAN be scoped (scopeType=PROJECT, scopeId=<project>), but those
# are arguments the model chooses to pass or not. An argument the model controls
# is not a guardrail. The scoped implementation below is registered under the
# same name instead, so the only search_entries this agent can reach is one that
# cannot leave this project.
CATALOG_TOOLS = [
    "lookup_entry",
    "lookup_context",
]


# ------------------------------------------------------------------------------
# Authentication (COMPLETE -- read it, you do not need to change it)
# ------------------------------------------------------------------------------
# Both servers are ordinary Google APIs, so they take an OAuth 2.0 bearer token
# from Application Default Credentials. `header_provider` is called per request,
# which matters: a token minted once at import would expire after an hour and
# every later tool call would fail with 401.
_CREDENTIALS, _ = google.auth.default(
    scopes=["https://www.googleapis.com/auth/cloud-platform"]
)


def google_auth_headers(readonly_context=None) -> dict:
    """Returns a freshly-refreshed ADC bearer token for each MCP request."""
    if not _CREDENTIALS.valid:
        _CREDENTIALS.refresh(google.auth.transport.requests.Request())
    return {"Authorization": f"Bearer {_CREDENTIALS.token}"}


def search_entries(query: str, page_size: int = 10) -> dict:
    """Search the Knowledge Catalog for governed entries matching a query.

    Use this FIRST for any business term in the user's question, and derive the
    SQL filters from the definition it returns.

    Args:
        query: Search string, e.g. "revenue" or "high value customer".
        page_size: Maximum number of entries to return.

    Returns:
        The raw searchEntries response. An empty `results` list means the term
        has no governed definition in this project.
    """
    # The scope is fixed here, in code, and is not a parameter the model can
    # set. `projects/<id>` restricts the search space to this project; without
    # it the API defaults to the whole organization the project sits in.
    #
    # Note `projectId`/`x-goog-user-project` do NOT do this. That field is
    # documented as "the project to which the request should be attributed" --
    # billing and quota, not filtering. Verified against the live API: attributed
    # to this project, results still came back from four others.
    if not _CREDENTIALS.valid:
        _CREDENTIALS.refresh(google.auth.transport.requests.Request())
    session = google.auth.transport.requests.AuthorizedSession(_CREDENTIALS)
    resp = session.post(
        f"https://dataplex.googleapis.com/v1/projects/{PROJECT_ID}"
        "/locations/global:searchEntries",
        json={
            "query": query,
            "pageSize": page_size,
            "scope": f"projects/{PROJECT_ID}",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def mcp_toolset(url: str, tool_filter: list) -> McpToolset:
    """Builds an ADK toolset bound to a remote, ADC-authenticated MCP server.

    Args:
        url: The MCP endpoint, e.g. https://bigquery.googleapis.com/mcp
        tool_filter: The exact tool names to expose to the model. Anything not
            named here is never advertised, and therefore cannot be called.

    Returns:
        An McpToolset ready to pass to Agent(tools=[...]).
    """
    return McpToolset(
        connection_params=StreamableHTTPConnectionParams(url=url, timeout=30.0),
        header_provider=google_auth_headers,
        tool_filter=tool_filter,
    )


# ------------------------------------------------------------------------------
# TODO_2: Connect the two MCP servers (YOUR TURN)
# ------------------------------------------------------------------------------
# Replace both `None`s using the `mcp_toolset` helper above:
#
#   CATALOG_TOOLSET  -> CATALOG_MCP_URL  with CATALOG_TOOLS
#   BIGQUERY_TOOLSET -> BIGQUERY_MCP_URL with BIGQUERY_READONLY_TOOLS
#
#   Think about the filter before you type it. Passing tool_filter=None would
#   expose every tool the server offers -- including `execute_sql`, which can
#   DROP a table. The filter is the guardrail; it is not a tidiness setting.
#
#   agy prompt hint:
#     agy -i "In agent/agent.py, replace the TODO_2 placeholders: build
#      CATALOG_TOOLSET from CATALOG_MCP_URL with CATALOG_TOOLS, and
#      BIGQUERY_TOOLSET from BIGQUERY_MCP_URL with BIGQUERY_READONLY_TOOLS,
#      both via the mcp_toolset helper. Do not widen either tool_filter and do
#      not add execute_sql."
CATALOG_TOOLSET = None
BIGQUERY_TOOLSET = None

if CATALOG_TOOLSET is None or BIGQUERY_TOOLSET is None:
    raise NotImplementedError(
        "TODO_2: connect the Knowledge Catalog and BigQuery MCP servers "
        "using the mcp_toolset() helper above."
    )


# ------------------------------------------------------------------------------
# Agent definition
# ------------------------------------------------------------------------------
# TODO_3: Replace "TODO_3" below with the agent's system instruction. It must:
#
#   * Be catalog-first: for any business term in the user's question (for
#     example "high value customer", "revenue", "churn risk"), call
#     `search_entries` on the Knowledge Catalog MCP server FIRST and derive the
#     SQL filters from the governed definition it returns -- never invent a
#     threshold or a column. `lookup_entry` fetches the full detail of a hit.
#   * Halt on a miss: if `search_entries` returns nothing for a term, stop and
#     ask the user to confirm a definition. Do not guess and do not proceed.
#   * Restrict every query to the `ecommerce_lab` dataset in this project.
#     Fully qualify tables as `PROJECT_ID.ecommerce_lab.<table>` and refuse
#     questions that require data outside it. The available tables are
#     customers, orders, and products.
#   * Explore before it queries: `get_table_info` is cheaper than a failed
#     query, and the schema it returns is authoritative.
#   * Use `execute_sql_readonly` for every query, aggregate in SQL rather than
#     pulling raw rows, and surface tool errors instead of retrying blindly.
#   * Explain its grounding: every answer states which catalog term was used
#     and how that definition mapped onto the SQL that was run.
#   * Report masked values honestly: `customers.email` is protected by
#     column-level security, so this agent receives a masked address. It must
#     say so rather than presenting the mask as a real address.
#
#   agy prompt hint:
#     agy -i "In agent/agent.py, replace the TODO_3 placeholder with the
#      instruction string for the ecommerce_analyst agent. It must force a
#      catalog-first workflow (call search_entries on the Knowledge Catalog MCP
#      server before writing SQL, and use only the governed definition it
#      returns), halt and ask when a term has no definition, restrict all SQL to
#      the ecommerce_lab dataset (customers, orders, products) with fully
#      qualified table names, use execute_sql_readonly for every query, and
#      require every answer to name the catalog term it relied on."
root_agent = Agent(
    name="ecommerce_analyst",
    model="gemini-3.8-flash",
    description="Governed analyst that answers ecommerce questions using catalog-grounded BigQuery SQL over remote MCP servers.",
    instruction="TODO_3",
    # search_entries is the scoped function above, not the MCP tool of the same
    # name -- that one is not in CATALOG_TOOLS and is therefore unreachable.
    tools=[CATALOG_TOOLSET, BIGQUERY_TOOLSET, search_entries],
)
