# ==============================================================================
# Dynamic Root Module - Stitched by Hackathon Scaffolder
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
    random = {
      source  = "hashicorp/random"
      version = ">= 3.0"
    }
    time = {
      source  = "hashicorp/time"
      version = ">= 0.9"
    }
  }
  backend "gcs" {}
}

provider "google" {
  project = var.project_id
  region  = var.region
}

provider "google-beta" {
  project = var.project_id
  region  = var.region
}

# 1. Base Core Infrastructure
module "core" {
  source       = "./00-core-infra"
  project_id   = var.project_id
  region       = var.region
  iap_member   = var.iap_member
}

# 1.5 Deployer IAM: roles the Terraform-running identity needs beyond roles/editor
#     (union of lab.yaml deployer_iam_roles). Bootstrap floor, granted out of band:
#     roles/editor + roles/resourcemanager.projectIamAdmin (or Owner).
data "google_client_openid_userinfo" "deployer" {}

locals {
  # null when the token carries no userinfo email (certificate-based access); the
  # precondition below then asks for var.deployer_member instead of endswith() failing.
  deployer_email  = data.google_client_openid_userinfo.deployer.email == null ? "" : data.google_client_openid_userinfo.deployer.email
  deployer_member = var.deployer_member != "" ? var.deployer_member : (
    endswith(local.deployer_email, ".gserviceaccount.com")
    ? "serviceAccount:${local.deployer_email}"
    : "user:${local.deployer_email}"
  )
}

resource "google_project_iam_member" "deployer_roles" {
  for_each = toset([
    "roles/bigquery.admin",
    "roles/datacatalog.categoryAdmin",
    "roles/iam.serviceAccountAdmin",
    "roles/secretmanager.admin",
  ])
  project = var.project_id
  role    = each.key
  member  = local.deployer_member

  lifecycle {
    precondition {
      condition     = local.deployer_member != "user:" && local.deployer_member != "serviceAccount:"
      error_message = "Could not resolve the deploying identity from the access token. Set var.deployer_member."
    }
  }
}

resource "time_sleep" "deployer_iam_propagation" {
  create_duration = "90s"
  triggers = {
    roles = join(",", sort(keys(google_project_iam_member.deployer_roles)))
  }
  depends_on = [google_project_iam_member.deployer_roles]
}

# 2.1 Lab Module: From Data to Agent (BigQuery & Knowledge Catalog)
module "lab_bigquery_agentic_data_cloud" {
  source                    = "./labs/lab01_bigquery_agentic_data_cloud/infra"
  project_id                = var.project_id
  region                    = var.region
  iap_member                = var.iap_member
  vpc_id                    = module.core.vpc_id
  private_vpc_connection_id = module.core.private_vpc_connection_id
  bq_dataset_id = var.bigquery_agentic_data_cloud_bq_dataset_id
  bq_graph_dataset_id = var.bigquery_agentic_data_cloud_bq_graph_dataset_id
  cloud_run_region = var.bigquery_agentic_data_cloud_cloud_run_region
  ar_repo_id = var.bigquery_agentic_data_cloud_ar_repo_id
  admin_project_id = var.admin_project_id

  depends_on = [
    module.core,
    time_sleep.deployer_iam_propagation,
  ]
}

# 2.2 Lab Module: Agentic Data Cloud: Zero-ETL Federation (AlloyDB, Spanner & BigQuery)
module "lab_agentic_data_cloud_zero_etl" {
  source                    = "./labs/lab02_agentic_data_cloud_zero_etl/infra"
  project_id                = var.project_id
  region                    = var.region
  iap_member                = var.iap_member
  vpc_id                    = module.core.vpc_id
  private_vpc_connection_id = module.core.private_vpc_connection_id
  bq_zeroetl_dataset_id = var.agentic_data_cloud_zero_etl_bq_zeroetl_dataset_id
  bq_source_dataset_id = var.agentic_data_cloud_zero_etl_bq_source_dataset_id
  spanner_instance_id = var.agentic_data_cloud_zero_etl_spanner_instance_id
  spanner_processing_units = var.agentic_data_cloud_zero_etl_spanner_processing_units
  alloydb_cluster_id = var.agentic_data_cloud_zero_etl_alloydb_cluster_id
  alloydb_instance_cpu_count = var.agentic_data_cloud_zero_etl_alloydb_instance_cpu_count
  enable_alloydb = var.agentic_data_cloud_zero_etl_enable_alloydb
  cloud_run_region = var.agentic_data_cloud_zero_etl_cloud_run_region
  ar_repo_id = var.agentic_data_cloud_zero_etl_ar_repo_id
  source_seed_version = module.lab_bigquery_agentic_data_cloud.seed_version

  depends_on = [
    module.core,
    time_sleep.deployer_iam_propagation,
    module.lab_bigquery_agentic_data_cloud,
  ]
}

# 2.3 Lab Module: Publish ADK Agents to Gemini Enterprise (Labs 10 & 11)
module "lab_gemini_enterprise_agents" {
  source                    = "./labs/lab03_gemini_enterprise_agents/infra"
  project_id                = var.project_id
  region                    = var.region
  iap_member                = var.iap_member
  vpc_id                    = module.core.vpc_id
  private_vpc_connection_id = module.core.private_vpc_connection_id
  cloud_run_region = var.gemini_enterprise_agents_cloud_run_region
  ar_repo_id = var.gemini_enterprise_agents_ar_repo_id

  depends_on = [
    module.core,
    time_sleep.deployer_iam_propagation,
    module.lab_bigquery_agentic_data_cloud,
  ]
}

# 3. Dynamic Least-Privilege Lab IAM Roles
resource "google_project_iam_member" "lab_roles" {
  for_each = toset([
    "roles/alloydb.admin",
    "roles/alloydb.databaseUser",
    "roles/artifactregistry.writer",
    "roles/bigquery.admin",
    "roles/bigquery.connectionUser",
    "roles/bigquery.jobUser",
    "roles/cloudbuild.builds.editor",
    "roles/datacatalog.admin",
    "roles/datacatalog.categoryAdmin",
    "roles/datacatalog.tagTemplateUser",
    "roles/dataplex.catalogViewer",
    "roles/dataplex.editor",
    "roles/dataplex.viewer",
    "roles/discoveryengine.admin",
    "roles/iam.securityReviewer",
    "roles/logging.viewer",
    "roles/mcp.toolUser",
    "roles/run.admin",
    "roles/secretmanager.viewer",
    "roles/serviceusage.serviceUsageConsumer",
    "roles/spanner.databaseAdmin",
    "roles/storage.admin",
    "roles/viewer",
    "roles/workstations.operationViewer"
  ])
  project = var.project_id
  role    = each.key
  member  = var.iap_member
}
