---
name: zero-etl-federation
description: Moving data between BigQuery, AlloyDB and Spanner without building a pipeline. Covers EXPORT DATA reverse ETL (format CLOUD_SPANNER; format ALLOYDB with a WITH CONNECTION clause) and the return path via EXTERNAL_QUERY and Spanner external datasets, the BigQuery location rules that decide every region in such an architecture, unified views with an origin column, and the node-key uniqueness trap that silently multiplies graph results. Use when data has to exist in an analytical and an operational engine at once, or when asked whether a copy job is really necessary.
---
# Zero-ETL: EXPORT DATA out, federation back

## 1. The two directions, and why they are different mechanisms

A warehouse and an operational database need the same rows for different reasons.
The instinct is to build a pipeline. Two SQL features remove the need:

| Direction | Mechanism | What it does |
|---|---|---|
| BigQuery → Spanner | `EXPORT DATA ... format='CLOUD_SPANNER'` | writes rows into an existing Spanner table |
| BigQuery → AlloyDB | `EXPORT DATA WITH CONNECTION ... format='ALLOYDB'` | writes rows into an existing AlloyDB table |
| AlloyDB → BigQuery | `EXTERNAL_QUERY(connection, sql)` | pushes SQL down to AlloyDB, returns a rowset |
| Spanner → BigQuery | external dataset, `google-cloudspanner:/` URI | exposes Spanner tables as BigQuery tables |

Some of these are pre-GA. Check the official product documentation and release notes for current status before recommending them.

The asymmetry is deliberate and worth understanding. **Going out is a write, so it
copies.** **Coming back is a read, so it does not.** `EXPORT DATA` materialises
rows in the target engine; `EXTERNAL_QUERY` and external datasets execute at
query time against live data. A row written to Spanner is visible to BigQuery on
the next query, with no freshness window, because nothing was copied in that
direction.

That is the entire architecture. There is no third component.

## 2. EXPORT DATA: two rules that are not optional

**The destination table must already exist.** `EXPORT DATA` never creates one.
For Spanner that is easy — put the DDL in `google_spanner_database.ddl` and the
table exists at database creation. For AlloyDB there is no declarative
equivalent, so the schema has to be created some other way before the export
runs (see `alloydb-transactional` for doing it through the MCP server).

**Spanner exports need an ENTERPRISE reservation.** On-demand billing refuses
them. If you already have a reservation for another reason — BigQuery Graph
needs one too — reuse it: only ONE project-wide QUERY assignment can exist per
location, so a second one fails rather than adding capacity.

```sql
EXPORT DATA OPTIONS (
  uri             = 'https://spanner.googleapis.com/projects/P/instances/I/databases/D',
  format          = 'CLOUD_SPANNER',
  spanner_options = '{"table": "CustomerOrders"}'
) AS
SELECT customer_id, order_item_id, order_id, product_id,
       total_amount, status, order_date, written_by
FROM sample_orders;
```

Column names in the `SELECT` must match the destination table's columns.

**The two URIs are not the same shape, and neither is the one you already know.**
This is the single most time-consuming mistake in the area, because every wrong
form fails with an error that names neither the option nor the alternative:

```sql
-- Spanner: the API endpoint, naming the DATABASE.
uri = 'https://spanner.googleapis.com/projects/P/instances/I/databases/D'
format = 'CLOUD_SPANNER'
spanner_options = '{"table": "CustomerOrders"}'

-- AlloyDB: the API endpoint naming the INSTANCE, format ALLOYDB (not
-- CLOUD_ALLOYDB), and the connection is a CLAUSE, not an option.
EXPORT DATA
WITH CONNECTION `PROJECT.us.alloydb_conn`
OPTIONS (
  uri = 'https://alloydb.googleapis.com/projects/P/locations/R/clusters/C/instances/I',
  format = 'ALLOYDB',
  alloydb_options = '{"table": "orders"}'
)
```

`google-cloudspanner:/` is the **external dataset** syntax — a different
feature, used in the other direction. Passing it to `EXPORT DATA` is rejected
with `uri must start with a valid prefix`.

The connection carries the credential; the uri only says which instance to write
to. Omit the clause and you get `CONNECTION clause is required for AlloyDB
export`; pass it as `connection_id` instead and you get `Unknown option`.

**Put several exports in one script, not several jobs.** Statements inside a
BigQuery script run sequentially and each commits before the next begins. Three
separate jobs are ordered only by *submission*, so a job reading a table an
earlier job is still writing sees it empty — and produces zero rows rather than
an error.

## 3. Location: get this wrong and nothing else matters

BigQuery federation co-locates by **geography, not by exact location**. This is
the single most useful fact in the whole area, and it is easy to miss:

* A query running in the **US multi-region** may read a Cloud SQL, AlloyDB or
  Spanner instance in **any single region in the US geography** — `us-central1`,
  `us-east4`, `us-west2`.
* The **connection resource** must be in the same location the query runs in. A
  query from the US multi-region needs a connection in `US`.
* Proximity still affects speed. US multi-region to `us-central1` is fast; to
  `us-east4` is slower.

So a design pinned to the US multi-region on the BigQuery side — which is forced
if you read `bigquery-public-data`, since it lives there — puts the operational
engines in a single US region and every BigQuery object in `US`. Mixing a
`us-central1` BigQuery dataset with a `US` one is not a performance question: they
are different locations and cannot be joined at all.

## 4. The unified view, and the trap underneath it

```sql
CREATE VIEW unified_orders AS
SELECT order_item_id, ..., 'BigQuery' AS origin
FROM   ecommerce_lab.orders
UNION ALL
SELECT order_item_id, ..., 'AlloyDB' AS origin
FROM   EXTERNAL_QUERY('project.us.alloydb_conn', '''SELECT ... FROM orders''')
UNION ALL
SELECT order_item_id, ..., 'Spanner' AS origin
FROM   spanner_federated.CustomerOrders;
```

`UNION ALL` matches by **position**, not by name. List columns explicitly in the
same order in every arm.

Two columns, not one, and they answer different questions:

* **`origin`** — which engine served this row. A property of the federation,
  added by the view.
* **`written_by`** — who put the row there (`reverse-etl-seed`, `agent-insert`).
  A property of the row, stored in the table.

Collapsing them into one column makes it impossible to find a newly written row
among thousands of seeded rows that came from the same engine.

### The node-key trap

If you then declare a property graph over these views, **do not union the node
sources**. The same customers (all of Lab 10's seed, ~1,000) exist in all three engines, so a
`UNION ALL` of customer views repeats every `customer_id` three times. BigQuery
Graph does not reject a duplicated node `KEY` — it returns three times as many
paths, and nine times as many on a two-hop traversal.

* **Node views: deduplicate.** Take entities from one canonical source.
* **Edge views: union, with a synthetic key** that includes the origin:
  `CONCAT(origin, '#', order_item_id) AS edge_id`.
* **Count distinct ids, not paths.** Every seeded order item is an edge once
  per engine, so each `PURCHASED` hop matches three times and `COUNT(*)` over
  matched paths inflates multiplicatively. Lab 10's next-best-product query uses
  `COUNT(DISTINCT ...)` and `SELECT DISTINCT`, which is why it runs unchanged on
  `UnifiedEcommerceGraph` (Lab 11 Step 10 shows both versions side by side).

The failure mode is plausible wrong numbers, never an error. Assert it:

```sql
SELECT COUNT(*) FROM (
  SELECT customer_id FROM unified_customers GROUP BY customer_id HAVING COUNT(*) > 1
);  -- must be 0
```

## 5. Verifying a zero-ETL setup

Check **row counts**, not the absence of exceptions. A federated view over a
failed export returns zero rows quite happily, and a property graph over empty
tables answers successfully with nothing in it.

1. Count the same table in all three engines and confirm the numbers agree —
   counting the operational engines *through* BigQuery proves the export and the
   federation in one query.
2. Confirm `SELECT COUNT(DISTINCT origin)` on the unified view returns 3.
3. Confirm node views have no duplicate keys.
4. Write a row directly in the operational engine, re-run the BigQuery count, and
   confirm it moved. That is the only test that proves the read path is live
   rather than a stale copy.

## 6. When NOT to do this

Reverse ETL earns its place when an operational system needs rows the warehouse
already computed. It is the wrong tool when:

* **The operational engine is the system of record.** Then data should flow the
  other way, and federation alone is enough — no export at all.
* **You need sub-second propagation of warehouse changes.** `EXPORT DATA` is a
  batch statement; re-running it is a job, not a stream. Use change streams or
  Datastream when latency is a requirement rather than a nicety.
* **The transformation is complex and stateful.** A pipeline tool exists for a
  reason. `EXPORT DATA` is `SELECT` plus a destination; if the `SELECT` is
  becoming a system, it is a system.
