# Author: Wissem Khlifi - August 2026
"""Intent parsing and isolated agent execution.

Ported from demo2-agentic-data-cloud `tools/dispatch.py`. Two things live here
and nothing else does:

  parse_intent()      turns whatever the router actually said into one of the six
                      canonical intent strings, or UNKNOWN.
  _run_agent_async()  runs any ADK agent in a throwaway session and returns its
                      final text.

The mapping from intent to specialist is deliberately NOT here. It is in
orchestrator.py, next to the agents it names, because that dict is Principle 2
made concrete: agent order is code, not prompt.
"""

from __future__ import annotations

import logging
import re

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

logger = logging.getLogger(__name__)

# The seven categories this lab routes. Six of them name a specialist; adding
# another of that kind means adding the specialist AND an entry in
# orchestrator._INTENT_TO_SPECIALIST -- an unwired intent stopping at dispatch is
# the intended failure, not a silently mis-routed query.
#
# CLARIFY_ENGINE is the odd one out and deliberately so: it is a TERMINAL intent
# with no specialist. It means "a write was requested and the user named no
# engine", and the only correct response is to stop and ask. It is handled in
# orchestrator.py BEFORE the dict lookup, so it must never appear in
# _INTENT_TO_SPECIALIST.
VALID_INTENTS: frozenset[str] = frozenset(
    {
        "ANALYTICS",
        "CATALOG",
        "GRAPH_ANALYTICAL",
        "GRAPH_OPERATIONAL",
        "TRANSACT_ALLOYDB",
        "TRANSACT_SPANNER",
        "CLARIFY_ENGINE",
    }
)

# Spellings a model reaches for when it is nearly right. Mapping them is not
# leniency about correctness: the alternative is a hard failure on a query the
# router classified correctly and then typed badly.
_INTENT_ALIASES: dict[str, str] = {
    "ANALYTIC": "ANALYTICS",
    "REVENUE_ANALYTICS": "ANALYTICS",
    "GRAPH_ANALYTICS": "GRAPH_ANALYTICAL",
    "GRAPH_ANALYSIS": "GRAPH_ANALYTICAL",
    "GRAPH_OPERATIONS": "GRAPH_OPERATIONAL",
    "OPERATIONAL_LOOKUP": "GRAPH_OPERATIONAL",
    "ALLOYDB": "TRANSACT_ALLOYDB",
    "SPANNER": "TRANSACT_SPANNER",
    # A bare "TRANSACT" is the model saying "this is a write" without committing
    # to an engine, which is precisely what CLARIFY_ENGINE exists for. It used to
    # map to UNKNOWN, which produced a "could not classify" error and told the
    # participant to rephrase -- punishing a classification that was right.
    "TRANSACT": "CLARIFY_ENGINE",
    "TRANSACT_UNKNOWN": "CLARIFY_ENGINE",
    "CLARIFY": "CLARIFY_ENGINE",
    "KNOWLEDGE_CATALOG": "CATALOG",
}


def parse_intent(router_output: str) -> str:
    """Extract the intent category from the router's raw response.

    The router is instructed to return ONLY a category name. This is tolerant of
    the four ways a model deviates from that in practice, in order:

      1. Exact match after upper-casing.
      2. First whitespace token with trailing punctuation stripped.
      3. Prefix match, but only when the suffix starts with a LOWERCASE letter --
         that is the signature of a model appending a word without a space
         ("ANALYTICSbecause"). It deliberately does NOT match "ANALYTICS_EXTRA",
         where the underscore says a different identifier was intended.
      4. The alias table above.

    Args:
        router_output: Raw text returned by the router LlmAgent.

    Returns:
        One of VALID_INTENTS, or "UNKNOWN".
    """
    stripped = (router_output or "").strip()
    if not stripped:
        logger.warning("Router returned empty output — UNKNOWN")
        return "UNKNOWN"

    if stripped.upper() in VALID_INTENTS:
        return stripped.upper()

    raw_first = stripped.split()[0]
    first_upper = re.sub(r"[^A-Z_]+$", "", raw_first.upper())
    if first_upper in VALID_INTENTS:
        return first_upper

    for intent in VALID_INTENTS:
        if raw_first.upper().startswith(intent):
            suffix = raw_first[len(intent):]
            if not suffix or suffix[0].islower():
                return intent

    if first_upper in _INTENT_ALIASES:
        resolved = _INTENT_ALIASES[first_upper]
        logger.info("Router said %r — resolved to %s via alias", first_upper, resolved)
        return resolved

    logger.warning("Router returned unrecognised intent %r — UNKNOWN", first_upper)
    return "UNKNOWN"


async def _run_agent_async(agent, query: str, session_id: str, app_name: str) -> str:
    """Run an ADK agent in a THROWAWAY session and return its final text.

    The throwaway session is the point, not an implementation detail. This is
    Principle 3: the router runs here, in a session of its own, so the word
    "TRANSACT_ALLOYDB" never appears in the conversation the specialist sees.
    The specialist is handed the participant's original question and nothing
    else, which is why it cannot be led by the classifier's phrasing.

    Args:
        agent: Any ADK BaseAgent.
        query: The text to send as a user-role message.
        session_id: Session id to create; empty means let ADK generate one.
        app_name: ADK app name for the Runner.

    Returns:
        The last non-empty text part from the agent's event stream.

    Raises:
        RuntimeError: If the agent produced no text at all.
    """
    session_service = InMemorySessionService()
    if session_id:
        session = await session_service.create_session(
            app_name=app_name, user_id="dispatch", session_id=session_id
        )
    else:
        session = await session_service.create_session(
            app_name=app_name, user_id="dispatch"
        )

    runner = Runner(app_name=app_name, agent=agent, session_service=session_service)
    message = types.Content(role="user", parts=[types.Part(text=query)])

    collected: list[str] = []
    async for event in runner.run_async(
        user_id="dispatch", session_id=session.id, new_message=message
    ):
        if event.content:
            for part in event.content.parts or []:
                text = getattr(part, "text", None)
                if text and text.strip():
                    collected.append(text.strip())

    if not collected:
        raise RuntimeError(f"Agent {agent.name!r} returned no text for: {query!r}")
    return collected[-1]
