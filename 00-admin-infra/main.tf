# ==============================================================================
# Data Forge: Admin Project Root Module
# ==============================================================================
# Applied ONLY to the single `role=admin` project in projects.txt, during
# Phase 0 of deploy.py, before any participant project is touched.
#
# This root is deliberately empty of resources. Every resource in the admin
# project belongs to a lab: each selected lab that declares
# `infra.admin_module_source` in its lab.yaml is copied to modules/<lab_id>/
# and wired up in the generated labs.tf. Nothing lab-specific belongs here.
#
# Module contract (every admin module MUST declare exactly these inputs):
#   project_id  - the admin project ID
#   region      - the admin project's region from projects.txt
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
