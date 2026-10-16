#!/usr/bin/env bash
# Author: Wissem Khlifi - August 2026
# ==============================================================================
# Lab 11 — build and deploy the three services to Cloud Run
#
#   bash deploy.sh                    all three
#   bash deploy.sh --agent-only       just the dispatch agent (after a TODO edit)
#   bash deploy.sh --transact-only    just the transactional agent
#   bash deploy.sh --runner-only      just the lab runner
#
# ORDER MATTERS, and it is a chain rather than a preference:
#   transact  has no dependencies
#   agent     needs TRANSACT_URL  (it calls the write service over HTTP)
#   runner    needs AGENT_URL     (and TRANSACT_URL, to show what it cannot reach)
#
# Builds run in PARALLEL because they are independent. Deploys do not, because
# they are not.
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

DEPLOY_AGENT=1; DEPLOY_TRANSACT=1; DEPLOY_RUNNER=1
case "${1:-}" in
    --agent-only)    DEPLOY_TRANSACT=0; DEPLOY_RUNNER=0 ;;
    --transact-only) DEPLOY_AGENT=0;    DEPLOY_RUNNER=0 ;;
    --runner-only)   DEPLOY_AGENT=0;    DEPLOY_TRANSACT=0 ;;
    "")              ;;
    -h|--help)       sed -n '3,20p' "$0" | sed 's/^# \?//'; exit 0 ;;
    *)               die "Unknown argument: $1  (try --help)" ;;
esac

# ==============================================================================
say "Configuration"
# ==============================================================================
[ -f "${SRC_DIR}/.deploy.env" ] || die \
".deploy.env not found — run the bootstrap first:
  bash init.sh"
set -a; . "${SRC_DIR}/.deploy.env"; set +a
ok "loaded .deploy.env"

command -v gcloud >/dev/null || die "gcloud not found."

export TAG="$(date +%Y%m%d-%H%M%S)"
export AGENT_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/${AGENT_SERVICE}:${TAG}"
export TRANSACT_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/${TRANSACT_SERVICE}:${TAG}"
export RUNNER_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/${RUNNER_SERVICE}:${TAG}"

printf '  project %s · region %s · tag %s\n' "${PROJECT_ID}" "${REGION}" "${TAG}"

# ==============================================================================
say "Pre-flight"
# ==============================================================================
# Deploying unfinished TODOs wastes a five-minute build and then fails at
# runtime. Catch it in a second instead.
if [ "${DEPLOY_AGENT}" -eq 1 ]; then
    if grep -q 'ROUTER_INSTRUCTION = "TODO_A"' agent/router.py; then
        die "TODO_A is not finished — the router's instruction in agent/router.py is
  still a placeholder. Every query would be classified as UNKNOWN.
  The prompt is in user_guide.md, Step 4."
    fi
    # Both patterns require a DICT ENTRY, not a mention. TODO_B's own comment
    # block names both intents, so a bare grep for the word passes on an
    # untouched file and the check would be worthless.
    if ! grep -q '"TRANSACT_ALLOYDB"[[:space:]]*:' agent/orchestrator.py \
       || ! grep -q '"TRANSACT_SPANNER"[[:space:]]*:' agent/orchestrator.py; then
        die "TODO_B is not finished — _INTENT_TO_SPECIALIST in agent/orchestrator.py
  has no entry for TRANSACT_ALLOYDB and TRANSACT_SPANNER, so every write
  request will stop at the orchestrator.
  The prompt is in user_guide.md, Step 5."
    fi
    python3 -c "import ast,sys; [ast.parse(open(f).read()) for f in \
        ['agent/router.py','agent/orchestrator.py','agent/dispatch.py']]" 2>/dev/null \
        || die "The dispatch agent's Python does not parse. Fix the syntax, then re-run."
    ok "dispatch agent: TODO_A and TODO_B complete, files parse"
fi

if [ "${DEPLOY_TRANSACT}" -eq 1 ]; then
    if grep -q 'SPANNER_INSTRUCTION = "TODO_C"' transact/spanner_specialist.py; then
        die "TODO_C is not finished — the Spanner specialist's instruction in
  transact/spanner_specialist.py is still a placeholder.
  The prompt is in user_guide.md, Step 6."
    fi
    python3 -c "import ast,sys; [ast.parse(open(f).read()) for f in \
        ['transact/spanner_specialist.py','transact/alloydb_specialist.py']]" 2>/dev/null \
        || die "The transactional agent's Python does not parse. Fix the syntax, then re-run."
    ok "transactional agent: TODO_C complete, files parse"
fi

# ==============================================================================
say "Build"
# ==============================================================================
BUILD_FAILED=0
build() {   # build <label> <dir> <image>
    printf '  building %s...\n' "$1"
    if gcloud builds submit "$2" --tag "$3" --project="${PROJECT_ID}" \
         --region="${REGION}" --quiet >"/tmp/lab11-build-$1.log" 2>&1; then
        ok "$1 image built"
    else
        printf '  %s✗%s %s build failed — last lines:\n' "${C_RED}" "${C_OFF}" "$1"
        tail -15 "/tmp/lab11-build-$1.log" | sed 's/^/      /'
        return 1
    fi
}

# `gcloud builds submit` creates gs://<project>_cloudbuild on first use. The
# builds below run in PARALLEL, so on a first deployment they race for that
# creation: one wins and the others read a bucket that does not exist yet,
# failing with a message that names the authenticated account and sends you
# looking for a permissions problem that is not there. Create it once, serially.
STAGING_BUCKET="gs://${PROJECT_ID}_cloudbuild"
if ! gcloud storage buckets describe "${STAGING_BUCKET}" \
        --project="${PROJECT_ID}" >/dev/null 2>&1; then
    gcloud storage buckets create "${STAGING_BUCKET}" \
        --project="${PROJECT_ID}" --location=US --uniform-bucket-level-access \
        >/dev/null 2>&1 \
        && ok "created Cloud Build staging bucket ${STAGING_BUCKET}" \
        || warn "could not pre-create ${STAGING_BUCKET}; letting the build try"
fi

PIDS=()
[ "${DEPLOY_TRANSACT}" -eq 1 ] && { build transact ./transact   "${TRANSACT_IMAGE}" & PIDS+=($!); }
[ "${DEPLOY_AGENT}"    -eq 1 ] && { build agent    ./agent      "${AGENT_IMAGE}"    & PIDS+=($!); }
[ "${DEPLOY_RUNNER}"   -eq 1 ] && { build runner   ./lab-runner "${RUNNER_IMAGE}"   & PIDS+=($!); }
for PID in "${PIDS[@]}"; do wait "${PID}" || BUILD_FAILED=1; done
[ "${BUILD_FAILED}" -eq 0 ] || die "A build failed. Nothing was deployed."

# ==============================================================================
say "Deploy"
# ==============================================================================
# service.yaml carries ${VAR} placeholders; expand them from the environment the
# same way Lab 10 does, so the committed file stays readable and portable.
resolve() {   # resolve <in> <out>
    python3 -c 'import os,sys; sys.stdout.write(os.path.expandvars(sys.stdin.read()))' \
        < "$1" > "$2"
}

svc_url() {   # svc_url <service>
    gcloud run services describe "$1" --region="${REGION}" \
        --project="${PROJECT_ID}" --format='value(status.url)' 2>/dev/null || true
}

# --- 1. transact, first: it has no dependencies and the agent needs its URL ---
if [ "${DEPLOY_TRANSACT}" -eq 1 ]; then
    resolve transact/service.yaml transact/service_resolved.yaml
    gcloud run services replace transact/service_resolved.yaml \
        --region="${REGION}" --project="${PROJECT_ID}" --quiet >/dev/null \
        || die "Transactional agent deploy failed. If the message names
  iam.serviceAccounts.actAs, re-run: bash init.sh"
    rm -f transact/service_resolved.yaml
    ok "transactional agent deployed as ${TRANSACT_SA}"
fi

export TRANSACT_URL="$(svc_url "${TRANSACT_SERVICE}")"
[ -n "${TRANSACT_URL}" ] || warn \
"The transactional service is not deployed, so the dispatch agent will have no
     write path. Deploy it: bash deploy.sh --transact-only"

# This is the binding that makes "only the router can write" true. The
# transactional service is never given allUsers; the dispatch identity alone
# holds run.invoker on it, so a participant's own curl is refused.
if [ -n "${TRANSACT_URL}" ]; then
    # IAM-EXCEPTION: the Cloud Run service is created by this script at participant runtime, so Terraform cannot reference it.
    gcloud run services add-iam-policy-binding "${TRANSACT_SERVICE}" \
        --region="${REGION}" --project="${PROJECT_ID}" \
        --member="serviceAccount:${AGENT_SA}" --role=roles/run.invoker \
        --quiet >/dev/null 2>&1 \
        && ok "only the dispatch agent may invoke the transactional agent" \
        || warn "could not grant the dispatch agent invoker on the transactional agent"
fi

# --- 2. the dispatch agent ---
if [ "${DEPLOY_AGENT}" -eq 1 ]; then
    resolve agent/service.yaml agent/service_resolved.yaml
    gcloud run services replace agent/service_resolved.yaml \
        --region="${REGION}" --project="${PROJECT_ID}" --quiet >/dev/null \
        || die "Dispatch agent deploy failed."
    rm -f agent/service_resolved.yaml
    ok "dispatch agent deployed as ${AGENT_SA}"
fi

export AGENT_URL="$(svc_url "${AGENT_SERVICE}")"
[ -n "${AGENT_URL}" ] || die \
"Could not read the dispatch agent's URL. Deploy it first:
  bash deploy.sh --agent-only"

# The agent stays private too — it is called service-to-service by the runner.
# IAM-EXCEPTION: the Cloud Run service is created by this script at participant runtime, so Terraform cannot reference it.
gcloud run services add-iam-policy-binding "${AGENT_SERVICE}" \
    --region="${REGION}" --project="${PROJECT_ID}" \
    --member="serviceAccount:${RUNNER_SA}" --role=roles/run.invoker \
    --quiet >/dev/null 2>&1 \
    || warn "could not grant the lab runner invoker on the dispatch agent"

# Lab switcher: link to Lab 10's console when it is deployed in this project.
# Lab 10 is a dependency, but its console is optional (an event may select
# Lab 11 only). Empty is valid and renders "not deployed".
LAB10_RUNNER_SERVICE="lab-runner"
export LAB10_CONSOLE_URL="$(svc_url "${LAB10_RUNNER_SERVICE}")"
# Lab 12 tab: the Gemini Enterprise publisher console, when deployed.
export GE_PUBLISHER_URL="$(svc_url ge-publisher)"

# --- 3. the lab runner: the one public surface ---
if [ "${DEPLOY_RUNNER}" -eq 1 ]; then
    resolve lab-runner/service.yaml lab-runner/service_resolved.yaml
    gcloud run services replace lab-runner/service_resolved.yaml \
        --region="${REGION}" --project="${PROJECT_ID}" --quiet >/dev/null \
        || die "Lab runner deploy failed."
    rm -f lab-runner/service_resolved.yaml
    # IAM-EXCEPTION: the Cloud Run service is created by this script at participant runtime, so Terraform cannot reference it.
    gcloud run services add-iam-policy-binding "${RUNNER_SERVICE}" \
        --region="${REGION}" --project="${PROJECT_ID}" \
        --member=allUsers --role=roles/run.invoker --quiet >/dev/null 2>&1 \
        || warn "could not make the lab runner public — an org policy may block allUsers.
     Open it with: gcloud run services proxy ${RUNNER_SERVICE} --region=${REGION} --project=${PROJECT_ID}"
    ok "lab runner deployed as ${RUNNER_SA}"
fi

RUNNER_URL="$(svc_url "${RUNNER_SERVICE}")"

# Tell Lab 10's console where this one lives, so its lab switcher links
# forward. One new revision on that service; failure is cosmetic.
if [ "${DEPLOY_RUNNER}" -eq 1 ] && [ -n "${LAB10_CONSOLE_URL}" ] && [ -n "${RUNNER_URL}" ]; then
    gcloud run services update "${LAB10_RUNNER_SERVICE}" \
        --region="${REGION}" --project="${PROJECT_ID}" \
        --update-env-vars="LAB11_CONSOLE_URL=${RUNNER_URL}" --quiet >/dev/null 2>&1 \
        && ok "From Data to Agent console now links to this one" \
        || warn "could not link the From Data to Agent console to this one (its lab switcher shows 'not deployed')"
fi

# ==============================================================================
say "Done"
# ==============================================================================
cat <<DONE

  ${C_BOLD}Open the lab console here, then continue with guide Step 6:${C_OFF}

      ${C_GREEN}${RUNNER_URL:-<not deployed>}${C_OFF}

  Organisation blocks public URLs? Open it through a local proxy instead:
      gcloud run services proxy ${RUNNER_SERVICE} --region=${REGION} --project=${PROJECT_ID}

  Three services, three identities, and what each one cannot do:

      lab runner   ${RUNNER_SA}
                   Fine-Grained Reader — sees real email. Writes nothing.
      dispatch     ${AGENT_SA}
                   Masked Reader. No write role on any engine.
      transact     ${TRANSACT_SA}
                   Writes AlloyDB and Spanner. No BigQuery data role.
                   Private: only the dispatch agent may invoke it.

  Service URLs:
      dispatch  ${AGENT_URL}
      transact  ${TRANSACT_URL:-<not deployed>}

  After editing a TODO:
      bash deploy.sh --agent-only        router.py, orchestrator.py
      bash deploy.sh --transact-only     spanner_specialist.py

DONE
