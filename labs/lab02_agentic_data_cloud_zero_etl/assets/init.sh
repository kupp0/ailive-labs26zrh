#!/usr/bin/env bash
# Author: Wissem Khlifi - August 2026
# ==============================================================================
# Lab 11 — one-time environment bootstrap
#
#   bash init.sh
#
# Terraform built three engines, three service accounts and the federation.
# This script prepares everything deploy.sh needs on top of that, and performs
# the two IAM grants Terraform structurally cannot (section 6).
#
# It validates far more than it creates, because in this lab a missing binding
# surfaces much later as a Cloud Run error that names a permission and never the
# cause.
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
AR_REPO="${AR_REPO_ID:-lab11}"
BQ_SOURCE_DATASET_ID="${BQ_SOURCE_DATASET_ID:-ecommerce_lab}"
BQ_ZEROETL_DATASET_ID="${BQ_ZEROETL_DATASET_ID:-ecommerce_zeroetl}"
BQ_SPANNER_DATASET_ID="${BQ_SPANNER_DATASET_ID:-spanner_federated}"
SPANNER_INSTANCE="${SPANNER_INSTANCE:-zeroetl-ops}"
SPANNER_DATABASE="${SPANNER_DATABASE:-ecommerce-operational}"
ALLOYDB_REGION="${ALLOYDB_REGION:-us-central1}"
ALLOYDB_CLUSTER="${ALLOYDB_CLUSTER:-zeroetl-cluster}"
ALLOYDB_INSTANCE="${ALLOYDB_INSTANCE:-zeroetl-primary}"
ALLOYDB_DATABASE="${ALLOYDB_DATABASE:-postgres}"
GLOSSARY_LOCATION="${GLOSSARY_LOCATION:-us-central1}"

# ==============================================================================
say "1. Prerequisites"
# ==============================================================================
command -v gcloud  >/dev/null || die "gcloud not found."
command -v python3 >/dev/null || die "python3 not found."
command -v bq      >/dev/null || die "bq not found (it ships with gcloud)."
ok "gcloud, bq, python3"

# Access tokens to try against REST APIs, one per line: the gcloud CLI account
# first (the identity the participant logged in as), then ADC. Some accounts
# get a CLI token that REST APIs reject (ACCESS_TOKEN_TYPE_UNSUPPORTED) while
# their ADC token works, so a caller tries each until one is accepted.
candidate_tokens() {
    gcloud auth print-access-token 2>/dev/null || true
    gcloud auth application-default print-access-token 2>/dev/null || true
}

# Exit 0 if stdin is a JSON object without an "error" key.
is_ok_json() {
    python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if isinstance(d, dict) and "error" not in d else 1)' 2>/dev/null
}

# `gcloud config get-value account` reports CONFIGURATION, not credential state.
# A Cloud Workstation image can ship with account already set, so it answers with
# an address while the credential store is empty. Ask the credential store.
active_account() {
    gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null | head -n 1
}
stored_account() {
    gcloud auth list --format='value(account)' 2>/dev/null | head -n 1
}

ACCOUNT="$(active_account || true)"
if [ -z "${ACCOUNT}" ]; then
    # A workstation restart leaves the credential on disk but DESELECTS it.
    # Reselecting costs one local command; sending a participant to a browser for
    # a credential they already have costs them the flow, on every restart.
    STORED="$(stored_account || true)"
    if [ -n "${STORED}" ]; then
        printf '  Reselecting stored credential %s ...\n' "${STORED}"
        gcloud config set account "${STORED}" >/dev/null 2>&1 || true
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

if [ ! -f "${HOME}/.config/gcloud/application_default_credentials.json" ]; then
    printf '  Application Default Credentials missing — starting login...\n'
    gcloud auth application-default login || die "ADC login failed."
fi
ok "application default credentials present"

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

AGENT_SA="lab11-dispatch-sa@${PROJECT_ID}.iam.gserviceaccount.com"
TRANSACT_SA="lab11-transact-sa@${PROJECT_ID}.iam.gserviceaccount.com"
RUNNER_SA="lab11-runner-sa@${PROJECT_ID}.iam.gserviceaccount.com"

# ==============================================================================
say "2. APIs"
# ==============================================================================
gcloud services enable \
    run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
    bigquery.googleapis.com bigqueryconnection.googleapis.com \
    alloydb.googleapis.com spanner.googleapis.com \
    dataplex.googleapis.com datacatalog.googleapis.com \
    secretmanager.googleapis.com aiplatform.googleapis.com \
    --project="${PROJECT_ID}" --quiet >/dev/null 2>&1 \
    && ok "required APIs enabled" \
    || warn "could not enable every API — continuing; Terraform may already have."

# ==============================================================================
say "3. The three identities (Terraform owns these)"
# ==============================================================================
for SA in "${AGENT_SA}" "${TRANSACT_SA}" "${RUNNER_SA}"; do
    if gcloud iam service-accounts describe "${SA}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
        ok "$(echo "${SA}" | cut -d@ -f1) exists"
    else
        die "Service account ${SA} not found.
  Terraform creates it. Apply the lab infrastructure first, then re-run this."
    fi
done

# ==============================================================================
say "4. Engines"
# ==============================================================================
SPANNER_STATE="$(gcloud spanner databases describe "${SPANNER_DATABASE}" \
    --instance="${SPANNER_INSTANCE}" --project="${PROJECT_ID}" \
    --format='value(state)' 2>/dev/null || true)"
[ "${SPANNER_STATE}" = "READY" ] \
    && ok "Spanner ${SPANNER_INSTANCE}/${SPANNER_DATABASE} is READY" \
    || warn "Spanner database is '${SPANNER_STATE:-missing}' — the graph panels will fail until it is READY."

ALLOYDB_ENABLED="false"
if gcloud alloydb clusters describe "${ALLOYDB_CLUSTER}" --region="${ALLOYDB_REGION}" \
     --project="${PROJECT_ID}" >/dev/null 2>&1; then
    ALLOYDB_ENABLED="true"
    ok "AlloyDB cluster ${ALLOYDB_CLUSTER} exists in ${ALLOYDB_REGION}"
else
    warn "AlloyDB cluster ${ALLOYDB_CLUSTER} not found. If this event was provisioned
     with enable_alloydb = false that is expected, and the lab runs on
     BigQuery + Spanner. Otherwise, check the Terraform apply log."
fi

# The password is only needed to inspect the connection by hand; the BigQuery
# connection already holds its own copy, and no agent ever sees it.
if [ "${ALLOYDB_ENABLED}" = "true" ]; then
    if gcloud secrets versions access latest --secret=lab11-alloydb-password \
         --project="${PROJECT_ID}" >/dev/null 2>&1; then
        ok "AlloyDB password is in Secret Manager (lab11-alloydb-password)"
    else
        warn "could not read secret lab11-alloydb-password — you will not be able to
     open the AlloyDB connection by hand, but nothing in the lab needs it."
    fi
fi

# ==============================================================================
say "5. Column-level security grants (From Data to Agent policy tag, this lab's identities)"
# ==============================================================================
# These two grants are performed HERE, not in Terraform, and the reason is worth
# reading because it is a genuine limitation rather than a preference:
#
#   The policy-tag grant needs the tag's full resource name, which embeds a
#   NUMERIC taxonomy id generated during LAB 10's apply. The Google provider
#   ships no data source for Data Catalog taxonomies, so Terraform has nothing to
#   look it up with.
#
#   The data-policy grant has a static id, but the scaffolding engine gives Lab
#   11's module no way to order itself after Lab 10's (it wires only
#   depends_on = [module.core]), so a binding applied first fails with a 404.
#
# Here, on the workstation, after everything is applied, both are discoverable.
TAXONOMY="$(gcloud data-catalog taxonomies list --location=us --project="${PROJECT_ID}" \
    --filter="displayName='PII Classification - ${PROJECT_ID}'" \
    --format='value(name)' 2>/dev/null || true)"

if [ -z "${TAXONOMY}" ]; then
    warn "The From Data to Agent taxonomy 'PII Classification - ${PROJECT_ID}' was not found.
     The column-masking panel cannot work until the From Data to Agent infrastructure is
     applied. Everything else in this lab is unaffected."
else
    POLICY_TAG="$(gcloud data-catalog taxonomies policy-tags list --taxonomy="${TAXONOMY}" \
        --format='value(name)' 2>/dev/null | head -1)"
    if [ -n "${POLICY_TAG}" ]; then
        # The lab runner stands in for the participant and must see REAL values.
        # IAM-EXCEPTION: the policy tag is Lab 10's; its numeric id exists only after Lab 10's apply and no Terraform data source can look it up (infra/main.tf section 10).
        gcloud data-catalog taxonomies policy-tags add-iam-policy-binding "${POLICY_TAG}" \
            --member="serviceAccount:${RUNNER_SA}" \
            --role=roles/datacatalog.categoryFineGrainedReader \
            --quiet >/dev/null 2>&1 \
            && ok "lab-runner granted Fine-Grained Reader (sees real email)" \
            || warn "could not grant Fine-Grained Reader to the lab runner"

        # The dispatch agent must NOT hold it. Fine-Grained Reader beats masking,
        # so granting both would show unmasked values on both sides and prove
        # nothing. Check rather than assume.
        if gcloud data-catalog taxonomies policy-tags get-iam-policy "${POLICY_TAG}" \
             --format=json 2>/dev/null | grep -q "${AGENT_SA}"; then
            warn "the DISPATCH agent holds Fine-Grained Reader — the masking panel will
     show real values on BOTH sides. Remove that binding; it must have
     maskedReader only."
        else
            ok "dispatch agent does not hold Fine-Grained Reader (correct)"
        fi
    fi

    # The masked read, granted over REST rather than with gcloud.
    #
    # There is no `gcloud bigquery data-policies` command group — verified
    # against the CLI, which answers "Invalid choice: 'bigquery'". A gcloud call
    # here would fail every time and, because that failure is tolerated, the
    # dispatch agent would silently end up with NEITHER maskedReader nor
    # fineGrainedReader. That is worse than a missing grant: the agent is then
    # refused the column outright, and the masking panel shows an access error
    # rather than a masked value — which reads as a bug in the lab instead of an
    # unfinished binding.
    #
    # The bigquerydatapolicy API exposes the standard IAM verbs, so this is an
    # ordinary read-modify-write: getIamPolicy, add the member if it is not
    # already there, setIamPolicy with the etag that came back.
    DP_URL="https://bigquerydatapolicy.googleapis.com/v1/projects/${PROJECT_ID}/locations/us/dataPolicies/sensitive_email_mask"
    TOKEN=""; CURRENT_POLICY=""; ANY_TOKEN=0
    while IFS= read -r CANDIDATE; do
        [ -n "${CANDIDATE}" ] || continue
        ANY_TOKEN=1
        RESP="$(curl -s --max-time 30 -X POST "${DP_URL}:getIamPolicy" \
            -H "Authorization: Bearer ${CANDIDATE}" \
            -H "Content-Type: application/json" -d '{}' 2>/dev/null || echo '')"
        if printf '%s' "${RESP}" | is_ok_json; then
            TOKEN="${CANDIDATE}"; CURRENT_POLICY="${RESP}"; break
        fi
        CURRENT_POLICY="${RESP}"
    done < <(candidate_tokens)

    if [ "${ANY_TOKEN}" -eq 0 ]; then
        warn "no access token; skipped the Masked Reader grant"
    else

        UPDATED="$(AGENT_SA="${AGENT_SA}" CURRENT_POLICY="${CURRENT_POLICY}" python3 - <<'PYGRANT'
import json, os, sys

member = "serviceAccount:" + os.environ["AGENT_SA"]
role = "roles/bigquerydatapolicy.maskedReader"

try:
    policy = json.loads(os.environ.get("CURRENT_POLICY") or "")
except ValueError:
    sys.exit(1)
if not isinstance(policy, dict) or "error" in policy:
    sys.exit(1)

bindings = policy.setdefault("bindings", [])
for binding in bindings:
    if binding.get("role") == role:
        if member in binding.get("members", []):
            sys.exit(3)                     # already bound; nothing to change
        binding.setdefault("members", []).append(member)
        break
else:
    bindings.append({"role": role, "members": [member]})

print(json.dumps({"policy": policy}))
PYGRANT
)"
        RC=$?

        if [ "${RC}" -eq 3 ]; then
            ok "dispatch agent already holds Masked Reader"
        elif [ "${RC}" -ne 0 ] || [ -z "${UPDATED}" ]; then
            warn "could not read the data policy 'sensitive_email_mask'.
     From Data to Agent creates it; until its infrastructure is applied, the
     column-masking panel cannot work."
        else
            # IAM-EXCEPTION: the data policy is owned by another lab (Lab 10); see infra/main.tf section 10.
            CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 \
                -X POST "${DP_URL}:setIamPolicy" \
                -H "Authorization: Bearer ${TOKEN}" \
                -H "Content-Type: application/json" \
                -d "${UPDATED}" 2>/dev/null || echo 000)"
            [ "${CODE}" = "200" ] \
                && ok "dispatch agent granted Masked Reader (sees XXXXX@)" \
                || warn "setIamPolicy on the data policy returned HTTP ${CODE}.
     Grant it by hand: BigQuery > Policy tags > sensitive_email_mask >
     Masked Reader > add ${AGENT_SA}"
        fi
    fi
fi

# ==============================================================================
say "6. Artifact Registry and build identities"
# ==============================================================================
if gcloud artifacts repositories describe "${AR_REPO}" --location="${REGION}" \
     --project="${PROJECT_ID}" >/dev/null 2>&1; then
    ok "repository ${AR_REPO} exists in ${REGION}"
else
    gcloud artifacts repositories create "${AR_REPO}" \
        --repository-format=docker --location="${REGION}" \
        --description="Lab 11 agent, transact and lab-runner images" \
        --project="${PROJECT_ID}" --quiet >/dev/null 2>&1 \
        && ok "created repository ${AR_REPO}" \
        || die "Could not create the Artifact Registry repository."
fi

# Cloud Build and the compute default identity push images into it. Lab 10's
# Terraform grants them roles/artifactregistry.writer (build_identities_ar_writer);
# Lab 11 depends on Lab 10, so nothing to do here.
ok "build identities: artifactregistry.writer granted by Terraform"

# Cloud Run refuses `serviceAccountName` unless the deployer may act as that
# account. Terraform grants it (participant_acts_as_*). This only VERIFIES, so a
# partial apply shows up here by name rather than as an opaque
# iam.serviceAccounts.actAs failure during deploy.sh.
for SA in "${AGENT_SA}" "${TRANSACT_SA}" "${RUNNER_SA}"; do
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
say "7. MCP configuration for agy — four servers"
# ==============================================================================
# init.sh is the SINGLE owner of mcp_config.json. Two other mechanisms look as
# though they could produce it and neither can:
#
#   Terraform — the scaffolding engine seals and uploads assets.zip BEFORE
#   `terraform apply` runs, so a local_file written during apply lands in the
#   deployment machine's workdir and never reaches a workstation.
#
#   The workstation bootstrapper — it renders lab.yaml's antigravity.mcp_servers
#   verbatim, has no project id to substitute, and the engine's lab.yaml parser
#   accepts only [A-Za-z0-9_] in keys, so `x-goog-user-project` cannot be written
#   there at all. That header is not optional: every one of these endpoints bills
#   and quotas against a project, and without it a user-credential call is
#   refused with "project not specified" even though the URL names one.
BIGQUERY_MCP_URL="https://bigquery.googleapis.com/mcp"
CATALOG_MCP_URL="https://dataplex.googleapis.com/mcp"
SPANNER_MCP_URL="https://spanner.googleapis.com/mcp"
ALLOYDB_MCP_URL="https://alloydb.googleapis.com/mcp"
MCP_DIRS=("${HOME}/.gemini/config" "${HOME}/.gemini/antigravity-cli")

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
    },
    "spanner": {
      "serverUrl": "${SPANNER_MCP_URL}",
      "authProviderType": "google_credentials",
      "headers": {"x-goog-user-project": "${PROJECT_ID}"}
    },
    "alloydb": {
      "serverUrl": "${ALLOYDB_MCP_URL}",
      "authProviderType": "google_credentials",
      "headers": {"x-goog-user-project": "${PROJECT_ID}"}
    }
  }
}
MCPEOF
done

# Read every file back rather than trusting the write. A stale config from an
# earlier run in a different project is the one failure that looks like success.
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

required = {"bigquery", "knowledge-catalog", "spanner", "alloydb"}
if not required <= set(servers):
    sys.exit(1)
for server in servers.values():
    if server.get("headers", {}).get("x-goog-user-project") != want_project:
        sys.exit(1)
PYEOF
    ok "wrote ${CFG}"
done

# Reachability is cheap and the failure is otherwise silent: agy simply finds
# nothing and reports that an object does not exist. A warning, not a failure —
# a transient non-answer from a remote MCP server is not a broken lab.
# The first token tools/list accepts is used for every server.
TOKEN=""
while IFS= read -r CANDIDATE; do
    [ -n "${CANDIDATE}" ] || continue
    if curl -s --max-time 30 -X POST "${BIGQUERY_MCP_URL}" \
        -H "Authorization: Bearer ${CANDIDATE}" -H "Content-Type: application/json" \
        -H "Accept: application/json, text/event-stream" -H "x-goog-user-project: ${PROJECT_ID}" \
        -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' 2>/dev/null \
        | sed 's/^data: //' | is_ok_json; then
        TOKEN="${CANDIDATE}"; break
    fi
    TOKEN="${TOKEN:-${CANDIDATE}}"
done < <(candidate_tokens)
if [ -n "${TOKEN}" ]; then
    for PAIR in "BigQuery|${BIGQUERY_MCP_URL}" \
                "Knowledge Catalog|${CATALOG_MCP_URL}" \
                "Spanner|${SPANNER_MCP_URL}" \
                "AlloyDB|${ALLOYDB_MCP_URL}"; do
        LABEL="${PAIR%%|*}"; URL="${PAIR#*|}"
        COUNT="$(curl -s --max-time 30 -X POST "${URL}" \
            -H "Authorization: Bearer ${TOKEN}" \
            -H "Content-Type: application/json" \
            -H "Accept: application/json, text/event-stream" \
            -H "x-goog-user-project: ${PROJECT_ID}" \
            -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' 2>/dev/null \
            | sed 's/^data: //' \
            | python3 -c "import json,sys; print(len(json.load(sys.stdin)['result']['tools']))" 2>/dev/null || echo 0)"
        [ "${COUNT}" -gt 0 ] 2>/dev/null \
            && ok "${LABEL} MCP server answers (${COUNT} tools)" \
            || warn "${LABEL} MCP server did not return a tool list — retry, then check your token."
    done
else
    warn "could not mint an access token; skipped the MCP reachability check"
fi

# ==============================================================================
say "8. Generated configuration"
# ==============================================================================
cat > "${SRC_DIR}/.deploy.env" <<EOF
# Generated by init.sh. Sourced by deploy.sh. Safe to delete and regenerate.
PROJECT_ID=${PROJECT_ID}
PROJECT_NUMBER=${PROJECT_NUMBER}
REGION=${REGION}
AR_REPO=${AR_REPO}
AGENT_SA=${AGENT_SA}
TRANSACT_SA=${TRANSACT_SA}
RUNNER_SA=${RUNNER_SA}
AGENT_SERVICE=lab11-agent
TRANSACT_SERVICE=lab11-transact
RUNNER_SERVICE=lab11-runner
BQ_SOURCE_DATASET_ID=${BQ_SOURCE_DATASET_ID}
BQ_ZEROETL_DATASET_ID=${BQ_ZEROETL_DATASET_ID}
BQ_SPANNER_DATASET_ID=${BQ_SPANNER_DATASET_ID}
SPANNER_INSTANCE=${SPANNER_INSTANCE}
SPANNER_DATABASE=${SPANNER_DATABASE}
ALLOYDB_REGION=${ALLOYDB_REGION}
ALLOYDB_CLUSTER=${ALLOYDB_CLUSTER}
ALLOYDB_INSTANCE=${ALLOYDB_INSTANCE}
ALLOYDB_DATABASE=${ALLOYDB_DATABASE}
ALLOYDB_ENABLED=${ALLOYDB_ENABLED}
GLOSSARY_LOCATION=${GLOSSARY_LOCATION}
EOF
ok "wrote .deploy.env"

# For running either service locally with `adk web` while you iterate on the
# TODOs. Cloud Run ignores these entirely — service.yaml supplies the deployed
# environment.
for D in agent transact; do
    cat > "${SRC_DIR}/${D}/.env" <<EOF
GOOGLE_GENAI_USE_VERTEXAI=true
GOOGLE_CLOUD_LOCATION=global
GOOGLE_CLOUD_PROJECT=${PROJECT_ID}
BQ_SOURCE_DATASET_ID=${BQ_SOURCE_DATASET_ID}
BQ_ZEROETL_DATASET_ID=${BQ_ZEROETL_DATASET_ID}
SPANNER_INSTANCE=${SPANNER_INSTANCE}
SPANNER_DATABASE=${SPANNER_DATABASE}
ALLOYDB_REGION=${ALLOYDB_REGION}
ALLOYDB_CLUSTER=${ALLOYDB_CLUSTER}
ALLOYDB_INSTANCE=${ALLOYDB_INSTANCE}
ALLOYDB_DATABASE=${ALLOYDB_DATABASE}
EOF
done
ok "wrote agent/.env and transact/.env (local runs only)"

# ==============================================================================
say "Ready"
# ==============================================================================
cat <<NEXT

  ${C_BOLD}agy can now reach four MCP servers${C_OFF} — BigQuery, Knowledge Catalog, Spanner
  and AlloyDB. That is what this script just configured, and it is why it runs
  before you use agy at all: without it agy has no MCP servers, which does not
  error. It simply finds nothing and reports that things do not exist.

  Continue with the user guide from Step 1. The lab's actual work is three TODOs:

      ${C_BOLD}agent/router.py${C_OFF}                 TODO_A — the intent taxonomy
      ${C_BOLD}agent/orchestrator.py${C_OFF}           TODO_B — the dispatch table
      ${C_BOLD}transact/spanner_specialist.py${C_OFF}  TODO_C — the Spanner instruction

  Then deploy all three services:

      ${C_BOLD}bash deploy.sh${C_OFF}

  This script is safe to re-run, and re-running it is the fix if you switch
  projects: the MCP configuration is rewritten with the new id.

NEXT
