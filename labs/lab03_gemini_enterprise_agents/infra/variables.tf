# ==============================================================================
# Lab 12 — Publish the Lab 10 & 11 ADK agents to Gemini Enterprise — inputs
# ==============================================================================

variable "project_id" {
  description = "The target ephemeral GCP project allocated for the participant"
  type        = string
}

variable "region" {
  description = "The target deployment region (unused: Gemini Enterprise is global and the console follows cloud_run_region)"
  type        = string
  default     = "europe-west3"
}

variable "vpc_id" {
  description = "The VPC network ID from core infrastructure (unused by this lab but required by the stitching contract)"
  type        = string
  default     = ""
}

variable "private_vpc_connection_id" {
  description = "The private services connection ID from core infrastructure (unused by this lab but required by the stitching contract)"
  type        = string
  default     = ""
}

variable "iap_member" {
  description = "The IAM member string for the participant (e.g., user:participant01@gcplab.me)"
  type        = string
}

variable "cloud_run_region" {
  description = "Region of the publisher console and its Artifact Registry repository. Must match the Lab 10/11 agents' region"
  type        = string
  default     = "us-central1"
}

variable "ar_repo_id" {
  description = "Artifact Registry repository for the publisher console image"
  type        = string
  default     = "lab12"
}
