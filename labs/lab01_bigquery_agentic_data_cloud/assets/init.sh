#!/usr/bin/env bash
# Author: Wissem Khlifi - August 2026
# ==============================================================================
# Lab 10 — one-time environment bootstrap
#
#   bash init.sh
#
# Terraform already built the data infrastructure and both service accounts.
# This script prepares everything deploy.sh needs on top of it: APIs, the
# Artifact Registry repository, the act-as permissions, and two generated
# config files. It validates far more than it creates, because in this lab a
# missing IAM binding surfaces much later as a Cloud Run error that names a
# permission and never the cause.
#
# Safe to re-run.
# ==============================================================================
set -uo pipefail

if [ -z "${BASH_VERSION:-}" ]; then
    echo "Run with bash, not sh:  bash $0" >&2
    exit 1
fi

cd "$(dirname "${BASH_SOURCE[0]}")"
SRC_DIR="$(pwd)"

C_CYAN=$'\033[36m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'
C_RED=$'\033[31m';  C_BOLD=$'\033[1m';   C_OFF=$'\033[0m'
say()  { printf '\n%s%s--- %s%s\n' "${C_CYAN}" "${C_BOLD}" "$1" "${C_OFF}"; }
ok()   { printf '  %s✓%s %s\n' "${C_GREEN}" "${C_OFF}" "$1"; }
warn() { printf '  %s⚠%s  %s\n' "${C_YELLOW}" "${C_OFF}" "$1"; }
die()  { printf '\n%s✗ %s%s\n\n' "${C_RED}" "$1" "${C_OFF}"; exit 1; }

REGION="${CLOUD_RUN_REGION:-us-central1}"
AR_REPO="${AR_REPO_ID:-lab10}"
BQ_DATASET_ID="${BQ_DATASET_ID:-ecommerce_lab}"
BQ_GRAPH_DATASET_ID="${BQ_GRAPH_DATASET_ID:-ecommerce_graph_lab}"
GLOSSARY_LOCATION="${GLOSSARY_LOCATION:-us-central1}"

# ==============================================================================
say "1. Prerequisites"
# ==============================================================================
command -v gcloud >/dev/null || die "gcloud not found."
command -v python3 >/dev/null || die "python3 not found."
ok "gcloud, python3"

# Access tokens to try against REST APIs, one per line: the gcloud CLI account
# first, then ADC. Some accounts get a CLI token that REST APIs reject
# (ACCESS_TOKEN_TYPE_UNSUPPORTED) while their ADC token works.
candidate_tokens() {
    gcloud auth print-access-token 2>/dev/null || true
    gcloud auth application-default print-access-token 2>/dev/null || true
}

# `gcloud config get-value account` reports CONFIGURATION, not credential state.
# A Cloud Workstation image can ship with account already set, so it answers with
# an address while the credential store is empty -- the login below is skipped and
# every gcloud/bq call after it fails with "You do not currently have an active
# account selected". Ask the credential store itself.
active_account() {
    gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null | head -n 1
}

# Any credential gcloud holds, whether or not it is the selected one.
stored_account() {
    gcloud auth list --format='value(account)' 2>/dev/null | head -n 1
}

ACCOUNT="$(active_account || true)"

if [ -z "${ACCOUNT}" ]; then
    # A workstation restart leaves the credential on disk but DESELECTS it:
    # `gcloud auth list` then shows the account with an empty status column, and
    # everything behaves as though you never signed in. Reselecting costs one
    # local command. Sending a participant to a browser for a credential they
    # already have costs them the flow, on every restart.
    STORED="$(stored_account || true)"
    if [ -n "${STORED}" ]; then
        printf '  Reselecting stored credential %s ...\n' "${STORED}"
        gcloud config set account "${STORED}" >/dev/null 2>&1 || true
        # Selecting a revoked or expired credential still makes it "active"
        # while every call keeps failing, so prove it can mint a token before
        # trusting it. If it cannot, fall through to a real sign-in.
        if gcloud auth print-access-token >/dev/null 2>&1; then
            ACCOUNT="$(active_account || true)"
        else
            printf '  Stored credential is no longer valid.\n'
        fi
    fi
fi

if [ -z "${ACCOUNT}" ]; then
    printf '  Authenticating...\n'
    gcloud auth login --update-adc || die "Sign-in failed."
    ACCOUNT="$(active_account || true)"
fi
[ -n "${ACCOUNT}" ] || die \
"Signed in, but gcloud still reports no active account. Run:
  gcloud auth login --update-adc"
ok "signed in as ${ACCOUNT}"

# The Terraform provider and every client library read ADC. Without it, the
# first thing that fails is a build, and the error talks about credentials
# rather than about the login you never completed.
if [ ! -f "${HOME}/.config/gcloud/application_default_credentials.json" ]; then
    printf '  Application Default Credentials missing — starting login...\n'
    gcloud auth application-default login || die "ADC login failed."
fi
ok "application default credentials present"

# Impersonation left over from an earlier run would make everything below act as
# a read-only service account.
IMP="$(gcloud config get-value auth/impersonate_service_account 2>/dev/null || true)"
if [ -n "${IMP}" ] && [ "${IMP}" != "(unset)" ]; then
    gcloud config unset auth/impersonate_service_account >/dev/null 2>&1
    ok "cleared leftover CLI impersonation (${IMP})"
fi

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
[ -n "${PROJECT_ID}" ] && [ "${PROJECT_ID}" != "(unset)" ] || die \
"No project set:
  gcloud config set project <your-lab-project>"
ok "project ${PROJECT_ID}"

PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)' 2>/dev/null || true)"
[ -n "${PROJECT_NUMBER}" ] || die "Cannot read project ${PROJECT_ID}. Wrong project id, or no access."

AGENT_SA="ecommerce-analyst-agent@${PROJECT_ID}.iam.gserviceaccount.com"
RUNNER_SA="lab-runner-sa@${PROJECT_ID}.iam.gserviceaccount.com"

# ==============================================================================
say "2. APIs"
# ==============================================================================
gcloud services enable \
    run.googleapis.com \
    cloudbuild.googleapis.com \
    artifactregistry.googleapis.com \
    bigquery.googleapis.com \
    dataplex.googleapis.com \
    datacatalog.googleapis.com \
    aiplatform.googleapis.com \
    --project="${PROJECT_ID}" --quiet >/dev/null 2>&1 \
    && ok "required APIs enabled" \
    || warn "could not enable every API — continuing; Terraform may already have."

# ==============================================================================
say "3. Service accounts (Terraform owns these)"
# ==============================================================================
for SA in "${AGENT_SA}" "${RUNNER_SA}"; do
    if gcloud iam service-accounts describe "${SA}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
        ok "$(echo "${SA}" | cut -d@ -f1) exists"
    else
        die "Service account ${SA} not found.
  Terraform creates it. Apply the lab infrastructure first, then re-run this."
    fi
done

# The masking demonstration is only meaningful if these two grants are exactly
# as Terraform declared them, so check rather than assume.
TAX="$(gcloud data-catalog taxonomies list --location=us --project="${PROJECT_ID}" \
        --filter="displayName='PII Classification - ${PROJECT_ID}'" \
        --format='value(name)' 2>/dev/null || true)"
if [ -n "${TAX}" ]; then
    PT="$(gcloud data-catalog taxonomies policy-tags list --taxonomy="${TAX}" \
          --format='value(name)' 2>/dev/null | head -1)"
    POLICY="$(gcloud data-catalog taxonomies policy-tags get-iam-policy "${PT}" \
              --format=json 2>/dev/null || echo '{}')"
    echo "${POLICY}" | grep -q "${RUNNER_SA}" \
        && ok "lab-runner holds Fine-Grained Reader (sees real email)" \
        || warn "lab-runner does NOT hold Fine-Grained Reader — Step 7 will show masked values on BOTH sides. Re-apply Terraform."
    echo "${POLICY}" | grep -q "${AGENT_SA}" \
        && warn "the AGENT holds Fine-Grained Reader — Step 7 will show real email on BOTH sides. Remove that binding; the agent must have maskedReader only." \
        || ok "agent does not hold Fine-Grained Reader (correct — it gets masked values)"
else
    warn "taxonomy 'PII Classification - ${PROJECT_ID}' not found; Step 7 cannot work until Terraform is applied."
fi

# ==============================================================================
say "4. Artifact Registry"
# ==============================================================================
if gcloud artifacts repositories describe "${AR_REPO}" --location="${REGION}" \
     --project="${PROJECT_ID}" >/dev/null 2>&1; then
    ok "repository ${AR_REPO} exists in ${REGION}"
else
    gcloud artifacts repositories create "${AR_REPO}" \
        --repository-format=docker --location="${REGION}" \
        --description="Lab 10 agent and lab-runner images" \
        --project="${PROJECT_ID}" --quiet >/dev/null 2>&1 \
        && ok "created repository ${AR_REPO}" \
        || die "Could not create the Artifact Registry repository."
fi

# Cloud Build and the compute default identity push images into it. Terraform
# grants them roles/artifactregistry.writer (infra/main.tf,
# build_identities_ar_writer); nothing to do here.
ok "build identities: artifactregistry.writer granted by Terraform"

# ==============================================================================
say "5. Deploy permissions"
# ==============================================================================
# Cloud Run refuses `serviceAccountName` unless the deployer may act as that
# account. Terraform grants it (participant_acts_as_*). This only VERIFIES, so a
# partial apply shows up here by name rather than as an opaque
# iam.serviceAccounts.actAs failure during deploy.sh.
for SA in "${AGENT_SA}" "${RUNNER_SA}"; do
    if gcloud iam service-accounts get-iam-policy "${SA}" --project="${PROJECT_ID}" \
        --flatten="bindings[].members" \
        --filter="bindings.role=roles/iam.serviceAccountUser AND bindings.members=user:${ACCOUNT}" \
        --format="value(bindings.role)" 2>/dev/null | grep -q .; then
        ok "you may act as ${SA%%@*}"
    else
        warn "no serviceAccountUser on ${SA%%@*} for ${ACCOUNT}. Ask the facilitator to re-run deploy.sh apply."
    fi
done

# ==============================================================================
say "6. MCP configuration for agy"
# ==============================================================================
# init.sh is the SINGLE owner of mcp_config.json. Two other mechanisms look as
# though they could produce it, and neither can:
#
#   Terraform -- the scaffolding engine seals and uploads assets.zip BEFORE
#   `terraform apply` runs, so a local_file written during apply lands in the
#   deployment machine's ephemeral workdir and never reaches a workstation.
#
#   The workstation bootstrapper -- it renders lab.yaml's
#   antigravity.mcp_servers verbatim and has no project id to substitute, and
#   the engine's lab.yaml parser accepts only [A-Za-z0-9_] in keys, so
#   `x-goog-user-project` cannot even be written there. That header is not
#   optional: both endpoints bill and quota against a project, and without it a
#   user-credential call is refused with "project not specified" even though the
#   URL names one.
#
# Here -- on the workstation, after the project id is known -- is the only place
# all three facts hold at once. Rewritten on every run, so the config always
# matches the project you are actually in.

BIGQUERY_MCP_URL="https://bigquery.googleapis.com/mcp"
CATALOG_MCP_URL="https://dataplex.googleapis.com/mcp"
MCP_DIRS=("${HOME}/.gemini/config" "${HOME}/.gemini/antigravity-cli")

# PROJECT_ID was resolved in section 1. Re-assert it here: a config carrying an
# empty project id does not error, it comes back as an empty search result.
[ -n "${PROJECT_ID}" ] && [ "${PROJECT_ID}" != "(unset)" ] || die \
"No project id, so the agy MCP configuration cannot be written:
  gcloud config set project <your-lab-project>"

for DIR in "${MCP_DIRS[@]}"; do
    mkdir -p "${DIR}" || die "Cannot create ${DIR}"
    cat > "${DIR}/mcp_config.json" <<MCPEOF
{
  "mcpServers": {
    "bigquery": {
      "serverUrl": "${BIGQUERY_MCP_URL}",
      "authProviderType": "google_credentials",
      "headers": {"x-goog-user-project": "${PROJECT_ID}"}
    },
    "knowledge-catalog": {
      "serverUrl": "${CATALOG_MCP_URL}",
      "authProviderType": "google_credentials",
      "headers": {"x-goog-user-project": "${PROJECT_ID}"}
    }
  }
}
MCPEOF
done

# Read every file back rather than trusting the write. Both servers must be
# present AND carry this project's header -- a stale config from an earlier run
# in a different project is the one failure that still looks like success.
for DIR in "${MCP_DIRS[@]}"; do
    CFG="${DIR}/mcp_config.json"
    [ -f "${CFG}" ] || die "Failed to write ${CFG}."
    python3 - "${CFG}" "${PROJECT_ID}" <<'PYEOF' || die "\
${CFG} is not the configuration this lab needs.
  Delete it and re-run: rm -f ${CFG} && bash init.sh"
import json, sys

cfg_path, want_project = sys.argv[1], sys.argv[2]
try:
    servers = json.load(open(cfg_path)).get("mcpServers", {})
except (OSError, ValueError):
    sys.exit(1)

if not {"bigquery", "knowledge-catalog"} <= set(servers):
    sys.exit(1)
for server in servers.values():
    if server.get("headers", {}).get("x-goog-user-project") != want_project:
        sys.exit(1)
PYEOF
    ok "wrote ${CFG}"
done

# Reachability is cheap, and the failure is otherwise silent: agy finds nothing
# and reports that the term does not exist. A warning, not a failure -- a
# transient non-answer from a remote MCP server is not a broken lab.
# The first token tools/list accepts is used for every server.
TOKEN=""
while IFS= read -r CANDIDATE; do
    [ -n "${CANDIDATE}" ] || continue
    if curl -s --max-time 30 -X POST "${BIGQUERY_MCP_URL}" \
        -H "Authorization: Bearer ${CANDIDATE}" -H "Content-Type: application/json" \
        -H "Accept: application/json, text/event-stream" -H "x-goog-user-project: ${PROJECT_ID}" \
        -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' 2>/dev/null \
        | sed 's/^data: //' \
        | python3 -c 'import json,sys; sys.exit(0 if "result" in json.load(sys.stdin) else 1)' 2>/dev/null; then
        TOKEN="${CANDIDATE}"; break
    fi
    TOKEN="${TOKEN:-${CANDIDATE}}"
done < <(candidate_tokens)
if [ -n "${TOKEN}" ]; then
    for PAIR in "BigQuery|${BIGQUERY_MCP_URL}" \
                "Knowledge Catalog|${CATALOG_MCP_URL}"; do
        LABEL="${PAIR%%|*}"; URL="${PAIR#*|}"
        COUNT="$(curl -s --max-time 30 -X POST "${URL}" \
            -H "Authorization: Bearer ${TOKEN}" \
            -H "Content-Type: application/json" \
            -H "Accept: application/json, text/event-stream" \
            -H "x-goog-user-project: ${PROJECT_ID}" \
            -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' 2>/dev/null \
            | python3 -c "import json,sys; print(len(json.load(sys.stdin)['result']['tools']))" 2>/dev/null || echo 0)"
        [ "${COUNT}" -gt 0 ] 2>/dev/null \
            && ok "${LABEL} MCP server answers (${COUNT} tools)" \
            || warn "${LABEL} MCP server did not return a tool list — retry, then check your token."
    done
else
    warn "could not mint an access token; skipped the MCP reachability check"
fi

printf '\n  %sagy is now configured:%s\n' "${C_BOLD}" "${C_OFF}"
printf '    project            %s\n' "${PROJECT_ID}"
printf '    bigquery           %s\n' "${BIGQUERY_MCP_URL}"
printf '    knowledge-catalog  %s\n' "${CATALOG_MCP_URL}"
printf '    written to         %s/mcp_config.json\n' "${MCP_DIRS[@]}"

# ==============================================================================
say "7. Generated configuration"
# ==============================================================================
cat > "${SRC_DIR}/.deploy.env" <<EOF
# Generated by init.sh. Sourced by deploy.sh. Safe to delete and regenerate.
PROJECT_ID=${PROJECT_ID}
PROJECT_NUMBER=${PROJECT_NUMBER}
REGION=${REGION}
AR_REPO=${AR_REPO}
AGENT_SA=${AGENT_SA}
RUNNER_SA=${RUNNER_SA}
AGENT_SERVICE=ecommerce-agent
RUNNER_SERVICE=lab-runner
BQ_DATASET_ID=${BQ_DATASET_ID}
BQ_GRAPH_DATASET_ID=${BQ_GRAPH_DATASET_ID}
GLOSSARY_LOCATION=${GLOSSARY_LOCATION}
EOF
ok "wrote .deploy.env"

# For running the agent locally with `adk web` while you iterate on the TODOs.
# Cloud Run ignores this file entirely -- service.yaml supplies the deployed
# environment.
cat > "${SRC_DIR}/agent/.env" <<EOF
GOOGLE_GENAI_USE_VERTEXAI=true
GOOGLE_CLOUD_LOCATION=global
GOOGLE_CLOUD_PROJECT=${PROJECT_ID}
EOF
ok "wrote agent/.env (local runs only)"

# ==============================================================================
say "Ready"
# ==============================================================================
cat <<NEXT

  ${C_BOLD}agy can now reach BigQuery and the Knowledge Catalog.${C_OFF} That is what this
  script just configured, and it is why it runs before you use agy at all --
  without it agy has no MCP servers, which does not error: it simply finds
  nothing and reports that glossary terms do not exist.

  Continue with the user guide from Step 1. When you reach the two TODOs in
  ${C_BOLD}agent/agent.py${C_OFF} -- the lab's actual work -- agy writes most of the code.

  Later, at Step 6, deploy both services:

      ${C_BOLD}bash deploy.sh${C_OFF}

  This script is safe to re-run at any point, and re-running it is the fix if
  you switch projects: the MCP configuration is rewritten with the new id.

NEXT
