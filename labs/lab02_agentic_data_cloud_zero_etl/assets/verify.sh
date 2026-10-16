#!/usr/bin/env bash
# Author: Wissem Khlifi - August 2026
# ==============================================================================
# Lab 11 — Day-0 verification
#
#   bash verify.sh
#
# Emits [PASS] / [FAIL] lines, which the Data Forge smoke framework parses
# straight into the event test report. A [FAIL] here means a participant would
# hit a wall; a warning means something is degraded but the lab still runs.
#
# The data checks are the point. A property graph declared over EMPTY tables
# succeeds and returns nothing, and a federated view over a failed export
# returns zero rows rather than an error — so every data assertion below checks a
# ROW COUNT, not the absence of an exception.
# ==============================================================================
set -uo pipefail

FAILURES=0
fail() { echo "[FAIL] $1"; FAILURES=$((FAILURES + 1)); }
pass() { echo "[PASS] $1"; }
warn() { echo "[WARN] $1"; }

cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null || true

echo "=============================================================="
echo " Agentic Data Cloud: Zero-ETL Federation — Day-0 verification"
echo "=============================================================="

# --------------------------------------------------------------------------
# 1. Workstation basics
# --------------------------------------------------------------------------
command -v gcloud  >/dev/null 2>&1 && pass "Google Cloud SDK present" || { fail "gcloud not found"; exit 1; }
command -v bq      >/dev/null 2>&1 && pass "bq CLI present"           || fail "bq not found (ships with gcloud)"
command -v python3 >/dev/null 2>&1 && pass "Python runtime available ($(python3 --version 2>&1))" || fail "python3 not found"
command -v curl    >/dev/null 2>&1 && pass "curl available"           || fail "curl not found"

PROJECT_ID="${PROJECT_ID:-${GOOGLE_CLOUD_PROJECT:-${GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null)}}}"
if [ -n "${PROJECT_ID}" ] && [ "${PROJECT_ID}" != "(unset)" ]; then
    pass "Active GCP project resolved (${PROJECT_ID})"
else
    fail "No active GCP project configured"
    exit 1
fi

IMP="$(gcloud config get-value auth/impersonate_service_account 2>/dev/null || true)"
if [ -z "${IMP}" ] || [ "${IMP}" = "(unset)" ]; then
    pass "Running as you, not impersonating a service account"
else
    fail "CLI impersonation is active (${IMP}) — run: gcloud config unset auth/impersonate_service_account"
fi

# Defaults mirror init.sh; .deploy.env overrides them when it exists.
SPANNER_INSTANCE="${SPANNER_INSTANCE:-zeroetl-ops}"
SPANNER_DATABASE="${SPANNER_DATABASE:-ecommerce-operational}"
ALLOYDB_REGION="${ALLOYDB_REGION:-us-central1}"
ALLOYDB_CLUSTER="${ALLOYDB_CLUSTER:-zeroetl-cluster}"
SOURCE_DATASET="${BQ_SOURCE_DATASET_ID:-ecommerce_lab}"
ZEROETL_DATASET="${BQ_ZEROETL_DATASET_ID:-ecommerce_zeroetl}"
SPANNER_DATASET="${BQ_SPANNER_DATASET_ID:-spanner_federated}"
[ -f .deploy.env ] && { set -a; . ./.deploy.env; set +a; pass ".deploy.env found (init.sh has run)"; } \
                   || warn ".deploy.env not found — run: bash init.sh"

bqq() {   # bqq <sql>  -> single scalar on stdout, empty on failure
    # stderr goes to a temp file, not /dev/null: a failed query and an empty
    # table both yield an empty scalar, and only the error text tells them apart.
    local errf out
    errf="$(mktemp)"
    if out="$(bq query --project_id="${PROJECT_ID}" --location=US --use_legacy_sql=false \
            --format=csv --quiet --nouse_cache "$1" 2>"${errf}")"; then
        printf '%s\n' "${out}" | tail -n 1
    else
        echo "[INFO] bq query failed: $(grep -m1 -v '^[[:space:]]*$' "${errf}" | cut -c1-240)" >&2
    fi
    rm -f "${errf}"
}

# --------------------------------------------------------------------------
# 2. Lab 10's infrastructure — Lab 11 depends on it and creates none of it
# --------------------------------------------------------------------------
for DS in "${SOURCE_DATASET}" ecommerce_graph_lab; do
    bq show --project_id="${PROJECT_ID}" "${PROJECT_ID}:${DS}" >/dev/null 2>&1 \
        && pass "From Data to Agent dataset ${DS} exists" \
        || fail "From Data to Agent dataset ${DS} missing — this lab depends on it; apply it first"
done

# Ask which reservation THIS project's queries are assigned to, not which
# reservations it owns: in an event the 'graph-lab' reservation lives in the
# shared admin project, so listing reservations here finds nothing even though
# GRAPH_TABLE works. The assignee-side search sees assignments in any project.
ASSIGNMENT="$(bq show --reservation_assignment --project_id="${PROJECT_ID}" --location=US \
    --job_type=QUERY --assignee_type=PROJECT --assignee_id="${PROJECT_ID}" 2>/dev/null \
    | grep -o '[^ ]*graph-lab[^ ]*' | head -n 1 || true)"
[ -n "${ASSIGNMENT}" ] \
    && pass "QUERY jobs assigned to ENTERPRISE reservation 'graph-lab' (${ASSIGNMENT%%:*}) — GRAPH_TABLE needs it" \
    || warn "no QUERY assignment to reservation 'graph-lab' — BigQuery Graph traversals will be refused until From Data to Agent creates it"

# --------------------------------------------------------------------------
# 3. The three engines
# --------------------------------------------------------------------------
SPANNER_STATE="$(gcloud spanner databases describe "${SPANNER_DATABASE}" \
    --instance="${SPANNER_INSTANCE}" --project="${PROJECT_ID}" \
    --format='value(state)' 2>/dev/null || true)"
[ "${SPANNER_STATE}" = "READY" ] \
    && pass "Spanner ${SPANNER_INSTANCE}/${SPANNER_DATABASE} is READY" \
    || fail "Spanner database state is '${SPANNER_STATE:-missing}'"

SPANNER_EDITION="$(gcloud spanner instances describe "${SPANNER_INSTANCE}" \
    --project="${PROJECT_ID}" --format='value(edition)' 2>/dev/null || true)"
[ "${SPANNER_EDITION}" = "ENTERPRISE" ] \
    && pass "Spanner instance is ENTERPRISE edition (Spanner Graph requires it)" \
    || fail "Spanner instance edition is '${SPANNER_EDITION:-unknown}' — Spanner Graph needs ENTERPRISE"

ALLOYDB_PRESENT=0
if gcloud alloydb clusters describe "${ALLOYDB_CLUSTER}" --region="${ALLOYDB_REGION}" \
     --project="${PROJECT_ID}" >/dev/null 2>&1; then
    ALLOYDB_PRESENT=1
    pass "AlloyDB cluster ${ALLOYDB_CLUSTER} exists"
else
    warn "AlloyDB cluster ${ALLOYDB_CLUSTER} not found — expected if this event was
       provisioned with enable_alloydb = false; the lab runs on BigQuery + Spanner"
fi

# --------------------------------------------------------------------------
# 4. Did the reverse ETL actually land? (row counts, not exceptions)
# --------------------------------------------------------------------------
BQ_ORDERS="$(bqq "SELECT COUNT(*) FROM \`${PROJECT_ID}.${SOURCE_DATASET}.orders\`")"
[ "${BQ_ORDERS:-0}" -gt 0 ] 2>/dev/null \
    && pass "BigQuery orders populated (${BQ_ORDERS} rows)" \
    || fail "BigQuery orders is empty or unreadable"

SP_ORDERS="$(bqq "SELECT COUNT(*) FROM \`${PROJECT_ID}.${SPANNER_DATASET}.CustomerOrders\`")"
if [ "${SP_ORDERS:-0}" -gt 0 ] 2>/dev/null; then
    pass "EXPORT DATA -> Spanner landed, read back through federation (${SP_ORDERS} rows)"
    [ "${SP_ORDERS}" = "${BQ_ORDERS}" ] \
        && pass "Spanner and BigQuery agree on the row count (${SP_ORDERS})" \
        || warn "row counts differ: BigQuery ${BQ_ORDERS}, Spanner ${SP_ORDERS} — an
       export may have been truncated, or an agent has already inserted rows"
else
    fail "Spanner has no order rows, or the external dataset ${SPANNER_DATASET} is unreadable"
fi

SP_STRING_PK="$(gcloud spanner databases execute-sql "${SPANNER_DATABASE}" \
    --instance="${SPANNER_INSTANCE}" --project="${PROJECT_ID}" \
    --sql="SELECT spanner_type FROM information_schema.columns WHERE table_name='CustomerOrders' AND column_name='customer_id'" \
    --format='value(rows[0])' 2>/dev/null || true)"
case "${SP_STRING_PK}" in
    STRING*) pass "Spanner customer_id is ${SP_STRING_PK} — ids are STRING in all three engines" ;;
    "")      warn "could not read the Spanner column type" ;;
    *)       fail "Spanner customer_id is ${SP_STRING_PK}, not STRING — the unified view will fail to compile" ;;
esac

if [ "${ALLOYDB_PRESENT}" -eq 1 ]; then
    AD_ORDERS="$(bqq "SELECT COUNT(*) FROM EXTERNAL_QUERY('${PROJECT_ID}.us.alloydb_conn', '''SELECT 1 FROM orders''')")"
    if [ "${AD_ORDERS:-0}" -gt 0 ] 2>/dev/null; then
        pass "EXPORT DATA -> AlloyDB landed, read back through EXTERNAL_QUERY (${AD_ORDERS} rows)"
        [ "${AD_ORDERS}" -ge "${BQ_ORDERS:-0}" ] 2>/dev/null \
            && pass "AlloyDB holds at least the ${BQ_ORDERS} seeded order items" \
            || warn "AlloyDB has ${AD_ORDERS} order items, fewer than the ${BQ_ORDERS} in BigQuery — the export may have been truncated"
    else
        # FAIL, not WARN: the cluster exists, so this event was provisioned WITH
        # AlloyDB and every participant would hit an empty AlloyDB arm. The apply
        # still tolerates a failed EXPORT DATA (one step must not take a cohort
        # build down); Day-0 verification is where it has to surface.
        fail "AlloyDB cluster ${ALLOYDB_CLUSTER} exists but no order rows are reachable through EXTERNAL_QUERY — the AlloyDB arm of the lab is empty.
       Check the apply log for 'EXPORT DATA -> AlloyDB', and any '[INFO] bq query failed'
       line above: without it, a failed query and an empty table look the same."
    fi
fi

# --------------------------------------------------------------------------
# 5. The unified layer
# --------------------------------------------------------------------------
ORIGINS="$(bqq "SELECT COUNT(DISTINCT origin) FROM \`${PROJECT_ID}.${ZEROETL_DATASET}.unified_orders\`")"
if [ "${ORIGINS:-0}" -ge 2 ] 2>/dev/null; then
    if [ "${ORIGINS}" -ge 3 ] 2>/dev/null; then
        pass "unified_orders returns all three origins"
    elif [ "${ALLOYDB_PRESENT}" -eq 1 ]; then
        warn "unified_orders returns ${ORIGINS} origins, not 3 — the AlloyDB arm is missing (see the AlloyDB check above)"
    else
        warn "unified_orders returns ${ORIGINS} origins, not 3 — expected for an event provisioned with enable_alloydb = false"
    fi
else
    fail "unified_orders returned fewer than 2 origins — federation is not working"
fi

# A node view that is not unique silently multiplies every traversal result.
DUPES="$(bqq "SELECT COUNT(*) FROM (SELECT customer_id FROM \`${PROJECT_ID}.${ZEROETL_DATASET}.unified_customers\` GROUP BY customer_id HAVING COUNT(*) > 1)")"
[ "${DUPES:-1}" = "0" ] \
    && pass "unified_customers has a unique node key (no duplicate customer_id)" \
    || fail "unified_customers has ${DUPES} duplicated customer_id values — every graph traversal will over-count"

EDGE_DUPES="$(bqq "SELECT COUNT(*) FROM (SELECT edge_id FROM \`${PROJECT_ID}.${ZEROETL_DATASET}.unified_order_edges\` GROUP BY edge_id HAVING COUNT(*) > 1)")"
[ "${EDGE_DUPES:-1}" = "0" ] \
    && pass "unified_order_edges has a unique edge key" \
    || fail "unified_order_edges has ${EDGE_DUPES} duplicated edge_id values"

# --------------------------------------------------------------------------
# 6. Both graphs
# --------------------------------------------------------------------------
SP_GRAPH="$(gcloud spanner databases execute-sql "${SPANNER_DATABASE}" \
    --instance="${SPANNER_INSTANCE}" --project="${PROJECT_ID}" \
    --sql="GRAPH EcommerceOpsGraph MATCH (c:Customer)-[:PLACED]->(o:CustomerOrder)-[:HAS_ITEM]->(p:Product) RETURN COUNT(*) AS n" \
    --format='value(rows[0])' 2>/dev/null || true)"
[ "${SP_GRAPH:-0}" -gt 0 ] 2>/dev/null \
    && pass "Spanner Graph traverses the exported data (${SP_GRAPH} paths)" \
    || fail "Spanner Graph returned no paths — the graph exists but its tables are empty, or the DDL did not apply"

BQ_GRAPH="$(bqq "SELECT COUNT(*) FROM GRAPH_TABLE(\`${PROJECT_ID}.${ZEROETL_DATASET}.UnifiedEcommerceGraph\` MATCH (c:Customer)-[e:PURCHASED]->(p:Product) RETURN c.customer_id AS cid)")"
[ "${BQ_GRAPH:-0}" -gt 0 ] 2>/dev/null \
    && pass "BigQuery Graph traverses the unified views (${BQ_GRAPH} edges)" \
    || warn "BigQuery Graph returned nothing. The apply tolerates its failure.
       Two usual causes: the Enterprise reservation is still propagating, or
       the graph was never declared — lab console Step 10 has a recovery
       button to declare it."

# Category nodes: one per distinct category in the seed, derived at runtime
# rather than hard-coded, because the seed size is a Lab 10 decision.
SRC_CATEGORIES="$(bqq "SELECT COUNT(DISTINCT category) FROM \`${PROJECT_ID}.${SOURCE_DATASET}.products\` WHERE category IS NOT NULL")"
BQ_CATEGORIES="$(bqq "SELECT COUNT(*) FROM GRAPH_TABLE(\`${PROJECT_ID}.${ZEROETL_DATASET}.UnifiedEcommerceGraph\` MATCH (k:Category) RETURN k.category_id AS k)")"
if [ "${BQ_CATEGORIES:-0}" -gt 0 ] 2>/dev/null && [ "${BQ_CATEGORIES}" = "${SRC_CATEGORIES}" ]; then
    pass "UnifiedEcommerceGraph has one Category node per seed category (${BQ_CATEGORIES})"
else
    warn "UnifiedEcommerceGraph Category nodes: ${BQ_CATEGORIES:-none}, seed categories: ${SRC_CATEGORIES:-unknown}.
       The graph may predate the Category label — lab console Step 10 → Recovery re-declares it."
fi

# --------------------------------------------------------------------------
# 7. The three identities and what they cannot do
# --------------------------------------------------------------------------
for SA in lab11-dispatch-sa lab11-transact-sa lab11-runner-sa; do
    gcloud iam service-accounts describe "${SA}@${PROJECT_ID}.iam.gserviceaccount.com" \
        --project="${PROJECT_ID}" >/dev/null 2>&1 \
        && pass "service account ${SA} exists" \
        || fail "service account ${SA} missing"
done

POLICY="$(gcloud projects get-iam-policy "${PROJECT_ID}" --format=json 2>/dev/null || echo '{}')"
has_role() {   # has_role <sa-prefix> <role>
    echo "${POLICY}" | python3 -c "
import json, sys
policy = json.load(sys.stdin)
member = 'serviceAccount:$1@${PROJECT_ID}.iam.gserviceaccount.com'
for binding in policy.get('bindings', []):
    if binding.get('role') == '$2' and member in binding.get('members', []):
        sys.exit(0)
sys.exit(1)
" 2>/dev/null
}

has_role lab11-transact-sa roles/spanner.databaseUser \
    && pass "transact SA can write Spanner (spanner.databaseUser)" \
    || fail "transact SA lacks spanner.databaseUser — no write in this lab will succeed"

if has_role lab11-dispatch-sa roles/spanner.databaseUser; then
    fail "dispatch SA holds spanner.databaseUser — it must be read-only; the write-separation demonstration is void"
else
    pass "dispatch SA cannot write Spanner (structural, not prompted)"
fi

if has_role lab11-transact-sa roles/bigquery.dataViewer; then
    fail "transact SA holds bigquery.dataViewer — it must not read the warehouse"
else
    pass "transact SA cannot read BigQuery data (blast radius is the two operational tables)"
fi

# --------------------------------------------------------------------------
# 8. MCP servers
# --------------------------------------------------------------------------
TOKEN="$(gcloud auth application-default print-access-token 2>/dev/null || gcloud auth print-access-token 2>/dev/null || true)"
if [ -n "${TOKEN}" ]; then
    pass "OAuth access token obtained from ADC"
    for PAIR in "BigQuery|https://bigquery.googleapis.com/mcp" \
                "Knowledge Catalog|https://dataplex.googleapis.com/mcp" \
                "Spanner|https://spanner.googleapis.com/mcp" \
                "AlloyDB|https://alloydb.googleapis.com/mcp"; do
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
            && pass "${LABEL} MCP server reachable (${COUNT} tools)" \
            || fail "${LABEL} MCP server returned no tool list"
    done
else
    fail "Could not mint an access token; run: gcloud auth login"
fi

MCP_CFG="${HOME}/.gemini/config/mcp_config.json"
if [ -f "${MCP_CFG}" ]; then
    python3 -c "
import json, sys
servers = json.load(open('${MCP_CFG}')).get('mcpServers', {})
sys.exit(0 if {'bigquery','knowledge-catalog','spanner','alloydb'} <= set(servers) else 1)
" 2>/dev/null \
        && pass "agy MCP config lists all four servers" \
        || fail "${MCP_CFG} does not list all four servers — re-run: bash init.sh"
else
    warn "${MCP_CFG} not found — run: bash init.sh"
fi

# --------------------------------------------------------------------------
# 9. Deployment sources
# --------------------------------------------------------------------------
for F in agent/router.py agent/orchestrator.py agent/dispatch.py \
         transact/spanner_specialist.py transact/alloydb_specialist.py \
         lab-runner/main.py deploy.sh init.sh; do
    [ -f "${F}" ] || { fail "missing source file: ${F}"; continue; }
done
[ -f agent/router.py ] && pass "Deployment sources present"

grep -q 'ROUTER_INSTRUCTION = "TODO_A"' agent/router.py 2>/dev/null \
    && warn "TODO_A is not finished yet (expected before Step 4)" \
    || pass "TODO_A complete"
grep -q '"TRANSACT_SPANNER"[[:space:]]*:' agent/orchestrator.py 2>/dev/null \
    && pass "TODO_B complete" \
    || warn "TODO_B is not finished yet (expected before Step 5)"
grep -q 'SPANNER_INSTRUCTION = "TODO_C"' transact/spanner_specialist.py 2>/dev/null \
    && warn "TODO_C is not finished yet (expected before Step 6)" \
    || pass "TODO_C complete"

# --------------------------------------------------------------------------
echo "=============================================================="
if [ "${FAILURES}" -gt 0 ]; then
    echo "[FAIL] Day-0 verification finished with ${FAILURES} failure(s) — see the [FAIL] lines above."
    exit 1
fi
echo "[PASS] Day-0 verification completed for Agentic Data Cloud: Zero-ETL"
