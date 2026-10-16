# Author: Wissem Khlifi - August 2026
# ==============================================================================
# Lab 11 — HTTP surface for the dispatch agent
#
# Adapted from Lab 10 src/agent/main.py. The participant edits router.py and
# orchestrator.py, never this file, so an unfinished TODO produces a clear error
# here rather than a mangled server.
#
# One endpoint is new. /route runs ONLY the router and returns the intent it
# chose, with no specialist and no side effect. It exists because "the router
# classified this as TRANSACT_SPANNER" is a claim, and the lab is built on the
# idea that a claim in prose is not evidence. The lab runner calls it to show the
# routing decision next to the answer it produced.
# ==============================================================================
import logging
import os
from typing import Any, Optional

from fastapi import FastAPI
from pydantic import BaseModel

from google.adk import Runner
from google.adk.sessions import InMemorySessionService
from google.genai.types import Content, Part

from dispatch import parse_intent, _run_agent_async
from orchestrator import CLARIFY_ENGINE, _INTENT_TO_SPECIALIST, orchestrator
from router import router
from a2a_surface import mount_a2a

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("lab11_dispatch")

APP_NAME = "lab11_dispatch"
app = FastAPI(title="Lab 11 Dispatch Agent")
session_service = InMemorySessionService()


class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"


class ChatResponse(BaseModel):
    response: str
    # The lab runner renders these. Seeing WHICH agent answered is the whole
    # point of a multi-agent architecture; an answer alone proves nothing about
    # how it was reached.
    agents_used: list[str] = []
    tools_called: list[str] = []
    sql: Optional[str] = None
    error: Optional[str] = None


class RouteResponse(BaseModel):
    intent: str
    raw: str
    specialist: Optional[str] = None
    wired: bool = False
    error: Optional[str] = None


@app.get("/health")
def health() -> dict[str, Any]:
    """Liveness, plus the facts the lab runner asserts before it trusts a run."""
    return {
        "status": "ok",
        "project": os.getenv("GOOGLE_CLOUD_PROJECT", "unset"),
        "agent": getattr(orchestrator, "name", "unknown"),
        "transact_url": os.getenv("TRANSACT_URL", "unset"),
        "wired_intents": sorted(_INTENT_TO_SPECIALIST.keys()),
        "terminal_intents": [CLARIFY_ENGINE],
    }


@app.post("/route", response_model=RouteResponse)
async def route(request: ChatRequest) -> RouteResponse:
    """Classify a query and report the decision WITHOUT running a specialist."""
    try:
        raw = await _run_agent_async(router, request.message, "", f"{APP_NAME}_route")
    except Exception as exc:  # noqa: BLE001
        log.exception("router failed")
        return RouteResponse(intent="UNKNOWN", raw="", error=f"{type(exc).__name__}: {exc}")

    intent = parse_intent(raw)

    # CLARIFY_ENGINE is terminal: handled by the orchestrator, never by a
    # specialist. Reporting it as unwired would render in the lab runner with a
    # warning and a "that is TODO_B" caption, which is exactly backwards -- the
    # router did the right thing.
    if intent == CLARIFY_ENGINE:
        return RouteResponse(
            intent=intent,
            raw=raw.strip(),
            specialist="(terminal — halts and asks which engine)",
            wired=True,
        )

    specialist = _INTENT_TO_SPECIALIST.get(intent)
    return RouteResponse(
        intent=intent,
        raw=raw.strip(),
        specialist=getattr(specialist, "name", None),
        wired=specialist is not None,
    )



def _function_calls(event: Any) -> list[Any]:
    """Function calls carried by one ADK event.

    ADK events have no `tool_call` attribute; calls live in the event's content
    parts. An earlier version read `event.tool_call.function_calls`, which is
    always absent, so every answer reported "tools called: none".
    """
    getter = getattr(event, "get_function_calls", None)
    if callable(getter):
        return list(getter() or [])
    parts = getattr(getattr(event, "content", None), "parts", None) or []
    return [p.function_call for p in parts if getattr(p, "function_call", None)]

def _extract(event: Any, agents: list[str], tools: list[str], sql: list[str]) -> None:
    """Pull the author, any tool names and any SQL out of one ADK event.

    Defensive throughout: ADK's event shape varies by version, and a missing
    attribute here would turn a working answer into a 500.
    """
    try:
        author = getattr(event, "author", None)
        if author and author not in agents:
            agents.append(author)
    except Exception:  # noqa: BLE001
        log.debug("could not read event author", exc_info=True)

    try:
        calls = _function_calls(event)
        for call in calls:
            name = getattr(call, "name", None)
            if name:
                tools.append(name)
            args = getattr(call, "args", None) or {}
            for key in ("query", "sql", "statement", "sqlStatement"):
                if isinstance(args.get(key), str):
                    sql.append(args[key])
    except Exception:  # noqa: BLE001
        log.debug("could not read function calls from event", exc_info=True)

    # Tools the transact service ran in ITS process (remote_transact.py records
    # them); they never show up as function calls here.
    try:
        remote = (getattr(event, "custom_metadata", None) or {}).get("remote_transact") or {}
        for name in remote.get("tools") or []:
            if isinstance(name, str) and name:
                tools.append(f"{name} (transact service)")
        if isinstance(remote.get("sql"), str) and remote["sql"].strip():
            sql.append(remote["sql"])
    except Exception:  # noqa: BLE001
        log.debug("could not read remote telemetry from event", exc_info=True)


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    user_id = "lab-participant"
    agents: list[str] = []
    tools: list[str] = []
    sql: list[str] = []

    try:
        session = await session_service.get_session(
            app_name=APP_NAME, user_id=user_id, session_id=request.session_id
        )
        if not session:
            await session_service.create_session(
                app_name=APP_NAME, user_id=user_id, session_id=request.session_id
            )

        runner = Runner(
            agent=orchestrator, app_name=APP_NAME, session_service=session_service
        )
        message = Content(role="user", parts=[Part(text=request.message)])

        answer = ""
        async for event in runner.run_async(
            user_id=user_id, session_id=request.session_id, new_message=message
        ):
            _extract(event, agents, tools, sql)
            content = getattr(event, "content", None)
            if content and getattr(content, "parts", None):
                for part in content.parts:
                    if getattr(part, "text", None):
                        answer = part.text

        return ChatResponse(
            response=answer or "(the agent returned no text)",
            agents_used=agents,
            tools_called=tools,
            sql=sql[-1] if sql else None,
        )

    except Exception as exc:  # noqa: BLE001
        # Returned as data, not as a 500. "TODO_A is still a placeholder" is a
        # far more useful thing for a participant to read than a stack trace in
        # Cloud Logging.
        log.exception("chat failed")
        return ChatResponse(
            response="",
            agents_used=agents,
            tools_called=tools,
            error=f"{type(exc).__name__}: {exc}",
        )


# ------------------------------------------------------------------------------
# Gemini Enterprise surface (Lab 12). The same orchestrator, exposed over A2A at
# /a2a/. Only THIS service is published: lab11-transact stays reachable by the
# dispatch identity alone, so a chat UI never gains a direct write path, and a
# write with no engine named still halts at CLARIFY_ENGINE. See a2a_surface.py.
# ------------------------------------------------------------------------------
mount_a2a(
    app,
    orchestrator,
    app_name=f"{APP_NAME}_a2a",
    card_name="Zero-ETL Dispatch (BigQuery, AlloyDB, Spanner)",
    description=(
        "Routes operational and analytical questions to the right engine: BigQuery for "
        "analytics over federated AlloyDB and Spanner data, Spanner Graph for "
        "relationships, and governed glossary terms from the Knowledge Catalog. Writes "
        "go only through a separate transactional service, and a write that does not "
        "name its engine is answered with a clarifying question instead of a guess."
    ),
    skills=[
        {
            "id": "federated_analytics",
            "name": "Federated analytics",
            "description": "Analytics across BigQuery, AlloyDB and Spanner without ETL.",
            "tags": ["bigquery", "alloydb", "spanner", "zero-etl"],
            "examples": ["What is revenue by region this quarter?"],
        },
        {
            "id": "graph_relationships",
            "name": "Relationship questions",
            "description": "Graph traversals over customers, orders and products.",
            "tags": ["spanner-graph"],
            "examples": ["Which customers are connected to at-risk accounts?"],
        },
        {
            "id": "safe_writes",
            "name": "Engine-explicit writes",
            "description": "Writes routed to AlloyDB or Spanner only when the engine is named.",
            "tags": ["transactions", "clarify-engine"],
            "examples": ["Record a new order in Spanner for customer 42."],
        },
    ],
    extract=_extract,
)
