# Author: Wissem Khlifi - August 2026
"""Dispatch orchestrator — code-level router → specialist routing.

Ported from demo2-agentic-data-cloud `agents/orchestrator.py`. Do not redesign
this; three principles are expressed in its shape.

  Principle 1 — structural enforcement.
      The router has tools=[] and cannot call anything. The mapping below is a
      dict lookup in Python, not a judgment the model makes at runtime.

  Principle 2 — agent order is code, not prompt.
      _INTENT_TO_SPECIALIST is the routing table. When a query routes wrongly,
      you fix a dict entry and the fix is permanent. Prompt-based hand-off gives
      you a suggestion that holds until the next model revision.

  Principle 3 — isolation from accumulated memory.
      The router runs in a throwaway sub-runner (dispatch._run_agent_async), so
      its output — the bare word "TRANSACT_SPANNER" — never enters the session
      the specialist reads. The specialist sees the participant's question and
      nothing else.

Trace in `adk web`:
  lab11_dispatch_orchestrator
    → [bq_analyst | catalog | graph | alloydb_transact | spanner_transact]
    → specialist.call_llm / call_tool → final response
"""

from __future__ import annotations

import logging
from typing import AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events.event import Event
from google.genai import types

from bq_analyst import bq_analyst_specialist
from catalog_specialist import catalog_specialist
from dispatch import VALID_INTENTS, _run_agent_async, parse_intent
from graph_specialist import graph_specialist
from remote_transact import alloydb_transact_agent, spanner_transact_agent
from router import router

# The terminal intent. It has no specialist and must never be given one: there is
# nothing to run, because the correct outcome is a question back to the user.
CLARIFY_ENGINE = "CLARIFY_ENGINE"

CLARIFY_ENGINE_RESPONSE = (
    "Which engine should I write to — **AlloyDB** or **Spanner**?\n\n"
    "You asked for a write but did not name one, and I will not choose for you. "
    "Every other kind of question, answered from the wrong source, produces a "
    "wrong answer you can correct with the next question. A write sent to the "
    "wrong engine puts a real row in the wrong database: it shows up in "
    "`unified_orders` under the wrong `origin`, the totals still reconcile, and "
    "nothing anywhere reports a problem.\n\n"
    "Re-send the request naming the engine, for example:\n"
    "  \"… on AlloyDB\"  or  \"… on Spanner\""
)

logger = logging.getLogger(__name__)


def extract_query(ctx: InvocationContext) -> str:
    """Return the text of the most recent user message in this invocation."""
    if ctx.user_content:
        for part in ctx.user_content.parts or []:
            text = getattr(part, "text", None)
            if text:
                return text.strip()
    for event in reversed(ctx.session.events):
        if event.content and event.content.role == "user":
            for part in event.content.parts or []:
                text = getattr(part, "text", None)
                if text:
                    return text.strip()
    return ""


def _error_event(ctx: InvocationContext, author: str, message: str) -> Event:
    """Build a terminal model event attributed to `author`."""
    return Event(
        invocation_id=ctx.invocation_id,
        author=author,
        branch=ctx.branch,
        content=types.Content(role="model", parts=[types.Part(text=message)]),
    )


# ------------------------------------------------------------------------------
# TODO_B: Complete the dispatch table (YOUR TURN)
# ------------------------------------------------------------------------------
# Four of the six intents are wired. The two that write are not.
#
# Add entries mapping:
#     "TRANSACT_ALLOYDB"  -> alloydb_transact_agent
#     "TRANSACT_SPANNER"  -> spanner_transact_agent
#
# Both are already imported at the top of this file. Read remote_transact.py
# before you do it: those two agents are not like the others. Every specialist
# above runs IN THIS PROCESS, as lab11-dispatch-sa, which holds no write role on
# any engine. The two you are adding are HTTP calls to a second Cloud Run service
# running as a different service account — which is the only reason a write can
# happen at all.
#
# Then look at what happens when you get it wrong. An unmapped intent raises a
# KeyError, caught below and reported as "no specialist is wired for X". Compare
# that to a prompt-routed system, where a mis-route produces a confident answer
# from the wrong agent and nothing anywhere reports a problem.
#
#   agy prompt hint:
#     agy -i "In agent/orchestrator.py, add the two missing entries to
#      _INTENT_TO_SPECIALIST: TRANSACT_ALLOYDB maps to alloydb_transact_agent and
#      TRANSACT_SPANNER maps to spanner_transact_agent. Both are already
#      imported. Do not change any other entry and do not add routing logic to
#      the router's instruction."
_INTENT_TO_SPECIALIST: dict[str, BaseAgent] = {
    "ANALYTICS": bq_analyst_specialist,
    "CATALOG": catalog_specialist,
    # Both graph intents reach the same specialist. The router decided THAT the
    # question is a traversal; graph_specialist.parse_graph_intent decides WHICH
    # engine, by the same keyword-classifier-in-code pattern one level down.
    "GRAPH_ANALYTICAL": graph_specialist,
    "GRAPH_OPERATIONAL": graph_specialist,
    # TODO_B: two entries go here.
    #
    # CLARIFY_ENGINE is NOT one of them and must not be added. It is terminal and
    # is handled above the dict lookup; giving it a specialist would mean running
    # an agent to answer a question whose only correct answer is another question.
}


class DispatchOrchestrator(BaseAgent):
    """Classifies the query, then delegates to exactly one specialist.

    The classification runs in an isolated sub-session; the specialist runs in
    the MAIN session, so its tool calls and responses appear in the trace with
    proper attribution rather than being flattened into this agent's output.
    """

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        query = extract_query(ctx)
        if not query:
            yield _error_event(ctx, self.name, "No user query received.")
            return

        # Step 1 — classify, in a throwaway session (Principle 3).
        try:
            raw = await _run_agent_async(
                router, query, "", f"dispatch_classify_{ctx.invocation_id}"
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("router failed")
            yield _error_event(
                ctx,
                self.name,
                f"The router could not classify this query: {type(exc).__name__}: {exc}\n\n"
                "If this says the instruction is a placeholder, TODO_A in "
                "agent/router.py is not finished yet.",
            )
            return

        intent = parse_intent(raw)

        if intent not in VALID_INTENTS:
            yield _error_event(
                ctx,
                self.name,
                f"Could not classify {query!r} — the router returned {raw.strip()!r}.\n\n"
                "Either rephrase the question, or widen the classification rules "
                "in TODO_A. Note what did NOT happen: nothing guessed a "
                "specialist and nothing ran.",
            )
            return

        # Terminal intent, handled BEFORE the dict lookup. Reaching the lookup
        # with CLARIFY_ENGINE would report it as an unwired intent and blame
        # TODO_B for a classification that was correct.
        if intent == CLARIFY_ENGINE:
            logger.info("intent=%s → halting to ask which engine", intent)
            yield _error_event(ctx, self.name, CLARIFY_ENGINE_RESPONSE)
            return

        specialist = _INTENT_TO_SPECIALIST.get(intent)
        if specialist is None:
            # The intended failure for an incomplete TODO_B: loud, specific, and
            # impossible to mistake for a bad answer.
            yield _error_event(
                ctx,
                self.name,
                f"The router classified this as {intent}, but no specialist is "
                f"wired for it in _INTENT_TO_SPECIALIST.\n\n"
                "That is TODO_B in agent/orchestrator.py. Routing is a dict "
                "lookup in code, so an unwired intent stops here rather than "
                "being answered by whichever agent seemed closest.",
            )
            return

        logger.info("intent=%s → %s", intent, specialist.name)

        # Step 2 — run the specialist in the MAIN session.
        async for event in specialist.run_async(ctx):
            yield event


# The specialists are listed as sub_agents so ADK lifecycle management and the
# `adk web` agent tree work. The ROUTER is deliberately absent: it runs in an
# isolated sub-runner, so it must have no parent and no shared state.
orchestrator = DispatchOrchestrator(
    name="lab11_dispatch_orchestrator",
    description=(
        "Classifies a question and delegates to one specialist: analytics, "
        "catalog, graph, or — across an identity boundary — a transactional write."
    ),
    sub_agents=[
        bq_analyst_specialist,
        catalog_specialist,
        graph_specialist,
        alloydb_transact_agent,
        spanner_transact_agent,
    ],
)
