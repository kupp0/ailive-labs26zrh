#!/usr/bin/env bash
# Author: Wissem Khlifi - August 2026
# ==============================================================================
# Lab 10 — which identity is what?
#
#   bash whoami.sh
#
# Three identities matter in this lab and only one of them is yours:
#
#   you          deploy the services, run bq by hand, write glossary terms
#   lab-runner   the Cloud Run service you open in a browser
#                Fine-Grained Reader -> reads customers.email raw
#   agent        the Cloud Run service the lab runner calls
#                Masked Reader       -> reads the same column as XXXXX@
#
# You never switch between them. Cloud Run assigns the two service identities at
# deploy time; this script reports what is actually in effect -- useful when a
# permission error names a permission but never the principal.
# ==============================================================================
set -uo pipefail

if [ -z "${BASH_VERSION:-}" ]; then
    echo "Run with bash, not sh:  bash $0" >&2
    exit 1
fi

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    C_CYAN=$'\033[36m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'
    C_BOLD=$'\033[1m';  C_OFF=$'\033[0m'
else
    C_CYAN=""; C_GREEN=""; C_YELLOW=""; C_BOLD=""; C_OFF=""
fi

row()  { printf '  %s%-22s%s %s\n' "${C_BOLD}" "$1" "${C_OFF}" "$2"; }
wrap() { printf '%s\n' "$1" | fold -s -w 76; }

REGION="${CLOUD_RUN_REGION:-us-central1}"
PROJECT="$(gcloud config get-value project 2>/dev/null || true)"
[ -n "${PROJECT}" ] && [ "${PROJECT}" != "(unset)" ] || PROJECT="(unset)"

ACCOUNT="$(gcloud config get-value account 2>/dev/null || true)"
[ -n "${ACCOUNT}" ] && [ "${ACCOUNT}" != "(unset)" ] || ACCOUNT="(none — run: gcloud auth login)"

IMP="$(gcloud config get-value auth/impersonate_service_account 2>/dev/null || true)"
if [ -z "${IMP}" ] || [ "${IMP}" = "(unset)" ]; then
    IMP=""
    IMP_STATE="${C_GREEN}no${C_OFF} — gcloud and bq run as you"
else
    IMP_STATE="${C_YELLOW}YES${C_OFF} — ${IMP}"
fi

printf '\n%s%s%s\n' "${C_CYAN}${C_BOLD}" \
    "==============================================================================" "${C_OFF}"
printf '%s  WHO IS WHO%s\n' "${C_CYAN}${C_BOLD}" "${C_OFF}"
printf '%s%s%s\n\n' "${C_CYAN}${C_BOLD}" \
    "==============================================================================" "${C_OFF}"

printf '%s  This terminal%s\n' "${C_BOLD}" "${C_OFF}"
row "gcloud account" "${ACCOUNT}"
row "impersonating" "${IMP_STATE}"
row "project" "${PROJECT}"
row "working directory" "$(pwd)"

# Read the identity from Cloud Run rather than assuming it. What each service is
# ACTUALLY running as is the only thing that decides whether Step 7 demonstrates
# anything at all.
printf '\n%s  Deployed services%s\n' "${C_BOLD}" "${C_OFF}"
FOUND=0
for PAIR in "lab-runner|the app you open in a browser" "ecommerce-agent|the agent it calls"; do
    SVC="${PAIR%%|*}"; DESC="${PAIR#*|}"
    SA="$(gcloud run services describe "${SVC}" --region="${REGION}" \
          --project="${PROJECT}" \
          --format='value(spec.template.spec.serviceAccountName)' 2>/dev/null || true)"
    URL="$(gcloud run services describe "${SVC}" --region="${REGION}" \
           --project="${PROJECT}" --format='value(status.url)' 2>/dev/null || true)"
    if [ -n "${SA}" ]; then
        FOUND=$((FOUND + 1))
        row "${SVC}" "runs as ${SA}"
        [ -n "${URL}" ] && row "" "${URL}"
    else
        row "${SVC}" "${C_YELLOW}not deployed${C_OFF} — ${DESC}"
    fi
done

printf '\n'
if [ -n "${IMP}" ]; then
    printf '%s' "${C_YELLOW}"
    wrap "This terminal is impersonating a service account. Nothing in this lab needs that — the identity split lives in the two Cloud Run services. Clear it before deploying:  gcloud config unset auth/impersonate_service_account"
    printf '%s\n' "${C_OFF}"
elif [ "${FOUND}" -lt 2 ]; then
    printf '%s' "${C_YELLOW}"
    wrap "Not everything is deployed yet. Run 'bash init.sh' once, complete the two TODOs in agent/agent.py with agy, then 'bash deploy.sh'."
    printf '%s\n' "${C_OFF}"
else
    printf '%s' "${C_GREEN}"
    wrap "Both services are up, running as different identities — which is what makes Step 7 a demonstration rather than an assertion. Open the lab-runner URL above to run the lab."
    printf '%s\n' "${C_OFF}"
fi
printf '\n'
