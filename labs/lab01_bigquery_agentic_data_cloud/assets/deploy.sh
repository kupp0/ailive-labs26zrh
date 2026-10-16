#!/usr/bin/env bash
# Author: Wissem Khlifi - August 2026
# ==============================================================================
# Lab 10 — build and deploy the agent and the lab console to Cloud Run
#
#   bash deploy.sh                 both services
#   bash deploy.sh --agent-only    just the agent (after editing agent.py)
#   bash deploy.sh --runner-only   just the lab console (service lab-runner)
#
# Order matters: the lab runner needs the agent's URL as an environment
# variable, so the agent is deployed first and its URL is read back. Builds run
# in parallel because they are independent; deploys do not, because they are not.
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

DEPLOY_AGENT=1
DEPLOY_RUNNER=1
case "${1:-}" in
    --agent-only)  DEPLOY_RUNNER=0 ;;
    --runner-only) DEPLOY_AGENT=0 ;;
    "")            ;;
    -h|--help)     sed -n '3,15p' "$0" | sed 's/^# \?//'; exit 0 ;;
    *)             die "Unknown argument: $1  (try --help)" ;;
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
export RUNNER_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/${RUNNER_SERVICE}:${TAG}"

printf '  project %s · region %s · tag %s\n' "${PROJECT_ID}" "${REGION}" "${TAG}"

# ==============================================================================
say "Pre-flight"
# ==============================================================================
# Deploying an agent whose TODOs are unfinished wastes a five-minute build and
# then fails at runtime with an import error. Catch it in a second instead.
if [ "${DEPLOY_AGENT}" -eq 1 ]; then
    if grep -qE '^(CATALOG|BIGQUERY)_TOOLSET[[:space:]]*=[[:space:]]*None' agent/agent.py; then
        die "TODO_2 is not finished — the toolsets in agent/agent.py are still None.
  The prompt is in user_guide.md, Step 4. (Not the lab console: this script
  creates it, so it does not exist yet.) Complete it with agy, then re-run."
    fi
    if grep -q 'instruction="TODO_3"' agent/agent.py; then
        die "TODO_3 is not finished — the instruction in agent/agent.py is still a placeholder.
  The prompt is in user_guide.md, Step 5. (Not the lab console: this script
  creates it, so it does not exist yet.) Complete it with agy, then re-run."
    fi
    python3 -c "import ast,sys; ast.parse(open('agent/agent.py').read())" 2>/dev/null \
        || die "agent/agent.py does not parse as Python. Fix the syntax, then re-run."
    ok "agent.py: both TODOs complete, file parses"
fi

# ==============================================================================
say "Build"
# ==============================================================================
BUILD_FAILED=0
build() {   # build <label> <dir> <image>
    printf '  building %s...\n' "$1"
    if gcloud builds submit "$2" --tag "$3" --project="${PROJECT_ID}" \
         --region="${REGION}" --quiet >"/tmp/lab10-build-$1.log" 2>&1; then
        ok "$1 image built"
    else
        printf '  %s✗%s %s build failed — last lines:\n' "${C_RED}" "${C_OFF}" "$1"
        tail -15 "/tmp/lab10-build-$1.log" | sed 's/^/      /'
        return 1
    fi
}

# `gcloud builds submit` creates gs://<project>_cloudbuild on first use in a
# project. The two builds below run in PARALLEL, so on a first deployment they
# race for that creation: one wins, and the other reads a bucket that does not
# exist yet and fails with
#
#   ERROR: (gcloud.builds.submit) ... [<project>_cloudbuild] not found:
#   The specified bucket does not exist.
#
# The message names the authenticated account, which sends you looking for a
# permissions problem that is not there. Create the bucket once, serially, so
# there is nothing left to race for. Only ever needed on the first deploy into
# a project; every run after this finds it and moves on.
STAGING_BUCKET="gs://${PROJECT_ID}_cloudbuild"
if ! gcloud storage buckets describe "${STAGING_BUCKET}" \
        --project="${PROJECT_ID}" >/dev/null 2>&1; then
    if gcloud storage buckets create "${STAGING_BUCKET}" \
            --project="${PROJECT_ID}" --location=US \
            --uniform-bucket-level-access >/dev/null 2>&1; then
        ok "created Cloud Build staging bucket ${STAGING_BUCKET}"
    else
        # A concurrent creation, or a policy that forbids it. Not fatal here --
        # let the build itself report the real reason.
        warn "could not pre-create ${STAGING_BUCKET}; letting the build try"
    fi
fi

PIDS=()
[ "${DEPLOY_AGENT}"  -eq 1 ] && { build agent  ./agent      "${AGENT_IMAGE}"  & PIDS+=($!); }
[ "${DEPLOY_RUNNER}" -eq 1 ] && { build runner ./lab-runner "${RUNNER_IMAGE}" & PIDS+=($!); }
for PID in "${PIDS[@]}"; do wait "${PID}" || BUILD_FAILED=1; done
[ "${BUILD_FAILED}" -eq 0 ] || die "A build failed. Nothing was deployed."

# ==============================================================================
say "Deploy"
# ==============================================================================
# service.yaml carries ${VAR} placeholders; expand them from the environment the
# same way Lab 03 does, so the committed file stays readable and portable.
resolve() {   # resolve <in> <out>
    python3 -c 'import os,sys; sys.stdout.write(os.path.expandvars(sys.stdin.read()))' \
        < "$1" > "$2"
}

if [ "${DEPLOY_AGENT}" -eq 1 ]; then
    resolve agent/service.yaml agent/service_resolved.yaml
    gcloud run services replace agent/service_resolved.yaml \
        --region="${REGION}" --project="${PROJECT_ID}" --quiet >/dev/null \
        || die "Agent deploy failed. If the message names iam.serviceAccounts.actAs, re-run: bash init.sh"
    rm -f agent/service_resolved.yaml
    ok "agent deployed as ${AGENT_SA}"
fi

AGENT_URL="$(gcloud run services describe "${AGENT_SERVICE}" --region="${REGION}" \
             --project="${PROJECT_ID}" --format='value(status.url)' 2>/dev/null || true)"
[ -n "${AGENT_URL}" ] || die \
"Could not read the agent's URL. Deploy it first:
  bash deploy.sh --agent-only"
export AGENT_URL

# The agent is called service-to-service by the lab runner with an ID token, so
# it stays private -- no allUsers binding. The lab runner holds run.invoker.
# IAM-EXCEPTION: the Cloud Run service is created by this script at participant runtime, so Terraform cannot reference it.
gcloud run services add-iam-policy-binding "${AGENT_SERVICE}" \
    --region="${REGION}" --project="${PROJECT_ID}" \
    --member="serviceAccount:${RUNNER_SA}" --role=roles/run.invoker \
    --quiet >/dev/null 2>&1 || warn "could not grant the lab runner invoker on the agent"

# Lab switcher: the console links to Lab 11's console when that one exists.
# Lab 11 is optional and usually deployed later; its own deploy.sh fills this
# in on our service then. Empty is valid and renders "not deployed yet".
LAB11_RUNNER_SERVICE="lab11-runner"
LAB11_CONSOLE_URL="$(gcloud run services describe "${LAB11_RUNNER_SERVICE}" --region="${REGION}" \
                     --project="${PROJECT_ID}" --format='value(status.url)' 2>/dev/null || true)"
export LAB11_CONSOLE_URL
# Lab 12 tab: the Gemini Enterprise publisher console, when deployed.
GE_PUBLISHER_URL="$(gcloud run services describe ge-publisher --region="${REGION}" \
                    --project="${PROJECT_ID}" --format='value(status.url)' 2>/dev/null || true)"
export GE_PUBLISHER_URL

if [ "${DEPLOY_RUNNER}" -eq 1 ]; then
    resolve lab-runner/service.yaml lab-runner/service_resolved.yaml
    gcloud run services replace lab-runner/service_resolved.yaml \
        --region="${REGION}" --project="${PROJECT_ID}" --quiet >/dev/null \
        || die "Lab runner deploy failed."
    rm -f lab-runner/service_resolved.yaml
    # The participant opens this in a browser, so it is the one public surface.
    # IAM-EXCEPTION: the Cloud Run service is created by this script at participant runtime, so Terraform cannot reference it.
    gcloud run services add-iam-policy-binding "${RUNNER_SERVICE}" \
        --region="${REGION}" --project="${PROJECT_ID}" \
        --member=allUsers --role=roles/run.invoker --quiet >/dev/null 2>&1 \
        || warn "could not make the lab console public — an org policy may block allUsers.
     Open it with: gcloud run services proxy ${RUNNER_SERVICE} --region=${REGION} --project=${PROJECT_ID}"
    ok "lab console deployed as ${RUNNER_SA}"
fi

RUNNER_URL="$(gcloud run services describe "${RUNNER_SERVICE}" --region="${REGION}" \
              --project="${PROJECT_ID}" --format='value(status.url)' 2>/dev/null || true)"

# Tell an already-deployed Lab 11 console where this one lives, so its lab
# switcher can link back. One new revision on that service; failure is cosmetic.
if [ "${DEPLOY_RUNNER}" -eq 1 ] && [ -n "${LAB11_CONSOLE_URL}" ] && [ -n "${RUNNER_URL}" ]; then
    gcloud run services update "${LAB11_RUNNER_SERVICE}" \
        --region="${REGION}" --project="${PROJECT_ID}" \
        --update-env-vars="LAB10_CONSOLE_URL=${RUNNER_URL}" --quiet >/dev/null 2>&1 \
        && ok "Zero-ETL Federation console now links to this one" \
        || warn "could not link the Zero-ETL Federation console to this one (its lab switcher shows 'not deployed')"
fi

# ==============================================================================
say "Done"
# ==============================================================================
cat <<DONE

  ${C_BOLD}Open the lab console here, then continue with guide Step 6:${C_OFF}

      ${C_GREEN}${RUNNER_URL:-<not deployed>}${C_OFF}

  Organisation blocks public URLs? Open it through a local proxy instead:
      gcloud run services proxy ${RUNNER_SERVICE} --region=${REGION} --project=${PROJECT_ID}

  Agent (private, called by the lab console):
      ${AGENT_URL}

  The two identities the lab demonstrates:
      lab console ${RUNNER_SA}
                  Fine-Grained Reader — sees real email
      agent       ${AGENT_SA}
                  Masked Reader — sees XXXXX@

  After editing agent.py:  bash deploy.sh --agent-only

DONE
