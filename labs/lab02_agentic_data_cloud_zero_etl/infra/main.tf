# Author: Wissem Khlifi - August 2026
# ==============================================================================
# Lab 11: Agentic Data Cloud — Zero-ETL (Decoupled)
#
# Lab 11 DEPENDS on Lab 10 and ADDS three things to it:
#   1. Two operational engines (AlloyDB, Spanner) populated by BigQuery
#      EXPORT DATA — reverse ETL, no GCS staging, no CSV, no psql, no pipeline.
#   2. Federation back into BigQuery (EXTERNAL_QUERY for AlloyDB, an external
#      dataset for Spanner) and a unified view carrying an `origin` column.
#   3. Three service accounts whose capabilities differ, so "this agent cannot
#      write" is an IAM fact rather than a prompt.
#
# It creates NOTHING that Lab 10 already owns: no reservation, no reservation
# assignment, no Data Catalog taxonomy, no policy tag, no glossary, no data
# product. A duplicate of any of those either 409s or silently fights Lab 10.
# ==============================================================================

terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 7.0"
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.0"
    }
    time = {
      source  = "hashicorp/time"
      version = ">= 0.9"
    }
    null = {
      source  = "hashicorp/null"
      version = ">= 3.0"
    }
  }
}

#--- 0. Locals ---
locals {
  # NOTE: there is deliberately no `thelook` local here. Lab 11 does not read
  # bigquery-public-data.thelook_ecommerce at all — it seeds from Lab 10's
  # tables, because that public dataset is REGENERATED and re-deriving the
  # sample independently produced a different one. See the comment on
  # local.sample_script.

  # See the long comment on var.region in variables.tf. These two constants are
  # the whole location story: BigQuery in the US multi-region (because Lab 10 is
  # there, because TheLook is there), both operational engines in a single US
  # region (because BigQuery federation co-locates by GEOGRAPHY, not by exact
  # location). var.region is never referenced in this file.
  bq_location = "US"
  data_region = "us-central1"

  # Connection resource paths spell the multi-region in lower case; BigQuery SQL
  # and job locations spell it upper case. Both appear below; neither is a typo.
  bq_conn_location = lower(local.bq_location)

  alloydb_instance_id = "zeroetl-primary"

  # AlloyDB's default database. Creating a second one buys nothing here and adds
  # a failure mode: CREATE DATABASE cannot run inside a transaction block, and
  # the managed execute_sql path gives no control over how statements are
  # wrapped. The tables live in the public schema of `postgres`.
  alloydb_database = "postgres"

  # The built-in (username/password) role the BigQuery connection authenticates
  # as. It has to be built-in, not IAM: the google-alloydb connector supports
  # username_password auth only.
  alloydb_bq_user = "bq_writer"

  spanner_database = "ecommerce-operational"

  alloydb_instance_uri = "projects/${var.project_id}/locations/${local.data_region}/clusters/${var.alloydb_cluster_id}/instances/${local.alloydb_instance_id}"
}

data "google_project" "project" {
  project_id = var.project_id
}


#--- 1. Enable Required GCP APIs for this Lab ---
# A superset of Lab 10's list. Re-declaring an API Lab 10 also enables is safe:
# google_project_service is idempotent and both modules converge on "enabled".
resource "google_project_service" "enabled_apis" {
  for_each = toset([
    "alloydb.googleapis.com",
    "spanner.googleapis.com",
    "bigquery.googleapis.com",
    "bigqueryconnection.googleapis.com",
    "bigquerydatapolicy.googleapis.com",
    "bigqueryreservation.googleapis.com",
    "servicenetworking.googleapis.com",
    "secretmanager.googleapis.com",
    "dataplex.googleapis.com",
    "datacatalog.googleapis.com",
    "aiplatform.googleapis.com",
    "run.googleapis.com",
    "cloudbuild.googleapis.com",
    "artifactregistry.googleapis.com",
    "iam.googleapis.com",
    "storage.googleapis.com",
    "logging.googleapis.com",
  ])
  project            = var.project_id
  service            = each.key
  disable_on_destroy = false
}

# ==============================================================================
# 2. SPANNER — the operational engine with a native graph
# Ported from labs/02_spanner_disneyland/infra/main.tf, with three changes:
#   config is regional-us-central1, NOT regional-${var.region}
#   the ddl list is populated (Lab 02 left DDL to the guide)
#   names are distinct from Lab 02's so both labs can be in one event
# ==============================================================================

resource "google_spanner_instance" "zeroetl" {
  name         = var.spanner_instance_id
  project      = var.project_id
  config       = "regional-${local.data_region}"
  display_name = "Reverse-ETL Operational Store"
  # ENTERPRISE is not a sizing choice. Spanner Graph refuses to run on STANDARD
  # edition, and the failure arrives at CREATE PROPERTY GRAPH — after the tables
  # exist and after the data has landed.
  edition          = "ENTERPRISE"
  processing_units = var.spanner_processing_units

  depends_on = [google_project_service.enabled_apis]
}

resource "google_spanner_database" "operational" {
  instance = google_spanner_instance.zeroetl.name
  name     = local.spanner_database
  project  = var.project_id

  # Spanner Graph requires GoogleSQL. POSTGRESQL dialect has no PROPERTY GRAPH.
  database_dialect    = "GOOGLE_STANDARD_SQL"
  deletion_protection = false

  # Schema AND graph are declared at database-creation time, before any data
  # lands. A property graph is metadata over tables, so declaring it on empty
  # tables is legal and means the graph is queryable the instant EXPORT DATA
  # finishes — there is no second DDL step to forget.
  ddl = [
    <<-SQL
      CREATE TABLE Customers (
        customer_id    STRING(64) NOT NULL,
        name           STRING(MAX),
        email          STRING(MAX),
        segment        STRING(64),
        lifetime_value FLOAT64,
      ) PRIMARY KEY (customer_id)
    SQL
    ,
    <<-SQL
      CREATE TABLE Products (
        product_id STRING(64) NOT NULL,
        name       STRING(MAX),
        category   STRING(128),
        price      FLOAT64,
      ) PRIMARY KEY (product_id)
    SQL
    ,
    # INTERLEAVE IN PARENT requires the child's primary key to START WITH the
    # parent's, which is why customer_id leads here even though order_item_id is
    # what actually identifies a row. Co-locating a customer's order items with
    # the customer is what makes the operational traversal a single-split read.
    <<-SQL
      CREATE TABLE CustomerOrders (
        customer_id   STRING(64) NOT NULL,
        order_item_id STRING(64) NOT NULL,
        order_id      STRING(64),
        product_id    STRING(64),
        total_amount  FLOAT64,
        status        STRING(32),
        order_date    DATE,
        written_by    STRING(32),
      ) PRIMARY KEY (customer_id, order_item_id),
        INTERLEAVE IN PARENT Customers ON DELETE CASCADE
    SQL
    ,
    "CREATE INDEX CustomerOrdersByProduct ON CustomerOrders(product_id)",
    # CustomerOrders appears three times — once as a node and twice as an edge —
    # so every use needs an explicit AS alias. Without them Spanner rejects the
    # statement with a duplicate-name error that names the table, not the cause.
    #
    # The labels avoid ORDER, INCLUDES and CONTAINS. All three are reserved in a
    # LABEL clause, and the parse error they produce points at the surrounding
    # syntax rather than at the word.
    <<-SQL
      CREATE OR REPLACE PROPERTY GRAPH EcommerceOpsGraph
        NODE TABLES (
          Customers
            KEY (customer_id)
            LABEL Customer,
          Products
            KEY (product_id)
            LABEL Product,
          CustomerOrders AS OrderNode
            KEY (customer_id, order_item_id)
            LABEL CustomerOrder
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
        )
    SQL
  ]
}

# ==============================================================================
# 3. ALLOYDB — the operational engine reached only through managed endpoints
# Ported from labs/01_alloydb_vectors/infra/main.tf.
#
# Two things about this cluster are different from Lab 01 and both are load-
# bearing:
#
#   Location. us-central1, not var.region. The BigQuery side is pinned to the US
#   multi-region by Lab 10, and a europe-west3 instance is outside the US
#   geography, so federation would fail.
#
#   Reachability. Core infra creates the VPC with the provider default
#   routing_mode = REGIONAL and puts the workstation subnet in var.region. A
#   us-central1 AlloyDB private IP is therefore NOT routable from the
#   participant's workstation. This lab never needs it to be: every interaction
#   with AlloyDB — DDL, seeding, the agent, the participant's own queries — goes
#   through the AlloyDB remote MCP server or the BigQuery connection, both
#   of which are Google-managed control planes that do not traverse the VPC.
#
#   The cost of that choice, stated plainly: no psql, no Auth Proxy and no
#   AlloyDB Studio in this lab. Restoring them means either moving AlloyDB to
#   var.region (breaks federation) or setting routing_mode = "GLOBAL" in
#   00-core-infra (a shared file this lab does not touch).
# ==============================================================================

resource "random_password" "alloydb" {
  count = var.enable_alloydb ? 1 : 0

  length  = 24
  special = true
  # The password is interpolated into a JSON connector configuration and read
  # back through shell in init.sh. Restricting the special characters to two
  # shell- and JSON-safe ones removes an entire class of quoting failure without
  # meaningfully weakening a credential that lives for the length of a workshop.
  override_special = "-_"
  min_upper        = 2
  min_lower        = 2
  min_numeric      = 2
}

resource "google_alloydb_cluster" "zeroetl" {
  count = var.enable_alloydb ? 1 : 0

  cluster_id          = var.alloydb_cluster_id
  project             = var.project_id
  location            = local.data_region
  deletion_protection = false

  network_config {
    network = var.vpc_id
  }

  initial_user {
    password = random_password.alloydb[0].result
  }

  depends_on = [google_project_service.enabled_apis]
}

resource "google_alloydb_instance" "primary" {
  count = var.enable_alloydb ? 1 : 0

  cluster       = google_alloydb_cluster.zeroetl[0].name
  instance_id   = local.alloydb_instance_id
  instance_type = "PRIMARY"

  machine_config {
    cpu_count = var.alloydb_instance_cpu_count
  }

  # Only IAM authentication is switched on. Lab 01's vector and AI flags
  # (scann.*, google_ml_integration.*, alloydb_ai_nl.*) are deliberately absent:
  # nothing in this lab embeds or searches vectors, and each flag is a restart
  # this cluster does not need to pay for.
  database_flags = {
    "alloydb.iam_authentication" = "on"
  }

  depends_on = [google_alloydb_cluster.zeroetl]
}

# The built-in role the BigQuery connector authenticates as. It cannot be an IAM
# user: the google-alloydb connector supports username/password only.
resource "google_alloydb_user" "bq_writer" {
  count = var.enable_alloydb ? 1 : 0

  cluster        = google_alloydb_cluster.zeroetl[0].name
  user_id        = local.alloydb_bq_user
  user_type      = "ALLOYDB_BUILT_IN"
  password       = random_password.alloydb[0].result
  database_roles = ["alloydbsuperuser"]

  depends_on = [google_alloydb_instance.primary]
}

# The participant, so `agy` plus the AlloyDB MCP server works as themselves.
#
# This is ALSO the only google_alloydb_user resource for a human, and that is a
# deliberate correction. An earlier version added a second one for whoever runs
# `terraform apply`, resolved through data.google_client_openid_userinfo. It was
# wrong twice over:
#
#   That data source is read during APPLY, so user_id was "(known after apply)"
#   at plan time — and user_id is ForceNew. Every single apply therefore planned
#   a replacement of the user, and the destroy half failed with
#     pq: role "..." cannot be dropped because some objects depend on it
#   because that role owns the tables it had just created.
#
#   When the facilitator and the participant are the same person — which is the
#   normal case for a single-project test — both resources managed the SAME
#   AlloyDB user, and Terraform fought itself over one API object.
#
# The DDL identity is now ensured at RUNTIME by alloydb_execute_sql.sh, which
# knows who is calling and creates the IAM user idempotently. Nothing about it
# needs to be known at plan time.
resource "google_alloydb_user" "participant" {
  count = var.enable_alloydb ? 1 : 0

  cluster        = google_alloydb_cluster.zeroetl[0].name
  user_id        = replace(var.iap_member, "user:", "")
  user_type      = "ALLOYDB_IAM_USER"
  database_roles = ["alloydbsuperuser", "alloydbiamuser"]

  lifecycle {
    ignore_changes = [database_roles]
  }

  depends_on = [google_alloydb_instance.primary]
}

# The participant needs the password to inspect the connection in the console,
# and init.sh reads it to print the connection summary. Secret Manager rather
# than a Terraform output because outputs of a stitched lab module are not
# surfaced anywhere a participant can reach.
resource "google_secret_manager_secret" "alloydb_password" {
  count = var.enable_alloydb ? 1 : 0

  project   = var.project_id
  secret_id = "lab11-alloydb-password"

  replication {
    auto {}
  }

  depends_on = [google_project_service.enabled_apis]
}

resource "google_secret_manager_secret_version" "alloydb_password" {
  count = var.enable_alloydb ? 1 : 0

  secret      = google_secret_manager_secret.alloydb_password[0].id
  secret_data = random_password.alloydb[0].result
}

resource "google_secret_manager_secret_iam_member" "alloydb_password_participant" {
  count = var.enable_alloydb ? 1 : 0

  project   = var.project_id
  secret_id = google_secret_manager_secret.alloydb_password[0].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = var.iap_member
}

#--- 3a. AlloyDB target tables (EXPORT DATA requires them to exist) ---
# EXPORT DATA does not create its destination. Both the Spanner and the AlloyDB
# export refuse to run against a table that is not already there.
#
# On the Spanner side that is solved declaratively, in google_spanner_database.ddl
# above. AlloyDB has no equivalent: the provider offers no SQL-execution
# resource, and the instance is unreachable from this host over the network.
#
# So the DDL goes through the AlloyDB remote MCP server's execute_sql tool (
# global endpoint), which is an ordinary HTTPS call carrying an ADC bearer token.
# The tool's own schema says "Uses IAM user authentication", which is why
# google_alloydb_user.bootstrap above exists.
resource "null_resource" "alloydb_schema" {
  count = var.enable_alloydb ? 1 : 0

  triggers = {
    instance = local.alloydb_instance_uri
    ddl      = local.alloydb_ddl_sha
    # Re-runs the DELETEs at the end of the DDL list when Lab 10 re-seeds, so
    # export_to_alloydb (same trigger) writes into cleared tables.
    seed = var.source_seed_version
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = "bash '${path.module}/scripts/alloydb_execute_sql.sh'"

    environment = {
      PROJECT_ID   = var.project_id
      INSTANCE_URI = local.alloydb_instance_uri
      DATABASE     = local.alloydb_database
      LABEL        = "AlloyDB reverse-ETL target schema"
      # The script registers the CALLING identity as an AlloyDB IAM user with
      # alloydbsuperuser before it runs any DDL: execute_sql authenticates as the
      # caller, and PostgreSQL 15 removed CREATE on the public schema from PUBLIC.
      ALLOYDB_CLUSTER    = var.alloydb_cluster_id
      ALLOYDB_REGION     = local.data_region
      BOOTSTRAP_OVERRIDE = var.alloydb_bootstrap_principal
      # Statements are newline-delimited and sent one at a time: execute_sql
      # takes a single sqlStatement, and a semicolon-joined batch is rejected by
      # some paths with a syntax error that points at the second statement.
      SQL_STATEMENTS = local.alloydb_ddl_statements
    }
  }

  depends_on = [
    google_alloydb_user.participant,
    google_alloydb_user.bq_writer,
  ]
}

#--- 3b. The AlloyDB DDL itself ---
locals {
  # One statement per line. alloydb_execute_sql.sh splits on newlines and sends
  # each through execute_sql separately, so nothing here may contain a newline.
  #
  # DELETE-then-export rather than TRUNCATE: a re-apply must not destroy rows the
  # AGENT wrote during the lab. Only the seeded rows are cleared, so re-running
  # `terraform apply` is idempotent for the seed and non-destructive for the
  # participant's own work.
  alloydb_ddl_list = [
    "CREATE TABLE IF NOT EXISTS customers (customer_id TEXT PRIMARY KEY, name TEXT, email TEXT, segment TEXT, lifetime_value DOUBLE PRECISION)",
    "CREATE TABLE IF NOT EXISTS products (product_id TEXT PRIMARY KEY, name TEXT, category TEXT, price DOUBLE PRECISION)",
    "CREATE TABLE IF NOT EXISTS orders (order_item_id TEXT PRIMARY KEY, customer_id TEXT, order_id TEXT, product_id TEXT, total_amount DOUBLE PRECISION, status TEXT, order_date DATE, written_by TEXT DEFAULT 'agent-insert')",
    "CREATE INDEX IF NOT EXISTS orders_customer_idx ON orders (customer_id)",
    "CREATE INDEX IF NOT EXISTS orders_product_idx ON orders (product_id)",
    # The connector authenticates as bq_writer; it must be able to write these
    # tables even though the bootstrap principal created them.
    "GRANT USAGE ON SCHEMA public TO ${local.alloydb_bq_user}",
    "GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO ${local.alloydb_bq_user}",
    "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO ${local.alloydb_bq_user}",
    "DELETE FROM orders WHERE written_by = 'reverse-etl-seed'",
    "DELETE FROM customers",
    "DELETE FROM products",
  ]

  alloydb_ddl_statements = join("\n", local.alloydb_ddl_list)
  alloydb_ddl_sha        = sha256(local.alloydb_ddl_statements)
}

# ==============================================================================
# 4. BIGQUERY CONNECTIONS — the two doors data goes out through and comes back in
# ==============================================================================

# AlloyDB. Verified present in hashicorp/google 8.0.0 as the generic
# `configuration` block with connector_id = "google-alloydb"; the older
# `cloud_sql` block cannot express it. Confirmed by reading
# `terraform providers schema -json` rather than by assuming.
resource "google_bigquery_connection" "alloydb_conn" {
  count = var.enable_alloydb ? 1 : 0

  connection_id = "alloydb_conn"
  project       = var.project_id
  location      = local.bq_location
  friendly_name = "AlloyDB export + federation connection"
  description   = "Carries EXPORT DATA out to AlloyDB and EXTERNAL_QUERY back in"

  configuration {
    connector_id = "google-alloydb"

    asset {
      database              = local.alloydb_database
      google_cloud_resource = "//alloydb.googleapis.com/${local.alloydb_instance_uri}"
    }

    # username_password, not service_account: EXPORT DATA to AlloyDB requires it,
    # and the connector offers no IAM path for the write direction.
    authentication {
      username_password {
        username = local.alloydb_bq_user
        password {
          plaintext = random_password.alloydb[0].result
        }
      }
    }
  }

  depends_on = [
    google_project_service.enabled_apis,
    google_alloydb_user.bq_writer,
  ]
}

# Mitigates the GCP IAM registration delay for the generated connection service agent.
resource "time_sleep" "wait_for_alloydb_conn_sa" {
  count = var.enable_alloydb ? 1 : 0

  create_duration = "30s"
  depends_on      = [google_bigquery_connection.alloydb_conn]
}

# The connection gets its own generated service account, and that account — not
# the caller — is what actually opens the AlloyDB socket. Without alloydb.client
# every EXTERNAL_QUERY fails with a permission error naming a principal the
# participant has never seen.
resource "google_project_iam_member" "alloydb_conn_client" {
  count = var.enable_alloydb ? 1 : 0

  project = var.project_id
  role    = "roles/alloydb.client"
  member  = "serviceAccount:${google_bigquery_connection.alloydb_conn[0].configuration[0].authentication[0].service_account}"

  depends_on = [time_sleep.wait_for_alloydb_conn_sa]
}

# The Spanner connection has had an IAM barrier since Lab 02; the AlloyDB one did
# not, and on a clean project that omission is the difference between a working
# export and an empty table.
#
# Observed on the first clean-project apply: the grant above completed, the
# export started immediately, and ten seconds later BigQuery refused it with
#   Permission 'alloydb.instances.get' denied on .../instances/zeroetl-primary
# even though roles/alloydb.client carries exactly that permission and the
# service account already held it. The grant simply had not propagated.
#
# It looked correct on the FIRST project only because the export happened to run
# on a later apply, after IAM had settled — an accident of sequencing, not a
# working design.
resource "time_sleep" "wait_for_alloydb_conn_iam" {
  count = var.enable_alloydb ? 1 : 0

  create_duration = "60s"
  depends_on      = [google_project_iam_member.alloydb_conn_client]
}

# Spanner. Ported from Lab 02, relocated to the US multi-region.
resource "google_bigquery_connection" "spanner_conn" {
  connection_id = "spanner_conn_us"
  project       = var.project_id
  location      = local.bq_location
  friendly_name = "Spanner federation connection"

  cloud_resource {}

  depends_on = [google_project_service.enabled_apis]
}

# Mitigates the GCP registration delay Lab 02 documents.
resource "time_sleep" "wait_for_connection_sa" {
  create_duration = "15s"
  depends_on      = [google_bigquery_connection.spanner_conn]
}

# Lab 02 grants this with google_project_iam_binding and roles/spanner.admin.
# Do NOT port that. google_project_iam_binding is AUTHORITATIVE: it replaces the
# full member list for the role, so if Lab 02 and Lab 11 are both selected for an
# event the two modules delete each other's member on alternate applies. And
# spanner.admin is far more than a read connection needs.
# databaseReaderWithDataBoost, not plain databaseReader. A BigQuery external
# dataset reads Spanner through DATA BOOST, which needs
# spanner.databases.useDataBoost — a permission roles/spanner.databaseReader
# does NOT carry. With only databaseReader every federated read fails with
#   Error accessing Cloud Spanner. Permission Denied.
# naming a generated table alias and no principal, which sends you looking at
# the query rather than at a missing role on a service account you never created.
# Lab 02 never hit this because it granted roles/spanner.admin.
resource "google_project_iam_member" "spanner_conn_reader" {
  project = var.project_id
  role    = "roles/spanner.databaseReaderWithDataBoost"
  member  = "serviceAccount:${google_bigquery_connection.spanner_conn.cloud_resource[0].service_account_id}"

  depends_on = [time_sleep.wait_for_connection_sa]
}

resource "time_sleep" "wait_for_connection_iam" {
  create_duration = "60s"
  depends_on = [
    google_project_iam_member.spanner_conn_reader,
    google_spanner_database.operational,
  ]
}

# ==============================================================================
# 5. REVERSE ETL — BigQuery EXPORT DATA into both operational engines
#
# This is the lab's thesis executed as infrastructure: the same rows land in
# three engines and nothing built a pipeline to put them there.
#
# The sample is read from Lab 10's seeded tables (see the locals below). The
# engine orders this module after Lab 10 (`depends_on` on module.lab_<dep>) and
# passes Lab 10's seed_version in as var.source_seed_version, so a Lab 10
# re-seed clears and re-exports the operational copies on the same apply.
# ==============================================================================

locals {
  # Lab 11 seeds the operational engines from LAB 10'S TABLES, not from TheLook.
  #
  # An earlier version read `bigquery-public-data.thelook_ecommerce` directly and
  # re-derived Lab 10's chained sample, on the reasoning that this removed the
  # cross-module ordering dependency. It removed the dependency and broke the lab:
  # TheLook is REGENERATED. Seeded two days apart, Lab 10's copy and Lab 11's
  # shared only 13 of 500 products. The unified view then unioned three sets that
  # barely overlapped, and two thirds of the graph edges pointed at customer and
  # product ids that were not in the node views — which a property graph does not
  # report as an error, it simply returns fewer paths.
  #
  # "The same rows live in three engines" is the entire thesis of this lab, so the
  # source has to be the one copy that already exists. The ordering problem this
  # reintroduces is real but already solved: bq_run.sh retries on
  # "not found: Table" for the same reason it retries on the missing reservation,
  # and both come from Lab 10.
  #
  # email is deliberately NOT copied. It carries Lab 10's SENSITIVE policy tag, so
  # reading it requires a fine-grained-reader grant the DEPLOYING identity has no
  # reason to hold — in a real event the facilitator is not the participant, and
  # the export would fail on that one column. Nor should it: the governed
  # warehouse is where the PII lives, and the operational copies do not need it.
  sample_script = <<-SQL
    CREATE TEMP TABLE sample_products AS
    SELECT product_id, name, category, price
    FROM `${var.project_id}.${var.bq_source_dataset_id}.products`;

    CREATE TEMP TABLE sample_customers AS
    SELECT
      customer_id,
      name,
      CAST(NULL AS STRING) AS email,
      segment,
      lifetime_value
    FROM `${var.project_id}.${var.bq_source_dataset_id}.customers`;

    CREATE TEMP TABLE sample_orders AS
    SELECT
      o.customer_id,
      o.order_item_id,
      o.order_id,
      o.product_id,
      o.total_amount,
      o.status,
      o.order_date,
      'reverse-etl-seed' AS written_by
    FROM `${var.project_id}.${var.bq_source_dataset_id}.orders` o
    JOIN sample_customers sc ON sc.customer_id = o.customer_id
    JOIN sample_products sp ON sp.product_id = o.product_id
    WHERE o.product_id IS NOT NULL;
  SQL

  # NOTE ON THE URI. EXPORT DATA wants the API endpoint form,
  # https://spanner.googleapis.com/projects/... -- NOT the google-cloudspanner:/
  # form. That second one is the EXTERNAL DATASET syntax used further down in
  # section 6, and it is a different feature. Passing it here is rejected with
  # "uri must start with a valid prefix", which names neither the option nor the
  # alternative. Confirmed against a live BigQuery dry run.
  #
  # EVERY id is CAST to STRING, in all three engines. Spanner uses STRING(64)
  # keys, AlloyDB TEXT, and Lab 10's BigQuery tables already carry STRING casts
  # of TheLook's INT64 ids. A single INT64 anywhere breaks the UNION ALL in
  # unified_orders with a type-mismatch error that names the view, not the cast.
  export_spanner_script = <<-SQL
    ${local.sample_script}

    EXPORT DATA OPTIONS (
      uri             = 'https://spanner.googleapis.com/projects/${var.project_id}/instances/${var.spanner_instance_id}/databases/${local.spanner_database}',
      format          = 'CLOUD_SPANNER',
      spanner_options = '{"table": "Products"}'
    ) AS
    SELECT product_id, name, category, price FROM sample_products;

    EXPORT DATA OPTIONS (
      uri             = 'https://spanner.googleapis.com/projects/${var.project_id}/instances/${var.spanner_instance_id}/databases/${local.spanner_database}',
      format          = 'CLOUD_SPANNER',
      spanner_options = '{"table": "Customers"}'
    ) AS
    SELECT customer_id, name, email, segment, lifetime_value FROM sample_customers;

    EXPORT DATA OPTIONS (
      uri             = 'https://spanner.googleapis.com/projects/${var.project_id}/instances/${var.spanner_instance_id}/databases/${local.spanner_database}',
      format          = 'CLOUD_SPANNER',
      spanner_options = '{"table": "CustomerOrders"}'
    ) AS
    SELECT customer_id, order_item_id, order_id, product_id,
           total_amount, status, order_date, written_by
    FROM sample_orders;
  SQL

  # EXPORT DATA to AlloyDB is pre-GA (check release notes), and its syntax differs from the Spanner
  # one in three ways that are each rejected with a different, unhelpful error.
  # All three were established against a live BigQuery dry run:
  #
  #   format is 'ALLOYDB', NOT 'CLOUD_ALLOYDB'
  #     -> "'CLOUD_ALLOYDB' is not a valid value; failed to set 'format'"
  #   uri is the AlloyDB API endpoint naming the INSTANCE, not the connection
  #     -> "URI: ... is invalid for AlloyDB. Correct format must be:
  #         'https://alloydb.googleapis.com/projects/.../instances/instance_id'"
  #   the connection is a WITH CONNECTION CLAUSE, not an option
  #     -> "CONNECTION clause is required for AlloyDB export"
  #        (and connection_id as an option is "Unknown option")
  #
  # The connection is what carries the username/password credential; the uri only
  # says which instance to write to.
  export_alloydb_script = <<-SQL
    ${local.sample_script}

    EXPORT DATA
    WITH CONNECTION `${var.project_id}.${local.bq_conn_location}.alloydb_conn`
    OPTIONS (
      uri             = 'https://alloydb.googleapis.com/${local.alloydb_instance_uri}',
      format          = 'ALLOYDB',
      alloydb_options = '{"table": "products"}'
    ) AS
    SELECT product_id, name, category, price FROM sample_products;

    EXPORT DATA
    WITH CONNECTION `${var.project_id}.${local.bq_conn_location}.alloydb_conn`
    OPTIONS (
      uri             = 'https://alloydb.googleapis.com/${local.alloydb_instance_uri}',
      format          = 'ALLOYDB',
      alloydb_options = '{"table": "customers"}'
    ) AS
    SELECT customer_id, name, email, segment, lifetime_value FROM sample_customers;

    EXPORT DATA
    WITH CONNECTION `${var.project_id}.${local.bq_conn_location}.alloydb_conn`
    OPTIONS (
      uri             = 'https://alloydb.googleapis.com/${local.alloydb_instance_uri}',
      format          = 'ALLOYDB',
      alloydb_options = '{"table": "orders"}'
    ) AS
    SELECT order_item_id, customer_id, order_id, product_id,
           total_amount, status, order_date, written_by
    FROM sample_orders;
  SQL
}

# Why null_resource and not google_bigquery_job, for both exports and for the
# property graph below. Two independent reasons, both observed rather than
# theorised:
#
#   1. The ENTERPRISE reservation belongs to LAB 10. EXPORT DATA to Spanner
#      requires one, and only one project-wide QUERY assignment may exist. The
#      engine now orders Lab 11 after Lab 10, but Lab 10's own comments record
#      that the assignment takes a minute or two to propagate after it is
#      created. google_bigquery_job cannot wait and cannot retry; bq_run.sh
#      does both, matching on the reservation error specifically.
#
#   2. google_bigquery_job is "created" the moment a job is SUBMITTED, not when
#      it finishes — the failure Lab 10 documents at its section 3b, where
#      downstream readers 404'd on tables the job had not written yet.
#      `bq query --sync` blocks until the script completes.
#
# Spanner gets the same treatment AlloyDB gets in alloydb_schema: clear the
# previous seed before exporting a new one. EXPORT DATA upserts, so without
# this a re-seeded Lab 10 leaves the old seed's rows in Spanner beside the new
# ones, and the unified view unions sets that barely overlap. Agent-written
# rows are kept, and so are the Customers and Products they reference
# (CustomerOrders is interleaved in Customers ON DELETE CASCADE).
#
# Normal DML, not partitioned: the subqueries are not allowed in partitioned
# DML, and ~7.4k seed rows are far below the per-transaction mutation limit.
resource "null_resource" "spanner_clear_seed" {
  triggers = {
    database = google_spanner_database.operational.id
    seed     = var.source_seed_version
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-BASH
      set -euo pipefail
      echo "--- [lab11] Clearing previous reverse-ETL seed rows from Spanner"
      for stmt in \
        "DELETE FROM CustomerOrders WHERE written_by = 'reverse-etl-seed'" \
        "DELETE FROM Customers WHERE customer_id NOT IN (SELECT customer_id FROM CustomerOrders)" \
        "DELETE FROM Products WHERE product_id NOT IN (SELECT product_id FROM CustomerOrders WHERE product_id IS NOT NULL)"; do
        gcloud spanner databases execute-sql "$${SPANNER_DATABASE}" \
          --instance="$${SPANNER_INSTANCE}" --project="$${PROJECT_ID}" \
          --sql="$${stmt}" --quiet
      done
    BASH

    environment = {
      PROJECT_ID       = var.project_id
      SPANNER_INSTANCE = var.spanner_instance_id
      SPANNER_DATABASE = local.spanner_database
    }
  }

  depends_on = [google_spanner_database.operational]
}

resource "null_resource" "export_to_spanner" {
  triggers = {
    script = sha256(local.export_spanner_script)
    # Lab 10 re-seeded: re-export over the cleared tables.
    seed = var.source_seed_version
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = "bash '${path.module}/scripts/bq_run.sh'"

    environment = {
      PROJECT_ID  = var.project_id
      BQ_LOCATION = local.bq_location
      LABEL       = "EXPORT DATA -> Spanner"
      SQL         = local.export_spanner_script
      # Waits on TWO things Lab 10 owns: the seeded tables and the ENTERPRISE
      # reservation. 20 x 30s covers Lab 10's ~8 minute seed plus propagation.
      MAX_ATTEMPTS = "20"
    }
  }

  depends_on = [
    google_spanner_database.operational,
    null_resource.spanner_clear_seed,
    time_sleep.wait_for_connection_iam,
  ]
}

resource "null_resource" "export_to_alloydb" {
  count = var.enable_alloydb ? 1 : 0

  triggers = {
    script = sha256(local.export_alloydb_script)
    # Tracking the barrier's id, not just the script, is deliberate: it means a
    # project whose export failed on the permission race re-runs it on the next
    # apply, rather than keeping an empty table because the SQL never changed.
    iam = time_sleep.wait_for_alloydb_conn_iam[0].id
    # Lab 10 re-seeded: alloydb_schema has cleared the old seed; re-export.
    seed = var.source_seed_version
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = "bash '${path.module}/scripts/bq_run.sh'"

    environment = {
      PROJECT_ID   = var.project_id
      BQ_LOCATION  = local.bq_location
      LABEL        = "EXPORT DATA -> AlloyDB"
      SQL          = local.export_alloydb_script
      MAX_ATTEMPTS = "20"
      # Still tolerated. The SYNTAX is now confirmed against a live dry run, but
      # the feature can change under us between events, and one step must not be
      # able to take a whole event build down. The Spanner half of the lab is
      # unaffected either way, and verify.sh reports it.
      ALLOW_FAILURE = "1"
    }
  }

  depends_on = [
    null_resource.alloydb_schema,
    time_sleep.wait_for_alloydb_conn_iam,
  ]
}

# The exports are synchronous, but Spanner needs a moment to make freshly
# committed rows visible to a federated reader, and the views below read both
# engines immediately.
resource "time_sleep" "wait_for_exports" {
  create_duration = "60s"

  depends_on = [
    null_resource.export_to_spanner,
    null_resource.export_to_alloydb,
  ]
}

# ==============================================================================
# 6. FEDERATION — both engines readable from BigQuery, in one query
#
# Data went OUT with EXPORT DATA. Queries come BACK with EXTERNAL_QUERY (AlloyDB)
# and an external dataset (Spanner). Nothing is copied in this direction: the
# unified view below reads live rows from three engines every time it runs, which
# is why an INSERT in Spanner shows up in BigQuery on the next query.
# ==============================================================================

# Ported from Lab 02 section 5, relocated to the US multi-region. The bq CLI is
# unreliable for external datasets; the provider's external_dataset_reference is
# the supported path and is what Lab 02 already uses.
resource "google_bigquery_dataset" "spanner_federated" {
  dataset_id    = "spanner_federated"
  friendly_name = "Spanner (federated)"
  description   = "Zero-copy view of the Spanner operational database"
  location      = local.bq_location
  project       = var.project_id

  external_dataset_reference {
    external_source = "google-cloudspanner:/projects/${var.project_id}/instances/${var.spanner_instance_id}/databases/${local.spanner_database}"
    connection      = google_bigquery_connection.spanner_conn.id
  }

  depends_on = [time_sleep.wait_for_connection_iam]
}

resource "google_bigquery_dataset" "zeroetl" {
  dataset_id    = var.bq_zeroetl_dataset_id
  friendly_name = "Zero-ETL Unified Layer"
  description   = "Unified views across BigQuery, AlloyDB and Spanner, and the property graph declared over them"
  location      = local.bq_location
  project       = var.project_id

  # The property graph declared in here is created by SQL DDL and is invisible to
  # Terraform state — the provider has no property-graph resource type. Without
  # this, destroy deletes the managed views and then fails on the dataset because
  # something it cannot see is still inside. Same reasoning as Lab 10 section 4.
  delete_contents_on_destroy = true

  depends_on = [google_project_service.enabled_apis]
}

locals {
  bq_orders = "`${var.project_id}.${var.bq_source_dataset_id}.orders`"

  # The AlloyDB arm is conditional: with enable_alloydb = false the lab still
  # runs, and unified_orders becomes a two-engine union rather than failing to
  # compile against a connection that does not exist.
  alloydb_orders_arm_sql = <<-SQL
    UNION ALL
    SELECT
      order_item_id, order_id, customer_id, product_id,
      total_amount, status, order_date, written_by,
      'AlloyDB' AS origin
    FROM EXTERNAL_QUERY(
      '${var.project_id}.${local.bq_conn_location}.alloydb_conn',
      '''SELECT order_item_id, order_id, customer_id, product_id,
                total_amount, status, order_date, written_by
         FROM orders'''
    )
  SQL

  alloydb_orders_arm = var.enable_alloydb ? local.alloydb_orders_arm_sql : ""

  # UNION ALL matches by POSITION, not by name. Every arm below lists the same
  # nine columns in the same order and casts nothing implicitly, because a
  # mismatch surfaces as a type error naming the view rather than the arm.
  unified_orders_sql = <<-SQL
    SELECT
      order_item_id, order_id, customer_id, product_id,
      total_amount, status, order_date,
      'reverse-etl-seed' AS written_by,
      'BigQuery' AS origin
    FROM ${local.bq_orders}
    ${local.alloydb_orders_arm}
    UNION ALL
    SELECT
      order_item_id, order_id, customer_id, product_id,
      total_amount, status, order_date, written_by,
      'Spanner' AS origin
    FROM `${var.project_id}.${google_bigquery_dataset.spanner_federated.dataset_id}.CustomerOrders`
  SQL
}

resource "google_bigquery_table" "unified_orders" {
  dataset_id          = google_bigquery_dataset.zeroetl.dataset_id
  table_id            = "unified_orders"
  project             = var.project_id
  deletion_protection = false

  view {
    query          = local.unified_orders_sql
    use_legacy_sql = false
  }

  depends_on = [time_sleep.wait_for_exports]
}

# ------------------------------------------------------------------------------
# The node views are DEDUPED, and this is the single most important line in the
# federation layer.
#
# A property graph's node KEY must be unique in its source. The same customers
# (every customer of Lab 10's seed) exist in all three engines, so `SELECT ... UNION ALL` across them
# would put every customer_id in the node view three times. BigQuery Graph does
# NOT reject that. It silently returns three times as many paths, and nine times
# as many on a two-hop traversal — the same class of failure Lab 10 documents for
# order_id versus order_item_id, arriving as plausible wrong numbers rather than
# as an error.
#
# Entities are therefore taken from ONE canonical source (Lab 10's BigQuery
# tables). Only the EDGE view unions all three engines, because an order item
# genuinely is a different fact in each engine — and it gets a synthetic key that
# includes the origin so the union cannot collide either.
# ------------------------------------------------------------------------------
resource "google_bigquery_table" "unified_customers" {
  dataset_id          = google_bigquery_dataset.zeroetl.dataset_id
  table_id            = "unified_customers"
  project             = var.project_id
  deletion_protection = false

  view {
    query          = "SELECT customer_id, name, segment, lifetime_value FROM `${var.project_id}.${var.bq_source_dataset_id}.customers`"
    use_legacy_sql = false
  }

  depends_on = [
    google_bigquery_dataset.zeroetl,
    time_sleep.wait_for_exports,
  ]
}

resource "google_bigquery_table" "unified_products" {
  dataset_id          = google_bigquery_dataset.zeroetl.dataset_id
  table_id            = "unified_products"
  project             = var.project_id
  deletion_protection = false

  view {
    query          = "SELECT product_id, name, category, price FROM `${var.project_id}.${var.bq_source_dataset_id}.products`"
    use_legacy_sql = false
  }

  depends_on = [
    google_bigquery_dataset.zeroetl,
    time_sleep.wait_for_exports,
  ]
}

# Category nodes and IN_CATEGORY edges, so Lab 10's next-best-product query runs
# on this graph by swapping only the graph name. Same shape as Lab 10's
# graph_categories / graph_product_category, from the same canonical source:
# the node view is DISTINCT (one category, one node), and the edge view is one
# row per product, keyed by product_id.
resource "google_bigquery_table" "unified_categories" {
  dataset_id          = google_bigquery_dataset.zeroetl.dataset_id
  table_id            = "unified_categories"
  project             = var.project_id
  deletion_protection = false

  view {
    query          = "SELECT DISTINCT category AS category_id FROM `${var.project_id}.${var.bq_source_dataset_id}.products` WHERE category IS NOT NULL"
    use_legacy_sql = false
  }

  depends_on = [
    google_bigquery_dataset.zeroetl,
    time_sleep.wait_for_exports,
  ]
}

resource "google_bigquery_table" "unified_product_category" {
  dataset_id          = google_bigquery_dataset.zeroetl.dataset_id
  table_id            = "unified_product_category"
  project             = var.project_id
  deletion_protection = false

  view {
    query          = "SELECT product_id, category AS category_id FROM `${var.project_id}.${var.bq_source_dataset_id}.products` WHERE category IS NOT NULL"
    use_legacy_sql = false
  }

  depends_on = [
    google_bigquery_dataset.zeroetl,
    time_sleep.wait_for_exports,
  ]
}

resource "google_bigquery_table" "unified_order_edges" {
  dataset_id          = google_bigquery_dataset.zeroetl.dataset_id
  table_id            = "unified_order_edges"
  project             = var.project_id
  deletion_protection = false

  view {
    # edge_id, not order_item_id: the same order item exists in three engines, so
    # order_item_id alone is not unique across the union and would multiply
    # traversal results by three.
    query          = <<-SQL
      SELECT
        CONCAT(origin, '#', order_item_id) AS edge_id,
        customer_id,
        product_id,
        total_amount,
        status,
        order_date,
        written_by,
        origin
      FROM `${var.project_id}.${google_bigquery_dataset.zeroetl.dataset_id}.unified_orders`
      WHERE product_id IS NOT NULL
    SQL
    use_legacy_sql = false
  }

  depends_on = [google_bigquery_table.unified_orders]
}

# The unified property graph DDL. Kept in sync with declare_unified_sql() in
# src/lab-runner/actions.py (the console's recovery button). Node and edge
# sources must be DATASET-QUALIFIED: a bare `unified_customers` is rejected with
# "Table ... must be qualified with a dataset", even though the graph name itself
# is fully qualified and every source lives in that same dataset. The AS alias is
# what the SOURCE/DESTINATION ... REFERENCES clauses match on.
locals {
  unified_graph_ddl = <<-SQL
    CREATE OR REPLACE PROPERTY GRAPH `${var.project_id}.${var.bq_zeroetl_dataset_id}.UnifiedEcommerceGraph`
      NODE TABLES (
        `${var.project_id}.${var.bq_zeroetl_dataset_id}.unified_customers` AS unified_customers
          KEY (customer_id)
          LABEL Customer PROPERTIES ALL COLUMNS,
        `${var.project_id}.${var.bq_zeroetl_dataset_id}.unified_products` AS unified_products
          KEY (product_id)
          LABEL Product PROPERTIES ALL COLUMNS,
        `${var.project_id}.${var.bq_zeroetl_dataset_id}.unified_categories` AS unified_categories
          KEY (category_id)
          LABEL Category PROPERTIES ALL COLUMNS
      )
      EDGE TABLES (
        `${var.project_id}.${var.bq_zeroetl_dataset_id}.unified_order_edges` AS unified_order_edges
          KEY (edge_id)
          SOURCE      KEY (customer_id) REFERENCES unified_customers (customer_id)
          DESTINATION KEY (product_id)  REFERENCES unified_products  (product_id)
          LABEL PURCHASED
          PROPERTIES (total_amount, status, order_date, written_by, origin),
        `${var.project_id}.${var.bq_zeroetl_dataset_id}.unified_product_category` AS unified_product_category
          KEY (product_id)
          SOURCE      KEY (product_id)  REFERENCES unified_products   (product_id)
          DESTINATION KEY (category_id) REFERENCES unified_categories (category_id)
          LABEL IN_CATEGORY
      );
  SQL
}

# The unified property graph. Terraform has no resource type for one, so it is
# SQL DDL run through the same retrying wrapper as the exports.
#
# Failure here is tolerated on purpose. This graph is declared over FEDERATED
# views, so it depends on
# two connections and two remote engines being reachable at apply time. An event
# build must not die because one of them moved. verify.sh reports it, the lab runner
# offers a button to declare it, and the guide says so.
resource "null_resource" "unified_graph" {
  triggers = {
    edges = google_bigquery_table.unified_order_edges.id
    nodes = "${google_bigquery_table.unified_customers.id},${google_bigquery_table.unified_products.id},${google_bigquery_table.unified_categories.id}"
    # Also re-declare when the DDL itself changes (e.g. Category was added).
    ddl = sha256(local.unified_graph_ddl)
    # Re-declare after a reseed too. Without this the graph keeps whatever
    # definition it had when the views were first created, and a failed earlier
    # attempt is never retried.
    seed = sha256(local.export_spanner_script)
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = "bash '${path.module}/scripts/bq_run.sh'"

    environment = {
      PROJECT_ID    = var.project_id
      BQ_LOCATION   = local.bq_location
      LABEL         = "CREATE PROPERTY GRAPH UnifiedEcommerceGraph"
      ALLOW_FAILURE = "1"
      SQL           = local.unified_graph_ddl
    }
  }

  depends_on = [
    google_bigquery_table.unified_order_edges,
    google_bigquery_table.unified_customers,
    google_bigquery_table.unified_products,
    google_bigquery_table.unified_categories,
    google_bigquery_table.unified_product_category,
  ]
}

# ==============================================================================
# 7. IDENTITIES — three service accounts, and what each one structurally cannot do
#
# Lab 10 proved a guardrail can live in IAM rather than in a prompt, using two
# identities and one policy tag. Lab 11 extends the same argument to WRITES, and
# it is the reason the agent is split across two Cloud Run services rather than
# one: ADK runs sub-agents in-process, and a single process has exactly one
# service account. Two capabilities that must differ therefore need two services.
#
#   lab11-dispatch-sa   reads BigQuery and Spanner. Cannot write anything.
#   lab11-transact-sa   writes AlloyDB and Spanner. Cannot read the warehouse.
#   lab11-runner-sa     reads everything a participant may read, writes nothing
#                       except the property graph it is allowed to declare.
#
# No instruction, prompt or code change moves a capability between them.
# ==============================================================================

resource "google_service_account" "dispatch" {
  project      = var.project_id
  account_id   = "lab11-dispatch-sa"
  display_name = "Lab 11 Dispatch Agent (router + analyst + graph)"
  description  = "Read-only identity: classifies, queries and traverses. Holds no write role on any engine."

  depends_on = [google_project_service.enabled_apis]
}

resource "google_service_account" "transact" {
  project      = var.project_id
  account_id   = "lab11-transact-sa"
  display_name = "Lab 11 Transactional Agent (AlloyDB + Spanner)"
  description  = "Write-capable identity for the two operational engines. Holds no BigQuery data role."

  depends_on = [google_project_service.enabled_apis]
}

resource "google_service_account" "runner" {
  count = var.lab_runner_enabled ? 1 : 0

  project      = var.project_id
  account_id   = "lab11-runner-sa"
  display_name = "Lab 11 Lab Runner (stands in for the participant)"

  depends_on = [google_project_service.enabled_apis]
}

resource "google_project_iam_member" "dispatch_roles" {
  for_each = toset([
    "roles/bigquery.jobUser",
    "roles/bigquery.dataViewer",
    # EXTERNAL_QUERY is refused without it, and the error names the connection
    # rather than the missing role.
    "roles/bigquery.connectionUser",
    # Read-only Spanner, for the operational graph traversal. NOT databaseUser:
    # that carries DML, which is the one thing this identity must not have.
    "roles/spanner.databaseReader",
    "roles/dataplex.viewer",
    "roles/dataplex.catalogViewer",
    "roles/aiplatform.user",
    # Reaching a tool THROUGH an MCP server is a separate permission from being
    # allowed to read the data behind it — see Lab 10 section 12.
    "roles/mcp.toolUser",
  ])
  project = var.project_id
  role    = each.key
  member  = "serviceAccount:${google_service_account.dispatch.email}"
}

resource "google_project_iam_member" "transact_roles" {
  for_each = toset([
    "roles/alloydb.client",
    "roles/alloydb.databaseUser",
    # The AlloyDB MCP server checks this before it checks anything else.
    "roles/serviceusage.serviceUsageConsumer",
    # databaseUser, not databaseReader: this identity exists to write.
    "roles/spanner.databaseUser",
    "roles/aiplatform.user",
    "roles/mcp.toolUser",
  ])
  project = var.project_id
  role    = each.key
  member  = "serviceAccount:${google_service_account.transact.email}"
}

# Deliberately absent from the list above: every BigQuery data role. Ask this
# agent for revenue and it fails at the platform, not at the prompt — which is
# the demonstration in Step 12 of the guide.

resource "google_project_iam_member" "runner_roles" {
  for_each = var.lab_runner_enabled ? toset([
    "roles/bigquery.jobUser",
    "roles/bigquery.dataViewer",
    "roles/bigquery.connectionUser",
    "roles/spanner.databaseReader",
    "roles/dataplex.editor",
    "roles/dataplex.catalogViewer",
    "roles/mcp.toolUser",
    "roles/aiplatform.user",
    "roles/run.admin",
  ]) : toset([])

  project = var.project_id
  role    = each.key
  member  = "serviceAccount:${google_service_account.runner[0].email}"
}

# Declaring a property graph needs bigquery.propertyGraphs.create, which
# dataViewer does not carry. Granted on the zero-ETL dataset ALONE so the runner
# can re-declare the unified graph from a button when the DDL in section
# 6 did not take. It must not be granted on Lab 10's ecommerce_lab: that is the
# dataset the masking demonstration reads, and write access there would muddy the
# identity story. Same split, and same reasoning, as Lab 10 section 4a.
resource "google_bigquery_dataset_iam_member" "runner_graph_editor" {
  count = var.lab_runner_enabled ? 1 : 0

  project    = var.project_id
  dataset_id = google_bigquery_dataset.zeroetl.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.runner[0].email}"
}

# The transactional agent as an AlloyDB IAM user. Without this it authenticates
# to the MCP server, is allowed to call the tool, and is then refused by
# PostgreSQL itself — a failure two layers deeper than where it looks.
resource "google_alloydb_user" "transact_agent" {
  count = var.enable_alloydb ? 1 : 0

  cluster = google_alloydb_cluster.zeroetl[0].name
  # The email WITHOUT ".gserviceaccount.com" -- see local.alloydb_agent_pg_role.
  user_id        = local.alloydb_agent_pg_role
  user_type      = "ALLOYDB_IAM_USER"
  database_roles = ["alloydbiamuser"]

  lifecycle {
    ignore_changes = [database_roles]
  }

  depends_on = [google_alloydb_instance.primary]
}

# An AlloyDB IAM service-account user's PostgreSQL role is its email with the
# ".gserviceaccount.com" suffix removed, so it needs quoting. It is granted DML
# but NOT DDL: the agent inserts orders, it does not reshape the schema.
resource "null_resource" "alloydb_agent_grants" {
  count = var.enable_alloydb ? 1 : 0

  triggers = {
    agent = google_service_account.transact.email
    ddl   = local.alloydb_ddl_sha
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = "bash '${path.module}/scripts/alloydb_execute_sql.sh'"

    environment = {
      PROJECT_ID         = var.project_id
      INSTANCE_URI       = local.alloydb_instance_uri
      DATABASE           = local.alloydb_database
      LABEL              = "AlloyDB grants for the transactional agent"
      ALLOYDB_CLUSTER    = var.alloydb_cluster_id
      ALLOYDB_REGION     = local.data_region
      BOOTSTRAP_OVERRIDE = var.alloydb_bootstrap_principal
      SQL_STATEMENTS = join("\n", [
        "GRANT USAGE ON SCHEMA public TO \"${local.alloydb_agent_pg_role}\"",
        "GRANT SELECT, INSERT, UPDATE ON orders TO \"${local.alloydb_agent_pg_role}\"",
        "GRANT SELECT ON customers TO \"${local.alloydb_agent_pg_role}\"",
        "GRANT SELECT ON products TO \"${local.alloydb_agent_pg_role}\"",
      ])
    }
  }

  depends_on = [
    null_resource.alloydb_schema,
    google_alloydb_user.transact_agent,
  ]
}

locals {
  # The service-account email WITHOUT ".gserviceaccount.com". Used in two places
  # that MUST agree: the user_id google_alloydb_user.transact_agent registers,
  # and the target of every GRANT.
  #
  # Two different names are in play and getting them backwards is silent:
  #
  #   what you REGISTER  decides the PostgreSQL role name that gets created
  #   what AlloyDB uses  at IAM login is ALWAYS the truncated email
  #
  # Register the full email and AlloyDB creates a role called
  # "...iam.gserviceaccount.com" that nothing ever authenticates as. The agent
  # then connects, AlloyDB resolves it to "...iam", and PostgreSQL answers
  #   role "lab11-transact-sa@<project>.iam" does not exist
  # naming a role you never created, while the one you DID create sits in
  # pg_roles looking perfectly correct. Granting to the full email "fixes" the
  # GRANT and changes nothing at login -- which is the wrong end, and is the
  # mistake this comment exists to stop being repeated.
  #
  # Verified on a live instance: registering the truncated form created exactly
  # the role the connection was looking for.
  alloydb_agent_pg_role = trimsuffix(google_service_account.transact.email, ".gserviceaccount.com")
}

# ==============================================================================
# 8. DEPLOYMENT — registry and act-as permissions
# ==============================================================================

resource "google_artifact_registry_repository" "lab11" {
  project       = var.project_id
  location      = var.cloud_run_region
  repository_id = var.ar_repo_id
  format        = "DOCKER"
  description   = "Container images for the Lab 11 dispatch agent, transactional agent and lab runner"

  depends_on = [google_project_service.enabled_apis]
}

# The dispatch agent is the ONLY caller allowed to invoke the transactional
# agent. This is what makes "the write path is reachable only through the router"
# a network fact rather than a convention: nothing else in the project, including
# the participant's own curl, holds run.invoker on that service.
resource "google_project_iam_member" "dispatch_invoker" {
  project = var.project_id
  role    = "roles/run.invoker"
  member  = "serviceAccount:${google_service_account.dispatch.email}"
}

resource "google_project_iam_member" "runner_invoker" {
  count = var.lab_runner_enabled ? 1 : 0

  project = var.project_id
  role    = "roles/run.invoker"
  member  = "serviceAccount:${google_service_account.runner[0].email}"
}

# Cloud Run refuses `serviceAccountName` unless the deployer may act as that
# account; without these, deploy.sh fails with an opaque
# iam.serviceAccounts.actAs error. Lab 10 section 14, extended to three accounts.
resource "google_service_account_iam_member" "participant_acts_as_dispatch" {
  service_account_id = google_service_account.dispatch.name
  role               = "roles/iam.serviceAccountUser"
  member             = var.iap_member
}

resource "google_service_account_iam_member" "participant_acts_as_transact" {
  service_account_id = google_service_account.transact.name
  role               = "roles/iam.serviceAccountUser"
  member             = var.iap_member
}

resource "google_service_account_iam_member" "participant_acts_as_runner" {
  count = var.lab_runner_enabled ? 1 : 0

  service_account_id = google_service_account.runner[0].name
  role               = "roles/iam.serviceAccountUser"
  member             = var.iap_member
}

#--- 9. Participant IAM: NOT here ---
# Participant PROJECT roles live in lab.yaml `antigravity.iam_roles` and are
# granted by the engine (tests/test_iam_conventions.py enforces it). Two
# google_project_iam_member resources for the same (role, member) pair are two
# owners of one grant: destroying either revokes it for both. The resource that
# used to live here is FORGOTTEN, not destroyed, so existing grants survive.
#
# Build identities (Cloud Build agent, compute default SA -> AR writer) are
# granted by Lab 10's module, which this lab depends on; not repeated here.
removed {
  from = google_project_iam_member.participant_roles
  lifecycle {
    destroy = false
  }
}

#--- 10. Column-level security grants: NOT here ---
# Lab 11's three service accounts should inherit Lab 10's masking split, so that
# the dispatch agent reads customers.email masked while the lab runner reads it
# raw. Those two grants are deliberately absent from this file, for two reasons
# that cannot be worked around inside Terraform:
#
#   No data source. The grant needs the policy tag's full resource name, which
#   embeds a NUMERIC taxonomy id generated at Lab 10's apply. The Google provider
#   ships no data source for Data Catalog taxonomies or policy tags, so there is
#   nothing to look it up with.
#
#   Historical: the data policy id, "sensitive_email_mask", IS a static string,
#   and the engine now orders Lab 11 after Lab 10 (depends_on module.lab_<dep>),
#   so that grant could move here. It stays in init.sh alongside the policy-tag
#   grant, which still has no data source to look its id up with.
#
# init.sh performs both grants instead: it runs on the workstation, after
# everything is applied, with the project id and the taxonomy both discoverable
# through gcloud. verify.sh asserts the result rather than trusting it.
