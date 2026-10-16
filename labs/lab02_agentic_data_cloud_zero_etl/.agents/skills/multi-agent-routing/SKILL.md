---
name: multi-agent-routing
description: Building a multi-agent system where routing is a dict lookup in code rather than a model's judgment. Covers the four structural principles (empty-tool router, code-level dispatch, an isolated classifier session, one tool set per agent), a tolerant intent parser, why a capability boundary needs a second deployment rather than a second agent object, and how to make a mis-route fail loudly instead of answering plausibly. Use when designing an agent that must delegate, or when a system's wrong answers come from the wrong agent having handled the question.
---
# Routing that is code, not judgment

## 1. The failure this prevents

A prompt-routed system mis-routes silently. The question goes to the wrong
specialist, that specialist answers confidently within its own competence, and
nothing anywhere reports a problem. You get a plausible answer to a question
nobody asked, and no trace that says so.

The alternative is not a better prompt. It is moving the decision out of the
model:

```python
_INTENT_TO_SPECIALIST: dict[str, BaseAgent] = {
    "ANALYTICS":         bq_analyst_specialist,
    "CATALOG":           catalog_specialist,
    "GRAPH_ANALYTICAL":  graph_specialist,
    "GRAPH_OPERATIONAL": graph_specialist,
    "TRANSACT_ALLOYDB":  alloydb_transact_agent,
    "TRANSACT_SPANNER":  spanner_transact_agent,
}
```

The model produces one word. Python does the routing. An unmapped intent raises
`KeyError` and stops — an outcome you can see, log and alert on, which a
confident wrong answer is not.

## 2. Four principles, each expressed as code shape

**Principle 1 — structural enforcement.** The router has `tools=[]`. You can
write "do not query the database" into an instruction and a determined model will
still try. An empty tool list means there is nothing on the menu.

```python
router = LlmAgent(
    name="router",
    instruction=CLASSIFICATION_INSTRUCTION,
    tools=[],            # not advice; there is nothing to call
    # sub_agents deliberately omitted
)
```

**Principle 2 — agent order is code, not prompt.** Use the dict above, not ADK's
prompt-driven hand-off. When routing is wrong you fix a dict entry and the fix is
permanent; a prompt fix holds until the next model revision.

**Principle 3 — isolate the classifier.** Run the router in a throwaway session
so its output never enters the conversation the specialist reads:

```python
async def _run_agent_async(agent, query, session_id, app_name) -> str:
    session_service = InMemorySessionService()          # fresh, per call
    session = await session_service.create_session(app_name=app_name, user_id="dispatch")
    runner = Runner(app_name=app_name, agent=agent, session_service=session_service)
    ...
```

The specialist then receives the user's original question and nothing else. It
cannot be led by the classifier's phrasing, and the word `TRANSACT_SPANNER` never
appears in its history.

**Principle 5 — one job, one tool set.** Each specialist gets only the tools for
its engine, and a `tool_filter` on every MCP toolset. A read-only analyst is not
*asked* to avoid the write tool; the write tool is never advertised to it.

## 3. Parse the router's output tolerantly

The router is told to return one word. Sometimes it returns
`"ANALYTICSbecause the user asked about revenue"`. Handle the four real
deviations in order, and no more than that:

```python
def parse_intent(raw: str) -> str:
    stripped = (raw or "").strip()
    if not stripped:
        return "UNKNOWN"
    if stripped.upper() in VALID_INTENTS:          # 1. exact
        return stripped.upper()

    first = stripped.split()[0]
    cleaned = re.sub(r"[^A-Z_]+$", "", first.upper())
    if cleaned in VALID_INTENTS:                    # 2. first token, depunctuated
        return cleaned

    for intent in VALID_INTENTS:                    # 3. prefix, lowercase suffix only
        if first.upper().startswith(intent):
            suffix = first[len(intent):]
            if not suffix or suffix[0].islower():
                return intent

    return _ALIASES.get(cleaned, "UNKNOWN")         # 4. known misspellings
```

Rule 3 is the subtle one. A **lowercase** suffix means a word was appended
without a space, so the prefix is the intent. An **underscore** suffix
(`ANALYTICS_EXTRA`) means a different identifier was intended, and must not
match. Getting this backwards silently accepts intents that do not exist.

Keep an alias table for spellings a model reaches for when it is nearly right
(`GRAPH_ANALYSIS` → `GRAPH_ANALYTICAL`). This is not leniency about correctness:
the alternative is a hard failure on a query that was classified correctly and
typed badly. But alias a genuinely ambiguous word to `UNKNOWN` — if the user did
not say which engine to write to, guessing puts real rows in the wrong database.

## 4. A capability boundary needs a second deployment

This is the constraint people meet late, and it changes the architecture.

**ADK runs sub-agents in-process, and a process has exactly one service
account.** So an in-process orchestrator with five specialists gives all five the
*same* permissions. If some of them must be able to write and others must not,
"specialist" is not a strong enough boundary — they need different identities,
and therefore different deployments.

The shape that works: keep the in-process orchestrator for everything that shares
a capability, and make the differently-capable agents a **separate service**
reached over HTTP, wrapped in a thin `BaseAgent` so the orchestrator calls it
like any other specialist.

```python
class RemoteTransactAgent(BaseAgent):
    engine: str = "alloydb"

    async def _run_async_impl(self, ctx):
        token = fetch_id_token(Request(), TRANSACT_URL)   # ID token, not access token
        response = requests.post(f"{TRANSACT_URL}/transact",
                                 json={"message": extract_query(ctx), "engine": self.engine},
                                 headers={"Authorization": f"Bearer {token}"})
        yield self._event(ctx, response.json()["response"])
```

Then enforce the boundary three ways, none of them in a prompt:

* the read service holds no write role, so rewriting its code changes nothing;
* the write service holds no read role, so its blast radius is the tables it was
  granted;
* only the read service holds `run.invoker` on the write service, so the write
  path is reachable **only** through the router — which means every write passes
  the classifier.

Cloud Run service-to-service auth needs an **identity** token whose audience is
the receiving URL. An access token is rejected with a bare 401.

Pass the already-decided intent in the request body and dispatch on it in code at
the far end too. Re-classifying downstream puts the decision back into model
judgment at the exact moment it matters most.

## 5. Make failure legible

Expose the routing decision as data, separately from the answer:

* A `/route` endpoint that classifies and returns the intent **without** running
  a specialist. "The router chose X" is a claim; this is the decision itself.
* `agents_used` in every response, collected from `event.author`. Which agent
  answered is the one thing a multi-agent system must be able to prove.
* Distinct, specific errors: "the router returned nothing recognisable" and
  "intent X has no specialist wired" are different bugs and should read
  differently.

When an agent refuses something, three sources are possible and they are worth
telling apart: **the model** decided not to (rephrasing may change it); a
**`tool_filter`** never offered the tool (editing the filter changes it,
rephrasing cannot); or **IAM** denied the request after it left the model
(nothing about the conversation changes it). Only the third is a guarantee.
Design so the ones that matter are all of the third kind.
