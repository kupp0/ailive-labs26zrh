# Author: Wissem Khlifi - August 2026
variable "project_id" {
  description = "The target ephemeral GCP project allocated for the participant"
  type        = string
}

variable "region" {
  description = "The target deployment region"
  type        = string
  default     = "europe-west3"
}

variable "vpc_id" {
  description = "The VPC network ID from core infrastructure (unused by BigQuery but required by stitching contract)"
  type        = string
  default     = ""
}

variable "private_vpc_connection_id" {
  description = "The private services connection ID from core infrastructure (unused by BigQuery but required by stitching contract)"
  type        = string
  default     = ""
}

variable "iap_member" {
  description = "The IAM member string for the participant (e.g., user:devstar@gcplab.me)"
  type        = string
}

variable "bq_dataset_id" {
  description = "BigQuery dataset ID for the ecommerce lab data"
  type        = string
  default     = "ecommerce_lab"
}

variable "bq_graph_dataset_id" {
  description = "BigQuery dataset ID for the graph analytics data"
  type        = string
  default     = "ecommerce_graph_lab"
}

# --- BigQuery Graph (Step 8) ---
# GRAPH_TABLE traversal is not available on on-demand billing: BigQuery refuses
# the query with "BigQuery Graph queries require a reservation with Enterprise or
# Enterprise Plus edition". CREATE PROPERTY GRAPH itself is only metadata and
# works without one, so the lab fails at Step 8.3, not 8.2.
#
# The shared `graph-lab` reservation (0 baseline slots, autoscaling) lives in
# the event's Admin Project, see ../admin/. This project only holds an
# assignment to it. Its autoscale ceiling is `max_slots` in ../admin/variables.tf.
variable "admin_project_id" {
  description = "Admin Project hosting the shared graph-lab reservation. Injected by the engine (declared by infra.admin_module_source); do not list it in lab.yaml."
  type        = string

  validation {
    condition     = var.admin_project_id != ""
    error_message = "admin_project_id is empty: Lab 10 requires an Admin Project. Mark exactly one projects.txt row role=admin."
  }
}

variable "enable_graph_reservation" {
  description = "Assign this project to the shared Enterprise reservation so Step 8 GRAPH_TABLE queries can run. Set false to keep the project on on-demand billing and use the self-join fallbacks in the guide instead."
  type        = bool
  default     = true
}

# --- Cloud Run deployment (Step: src/init.sh and src/deploy.sh) ---
# BigQuery lives in the US multi-region and the glossary in us-central1; putting
# the services and their images in us-central1 keeps every call in-region.
variable "cloud_run_region" {
  description = "Region for the Cloud Run services and the Artifact Registry repository."
  type        = string
  default     = "us-central1"
}

variable "ar_repo_id" {
  description = "Artifact Registry repository holding the agent and lab-runner images."
  type        = string
  default     = "lab10"
}

variable "lab_runner_enabled" {
  description = "Create the lab-runner service account and its IAM. Set false to provision the data infrastructure only."
  type        = bool
  default     = true
}
