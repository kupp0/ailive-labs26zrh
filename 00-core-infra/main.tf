# ==============================================================================
# DACH Summit 2026: Core Shared Infrastructure Baseline (Decoupled)
# ==============================================================================

terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0"
    }
  }
}

data "google_project" "project" {}

# --- 1. Enable Core APIs ---
resource "google_project_service" "core_apis" {
  for_each = toset([
    "compute.googleapis.com",
    "servicenetworking.googleapis.com",
    "workstations.googleapis.com",
    "iam.googleapis.com",
    "secretmanager.googleapis.com",
    "cloudresourcemanager.googleapis.com"
  ])
  project            = var.project_id
  service            = each.key
  disable_on_destroy = false
}

# --- 2. VPC Network & Subnet ---
resource "google_compute_network" "vpc" {
  name                    = var.network_name
  auto_create_subnetworks = false
  project                 = var.project_id
  depends_on              = [google_project_service.core_apis]
}

resource "google_compute_subnetwork" "subnet" {
  name                     = var.subnet_name
  ip_cidr_range            = var.subnet_cidr
  region                   = var.region
  network                  = google_compute_network.vpc.id
  project                  = var.project_id
  private_ip_google_access = true
}

# --- 3. Cloud NAT & Routing ---
resource "google_compute_router" "router" {
  name    = "${var.network_name}-router"
  region  = var.region
  network = google_compute_network.vpc.id
  project = var.project_id
}

resource "google_compute_router_nat" "nat" {
  name                               = "${var.network_name}-nat"
  router                             = google_compute_router.router.name
  region                             = var.region
  project                            = var.project_id
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"
}

# --- 4. Service Networking (VPC Peering for Databases) ---
resource "google_compute_global_address" "private_ip_alloc" {
  name          = "alloydb-private-ip"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = 16
  network       = google_compute_network.vpc.id
  project       = var.project_id
}

resource "google_service_networking_connection" "private_vpc_connection" {
  network                 = google_compute_network.vpc.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.private_ip_alloc.name]
  deletion_policy         = "ABANDON"
  depends_on              = [google_project_service.core_apis]
}

# --- 5. Cloud Workstation Cluster & Config ---
resource "google_workstations_workstation_cluster" "default" {
  workstation_cluster_id = "workstation-cluster"
  location               = var.region
  network                = google_compute_network.vpc.id
  subnetwork             = google_compute_subnetwork.subnet.id
  project                = var.project_id

  depends_on = [google_project_service.core_apis]
}

# Note: The bootargs and container setup are dynamically injected by the Scaffolder
# to point to the event's specific bootstrapping script and event repo.
resource "google_workstations_workstation_config" "default" {
  workstation_config_id  = "workstation-config"
  workstation_cluster_id = google_workstations_workstation_cluster.default.workstation_cluster_id
  location               = var.region
  project                = var.project_id

  # The 20-minute default shut workstations down mid-exercise whenever a
  # participant spent a while reading the guide, and a cold start costs several
  # minutes. 4 hours outlasts a lab segment AND a lunch break, so participants
  # come back to a running workstation instead of a cold start; the trade-off is
  # up to 4 idle billable hours per participant. The 12-hour hard cap means a
  # workstation forgotten at the end of the day still stops billing overnight.
  idle_timeout    = "14400s"
  running_timeout = "43200s"

  host {
    gce_instance {
      machine_type                = "e2-standard-4"
      boot_disk_size_gb           = 50
      disable_public_ip_addresses = true
      service_account             = "${data.google_project.project.number}-compute@developer.gserviceaccount.com"
      shielded_instance_config {
        enable_secure_boot = true
        enable_vtpm        = true
      }
    }
  }

  container {
    image   = "us-central1-docker.pkg.dev/cloud-workstations-images/predefined/code-oss:latest"
    command = ["/bin/bash", "-c"]
    # The actual bootstrapping command will be hydrated dynamically by the scaffolder
    args    = [
      "until curl -s -H 'Metadata-Flavor: Google' http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token | grep -q 'access_token'; do echo 'Waiting for metadata server...'; sleep 2; done; TOKEN=$(curl -s -H 'Metadata-Flavor: Google' http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token | grep -o '\"access_token\": *\"[^\"]*\"' | cut -d'\"' -f4); curl -sSL --retry 5 --retry-delay 2 --retry-all-errors -f -H \"Authorization: Bearer $TOKEN\" \"https://storage.googleapis.com/${var.project_id}-lab-assets/assets.zip\" -o /tmp/assets.zip && unzip -q -o /tmp/assets.zip -d /tmp/event-repo && bash /tmp/event-repo/bootstrapping.sh; /google/scripts/entrypoint.sh"
    ]
    env = {
      GOOGLE_CLOUD_PROJECT  = var.project_id
      GOOGLE_CLOUD_LOCATION = "global"
      # Gemini Code Assist resolves its Vertex endpoint from this variable
      # rather than from GOOGLE_CLOUD_LOCATION. Without it the agent targets a
      # regional endpoint and fails for models published only to "global".
      CLOUDSDK_AIPLATFORM_LOCATION = "global"
    }
  }

  persistent_directories {
    mount_path = "/home"
    gce_pd {
      size_gb        = 50
      disk_type      = "pd-balanced"
      reclaim_policy = "DELETE"
    }
  }

  # Ordering guard only: the config waits for this module's project IAM writes
  # so they do not run concurrently with config creation. It is NOT a proven fix
  # for the degraded-config incident (#151, root cause unconfirmed); deploy.py
  # Step 2b detects a degraded config after every apply and reports it.
  depends_on = [
    google_workstations_workstation_cluster.default,
    google_project_iam_member.workstation_sa_roles,
    google_project_iam_member.user_roles,
  ]
}

resource "google_workstations_workstation" "default" {
  workstation_id         = "my-workstation"
  workstation_config_id  = google_workstations_workstation_config.default.workstation_config_id
  workstation_cluster_id = google_workstations_workstation_cluster.default.workstation_cluster_id
  location               = var.region
  project                = var.project_id

  lifecycle {
    # Replace the workstation only when the config is RE-CREATED (its uid
    # changes), never on an in-place config edit. Referencing the whole config
    # resource fires on any planned update; with reclaim_policy = "DELETE" that
    # deleted every participant's /home on an edit as small as idle_timeout.
    replace_triggered_by = [google_workstations_workstation_config.default.uid]
  }

  depends_on = [google_workstations_workstation_config.default]
}

# Grant workstation user access to the participant
resource "google_workstations_workstation_iam_member" "workstationuser" {
  project                = var.project_id
  location               = var.region
  workstation_cluster_id = google_workstations_workstation_cluster.default.workstation_cluster_id
  workstation_config_id  = google_workstations_workstation_config.default.workstation_config_id
  workstation_id         = google_workstations_workstation.default.workstation_id
  role                   = "roles/workstations.user"
  member                 = var.iap_member

  lifecycle {
    replace_triggered_by = [google_workstations_workstation.default]
  }
}

# --- 6. Global IAM Roles for Participant & Workstation SA ---
resource "google_project_iam_member" "user_roles" {
  for_each = toset([
    "roles/aiplatform.user",
    "roles/cloudaicompanion.user",
    "roles/oauthconfig.editor"
  ])

  project = var.project_id
  role    = each.key
  member  = var.iap_member
}

# The agent inside the workstation authenticates as the default compute service
# account, not as the participant, so granting the participant Vertex and Code
# Assist access is not sufficient on its own.
resource "google_project_iam_member" "workstation_sa_roles" {
  for_each = toset([
    "roles/aiplatform.user",
    "roles/cloudaicompanion.user"
  ])

  project = var.project_id
  role    = each.key
  member  = "serviceAccount:${data.google_project.project.number}-compute@developer.gserviceaccount.com"
}

# --- 7. Event Assets Bucket ---
resource "google_storage_bucket" "assets" {
  name                        = "${var.project_id}-lab-assets"
  location                    = var.region
  force_destroy               = true
  uniform_bucket_level_access = true
  project                     = var.project_id
}

resource "google_storage_bucket_object" "assets_zip" {
  name   = "assets.zip"
  bucket = google_storage_bucket.assets.name
  source = "assets.zip"
}
