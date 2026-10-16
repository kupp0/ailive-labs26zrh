---
name: knowledge-catalog
description: Grounding rules for BigQuery Knowledge Catalog (Dataplex Universal Catalog). Covers why agents must resolve business glossary terms before generating SQL, how to reach the catalog through the remote Knowledge Catalog MCP server at dataplex.googleapis.com/mcp, the catalog-first agent instruction contract, and how policy tags and column-level security produce masked results for an agent while a human sees real values. Use when building or reviewing an agent that answers questions about governed business metrics.
---
# Knowledge Catalog Grounding for Data Agents

## 1. Why the Catalog Comes Before the Agent

An LLM asked *"what is our revenue from high value customers?"* will confidently write SQL. It will also confidently invent the definitions, because nothing in the schema tells it what the business means:

```sql
-- What an ungrounded agent writes. Every line is a silent guess.
SELECT SUM(o.total_amount) AS revenue          -- guess: revenue = orders in ANY status
FROM `{PROJECT_ID}.ecommerce_lab.orders` o
JOIN `{PROJECT_ID}.ecommerce_lab.customers` c USING (customer_id)
WHERE c.lifetime_value > 1000;                  -- guess: the threshold
```

Two failures are baked in:

* **`revenue` sums every order**, whatever its state. The seeded `orders` table carries five status values — `Complete`, `Shipped`, `Processing`, `Cancelled`, `Returned` — and only `Complete` is recognised revenue. Summing all five counts cancelled and returned orders, plus in-flight ones that may never settle. On the lab data that turns **79,849.40 into 320,604.36 — a four-fold overstatement**. The number is wrong, plausible, and impossible to spot without knowing the rule.
* **`lifetime_value > 1000` is a hallucinated threshold.** The governed definition of "high value customer" lives in the glossary and may be a different number, a different column, or a segment membership rule entirely. Here the guess is not merely off — the largest `lifetime_value` in this data is in the hundreds, not the thousands, so the filter matches **nothing** and the agent reports that there are no high value customers. A hallucinated threshold fails silently as an empty result, never as an error.

The output is a syntactically valid query, a confident answer, and a figure that does not reconcile with any governed report. This is worse than an error, because nothing surfaces as failed.

**The correction is architectural, not a matter of better prompting.** The business definition exists exactly once — in the Knowledge Catalog glossary — and the agent must read it before it writes SQL. The catalog is the source of truth; the agent is only a translator from a governed definition into a query. Never let the model supply a metric definition it retrieved from its own weights.

---

## 2. Reaching the Catalog: the Knowledge Catalog MCP Server

Do not write Dataplex client code. The catalog is exposed as a remote MCP server, and an ADK agent consumes it as a toolset:

| | |
|---|---|
| **Endpoint** | `https://dataplex.googleapis.com/mcp` |
| **Transport** | Streamable HTTP (MCP protocol `2025-06-18`) |
| **Auth** | OAuth 2.0 bearer token from Application Default Credentials |

**Tools it exposes:**

| Tool | Use it for |
|---|---|
| `search_entries` | Resolving a business term. Required args: `projectId`, `query`. Optional: `pageSize`, `orderBy`, `scopeType`, `scopeId`, `pageToken`. |
| `lookup_entry` | Full technical metadata for a single entry returned by a search. |
| `lookup_context` | LLM-ready metadata for up to 10 named resources at once. Required: `projectId`, `location`, `resources`. |

Wire it up with `McpToolset` and a **per-request** header provider:

```python
import google.auth
import google.auth.transport.requests
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool import StreamableHTTPConnectionParams

_CREDENTIALS, _ = google.auth.default(
    scopes=["https://www.googleapis.com/auth/cloud-platform"]
)


def google_auth_headers(readonly_context=None) -> dict:
    if not _CREDENTIALS.valid:
        _CREDENTIALS.refresh(google.auth.transport.requests.Request())
    return {"Authorization": f"Bearer {_CREDENTIALS.token}"}


CATALOG_TOOLSET = McpToolset(
    connection_params=StreamableHTTPConnectionParams(
        url="https://dataplex.googleapis.com/mcp",
        timeout=30.0,
    ),
    header_provider=google_auth_headers,
    tool_filter=["search_entries", "lookup_entry", "lookup_context"],
)
```

Structural rules for this connection:

* **Use `header_provider`, not a static `headers` dict.** A token minted once at import expires in an hour, after which every tool call returns 401. `header_provider` is invoked per request, so the credential is refreshed on demand.
* **Always name a `tool_filter`.** Passing `None` exposes whatever the server offers today *and* whatever it adds tomorrow. On a server whose tool surface is still evolving, that can change under you.
* **`projectId` is required on every call.** The model must pass it; state the project in the instruction so it has the value.
* **A miss is a governance finding, not an error to route around.** When `search_entries` returns nothing, the agent stops and asks — see the instruction contract below.
* **`google-adk` requires `mcp>=1.24,<2`.** With `mcp` 2.x installed, `McpToolset` fails at import with `cannot import name 'McpHttpClientFactory'`.

---

## 3. Catalog-First Agent Instruction Contract

The tool is necessary but not sufficient — the instruction must make the lookup mandatory and make the grounding visible in every answer:

```python
CATALOG_FIRST_INSTRUCTION = """
You are a governed ecommerce analyst. You answer questions using only BigQuery
data in the `ecommerce_lab` dataset (tables: customers, orders, products).

MANDATORY WORKFLOW — follow it in this order, every time:

1. IDENTIFY every business term in the user's question ("high value customer",
   "revenue", "churn risk", "active order"). A business term is any concept
   whose meaning is not literally a column name.

2. CALL search_entries (Knowledge Catalog MCP) FOR EACH TERM before you write
   a single line of SQL. Pass the project id. Use lookup_entry when a hit needs
   its full definition. You must not write SQL until every term is resolved.

3. DERIVE your filters, joins and aggregations ONLY from the returned
   definitions. If the catalog says revenue is recognised only on completed
   orders, your WHERE clause says status = 'Complete'. Never substitute a
   threshold, a column, or a status filter from your own knowledge.

4. IF search_entries RETURNS NO MATCH for a term: STOP. Tell the user no
   governed definition exists for that term, and ask them to confirm one.
   Do NOT guess and do NOT proceed with an assumed definition.

5. RUN the query with execute_sql_readonly (BigQuery MCP). Inspect schemas
   with get_table_info first when unsure. Aggregate in SQL rather than pulling
   raw rows. If a tool returns an error, report the message; do not retry
   blindly.

6. EXPLAIN your grounding in every answer. State which catalog term you used,
   quote the governed definition, and show how it maps onto the SQL you ran:
   "I used the catalog term 'Revenue', which defines revenue on completed
    orders only. That is why the query filters on the completed status value
    the catalog gave me and excludes every other state."

Never write a status literal from memory, and never copy one out of this file.
The permitted values and their exact casing live in the catalog, in the Order
Status term — retrieve them. This matters because BigQuery string comparison is
case-sensitive: a mis-cased literal such as 'complete' silently matches nothing
and returns zero rather than an error, so a guess fails as a confident wrong
answer rather than as an error. If the term is missing, halt and ask; do not
fall back on values you have seen elsewhere.

Fully qualify every table as `{PROJECT_ID}.ecommerce_lab.<table>`. Refuse
questions that require data outside this dataset.

If any column returns a masked value, say so explicitly and explain that
column-level security applies to the agent's service account.
"""
```

The load-bearing clauses are **step 4** (a missing definition halts the agent instead of degrading it to a guess) and **step 6** (grounding is stated, so a wrong definition is caught by the human reading the answer rather than discovered in a quarterly reconciliation).

---

## 4. Column-Level Security Integration

Catalog grounding governs *meaning*. Policy tags govern *visibility*. They operate independently, and an agent must respect both.

**The mechanism:**

1. A **taxonomy** (in this lab, `PII Classification - <project id>`) holds **policy tags** (in this lab, `SENSITIVE`).
2. A policy tag is attached to a **column** in the BigQuery schema — for example `customers.email`.
3. Once tagged, the column is denied to every principal by default. Access is granted per-identity by IAM role on the tag:

| Role | Effect on a tagged column |
|---|---|
| `roles/datacatalog.categoryFineGrainedReader` | Reads the **real value** |
| `roles/bigquerydatapolicy.maskedReader` | Reads a **masked value** (per the data policy's masking rule) |
| *neither* | Query **fails** — the column cannot be selected at all |

**The two-identity split that makes this visible in the lab:** the human participant and the agent are different principals, so they get different results from the same SQL.

* The **participant** holds `categoryFineGrainedReader` on the `SENSITIVE` tag and sees `alice@example.com`.
* The **agent's service account** holds `maskedReader` and sees the masked form for the same row.
* A third identity with neither role does not get a masked answer — its query errors out entirely.

```sql
-- Identical SQL. Different principals. Different results.
SELECT customer_id, email
FROM `{PROJECT_ID}.ecommerce_lab.customers`
LIMIT 5;

-- Participant (Fine-Grained Reader): alice@example.com
-- Agent SA     (Masked Reader):      XXXXXXXX  (or SHA256 / NULL / default,
--                                               per the masking rule)
```

Enforcement is in **BigQuery**, not in the agent. The agent cannot prompt its way to an unmasked value, and no instruction change alters what it receives — which is precisely the property that makes this safe to deploy. Least privilege for an agent identity means `maskedReader`, never `categoryFineGrainedReader`.

The instruction's job is only **honesty about the masking**: the agent must report that a value was masked rather than treating `XXXXXXXX` as the customer's actual email address, and must never claim it cannot find data when the real cause is column-level access control.

---

## 5. Release Status and Disclosure Requirements

Do not state a release status (GA / Preview) from memory or from this skill: it changes. A few capabilities in this architecture are pre-GA. When you recommend any of them, tell the user to check the official product documentation and release notes for the current status, SLA and terms, and say so plainly if a pre-GA component would sit on a production path.

For a production commitment that must avoid pre-GA MCP servers, note the fallback: the same operations are available on the `google-cloud-dataplex` and `google-cloud-bigquery` client libraries, at the cost of writing and owning the tool code yourself.

---

## 6. Managing Glossary Terms: MCP Reads, gcloud Writes

The Knowledge Catalog MCP server is a **read** surface. `search_entries`,
`lookup_entry` and `lookup_context` discover and return governed definitions;
none of them creates, edits or deletes a term. Attempting a write through MCP
fails as "no such tool", which reads like a broken configuration rather than a
missing capability.

So the split is:

| Intent | Use | Why |
|---|---|---|
| *What does this term say?* | `search_entries` / `lookup_entry` via the **knowledge-catalog MCP server** | Fast, structured, returns the definition text, and the call appears in the agent trace — which is what makes grounding provable |
| *Create / update / delete a term* | `gcloud dataplex glossaries terms …` via **shell** | The write surface. No MCP equivalent today |

Both are needed: the MCP config grants **access** to read, this skill supplies
the **knowledge** of how to write.

### Reading a term (MCP)

```
search_entries(projectId=<project>, query="Revenue", pageSize=5)
lookup_entry(projectId=<project>, name="<entry name from search_entries>")
```

Search resolves from `projects/<project>/locations/global`, which covers every
location — so a glossary in `us-central1` is found without naming the region.

### Creating a term (gcloud)

`--parent` is required and must be the full glossary path. It is the single most
common failure: an empty `$GLOSSARY` produces
`'Term.parent' field should be of the format …  Provided value: .`

```bash
export PROJECT_ID=$(gcloud config get-value project)
export GLOSSARY_LOCATION=us-central1
export GLOSSARY=projects/$PROJECT_ID/locations/$GLOSSARY_LOCATION/glossaries/ecommerce-business-terms

gcloud dataplex glossaries terms create <term-id> \
  --glossary=ecommerce-business-terms \
  --location=$GLOSSARY_LOCATION \
  --project=$PROJECT_ID \
  --parent=$GLOSSARY \
  --display-name="<Term Name>" \
  --description="<the governed definition>"
```

`<term-id>` is lower-kebab-case (`order-status`), distinct from the display name
(`Order Status`).

### Deleting a term (gcloud)

```bash
gcloud dataplex glossaries terms delete <term-id> \
  --glossary=ecommerce-business-terms \
  --location=$GLOSSARY_LOCATION \
  --project=$PROJECT_ID --quiet
```

### Updating a term (gcloud)

Prefer `update` over delete-then-create: it preserves the term's uid and
creation time, and a create against an existing id fails with
`Term with name … already exists`.

```bash
gcloud dataplex glossaries terms update <term-id> \
  --glossary=ecommerce-business-terms \
  --location=$GLOSSARY_LOCATION \
  --project=$PROJECT_ID \
  --description="<new definition>"
```

### The canonical definitions for this lab

These are the exact texts. When asked to *restore*, *reset* or *recreate* a term
"with its original definition", use the matching entry below verbatim — do not
paraphrase, and do not invent a shorter version. The wording is load-bearing:
each sentence becomes part of a `WHERE` clause.

**Term id `revenue`** · display name `Revenue`

```
Total revenue is calculated as SUM(total_amount) from the orders table WHERE status = 'Complete'. Orders in any other state are excluded. The permitted state values and their exact casing are defined by the Order Status term, not here.
```

**Term id `high-value-customer`** · display name `High Value Customer`

```
A customer whose lifetime_value is in the top decile of the customers table. Derive the threshold with APPROX_QUANTILES(lifetime_value, 10)[OFFSET(9)] rather than assuming a constant; it is on the order of tens of dollars, not thousands.
```

**Term id `order-status`** · display name `Order Status`

```
Current state of an order item: Complete, Shipped, Processing, Cancelled or Returned. Values are case-sensitive, so status = 'complete' matches nothing and returns zero rather than an error.
```

So `agy -i "Restore the Revenue term in the glossary with its original definition"`
is enough — the definition is here, and the participant never has to paste a
five-line prompt.

Note what each one carries beyond the rule itself. **Revenue** names the filter
but deliberately does *not* enumerate the other states, deferring that to
**Order Status** — one definition, one owner. **High Value Customer** states the
*scale*, because a threshold without one invites a guess. **Order Status** states
the *casing*, because the failure it prevents is silent.

### What a write actually means here

Editing a term changes what every grounded agent computes, on its next question,
with no code edit and no redeploy. Treat a term's text as production
configuration: the words in `description` become a `WHERE` clause.

Two properties of this dataset are worth carrying into any definition you write:

* **Case sensitivity.** `status = 'complete'` is valid SQL that matches nothing
  and returns zero rather than erroring. A definition that names the permitted
  values must state their exact casing.
* **State the scale, not just the rule.** `lifetime_value` tops out in the hundreds,
  so a definition that says "high value" without a number invites a guess of
  1000 — which returns zero rows and gets reported as "you have none".

> If the Knowledge Catalog MCP server later exposes write tools (its tool surface is still evolving),
> prefer them over `gcloud` and update this section. Until then: MCP reads,
> `gcloud` writes.

---

## Execution Guidelines

* **Catalog before SQL, without exception.** A business term with no governed definition halts the workflow; it never falls back to a model-supplied guess.
* **Zero Hallucination Constraints:** Treat `search_entries`, `lookup_entry`, `lookup_context`, `execute_sql_readonly`, `McpToolset`, `StreamableHTTPConnectionParams`, `header_provider`, `tool_filter`, `categoryFineGrainedReader`, and `maskedReader` as reserved identifiers — do not paraphrase tool, API, or role names.
* **Strict Variable Substitution:** Replace `{PROJECT_ID}` and all dataset, table, term, and taxonomy names from the workspace configuration resolved at runtime.
* **MCP reads, `gcloud` writes.** Use `search_entries` to discover a definition and `gcloud dataplex glossaries terms` to create, update or delete one. Never claim a term was written through MCP.
* **Enforce structurally, not by prompt.** With MCP the guardrail moves but does not weaken: `tool_filter` decides which tools exist at all, and IAM decides what those tools may do. The instruction makes the agent *explain* the guardrails; it is never what *implements* them.
