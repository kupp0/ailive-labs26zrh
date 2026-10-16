#!/usr/bin/env bash
# ==============================================================================
# Day-0 Verification Script: Lab 12 — Publish ADK Agents to Gemini Enterprise
#
# Asserts what Terraform pre-provisioned. Anything that depends on a participant
# action (the Lab 10/11 agents being deployed, the publish itself) is reported
# as [INFO]/[WARN], never [FAIL]: on Day 0 none of it exists yet.
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

for tool in gcloud python3 curl; do
    command -v "${tool}" &>/dev/null || { echo "[FAIL] Missing ${tool}"; exit 1; }
done

echo "🧪 Running Day-0 Verification: Publish ADK Agents to Gemini Enterprise..."

PROJECT_ID="${PROJECT_ID:-${GOOGLE_CLOUD_PROJECT:-$(gcloud config get-value project 2>/dev/null || true)}}"
if [ -z "${PROJECT_ID}" ] || [ "${PROJECT_ID}" = "(unset)" ]; then
    echo "[FAIL] No project configured. Run: gcloud config set project <lab-project-id>"
    exit 1
fi
REGION="${CLOUD_RUN_REGION:-us-central1}"
AR_REPO="${AR_REPO_ID:-lab12}"
PUBLISHER_SA="ge-publisher-sa@${PROJECT_ID}.iam.gserviceaccount.com"
FAILED=0

# --- 1. APIs ---------------------------------------------------------------
ENABLED="$(gcloud services list --enabled --project="${PROJECT_ID}" --format='value(config.name)' 2>/dev/null || true)"
for api in discoveryengine.googleapis.com cloudaicompanion.googleapis.com run.googleapis.com \
           cloudbuild.googleapis.com artifactregistry.googleapis.com iamcredentials.googleapis.com; do
    if printf '%s\n' "${ENABLED}" | grep -qx "${api}"; then
        echo "[PASS] API enabled: ${api}"
    else
        echo "[FAIL] API not enabled: ${api}"
        FAILED=1
    fi
done

# --- 2. Identities ---------------------------------------------------------
if gcloud iam service-accounts describe "${PUBLISHER_SA}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    echo "[PASS] Publisher identity exists: ${PUBLISHER_SA%%@*}"
else
    echo "[FAIL] Publisher identity missing: ${PUBLISHER_SA}"
    FAILED=1
fi

POLICY="$(gcloud projects get-iam-policy "${PROJECT_ID}" --format=json 2>/dev/null || echo '{}')"
for role in roles/discoveryengine.admin roles/run.admin; do
    if printf '%s' "${POLICY}" | python3 -c '
import json, sys
role, member = sys.argv[1], sys.argv[2]
policy = json.load(sys.stdin)
sys.exit(0 if any(b.get("role") == role and member in b.get("members", []) for b in policy.get("bindings", [])) else 1)
' "${role}" "serviceAccount:${PUBLISHER_SA}"; then
        echo "[PASS] Publisher holds ${role}"
    else
        echo "[FAIL] Publisher is missing ${role}"
        FAILED=1
    fi
done

# The Discovery Engine service agent is the identity Gemini Enterprise uses to
# call the agents. Terraform materialises it; its presence is visible through
# the project number in its email.
PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)' 2>/dev/null || true)"
echo "[INFO] Gemini Enterprise calls agents as service-${PROJECT_NUMBER}@gcp-sa-discoveryengine.iam.gserviceaccount.com"

# --- 3. Artifact Registry --------------------------------------------------
if gcloud artifacts repositories describe "${AR_REPO}" --location="${REGION}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    echo "[PASS] Artifact Registry repository ${AR_REPO} in ${REGION}"
else
    echo "[FAIL] Artifact Registry repository ${AR_REPO} missing in ${REGION}"
    FAILED=1
fi

# --- 4. Lab files ----------------------------------------------------------
for f in deploy.sh publisher/main.py publisher/ge.py publisher/Dockerfile publisher/service.yaml; do
    if [ -f "${f}" ]; then
        echo "[PASS] Present: ${f}"
    else
        echo "[FAIL] Missing: ${f}"
        FAILED=1
    fi
done
if python3 -c "import ast; [ast.parse(open(p).read()) for p in ('publisher/main.py', 'publisher/ge.py')]" 2>/dev/null; then
    echo "[PASS] Publisher sources parse"
else
    echo "[FAIL] Publisher sources do not parse"
    FAILED=1
fi

# --- 5. Participant progress (informational) --------------------------------
for svc in ecommerce-agent lab11-agent ge-publisher; do
    if gcloud run services describe "${svc}" --region="${REGION}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
        echo "[INFO] Cloud Run service ${svc} is deployed"
    else
        echo "[INFO] Cloud Run service ${svc} not deployed yet (expected before the guide's deploy steps)"
    fi
done

if [ "${FAILED}" -ne 0 ]; then
    echo "[FAIL] Day-0 verification finished with failures — see the [FAIL] lines above."
    exit 1
fi
echo "[PASS] Day-0 verification completed: Publish ADK Agents to Gemini Enterprise"
exit 0
