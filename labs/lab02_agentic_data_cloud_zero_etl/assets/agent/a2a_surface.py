# ==============================================================================
# A2A surface for Gemini Enterprise (shared by the Lab 10 and Lab 11 agents)
#
# Gemini Enterprise (GE) calls a "custom agent via A2A" over JSON-RPC, protocol
# v0.3. This module adds that surface to the agent's existing FastAPI app:
#
#   POST /a2a/                              A2A JSON-RPC (v1.x + v0.3 compat)
#   GET  /a2a/.well-known/agent-card.json   v0.3 agent card, `url` = this host
#
# What it does NOT change: /chat, /health and the participant's files
# (agent.py, router.py, orchestrator.py). It wraps the same agent object the
# /chat endpoint runs, so GE and the lab console exercise identical logic.
#
# Two choices worth knowing:
#
# * Grounding footer. GE renders only the answer text; the tool trace the lab
#   console shows is invisible there. Since these labs are built on "a claim in
#   prose is not evidence", the last answer of each turn gets a short footer
#   naming the agents and tools that actually ran and the SQL that was sent.
#   Telemetry is best effort: any failure here leaves the answer untouched.
#
# * The service stays private. Cloud Run IAM decides who may call /a2a/; the
#   Lab 12 publisher grants roles/run.invoker on THIS service only to the
#   Discovery Engine service agent. Nothing in this file authenticates.
#
# Gated by A2A_ENABLED (default "true"; set "false" in service.yaml to remove
# the routes). A missing a2a-sdk logs a warning and leaves /chat working.
# ==============================================================================
from __future__ import annotations

import logging
import os
from typing import Any, AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

log = logging.getLogger("a2a_surface")

A2A_PATH = "/a2a"
CARD_PATH = f"{A2A_PATH}/.well-known/agent-card.json"
A2A_PROTOCOL_VERSION = "0.3.0"
_SQL_FOOTER_LIMIT = 600


def a2a_enabled() -> bool:
    """Returns True unless A2A_ENABLED is set to a false-like value."""
    return os.getenv("A2A_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}


def public_base_url(request: Request) -> str:
    """Returns the scheme://host the caller used to reach this service.

    Cloud Run terminates TLS and forwards plain HTTP, so the scheme comes from
    X-Forwarded-Proto. The card's `url` must be the address GE will call.

    Args:
        request: The incoming request.

    Returns:
        Base URL without a trailing slash.
    """
    proto = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


def agent_card_v03(base_url: str, *, name: str, description: str, skills: list[dict[str, Any]]) -> dict[str, Any]:
    """Builds an A2A v0.3 agent card, the shape GE's "Custom agent via A2A" accepts.

    Args:
        base_url: Public base URL of this service.
        name: Agent name shown in GE.
        description: What the agent does; GE's model reads it to route questions.
        skills: Skill dicts with id, name, description, tags and examples.

    Returns:
        The agent card as a JSON-serialisable dict.
    """
    return {
        "protocolVersion": A2A_PROTOCOL_VERSION,
        "name": name,
        "description": description,
        "url": f"{base_url}{A2A_PATH}/",
        "preferredTransport": "JSONRPC",
        "version": "1.0.0",
        "capabilities": {"streaming": True},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": skills,
    }


def _grounding_footer(agents: list[str], tools: list[str], sql: list[str]) -> str:
    """Formats the evidence line appended to the final answer."""
    lines = ["", "---", "**Grounding** (what actually ran, not what the answer claims)"]
    if agents:
        lines.append(f"- Agents: {' → '.join(agents)}")
    lines.append(f"- Tools: {', '.join(dict.fromkeys(tools)) if tools else 'none'}")
    if sql:
        statement = sql[-1].strip()
        if len(statement) > _SQL_FOOTER_LIMIT:
            statement = statement[:_SQL_FOOTER_LIMIT] + " …"
        lines.append(f"- SQL:\n```sql\n{statement}\n```")
    return "\n".join(lines)


def _make_grounded_agent(inner: Any, extract: Any) -> Any:
    """Wraps `inner` so the last text event of a turn carries a grounding footer.

    The wrapper is a plain pass-through: every event of `inner` is yielded
    unchanged except that the most recent final text event is held back one
    step, so the footer can be added to the one that turns out to be last.

    Args:
        inner: The participant's root agent (not modified, not re-parented).
        extract: Callable(event, agents, tools, sql) collecting telemetry.

    Returns:
        A BaseAgent instance wrapping `inner`.
    """
    from google.adk.agents import BaseAgent  # imported lazily: optional surface

    class GroundedAgent(BaseAgent):
        """Pass-through agent that appends a grounding footer to the final answer."""

        async def _run_async_impl(self, ctx: Any) -> AsyncGenerator[Any, None]:
            agents: list[str] = []
            tools: list[str] = []
            sql: list[str] = []
            held = None
            async for event in inner.run_async(ctx):
                try:
                    extract(event, agents, tools, sql)
                except Exception:  # noqa: BLE001 - telemetry must never break an answer
                    log.debug("grounding extract failed", exc_info=True)
                has_text = bool(
                    getattr(event, "content", None)
                    and any(getattr(p, "text", None) for p in (event.content.parts or []))
                )
                if has_text and not getattr(event, "partial", False):
                    if held is not None:
                        yield held
                    held = event
                    continue
                yield event
            if held is not None:
                try:
                    footer = _grounding_footer(agents, tools, sql)
                    for part in reversed(held.content.parts):
                        if getattr(part, "text", None):
                            part.text = f"{part.text}\n{footer}"
                            break
                except Exception:  # noqa: BLE001
                    log.debug("could not append grounding footer", exc_info=True)
                yield held

    return GroundedAgent(name=f"{getattr(inner, 'name', 'agent')}_ge", description="A2A facade")


def mount_a2a(
    app: FastAPI,
    agent: Any,
    *,
    app_name: str,
    card_name: str,
    description: str,
    skills: list[dict[str, Any]],
    extract: Any,
) -> bool:
    """Adds the A2A JSON-RPC route and the v0.3 agent card route to `app`.

    Args:
        app: The agent's FastAPI app.
        agent: The ADK agent that /chat runs.
        app_name: ADK app name for the A2A runner's sessions.
        card_name: Agent name advertised in the card.
        description: Agent description advertised in the card.
        skills: Skills advertised in the card.
        extract: Callable(event, agents, tools, sql) for the grounding footer.

    Returns:
        True if the routes were mounted, False if disabled or unavailable.
    """
    if not a2a_enabled():
        log.info("A2A surface disabled (A2A_ENABLED=false)")
        return False
    try:
        from a2a.server.request_handlers import DefaultRequestHandler
        from a2a.server.routes import create_jsonrpc_routes
        from a2a.server.tasks import InMemoryTaskStore
        from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
        from google.adk import Runner
        from google.adk.a2a.executor.a2a_agent_executor import A2aAgentExecutor
        from google.adk.artifacts import InMemoryArtifactService
        from google.adk.memory import InMemoryMemoryService
        from google.adk.sessions import InMemorySessionService
    except ImportError as exc:
        log.warning("A2A surface unavailable (%s); /chat still works", exc)
        return False

    runner = Runner(
        app_name=app_name,
        agent=_make_grounded_agent(agent, extract),
        session_service=InMemorySessionService(),
        artifact_service=InMemoryArtifactService(),
        memory_service=InMemoryMemoryService(),
    )
    # The handler needs a card object; the public card (with this host's URL)
    # is served by the dynamic route below, because Cloud Run assigns the URL
    # only after the first deploy.
    internal_card = AgentCard(
        name=card_name,
        description=description,
        version="1.0.0",
        supported_interfaces=[
            AgentInterface(url=f"http://localhost{A2A_PATH}/", protocol_binding="JSONRPC", protocol_version="0.3")
        ],
        capabilities=AgentCapabilities(streaming=True),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=[
            AgentSkill(
                id=s["id"], name=s["name"], description=s["description"],
                tags=s.get("tags", []), examples=s.get("examples", []),
            )
            for s in skills
        ],
    )
    handler = DefaultRequestHandler(
        agent_executor=A2aAgentExecutor(runner=runner),
        task_store=InMemoryTaskStore(),
        agent_card=internal_card,
    )
    app.router.routes.extend(create_jsonrpc_routes(handler, f"{A2A_PATH}/", enable_v0_3_compat=True))

    @app.get(CARD_PATH, include_in_schema=False)
    def agent_card(request: Request) -> JSONResponse:
        """Serves the v0.3 agent card with this service's public URL."""
        return JSONResponse(
            agent_card_v03(public_base_url(request), name=card_name, description=description, skills=skills)
        )

    log.info("A2A surface mounted at %s/ (card %s)", A2A_PATH, CARD_PATH)
    return True
