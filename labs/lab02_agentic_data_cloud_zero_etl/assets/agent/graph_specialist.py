# Author: Wissem Khlifi - August 2026
"""Two graph engines, one specialist, a deliberate choice per question.

Ported from demo2-agentic-data-cloud `agents/graph_specialist.py`, repointed at
this lab's two graphs. The structure — three tools, a keyword classifier, and an
async both-engines comparison — is unchanged, because the lesson is the same:

  Spanner Graph               operational. One entity, current state,
    EcommerceOpsGraph         milliseconds, ACID. Sees SPANNER rows only.

  BigQuery Graph              analytical. Population patterns, full history,
    UnifiedEcommerceGraph     seconds. Sees ALL THREE engines, because it is
                              declared over the federated unified views.

That second line is what Lab 11 adds to Demo 2's version. An INSERT committed in
Spanner is visible to the BigQuery graph on the next query — no pipeline, no
refresh, no copy. The two engines answer differently on purpose, and saying WHY
is part of the answer.

Both client factories are isolated so tests can replace them without touching
auth or the network.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time

from google.cloud import bigquery, spanner_v1

logger = logging.getLogger(__name__)

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
SPANNER_INSTANCE = os.environ.get("SPANNER_INSTANCE", "zeroetl-ops")
SPANNER_DATABASE = os.environ.get("SPANNER_DATABASE", "ecommerce-operational")
ZEROETL_DATASET = os.environ.get("BQ_ZEROETL_DATASET_ID", "ecommerce_zeroetl")
UNIFIED_GRAPH = "UnifiedEcommerceGraph"

# ── The two queries ───────────────────────────────────────────────────────────
#
# Note the label names. ORDER, INCLUDES and CONTAINS are all reserved in a LABEL
# clause, which is why the Spanner graph says CustomerOrder and HAS_ITEM.

_SPANNER_GQL = """\
GRAPH EcommerceOpsGraph
MATCH (c:Customer)-[:PLACED]->(o:CustomerOrder)
WHERE o.status = 'Processing'
RETURN c.customer_id AS customer_id, c.segment AS segment,
       COUNT(o.order_item_id) AS open_orders
GROUP BY customer_id, segment
ORDER BY open_orders DESC
LIMIT 10"""

# Two layers, and the split matters: GRAPH_TABLE does pattern matching and
# returns a flat rowset; the outer SELECT aggregates it. Pushing the aggregation
# inside GRAPH_TABLE is the most common way this query fails to compile.
#
# "Open or reversed" = any item still Processing, or Returned/Cancelled. It is
# deliberately NOT called "at risk": in Lab 10 and Lab 11 Step 10, at risk means
# the customer's LATEST order item is Returned or Cancelled. Same word, same set.
_BIGQUERY_GQL = f"""\
SELECT
  origin,
  segment,
  COUNT(DISTINCT cid) AS customers,
  COUNT(*)            AS open_or_reversed
FROM GRAPH_TABLE(
  `{PROJECT_ID}.{ZEROETL_DATASET}.{UNIFIED_GRAPH}`
  MATCH (c:Customer)-[e:PURCHASED]->(p:Product)
  WHERE e.status IN ('Returned', 'Cancelled', 'Processing')
  RETURN c.segment AS segment, c.customer_id AS cid, e.origin AS origin
)
GROUP BY origin, segment
ORDER BY open_or_reversed DESC
LIMIT 20"""


# Next best product for ONE customer: Lab 10's lookalike-peer query, verbatim,
# on the unified graph. It must stay textually identical to the console's
# next_best_unified_gql() (lab-runner/actions.py) — tests/test_lab11_agent_graph.py
# enforces that, so the agent and the Step 10 card can never disagree.
# The two DISTINCTs are what keep it correct on a graph where every seeded
# purchase is an edge in all three engines (see Step 10's dedup trap).
_NEXT_BEST_GQL = """
        WITH hops AS (
          SELECT *
            FROM GRAPH_TABLE(`{graph}`
              MATCH (me:Customer)-[:PURCHASED]->(s:Product)<-[:PURCHASED]-(peer:Customer)
                    -[:PURCHASED]->(rec:Product)-[:IN_CATEGORY]->(cat:Category)
              WHERE me.customer_id = @cid
                AND peer.customer_id <> me.customer_id
                AND NOT EXISTS {{
                  MATCH (m2:Customer)-[:PURCHASED]->(r2:Product)
                  WHERE m2.customer_id = me.customer_id AND r2.product_id = rec.product_id }}
              RETURN peer.customer_id AS peer_id, s.product_id AS shared_id,
                     rec.product_id AS rec_id, rec.name AS rec_name,
                     cat.category_id AS rec_category)
        ),
        peer_strength AS (
          SELECT peer_id, COUNT(DISTINCT shared_id) AS shared
            FROM hops GROUP BY peer_id HAVING shared >= 2
        ),
        recs AS (SELECT DISTINCT rec_id, rec_name, rec_category, peer_id FROM hops)
        SELECT r.rec_id, r.rec_name, r.rec_category, SUM(p.shared) AS score, COUNT(*) AS peers
          FROM recs r JOIN peer_strength p USING (peer_id)
         GROUP BY 1, 2, 3
         ORDER BY score DESC, rec_id
         LIMIT 5
    """

# Same rule as graph_story.CUSTOMER_ID_PATTERN: digits only, so an id taken from
# a chat message can never reach the query as anything but a bound parameter.
_CUSTOMER_ID = re.compile(r"[0-9]{1,12}")


def _spanner_client() -> spanner_v1.Client:
    """Return a Spanner client using Application Default Credentials."""
    return spanner_v1.Client(project=PROJECT_ID)


def _bq_client() -> bigquery.Client:
    """Return a BigQuery client using Application Default Credentials."""
    return bigquery.Client(project=PROJECT_ID)


def query_spanner_graph() -> str:
    """Traverse the OPERATIONAL graph in Spanner for orders open right now.

    Runs an ISO GQL traversal over EcommerceOpsGraph to find the customers with
    the most orders currently in 'Processing'. This reads Spanner's own tables,
    so it sees only rows that live in Spanner — and it sees them the instant they
    commit.

    Use this tool when the question is about:
      - one customer, or a handful, rather than a population
      - what is true RIGHT NOW
      - "which orders are stuck today", "show me this customer's order chain"

    Returns:
        A formatted table of customer_id, segment and open_orders, or a
        description of the error if the Spanner query failed.
    """
    try:
        database = _spanner_client().instance(SPANNER_INSTANCE).database(SPANNER_DATABASE)
        with database.snapshot() as snapshot:
            rows = list(snapshot.execute_sql(_SPANNER_GQL))

        if not rows:
            return (
                "Spanner Graph returned no rows — no orders are in 'Processing' "
                "in the Spanner copy at this moment."
            )

        lines = ["customer_id | segment | open_orders", "-" * 44]
        for row in rows:
            lines.append(f"{str(row[0]):<20} | {str(row[1]):<12} | {row[2]}")
        return "\n".join(lines) + (
            f"\n\nEngine: Spanner Graph (EcommerceOpsGraph), operational, "
            f"Spanner rows only. {len(rows)} customer(s)."
        )
    except Exception as exc:  # noqa: BLE001 — the error text is the useful output
        logger.error("Spanner Graph query failed: %s", exc)
        return f"Spanner Graph query failed: {exc}"


def query_bigquery_graph() -> str:
    """Traverse the ANALYTICAL graph in BigQuery across all three engines.

    Runs a GRAPH_TABLE traversal over UnifiedEcommerceGraph, which is declared on
    top of the federated unified views — so it spans BigQuery, AlloyDB and
    Spanner in a single query and reports which engine each edge came from.

    Use this tool when the question is about:
      - patterns across the whole population, or history
      - population questions: lookalike customers, segment behaviour,
        open or reversed orders by segment
      - whether the three engines agree

    Returns:
        A formatted breakdown by origin and segment, or a description of the
        error if the BigQuery query failed.
    """
    try:
        job = _bq_client().query(_BIGQUERY_GQL, location="US")
        rows = list(job.result())

        if not rows:
            return "BigQuery Graph returned no rows — no open or reversed orders found."

        lines = ["origin | segment | customers | open_or_reversed", "-" * 56]
        for row in rows:
            lines.append(
                f"{str(row['origin']):<8} | {str(row['segment']):<12} | "
                f"{row['customers']:<9} | {row['open_or_reversed']}"
            )
        return "\n".join(lines) + (
            f"\n\nEngine: BigQuery Graph ({ZEROETL_DATASET}.{UNIFIED_GRAPH}), "
            "analytical, all three engines via federation."
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("BigQuery Graph query failed: %s", exc)
        return (
            f"BigQuery Graph query failed: {exc}\n\n"
            "GRAPH_TABLE requires an "
            "Enterprise reservation. If the message mentions a reservation, it is "
            "propagating — retry in a minute."
        )


def recommend_next_best_products(customer_id: str) -> str:
    """Recommend the next best products for ONE customer from lookalike peers.

    Traverses UnifiedEcommerceGraph (BigQuery + AlloyDB + Spanner through
    federation): customer -> products they bought <- lookalike peers (at least
    2 shared products) -> products those peers bought that this customer has
    not. Score = sum over recommending peers of how many products each shares
    with the customer. This is the same query as the Step 10 "Next best
    product" card, so the answers match exactly.

    Use this tool when the question names or implies a specific customer and
    asks what to recommend, offer, or show them next — e.g. "what should we
    recommend to customer 15481", "next best product for this at-risk
    customer", "win-back offer for 1131".

    Args:
        customer_id: The customer's numeric id as a string, e.g. "15481".
            Ask the user for it if the question does not contain one.

    Returns:
        Up to five ranked products (id, name, category, score, peers), or an
        explanation if the id is invalid, has no lookalike peers, or the query
        failed.
    """
    cid = str(customer_id or "").strip()
    if not _CUSTOMER_ID.fullmatch(cid):
        return (
            f"'{customer_id}' is not a valid customer id (digits only, e.g. 15481). "
            "Ask the user which customer they mean."
        )
    graph = f"{PROJECT_ID}.{ZEROETL_DATASET}.{UNIFIED_GRAPH}"
    try:
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("cid", "STRING", cid)]
        )
        job = _bq_client().query(_NEXT_BEST_GQL.format(graph=graph), job_config=job_config, location="US")
        rows = list(job.result())
        if not rows:
            return (
                f"No recommendation for customer {cid}: no lookalike peers share at least "
                "2 products with them, or the id does not exist. Nothing was invented."
            )
        lines = ["rank | product_id | name | category | score | peers", "-" * 64]
        for rank, row in enumerate(rows, start=1):
            lines.append(
                f"{rank} | {row['rec_id']} | {row['rec_name']} | {row['rec_category']} | "
                f"{row['score']} | {row['peers']}"
            )
        return "\n".join(lines) + (
            f"\n\nEngine: BigQuery Graph ({ZEROETL_DATASET}.{UNIFIED_GRAPH}), lookalike peers "
            "across all three engines. Score = sum of shared products over recommending peers."
        )
    except Exception as exc:  # noqa: BLE001 — the error text is the useful output
        logger.error("Next-best-product query failed: %s", exc)
        return (
            f"Next-best-product query failed: {exc}\n\n"
            "GRAPH_TABLE requires an Enterprise reservation. If the message mentions a "
            "reservation, it is propagating — retry in a minute."
        )


async def _timed(fn) -> tuple[str, float]:
    """Run a blocking tool function in a thread and return (result, seconds)."""
    started = time.perf_counter()
    result = await asyncio.to_thread(fn)
    return result, time.perf_counter() - started


async def query_both_graph_engines() -> str:
    """Run BOTH graph engines in parallel and return a compared answer.

    Executes the Spanner and BigQuery traversals concurrently with
    asyncio.gather, then reports each result with its measured wall-clock
    latency and a short synthesis. The latency difference is not decoration: it
    is the reason a platform carries two graph engines rather than one.

    Use when the question asks for both perspectives, or says "compare",
    "both engines", "side by side", or "the full picture".

    Returns:
        Both results, each labelled with its engine and latency, plus a synthesis.
    """
    (spanner_result, spanner_s), (bq_result, bq_s) = await asyncio.gather(
        _timed(query_spanner_graph), _timed(query_bigquery_graph)
    )
    return (
        f"=== SPANNER GRAPH — operational ({spanner_s:.2f}s) ===\n{spanner_result}\n\n"
        f"=== BIGQUERY GRAPH — analytical ({bq_s:.2f}s) ===\n{bq_result}\n\n"
        "=== SYNTHESIS ===\n"
        f"Spanner answered in {spanner_s:.2f}s over its own committed rows: current, "
        "transactional, one engine.\n"
        f"BigQuery answered in {bq_s:.2f}s over all three engines through federation: "
        "broader, historical, and it can see rows that were written to Spanner and "
        "AlloyDB without anything copying them here.\n"
        "Neither is 'better'. They answer different questions, and choosing between "
        "them is the architectural decision this lab exists to make visible."
    )


# ── Code-level engine classifier ──────────────────────────────────────────────
# Same pattern as parse_intent() one level up: keyword matching in code rather
# than a second round of model judgment. The orchestrator has already decided
# this is a graph question; this decides WHICH graph.

_SYNTHESIS_KEYWORDS = frozenset(
    {"compare", "both", "synthesis", "full picture", "both engines",
     "side by side", "side-by-side", "all engines", "which engine"}
)
_OPERATIONAL_KEYWORDS = frozenset(
    {"right now", "now", "stuck", "processing", "today", "current", "currently",
     "real-time", "realtime", "live", "this customer", "immediately"}
)
# Recommendation wording wins over "this customer": the answer needs the whole
# population of lookalike peers, which only the BigQuery graph can traverse.
_RECOMMEND_KEYWORDS = frozenset(
    {"recommend", "next best", "lookalike", "win-back", "win back", "what should we offer"}
)
_ANALYTICAL_KEYWORDS = frozenset(
    {"segment", "pattern", "across", "population", "cohort", "historical",
     "history", "trend", "recommend", "next best", "lookalike", "all customers",
     "every engine", "origin"}
)


def parse_graph_intent(query: str) -> str:
    """Classify a graph question as SYNTHESIS, OPERATIONAL or ANALYTICAL.

    Priority is SYNTHESIS, then recommendation wording (ANALYTICAL, because the
    peers come from the whole population), then OPERATIONAL, then ANALYTICAL. The default is
    OPERATIONAL because Spanner is the faster engine and an ambiguous question is
    better answered quickly and narrowly than slowly and broadly.

    Args:
        query: The participant's raw question.

    Returns:
        "SYNTHESIS", "OPERATIONAL" or "ANALYTICAL".
    """
    lowered = (query or "").lower()
    if any(keyword in lowered for keyword in _SYNTHESIS_KEYWORDS):
        return "SYNTHESIS"
    if any(keyword in lowered for keyword in _RECOMMEND_KEYWORDS):
        return "ANALYTICAL"
    if any(keyword in lowered for keyword in _OPERATIONAL_KEYWORDS):
        return "OPERATIONAL"
    if any(keyword in lowered for keyword in _ANALYTICAL_KEYWORDS):
        return "ANALYTICAL"
    return "OPERATIONAL"


from google.adk import Agent  # noqa: E402 — after the tools, for readability

graph_specialist = Agent(
    name="graph_specialist",
    model=os.environ.get("GRAPH_SPECIALIST_MODEL", "gemini-3.8-flash"),
    description="Traverses relationships using Spanner Graph (operational) or BigQuery Graph (analytical).",
    instruction=f"""You are the graph specialist for a platform that runs TWO graph
engines over the same ecommerce data, and your first job on every question is to
pick the right one.

  query_spanner_graph  →  operational, real-time, one entity or a few
      Engine:  Spanner EcommerceOpsGraph ({SPANNER_INSTANCE}/{SPANNER_DATABASE})
      Scope:   rows that live in Spanner, visible the instant they commit
      Latency: milliseconds
      Ask it:  "which orders are stuck right now", "this customer's order chain"

  query_bigquery_graph  →  analytical, population-wide, all three engines
      Engine:  BigQuery {ZEROETL_DATASET}.{UNIFIED_GRAPH}
      Scope:   BigQuery + AlloyDB + Spanner, through federation. Rows written to
               the operational engines appear here with no pipeline and no copy.
      Latency: seconds
      Ask it:  "which segments have the most open or reversed orders",
               "do the three engines agree"

  recommend_next_best_products(customer_id)  →  one customer, what to offer next
      Engine:  BigQuery {ZEROETL_DATASET}.{UNIFIED_GRAPH}, all three engines
      How:     lookalike peers (>= 2 shared products) -> what they bought that
               this customer has not; score = sum of shared products
      Ask it:  "what should we recommend to customer 15481", "next best product
               for this at-risk customer". Same query as the Step 10 card.
      Needs:   a numeric customer id. If the question has none, ask for it —
               never guess one.

  query_both_graph_engines  →  comparison or synthesis
      Runs both concurrently and reports each latency. Use it whenever the user
      says compare, both, side by side, or asks which engine to use.

Protocol:
1. Choose exactly ONE tool and call it. A recommendation for a specific
   customer is recommend_next_best_products, even though it names one entity:
   the peers come from the whole population, which Spanner alone cannot see.
2. Never call query_spanner_graph and query_bigquery_graph separately for the
   same question — that is what query_both_graph_engines is for.
3. Report the result, then state which engine answered and why that engine was
   the right one for this question. That sentence is not optional; a graph answer
   without its engine is unreadable.

Rules:
- Never invent counts, segments or customer ids. If a tool fails, report the
  failure and what it means.
- If the BigQuery graph reports a missing reservation, say that GRAPH_TABLE needs
  an Enterprise reservation and that it propagates within a minute or two.
- Both engines are read-only here. If asked to change data, say this agent
  traverses and does not write.
""",
    tools=[query_spanner_graph, query_bigquery_graph, recommend_next_best_products,
           query_both_graph_engines],
)
