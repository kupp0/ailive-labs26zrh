# Google AI Live + Labs Zurich 2026 — Agentic Data Cloud

> **Thank you for participating in the Google AI Live + Labs Event in Zurich!** 🇨🇭
> We appreciate the time, curiosity, and technical rigor you brought to the hands-on sessions. We are sharing this repository so you can revisit the lab guides, explore the reference agent and infrastructure code at your own pace, and deploy or adapt these architectures in your own Google Cloud environments.

---

## 📖 Overview

This repository contains the complete infrastructure-as-code (Terraform), orchestration scripts, application source code, Agent Development Kit (ADK) agents, Model Context Protocol (MCP) configurations, and step-by-step user guides used during **Google AI Live + Labs Zurich 2026**.

Across three progressive lab tracks, this repository demonstrates how to build a **governed Agentic Data Cloud** where business semantics, security boundaries, and read/write permissions are enforced by the data platform and IAM rather than by fragile LLM prompt instructions.

---

## 🚀 Lab Tracks

### 1. [Lab 1: From Data to Agent — BigQuery, Knowledge Catalog, BigQuery Graph & ADK](labs/lab01_bigquery_agentic_data_cloud/user_guide.md)
Build an autonomous, governed data analyst agent using the **Google Agent Development Kit (ADK)** backed by **BigQuery** and **Dataplex Knowledge Catalog**:
* **Catalog Grounding**: Resolve official business definitions and SQL formulas dynamically from a Dataplex Business Glossary over MCP (`dataplex.googleapis.com/mcp`) instead of hardcoding rules in prompts.
* **Read-Only Protocol Filtering**: Restrict the agent's BigQuery MCP toolset (`bigquery.googleapis.com/mcp`) to read-only execution.
* **Column-Level Security & Dynamic Data Masking**: Enforce PII masking (`EMAIL_MASK` via BigQuery Data Policies and Policy Tags) at the query engine layer by running the agent under a dedicated least-privilege service account (`ecommerce-analyst-agent`).
* **BigQuery Property Graphs**: Declare and traverse a customer-product property graph using SQL/PGQ (`GRAPH_TABLE`).

### 2. [Lab 2: Agentic Data Cloud — Zero-ETL Federation across AlloyDB, Spanner & BigQuery](labs/lab02_agentic_data_cloud_zero_etl/user_guide.md)
Extend the governed analyst into a multi-engine operational and analytical architecture without building ETL pipelines:
* **Reverse ETL from BigQuery**: Seed operational tables directly into **AlloyDB for PostgreSQL** and **Cloud Spanner** using BigQuery `EXPORT DATA`.
* **Live Zero-ETL Federation**: Query AlloyDB (`EXTERNAL_QUERY`) and Cloud Spanner (Spanner external dataset) live from BigQuery through a unified view with zero data duplication.
* **Dual Graph Engines**: Compare analytical property graph traversal in **BigQuery Graph** with operational graph traversal in **Spanner Graph**.
* **IAM-Enforced Multi-Agent Routing**: Separate read/analytics (`lab11-agent` dispatch orchestrator) from operational writes (`lab11-transact` service for AlloyDB and Spanner mutations) using distinct Cloud Run service identities.

### 3. [Lab 3: Publish ADK Agents to Gemini Enterprise](labs/lab03_gemini_enterprise_agents/user_guide.md)
Expose the Cloud Run ADK agents from Labs 1 and 2 to business users through **Gemini Enterprise** (Discovery Engine):
* **Agent2Agent (A2A v0.3) Surface**: Wrap existing ADK agents with an A2A v0.3 protocol endpoint without rewriting agent business logic.
* **One-Click Publisher Console**: Provision a Gemini Enterprise app, register the Cloud Run A2A agents, and grant the Discovery Engine service agent least-privilege `roles/run.invoker` access on only the read-facing entrypoints—never on the transactional write service.

---

## 📂 Repository & Directory Structure

```text
.
├── README.md                        # Repository overview, architecture, and deployment guide
├── event-manifest.json              # Declarative manifest of selected labs, skills, and MCP servers
├── projects.txt.example             # Template mapping GCP project IDs to participant IAM identities
├── deploy.sh                        # CLI wrapper entrypoint for the parallel provisioning engine
├── deploy.py                        # Parallel Terraform orchestrator (apply, plan, check, handoff, destroy)
├── warmup.sh                        # CLI wrapper for Cloud Workstations warm-up and idle timeout management
├── warmup_workstations.py           # Pre-warms, audits, and stops participant Cloud Workstations
├── bootstrapping.sh                 # Startup script executed inside each Cloud Workstation container
├── main.tf                          # Root Terraform module for participant projects
├── variables.tf                     # Root Terraform input variable declarations
├── terraform.tfvars                 # Default variable values for the event lab modules
│
├── 00-admin-infra/                  # Phase 0 Terraform root module (applied to the role=admin project)
│   ├── main.tf                      # Admin provider & GCS backend configuration
│   ├── labs.tf                      # Stitched admin modules required by selected labs
│   ├── variables.tf                 # Admin project input variables (project_id, region)
│   └── modules/
│       └── bigquery_agentic_data_cloud/
│           ├── main.tf              # Shared BigQuery Enterprise reservation (graph-lab, 0-slot baseline, autoscale)
│           └── variables.tf         # Max autoscale slots configuration
│
├── 00-core-infra/                   # Base per-participant infrastructure module
│   ├── main.tf                      # VPC, Subnet, Cloud NAT, Private Service Networking, Cloud Workstations, GCS assets bucket
│   ├── variables.tf                 # Network and workstation variables
│   └── outputs.tf                   # VPC and private connection outputs consumed by lab modules
│
└── labs/                            # Self-contained lab tracks (infrastructure, code assets, skills, guides)
    ├── lab01_bigquery_agentic_data_cloud/
    │   ├── user_guide.md            # Step-by-step instructions for Lab 1
    │   ├── images/                  # Architecture and conceptual diagrams
    │   ├── .agents/skills/          # Agent skills hydrated into the workstation (bigquery-graph, knowledge-catalog)
    │   ├── infra/                   # Terraform module: BigQuery datasets/tables, Policy Tags, Dataplex Glossary, SAs
    │   └── assets/                  # Code copied to ~/labs/ on the workstation:
    │       ├── init.sh / deploy.sh / verify.sh / whoami.sh
    │       ├── agent/               # ADK governed analyst agent + A2A surface (Cloud Run)
    │       └── lab-runner/          # Interactive web console for testing steps 6–9 (Cloud Run)
    │
    ├── lab02_agentic_data_cloud_zero_etl/
    │   ├── user_guide.md            # Step-by-step instructions for Lab 2
    │   ├── images/                  # Multi-engine and zero-ETL architecture diagrams
    │   ├── .agents/skills/          # Agent skills (zero-etl-federation, alloydb-transactional, spanner-graph-operational, multi-agent-routing)
    │   ├── infra/                   # Terraform module: AlloyDB cluster, Spanner instance/graph, BQ federation, SAs
    │   └── assets/                  # Code copied to ~/labs/ on the workstation:
    │       ├── init.sh / deploy.sh / verify.sh / whoami.sh
    │       ├── agent/               # Multi-agent router & read-only specialists (Cloud Run)
    │       ├── transact/            # Isolated write service for AlloyDB & Spanner mutations (Cloud Run)
    │       └── lab-runner/          # Interactive web console for zero-ETL & routing demos (Cloud Run)
    │
    └── lab03_gemini_enterprise_agents/
        ├── user_guide.md            # Step-by-step instructions for Lab 3
        ├── infra/                   # Terraform module: Discovery Engine APIs, service identity, publisher SA
        └── assets/                  # Code copied to ~/labs/ on the workstation:
            ├── deploy.sh / verify.sh
            └── publisher/           # One-click Gemini Enterprise A2A publisher web console (Cloud Run)
```

### How the Pieces Fit Together
1. **`00-admin-infra/` (Phase 0 — Admin Project)**: BigQuery Property Graph traversal (`GRAPH_TABLE`) requires a **BigQuery Enterprise Edition** slot reservation. To avoid creating redundant reservations per participant, `00-admin-infra` creates a single shared reservation (`graph-lab` in the `US` multi-region with `slot_capacity = 0` baseline and autoscaling up to `max_slots`) in a dedicated `role=admin` project. Idle baseline cost is $0 when no queries are running.
2. **`00-core-infra/` (Participant Project Baseline)**: Provisions a custom VPC, Cloud Router/NAT, Private Service Access peering (required by AlloyDB), a GCS bucket (`<project_id>-lab-assets`) holding `assets.zip`, and a managed **Cloud Workstation** (`e2-standard-4`, Code OSS IDE). On boot, the workstation downloads `assets.zip` and runs `bootstrapping.sh` to populate `/home/user/labs/` and wire up the MCP server configurations.
3. **`labs/*/infra/` (Participant Lab Infrastructure)**: Provisions the BigQuery datasets (`ecommerce_lab`, `ecommerce_graph_lab`, `ecommerce_zeroetl`), seeds them from `bigquery-public-data.thelook_ecommerce`, sets up Data Catalog Policy Tags and Dataplex Business Glossaries, provisions the **Cloud Spanner** (`zeroetl-ops`, 100 PUs, Enterprise Edition) and **AlloyDB** (`zeroetl-cluster`) instances, exports seed data into both operational engines, configures BigQuery federated connections, and sets up least-privilege Service Accounts.

---

## 🔐 Required GCP IAM Privileges for Deployment

The provisioning orchestrator (`./deploy.sh` / `deploy.py`) uses your active **Google Cloud SDK (`gcloud`)** identity to enable APIs, create remote Terraform state buckets (`gs://<project_id>-tfstate`), bind project-level IAM policies, and run Terraform across the target project(s).

### 1. Prerequisites on Target GCP Projects
* **Active Billing Account**: Every target GCP project (`role=admin` and `role=participant`) must be linked to an active Cloud Billing account.
* **Organization Policies (If deploying inside a Google Cloud Organization)**:
  * All lab services use IAM-authenticated Cloud Run (`allUsers` is **not** required), internal VPC peering for AlloyDB, and shielded VMs without public IPs for Cloud Workstations.
  * **Domain Restricted Sharing (` constraints/iam.allowedPolicyMemberDomains `)**: Ensure that the Google Workspace domain or identity of the participant emails listed in `projects.txt` is permitted by your organization's allowed policy member domains constraint.

### 2. Required IAM Roles for the Provisioning `gcloud` User

#### Option A: Simplest Setup (Recommended for Sandbox / Lab Projects)
Grant your provisioning user account **`roles/owner`** on both the `role=admin` project and each `role=participant` project.

#### Option B: Granular / Least-Privilege Roles for the Provisioning User
If you cannot use `roles/owner`, the `gcloud` identity executing `./deploy.sh` requires the following roles **prior to running the script**:

| Scope | Required Pre-Flight IAM Role | Why It Is Needed |
| :--- | :--- | :--- |
| **Admin & Participant Projects** | `roles/editor` | Base creation and management of VPC networks, subnets, Cloud Routers/NAT, Compute addresses, Cloud Workstations, AlloyDB, Spanner, Cloud Run, and Artifact Registry. |
| **Admin & Participant Projects** | `roles/resourcemanager.projectIamAdmin` | Required by `deploy.py` and `main.tf` to grant IAM bindings to the Compute Engine default service account, lab runtime service accounts (`ecommerce-analyst-agent`, `lab-runner-sa`, `ge-publisher-sa`, etc.), the participant user (`iap_member`), and the deployer itself. |
| **Admin & Participant Projects** | `roles/serviceusage.serviceUsageAdmin` | Required to enable GCP APIs (`compute`, `workstations`, `alloydb`, `spanner`, `bigquery`, `dataplex`, `discoveryengine`, `run`, `cloudbuild`, etc.) and apply quota caps. |
| **Admin & Participant Projects** | `roles/storage.admin` | Required by `deploy.py` to create and manage the versioned Terraform state bucket (`gs://<project_id>-tfstate`) and the workstation asset bundle bucket (`gs://<project_id>-lab-assets`). |
| **Admin Project (`role=admin`)** | `roles/bigquery.resourceAdmin` | Required during Phase 0 (`00-admin-infra`) to create the shared BigQuery Enterprise Edition slot reservation (`graph-lab`) and during participant apply to create `google_bigquery_reservation_assignment` bindings against it. |

> **Note on Automated Deployer Self-Grants**: During `terraform apply` on each participant project, `main.tf` (`google_project_iam_member.deployer_roles`) automatically grants the active deploying identity four additional roles needed by the lab Terraform resources and waits 90 seconds for IAM propagation:
> * `roles/bigquery.admin` (to create datasets, connections, and data masking policies)
> * `roles/datacatalog.categoryAdmin` (to create policy tag taxonomies and fine-grained reader bindings)
> * `roles/iam.serviceAccountAdmin` (to create lab service accounts and bind `actAs` / token creator permissions)
> * `roles/secretmanager.admin` (to create and store the AlloyDB connection secret consumed by BigQuery federation)

---

## 🛠️ How to Deploy

### 1. Install Prerequisites
Ensure the following CLI tools are installed on your workstation or Cloud Shell environment:
* **Google Cloud SDK (`gcloud`)**
* **Terraform** (`>= 1.5`)
* **Python 3** (`>= 3.10`)

### 2. Authenticate with Google Cloud
Authenticate both the `gcloud` CLI and Application Default Credentials (ADC) with the provisioning user account:

```bash
gcloud auth login
gcloud auth application-default login
```

### 3. Configure `projects.txt`
`projects.txt` is intentionally ignored by Git (`.gitignore`) so real project IDs and user email addresses are never committed. Copy the template file to create your local mapping:

```bash
cp projects.txt.example projects.txt
```

Edit `projects.txt` and specify **1 Admin Project** (`role=admin`) and **at least 1 Participant Project** (`role=participant`):

```text
# Format: PROJECT_ID, IAP_MEMBER, REGION, ROLE
my-admin-project-id, user:your-admin@example.com, us-central1, role=admin
my-sandbox-project-01, user:participant@example.com, us-central1, role=participant
```

> **Tip — Deploying with a Single GCP Project**:
> Because the Admin Project uses a separate Terraform state prefix (`terraform/admin-state`) inside `gs://<project_id>-tfstate` while the Participant Project uses `terraform/state`, we recommend using two separate projects so teardown sequencing stays isolated. However, if you only have **one** GCP project for personal testing, you can list the same project ID on both lines (once with `role=admin` and once with `role=participant`).

> **Tip — Faster Spanner-Only Deployment**:
> Provisioning an AlloyDB cluster and primary instance takes ~15–20 minutes. If you want a faster deployment (~18 minutes quicker) to test Lab 1, Spanner Graph, and Lab 3 without AlloyDB, set `agentic_data_cloud_zero_etl_enable_alloydb = false` in `terraform.tfvars` before deploying.

### 4. Run the Provisioning Orchestrator
Launch the parallel provisioning engine:

```bash
./deploy.sh
```

What `./deploy.sh` does automatically:
1. **Step 0 (Pre-flight & Reachability)**: Validates your `gcloud` / ADC tokens, parses `projects.txt`, and probes every project to verify existence and IAM access.
2. **Step 1 (Provider Cache)**: Pre-warms the local Terraform provider plugin cache in `.terraform_plugin_cache/`.
3. **Phase 0 (Admin Project)**: Provisions `00-admin-infra` (the shared BigQuery Enterprise `graph-lab` slot reservation) in the `role=admin` project.
4. **Step 2 (State Buckets & Prerequisites)**: Enables core bootstrap APIs, configures the Compute Engine default service account, and creates `gs://<project_id>-tfstate` for each participant project.
5. **Step 3 (Parallel Participant Apply)**: Packages `labs/`, `bootstrapping.sh`, and `event-manifest.json` into `assets.zip`, runs `terraform apply` for each participant project in isolated `.deploy_workdirs/<project_id>/` workspaces, verifies Cloud Workstation config health, and writes detailed logs to `deploy_logs/`.

### 5. Useful Orchestrator & Workstation Commands

```bash
# Preview Terraform changes across participant projects (read-only)
./deploy.sh --plan

# Verify Cloud Workstation config health and optionally test-start stopped VMs
./deploy.sh --check-workstations
./deploy.sh --check-workstations --start

# Pre-warm all participant Cloud Workstations and set an 8-hour idle timeout for event day
./warmup.sh

# Check live status of all Cloud Workstations
./warmup.sh --status

# Stop running Cloud Workstations to save compute costs without deleting data
./warmup.sh --stop
```

### 6. Teardown / Clean Up Resources
To avoid ongoing cloud charges (especially from AlloyDB, Cloud Spanner, and Cloud Workstations) after you finish exploring the labs, destroy all provisioned infrastructure:

```bash
./deploy.sh --destroy
```

This destroys all participant project resources in parallel first, cleans up their remote state buckets, and then destroys the shared Admin Project reservation once no participant project references it.
