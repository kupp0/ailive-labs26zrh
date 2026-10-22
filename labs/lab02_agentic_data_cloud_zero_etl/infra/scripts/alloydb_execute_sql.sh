#!/usr/bin/env bash
# Author: Wissem Khlifi - August 2026
# ==============================================================================
# Lab 11 — run SQL against AlloyDB through the remote MCP server
#
#   PROJECT_ID      required
#   INSTANCE_URI    required   projects/P/locations/R/clusters/C/instances/I
#   DATABASE        required   e.g. postgres
#   SQL_STATEMENTS  required   one statement per LINE (no trailing semicolons)
#   LABEL           optional
#
# WHY THIS EXISTS AT ALL
# ----------------------
# EXPORT DATA does not create its destination table, so AlloyDB needs DDL before
# the reverse-ETL job can run. Three obvious ways to deliver it are all closed:
#
#   The provider has no SQL-execution resource for AlloyDB.
#   psql and the Auth Proxy need network reach to a private IP, and core infra's
#     VPC uses regional routing with the workstation subnet in another region.
#   Terraform runs on the deployment host, which is not on that VPC at all.
#
# The AlloyDB remote MCP server (global endpoint) is an ordinary HTTPS API
# that reaches the instance through Google's own control plane. Its execute_sql
# tool takes {instance, database, sqlStatement} and, per its published schema,
# "Uses IAM user authentication" — so the caller's own identity is the Postgres
# role. That is why main.tf makes whoever runs `terraform apply` an
# ALLOYDB_IAM_USER with alloydbsuperuser.
#
# Statements are sent ONE AT A TIME: execute_sql takes a single sqlStatement, and
# a semicolon-joined batch is not reliably accepted.
# ==============================================================================
set -uo pipefail

C_CYAN=$'\033[36m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'
C_RED=$'\033[31m';  C_BOLD=$'\033[1m';   C_OFF=$'\033[0m'
say()  { printf '\n%s%s--- [lab11] %s%s\n' "${C_CYAN}" "${C_BOLD}" "$1" "${C_OFF}"; }
ok()   { printf '  %s✓%s %s\n' "${C_GREEN}" "${C_OFF}" "$1"; }
warn() { printf '  %s⚠%s  %s\n' "${C_YELLOW}" "${C_OFF}" "$1"; }
die()  { printf '\n%s✗ %s%s\n\n' "${C_RED}" "$1" "${C_OFF}"; exit 1; }

LABEL="${LABEL:-AlloyDB SQL}"
if [ -n "${ALLOYDB_MCP_URL:-}" ]; then
    MCP_URL="${ALLOYDB_MCP_URL}"
elif [ -n "${ALLOYDB_REGION:-}" ]; then
    MCP_URL="https://alloydb.${ALLOYDB_REGION}.rep.googleapis.com/mcp"
else
    MCP_URL="https://alloydb.googleapis.com/mcp"
fi
MAX_ATTEMPTS="${MAX_ATTEMPTS:-15}"
SLEEP_SECONDS="${SLEEP_SECONDS:-20}"
ALLOW_FAILURE="${ALLOW_FAILURE:-0}"

say "${LABEL}"

[ -n "${PROJECT_ID:-}" ]     || die "PROJECT_ID is not set."
[ -n "${INSTANCE_URI:-}" ]   || die "INSTANCE_URI is not set."
[ -n "${DATABASE:-}" ]       || die "DATABASE is not set."
[ -n "${SQL_STATEMENTS:-}" ] || die "SQL_STATEMENTS is not set."

command -v gcloud  >/dev/null 2>&1 || die "gcloud not found on the deployment host."
command -v curl    >/dev/null 2>&1 || die "curl not found on the deployment host."
command -v python3 >/dev/null 2>&1 || die "python3 not found on the deployment host."

# ------------------------------------------------------------------------------
# Ensure the CALLING identity is an AlloyDB IAM user that can create tables.
#
# execute_sql authenticates as the caller, so the caller must exist as an
# ALLOYDB_IAM_USER. It also needs alloydbsuperuser: PostgreSQL 15 removed CREATE
# on the public schema from PUBLIC, so an ordinary IAM user connects fine and is
# then refused "permission denied for schema public".
#
# This is done HERE rather than with a google_alloydb_user resource because the
# deploying identity is not knowable at plan time. Resolving it through
# data.google_client_openid_userinfo made user_id "(known after apply)", and
# user_id is ForceNew — so every apply planned a replacement whose destroy then
# failed with "role cannot be dropped because some objects depend on it".
#
# Idempotent: an existing user is left exactly as it is.
# ------------------------------------------------------------------------------
if [ -n "${ALLOYDB_CLUSTER:-}" ] && [ -n "${ALLOYDB_REGION:-}" ]; then
    CALLER="${BOOTSTRAP_OVERRIDE:-}"
    [ -n "${CALLER}" ] || CALLER="$(gcloud auth list --filter=status:ACTIVE \
        --format='value(account)' 2>/dev/null | head -n 1)"

    if [ -z "${CALLER}" ]; then
        die "No active gcloud account on the deployment host. Run:
      gcloud auth login --update-adc"
    fi

    # `gcloud alloydb users list`, not `describe`: there IS no
    # `gcloud alloydb users describe` subcommand, and calling it fails with
    # "Invalid choice: 'describe'" rather than with "not found" — so the check
    # would report every user as missing.
    if gcloud alloydb users list \
         --cluster="${ALLOYDB_CLUSTER}" --region="${ALLOYDB_REGION}" \
         --project="${PROJECT_ID}" --format='value(name)' 2>/dev/null \
       | grep -qxF "projects/${PROJECT_ID}/locations/${ALLOYDB_REGION}/clusters/${ALLOYDB_CLUSTER}/users/${CALLER}"; then
        ok "${CALLER} is already an AlloyDB user"
    elif gcloud alloydb users create "${CALLER}" \
           --cluster="${ALLOYDB_CLUSTER}" --region="${ALLOYDB_REGION}" \
           --project="${PROJECT_ID}" --type=IAM_BASED \
           --db-roles=alloydbsuperuser --quiet >/dev/null 2>&1; then
        ok "registered ${CALLER} as an AlloyDB IAM user (alloydbsuperuser)"
    else
        warn "could not register ${CALLER} as an AlloyDB IAM user; continuing in
     case it already exists under a different spelling. If the statements below
     fail with \"permission denied for schema public\", create it by hand:
       gcloud alloydb users create ${CALLER} --cluster=${ALLOYDB_CLUSTER} \\
         --region=${ALLOYDB_REGION} --type=IAM_BASED --db-roles=alloydbsuperuser"
    fi
fi

# Build the JSON-RPC body in Python rather than with string concatenation: the
# DDL contains quotes, and a hand-rolled JSON string breaks on the first GRANT
# to a quoted role name.
build_body() {
    SQL_STATEMENT="$1" python3 - "$INSTANCE_URI" "$DATABASE" <<'PYEOF'
import json, os, sys
print(json.dumps({
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {
        "name": "execute_sql",
        "arguments": {
            "instance":     sys.argv[1],
            "database":     sys.argv[2],
            "sqlStatement": os.environ["SQL_STATEMENT"],
        },
    },
}))
PYEOF
}

# The endpoint speaks streamable HTTP, so a reply may arrive as SSE `data:` lines
# rather than a bare JSON body. Both shapes are handled. Exit 0 = success,
# 2 = retryable, 1 = a real error in the statement.
read_verdict() {
    python3 - "$1" <<'PYEOF'
import json, re, sys

raw = open(sys.argv[1], encoding="utf-8", errors="replace").read().strip()
if not raw:
    print("RETRY empty response from the MCP endpoint")
    sys.exit(2)

payload = None
for chunk in re.split(r"\n\n+", raw):
    body = "".join(
        line[5:].lstrip() for line in chunk.splitlines() if line.startswith("data:")
    ) or chunk
    try:
        payload = json.loads(body)
    except ValueError:
        continue
if payload is None:
    print("RETRY unparseable response: " + raw[:300].replace("\n", " "))
    sys.exit(2)

RETRYABLE = (
    "not ready", "pending", "creating", "unavailable", "try again",
    "deadline exceeded", "rate limit", "quota", "internal error",
    "503", "500", "connection refused", "does not exist yet",
    "invalid authentication credentials", "unauthenticated",
)

def verdict(message, code=None):
    low = message.lower()
    if any(k in low for k in RETRYABLE) or code in (401, 429, 500, 503):
        print("RETRY " + message[:400].replace("\n", " "))
        sys.exit(2)
    print("ERROR " + message[:800].replace("\n", " "))
    sys.exit(1)

if "error" in payload:
    err = payload["error"]
    verdict(str(err.get("message", err)), err.get("code"))

result = payload.get("result", {})
if result.get("isError"):
    parts = result.get("content") or []
    text = " ".join(p.get("text", "") for p in parts if isinstance(p, dict))
    verdict(text or json.dumps(result))

# A FAILED STATEMENT DOES NOT SET isError.
#
# The AlloyDB MCP server answers a broken statement with HTTP 200, no top-level
# "error", and isError absent -- burying the failure inside the tool result as
#   {"metadata": {"status": "ERROR", "message": "postgresql error: ..."}}
#
# Checking only isError therefore reported four GRANTs to a non-existent role as
# succeeding, and the first thing to notice was an agent failing at runtime. Any
# check that can print a tick for a failed write is worse than no check.
for part in result.get("content") or []:
    if not isinstance(part, dict):
        continue
    try:
        inner = json.loads(part.get("text") or "")
    except ValueError:
        continue
    meta = inner.get("metadata") or {}
    if str(meta.get("status", "")).upper() == "ERROR":
        verdict(meta.get("message") or json.dumps(meta))

print("OK")
PYEOF
}

RESP="$(mktemp -t lab11-alloydb-XXXXXX.json)"
trap 'rm -f "${RESP}"' EXIT

STATEMENT_NO=0
while IFS= read -r STATEMENT; do
    [ -n "${STATEMENT//[[:space:]]/}" ] || continue
    STATEMENT_NO=$((STATEMENT_NO + 1))
    SHORT="$(printf '%.72s' "${STATEMENT}")"

    ATTEMPT=1
    while :; do
        TOKEN=""
        if [ "${ATTEMPT}" -eq 1 ] && [ -n "${GOOGLE_OAUTH_ACCESS_TOKEN:-}" ]; then
            TOKEN="${GOOGLE_OAUTH_ACCESS_TOKEN}"
        fi
        if [ -z "${TOKEN}" ]; then
            TOKEN="$(gcloud auth application-default print-access-token 2>/dev/null || true)"
        fi
        if [ -z "${TOKEN}" ]; then
            TOKEN="$(gcloud auth print-access-token 2>/dev/null || true)"
        fi
        [ -n "${TOKEN}" ] || die \
"Could not mint an access token on the deployment host. Run:
      gcloud auth application-default login"

        HTTP_CODE="$(curl -s --max-time 120 -o "${RESP}" -w '%{http_code}' \
            -X POST "${MCP_URL}" \
            -H "Authorization: Bearer ${TOKEN}" \
            -H "Content-Type: application/json" \
            -H "Accept: application/json, text/event-stream" \
            -H "x-goog-user-project: ${PROJECT_ID}" \
            -d "$(build_body "${STATEMENT}")" 2>/dev/null || echo 000)"

        VERDICT="$(read_verdict "${RESP}")"; RC=$?

        if [ "${RC}" -eq 0 ] && [ "${HTTP_CODE}" = "200" ]; then
            ok "${STATEMENT_NO}. ${SHORT}"
            break
        fi

        # A non-200 with an unparseable body is retryable on its own merits.
        if [ "${RC}" -eq 1 ]; then
            printf '\n  %sAlloyDB rejected statement %s:%s\n' "${C_RED}" "${STATEMENT_NO}" "${C_OFF}"
            printf '      %s\n' "${STATEMENT}"
            printf '      %s\n' "${VERDICT#ERROR }"
            die "${LABEL} failed."
        fi

        if [ "${ATTEMPT}" -ge "${MAX_ATTEMPTS}" ]; then
            printf '\n  %slast response (HTTP %s):%s\n' "${C_RED}" "${HTTP_CODE}" "${C_OFF}"
            printf '      %s\n' "${VERDICT#RETRY }"
            if [ "${ALLOW_FAILURE}" = "1" ]; then
                warn "${LABEL}: statement ${STATEMENT_NO} still failing after ${MAX_ATTEMPTS} attempts; continuing because ALLOW_FAILURE=1"
                break
            fi
            die "${LABEL}: statement ${STATEMENT_NO} still failing after ${MAX_ATTEMPTS} attempts.

  If this says the tool or its arguments are unknown, the MCP surface has moved.
  List what the server actually offers with:

      curl -s -X POST ${MCP_URL} \\
        -H \"Authorization: Bearer \$(gcloud auth print-access-token)\" \\
        -H 'Content-Type: application/json' \\
        -H 'Accept: application/json, text/event-stream' \\
        -H 'x-goog-user-project: ${PROJECT_ID}' \\
        -d '{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\",\"params\":{}}'"
        fi

        warn "statement ${STATEMENT_NO}, attempt ${ATTEMPT}/${MAX_ATTEMPTS} (HTTP ${HTTP_CODE}); waiting ${SLEEP_SECONDS}s"
        sleep "${SLEEP_SECONDS}"
        ATTEMPT=$((ATTEMPT + 1))
    done
done <<< "${SQL_STATEMENTS}"

ok "${LABEL} — ${STATEMENT_NO} statement(s) applied"
