# Author: Wissem Khlifi - August 2026
# ==============================================================================
# Lab 11 — Agentic Data Cloud: Zero-ETL — input variables
# ==============================================================================

variable "project_id" {
  description = "The target ephemeral GCP project allocated for the participant"
  type        = string
}

# --- READ THIS BEFORE USING var.region ANYWHERE IN main.tf ---
# This lab does NOT use var.region, and that is deliberate rather than an
# oversight. Lab 10 pins BigQuery to the US multi-region because
# `bigquery-public-data.thelook_ecommerce` lives there and BigQuery cannot read
# across locations. Lab 11 federates back into Lab 10's datasets, so every
# federated read is a US job.
#
# This lab assumes BigQuery federated READS (EXTERNAL_QUERY, external datasets)
# co-locate by GEOGRAPHY, not exact location: a US multi-region query reading a
# Spanner or AlloyDB instance in a single US region, through a connection that
# itself lives in US. That assumption has only been exercised with us-central1;
# other US regions are unverified for this lab.
#
# The reverse-ETL WRITE path is stricter. EXPORT DATA from the US multi-region
# into an AlloyDB instance in us-east4 was rejected ("Exporting data from "us" to
# "us-east4" is not supported"), so us-central1 is the only region verified for
# both directions. Spanner and AlloyDB therefore stay together in us-central1
# (local.data_region) and every BigQuery object goes to US (local.bq_location).
# If us-central1 has no AlloyDB capacity, provision with enable_alloydb = false
# (a Spanner-only lab that verify.sh reports honestly) instead of moving AlloyDB.
#
# The Data Forge default for var.region is europe-west3. Wiring it into either
# engine would put them outside the US geography and every federated query would
# fail with a location error that names neither this file nor the cause.
#
# Lab 02 DOES use var.region for both its Spanner instance and its BigQuery
# dataset. That is correct there — both sides move together. It is wrong here.
variable "region" {
  description = "The target deployment region from the stitching contract. UNUSED by this lab: see the comment above. Spanner and AlloyDB are pinned to local.data_region, BigQuery to local.bq_location."
  type        = string
  default     = "europe-west3"
}

variable "vpc_id" {
  description = "The VPC network ID from core infrastructure. Used for the AlloyDB cluster's private services access."
  type        = string
  default     = ""
}

variable "private_vpc_connection_id" {
  description = "The private services connection ID from core infrastructure. AlloyDB depends on it existing; the peering range itself is VPC-global, so a us-central1 cluster on a VPC subnetted in another region provisions correctly."
  type        = string
  default     = ""
}

variable "iap_member" {
  description = "The IAM member string for the participant (e.g., user:devstar@gcplab.me)"
  type        = string
}

# --- BigQuery ---
variable "bq_zeroetl_dataset_id" {
  description = "BigQuery dataset holding the unified views and the unified property graph. Lab 10's ecommerce_lab and ecommerce_graph_lab are read, never modified."
  type        = string
  default     = "ecommerce_zeroetl"
}

variable "bq_source_dataset_id" {
  description = "Lab 10's ecommerce dataset. Lab 11 reads it for the BigQuery arm of the unified view. Must match Lab 10's bq_dataset_id."
  type        = string
  default     = "ecommerce_lab"
}

# --- AlloyDB ---
# The 13-18 minute cluster create is the entire provisioning delta this lab adds
# over Lab 10. Setting this false drops it and leaves a Spanner-only lab that
# still demonstrates EXPORT DATA, federation, both graph engines and the
# transactional split — roughly 70% of the story at half the wall clock.
variable "enable_alloydb" {
  description = "Provision the AlloyDB cluster, its reverse-ETL export and its federation. Set false for a Spanner-only run (~18 min faster)."
  type        = bool
  default     = true
}

variable "alloydb_cluster_id" {
  description = "AlloyDB cluster ID. Deliberately distinct from Lab 01's 'search-cluster' so both labs can be selected for one event."
  type        = string
  default     = "zeroetl-cluster"
}

variable "alloydb_instance_cpu_count" {
  description = "vCPUs for the AlloyDB primary instance (minimum 2)"
  type        = number
  default     = 2

  validation {
    condition     = var.alloydb_instance_cpu_count >= 2
    error_message = "alloydb_instance_cpu_count must be at least 2."
  }
}

# --- Spanner ---
variable "spanner_instance_id" {
  description = "Spanner instance ID. Distinct from Lab 02's 'disneyland' so both labs can be selected for one event."
  type        = string
  default     = "zeroetl-ops"
}

variable "spanner_processing_units" {
  description = "Processing units for the Spanner instance. 100 is the minimum that supports ENTERPRISE edition, which Spanner Graph requires."
  type        = number
  default     = 100

  validation {
    condition     = var.spanner_processing_units >= 100
    error_message = "spanner_processing_units must be at least 100 (Spanner Graph requires ENTERPRISE edition)."
  }
}

# --- Cloud Run ---
variable "cloud_run_region" {
  description = "Region for the three Cloud Run services and the Artifact Registry repository. Matches Lab 10 so every call stays in-region."
  type        = string
  default     = "us-central1"
}

variable "ar_repo_id" {
  description = "Artifact Registry repository holding the dispatch, transact and lab-runner images."
  type        = string
  default     = "lab11"
}

variable "lab_runner_enabled" {
  description = "Create the lab-runner service account and its IAM. Set false to provision the data infrastructure only."
  type        = bool
  default     = true
}

# --- Bootstrap identity ---
# The AlloyDB MCP server's execute_sql tool authenticates as the CALLING IAM
# principal ("Uses IAM user authentication" — verified against the live
# tools/list schema), so whoever runs `terraform apply` is the PostgreSQL user
# that creates the reverse-ETL target tables.
#
# That identity is registered as an AlloyDB IAM user by
# infra/scripts/alloydb_execute_sql.sh at RUN time, not by a Terraform resource.
# An earlier version used data.google_client_openid_userinfo here; because that
# data source resolves during apply, the resulting user_id was unknown at plan
# time and user_id is ForceNew, so every apply planned a replacement whose
# destroy then failed on "role cannot be dropped because some objects depend on
# it".
#
# Set this only when the deployment host's active gcloud account is NOT the
# identity that should own the tables.
variable "alloydb_bootstrap_principal" {
  description = "Override for the identity that runs terraform apply, used as the AlloyDB superuser that creates the reverse-ETL target tables. Leave empty to use the deployment host's active gcloud account."
  type        = string
  default     = ""
}

# Wired by the engine from Lab 10's `seed_version` output (lab.yaml
# infra.dependency_inputs). A change clears and re-exports the seed rows in
# Spanner and AlloyDB. Empty when the module is applied on its own.
variable "source_seed_version" {
  description = "Lab 10 seed identifier. A change re-runs the reverse-ETL clear and export into Spanner and AlloyDB."
  type        = string
  default     = ""
}
