# Author: Wissem Khlifi - August 2026
"""The router: an intent classifier with no tools and no sub-agents.

Ported from demo2-agentic-data-cloud `agents/router.py`.

Two design principles are enforced by the SHAPE of this file, not by its text:

  Principle 1 — structural enforcement.
      tools=[] means the router physically cannot call anything. You could write
      "do not query BigQuery" into the instruction and a determined model would
      still try. An empty tool list is not advice; there is nothing on the menu.

  Principle 3 — isolation from accumulated memory.
      sub_agents is omitted, so this agent has no parent and no shared session.
      orchestrator.py runs it in a throwaway sub-runner, which means its answer
      ("TRANSACT_SPANNER") never lands in the conversation the specialist reads.

You complete TODO_A below.
"""

import os

from google.adk.agents import LlmAgent

# ------------------------------------------------------------------------------
# TODO_A: Write the router's classification instruction (YOUR TURN)
# ------------------------------------------------------------------------------
# Replace ROUTER_INSTRUCTION with the prompt that classifies a participant's
# question into EXACTLY ONE of these seven strings, and returns nothing else:
#
#   ANALYTICS          revenue, totals, averages, counts, "how much", "how many",
#                      anything answered by aggregating the warehouse
#   CATALOG            what a business term MEANS; glossary lookups; "how is
#                      revenue defined"; "what counts as a high value customer"
#   GRAPH_ANALYTICAL   patterns ACROSS the population -- recommend, next best,
#                      lookalike customers,
#                      "which segments", "customers like this one", blast radius.
#                      Spans all three engines via the unified graph.
#   GRAPH_OPERATIONAL  the live operational state, right now -- one entity's
#                      chain ("this customer's order chain") OR what is stuck
#                      or open at the moment ("which customers have orders
#                      stuck in Processing right now"). Spanner only, milliseconds.
#   TRANSACT_ALLOYDB   create/insert/update, where the user NAMES AlloyDB
#   TRANSACT_SPANNER   create/insert/update, where the user NAMES Spanner
#   CLARIFY_ENGINE     a write is requested but NO engine is named. Halt and ask.
#
# What makes this hard, and what the instruction has to settle:
#
#   * ANALYTICS vs GRAPH_ANALYTICAL. "Which products sell most" is an aggregate.
#     "Which products are bought TOGETHER" is a traversal. The distinction is
#     whether the answer needs a relationship between two rows.
#   * GRAPH_ANALYTICAL vs GRAPH_OPERATIONAL. Population versus entity; historical
#     versus current. When the question names one customer and says "now", it is
#     operational.
#   * ANALYTICS vs GRAPH_OPERATIONAL. Both can say "which customers". The test is
#     TIME: a read about what is true RIGHT NOW -- stuck, open, currently, at the
#     moment -- is GRAPH_OPERATIONAL even when it names no customer, because only
#     the operational store sees a row the instant it commits. ANALYTICS
#     aggregates history: revenue, totals, trends, "how much".
#   * The two TRANSACT intents, and the third outcome. A write goes to
#     TRANSACT_ALLOYDB or TRANSACT_SPANNER ONLY when the user names that engine.
#
#     If a write is requested and NO engine is named -- "create an order for
#     customer C042, product P015, $299" -- the answer is CLARIFY_ENGINE. Not a
#     guess, and not a refusal: a halt, followed by asking the user which engine
#     to write to.
#
#     This is the one place in the lab where guessing is worse than stopping.
#     Every other intent, guessed wrong, produces a wrong ANSWER that the next
#     question can correct. A write guessed wrong puts a real row in the wrong
#     database, and nothing downstream tells you it happened -- unified_orders
#     shows it under the wrong `origin` and the totals still reconcile.
#
#     Do not infer the engine from anything except the user's own words. Not
#     from which engine was used last, not from what the ids look like, not from
#     a default. If it is not named, it is CLARIFY_ENGINE.
#
# Rules the instruction must state explicitly:
#   1. Return ONLY the category name. No explanation, no punctuation, no
#      markdown, no tool calls.
#   2. If a query spans categories, pick the dominant intent.
#   3. Never refuse. CLARIFY_ENGINE is not a refusal -- it is a classification,
#      and it is the correct one when a write names no engine.
#   4. CLARIFY_ENGINE applies to WRITES only. A read that names no engine is
#      fine: ANALYTICS and the graph intents choose their own source.
#
#   agy prompt hint:
#     agy -i "In agent/router.py, replace the ROUTER_INSTRUCTION placeholder with
#      the classification instruction for a seven-way intent router. The seven
#      categories are ANALYTICS, CATALOG, GRAPH_ANALYTICAL, GRAPH_OPERATIONAL,
#      TRANSACT_ALLOYDB, TRANSACT_SPANNER and CLARIFY_ENGINE. It must return only
#      the category name, must distinguish aggregation from traversal,
#      population from single entity, and history from right now (a read about
#      what is stuck or open right now is GRAPH_OPERATIONAL, even with no customer
#      named), and must return CLARIFY_ENGINE whenever a
#      write is requested without the user naming AlloyDB or Spanner -- never
#      guessing an engine and never inferring one from context or defaults.
#      CLARIFY_ENGINE applies to writes only. Do not add tools and do not add
#      sub_agents."
ROUTER_INSTRUCTION = "TODO_A"

router = LlmAgent(
    name="lab11_router",
    model=os.environ.get("ROUTER_MODEL", "gemini-3.8-flash"),
    description="Narrow intent classifier. Returns one category name and nothing else.",
    instruction=ROUTER_INSTRUCTION,
    # Principle 1. Do not add anything to this list -- not even a "read-only"
    # tool. The moment the router can call something, routing becomes a
    # negotiation between the model and the prompt.
    tools=[],
    # sub_agents intentionally omitted: dispatch is code-level, in
    # orchestrator._INTENT_TO_SPECIALIST.
)
