---
name: alloydb-transactional
description: Reaching AlloyDB from an agent through the remote MCP server at alloydb.googleapis.com/mcp instead of a client library or psql. Covers the 17-tool surface and which two of them write, execute_sql versus execute_sql_read_only as a structural guardrail, IAM database users, running DDL when the instance is unreachable over the network, and the BigQuery google-alloydb connector for EXPORT DATA and EXTERNAL_QUERY. Use when an agent must read or write AlloyDB, or when AlloyDB has to exchange data with BigQuery.
---
# AlloyDB for agents: the managed MCP server

## 1. Do not write a database client

The reflex is `psycopg`, a connection string and a secret. For an agent that is
the wrong shape, and often an impossible one: an AlloyDB instance is
private-IP-only by default, so anything that opens a socket needs to be inside
the VPC, in the right region, with the Auth Proxy running. A Cloud Run service,
a Cloud Build step and a Terraform host are frequently none of those.

The **AlloyDB remote MCP server** is a Google-managed HTTPS endpoint that reaches
the instance through Google's own control plane:

| | |
|---|---|
| **Global endpoint** | `https://alloydb.googleapis.com/mcp` |
| **Regional endpoints** | `https://alloydb.REGION.rep.googleapis.com/mcp` |
| **Transport** | Streamable HTTP (a reply may arrive as SSE `data:` lines) |
| **Auth** | OAuth 2.0 bearer token from ADC; connections use IAM and mTLS |
| **Needs** | `roles/alloydb.client`, `roles/serviceusage.serviceUsageConsumer`, and an AlloyDB IAM **database** user |

Prefer the global endpoint unless you have a specific reason for a regional one.
The endpoints can differ in release status; check the AlloyDB release notes.

## 2. The tool surface, and the two that write

The server exposes 17 tools. Exactly two can change data:

* **`execute_sql`** — any statement, including DDL and DML. **Writes.**
* **`import_data`** — loads from Cloud Storage. **Writes.**

Everything else reads or manages: `execute_sql_read_only`, `list_clusters`,
`get_cluster`, `list_instances`, `get_instance`, `update_instance`,
`create_instance`, `create_cluster`, `restore_cluster`, `create_backup`,
`create_user`, `get_user`, `list_users`, `export_data`, `get_operation`.

**This is where the guardrail goes.** An agent that only answers questions gets
`tool_filter=["execute_sql_read_only"]` and is then incapable of writing —
not discouraged from it, incapable, because the write tool is never advertised to
the model. An agent that must write gets both, and its *IAM* is what bounds the
damage. Never hand an agent the full 17: `create_cluster` and `create_user` are
not tools a data agent has any business holding.

```python
McpToolset(
    connection_params=StreamableHTTPConnectionParams(
        url="https://alloydb.googleapis.com/mcp", timeout=60.0),
    header_provider=lambda ctx=None: {
        "Authorization": f"Bearer {fresh_adc_token()}",
        "x-goog-user-project": PROJECT_ID,
    },
    tool_filter=["execute_sql", "execute_sql_read_only"],
)
```

Refresh the token **per request**. A token minted once at import expires within
the hour and every later call fails with a 401 that looks like a permissions bug.

## 3. execute_sql arguments, and the authentication detail that catches people

```json
{
  "name": "execute_sql",
  "arguments": {
    "instance": "projects/P/locations/us-central1/clusters/C/instances/I",
    "database": "postgres",
    "sqlStatement": "INSERT INTO orders (...) VALUES (...)"
  }
}
```

`instance` is the **full resource name** — not the instance id. Put it verbatim
into the agent's instruction; a model that assembles the string itself gets it
subtly wrong and the error says the instance does not exist, which sends you to
Terraform for a problem that is in a prompt.

**The tool authenticates as the CALLING IAM principal** — its own schema says
"Uses IAM user authentication". So:

* The caller must exist as an AlloyDB IAM database user
  (`google_alloydb_user` with `user_type = "ALLOYDB_IAM_USER"`).
* A service-account IAM user's PostgreSQL role is its email **with
  `.gserviceaccount.com` stripped**: `my-sa@my-project.iam`. It contains `@` and
  `.`, so it must be double-quoted in SQL: `GRANT ... TO "my-sa@my-project.iam"`.
* To create tables it needs `alloydbsuperuser`. PostgreSQL 15 removed `CREATE` on
  the `public` schema from `PUBLIC`, so an ordinary IAM user connects fine and is
  then refused "permission denied for schema public".

Send **one statement per call**. A semicolon-joined batch is not reliably
accepted, and when it fails the error points at the second statement.

## 4. Creating schema when nothing can reach the instance

This is the situation that makes the MCP server more than a convenience.
`EXPORT DATA ... format='ALLOYDB'` requires the destination table to exist,
but the machine running your provisioning has no route to a private IP. Three
obvious answers are all closed: the Terraform provider has no SQL-execution
resource; `psql` and the Auth Proxy need network reach; a deployment host is not
on the VPC.

The MCP server is an ordinary HTTPS call, so a provisioning script can issue DDL
with `curl` and a bearer token. Build the JSON-RPC body with a real JSON
serialiser — DDL contains quotes, and hand-concatenated JSON breaks on the first
`GRANT` to a quoted role name.

Make the DDL re-runnable: `CREATE TABLE IF NOT EXISTS`, and clear only the rows
your seed owns (`DELETE FROM orders WHERE written_by = 'reverse-etl-seed'`)
rather than truncating, so a re-apply does not destroy rows an agent wrote.

## 5. The BigQuery connector, both directions

One connection serves the export path and the federation path:

```bash
bq mk --connection --location=US --project_id=PROJECT \
  --connector_configuration '{
    "connector_id": "google-alloydb",
    "asset": {
      "database": "postgres",
      "google_cloud_resource": "//alloydb.googleapis.com/projects/P/locations/R/clusters/C/instances/I"
    },
    "authentication": {
      "username_password": {"username": "bq_writer", "password": {"plaintext": "..."}}
    }
  }' alloydb_conn
```

In Terraform (`hashicorp/google` 8.x) this is the generic `configuration` block
on `google_bigquery_connection` — **not** the older `cloud_sql` block, which
cannot express it.

Three things to get right:

* **Username/password, not IAM.** The connector supports only username/password,
  so the write path needs a built-in database user
  (`user_type = "ALLOYDB_BUILT_IN"`). This is the one place in an
  otherwise-IAM-everywhere design where a password is unavoidable; put it in
  Secret Manager and never hand it to an agent.
* **The connection has its own service account** (`authentication.service_account`,
  computed) and *it* opens the socket, not the caller. Grant it
  `roles/alloydb.client` or every `EXTERNAL_QUERY` fails naming a principal
  nobody recognises.
* **Callers need `roles/bigquery.connectionUser`.** Without it, `EXTERNAL_QUERY`
  is refused with an error about the connection rather than about the role.

Writing out through the same connection — note that the connection is a
**clause**, the format is `ALLOYDB` (not `CLOUD_ALLOYDB`), and the uri names the
**instance**, not the connection:

```sql
EXPORT DATA
WITH CONNECTION `PROJECT.us.alloydb_conn`
OPTIONS (
  uri = 'https://alloydb.googleapis.com/projects/P/locations/R/clusters/C/instances/I',
  format = 'ALLOYDB',
  alloydb_options = '{"table": "orders"}'
) AS SELECT ...;
```

Reading back:

```sql
SELECT * FROM EXTERNAL_QUERY('project.us.alloydb_conn',
                             '''SELECT customer_id, status FROM orders''');
```

The inner query is **PostgreSQL**, pushed down and executed by AlloyDB. Triple
quotes keep the single quotes inside it intact.

## 6. Instructing an agent that writes

* Give it the instance URI and database name verbatim.
* Make it verify foreign keys with the read-only tool **before** inserting, and
  stop rather than invent a missing id.
* Have it generate obviously synthetic keys (`'agent-' + timestamp`), never small
  integers that might already exist.
* Have it read the row back after writing and show it.
* Forbid DDL explicitly, and back that with grants: `SELECT, INSERT, UPDATE` on
  the one table it needs, `SELECT` elsewhere. The instruction states the rule;
  the grant enforces it.
