#!/usr/bin/env bash
# Author: Wissem Khlifi - August 2026
# ==============================================================================
# Lab 11 — who is who, and what each one cannot do
#
#   bash whoami.sh
#
# Lab 10 had two identities and one policy tag. Lab 11 has three identities and
# the difference between them is the whole architecture, so it is worth being
# able to print at any moment — particularly when an agent has just refused
# something and you want to know whether the refusal came from the model, from a
# tool filter, or from IAM.
# ==============================================================================
set -uo pipefail

C_BOLD=$'\033[1m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_OFF=$'\033[0m'

cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null || true
[ -f .deploy.env ] && { set -a; . ./.deploy.env; set +a; }

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-us-central1}"
ACCOUNT="$(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null | head -1)"

printf '\n%sYou%s\n' "${C_BOLD}" "${C_OFF}"
printf '  account   %s\n' "${ACCOUNT:-<not signed in>}"
printf '  project   %s\n' "${PROJECT_ID:-<unset>}"
IMP="$(gcloud config get-value auth/impersonate_service_account 2>/dev/null || true)"
if [ -n "${IMP}" ] && [ "${IMP}" != "(unset)" ]; then
    printf '  %s⚠ impersonating %s%s\n' "${C_YELLOW}" "${IMP}" "${C_OFF}"
    printf '    Everything you run is acting as that account. Clear it with:\n'
    printf '      gcloud config unset auth/impersonate_service_account\n'
fi

svc() {   # svc <service>  -> url + service account
    gcloud run services describe "$1" --region="${REGION}" --project="${PROJECT_ID}" \
        --format='value(status.url,spec.template.spec.serviceAccountName)' 2>/dev/null
}

printf '\n%sThe three services%s\n' "${C_BOLD}" "${C_OFF}"
for TRIPLE in \
    "lab runner|${RUNNER_SERVICE:-lab11-runner}|reads everything a participant may read; writes nothing; sees REAL email" \
    "dispatch|${AGENT_SERVICE:-lab11-agent}|router + analyst + graph; reads BigQuery and Spanner; NO write role anywhere; sees MASKED email" \
    "transact|${TRANSACT_SERVICE:-lab11-transact}|writes AlloyDB and Spanner; NO BigQuery data role; private, only dispatch may invoke it"; do
    LABEL="${TRIPLE%%|*}"; REST="${TRIPLE#*|}"
    NAME="${REST%%|*}"; NOTE="${REST#*|}"
    INFO="$(svc "${NAME}")"
    URL="$(echo "${INFO}" | awk '{print $1}')"
    SA="$(echo "${INFO}" | awk '{print $2}')"
    printf '\n  %s%-10s%s %s\n' "${C_BOLD}" "${LABEL}" "${C_OFF}" "${NAME}"
    printf '    url       %s\n' "${URL:-<not deployed>}"
    printf '    runs as   %s\n' "${SA:-<not deployed>}"
    printf '    %s\n' "${NOTE}"
done

printf '\n%sWhy this matters%s\n' "${C_BOLD}" "${C_OFF}"
cat <<'NOTE'
  When an agent refuses something in this lab, the refusal has one of three
  sources and they are worth telling apart:

    the model      it decided not to. Rephrasing might change it.
    a tool_filter  the tool was never advertised, so there was nothing to call.
                   Rephrasing cannot change it; editing the filter can.
    IAM            the request left the model, reached the API, and was denied.
                   Neither rephrasing nor editing the agent changes it.

  Only the third is a guarantee. This lab is built so the important ones are all
  of the third kind.
NOTE
printf '\n'
