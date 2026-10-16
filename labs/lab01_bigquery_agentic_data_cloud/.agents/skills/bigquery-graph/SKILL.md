---
name: bigquery-graph
description: Structural rules for BigQuery Property Graphs covering CREATE PROPERTY GRAPH DDL over existing tables and views (Customer, Product, Category nodes; PURCHASED, IN_CATEGORY edges), ISO GQL traversal patterns for next-best-product recommendations from lookalike customers, how to run them through the remote BigQuery MCP server at bigquery.googleapis.com/mcp, and the decision criteria for choosing BigQuery Graph over Spanner Graph. Use when modelling relationships on top of BigQuery data or when asked which graph engine fits a workload.
---
# BigQuery Property Graph DDL, GQL, and Engine Selection

## 1. Graph Construction over Existing Tables (DDL)

BigQuery does not store graph data separately. A property graph is a **metadata overlay** on tables or views that already exist in a dataset — no data is copied and no ETL step is required. Model the node and edge sources first (views are the idiomatic choice, so the graph tracks the source tables automatically), then declare the graph:

```sql
-- Step 1: Node and edge sources already exist as views over the base tables.
--   ecommerce_graph_lab.graph_customers        <- ecommerce_lab.customers
--   ecommerce_graph_lab.graph_products         <- ecommerce_lab.products
--   ecommerce_graph_lab.graph_categories       <- SELECT DISTINCT category FROM products
--   ecommerce_graph_lab.graph_purchases        <- ecommerce_lab.orders (product_id IS NOT NULL)
--        one row per ORDER ITEM, uniquely keyed by order_item_id
--   ecommerce_graph_lab.graph_product_category <- products (product_id, category)

-- Step 2: Declare the property graph as a metadata layer.
CREATE OR REPLACE PROPERTY GRAPH `{PROJECT_ID}.ecommerce_graph_lab.ecommerce`
  NODE TABLES (
    graph_customers  KEY (customer_id) LABEL Customer PROPERTIES ALL COLUMNS,
    graph_products   KEY (product_id)  LABEL Product  PROPERTIES ALL COLUMNS,
    graph_categories KEY (category_id) LABEL Category PROPERTIES ALL COLUMNS
  )
  EDGE TABLES (
    graph_purchases
      KEY (order_item_id)
      SOURCE KEY (customer_id) REFERENCES graph_customers (customer_id)
      DESTINATION KEY (product_id) REFERENCES graph_products (product_id)
      LABEL PURCHASED
      PROPERTIES (total_amount, order_date, status),
    graph_product_category
      KEY (product_id)
      SOURCE KEY (product_id) REFERENCES graph_products (product_id)
      DESTINATION KEY (category_id) REFERENCES graph_categories (category_id)
      LABEL IN_CATEGORY
  );
```

A row becomes a node; a foreign key becomes an edge, whether it sits in a fact table (`PURCHASED` from `orders`) or on the entity itself (`IN_CATEGORY` from `products.category`). Nodes may come from a `DISTINCT` view (`graph_categories`). Keep node and edge sources as separate views: a thin edge view (`graph_product_category`) is clearer than reusing a node view as an edge table.

Structural rules:

* **`KEY` must be unique** in its source table or view. For nodes this is the entity's primary identifier; for edges it is whatever uniquely identifies one relationship *row*. Check this rather than assuming it: `order_id` looks like the natural edge key but is **not** unique here, because a basket containing two items produces two rows sharing one order id. The grain is the order item, so the key is `order_item_id`. A duplicated edge key silently multiplies traversal results.
* **`SOURCE KEY` / `DESTINATION KEY`** are the foreign-key columns on the *edge* source; `REFERENCES` names the *node* source and the column matching its `KEY`. Direction follows source → destination, so `graph_purchases` reads as Customer → Product.
* **`LABEL`** is the identifier used in `MATCH` patterns. It is independent of the table name; keep labels singular and PascalCase for nodes, SCREAMING_CASE verbs for edges.
* **`PROPERTIES ALL COLUMNS`** exposes every column of the source; list columns explicitly when the source carries fields the graph layer should not surface.
* The graph and all its node/edge sources must live in the **same region**. Dropping or replacing an underlying view invalidates the graph.

---

## 2. Graph Traversals (GQL)

Queries wrap an ISO GQL `MATCH` inside the `GRAPH_TABLE` table-valued function. `GRAPH_TABLE` returns a normal relational rowset, so standard SQL — `GROUP BY`, `HAVING`, `ORDER BY`, window functions — composes on top of it in the outer query. Push pattern matching into `MATCH`; push aggregation into the outer `SELECT`.

### Pattern A: Next Best Product (lookalike peers, 3 hops + NOT EXISTS)

The question: *customers who share at least two products with this customer — what did they buy that this customer has not?* As one pattern:

```sql
WITH hops AS (
  SELECT *
  FROM GRAPH_TABLE(`{PROJECT_ID}.ecommerce_graph_lab.ecommerce`
    MATCH (me:Customer)-[:PURCHASED]->(s:Product)<-[:PURCHASED]-(peer:Customer)
          -[:PURCHASED]->(rec:Product)-[:IN_CATEGORY]->(cat:Category)
    WHERE me.customer_id = @cid
      AND peer.customer_id <> me.customer_id
      AND NOT EXISTS {
        MATCH (m2:Customer)-[:PURCHASED]->(r2:Product)
        WHERE m2.customer_id = me.customer_id AND r2.product_id = rec.product_id }
    RETURN peer.customer_id AS peer_id, s.product_id AS shared_id,
           rec.product_id AS rec_id, rec.name AS rec_name, cat.category_id AS rec_category)
),
peer_strength AS (            -- how much each peer resembles the customer
  SELECT peer_id, COUNT(DISTINCT shared_id) AS shared
  FROM hops GROUP BY peer_id HAVING shared >= 2
),
recs AS (SELECT DISTINCT rec_id, rec_name, rec_category, peer_id FROM hops)
SELECT r.rec_id, r.rec_name, r.rec_category, SUM(p.shared) AS score, COUNT(*) AS peers
FROM recs r JOIN peer_strength p USING (peer_id)
GROUP BY 1, 2, 3
ORDER BY score DESC, rec_id
LIMIT 5;
```

Rules this pattern encodes:

* **An `EXISTS` / `NOT EXISTS { MATCH ... }` subquery cannot re-bind outer variables.** `NOT EXISTS { MATCH (me)-[:PURCHASED]->(rec) }` is rejected; bind fresh variables (`m2`, `r2`) and correlate with equality filters.
* **Paths multiply.** One row per (shared product × peer × recommendation) path, so count with `DISTINCT` in the outer query. A peer who shares three products produces three rows per recommendation.
* **Pass the seed as a query parameter** (`@cid`), never by string interpolation.
* The plain-SQL equivalent (self-joins on `graph_purchases` + anti-join) returns the identical ranking. BigQuery executes `GRAPH_TABLE` as a relational plan, so do not claim the graph is faster or cheaper; the benefit is a query that reads like the question and grows by one arrow per hop.

### Pattern B: Lookalike Peers (who resembles this customer)

The first two hops of Pattern A on their own — audience expansion and lookalike segments:

```sql
SELECT peer_id, COUNT(DISTINCT shared_id) AS shared_products
FROM GRAPH_TABLE(`{PROJECT_ID}.ecommerce_graph_lab.ecommerce`
  MATCH (me:Customer)-[:PURCHASED]->(s:Product)<-[:PURCHASED]-(peer:Customer)
  WHERE me.customer_id = @cid AND peer.customer_id <> me.customer_id
  RETURN peer.customer_id AS peer_id, s.product_id AS shared_id
)
GROUP BY peer_id
HAVING shared_products >= 2
ORDER BY shared_products DESC
LIMIT 20;
```

For whole-graph pairs instead of one seed, drop the `@cid` filter and add `a.customer_id < b.customer_id` to remove mirrored (A,B)/(B,A) pairs.

### Pattern C: One More Hop (peers of peers)

Edge direction matters: `PURCHASED` always points Customer → Product, so `(c:Customer)-[:PURCHASED]->{1,3}(p:Product)` can never go beyond one hop (a Product has no outgoing `PURCHASED`). Reach further by alternating directions explicitly:

```sql
SELECT COUNT(DISTINCT peer2_id) AS second_degree_peers
FROM GRAPH_TABLE(`{PROJECT_ID}.ecommerce_graph_lab.ecommerce`
  MATCH (me:Customer)-[:PURCHASED]->(:Product)<-[:PURCHASED]-(p1:Customer)
        -[:PURCHASED]->(:Product)<-[:PURCHASED]-(p2:Customer)
  WHERE me.customer_id = @cid AND p2.customer_id <> me.customer_id
  RETURN p2.customer_id AS peer2_id
);
```

Path counts grow multiplicatively per hop. Always bound the pattern, always `LIMIT` exploratory queries, and dry-run before paying for a wider traversal.

---

## 2b. Running These Queries From an Agent: the BigQuery MCP Server

An agent does not call the BigQuery client library for any of this. It reaches BigQuery through a remote MCP server, and `GRAPH_TABLE` queries go through the same read-only tool as ordinary SQL:

| | |
|---|---|
| **Endpoint** | `https://bigquery.googleapis.com/mcp` |
| **Transport** | Streamable HTTP (MCP protocol `2025-06-18`) |
| **Auth** | OAuth 2.0 bearer token from Application Default Credentials |

**Tools it exposes:**

| Tool | Use it for |
|---|---|
| `list_dataset_ids` | Discovering datasets in a project. |
| `get_dataset_info` | Dataset metadata. |
| `list_table_ids` | Listing tables in a dataset. |
| `get_table_info` | Authoritative column names and types — cheaper than a failed query. |
| `execute_sql_readonly` | Running `SELECT` / `WITH` / `GRAPH_TABLE`. Required args: `projectId`, `query`. Optional `dryRun`. |
| `execute_sql` | **Write-capable. Do not expose it to an analyst agent.** |

```python
BIGQUERY_TOOLSET = McpToolset(
    connection_params=StreamableHTTPConnectionParams(
        url="https://bigquery.googleapis.com/mcp",
        timeout=30.0,
    ),
    header_provider=google_auth_headers,   # per-request ADC bearer token
    tool_filter=[
        "list_dataset_ids",
        "get_dataset_info",
        "list_table_ids",
        "get_table_info",
        "execute_sql_readonly",
    ],
)
```

Structural rules:

* **The `tool_filter` is the guardrail, not a tidiness setting.** `execute_sql` can `DROP` a table. Omitting it from the filter means the model is never told it exists, so no prompt can reach it. Back that with IAM — an identity holding only `roles/bigquery.dataViewer` and `roles/bigquery.jobUser` cannot write even if a write tool were somehow invoked.
* **`CREATE PROPERTY GRAPH` is DDL**, so it is *not* runnable through `execute_sql_readonly`. Declare the graph in Terraform or by hand once, as an operator; the agent only traverses it.
* **Use `get_table_info` before composing a traversal.** Node and edge keys must be verified, not assumed — see the `KEY` rule in Section 1.
* **`dryRun: true` on `execute_sql_readonly`** validates a GQL query and reports bytes scanned without running it. Use it to check an unbounded traversal before paying for it.

---

## 3. Engine Selection: BigQuery Graph vs Spanner Graph

Both engines speak ISO GQL and use `GRAPH_TABLE` + `MATCH`, so the query language is *not* the deciding factor. The workload is.

| Dimension | BigQuery Graph | Spanner Graph |
|---|---|---|
| **Latency** | Seconds | Milliseconds |
| **Workload class** | Analytical (OLAP) | Operational (OLTP) |
| **Access pattern** | Batch, scheduled, exploratory | Real-time, ACID transactional |
| **Best at** | Multi-hop pattern analysis over the full graph | Live traversals from a known starting node |
| **Data scale per query** | Millions of edges scanned | Small neighborhoods, indexed lookups |
| **Storage model** | Metadata overlay on existing tables/views — no copy | Purpose-built graph over Spanner tables |
| **Writes** | Batch loads into the underlying tables | Transactional DML, read-write transactions |
| **Typical caller** | Analyst, dashboard, notebook, nightly job | Application request path, API endpoint |

**The selection rule:**

> * *"Analyze patterns across millions of edges"* → **BigQuery Graph**
> * *"Traverse live data in a transaction"* → **Spanner Graph**

Applied to concrete asks:

* "Which products are bought together across our entire order history?" → BigQuery Graph. Whole-graph aggregation, seconds are fine.
* "Show this logged-in customer's recommendations right now, inside the checkout transaction." → Spanner Graph. Single-node start, millisecond budget, must be transactionally consistent.
* "Find fraud rings spanning 4 hops in last quarter's data." → BigQuery Graph. Analytical, retrospective, wide.
* "Block this transfer if the counterparty is within 2 hops of a flagged account." → Spanner Graph. Request path, live data, ACID.

The two are complementary, not competing: discover the pattern analytically in BigQuery Graph, then serve the resulting decision from Spanner Graph.

---

## 4. Release Status and Disclosure Requirements

Do not state a release status (GA / Preview) from memory or from this skill: it changes. A few capabilities in this architecture are pre-GA. When you recommend any of them, tell the user to check the official product documentation and release notes for the current status, SLA and terms, and say so plainly if a pre-GA component would sit on a production path.

Two requirements are not release-status questions and must always be stated when you recommend BigQuery Graph: `GRAPH_TABLE` requires an **Enterprise or Enterprise Plus reservation**, and node `KEY` uniqueness is not enforced.

---

## Execution Guidelines

* **Zero Hallucination Constraints:** Treat capitalized terms (`CREATE PROPERTY GRAPH`, `NODE TABLES`, `EDGE TABLES`, `KEY`, `SOURCE KEY`, `DESTINATION KEY`, `LABEL`, `PROPERTIES`, `GRAPH_TABLE`, `MATCH`, `RETURN`) as reserved structural constants, and the MCP tool names (`execute_sql_readonly`, `get_table_info`, `list_table_ids`) as reserved identifiers.
* **Strict Variable Substitution:** Replace `{PROJECT_ID}`, `{CUSTOMER_ID}`, and any label or view name from the workspace configuration resolved at runtime — never invent dataset, view, or label names.
* **Aggregate outside, match inside:** `MATCH ... RETURN` emits rows; `GROUP BY`/`HAVING`/`ORDER BY` belong in the outer `SELECT` over `GRAPH_TABLE`.
* **Always bound hop counts** in quantified patterns, and always `LIMIT` exploratory graph queries.
