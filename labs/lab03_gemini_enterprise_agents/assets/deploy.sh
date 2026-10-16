#!/usr/bin/env bash
# ==============================================================================
# Lab 12 — build and deploy the Gemini Enterprise publisher console
#
#   bash deploy.sh
#
# One Cloud Run service, ge-publisher, running as ge-publisher-sa. Open the URL
# it prints and press "Publish ADK Agents to Gemini Enterprise". Everything else
# (trial licence, app, invoker grants, A2A registrations, seats) happens there.
#
# Run Lab 10's deploy.sh first (and Lab 11's, if you want both agents): this
# console publishes the agent services those scripts created. Safe to re-run.
# ==============================================================================
set -euo pipefail

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

for tool in gcloud python3; do
    command -v "${tool}" &>/dev/null || die "Missing ${tool}"
done

# ==============================================================================
say "Configuration"
# ==============================================================================
PROJECT_ID="${PROJECT_ID:-${GOOGLE_CLOUD_PROJECT:-$(gcloud config get-value project 2>/dev/null || true)}}"
[ -n "${PROJECT_ID}" ] && [ "${PROJECT_ID}" != "(unset)" ] || die \
"No project. Run:  gcloud config set project <your-lab-project-id>"
REGION="${CLOUD_RUN_REGION:-us-central1}"
AR_REPO="${AR_REPO_ID:-lab12}"
PUBLISHER_SERVICE="ge-publisher"
PUBLISHER_SA="ge-publisher-sa@${PROJECT_ID}.iam.gserviceaccount.com"
LAB10_AGENT_SERVICE="${LAB10_AGENT_SERVICE:-ecommerce-agent}"
LAB11_AGENT_SERVICE="${LAB11_AGENT_SERVICE:-lab11-agent}"
PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)' 2>/dev/null || true)"
[ -n "${PROJECT_NUMBER}" ] || die "Cannot read project ${PROJECT_ID}. Check: gcloud auth list"
# The participant gets the first Gemini Enterprise seat. A service account
# login (CI) has no seat to take, so it is left empty.
PARTICIPANT_EMAIL="$(gcloud config get-value account 2>/dev/null || true)"
case "${PARTICIPANT_EMAIL}" in
    *.gserviceaccount.com|"(unset)") PARTICIPANT_EMAIL="" ;;
esac
TAG="$(date +%Y%m%d-%H%M%S)"
PUBLISHER_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/${PUBLISHER_SERVICE}:${TAG}"
# Lab switcher: the Lab 10 / Lab 11 consoles, when deployed (empty = "not deployed").
console_url() { gcloud run services describe "$1" --region="${REGION}" --project="${PROJECT_ID}" \
                    --format='value(status.url)' 2>/dev/null || true; }
LAB10_CONSOLE_URL="$(console_url lab-runner)"
LAB11_CONSOLE_URL="$(console_url lab11-runner)"
export PROJECT_ID PROJECT_NUMBER REGION PUBLISHER_SERVICE PUBLISHER_SA PUBLISHER_IMAGE \
       PARTICIPANT_EMAIL LAB10_AGENT_SERVICE LAB11_AGENT_SERVICE LAB10_CONSOLE_URL LAB11_CONSOLE_URL
printf '  project %s (%s) · region %s · participant %s\n' \
    "${PROJECT_ID}" "${PROJECT_NUMBER}" "${REGION}" "${PARTICIPANT_EMAIL:-<none>}"

# ==============================================================================
say "Pre-flight"
# ==============================================================================
gcloud iam service-accounts describe "${PUBLISHER_SA}" --project="${PROJECT_ID}" >/dev/null 2>&1 \
    || die "${PUBLISHER_SA} does not exist. This lab's Terraform has not been applied to this project."
ok "publisher identity ${PUBLISHER_SA%%@*}"
for svc in "${LAB10_AGENT_SERVICE}" "${LAB11_AGENT_SERVICE}"; do
    if gcloud run services describe "${svc}" --region="${REGION}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
        ok "agent service ${svc} found"
    else
        warn "agent service ${svc} not deployed yet (fine: publish it later, the console re-checks)"
    fi
done

# Same race as Lab 10: create the Cloud Build staging bucket once, serially.
STAGING_BUCKET="gs://${PROJECT_ID}_cloudbuild"
if ! gcloud storage buckets describe "${STAGING_BUCKET}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    gcloud storage buckets create "${STAGING_BUCKET}" --project="${PROJECT_ID}" --location=US \
        --uniform-bucket-level-access >/dev/null 2>&1 \
        && ok "created ${STAGING_BUCKET}" \
        || warn "could not pre-create ${STAGING_BUCKET}; letting the build try"
fi

# ==============================================================================
say "Build"
# ==============================================================================
BUILD_LOG="$(mktemp -t lab12-build.XXXXXX)"
if gcloud builds submit ./publisher --tag "${PUBLISHER_IMAGE}" --project="${PROJECT_ID}" \
       --region="${REGION}" --quiet >"${BUILD_LOG}" 2>&1; then
    ok "publisher image built"
else
    tail -15 "${BUILD_LOG}" | sed 's/^/      /'
    die "Build failed. Nothing was deployed."
fi

# ==============================================================================
say "Deploy"
# ==============================================================================
RESOLVED="$(mktemp -t lab12-service.XXXXXX)"
python3 -c 'import os,sys; sys.stdout.write(os.path.expandvars(sys.stdin.read()))' \
    < publisher/service.yaml > "${RESOLVED}"
gcloud run services replace "${RESOLVED}" --region="${REGION}" --project="${PROJECT_ID}" --quiet >/dev/null \
    || die "Deploy failed. If the message names iam.serviceAccounts.actAs, this lab's Terraform grant is missing."
rm -f "${RESOLVED}"
ok "publisher deployed as ${PUBLISHER_SA%%@*}"

# The participant opens this in a browser, like the Lab 10/11 consoles.
# IAM-EXCEPTION: the Cloud Run service is created by this script at participant runtime, so Terraform cannot reference it.
gcloud run services add-iam-policy-binding "${PUBLISHER_SERVICE}" \
    --region="${REGION}" --project="${PROJECT_ID}" \
    --member=allUsers --role=roles/run.invoker --quiet >/dev/null 2>&1 \
    || warn "could not make the console public — an org policy may block allUsers.
     Open it with: gcloud run services proxy ${PUBLISHER_SERVICE} --region=${REGION} --project=${PROJECT_ID}"

PUBLISHER_URL="$(gcloud run services describe "${PUBLISHER_SERVICE}" --region="${REGION}" \
                 --project="${PROJECT_ID}" --format='value(status.url)' 2>/dev/null || true)"

# Lab switcher: add a "Lab 12" tab to the Lab 10 / Lab 11 consoles. One new
# revision on each; failure is cosmetic (the tab shows "not deployed").
for runner in lab-runner lab11-runner; do
    if [ -n "${PUBLISHER_URL}" ] && [ -n "$(console_url "${runner}")" ]; then
        gcloud run services update "${runner}" --region="${REGION}" --project="${PROJECT_ID}" \
            --update-env-vars="GE_PUBLISHER_URL=${PUBLISHER_URL}" --quiet >/dev/null 2>&1 \
            && ok "${runner} console now has a Publish to Gemini Enterprise tab" \
            || warn "could not add the Publish to Gemini Enterprise tab to ${runner}"
    fi
done

# ==============================================================================
say "Done"
# ==============================================================================
cat <<DONE

  ${C_BOLD}Open the publisher console and press "Publish ADK Agents to Gemini Enterprise":${C_OFF}

      ${C_GREEN}${PUBLISHER_URL:-<not deployed>}${C_OFF}

  Organisation blocks public URLs? Open it through a local proxy instead:
      gcloud run services proxy ${PUBLISHER_SERVICE} --region=${REGION} --project=${PROJECT_ID}

DONE
