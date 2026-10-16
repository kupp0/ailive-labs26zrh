# Author: Wissem Khlifi - August 2026
# ==============================================================================
# Lab 11 — HTTP surface for the transactional agent
#
# This service exists for one reason: a process has exactly one service account,
# and the agents that WRITE must not share an identity with the agents that read
# the warehouse. Everything here runs as lab11-transact-sa.
#
# Note what this file does NOT do. It does not classify. The dispatch service
# already decided which engine, in code, and passes that decision in the request
# body. Re-deciding it here would put the routing back into model judgment at the
# exact moment it matters most — when the next statement is a write.
# ==============================================================================
import logging
import os
from typing import Any, Optional

from fastapi import FastAPI
from pydantic import BaseModel

from google.adk import Runner
from google.adk.agents.run_config import RunConfig
from google.adk.sessions import InMemorySessionService
from google.genai.types import Content, Part

from alloydb_specialist import alloydb_specialist
from spanner_specialist import spanner_specialist

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("lab11_transact")

APP_NAME = "lab11_transact"
app = FastAPI(title="Lab 11 Transactional Agent")
session_service = InMemorySessionService()

# A correct write is about six model calls (check, session, DML, commit,
# read-back, answer). A loose instruction can loop for 50+, which outlives the
# dispatch agent's HTTP timeout (remote_transact.TRANSACT_TIMEOUT, 180 s) and
# reports "could not be reached" for a write that may already have committed.
# Capping here turns that into a fast, explicit error instead.
MAX_LLM_CALLS = int(os.environ.get("TRANSACT_MAX_LLM_CALLS", "15"))
_RUN_CONFIG = RunConfig(max_llm_calls=MAX_LLM_CALLS)

_LIMIT_MESSAGE = (
    "The {engine} specialist hit its limit of {limit} model calls and was stopped. "
    "This usually means its instruction lets it wander (schema exploration, "
    "id lookups, retries). A write MAY already have committed before it stopped: "
    "check with Step 8 'Verify' before retrying, or you will create a duplicate."
)

# Code-level, exactly like _INTENT_TO_SPECIALIST one service up. An unknown
# engine is an error, never a default.
_ENGINE_TO_SPECIALIST = {
    "alloydb": alloydb_specialist,
    "spanner": spanner_specialist,
}


class TransactRequest(BaseModel):
    message: str
    engine: str
    session_id: str = "default"


class TransactResponse(BaseModel):
    response: str
    engine: str
    tools_called: list[str] = []
    sql: Optional[str] = None
    error: Optional[str] = None


@app.get("/health")
def health() -> dict[str, Any]:
    """Liveness, plus which engines this service can actually reach."""
    return {
        "status": "ok",
        "project": os.getenv("GOOGLE_CLOUD_PROJECT", "unset"),
        "engines": sorted(_ENGINE_TO_SPECIALIST.keys()),
        "alloydb_instance": os.getenv("ALLOYDB_INSTANCE", "unset"),
        "spanner_database": os.getenv("SPANNER_DATABASE", "unset"),
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

def _extract(event: Any, tools: list[str], sql: list[str]) -> None:
    """Pull tool names and any SQL out of one ADK event, defensively."""
    try:
        calls = _function_calls(event)
        for call in calls:
            name = getattr(call, "name", None)
            if name:
                tools.append(name)
            args = getattr(call, "args", None) or {}
            for key in ("sqlStatement", "sql", "query", "statement"):
                if isinstance(args.get(key), str):
                    sql.append(args[key])
    except Exception:  # noqa: BLE001 — telemetry must never break the answer
        log.debug("could not read function calls from event", exc_info=True)


@app.post("/transact", response_model=TransactResponse)
async def transact(request: TransactRequest) -> TransactResponse:
    engine = (request.engine or "").strip().lower()
    specialist = _ENGINE_TO_SPECIALIST.get(engine)

    if specialist is None:
        return TransactResponse(
            response="",
            engine=engine,
            error=(
                f"Unknown engine {engine!r}. This service writes to "
                f"{sorted(_ENGINE_TO_SPECIALIST)} and refuses to guess: routing a "
                "write to the wrong engine puts real rows in the wrong database."
            ),
        )

    user_id = "dispatch-agent"
    tools: list[str] = []
    sql: list[str] = []
    # One session per engine so an AlloyDB conversation cannot leak table names
    # or ids into a Spanner one.
    session_id = f"{request.session_id}-{engine}"

    try:
        session = await session_service.get_session(
            app_name=APP_NAME, user_id=user_id, session_id=session_id
        )
        if not session:
            await session_service.create_session(
                app_name=APP_NAME, user_id=user_id, session_id=session_id
            )

        runner = Runner(
            agent=specialist, app_name=APP_NAME, session_service=session_service
        )
        message = Content(role="user", parts=[Part(text=request.message)])

        answer = ""
        async for event in runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=message,
            run_config=_RUN_CONFIG,
        ):
            _extract(event, tools, sql)
            content = getattr(event, "content", None)
            if content and getattr(content, "parts", None):
                for part in content.parts:
                    if getattr(part, "text", None):
                        answer = part.text

        return TransactResponse(
            response=answer or "(the agent returned no text)",
            engine=engine,
            tools_called=tools,
            sql=sql[-1] if sql else None,
        )

    except Exception as exc:  # noqa: BLE001
        # Matched by name: ADK raises LlmCallsLimitExceededError, and its
        # module path has moved between releases.
        if type(exc).__name__ == "LlmCallsLimitExceededError":
            log.warning("transact stopped at %d model calls (engine=%s)", MAX_LLM_CALLS, engine)
            error = _LIMIT_MESSAGE.format(engine=engine, limit=MAX_LLM_CALLS)
        else:
            log.exception("transact failed")
            error = f"{type(exc).__name__}: {exc}"
        return TransactResponse(
            response="",
            engine=engine,
            tools_called=tools,
            sql=sql[-1] if sql else None,
            error=error,
        )
