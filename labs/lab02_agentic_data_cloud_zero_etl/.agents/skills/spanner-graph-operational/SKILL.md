---
name: spanner-graph-operational
description: Choosing and operating Spanner Graph as the operational half of a two-graph architecture, and knowing when BigQuery Graph is the right engine instead. Covers the ENTERPRISE and GoogleSQL prerequisites, the reserved words that break a LABEL clause, interleaving and its primary-key rule, tables used as both node and edge, and the two-phase execute_sql then commit write through the Spanner MCP server. Use when modelling relationships that must be traversed transactionally, or when deciding which of two graph engines answers a question.
---
# Spanner Graph as the operational engine

Lab 02's `spanner-graph` skill covers the DDL, DML and GQL grammar generically.
This one is about the decisions around it: when Spanner Graph is the right engine,
the four structural rules that break a schema, and how a write actually commits.

## 1. Two graph engines is a design, not a duplication

| | **Spanner Graph** | **BigQuery Graph** |
|---|---|---|
| Sits on | Spanner's own tables | tables or views, including federated ones |
| Sees | Spanner rows only | every source its views reach |
| Latency | milliseconds | seconds |
| Freshness | the instant a transaction commits | live at query time |
| Good at | one entity and its neighbourhood, right now | population patterns, history, cross-source |
| Costs | provisioned Spanner capacity | a query, and GRAPH_TABLE needs an ENTERPRISE reservation |

The choice follows the question, not preference:

* *"What is stuck for this customer right now?"* — one entity, current state,
  inside a transaction. **Spanner.**
* *"Which segments return the most, across every system we have?"* — population,
  history, more than one source. **BigQuery.**
* *"Show me both, and how long each took."* — run them concurrently and report
  the latencies. The difference *is* the answer to "which engine should we use".

Put that decision in **code**, not in a prompt: a keyword classifier that maps a
question to one of three tools is auditable and stable, where a model asked to
"pick the right engine" drifts between releases.

## 2. Four structural rules that will break your schema

**ENTERPRISE edition and `GOOGLE_STANDARD_SQL`.** Both are hard prerequisites.
The failure arrives at `CREATE PROPERTY GRAPH` — after the tables exist and after
the data has landed — which is the most expensive moment to discover it. The
POSTGRESQL dialect has no property graphs at all.

**`ORDER`, `INCLUDES` and `CONTAINS` are reserved in a `LABEL` clause.** They are
also the three most natural labels for a commerce model. The parse error points
at the surrounding syntax, never at the word. Use `CustomerOrder`, `HAS_ITEM`,
`PART_OF`.

**`INTERLEAVE IN PARENT` requires the child's primary key to *start with* the
parent's.** So an order-item table interleaved in customers is keyed
`(customer_id, order_item_id)` — customer first, even though `order_item_id` is
what identifies the row. Interleaving is worth the awkwardness: it physically
co-locates a customer's rows with the customer, so the traversal is a
single-split read. An `INSERT` that omits `customer_id` fails with an error about
a key column that never mentions interleaving.

**A table used as both a node and an edge needs explicit `AS` aliases, one per
role.** An order-items table is usually all three: the order node, the
customer→order edge, and the order→product edge. Without aliases Spanner rejects
the statement with a duplicate-name error naming the table.

```sql
CREATE OR REPLACE PROPERTY GRAPH EcommerceOpsGraph
  NODE TABLES (
    Customers      KEY (customer_id)                LABEL Customer,
    Products       KEY (product_id)                 LABEL Product,
    CustomerOrders AS OrderNode
                   KEY (customer_id, order_item_id) LABEL CustomerOrder
  )
  EDGE TABLES (
    CustomerOrders AS Placed
      KEY (customer_id, order_item_id)
      SOURCE      KEY (customer_id)                REFERENCES Customers (customer_id)
      DESTINATION KEY (customer_id, order_item_id) REFERENCES OrderNode (customer_id, order_item_id)
      LABEL PLACED,
    CustomerOrders AS HasItem
      KEY (customer_id, order_item_id)
      SOURCE      KEY (customer_id, order_item_id) REFERENCES OrderNode (customer_id, order_item_id)
      DESTINATION KEY (product_id)                 REFERENCES Products (product_id)
      LABEL HAS_ITEM
  );
```

Declare the graph in the **database DDL**, alongside the tables. A property graph
is metadata over tables, so declaring it on empty tables is legal — and it means
the graph is queryable the moment data lands, with no second step to forget.

## 3. Traversing

```sql
GRAPH EcommerceOpsGraph
MATCH (c:Customer)-[:PLACED]->(o:CustomerOrder)
WHERE o.status = 'Processing'
RETURN c.customer_id, c.segment, COUNT(o.order_item_id) AS open_orders
GROUP BY customer_id, segment
ORDER BY open_orders DESC
LIMIT 10
```

Spanner GQL aggregates inside the statement. **BigQuery does not** — there,
`GRAPH_TABLE` returns a flat rowset and the aggregation belongs in an outer
`SELECT`. Carrying a Spanner-shaped query across to BigQuery unchanged is the
most common way a traversal fails to compile.

## 4. Writing through the Spanner MCP server: the two-phase commit

`https://spanner.googleapis.com/mcp` (check the release notes for its current status). Useful tools:

* `create_session` — DML and its commit must share a session
* `execute_sql` — runs DML or DQL, returns a **`precommit_token`**
* `commit` — pass that token back; this is what makes the write durable
* `execute_sql_readonly` — single-use read, no session, no commit

**A DML statement is not durable when `execute_sql` returns.** An agent that
stops there reports success and changes nothing, and the row it claims to have
written is simply absent. This is the single most likely reason a
"write, then verify" demonstration shows an unchanged count. Say it explicitly
in the agent's instruction, and require it to confirm the commit before claiming
the row exists.

Filter the toolset. `create_instance`, `create_database` and
`update_database_schema` are also on that server, and a row-writing agent has no
use for any of them.

Writers need `roles/spanner.databaseUser`; readers need
`roles/spanner.databaseReader`. Giving a read-only agent `databaseUser` because
it is "the Spanner role" hands it DML.

## 5. Verifying

A property graph over empty tables answers successfully and returns nothing, so
**assert row counts**:

```sql
GRAPH EcommerceOpsGraph
MATCH (c:Customer)-[:PLACED]->(o:CustomerOrder)-[:HAS_ITEM]->(p:Product)
RETURN COUNT(*) AS n   -- must be > 0
```

Check the key types too. If ids are `STRING` in one engine and `INT64` in
another, every cross-engine union fails later with a type error that names a view
rather than the schema that caused it.
