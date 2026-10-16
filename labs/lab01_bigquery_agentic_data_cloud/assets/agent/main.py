# Author: Wissem Khlifi - August 2026
# ==============================================================================
# Lab 10 — HTTP surface for the ecommerce analyst agent
#
# agent.py holds the agent and the three TODOs the participant completes. This
# file wraps it in the one endpoint the lab runner needs and nothing else.
#
# It is deliberately separate: the participant edits agent.py, never this, so a
# broken TODO produces a clear import error here rather than a mangled server.
#
# The identity question this file does NOT answer: which credentials the agent
# uses. That is decided by `serviceAccountName` in service.yaml. This process
# simply runs as whatever Cloud Run gave it, which is the whole point -- the
# masking demonstration cannot be influenced by anything written here.
# ==============================================================================
import os
import logging
from typing import Any, Optional

from fastapi import FastAPI
from pydantic import BaseModel

from google.adk import Runner
from google.adk.sessions import InMemorySessionService
from google.genai.types import Content, Part

from agent import root_agent
from a2a_surface import mount_a2a

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ecommerce_analyst")

APP_NAME = "ecommerce_analyst"
app = FastAPI(title="Lab 10 Ecommerce Analyst Agent")
session_service = InMemorySessionService()


class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"


class ChatResponse(BaseModel):
    response: str
    # The lab runner shows these: Step 6 is about SEEING search_entries called
    # before any SQL. A claim in the answer text is not evidence; the tool trace
    # is.
    tools_called: list[str] = []
    sql: Optional[str] = None
    error: Optional[str] = None


@app.get("/health")
def health() -> dict[str, Any]:
    """Liveness plus the two facts the lab runner asserts in Step 6."""
    return {
        "status": "ok",
        "project": os.getenv("GOOGLE_CLOUD_PROJECT", "unset"),
        "agent": getattr(root_agent, "name", "unknown"),
        "model": getattr(root_agent, "model", "unknown"),
    }



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

def _extract(event: Any, tools_called: list[str], sql_seen: list[str]) -> None:
    """Pull tool names and any SQL out of one ADK event.

    Defensive on purpose: ADK's event shape varies by version and a missing
    attribute here would otherwise turn a working answer into a 500.
    """
    try:
        calls = _function_calls(event)
        for fc in calls:
            if getattr(fc, "name", None):
                tools_called.append(fc.name)
            args = getattr(fc, "args", None) or {}
            for key in ("query", "sql", "statement"):
                if isinstance(args.get(key), str):
                    sql_seen.append(args[key])
    except Exception:  # noqa: BLE001 - never let telemetry break the answer
        log.debug("could not read function calls from event", exc_info=True)


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    user_id = "lab-participant"
    tools_called: list[str] = []
    sql_seen: list[str] = []

    try:
        session = await session_service.get_session(
            app_name=APP_NAME, user_id=user_id, session_id=request.session_id
        )
        if not session:
            await session_service.create_session(
                app_name=APP_NAME, user_id=user_id, session_id=request.session_id
            )

        runner = Runner(
            agent=root_agent, app_name=APP_NAME, session_service=session_service
        )
        message = Content(role="user", parts=[Part(text=request.message)])

        answer = ""
        async for event in runner.run_async(
            user_id=user_id, session_id=request.session_id, new_message=message
        ):
            _extract(event, tools_called, sql_seen)
            content = getattr(event, "content", None)
            if content and getattr(content, "parts", None):
                for part in content.parts:
                    if getattr(part, "text", None):
                        answer = part.text

        return ChatResponse(
            response=answer or "(the agent returned no text)",
            tools_called=tools_called,
            sql=sql_seen[-1] if sql_seen else None,
        )

    except Exception as exc:  # noqa: BLE001
        # Return the error as data rather than a 500: the lab runner renders it
        # for the participant, and "TODO_2 is still None" is a far more useful
        # thing to read than a stack trace in Cloud Logging.
        log.exception("chat failed")
        return ChatResponse(
            response="",
            tools_called=tools_called,
            error=f"{type(exc).__name__}: {exc}",
        )


# ------------------------------------------------------------------------------
# Gemini Enterprise surface (Lab 12). Same root_agent, exposed over A2A at /a2a/
# so a business user can ask it questions in Gemini Enterprise. Private like the
# rest of this service: Cloud Run IAM decides who may call it. See a2a_surface.py.
# ------------------------------------------------------------------------------
def _a2a_extract(event: Any, agents: list[str], tools: list[str], sql: list[str]) -> None:
    """Feeds the grounding footer from the same telemetry /chat reports."""
    author = getattr(event, "author", None)
    if author and author not in agents:
        agents.append(author)
    _extract(event, tools, sql)


mount_a2a(
    app,
    root_agent,
    app_name=f"{APP_NAME}_a2a",
    card_name="Ecommerce Analyst (governed)",
    description=(
        "Answers business questions about the ecommerce data in BigQuery: revenue, "
        "orders, customers and high-value customers. Every metric is resolved against "
        "the governed Knowledge Catalog glossary before any SQL runs, and customer PII "
        "is masked by BigQuery column-level security."
    ),
    skills=[
        {
            "id": "governed_revenue",
            "name": "Governed metrics",
            "description": "Revenue, order and customer metrics defined by the business glossary.",
            "tags": ["bigquery", "knowledge-catalog", "governance"],
            "examples": ["What is our revenue?", "How many orders were cancelled?"],
        },
        {
            "id": "customer_insights",
            "name": "Customer insights",
            "description": "High-value customer analysis with PII masked by policy tags.",
            "tags": ["customers", "pii-masking"],
            "examples": ["Who are our high-value customers?"],
        },
    ],
    extract=_a2a_extract,
)
