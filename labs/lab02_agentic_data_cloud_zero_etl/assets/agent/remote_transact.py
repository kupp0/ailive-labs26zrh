# Author: Wissem Khlifi - August 2026
"""The one hop that crosses an identity boundary.

Every other specialist in this service runs IN PROCESS: the orchestrator calls
`specialist.run_async(ctx)` and the specialist inherits this container's service
account, lab11-dispatch-sa, which holds no write role anywhere.

That is exactly why the transactional specialists cannot live here. ADK runs
sub-agents in-process, and a process has one service account. Two capabilities
that must differ therefore need two services — so the write path is a separate
Cloud Run deployment running as lab11-transact-sa, and this agent is the HTTP
call that reaches it.

The boundary is enforced three ways, none of them in a prompt:
  * lab11-dispatch-sa holds no alloydb.* or spanner.databaseUser role, so even if
    this file were rewritten to call the databases directly, it would be refused.
  * lab11-transact holds no BigQuery data role, so a compromised write agent
    cannot read the warehouse.
  * Only lab11-dispatch-sa holds run.invoker on lab11-transact, so nothing else
    in the project — including the participant's own curl — can reach the write
    path at all.
"""

from __future__ import annotations

import logging
import os
from typing import AsyncGenerator

import google.auth.transport.requests
import requests
from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events.event import Event
from google.genai import types
from google.oauth2 import id_token as google_id_token

logger = logging.getLogger(__name__)

TRANSACT_URL = os.environ.get("TRANSACT_URL", "").rstrip("/")
TRANSACT_TIMEOUT = int(os.environ.get("TRANSACT_TIMEOUT", "180"))


def _id_token(audience: str) -> str:
    """Mint a Cloud Run identity token for `audience`.

    Cloud Run service-to-service auth needs an ID token whose audience is the
    RECEIVING service's URL. An access token — the thing every other Google API
    call in this lab uses — is rejected with a bare 401 and no explanation.
    """
    return google_id_token.fetch_id_token(
        google.auth.transport.requests.Request(), audience
    )


class RemoteTransactAgent(BaseAgent):
    """Forwards a write request to the transactional service over HTTP.

    The routed intent is passed alongside the query so the remote service knows
    which engine was chosen without having to re-classify — the decision was
    already made in code, and re-deciding it downstream is exactly the drift this
    architecture exists to prevent.
    """

    engine: str = "alloydb"

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        from orchestrator import extract_query  # local import avoids a cycle

        query = extract_query(ctx)

        if not TRANSACT_URL:
            yield self._event(
                ctx,
                "The transactional service is not wired up: TRANSACT_URL is unset "
                "on this service. Deploy it with `bash deploy.sh` — deploy.sh "
                "deploys transact first precisely so its URL exists for this one.",
            )
            return

        try:
            token = _id_token(TRANSACT_URL)
            response = requests.post(
                f"{TRANSACT_URL}/transact",
                json={
                    "message": query,
                    "engine": self.engine,
                    "session_id": getattr(ctx.session, "id", "") or "dispatch",
                },
                headers={"Authorization": f"Bearer {token}"},
                timeout=TRANSACT_TIMEOUT,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.exceptions.ReadTimeout:
            # The request reached the service; only the answer is missing. The
            # specialist keeps running server-side, so its write can still
            # commit. Saying "could not be reached" here invites a retry, and the
            # retry is a duplicate row.
            logger.warning("transact call timed out after %ss", TRANSACT_TIMEOUT)
            yield self._event(
                ctx,
                f"The transactional agent did not answer within {TRANSACT_TIMEOUT}s. "
                "The write MAY still have committed — the request was delivered and "
                "the agent keeps running after this timeout. Check with Step 8 "
                "'Verify' (or query the engine) BEFORE retrying, or you may create "
                "a duplicate row.",
            )
            return
        except Exception as exc:  # noqa: BLE001 — reported as data, not a 500
            logger.exception("transact call failed")
            yield self._event(
                ctx, f"The transactional agent could not be reached: {type(exc).__name__}: {exc}"
            )
            return

        if payload.get("error"):
            yield self._event(ctx, f"Transactional agent error: {payload['error']}")
            return

        tools = payload.get("tools_called") or []
        trace = f"\n\n(engine: {self.engine}; tools called: {', '.join(tools) or 'none'})"
        event = self._event(ctx, (payload.get("response") or "(no response)") + trace)
        attach_remote_telemetry(event, tools, payload.get("sql"))
        yield event

    def _event(self, ctx: InvocationContext, text: str) -> Event:
        """Wrap `text` as a terminal model event attributed to this agent."""
        return Event(
            invocation_id=ctx.invocation_id,
            author=self.name,
            branch=ctx.branch,
            content=types.Content(role="model", parts=[types.Part(text=text)]),
        )


# Key on Event.custom_metadata that main._extract reads for the grounding footer.
REMOTE_TELEMETRY_KEY = "remote_transact"


def attach_remote_telemetry(event: Event, tools: list[str], sql: str | None) -> None:
    """Records the transact service's tool calls and SQL on `event`.

    Those tools ran in lab11-transact, a separate Cloud Run service, so they
    never appear as function calls in this process. Without this, the grounding
    footer reported "Tools: none" under an answer that had just run two queries.

    Best effort: an ADK without Event.custom_metadata loses the footer entry,
    never the answer.

    Args:
        event: The event that carries the transact service's answer.
        tools: Tool names the transact service reported.
        sql: The last SQL statement it sent, if any.
    """
    try:
        event.custom_metadata = {
            **(getattr(event, "custom_metadata", None) or {}),
            REMOTE_TELEMETRY_KEY: {"tools": [t for t in tools if isinstance(t, str)], "sql": sql or ""},
        }
    except (AttributeError, TypeError, ValueError):
        logger.debug("could not attach remote telemetry to the event", exc_info=True)


alloydb_transact_agent = RemoteTransactAgent(
    name="alloydb_transact_agent",
    description="Forwards AlloyDB writes to the transactional service, which runs as a different identity.",
    engine="alloydb",
)

spanner_transact_agent = RemoteTransactAgent(
    name="spanner_transact_agent",
    description="Forwards Spanner writes to the transactional service, which runs as a different identity.",
    engine="spanner",
)
