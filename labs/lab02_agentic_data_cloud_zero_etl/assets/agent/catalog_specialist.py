# Author: Wissem Khlifi - August 2026
"""Knowledge Catalog grounding: the scoped search_entries tool, and an agent.

The tool is ported from Lab 10 `src/agent/agent.py`; the specialist wrapper
around it is new.

WHY THIS IS A FUNCTION AND NOT THE MCP TOOL OF THE SAME NAME
-------------------------------------------------------------
The Knowledge Catalog MCP server exposes `search_entries`, and its version
searches every project the caller can read. In a shared org that means a lab
query for "revenue" comes back with definitions from unrelated projects: delete
the Revenue term in YOUR project and the agent quietly answers from someone
else's, with search_entries visible in the trace, looking perfectly grounded.

The MCP tool can be scoped -- but only by arguments the MODEL chooses to pass.
An argument the model controls is not a guardrail. The implementation below
fixes the scope in code, and is registered under the same name, so the only
search_entries any agent in this service can reach is one that cannot leave this
project.
"""

from __future__ import annotations

import os

import google.auth
import google.auth.transport.requests
from google.adk import Agent

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
GLOSSARY_LOCATION = os.environ.get("GLOSSARY_LOCATION", "us-central1")

_CREDENTIALS, _ = google.auth.default(
    scopes=["https://www.googleapis.com/auth/cloud-platform"]
)


def _session() -> google.auth.transport.requests.AuthorizedSession:
    """Return an ADC-authorised session, refreshing the token if it has expired."""
    if not _CREDENTIALS.valid:
        _CREDENTIALS.refresh(google.auth.transport.requests.Request())
    return google.auth.transport.requests.AuthorizedSession(_CREDENTIALS)


def search_entries(query: str, page_size: int = 10) -> dict:
    """Search this project's Knowledge Catalog for governed business definitions.

    Call this FIRST for any business term in the user's question -- "revenue",
    "high value customer", "churn risk" -- and derive SQL filters from the
    definition it returns. Never invent a threshold or a column.

    Args:
        query: Search string, e.g. "revenue".
        page_size: Maximum number of entries to return.

    Returns:
        The raw searchEntries response. An empty `results` list means the term
        has no governed definition in this project, which is a reason to stop and
        ask -- not a reason to guess.
    """
    # `scope` is set here, in code, and is not a parameter the model can reach.
    # Note that projectId / x-goog-user-project do NOT do this: those are
    # documented as billing and quota attribution, not filtering.
    resp = _session().post(
        f"https://dataplex.googleapis.com/v1/projects/{PROJECT_ID}"
        "/locations/global:searchEntries",
        json={
            "query": query,
            "pageSize": page_size,
            "scope": f"projects/{PROJECT_ID}",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def lookup_entry(name: str) -> dict:
    """Fetch the full detail of one catalog entry returned by search_entries.

    Args:
        name: The entry's full resource name, taken verbatim from a
            search_entries hit.

    Returns:
        The raw lookupEntry response, including aspects and the term description.
    """
    resp = _session().get(
        f"https://dataplex.googleapis.com/v1/projects/{PROJECT_ID}"
        "/locations/global:lookupEntry",
        params={"entry": name, "view": "ALL"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


catalog_specialist = Agent(
    name="catalog_specialist",
    model=os.environ.get("CATALOG_SPECIALIST_MODEL", "gemini-3.8-flash"),
    description="Answers what a business term MEANS, from the governed glossary only.",
    instruction=f"""You are the Knowledge Catalog specialist for a governed data platform.

Your job is to say what a business term MEANS according to the glossary in
project {PROJECT_ID} — never to compute a number, and never to supply a
definition from your own knowledge.

Protocol:
1. Call search_entries with the term from the user's question.
2. If there is a promising hit, call lookup_entry on it for the full description.
3. Answer with the governed definition, quoted, and name the glossary term you
   read it from.

Rules:
- If search_entries returns nothing, say so plainly and stop. Ask the user to
  confirm a definition. Do NOT fall back on a general-knowledge answer: a
  plausible invented definition is worse than "there is no governed definition",
  because nothing downstream can tell the two apart.
- Never write or run SQL. You explain definitions; another specialist computes.
- If the user is really asking for a number rather than a definition, answer the
  definitional part and say which metric they should ask for next.
""",
    tools=[search_entries, lookup_entry],
)
