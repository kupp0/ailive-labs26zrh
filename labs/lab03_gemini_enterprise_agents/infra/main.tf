# ==============================================================================
# Lab 12 — Publish the Lab 10 & 11 ADK agents to Gemini Enterprise
#
# Terraform owns only what exists before the participant arrives:
#
#   * the APIs Gemini Enterprise (Discovery Engine) needs,
#   * the Discovery Engine service agent -- the identity GE uses to call an
#     A2A agent on Cloud Run -- materialised up front so it can be granted
#     run.invoker the moment the participant publishes,
#   * the publisher console's identity, and the repository its image lives in.
#
# Everything that depends on a participant action happens in the publisher
# console at runtime: the Gemini Enterprise trial licence, the GE app, the
# service-level invoker grants on the Lab 10/11 agent services (created by those
# labs' deploy.sh, so Terraform cannot reference them), the A2A registrations
# and the licence seats. That split is why this module is short.
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
  }
}

data "google_project" "project" {
  project_id = var.project_id
}

#--- 1. APIs ---
# discoveryengine: Gemini Enterprise apps, licences, A2A agent registrations.
# cloudaicompanion: GE onboarding checks it, and GE conversations use it.
# iamcredentials: the console mints an ID token to fetch each agent's card.
resource "google_project_service" "enabled_apis" {
  for_each = toset([
    "discoveryengine.googleapis.com",
    "cloudaicompanion.googleapis.com",
    "run.googleapis.com",
    "cloudbuild.googleapis.com",
    "artifactregistry.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "cloudresourcemanager.googleapis.com",
  ])
  project            = var.project_id
  service            = each.key
  disable_on_destroy = false
}

#--- 2. Discovery Engine service agent ---
# service-<number>@gcp-sa-discoveryengine.iam.gserviceaccount.com. GE sends an
# OIDC token for this identity (X-Serverless-Authorization) when it calls a
# Cloud Run A2A agent. Service agents are created lazily; creating it here means
# the publisher's invoker grant never races its creation.
resource "google_project_service_identity" "discoveryengine" {
  provider   = google-beta
  project    = var.project_id
  service    = "discoveryengine.googleapis.com"
  depends_on = [google_project_service.enabled_apis]
}

#--- 3. Publisher console identity ---
# The console that does the publishing runs as this account, never as the
# participant. Its roles, and why each one:
#
#   discoveryengine.admin  provision GE, create the trial licence, the app, the
#                          A2A agent registrations and the licence seats.
#   run.admin              read the Lab 10/11 agent services and set IAM on
#                          them (service-level run.invoker for the service agent
#                          above). It holds actAs on no runtime identity, so it
#                          cannot deploy or change what any service runs as.
#   serviceUsageConsumer   call Google APIs billed to this project.
resource "google_service_account" "publisher" {
  project      = var.project_id
  account_id   = "ge-publisher-sa"
  display_name = "Lab 12 Gemini Enterprise publisher console"

  depends_on = [google_project_service.enabled_apis]
}

resource "google_project_iam_member" "publisher_roles" {
  for_each = toset([
    "roles/discoveryengine.admin",
    "roles/run.admin",
    "roles/serviceusage.serviceUsageConsumer",
  ])
  project = var.project_id
  role    = each.key
  member  = "serviceAccount:${google_service_account.publisher.email}"
}

# deploy.sh sets serviceAccountName on the console's Cloud Run service, and Cloud
# Run checks that the deployer may act as that account.
resource "google_service_account_iam_member" "participant_acts_as_publisher" {
  service_account_id = google_service_account.publisher.name
  role               = "roles/iam.serviceAccountUser"
  member             = var.iap_member
}

#--- 4. Artifact Registry for the console image ---
resource "google_artifact_registry_repository" "lab12" {
  project       = var.project_id
  location      = var.cloud_run_region
  repository_id = var.ar_repo_id
  format        = "DOCKER"
  description   = "Container image for the Lab 12 Gemini Enterprise publisher console"

  depends_on = [google_project_service.enabled_apis]
}
