# ==============================================================================
# Lab 10 — Admin Project module: shared BigQuery Enterprise reservation
# ==============================================================================
# Applied once per event to the `role=admin` project (deploy.py Phase 0),
# stitched by the engine into 00-admin-infra/modules/bigquery_agentic_data_cloud.
#
# Why here and not per participant: GRAPH_TABLE traversal (Step 8.3) is refused
# on on-demand billing ("BigQuery Graph queries require a reservation with
# Enterprise or Enterprise Plus edition"). One reservation per participant
# multiplies org-level reservation quota by the cohort size; one shared pool
# with a 0-slot baseline costs nothing while idle.
#
# Participant projects attach to it in ../infra/main.tf section 11 via
# `google_bigquery_reservation_assignment`, addressing it by the derived name
# `graph-lab` in project `var.admin_project_id`. No Terraform output crosses the
# admin/participant state boundary, so participant destroy never needs this
# state. The engine destroys this module only after every participant is gone.
# ==============================================================================

locals {
  # Must equal Lab 10 infra `local.bq_location`: an assignment can only target
  # a reservation in the same location as the assignee's datasets.
  bq_location      = "US"
  reservation_name = "graph-lab"
}

resource "google_project_service" "reservation_apis" {
  for_each = toset([
    "bigquery.googleapis.com",
    "bigqueryreservation.googleapis.com",
  ])
  project            = var.project_id
  service            = each.key
  disable_on_destroy = false
}

# slot_capacity = 0: no baseline, idle costs nothing. Queries run on autoscaled
# slots, billed per second with a one-minute minimum, shared by the cohort.
resource "google_bigquery_reservation" "graph_lab" {
  name          = local.reservation_name
  project       = var.project_id
  location      = local.bq_location
  slot_capacity = 0
  edition       = "ENTERPRISE"

  # Idle slots from OTHER reservations must not leak in: the lab demonstrates
  # the Enterprise requirement, and borrowed capacity would make behaviour
  # depend on whatever else the admin project runs.
  ignore_idle_slots = true

  autoscale {
    max_slots = var.max_slots
  }

  depends_on = [google_project_service.reservation_apis]
}
