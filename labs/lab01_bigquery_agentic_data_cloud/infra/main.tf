# Author: Wissem Khlifi - August 2026
# ==============================================================================
# Lab 10 BigQuery Agentic Data Cloud Setup (Decoupled)
# ==============================================================================

terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = ">= 5.0"
    }
    null = {
      source  = "hashicorp/null"
      version = ">= 3.0"
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.0"
    }
    time = {
      source  = "hashicorp/time"
      version = ">= 0.9"
    }
  }
}

#--- 0. Locals ---
locals {
  # Source of the seed data for this lab.
  thelook = "bigquery-public-data.thelook_ecommerce"

  # NOTE: `thelook_ecommerce` lives in the US multi-region. BigQuery cannot read
  # across locations, so a CTAS job reading it MUST run in - and write to - a US
  # dataset. Everything this lab creates in BigQuery and Data Catalog is therefore
  # pinned here rather than to var.region, which this module no longer uses.
  bq_location = "US"

  # A policy tag can only be applied to a column whose table lives in the same
  # location as its taxonomy, so the taxonomy follows the datasets. Data Catalog
  # spells the multi-region in lower case ("us"), BigQuery in upper case ("US").
  taxonomy_region = lower(local.bq_location)

  # Taxonomy display names must be unique per project AND location, and a deleted
  # taxonomy keeps its name reserved in an index that `taxonomies.list` no longer
  # returns. Creating one then fails with "409 ALREADY_EXISTS" naming a taxonomy
  # that cannot be listed, read or imported — nothing to adopt, nothing to delete.
  # Qualifying the name with the project id makes it unique by construction, so a
  # fresh project can never inherit that dead end.
  #
  # The separator is a dash, NOT parentheses: a taxonomy display name may contain
  # only Unicode letters, numbers, underscores, dashes and spaces. "PII
  # Classification (my-project)" is rejected at apply with a bare
  # "400 INVALID_ARGUMENT: Request contains an invalid argument".
  # Length is not a concern — the cap is 200 bytes and this is at most 51
  # ("PII Classification - " plus a project id, which maxes out at 30 characters).
  taxonomy_display_name = "PII Classification - ${var.project_id}"

  # Dataplex glossaries and data products are regional. us-central1 sits inside
  # the US multi-region holding the datasets, and search_entries resolves terms
  # from projects/<id>/locations/global regardless of where they were created.
  glossary_location = "us-central1"
}

# Retrieve project number dynamically
data "google_project" "project" {
  project_id = var.project_id
}

#--- 1. Enable Required GCP APIs for this Lab ---
resource "google_project_service" "enabled_apis" {
  for_each = toset([
    "bigquery.googleapis.com",
    "bigquerydatapolicy.googleapis.com",
    "dataplex.googleapis.com",
    "datacatalog.googleapis.com",
    "datalineage.googleapis.com",
    "aiplatform.googleapis.com",
    "logging.googleapis.com",
    "run.googleapis.com",
    "cloudbuild.googleapis.com",
    "storage.googleapis.com",
    "iam.googleapis.com",
    "bigqueryreservation.googleapis.com",
    "artifactregistry.googleapis.com"
  ])
  project            = var.project_id
  service            = each.key
  disable_on_destroy = false
}

#--- 2. BigQuery Dataset: Ecommerce Lab Data ---
resource "google_bigquery_dataset" "ecommerce_lab" {
  dataset_id    = var.bq_dataset_id
  friendly_name = "Ecommerce Lab Data"
  description   = "Source ecommerce tables for the agentic data cloud lab"
  location      = local.bq_location
  project       = var.project_id

  # The three tables below are created by BigQuery jobs, not by Terraform
  # resources, so Terraform cannot delete them on destroy. Without this the
  # dataset delete fails because the dataset is not empty.
  delete_contents_on_destroy = true

  depends_on = [google_project_service.enabled_apis]
}

#--- 3. Lab Tables Seeded from the TheLook Ecommerce Public Dataset ---
# Each table is materialised once by a CREATE OR REPLACE TABLE ... AS SELECT job.
# Materialising (rather than defining views over the public dataset) matters for
# two reasons: LIMIT without ORDER BY is non-deterministic, so a view would return
# a different sample on every execution; and column-level policy tags can only be
# attached to real table columns, never to a view.

# The three samples are CHAINED so that referential integrity holds: products are
# picked first, customers are picked from the buyers of those products, and orders
# are then restricted to the kept customers AND the kept products. Sampling the
# three tables independently would leave almost every order pointing at a customer
# or product that was not kept, and the graph queries would return nothing.
locals {
  # 1. The 500 most-ordered products.
  ctas_products = <<-SQL
    CREATE OR REPLACE TABLE `${var.project_id}.${var.bq_dataset_id}.products` AS
    WITH top_products AS (
      SELECT product_id, COUNT(*) AS order_count
      FROM `${local.thelook}.order_items`
      GROUP BY product_id
      ORDER BY order_count DESC, product_id
      LIMIT 500
    )
    SELECT
      CAST(p.id AS STRING) AS product_id,
      p.name,
      p.category,
      p.retail_price       AS price,
      p.department         AS description
    FROM `${local.thelook}.products` p
    JOIN top_products tp
      ON tp.product_id = p.id
  SQL

  # 2. The 1,000 most active buyers OF THOSE PRODUCTS. Ranking by order_count
  #    keeps every repeat buyer; 1,000 (not more) keeps each synthetic persona
  #    pool dense enough that almost every customer has lookalike peers.
  #
  #    Unlike the other two, this one is TRUNCATE + INSERT rather than CREATE OR
  #    REPLACE TABLE: the table is a Terraform resource (see below) because its
  #    email column carries a policy tag, and CREATE OR REPLACE would drop the
  #    table and take the tag with it on every re-seed. TRUNCATE preserves the
  #    schema, and re-running the job is still idempotent.
  dml_customers = <<-SQL
    TRUNCATE TABLE `${var.project_id}.${var.bq_dataset_id}.customers`;

    INSERT INTO `${var.project_id}.${var.bq_dataset_id}.customers`
      (customer_id, name, email, segment, lifetime_value, created_at)
    WITH kept_products AS (
      SELECT CAST(product_id AS INT64) AS product_id
      FROM `${var.project_id}.${var.bq_dataset_id}.products`
    ),
    top_users AS (
      SELECT oi.user_id AS user_id, COUNT(*) AS order_count
      FROM `${local.thelook}.order_items` oi
      JOIN kept_products kp
        ON kp.product_id = oi.product_id
      GROUP BY user_id
      ORDER BY order_count DESC, user_id
      LIMIT 1000
    )
    SELECT
      CAST(u.id AS STRING)               AS customer_id,
      u.first_name || ' ' || u.last_name AS name,
      u.email,
      u.country                          AS segment,
      CAST(NULL AS FLOAT64)              AS lifetime_value, -- backfilled below
      u.created_at
    FROM `${local.thelook}.users` u
    JOIN top_users tu
      ON tu.user_id = u.id
  SQL

  # 3. Order rows: REAL order items of the kept customers and products, plus a
  #    SYNTHETIC co-purchase signal.
  #
  #    Why synthetic: TheLook assigns products at random, so the real sample has
  #    ~1 product per customer and almost no two customers share two products.
  #    Step 9's "lookalike peers" graph would be empty. Each customer is given a
  #    persona (1 of 8, by fingerprint of their id); each persona is a contiguous
  #    slice of the 500 products sorted by category, i.e. a coherent taste. Every
  #    customer then "buys" 6 products from their persona pool. The guide says
  #    openly that this signal is synthetic; its rows are marked by the `syn-`
  #    prefix on order_item_id and order_id.
  #
  #    The synthetic half is deterministic: every choice is a FARM_FINGERPRINT
  #    of stable ids and the dates hang off a fixed anchor. The real half follows
  #    TheLook, which Google regenerates daily, so absolute counts can drift
  #    slightly between deploy days (~7.4k rows); the guide therefore quotes
  #    shapes, and the console computes every number live. No LIMIT: the result
  #    is bounded by construction.
  #
  #    The grain is one ORDER ITEM, not one order: a basket with several items
  #    shares a single order_id. order_item_id is the unique row id, so the graph
  #    edge table in section 5 has a valid KEY.
  ctas_orders = <<-SQL
    CREATE OR REPLACE TABLE `${var.project_id}.${var.bq_dataset_id}.orders` AS
    WITH real_orders AS (
      SELECT
        CAST(oi.id AS STRING)         AS order_item_id,
        CAST(oi.order_id AS STRING)   AS order_id,
        CAST(oi.user_id AS STRING)    AS customer_id,
        CAST(oi.product_id AS STRING) AS product_id,
        oi.sale_price                 AS total_amount,
        oi.status,
        DATE(oi.created_at)           AS order_date
      FROM `${local.thelook}.order_items` oi
      JOIN `${var.project_id}.${var.bq_dataset_id}.customers` c
        ON c.customer_id = CAST(oi.user_id AS STRING)
      JOIN `${var.project_id}.${var.bq_dataset_id}.products` p
        ON p.product_id = CAST(oi.product_id AS STRING)
    ),
    pools AS (
      SELECT product_id, price,
             NTILE(8) OVER (ORDER BY category, product_id) AS persona
      FROM `${var.project_id}.${var.bq_dataset_id}.products`
    ),
    personas AS (
      SELECT customer_id,
             1 + MOD(ABS(FARM_FINGERPRINT(CONCAT('persona:', customer_id))), 8) AS persona
      FROM `${var.project_id}.${var.bq_dataset_id}.customers`
    ),
    picks AS (
      SELECT c.customer_id, p.product_id, p.price,
             ROW_NUMBER() OVER (
               PARTITION BY c.customer_id
               ORDER BY FARM_FINGERPRINT(CONCAT(c.customer_id, ':', p.product_id))
             ) AS n
      FROM personas c
      JOIN pools p USING (persona)
    ),
    synthetic AS (
      SELECT
        CONCAT('syn-', customer_id, '-', CAST(n AS STRING))              AS order_item_id,
        CONCAT('syn-', customer_id, '-b', CAST(DIV(n - 1, 3) AS STRING)) AS order_id,
        customer_id,
        product_id,
        price AS total_amount,
        -- Status mix mirrors TheLook (~30% Shipped, 25% Complete, 20% Processing,
        -- 15% Cancelled, 10% Returned). Step 7's lesson depends on it: naive
        -- SUM(total_amount) is ~4x the governed Complete-only revenue. A
        -- Complete-heavy mix measured 2.0x and quietly broke that claim.
        CASE MOD(ABS(FARM_FINGERPRINT(CONCAT('status:', customer_id, ':', CAST(n AS STRING)))), 20)
          WHEN 0 THEN 'Cancelled'  WHEN 1 THEN 'Cancelled'  WHEN 2 THEN 'Cancelled'
          WHEN 3 THEN 'Returned'   WHEN 4 THEN 'Returned'
          WHEN 5 THEN 'Processing' WHEN 6 THEN 'Processing' WHEN 7 THEN 'Processing' WHEN 8 THEN 'Processing'
          WHEN 9 THEN 'Complete'   WHEN 10 THEN 'Complete'  WHEN 11 THEN 'Complete'
          WHEN 12 THEN 'Complete'  WHEN 13 THEN 'Complete'
          ELSE 'Shipped'
        END AS status,
        DATE_SUB(DATE '2026-09-01',
                 INTERVAL MOD(ABS(FARM_FINGERPRINT(CONCAT('date:', customer_id, ':', CAST(n AS STRING)))), 540) DAY) AS order_date
      FROM picks
      WHERE n <= 6
    )
    SELECT * FROM real_orders
    UNION ALL
    SELECT * FROM synthetic
  SQL

  # 4. Backfill lifetime_value from the orders table once it exists. Deriving it
  #    from `orders` rather than recomputing it from the public dataset is what
  #    guarantees it reconciles with what participants query: SUM(lifetime_value)
  #    equals the governed Revenue figure exactly. Customers with no completed
  #    order get 0.0 rather than NULL, so downstream aggregates need no COALESCE.
  dml_customer_ltv = <<-SQL
    UPDATE `${var.project_id}.${var.bq_dataset_id}.customers` c
    SET lifetime_value = (
      SELECT COALESCE(SUM(o.total_amount), 0)
      FROM `${var.project_id}.${var.bq_dataset_id}.orders` o
      WHERE o.customer_id = c.customer_id
        AND o.status = 'Complete'
    )
    WHERE TRUE
  SQL

  # 5. The four statements above are executed as ONE multi-statement script.
  #    This is not cosmetic. google_bigquery_job returns as soon as the job is
  #    SUBMITTED, not when it finishes, so depends_on between four separate job
  #    resources orders their submission and nothing else. Terraform fired them
  #    ~1.4s apart, `orders` began while the `customers` script was still between
  #    its TRUNCATE and its INSERT, and the join read an empty table: orders came
  #    out with 0 rows and the lifetime_value backfill then failed outright with
  #    "Table ecommerce_lab.orders was not found".
  #
  #    Statements inside a single BigQuery script run sequentially and each one
  #    commits before the next begins, so the ordering is guaranteed by BigQuery
  #    rather than by Terraform's graph.
  seed_script = join(";\n\n", [
    local.ctas_products,
    local.dml_customers,
    local.ctas_orders,
    local.dml_customer_ltv,
  ])
}

# BigQuery job IDs are unique and immutable per project: re-running a job with an
# ID that already exists fails. Keying a random suffix on the script text gives a
# fresh ID whenever the SQL changes, and a stable one when it does not.
resource "random_id" "seed" {
  byte_length = 4
  keepers = {
    query = local.seed_script
  }
}

#--- 3a. Terraform-managed `customers` table (column-level security) ---
# The SENSITIVE policy tag is attached to `email` here, in the schema Terraform
# owns. This is why the customers table is a resource while orders and products
# are left to their CTAS jobs: a policy tag has to live on a column of a table
# whose schema something is accountable for.
resource "google_bigquery_table" "customers" {
  dataset_id          = google_bigquery_dataset.ecommerce_lab.dataset_id
  table_id            = "customers"
  project             = var.project_id
  deletion_protection = false

  schema = jsonencode([
    { name = "customer_id", type = "STRING", mode = "REQUIRED" },
    { name = "name", type = "STRING", mode = "NULLABLE" },
    {
      name = "email",
      type = "STRING",
      mode = "NULLABLE",
      policyTags = {
        names = [google_data_catalog_policy_tag.sensitive.name]
      }
    },
    { name = "segment", type = "STRING", mode = "NULLABLE" },
    { name = "lifetime_value", type = "FLOAT64", mode = "NULLABLE" },
    { name = "created_at", type = "TIMESTAMP", mode = "NULLABLE" }
  ])
}

resource "google_bigquery_job" "seed" {
  project  = var.project_id
  job_id   = "lab10-seed-${random_id.seed.hex}"
  location = local.bq_location

  query {
    query          = local.seed_script
    use_legacy_sql = false

    # The destinations are set by the DDL/DML itself; leaving the dispositions
    # empty keeps the provider from adding a conflicting destination table.
    create_disposition = ""
    write_disposition  = ""
  }

  # The script INSERTs into the Terraform-managed customers table, so that table
  # has to exist first. Table resources, unlike job resources, really are
  # complete when Terraform says they are.
  depends_on = [
    google_bigquery_dataset.ecommerce_lab,
    google_bigquery_table.customers,
  ]
}

#--- 3b. Barrier: wait for the seed script to actually finish ---
# google_bigquery_job is created the moment the job is SUBMITTED, not when it
# finishes. depends_on therefore orders submission and nothing more.
# Instead of an arbitrary fixed sleep that can expire before the 4-statement
# seed script completes in BigQuery (causing 404s on downstream readers),
# this barrier actively polls until both `products` and `orders` exist.
resource "null_resource" "wait_for_seed" {
  depends_on = [google_bigquery_job.seed]

  triggers = {
    seed_job = random_id.seed.hex
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-BASH
      set -uo pipefail
      echo "--- [lab10] Waiting for seed tables (products, orders) in ${var.bq_dataset_id}..."
      for i in $(seq 1 60); do
        if bq show --project_id="${var.project_id}" "${var.bq_dataset_id}.products" >/dev/null 2>&1 && \
           bq show --project_id="${var.project_id}" "${var.bq_dataset_id}.orders" >/dev/null 2>&1; then
          echo "  ✓ Seed tables (products, orders) are ready!"
          exit 0
        fi
        echo "  ⏳ [attempt $i/60] waiting for BigQuery seed job to finish creating tables... (sleeping 10s)"
        sleep 10
      done
      echo "  ✗ Timed out waiting for seed tables after 600s!"
      exit 1
    BASH
  }
}

#--- 4. BigQuery Dataset: Graph Analytics ---
resource "google_bigquery_dataset" "ecommerce_graph" {
  dataset_id    = var.bq_graph_dataset_id
  friendly_name = "Ecommerce Graph Data"
  description   = "Node and edge views backing the BigQuery property graph"
  location      = local.bq_location
  project       = var.project_id

  # Step 8 declares a PROPERTY GRAPH in here, and Terraform never learns about
  # it: the Google provider has no property-graph resource type, so the object
  # is created by SQL DDL and is invisible to state. Without this, `terraform
  # destroy` deletes the three managed views, then fails on the dataset because
  # something it cannot see is still inside -- leaving the lab half torn down.
  delete_contents_on_destroy = true

  depends_on = [google_project_service.enabled_apis]
}

#--- 4a. Graph-dataset write access for the lab runner ---
# Step 8's "Declare the graph and run both traversals" issues CREATE OR REPLACE
# PROPERTY GRAPH, which needs bigquery.propertyGraphs.create. The lab runner
# holds bigquery.dataViewer project-wide -- read-only, and deliberately so --
# and that role carries only propertyGraphs.get and .list, so the button failed:
#
#   403 Permission bigquery.propertyGraphs.create denied on dataset
#       <project>:ecommerce_graph_lab
#
# dataEditor is the least-privilege predefined role that includes create, and it
# is granted HERE, on the graph dataset alone. It must NOT be granted on
# ecommerce_lab: that is the dataset the Step 7 masking demonstration reads, and
# giving the lab runner write access there would muddy the identity story that
# step exists to make.
#
# The agent needs no equivalent grant. It only ever TRAVERSES a graph, and
# traversal is covered by propertyGraphs.get/.list, which its dataViewer role
# already carries. Declaring is the app's job; querying is the agent's.
resource "google_bigquery_dataset_iam_member" "lab_runner_graph_editor" {
  project    = var.project_id
  dataset_id = google_bigquery_dataset.ecommerce_graph.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.lab_runner.email}"
}

#--- 5. Graph Node & Edge Views ---
resource "google_bigquery_table" "graph_customers" {
  dataset_id          = google_bigquery_dataset.ecommerce_graph.dataset_id
  table_id            = "graph_customers"
  project             = var.project_id
  deletion_protection = false

  view {
    query          = "SELECT customer_id, name, segment, lifetime_value FROM `${var.project_id}.${google_bigquery_dataset.ecommerce_lab.dataset_id}.customers`"
    use_legacy_sql = false
  }

  depends_on = [null_resource.wait_for_seed]
}

resource "google_bigquery_table" "graph_products" {
  dataset_id          = google_bigquery_dataset.ecommerce_graph.dataset_id
  table_id            = "graph_products"
  project             = var.project_id
  deletion_protection = false

  view {
    query          = "SELECT product_id, name, category, price FROM `${var.project_id}.${google_bigquery_dataset.ecommerce_lab.dataset_id}.products`"
    use_legacy_sql = false
  }

  depends_on = [null_resource.wait_for_seed]
}

resource "google_bigquery_table" "graph_purchases" {
  dataset_id          = google_bigquery_dataset.ecommerce_graph.dataset_id
  table_id            = "graph_purchases"
  project             = var.project_id
  deletion_protection = false

  view {
    # status rides on the PURCHASED edge: Step 9 finds at-risk customers
    # (latest item Returned/Cancelled) from the same edge table it traverses.
    query          = "SELECT order_item_id, order_id, customer_id, product_id, total_amount, status, order_date FROM `${var.project_id}.${google_bigquery_dataset.ecommerce_lab.dataset_id}.orders` WHERE product_id IS NOT NULL"
    use_legacy_sql = false
  }

  depends_on = [null_resource.wait_for_seed]
}

# Category nodes: a DISTINCT view, no table of their own. Teaches that a node
# label needs only a key, not a physical entity table.
resource "google_bigquery_table" "graph_categories" {
  dataset_id          = google_bigquery_dataset.ecommerce_graph.dataset_id
  table_id            = "graph_categories"
  project             = var.project_id
  deletion_protection = false

  view {
    query          = "SELECT DISTINCT category AS category_id FROM `${var.project_id}.${google_bigquery_dataset.ecommerce_lab.dataset_id}.products` WHERE category IS NOT NULL"
    use_legacy_sql = false
  }

  depends_on = [null_resource.wait_for_seed]
}

# IN_CATEGORY edges (Product -> Category). A thin view of its own rather than
# reusing graph_products as the edge table: one view serving as both a node and
# an edge table is not something every BigQuery release accepts.
resource "google_bigquery_table" "graph_product_category" {
  dataset_id          = google_bigquery_dataset.ecommerce_graph.dataset_id
  table_id            = "graph_product_category"
  project             = var.project_id
  deletion_protection = false

  view {
    query          = "SELECT product_id, category AS category_id FROM `${var.project_id}.${google_bigquery_dataset.ecommerce_lab.dataset_id}.products` WHERE category IS NOT NULL"
    use_legacy_sql = false
  }

  depends_on = [null_resource.wait_for_seed]
}

#--- 6. Data Catalog Taxonomy & Policy Tag (Column-Level Security) ---
resource "google_data_catalog_taxonomy" "pii" {
  project                = var.project_id
  region                 = local.taxonomy_region
  display_name           = local.taxonomy_display_name
  description            = "Taxonomy used for column-level security on sensitive ecommerce columns"
  activated_policy_types = ["FINE_GRAINED_ACCESS_CONTROL"]

  depends_on = [google_project_service.enabled_apis]
}

resource "google_data_catalog_policy_tag" "sensitive" {
  taxonomy     = google_data_catalog_taxonomy.pii.id
  display_name = "SENSITIVE"
  description  = "Columns containing personally identifiable information"
}

#--- 7. Fine-Grained Reader on the SENSITIVE Policy Tag ---
resource "google_data_catalog_policy_tag_iam_member" "sensitive_reader" {
  policy_tag = google_data_catalog_policy_tag.sensitive.name
  role       = "roles/datacatalog.categoryFineGrainedReader"
  member     = var.iap_member
}

#--- 8. Participant IAM Roles ---
# Declared ONCE, in lab.yaml `antigravity.iam_roles`, and granted by the engine
# (root module `google_project_iam_member.lab_roles`). This module used to grant
# the same roles again and the two lists drifted. The old resource is forgotten
# rather than destroyed, so running projects keep every binding.
removed {
  from = google_project_iam_member.participant_roles
  lifecycle {
    destroy = false
  }
}

#--- 9. Knowledge Catalog Business Glossary ---
# Verified present in hashicorp/google v7.45.0: google_dataplex_glossary,
# google_dataplex_glossary_term, google_dataplex_glossary_category.
# These terms are what `catalog_lookup` resolves before the agent writes SQL.
resource "google_dataplex_glossary" "ecommerce" {
  project      = var.project_id
  location     = local.glossary_location
  glossary_id  = "ecommerce-business-terms"
  display_name = "Ecommerce Business Terms"
  description  = "Governed business definitions grounding the ecommerce analyst agent"

  depends_on = [google_project_service.enabled_apis]
}

resource "google_dataplex_glossary_term" "revenue" {
  project      = var.project_id
  location     = local.glossary_location
  term_id      = "revenue"
  glossary_id  = google_dataplex_glossary.ecommerce.glossary_id
  parent       = "projects/${var.project_id}/locations/${local.glossary_location}/glossaries/${google_dataplex_glossary.ecommerce.glossary_id}"
  display_name = "Revenue"
  description  = "Total revenue is calculated as SUM(total_amount) from the orders table WHERE status = 'Complete'. Orders in any other state are excluded. The permitted state values and their exact casing are defined by the Order Status term, not here."
}

resource "google_dataplex_glossary_term" "high_value_customer" {
  project      = var.project_id
  location     = local.glossary_location
  term_id      = "high-value-customer"
  glossary_id  = google_dataplex_glossary.ecommerce.glossary_id
  parent       = "projects/${var.project_id}/locations/${local.glossary_location}/glossaries/${google_dataplex_glossary.ecommerce.glossary_id}"
  display_name = "High Value Customer"
  description  = "A customer whose lifetime_value is in the top decile of the customers table. Derive the threshold with APPROX_QUANTILES(lifetime_value, 10)[OFFSET(9)] rather than assuming a constant; it is on the order of tens of dollars, not thousands."
}

#--- 10. Data Product Packaging the Three Lab Tables ---
# google_dataplex_entry_group also exists in the provider, but
# google_dataplex_data_product / _data_asset is the closer fit for "package
# these tables as one consumable product" and is used here.
#
# LOCATION: a data product must sit in the SAME location as the resources it
# packages. The lab tables are in the US multi-region, so these use
# local.taxonomy_region ("us") and NOT local.glossary_location ("us-central1").
# A regional data product over multi-region tables is rejected at apply with:
#   Error 400: Location of resource '//bigquery.googleapis.com/.../tables/...'
#   (us) does not match the location of data product (us-central1).
resource "google_dataplex_data_product" "ecommerce" {
  project         = var.project_id
  location        = local.taxonomy_region
  data_product_id = "ecommerce-lab-product"
  display_name    = "Ecommerce Lab Data Product"
  description     = "Customers, orders and products for the agentic data cloud lab"
  owner_emails    = [replace(var.iap_member, "user:", "")]

  depends_on = [google_project_service.enabled_apis]
}

resource "google_dataplex_data_product_data_asset" "customers" {
  project         = var.project_id
  location        = local.taxonomy_region
  data_product_id = google_dataplex_data_product.ecommerce.data_product_id
  data_asset_id   = "customers"
  resource        = "//bigquery.googleapis.com/projects/${var.project_id}/datasets/${var.bq_dataset_id}/tables/customers"

  depends_on = [google_bigquery_table.customers]
}

resource "google_dataplex_data_product_data_asset" "orders" {
  project         = var.project_id
  location        = local.taxonomy_region
  data_product_id = google_dataplex_data_product.ecommerce.data_product_id
  data_asset_id   = "orders"
  resource        = "//bigquery.googleapis.com/projects/${var.project_id}/datasets/${var.bq_dataset_id}/tables/orders"

  depends_on = [null_resource.wait_for_seed]
}

resource "google_dataplex_data_product_data_asset" "products" {
  project         = var.project_id
  location        = local.taxonomy_region
  data_product_id = google_dataplex_data_product.ecommerce.data_product_id
  data_asset_id   = "products"
  resource        = "//bigquery.googleapis.com/projects/${var.project_id}/datasets/${var.bq_dataset_id}/tables/products"

  depends_on = [null_resource.wait_for_seed]
}

#--- 11. Data Masking on the SENSITIVE Policy Tag ---
# A policy tag on its own is all-or-nothing: a principal either reads the column
# or is refused it. A DATA_MASKING_POLICY adds the third outcome the lab needs —
# a caller who gets a masked value back instead of an error.
#
# EMAIL_MASK keeps the domain and hides the local part (XXXXX@example.com), which
# is the useful shape for this dataset: the agent can still group customers by
# email domain while being unable to identify anyone.
resource "google_bigquery_datapolicy_data_policy" "email_mask" {
  project          = var.project_id
  location         = local.taxonomy_region
  data_policy_id   = "sensitive_email_mask"
  data_policy_type = "DATA_MASKING_POLICY"
  policy_tag       = google_data_catalog_policy_tag.sensitive.name

  data_masking_policy {
    predefined_expression = "EMAIL_MASK"
  }
}

#--- 12. Agent Identity (the principal the masking actually applies to) ---
# The masking policy needs a principal that holds maskedReader and does NOT hold
# categoryFineGrainedReader — Fine-Grained Reader wins over masking, so granting
# both to the participant would show unmasked values and prove nothing. The agent
# therefore runs as its own least-privilege service account: it can run queries
# and read the lab data, but sees customers.email only through the mask.
resource "google_service_account" "agent" {
  project      = var.project_id
  account_id   = "ecommerce-analyst-agent"
  display_name = "Ecommerce Analyst Agent"
  description  = "Least-privilege identity the ADK agent runs as; reads customers.email masked"

  depends_on = [google_project_service.enabled_apis]
}

resource "google_project_iam_member" "agent_roles" {
  for_each = toset([
    "roles/bigquery.jobUser",
    "roles/bigquery.dataViewer",
    "roles/dataplex.viewer",
    # search_entries and lookup_entry need dataplex.projects.search plus read on
    # entries and glossary terms. roles/dataplex.viewer does NOT carry any of
    # them — it covers lakes and zones, not the catalog — so without this the
    # agent gets past the MCP permission check and is refused one layer deeper:
    #   Permission 'dataplex.projects.search' denied on
    #   'projects/<project>/locations/global'
    # and reports "no definition found", which is indistinguishable from a term
    # that genuinely does not exist. roles/dataplex.catalogViewer supplies
    # dataplex.projects.search, entries.get/list and glossaryTerms.get/list.
    "roles/dataplex.catalogViewer",
    # The agent runs ON Vertex, not just against BigQuery. Without this the
    # service account authenticates, reaches the model endpoint and is refused:
    # adk web fails on the first turn even though every data permission is right.
    "roles/aiplatform.user",
    # Reaching a tool THROUGH an MCP server is a separate permission from being
    # allowed to read the data behind it. Both bigquery.googleapis.com/mcp and
    # dataplex.googleapis.com/mcp check mcp.tools.call, and no BigQuery or
    # Dataplex role carries it — verified against roles/bigquery.admin,
    # roles/dataplex.editor, roles/dataplex.viewer and roles/bigquery.jobUser.
    # Without it the agent authenticates, calls search_entries and is refused:
    #   Permission 'mcp.googleapis.com/tools.call' denied on resource
    #   '//dataplex.googleapis.com/mcp/projects/<project>'
    # roles/mcp.toolUser is the least-privilege option; roles/mcp.admin carries
    # the same three permissions and nothing more, so it buys nothing here.
    "roles/mcp.toolUser",
  ])
  project = var.project_id
  role    = each.key
  member  = "serviceAccount:${google_service_account.agent.email}"
}

# The masked read. Note the deliberate absence of a
# google_data_catalog_policy_tag_iam_member granting this SA
# roles/datacatalog.categoryFineGrainedReader — that omission is the guardrail.
resource "google_bigquery_datapolicy_data_policy_iam_member" "agent_masked_reader" {
  project        = var.project_id
  location       = local.taxonomy_region
  data_policy_id = google_bigquery_datapolicy_data_policy.email_mask.data_policy_id
  role           = "roles/bigquerydatapolicy.maskedReader"
  member         = "serviceAccount:${google_service_account.agent.email}"
}

# Lets the participant run the agent AS that service account:
#   gcloud auth application-default login --impersonate-service-account=<email>
resource "google_service_account_iam_member" "participant_impersonation" {
  service_account_id = google_service_account.agent.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = var.iap_member
}

#--- 13. Generated Identity-Switch Scripts ---
# The service account email is only known after apply, and a mistyped address in
# an impersonation command fails in a way that looks like a permissions problem.
# Terraform interpolates it once, here, so the participant never types it.
#
# These land in assets/ next to agent.py, and are regenerated on every apply.

#--- 11. BigQuery Enterprise Reservation Assignment for Step 8 Graph Queries ---
# Declaring a property graph is metadata and runs on on-demand billing. TRAVERSING
# it does not: GRAPH_TABLE is refused with "BigQuery Graph queries require a
# reservation with Enterprise or Enterprise Plus edition". Without this, the lab
# gets through Step 8.2 and dies at 8.3 — the one place a participant cannot fix
# by correcting their own command.
#
# The reservation itself is event-wide and lives in the Admin Project
# (../admin/main.tf, applied in deploy.py Phase 0 before this module). It is
# addressed by its derived name, so no admin state is read here and this
# project's destroy never depends on the admin project still existing.
resource "google_bigquery_reservation_assignment" "graph_lab" {
  count = var.enable_graph_reservation ? 1 : 0

  project     = var.admin_project_id
  location    = local.bq_location
  reservation = "graph-lab" # = labs/10_bigquery_agentic_data_cloud/admin local.reservation_name
  assignee    = "projects/${var.project_id}"
  job_type    = "QUERY"

  # Wait until the seed job completes before routing project queries to the
  # reservation. This allows the seed script to run on fast on-demand slots
  # without waiting for the Enterprise reservation slot autoscaler.
  depends_on = [
    null_resource.wait_for_seed,
  ]
}

#--- 12. Artifact Registry for the Cloud Run Images ---
# deploy.sh pushes two images here: the agent and the lab runner. Regional
# rather than multi-region so it sits next to the Cloud Run services that pull
# from it.
resource "google_artifact_registry_repository" "lab10" {
  project       = var.project_id
  location      = var.cloud_run_region
  repository_id = var.ar_repo_id
  format        = "DOCKER"
  description   = "Container images for the Lab 10 agent and lab runner"

  depends_on = [google_project_service.enabled_apis]
}

#--- 13. Lab Runner Identity ---
# Cloud Run services run as a service account, never as a human. The lab runner
# therefore needs an identity that stands in for the PARTICIPANT, and the whole
# Step 7 demonstration rests on how it differs from the agent's:
#
#   lab runner SA  -> categoryFineGrainedReader  -> reads customers.email raw
#   agent SA       -> maskedReader               -> reads it as XXXXX@domain
#
# Same table, same column, same SQL, one policy tag, two grants. Nothing in
# either service's code decides this and no prompt can talk its way around it.
resource "google_service_account" "lab_runner" {
  project      = var.project_id
  account_id   = "lab-runner-sa"
  display_name = "Lab 10 Lab Runner (stands in for the participant)"

  depends_on = [google_project_service.enabled_apis]
}

# Deliberately mirrors what a participant holds, no more: read the data, run
# jobs, read the catalog. It never gets a write role, so a bug in the lab UI
# cannot damage the lab.
resource "google_project_iam_member" "lab_runner_roles" {
  for_each = toset([
    "roles/bigquery.jobUser",
    "roles/bigquery.dataViewer",
    # dataplex.editor, not viewer: Step 3 has the participant create and delete
    # glossary terms with agy, and the lab runner reads the result back.
    "roles/dataplex.editor",
    "roles/dataplex.catalogViewer",
    "roles/mcp.toolUser",
    "roles/aiplatform.user",
    # Reads its own and the agent's service state for the Step 6 health check.
    "roles/run.admin",
  ])
  project = var.project_id
  role    = each.key
  member  = "serviceAccount:${google_service_account.lab_runner.email}"
}

# The grant that makes the left/right split in Step 7 real.
resource "google_data_catalog_policy_tag_iam_member" "lab_runner_fine_grained" {
  policy_tag = google_data_catalog_policy_tag.sensitive.name
  role       = "roles/datacatalog.categoryFineGrainedReader"
  member     = "serviceAccount:${google_service_account.lab_runner.email}"
}

# The lab runner calls the deployed agent over HTTP with an ID token.
resource "google_project_iam_member" "lab_runner_invoker" {
  project = var.project_id
  role    = "roles/run.invoker"
  member  = "serviceAccount:${google_service_account.lab_runner.email}"
}

#--- 14. Deployment Permissions for the Participant ---
# deploy.sh builds with Cloud Build and deploys to Cloud Run as the participant,
# and Cloud Run checks that whoever sets serviceAccountName may act as that
# account -- without this, `gcloud run services replace` fails on the agent
# service with a permission error naming iam.serviceAccounts.actAs.
resource "google_service_account_iam_member" "participant_acts_as_lab_runner" {
  service_account_id = google_service_account.lab_runner.name
  role               = "roles/iam.serviceAccountUser"
  member             = var.iap_member
}

resource "google_service_account_iam_member" "participant_acts_as_agent" {
  service_account_id = google_service_account.agent.name
  role               = "roles/iam.serviceAccountUser"
  member             = var.iap_member
}

# deploy.sh runs `gcloud builds submit` without --service-account, so Cloud Build runs as the
# Compute Engine default service account, and the caller must be allowed to act as it. Lab 11's
# deploy.sh builds the same way; it always deploys with this lab, so this one grant covers both.
resource "google_service_account_iam_member" "participant_acts_as_build_sa" {
  service_account_id = "projects/${var.project_id}/serviceAccounts/${data.google_project.project.number}-compute@developer.gserviceaccount.com"
  role               = "roles/iam.serviceAccountUser"
  member             = var.iap_member
}

# Participant PROJECT roles (run.admin, cloudbuild.builds.editor, ...) live in
# lab.yaml `antigravity.iam_roles` and are granted by the engine. The resource
# that used to repeat them here is FORGOTTEN, not destroyed: the engine's grant
# owns the same (role, member) pairs, and destroying would revoke them.
removed {
  from = google_project_iam_member.participant_deploy_roles
  lifecycle {
    destroy = false
  }
}

# Build identities push the lab images into Artifact Registry. This used to be
# a best-effort `gcloud ... || true` in init.sh; here it is reproducible and a
# failure is visible at apply time. The Cloud Build service agent is created
# lazily, so it is materialised explicitly before it is granted anything.
resource "google_project_service_identity" "cloudbuild" {
  provider   = google-beta
  project    = var.project_id
  service    = "cloudbuild.googleapis.com"
  depends_on = [google_project_service.enabled_apis]
}

resource "google_project_iam_member" "build_identities_ar_writer" {
  for_each = {
    cloudbuild = "serviceAccount:${google_project_service_identity.cloudbuild.email}"
    compute    = "serviceAccount:${data.google_project.project.number}-compute@developer.gserviceaccount.com"
  }
  project = var.project_id
  role    = "roles/artifactregistry.writer"
  member  = each.value
}

#--- 15. MCP Configuration for agy: NOT generated here ---
# There is deliberately no local_file resource for mcp_config.json. An earlier
# version of this file had one, writing ../assets/mcp_config.json. It was
# removed because it can never work, on two counts:
#
#   Wrong machine. Terraform runs on whoever deploys the event, not on the
#   participant's workstation. local_file writes to the deployment host.
#
#   Wrong time. The scaffolding engine builds and uploads assets.zip BEFORE it
#   runs `terraform apply` (engine/templates/deploy.py builds the shared zip,
#   then each project's workdir is applied). Anything apply writes lands after
#   the zip is sealed, so it is not in the bundle a workstation unpacks.
#
# Both failures are silent: agy simply finds no MCP servers and reports that
# glossary terms do not exist.
#
# `init.sh` (section 6) is the single owner of mcp_config.json. It runs ON the
# workstation, AFTER the project id is known, so it is the only place that can
# write the x-goog-user-project header both endpoints require. It writes to
# ~/.gemini/config/ and ~/.gemini/antigravity-cli/ and verifies both.
#
# `antigravity.mcp_servers` in lab.yaml stays declared but is currently inert:
# the bootstrapper renders it verbatim with no project id to substitute, and the
# engine's lab.yaml parser rejects hyphenated keys, so the required header
# cannot be expressed there. When the engine gains project substitution, the
# declaration is already correct and init.sh becomes a belt-and-braces rewrite.

#--- 16. (removed) Identity-Switch Scripts ---
# There were two local_file resources here writing impersonate_agent.sh and
# restore_identity.sh into ../archive/. They reached nobody, for the same reason
# section 15 gives for mcp_config.json: local_file writes to the DEPLOYMENT host,
# not to a participant's workstation. Three things had to line up and none did --
# archive/ is gitignored, lab.yaml's assets stanza ships only src/, and the
# scaffolder seals assets.zip before `terraform apply` runs anyway.
#
# They also had no caller: nothing in the guide, the lab runner, verify.sh or
# whoami.sh referenced either script. The lab no longer switches identity at all
# -- Cloud Run assigns each service its account -- so the debugging need they
# were written for does not arise. Use whoami.sh instead.
#
# If a future version genuinely needs a script on the workstation, it belongs in
# src/ (which the scaffolder copies to assets/), never in a local_file.
