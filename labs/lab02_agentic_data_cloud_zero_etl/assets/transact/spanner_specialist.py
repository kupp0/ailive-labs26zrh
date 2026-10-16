# Author: Wissem Khlifi - August 2026
"""Spanner transactional specialist — reads, writes, and traverses the graph.

The toolset wiring below is complete. The INSTRUCTION is TODO_C, and it is the
hardest of the three TODOs on purpose: this agent is the only one in the lab that
has to understand a two-phase write AND choose between scanning and traversing.

Structure follows Lab 02's Spanner MCP wiring and Demo 2's graph_specialist
shape.
"""

from __future__ import annotations

import os

import google.auth
import google.auth.transport.requests
from google.adk.agents import LlmAgent
from google.adk.tools.mcp_tool import McpToolset, StreamableHTTPConnectionParams

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
SPANNER_INSTANCE = os.environ.get("SPANNER_INSTANCE", "zeroetl-ops")
SPANNER_DATABASE = os.environ.get("SPANNER_DATABASE", "ecommerce-operational")

SPANNER_MCP_URL = os.environ.get(
    "SPANNER_MCP_URL", "https://spanner.googleapis.com/mcp"
)

# execute_sql runs DML but does NOT commit it; commit finalises the transaction;
# execute_sql_readonly runs a single-use read. create_session is needed because a
# DML statement and its commit have to happen inside one session.
#
# Deliberately absent: create_instance, create_database, update_database_schema.
# This agent writes ROWS. It does not create infrastructure or reshape schemas,
# and the tools to do so are never advertised to it.
SPANNER_TOOLS = [
    "create_session",
    "execute_sql",
    "execute_sql_readonly",
    "commit",
]

_CREDENTIALS, _ = google.auth.default(
    scopes=["https://www.googleapis.com/auth/cloud-platform"]
)


def _headers(readonly_context=None) -> dict:
    """Return a freshly-refreshed ADC bearer token for each MCP request."""
    if not _CREDENTIALS.valid:
        _CREDENTIALS.refresh(google.auth.transport.requests.Request())
    return {
        "Authorization": f"Bearer {_CREDENTIALS.token}",
        "x-goog-user-project": PROJECT_ID,
    }


SPANNER_TOOLSET = McpToolset(
    connection_params=StreamableHTTPConnectionParams(url=SPANNER_MCP_URL, timeout=60.0),
    header_provider=_headers,
    tool_filter=SPANNER_TOOLS,
)

DATABASE_URI = (
    f"projects/{PROJECT_ID}/instances/{SPANNER_INSTANCE}/databases/{SPANNER_DATABASE}"
)

# ------------------------------------------------------------------------------
# TODO_C: Write the Spanner specialist's instruction (YOUR TURN)
# ------------------------------------------------------------------------------
# Replace SPANNER_INSTRUCTION below. It has to carry four things the other two
# TODOs did not, and each one is a place where a plausible-looking instruction
# quietly produces a broken agent.
#
# 1. THE CONNECTION. Every tool call needs the database:
#        {DATABASE_URI}
#    Put it in the instruction verbatim. A model that assembles this string
#    itself gets it subtly wrong and the error names a database that "does not
#    exist", which sends you looking at Terraform.
#
# 2. THE TWO-PHASE WRITE. This is the part people get wrong. On Spanner's MCP
#    server a DML statement is NOT durable when execute_sql returns:
#        create_session         -> a session to work in
#        execute_sql (the DML)  -> returns a precommit_token
#        commit                 -> pass that precommit_token back
#    Skip the commit and the agent will cheerfully report success while nothing
#    was written. Say this explicitly, and require the agent to confirm the
#    commit before it claims the row exists. Reads (execute_sql_readonly) need
#    none of this.
#
# 3. THE SCHEMA, and its one surprise. Tables are Customers, Products and
#    CustomerOrders. CustomerOrders is INTERLEAVED IN PARENT Customers, so its
#    primary key is (customer_id, order_item_id) — customer_id FIRST, even though
#    order_item_id is what identifies the row. An INSERT missing customer_id
#    fails, and the error talks about a key column rather than about interleaving.
#    All ids are STRING. Set written_by = 'agent-insert' on every row you write;
#    the seeded rows carry 'reverse-etl-seed' and the demonstration depends on
#    being able to tell them apart in BigQuery's unified view.
#    status must be one of the governed Order Status values from Lab 10's
#    glossary: Complete, Shipped, Processing, Cancelled, Returned (exact case).
#    Default to 'Complete' unless the user names one. Left to itself a model
#    writes 'Pending' or 'pending', which no governed query counts.
#    Let Spanner generate order_item_id / order_id unless the user gives them:
#        CONCAT('agent-', GENERATE_UUID())
#    and end the INSERT with THEN RETURN order_item_id, order_id so the agent
#    learns them for the read-back. Do NOT ask the model for a timestamp: it
#    does not know the current time and invents one, so two orders collide on
#    the primary key. An id built from customer and product (e.g.
#    'ord-10007-10088') collides on the second identical order too.
#
# 4. WHEN TO TRAVERSE INSTEAD OF SCANNING. The database has a property graph,
#    EcommerceOpsGraph, over the same three tables:
#        GRAPH EcommerceOpsGraph
#        MATCH (c:Customer)-[:PLACED]->(o:CustomerOrder)-[:HAS_ITEM]->(p:Product)
#        RETURN ...
#    Use GQL when the question is about a RELATIONSHIP — this customer's order
#    chain, which products connect two customers. Use ordinary SQL when it is
#    about rows — counts, totals, a lookup by id. Say which one you used and why.
#    Note the labels: PLACED and HAS_ITEM, and the node is CustomerOrder. ORDER,
#    INCLUDES and CONTAINS are reserved words in a LABEL clause and cannot be
#    used here.
#
# Also state: report errors verbatim, never invent ids, and say plainly that this
# agent cannot see BigQuery if asked for warehouse analytics.
#
#   agy prompt hint:
#     agy -i "In transact/spanner_specialist.py, replace the SPANNER_INSTRUCTION
#      placeholder. The agent reads and writes the Spanner database
#      {DATABASE_URI} through the Spanner MCP server. It must: pass that database
#      on every call; perform DML as create_session then execute_sql then commit
#      with the returned precommit_token, and never claim success before the
#      commit; know that CustomerOrders is interleaved in Customers with primary
#      key (customer_id, order_item_id) and that all ids are STRING; set
#      written_by='agent-insert' on every row it writes; set status to one of
#      Complete, Shipped, Processing, Cancelled, Returned (default Complete);
#      generate ids in the INSERT with CONCAT('agent-', GENERATE_UUID()) and
#      THEN RETURN order_item_id, order_id; and choose GQL over
#      EcommerceOpsGraph for relationship questions versus plain SQL for row
#      questions, stating which it used. Do not add tools."
SPANNER_INSTRUCTION = "TODO_C"

spanner_specialist = LlmAgent(
    name="spanner_specialist",
    model=os.environ.get("SPANNER_SPECIALIST_MODEL", "gemini-3.8-flash"),
    description="Reads, writes and traverses the Spanner operational database and its property graph.",
    instruction=SPANNER_INSTRUCTION,
    tools=[SPANNER_TOOLSET],
)
