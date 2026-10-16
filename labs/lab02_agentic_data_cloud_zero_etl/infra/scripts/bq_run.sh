#!/usr/bin/env bash
# Author: Wissem Khlifi - August 2026
# ==============================================================================
# Lab 11 — run one BigQuery script, synchronously, with bounded retries
#
# Called by null_resource.export_to_spanner, null_resource.export_to_alloydb and
# null_resource.unified_graph. Everything arrives in the environment:
#
#   PROJECT_ID     required
#   BQ_LOCATION    required   e.g. US
#   SQL            required   the script to run
#   LABEL          optional   what to print
#   ALLOW_FAILURE  optional   "1" -> report and exit 0 instead of failing apply
#
# This exists instead of google_bigquery_job for two reasons, both of them
# observed rather than theoretical:
#
#   1. google_bigquery_job is "created" when a job is SUBMITTED, not when it
#      finishes. Lab 10 documents the consequence at its section 3b: downstream
#      resources 404'd on tables the job had not written yet. `bq query` blocks.
#
#   2. EXPORT DATA to Spanner needs an ENTERPRISE reservation, and that
#      reservation belongs to LAB 10. Only one project-wide QUERY assignment may
#      exist, so Lab 11 cannot create its own. The engine orders Lab 11 after
#      Lab 10, but Lab 10's own comments note that the assignment takes a
#      minute or two to propagate after creation, during which the error is
#      indistinguishable from never having created it. So: retry.
# ==============================================================================
set -uo pipefail

C_CYAN=$'\033[36m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'
C_RED=$'\033[31m';  C_BOLD=$'\033[1m';   C_OFF=$'\033[0m'
say()  { printf '\n%s%s--- [lab11] %s%s\n' "${C_CYAN}" "${C_BOLD}" "$1" "${C_OFF}"; }
ok()   { printf '  %s✓%s %s\n' "${C_GREEN}" "${C_OFF}" "$1"; }
warn() { printf '  %s⚠%s  %s\n' "${C_YELLOW}" "${C_OFF}" "$1"; }
die()  { printf '\n%s✗ %s%s\n\n' "${C_RED}" "$1" "${C_OFF}"; exit 1; }

LABEL="${LABEL:-BigQuery script}"
ALLOW_FAILURE="${ALLOW_FAILURE:-0}"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-12}"
SLEEP_SECONDS="${SLEEP_SECONDS:-30}"

say "${LABEL}"

[ -n "${PROJECT_ID:-}" ]  || die "PROJECT_ID is not set."
[ -n "${BQ_LOCATION:-}" ] || die "BQ_LOCATION is not set."
[ -n "${SQL:-}" ]         || die "SQL is not set."

command -v bq >/dev/null 2>&1 || die \
"The 'bq' CLI is not on PATH on this machine.

  Terraform runs this script on the DEPLOYMENT host, not on a participant's
  workstation, so the Google Cloud SDK has to be installed wherever deploy.py
  runs. 'bq' ships with gcloud:
      https://cloud.google.com/sdk/docs/install"

SQL_FILE="$(mktemp -t lab11-bq-XXXXXX.sql)"
LOG_FILE="$(mktemp -t lab11-bq-XXXXXX.log)"
trap 'rm -f "${SQL_FILE}" "${LOG_FILE}"' EXIT
printf '%s\n' "${SQL}" > "${SQL_FILE}"

# Errors worth waiting out. Everything else is a real bug in the SQL, and
# retrying it eleven more times only delays the report.
#
# The pattern is assembled by APPENDING to a variable rather than written as one
# backslash-continued literal. A backslash-newline inside single quotes is a
# LITERAL backslash, so the continued form reaches grep with a trailing
# backslash and grep refuses the whole expression with "Trailing backslash" --
# which makes every error look non-retryable and silently disables this function.
_RETRYABLE_RE='requires a reservation|Enterprise or Enterprise Plus'
_RETRYABLE_RE="${_RETRYABLE_RE}|reservation.*not found|was not found in location"
_RETRYABLE_RE="${_RETRYABLE_RE}|not found: (Dataset|Table|Connection)"
_RETRYABLE_RE="${_RETRYABLE_RE}|rateLimitExceeded|quota|backendError|internalError"
_RETRYABLE_RE="${_RETRYABLE_RE}|Internal error|deadline exceeded|try again"
_RETRYABLE_RE="${_RETRYABLE_RE}|currently unavailable|is not ready"
_RETRYABLE_RE="${_RETRYABLE_RE}|still being created|PENDING_CREATE"
_RETRYABLE_RE="${_RETRYABLE_RE}|Parent row.*missing|writing to Cloud Spanner"
# Permission errors ARE retryable here, and that is not a shortcut. Every IAM
# grant these scripts depend on is made by the SAME apply, seconds earlier, so a
# denial is far more often propagation than misconfiguration. Treating it as
# fatal is what left an AlloyDB table empty on a clean project while the role was
# present and correct the whole time.
#
# The cost is bounded and visible: a genuinely missing role burns MAX_ATTEMPTS
# before failing, and every attempt prints the error, so the log says plainly
# what is being waited on.
# `permission.*denied`, not `permission .* denied`: the second form needs a space
# on both sides and so misses "Permission Denied." -- which is exactly how the
# Spanner federation error is worded. grep runs with -i, so case is irrelevant.
_RETRYABLE_RE="${_RETRYABLE_RE}|Access Denied|permission.*denied"
_RETRYABLE_RE="${_RETRYABLE_RE}|does not have permission|PERMISSION_DENIED"

is_retryable() {
    grep -qiE "${_RETRYABLE_RE}" "$1"
}

# ── Fast-fail: is every source table this script reads actually there? ───────
#
# "not found: Table" is in the retryable list above, and rightly so: within a
# correct event Lab 10's seed job may still be running when this starts. But if
# Lab 10 was never selected, that error can NEVER clear, and the retry budget
# turns a misconfiguration into 20 x 30s = TEN MINUTES of silence -- paid AFTER
# the AlloyDB cluster is already built and billing, and ending in an error that
# names a BigQuery table rather than a missing lab.
#
# So probe once, up front. A table that is missing because Lab 10 has not
# finished yet still exists as a DATASET, which is what this checks: a missing
# dataset means the lab was never applied at all, and no amount of waiting fixes
# it. Costs one metadata call; saves ten minutes and a confusing error.
probe_source_datasets() {
    local refs dataset
    # Fully-qualified references of the form `project.dataset.table`.
    # Strip WITH CONNECTION clauses first so connection identifiers (e.g. `project.us.conn`)
    # are not falsely probed as datasets.
    refs="$(tr '\n' ' ' < "${SQL_FILE}" \
            | sed -E 's/WITH[[:space:]]+CONNECTION[[:space:]]+`[^`]+`//gI' \
            | grep -oE '`[A-Za-z0-9_-]+\.[A-Za-z0-9_]+\.[A-Za-z0-9_]+`' \
            | tr -d '`' | cut -d. -f1,2 | sort -u || true)"
    [ -n "${refs}" ] || return 0

    while IFS= read -r dataset; do
        [ -n "${dataset}" ] || continue
        # Only check datasets in THIS project; a public dataset is not ours to own.
        case "${dataset}" in "${PROJECT_ID}."*) ;; *) continue ;; esac
        if ! bq show --project_id="${PROJECT_ID}" "${dataset/./:}" >/dev/null 2>&1; then
            printf '\n  %sSource dataset does not exist: %s%s\n' "${C_RED}" "${dataset}" "${C_OFF}"
            die "${LABEL} cannot run: it reads \`${dataset}\`, which is not in this project.

  This dataset belongs to LAB 10 (bigquery_agentic_data_cloud). Lab 11 seeds its
  operational engines from it, so Lab 10 must be part of the same event.

  Most likely cause:
    Lab 10 was not selected, or its module was removed from the root main.tf.

  Check:
    bq ls --project_id=${PROJECT_ID}
    grep '^module' main.tf        # both lab modules should be present

  Not retrying: a missing dataset is a misconfiguration, not a race. Waiting
  cannot fix it, and the ${MAX_ATTEMPTS}-attempt retry budget below exists for
  the case where Lab 10's seed job is still RUNNING, not absent."
        fi
    done <<< "${refs}"
}

probe_source_datasets

ATTEMPT=1
RETRYABLE=1
while [ "${ATTEMPT}" -le "${MAX_ATTEMPTS}" ]; do
    if bq query \
          --project_id="${PROJECT_ID}" \
          --location="${BQ_LOCATION}" \
          --use_legacy_sql=false \
          --format=none \
          --quiet \
          --nouse_cache \
          < "${SQL_FILE}" > "${LOG_FILE}" 2>&1; then
        ok "${LABEL} — succeeded on attempt ${ATTEMPT}"
        exit 0
    fi

    if ! is_retryable "${LOG_FILE}"; then
        RETRYABLE=0
        printf '\n  %sBigQuery rejected the script (not a retryable error):%s\n' "${C_RED}" "${C_OFF}"
        sed 's/^/      /' "${LOG_FILE}"
        break
    fi

    if [ "${ATTEMPT}" -lt "${MAX_ATTEMPTS}" ]; then
        warn "attempt ${ATTEMPT}/${MAX_ATTEMPTS} failed with a retryable error; waiting ${SLEEP_SECONDS}s"
        printf '      %s\n' "$(tail -2 "${LOG_FILE}" | tr '\n' ' ')"
        sleep "${SLEEP_SECONDS}"
    fi
    ATTEMPT=$((ATTEMPT + 1))
done

# The loop exits either by break (ATTEMPT is the attempt that failed) or by
# exhaustion (ATTEMPT is MAX+1). Report the real number: saying "failed after 12
# attempts" when it gave up on the first one sends you looking for a flaky API
# instead of at the SQL.
[ "${ATTEMPT}" -gt "${MAX_ATTEMPTS}" ] && ATTEMPT="${MAX_ATTEMPTS}"

printf '\n  %slast error:%s\n' "${C_RED}" "${C_OFF}"
sed 's/^/      /' "${LOG_FILE}"

if [ "${ALLOW_FAILURE}" = "1" ]; then
    warn "${LABEL} did not complete. Continuing: this step is tolerated so one
     step cannot take an entire event build down. verify.sh reports it, and
     the lab console offers a button to retry it by hand."
    exit 0
fi

if [ "${RETRYABLE}" -eq 0 ]; then
    die "${LABEL} failed on attempt ${ATTEMPT}: BigQuery rejected the script."
fi
die "${LABEL} failed after ${ATTEMPT} attempt(s)."
