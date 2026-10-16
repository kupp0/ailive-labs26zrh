#!/usr/bin/env bash
# Author: Wissem Khlifi - August 2026
# ==============================================================================
# Day-0 Verification Script: BigQuery Agentic Data Cloud Lab
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

echo "🧪 Running Day-0 Verification for BigQuery Agentic Data Cloud..."

DATASET_ID="ecommerce_lab"
GRAPH_DATASET_ID="ecommerce_graph_lab"
TABLES=("customers" "orders" "products")
REQUIRED_APIS=("bigquery.googleapis.com" "dataplex.googleapis.com" "datacatalog.googleapis.com")

# Remote MCP servers the agent depends on. Their tool surface
# may change, which is exactly why this script asserts the tools it needs by
# name instead of only checking that the endpoint answers.
BIGQUERY_MCP_URL="https://bigquery.googleapis.com/mcp"
CATALOG_MCP_URL="https://dataplex.googleapis.com/mcp"
BIGQUERY_MCP_TOOLS=("get_table_info" "execute_sql_readonly")
CATALOG_MCP_TOOLS=("search_entries" "lookup_entry")

# Lists the tools a remote MCP server advertises, then asserts the required ones
# are present. Args: <label> <url> <required tool>...
check_mcp_server() {
    local label="$1" url="$2"
    shift 2
    local required=("$@")
    local body tools missing=()

    body="$(curl -s --max-time 30 -X POST "${url}" \
        -H "Authorization: Bearer ${ACCESS_TOKEN}" \
        -H "Content-Type: application/json" \
        -H "Accept: application/json, text/event-stream" \
        -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' 2>/dev/null || true)"

    if [ -z "${body}" ]; then
        echo "[FAIL] ${label} MCP server unreachable at ${url}"
        return 1
    fi

    tools="$(printf '%s' "${body}" | python3 -c '
import json, sys
try:
    payload = json.load(sys.stdin)
except Exception:
    sys.exit(1)
if "error" in payload:
    sys.exit(1)
print(" ".join(t["name"] for t in payload.get("result", {}).get("tools", [])))
' 2>/dev/null || true)"

    if [ -z "${tools}" ]; then
        echo "[FAIL] ${label} MCP server returned no usable tool list"
        echo "       response: $(printf '%s' "${body}" | head -c 200)"
        return 1
    fi

    for TOOL in "${required[@]}"; do
        if ! printf '%s' "${tools}" | grep -qw "${TOOL}"; then
            missing+=("${TOOL}")
        fi
    done

    if [ ${#missing[@]} -gt 0 ]; then
        echo "[FAIL] ${label} MCP server is missing required tool(s): ${missing[*]}"
        echo "       advertised: ${tools}"
        return 1
    fi

    echo "[PASS] ${label} MCP server reachable; tools: ${tools}"
    return 0
}

# tools/list is served WITHOUT authentication, so reachability alone proves
# nothing about the caller's credentials. This makes one real tool call and
# checks the server did not answer with isError, which is what catches an
# expired or wrong-project ADC token before the agent hits it at runtime.
# Args: <label> <url> <json arguments object> <tool name>
check_mcp_auth() {
    local label="$1" url="$2" args="$3" tool="$4"
    local body verdict

    body="$(curl -s --max-time 40 -X POST "${url}" \
        -H "Authorization: Bearer ${ACCESS_TOKEN}" \
        -H "Content-Type: application/json" \
        -H "Accept: application/json, text/event-stream" \
        -H "x-goog-user-project: ${PROJECT_ID}" \
        -d "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/call\",\"params\":{\"name\":\"${tool}\",\"arguments\":${args}}}" \
        2>/dev/null || true)"

    verdict="$(printf '%s' "${body}" | python3 -c '
import json, sys
try:
    payload = json.load(sys.stdin)
except Exception:
    print("UNPARSEABLE RESPONSE")
    raise SystemExit
result = payload.get("result", {})
if payload.get("error"):
    print("ERROR " + str(payload["error"])[:160])
elif result.get("isError"):
    text = (result.get("content") or [{}])[0].get("text", "")
    print("ERROR " + text[:160])
else:
    print("OK")
' 2>/dev/null || echo "UNPARSEABLE RESPONSE")"

    if [ "${verdict}" = "OK" ]; then
        echo "[PASS] ${label} MCP call authenticated (${tool})"
        return 0
    fi

    echo "[FAIL] ${label} MCP call rejected: ${verdict#ERROR }"
    return 1
}

#--- 1. Lab assets ---
AGENT_PY=""
# Script-relative first (the E2E harness runs ./assets/verify.sh from the lab
# root); then the same directory on a workstation; ../src in the source repo.
for CANDIDATE in "${SCRIPT_DIR}/agent/agent.py" "agent/agent.py" "../src/agent/agent.py" "src/agent/agent.py"; do
    [ -f "${CANDIDATE}" ] && { AGENT_PY="${CANDIDATE}"; break; }
done
if [ -n "${AGENT_PY}" ]; then
    echo "[PASS] ADK agent template present (${AGENT_PY})"
    grep -qE '^(CATALOG|BIGQUERY)_TOOLSET[[:space:]]*=[[:space:]]*None' "${AGENT_PY}" \
        && echo "[INFO] TODO_2 not completed yet — expected until you run agy."
    grep -q 'instruction="TODO_3"' "${AGENT_PY}" \
        && echo "[INFO] TODO_3 not completed yet — expected until you run agy."
else
    echo "[FAIL] agent/agent.py not found — looked in ., ./src and ../src."
    echo "       Run this from the folder that holds init.sh and agent/:"
    echo "         on a workstation:   cd ~/labs/<your lab folder>/assets"
    echo "         in the source repo: cd labs/10_bigquery_agentic_data_cloud/src"
    exit 1
fi

#--- 2. Google Cloud SDK ---
if command -v gcloud &>/dev/null; then
    echo "[PASS] Google Cloud SDK initialized on Workstation"
else
    echo "[FAIL] gcloud CLI not found"
    exit 1
fi

#--- 3. Python runtime ---
if command -v python3 &>/dev/null; then
    echo "[PASS] Python runtime available ($(python3 --version))"
else
    echo "[FAIL] Python3 runtime not found"
    exit 1
fi

#--- 3b. Identity in effect ---
# Every BigQuery and Dataplex check below runs as whoever this shell is. If a
# previous agent run left the CLI impersonating -- or the participant ran
# impersonate_agent.sh by hand -- the failures that follow describe the service
# account's permissions, not the state of the lab, and nothing in their wording
# says so.
CLI_IMPERSONATION="$(gcloud config get-value auth/impersonate_service_account 2>/dev/null || true)"
if [ -n "${CLI_IMPERSONATION}" ] && [ "${CLI_IMPERSONATION}" != "(unset)" ]; then
    echo "[WARN] This shell is impersonating ${CLI_IMPERSONATION}"
    echo "       Every check below runs as that service account, which is read-only."
    echo "       Day-0 verification is meant to run as YOU. Clear it and re-run:"
    echo "         gcloud config unset auth/impersonate_service_account"
else
    echo "[PASS] Running as you, not impersonating a service account"
fi

PROJECT_ID="${PROJECT_ID:-${GOOGLE_CLOUD_PROJECT:-${GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null)}}}"
if [ -n "${PROJECT_ID}" ] && [ "${PROJECT_ID}" != "(unset)" ]; then
    echo "[PASS] Active GCP project resolved (${PROJECT_ID})"
else
    echo "[FAIL] No active GCP project configured"
    exit 1
fi

#--- 4a. Deployment sources ---
SRC_DIR=""
for CANDIDATE in "." "../src" "src"; do
    [ -f "${CANDIDATE}/init.sh" ] && { SRC_DIR="${CANDIDATE}"; break; }
done
if [ -z "${SRC_DIR}" ]; then
    echo "[FAIL] init.sh not found — the lab source tree is missing."
    echo "       Run this from the lab folder, the one holding init.sh and agent/."
    exit 1
fi
MISSING_SRC=()
for F in init.sh deploy.sh agent/agent.py agent/main.py agent/Dockerfile \
         agent/service.yaml lab-runner/main.py lab-runner/Dockerfile \
         lab-runner/service.yaml; do
    [ -f "${SRC_DIR}/${F}" ] || MISSING_SRC+=("${F}")
done
if [ ${#MISSING_SRC[@]} -gt 0 ]; then
    echo "[FAIL] Missing deployment sources: ${MISSING_SRC[*]}"
    exit 1
fi
echo "[PASS] Deployment sources present (${SRC_DIR})"

#--- 4a2. MCP configuration for agy ---
# Written by init.sh, which owns it -- not by Terraform and not by the
# workstation bootstrapper (neither has both the project id and a path to this
# machine at the same time; see section 6 of init.sh). Both paths are checked
# because agy has been observed reading either one.
#
# Missing is a WARN, not a FAIL: this script is the first thing a participant
# runs, and on a fresh workstation init.sh has legitimately not run yet. What
# it must never do is stay quiet, because the downstream symptom is agy
# reporting that a glossary term does not exist rather than any config error.
MCP_FOUND=()
MCP_ABSENT=()
for CANDIDATE in "${HOME}/.gemini/antigravity-cli/mcp_config.json" \
                 "${HOME}/.gemini/config/mcp_config.json"; do
    if [ -f "${CANDIDATE}" ]; then
        MCP_FOUND+=("${CANDIDATE}")
    else
        MCP_ABSENT+=("${CANDIDATE}")
    fi
done

if [ ${#MCP_FOUND[@]} -eq 0 ]; then
    echo "[WARN] No agy MCP config at either path:"
    for MISSING_CFG in "${MCP_ABSENT[@]}"; do echo "         ${MISSING_CFG}"; done
    echo "       Run init.sh first:  bash init.sh"
    echo "       It generates both files with your project id. Until then agy has"
    echo "       no MCP servers and Step 3 (agy + glossary) cannot work."
else
    for MCP_CFG in "${MCP_FOUND[@]}"; do
        if python3 -c "
import json, sys
d = json.load(open('${MCP_CFG}')).get('mcpServers', {})
sys.exit(0 if {'bigquery', 'knowledge-catalog'} <= set(d) else 1)
" 2>/dev/null; then
            echo "[PASS] agy MCP config lists bigquery and knowledge-catalog (${MCP_CFG})"
        else
            echo "[FAIL] ${MCP_CFG} does not list both MCP servers."
            echo "       agy cannot reach the Knowledge Catalog, so Step 3 cannot work."
            echo "       Regenerate it:  rm -f ${MCP_CFG} && bash init.sh"
            MCP_BROKEN=1
        fi
    done
    if [ ${#MCP_ABSENT[@]} -gt 0 ]; then
        for MISSING_CFG in "${MCP_ABSENT[@]}"; do
            echo "[WARN] ${MISSING_CFG} is missing (the other path is present)."
            echo "       Run 'bash init.sh' to write both."
        done
    fi
fi

#--- 4b. Cloud Run services, and the identity split they exist to create ---
# Step 7 is only a demonstration if these two services run as DIFFERENT service
# accounts against one policy tag. Assert it here rather than let a participant
# discover at Step 7 that both panels show the same thing.
CR_REGION="${CLOUD_RUN_REGION:-us-central1}"
RUNNER_SA_ACTUAL="$(gcloud run services describe lab-runner --region="${CR_REGION}" \
    --project="${PROJECT_ID}" --format='value(spec.template.spec.serviceAccountName)' 2>/dev/null || true)"
AGENT_SA_ACTUAL="$(gcloud run services describe ecommerce-agent --region="${CR_REGION}" \
    --project="${PROJECT_ID}" --format='value(spec.template.spec.serviceAccountName)' 2>/dev/null || true)"

if [ -z "${RUNNER_SA_ACTUAL}" ] && [ -z "${AGENT_SA_ACTUAL}" ]; then
    echo "[WARN] Neither Cloud Run service is deployed yet. Expected before deploy.sh:"
    echo "         bash init.sh"
    echo "         # complete TODO_2 and TODO_3 in agent/agent.py with agy"
    echo "         bash deploy.sh"
elif [ -z "${RUNNER_SA_ACTUAL}" ] || [ -z "${AGENT_SA_ACTUAL}" ]; then
    echo "[WARN] Only one service is deployed. Re-run: bash deploy.sh"
elif [ "${RUNNER_SA_ACTUAL}" = "${AGENT_SA_ACTUAL}" ]; then
    echo "[FAIL] Both services run as ${AGENT_SA_ACTUAL}."
    echo "       Step 7 would show identical output on both sides and prove nothing."
    echo "       Check serviceAccountName in both service.yaml files, then re-deploy."
else
    echo "[PASS] Two services, two identities:"
    echo "         lab-runner      ${RUNNER_SA_ACTUAL}"
    echo "         ecommerce-agent ${AGENT_SA_ACTUAL}"
fi

#--- 5/6/7. BigQuery objects ---
# Report WHY, not just that it failed. `bq show ... &>/dev/null` treats every
# failure as "not found", so an expired credential, a wrong quota project or a
# permission denial all print as a missing dataset -- and the participant goes
# looking for infrastructure that is sitting there in perfect health.
check_bq_object() {   # check_bq_object <label> <object>
    local label="$1" obj="$2" out rc
    out="$(bq --project_id="${PROJECT_ID}" show --format=none "${obj}" 2>&1)"
    rc=$?
    if [ "${rc}" -eq 0 ]; then
        echo "[PASS] BigQuery ${label} exists"
        return 0
    fi

    echo "[FAIL] BigQuery ${label} — 'bq show ${obj}' failed:"
    # Print it whole. A bq crash ends with the traceback that names the actual
    # exception, and truncating leaves only the generic "you have encountered a
    # bug" preamble -- which says nothing about what to fix.
    printf '%s\n' "${out}" | sed 's/^/       /' | head -25

    case "${out}" in
        *"Not found"*|*"not found"*)
            echo "       The object really is missing. Re-apply the lab infrastructure." ;;
        *"Permission denied"*|*"Access Denied"*|*PERMISSION_DENIED*)
            echo "       This is a PERMISSIONS problem, not a missing dataset."
            echo "       Check which identity this shell is using: bash whoami.sh" ;;
        *"credentials"*|*"Reauthentication"*|*"invalid_grant"*|*"expired"*)
            echo "       Your credentials are stale. Refresh them:"
            echo "         gcloud auth login && gcloud auth application-default login" ;;
        *"encountered a bug"*|*Traceback*)
            echo "       The bq CLI itself crashed — this is not about your infrastructure."
            echo "       Re-run the command alone to see the whole traceback:"
            echo "         bq --project_id=${PROJECT_ID} show ${obj}"
            echo "       Most often a stale credential cache. Try, in order:"
            echo "         gcloud auth login && gcloud auth application-default login"
            echo "         deactivate            # if a Python venv is active"
            echo "         gcloud components update" ;;
        *)
            echo "       Confirm the project first:"
            echo "         gcloud config get-value project"
            echo "         bash whoami.sh" ;;
    esac
    return 1
}

check_bq_object "dataset ${DATASET_ID}" "${PROJECT_ID}:${DATASET_ID}" || exit 1
for TABLE in "${TABLES[@]}"; do
    check_bq_object "table ${DATASET_ID}.${TABLE}" "${PROJECT_ID}:${DATASET_ID}.${TABLE}" || exit 1
done
check_bq_object "dataset ${GRAPH_DATASET_ID}" "${PROJECT_ID}:${GRAPH_DATASET_ID}" || exit 1

#--- 8. Required APIs ---
ENABLED_APIS="$(gcloud services list --enabled --project="${PROJECT_ID}" --format='value(config.name)' 2>/dev/null || true)"
for API in "${REQUIRED_APIS[@]}"; do
    if echo "${ENABLED_APIS}" | grep -qx "${API}"; then
        echo "[PASS] API enabled: ${API}"
    else
        echo "[FAIL] API not enabled: ${API}"
        exit 1
    fi
done

#--- 9. curl, needed to reach the MCP endpoints ---
if command -v curl &>/dev/null; then
    echo "[PASS] curl available"
else
    echo "[FAIL] curl not found; the agent's MCP endpoints cannot be verified"
    exit 1
fi

#--- 10. ADC access token (the MCP servers authenticate with OAuth 2.0) ---
ACCESS_TOKEN="$(gcloud auth application-default print-access-token 2>/dev/null || gcloud auth print-access-token 2>/dev/null || true)"
if [ -n "${ACCESS_TOKEN}" ]; then
    echo "[PASS] OAuth access token obtained from ADC"
else
    echo "[FAIL] Could not mint an access token; run: gcloud auth login"
    exit 1
fi

#--- 11. Remote MCP servers ---
check_mcp_server "BigQuery" "${BIGQUERY_MCP_URL}" "${BIGQUERY_MCP_TOOLS[@]}" || exit 1
check_mcp_server "Knowledge Catalog" "${CATALOG_MCP_URL}" "${CATALOG_MCP_TOOLS[@]}" || exit 1

#--- 12. The credentials actually work against both servers ---
check_mcp_auth "BigQuery" "${BIGQUERY_MCP_URL}" \
    "{\"projectId\":\"${PROJECT_ID}\"}" "list_dataset_ids" || exit 1
check_mcp_auth "Knowledge Catalog" "${CATALOG_MCP_URL}" \
    "{\"projectId\":\"${PROJECT_ID}\",\"query\":\"system=bigquery\",\"pageSize\":1}" "search_entries" || exit 1

#--- 13. ADK MCP client (advisory: dependencies are installed after this runs) ---
# The dependencies go into ~/.venv, so check THAT interpreter. Checking the
# system python3 would report a permanent [WARN] no matter how correct the
# install was, and would teach participants to ignore this line.
if [ -x "${HOME}/.venv/bin/python" ]; then
    PY_BIN="${HOME}/.venv/bin/python"
    PY_WHERE="~/.venv"
else
    PY_BIN="python3"
    PY_WHERE="system python3 (no ~/.venv found)"
fi

if "${PY_BIN}" -c "from google.adk.tools.mcp_tool import McpToolset, StreamableHTTPConnectionParams" 2>/dev/null; then
    echo "[PASS] ADK MCP client importable (${PY_WHERE})"
else
    echo "[WARN] ADK MCP client not importable yet (checked ${PY_WHERE})."
    echo "       Expected before the install step. Create the environment with:"
    echo "         python3 -m venv ~/.venv"
    echo "         ~/.venv/bin/pip install -r requirements.txt"
    echo "       If it persists afterwards, check the mcp version: google-adk needs"
    echo "       mcp>=1.24,<2, and mcp 2.x fails with 'cannot import name"
    echo "       McpHttpClientFactory'. Fix with: ~/.venv/bin/pip install 'mcp>=1.24,<2'"
fi

if [ "${MCP_BROKEN:-0}" -eq 1 ]; then
    echo "[FAIL] Day-0 verification finished with failures — see the [FAIL] lines above."
    exit 1
fi
echo "[PASS] Day-0 verification completed for BigQuery Agentic Data Cloud"
echo "[INFO] From guide Step 6 on, the lab console URL (service lab-runner) is printed by deploy.sh."
echo "       Unsure which identity is which? bash whoami.sh"
exit 0
