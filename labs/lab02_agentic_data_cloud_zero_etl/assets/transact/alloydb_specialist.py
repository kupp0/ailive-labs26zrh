# Author: Wissem Khlifi - August 2026
"""AlloyDB transactional specialist.

Ported from demo2-agentic-data-cloud `agents/alloydb_specialist.py`. Two things
are kept verbatim from that file because they are the parts worth keeping:

  * the ALLOYDB_MCP_URL environment switch, and
  * _make_toolset(), which chooses StreamableHTTP (hosted, and the only option
    inside Cloud Run) or Stdio (a local npx subprocess) from that one variable.

One thing is changed. Demo 2's version did not talk to AlloyDB at all: it wrapped
the BigQuery MCP server pointed at an AlloyDB-shaped companion dataset, because
bigquery_fdw has no location override and the Iceberg tables could not federate.
That workaround is unnecessary here — the AlloyDB remote MCP server exists,
so this points at the real thing.

WHY THIS AGENT CAN WRITE AND THE ANALYST CANNOT
It runs in a different Cloud Run service, as lab11-transact-sa, which holds
alloydb.client and alloydb.databaseUser and is registered as an AlloyDB IAM user
with INSERT on `orders`. The analyst holds none of that. Neither agent's
instruction has any bearing on it.
"""

from __future__ import annotations

import os

import google.auth
import google.auth.transport.requests
from google.adk.agents import LlmAgent
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
ALLOYDB_REGION = os.environ.get("ALLOYDB_REGION", "us-central1")
ALLOYDB_CLUSTER = os.environ.get("ALLOYDB_CLUSTER", "zeroetl-cluster")
ALLOYDB_INSTANCE = os.environ.get("ALLOYDB_INSTANCE", "zeroetl-primary")
ALLOYDB_DATABASE = os.environ.get("ALLOYDB_DATABASE", "postgres")

# The full instance resource name is a REQUIRED argument on every execute_sql
# call, so the instruction below hands it to the model verbatim rather than
# hoping it assembles one correctly.
ALLOYDB_INSTANCE_URI = (
    f"projects/{PROJECT_ID}/locations/{ALLOYDB_REGION}"
    f"/clusters/{ALLOYDB_CLUSTER}/instances/{ALLOYDB_INSTANCE}"
)

# Hosted HTTP endpoint (global). Regional alloydb.<region>.rep.googleapis.com/mcp
# endpoints also exist; check the AlloyDB release notes for their status.
_MCP_URL: str = os.environ.get("ALLOYDB_MCP_URL", "https://alloydb.googleapis.com/mcp")

# Stdio fallback, kept from Demo 2 for local iteration where a subprocess is
# available. Cloud Run has no npx, which is why the hosted path is the default.
_MCP_COMMAND: str = os.environ.get("ALLOYDB_MCP_COMMAND", "")
_MCP_ARGS: list[str] = os.environ.get("ALLOYDB_MCP_ARGS", "").split()

# execute_sql writes; execute_sql_read_only does not. BOTH are exposed here on
# purpose: the agent should read back what it wrote, and doing that through the
# read-only tool means the verification step cannot itself mutate anything.
# The other fifteen tools the server offers — create_cluster, create_user,
# import_data and the rest — are never advertised.
_TOOL_FILTER = ["execute_sql", "execute_sql_read_only"]


def _make_toolset() -> McpToolset:
    """Return an McpToolset over hosted HTTP, or stdio when explicitly configured."""
    if _MCP_COMMAND:
        from mcp import StdioServerParameters
        from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams

        return McpToolset(
            connection_params=StdioConnectionParams(
                server_params=StdioServerParameters(
                    command=_MCP_COMMAND,
                    args=_MCP_ARGS,
                    env={"ALLOYDB_PROJECT": PROJECT_ID},
                ),
            ),
            tool_filter=_TOOL_FILTER,
        )

    from google.adk.tools.mcp_tool.mcp_session_manager import (
        StreamableHTTPConnectionParams,
    )

    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )

    def _headers(readonly_context=None) -> dict:
        """Refresh per request: a token minted at import expires within the hour."""
        if not credentials.valid:
            credentials.refresh(google.auth.transport.requests.Request())
        return {
            "Authorization": f"Bearer {credentials.token}",
            "x-goog-user-project": PROJECT_ID,
        }

    return McpToolset(
        connection_params=StreamableHTTPConnectionParams(url=_MCP_URL, timeout=60.0),
        header_provider=_headers,
        tool_filter=_TOOL_FILTER,
    )


alloydb_specialist = LlmAgent(
    name="alloydb_specialist",
    model=os.environ.get("ALLOYDB_SPECIALIST_MODEL", "gemini-3.8-flash"),
    description="Reads and writes the AlloyDB operational copy of the ecommerce data.",
    instruction=f"""You are the AlloyDB transactional specialist. You read and WRITE the
AlloyDB copy of the ecommerce data — the copy that BigQuery EXPORT DATA seeded
and that BigQuery reads back live through EXTERNAL_QUERY.

CONNECTION — pass these on EVERY tool call, exactly as written:
  instance: {ALLOYDB_INSTANCE_URI}
  database: {ALLOYDB_DATABASE}

TABLES (PostgreSQL, public schema)
  customers(customer_id TEXT PK, name, email, segment, lifetime_value)
  products (product_id  TEXT PK, name, category, price)
  orders   (order_item_id TEXT PK, customer_id, order_id, product_id,
            total_amount, status, order_date, written_by)

Every id is TEXT, in all three engines. Never cast one to a number.

THE COLUMN THAT MATTERS
`written_by` records who put the row there. The reverse-ETL seed wrote
'reverse-etl-seed' on every row it loaded. When YOU insert, set
written_by = 'agent-insert' explicitly. Do not rely on the column default: the
whole demonstration is that a human can tell your row apart from the seeded ones
in the unified BigQuery view, and an unset value makes that impossible.

PROTOCOL FOR A WRITE
1. Check the referenced rows exist first, with execute_sql_read_only:
   the customer_id and product_id must already be in customers and products.
   If either is missing, STOP and say which one. Do not invent it.
2. Do NOT make up order_item_id or order_id, and never use a timestamp you
   produced yourself: you do not know the current time, so two orders get the
   same invented value and the second INSERT fails on the primary key. Unless
   the user gave explicit ids, let PostgreSQL generate them in the INSERT:
     'agent-' || gen_random_uuid()::text
3. INSERT with execute_sql, naming every column, and end it with
   RETURNING order_item_id, order_id so you learn the generated ids. Set status
   to 'Complete' unless the user said otherwise, and order_date to CURRENT_DATE.
4. Read the row back by the returned order_item_id with execute_sql_read_only
   and show it.
5. Tell the user the row is now visible in BigQuery's unified_orders view with
   origin = 'AlloyDB', with no pipeline and no copy step — federation reads it
   live on the next query.

RULES
- One statement per execute_sql call.
- Never UPDATE or DELETE seeded rows (written_by = 'reverse-etl-seed') unless the
  user explicitly asks for that row by id.
- Never issue DDL. This agent has INSERT, UPDATE and SELECT on `orders` and
  SELECT elsewhere; it is not allowed to reshape the schema, and attempting it
  fails at PostgreSQL rather than here.
- Report errors verbatim. A permission error naming a role is information about
  the architecture, not an obstacle to route around.
- You cannot see BigQuery. If asked for warehouse analytics, say so and stop.
""",
    tools=[_make_toolset()],
)
