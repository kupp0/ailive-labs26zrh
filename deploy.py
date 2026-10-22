#!/usr/bin/env python3
# ==============================================================================
# Data Forge: Resilient Parallel Local Loop Orchestrator (Deploy & Destroy)
# ==============================================================================
import os
import sys
import json
import time
import shutil
import random
import re
import zipfile
import signal
import threading
import argparse
import subprocess
import logging
from concurrent.futures import ThreadPoolExecutor

STATE_FILE = ".deploy_state.json"
PROJECTS_FILE = "projects.txt"
WORKDIR_BASE = ".deploy_workdirs"
PLUGIN_CACHE_DIR = ".terraform_plugin_cache"

# Configure local file logging
LOG_DIR = "deploy_logs"
LOG_FILE = None

def setup_logging():
    global LOG_FILE
    os.makedirs(LOG_DIR, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    LOG_FILE = os.path.join(LOG_DIR, f"deploy_{timestamp}.log")
    logging.basicConfig(
        filename=LOG_FILE,
        filemode="w",
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s"
    )
    return LOG_FILE

# ANSI Color Codes for Premium CLI Styling
COLOR_RESET = "\033[0m"
COLOR_BOLD = "\033[1m"
COLOR_GREEN = "\033[92m"
COLOR_YELLOW = "\033[93m"
COLOR_RED = "\033[91m"
COLOR_GRAY = "\033[90m"
COLOR_CYAN = "\033[96m"
COLOR_MAGENTA = "\033[95m"

BANNER = f"""{COLOR_CYAN}{COLOR_BOLD}
=================================================================
  🚀 DATA FORGE LOCAL PARALLEL PROVISIONING ENGINE 🚀
================================================================={COLOR_RESET}
"""

DESTROY_BANNER = f"""{COLOR_RED}{COLOR_BOLD}
=================================================================
  🔥 DATA FORGE LOCAL PARALLEL TEARDOWN ENGINE 🔥
================================================================={COLOR_RESET}
"""

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
FIRST_ERROR_LINE = re.compile(r"^[ \t│╷╵|]*(?:Error|ERROR):", re.MULTILINE)
PROJECT_MISSING = re.compile(
    r"project\s+['\"]?[a-z][a-z0-9-]{4,}['\"]?\s+(?:was\s+)?not\s+found"
    r"|cannot find project|project_not_found|project does not exist"
    r"|gcloud\.projects\.describe\)\s+not_found"
)


def first_error_block(text, max_lines=14):
    """Returns the first Terraform/gcloud error block in a log, or the whole text.

    A failing apply usually logs several errors; the later ones are often
    consequences of the first. Classifying the tail of the log labelled a
    missing prerequisite table as "Bad Project ID" because an unrelated
    "not found" happened to be at the end.

    Args:
        text: Full or partial log text.
        max_lines: How many lines of the first error block to keep.

    Returns:
        The first error block (up to max_lines lines), or `text` unchanged
        when it contains no error line.
    """
    text = ANSI_ESCAPE.sub("", str(text))  # terraform colours its diagnostics
    match = FIRST_ERROR_LINE.search(text)
    if not match:
        return text
    lines = text[match.start():].splitlines()
    block = [lines[0]]
    for line in lines[1:max_lines]:
        # Terraform closes each diagnostic with "╵"; a new error line starts the next one.
        if line.lstrip().startswith("╵") or FIRST_ERROR_LINE.match(line):
            break
        block.append(line)
    return "\n".join(block)


CAUSE_HINT = re.compile(r"does not exist|not found|denied|forbidden|invalid|✗|quota|already exists", re.IGNORECASE)


def first_error_line(text):
    """Summarises the first error: its headline plus the first line naming a cause.

    "Error: local-exec provisioner error" alone says nothing; the useful line
    ("Source dataset does not exist: ...") sits a few lines below it.

    Args:
        text: Full or partial log text.

    Returns:
        "<headline> — <cause>" (or just the headline), at most 240 characters.
    """
    lines = [ln.strip(" │╷╵|\t") for ln in first_error_block(text).splitlines()]
    lines = [ln for ln in lines if ln]
    if not lines:
        return ""
    headline = lines[0]
    cause = next((ln for ln in lines[1:] if CAUSE_HINT.search(ln)), "")
    summary = f"{headline} — {cause}" if cause and not CAUSE_HINT.search(headline) else headline
    return summary[:240]


def classify_error(error_text):
    """
    Classifies an error string into a standardized failure category,
    a concise badge, and actionable resolution advice.
    """
    raw = ANSI_ESCAPE.sub("", str(error_text))
    # Step 2b verdicts carry fixed markers. Match them before the generic
    # keywords below, which the GCP condition text inside them may also contain.
    if "CONFIG_DEGRADED" in raw:
        return (
            "WORKSTATION_DEGRADED",
            "WS Degraded",
            "The Cloud Workstation config is degraded, so the participant workstation cannot start.",
            "Follow the RUNBOOK in deploy_logs/[PROJECT_ID]_apply.log; confirm with ./deploy.sh --check-workstations."
        )
    if "workstation_health_unknown" in raw:
        return (
            "WORKSTATION_HEALTH_UNKNOWN",
            "WS Health Unknown",
            "The workstation config could not be verified after apply; it is NOT known to be healthy.",
            "Run ./deploy.sh --check-workstations --start before hand-off."
        )

    err = first_error_block(error_text).lower()
    
    if "access_token_type_unsupported" in err:
        return (
            "CBA_TOKEN_UNSUPPORTED",
            "CBA Token",
            "The account uses certificate-based access and its token was sent without the client certificate.",
            "Check `gcloud config get-value context_aware/use_client_certificate` is true (deploy.py then "
            "sets GOOGLE_API_USE_CLIENT_CERTIFICATE=true), or deploy from Cloud Shell."
        )
    if any(kw in err for kw in ["oauth2: access_denied", "invalid_grant", "restricted", "credentials not found", "application default credentials"]):
        return (
            "ADC_AUTH_EXPIRED",
            "ADC Expired",
            "Application Default Credentials (ADC) token missing or restricted.",
            "Run: gcloud auth application-default login"
        )
    if PROJECT_MISSING.search(err) and "service account" not in err:
        return (
            "PROJECT_NOT_FOUND",
            "Bad Project ID",
            "Project does not exist or project ID is mistyped in projects.txt.",
            "Check projects.txt: use the GCP project ID, not the participant's username."
        )
    if any(kw in err for kw in ["not found", "not_found", "does not exist", "404"]):
        return (
            "MISSING_RESOURCE",
            "Missing Resource",
            "A resource this deployment reads does not exist (yet).",
            "Check module ordering / prerequisite labs and the first error in deploy_logs/[PROJECT_ID]_apply.log."
        )
    if any(kw in err for kw in ["permission denied", "forbidden", "403 forbidden", "unauthorized", "caller does not have permission"]):
        return (
            "IAM_UNAUTHORIZED",
            "Permission Denied",
            "Caller account does not have sufficient IAM permissions on this project.",
            "Deployer needs Editor + Project IAM Admin; lab-specific roles come from deployer_iam_roles in lab.yaml."
        )
    if any(kw in err for kw in ["state locked", "412 precondition", "tflock", "lock info"]):
        return (
            "STATE_LOCK",
            "State Lock",
            "Terraform remote state lock contention in GCS bucket.",
            "Run: gcloud storage rm gs://[PROJECT_ID]-tfstate/terraform/state/default.tflock"
        )
    if any(kw in err for kw in ["429", "quota", "rate limit", "too many requests", "resource_exhausted"]):
        return (
            "QUOTA_RATE_LIMIT",
            "Rate Limited",
            "GCP API rate limit or quota exceeded.",
            "Transient rate limit. Before hand-off only: re-run ./deploy.sh with --max-workers 5."
        )
    if any(kw in err for kw in ["500", "503", "internal error", "internal exception", "service unavailable", "backend error"]):
        return (
            "SERVICE_UNAVAILABLE",
            "GCP 503/500",
            "Transient GCP internal API error.",
            "Transient backend issue. Before hand-off only: re-run ./deploy.sh (resumes from remote state)."
        )
    if any(kw in err for kw in ["billing not enabled", "billing_disabled", "account is disabled"]):
        return (
            "BILLING_DISABLED",
            "Billing Disabled",
            "Cloud Billing is not enabled or linked to this GCP project.",
            "Link a Cloud Billing account to the project in Google Cloud Console."
        )
        
    return (
        "TERRAFORM_ERROR",
        "Apply Error",
        "Terraform apply failed. Inspect the detailed execution log.",
        "Inspect log: deploy_logs/[PROJECT_ID]_apply.log"
    )

def is_retryable_error(error_text):
    """
    Distinguishes transient network/rate-limit glitches and eventual consistency
    delays from permanent fatal errors.
    Fatal errors (auth expired, missing project, unauthorized IAM) FAIL FAST.
    """
    err = str(error_text).lower()

    # 0. Known GCP eventual consistency delays that are retryable:
    # Service account propagation delay across IAM regions/cells:
    if "service account" in err and ("does not exist" in err or "not found" in err):
        return True
    # Resource initialization or creation still in progress:
    if any(phrase in err for phrase in ["is being created", "is still creating", "pending_create", "not ready", "timed out waiting"]):
        return True

    # 1. Hard fatal indicators -> NEVER retry
    fatal_keywords = [
        "not found", "does not exist", "permission denied", "forbidden",
        "account restricted", "invalid_grant", "unauthorized", "unauthenticated",
        "credentials not found", "403", "404", "invalid project", "billing not enabled",
        "billing_disabled"
    ]
    if any(kw in err for kw in fatal_keywords):
        return False
        
    # 2. Transient retryable indicators
    transient_keywords = [
        "429", "quota", "rate limit", "too many requests", "resource_exhausted",
        "service unavailable", "503", "500", "internal error", "internal exception", "try again later",
        "deadline exceeded", "timeout", "connection reset", "broken pipe"
    ]
    return any(kw in err for kw in transient_keywords)

def check_environment_and_auth():
    """
    Verifies that gcloud CLI, Terraform, and Application Default Credentials (ADC)
    are installed, active, and properly aligned before starting execution.
    """
    print(f"🔍 Step 0: Checking environment & authentication consistency...")
    
    # 1. Check gcloud CLI active account
    res_gcloud = subprocess.run(["gcloud", "config", "get-value", "account"], capture_output=True, text=True)
    active_account = res_gcloud.stdout.strip()
    if not active_account:
        print(f"\n{COLOR_RED}❌ ERROR: No active gcloud account found.{COLOR_RESET}")
        print(f"👉 Please authenticate with your Google account:")
        print(f"   {COLOR_GREEN}gcloud auth login{COLOR_RESET}\n")
        sys.exit(1)
        
    # 2. Check active OAuth2 token validity
    try:
        token = get_active_oauth_token()
        if not token:
            raise Exception("Empty access token returned by gcloud auth print-access-token")
    except Exception as e:
        print(f"\n{COLOR_RED}================================================================={COLOR_RESET}")
        print(f"{COLOR_RED}❌ AUTHENTICATION ERROR: Could not obtain OAuth2 token from gcloud{COLOR_RESET}")
        print(f"{COLOR_RED}================================================================={COLOR_RESET}")
        print(f"Active gcloud account is: {COLOR_BOLD}{active_account}{COLOR_RESET}")
        print(f"Details: {e}")
        print(f"\n👉 {COLOR_BOLD}Run the following command to authenticate:{COLOR_RESET}")
        print(f"   {COLOR_GREEN}{COLOR_BOLD}gcloud auth login{COLOR_RESET}\n")
        sys.exit(1)
        
    # 3. Check Terraform binary
    res_tf = subprocess.run(["terraform", "version"], capture_output=True, text=True)
    if res_tf.returncode != 0:
        print(f"\n{COLOR_RED}❌ ERROR: Terraform is not working properly.{COLOR_RESET}")
        print(res_tf.stderr)
        sys.exit(1)
        
    logging.info(f"Pre-flight check passed: Active gcloud account '{active_account}' with valid OAuth2 access token.")
    print(f"  {COLOR_GREEN}✔{COLOR_RESET} Authenticated as: {COLOR_BOLD}{active_account}{COLOR_RESET} (OAuth token verified & active)")
    # The identity behind the token Terraform will use. get_active_oauth_token()
    # already aligned it to the active account where it could; say so if not.
    tf_identity = token_email(token)
    if tf_identity and tf_identity.lower() != active_account.lower():
        print(f"  {COLOR_RED}⚠{COLOR_RESET}  Terraform will act as {COLOR_BOLD}{tf_identity}{COLOR_RESET}, "
              f"not {active_account}. Align them with: gcloud auth application-default login")
    elif _TOKEN_SOURCE == "gcloud":
        print(f"  {COLOR_GREEN}✔{COLOR_RESET} ADC is a different identity; using the gcloud token for {active_account}")

# ------------------------------------------------------------------------------
# Admin Project
#
# An event whose labs declare `infra.admin_module_source` needs exactly one
# `role=admin` row in projects.txt. That project is applied in Phase 0 (admin
# Terraform, then `admin_post_deploy` hooks) before any participant project,
# and destroyed only after every participant project is gone
# (`admin_pre_destroy` hooks, then admin Terraform). This file knows nothing
# about what the labs put there; the scaffolder resolves it into
# 00-admin-infra/ and event-manifest.json.
# ------------------------------------------------------------------------------
ADMIN_DIR = "00-admin-infra"
ADMIN_STATE_PREFIX = "terraform/admin-state"
EVENT_MANIFEST = "event-manifest.json"
_MANIFEST_CACHE = None


def parse_trailing_fields(tokens: list[str]) -> tuple[str, str, dict[str, str]]:
    """Parses projects.txt fields after PROJECT_ID, IAP_MEMBER, REGION.

    Trailing fields are order-independent `key=value` tokens (`role=admin`,
    `city=Tokyo`). A bare token is accepted for backward compatibility: one
    containing "admin" is the role, the first other bare token is the city.

    Args:
        tokens: Fields from index 3 onward, already stripped.

    Returns:
        (role, city, extras) where role is "admin" or "participant" (or another
        explicit value), city may be empty, and extras holds unknown keys.
    """
    role, city, extras = "participant", "", {}
    for token in tokens:
        if not token:
            continue
        if "=" in token:
            key, _, value = token.partition("=")
            key, value = key.strip().lower(), value.strip()
            if key == "role":
                role = "admin" if "admin" in value.lower() else (value.lower() or role)
            elif key == "city":
                city = value
            else:
                extras[key] = value
        elif "admin" in token.lower():
            role = "admin"
        elif not city:
            city = token
    return role, city, extras


def load_event_manifest() -> dict:
    """Reads event-manifest.json once and caches it.

    Returns:
        The manifest, or an empty dict when the file does not exist.

    Raises:
        RuntimeError: If the file exists but cannot be parsed. Treating that as
            "no hooks" would silently skip admin setup or teardown.
    """
    global _MANIFEST_CACHE
    if _MANIFEST_CACHE is not None:
        return _MANIFEST_CACHE
    if not os.path.exists(EVENT_MANIFEST):
        _MANIFEST_CACHE = {}
        return _MANIFEST_CACHE
    try:
        with open(EVENT_MANIFEST, "r") as f:
            _MANIFEST_CACHE = json.load(f)
    except (ValueError, OSError) as e:
        raise RuntimeError(f"{EVENT_MANIFEST} exists but could not be read: {e}") from e
    return _MANIFEST_CACHE


def requires_admin_project() -> bool:
    """Returns True if the scaffolded event contains an admin project module."""
    return bool(load_event_manifest().get("requires_admin_project")) or os.path.isdir(ADMIN_DIR)


def split_projects_by_role(projects: list[dict]) -> tuple[list[dict], list[dict]]:
    """Splits parsed projects.txt rows into (admin rows, participant rows)."""
    admins = [p for p in projects if p.get("role") == "admin"]
    participants = [p for p in projects if p.get("role") != "admin"]
    return admins, participants


def validate_admin_topology(valid_projects: list[dict], invalid_projects: list,
                            admin_required: bool) -> list[str]:
    """Checks the projects.txt shape against the event's admin requirement.

    Args:
        valid_projects: Reachable rows from parse_and_validate_projects().
        invalid_projects: (row, reason) pairs that failed the reachability probe.
        admin_required: Output of requires_admin_project().

    Returns:
        Human-readable errors; empty when the topology is deployable.
    """
    all_admin_rows = [p for p in valid_projects if p.get("role") == "admin"] + \
                     [row for row, _ in invalid_projects if row.get("role") == "admin"]
    admins, participants = split_projects_by_role(valid_projects)
    errors: list[str] = []
    if not admin_required:
        return errors
    if len(all_admin_rows) != 1:
        errors.append(
            f"Exactly 1 `role=admin` row is required; found {len(all_admin_rows)}. "
            f"Apply and teardown both target a single admin project."
        )
    elif not admins:
        errors.append(f"The `role=admin` project {all_admin_rows[0]['project_id']} is not reachable.")
    if not participants:
        errors.append("At least 1 participant project is required alongside the admin project.")
    return errors


def admin_teardown_blockers(builds: list[dict], invalid_projects: list) -> list[str]:
    """Returns reasons the admin project must NOT be destroyed yet.

    Participant state may reference admin resources (reservation assignments,
    grants to an admin service account). Destroying the admin project while any
    participant might still hold such references leaves dangling state, so the
    admin teardown waits until every participant row is confirmed destroyed.

    Args:
        builds: Participant build records after the destroy dashboard.
        invalid_projects: (row, reason) pairs skipped by the reachability probe.

    Returns:
        One entry per blocking participant; empty when teardown may proceed.
    """
    blockers = [
        f"{b['project_id']}: {b['status']}" for b in builds
        if b.get("role") != "admin" and b.get("status") != "DESTROYED"
    ]
    blockers += [
        f"{row['project_id']}: skipped ({reason})" for row, reason in invalid_projects
        if row.get("role") != "admin"
    ]
    return blockers


def apply_quota_overrides(project):
    """
    Applies the consumer quota CAPS declared by the selected labs.

    These only ever LOWER a limit, to contain hackathon spend. Cloud Spanner is
    the motivating case: the per-project default is 100 nodes, and an agent told
    to "make it fast" will happily allocate them.

    Failures are logged, not fatal -- the caller may lack serviceusage admin on
    the project, and an uncapped project is still a deployable project. The
    return code IS inspected, which is the whole point: the previous
    implementation discarded it, so a cap that never applied looked identical to
    one that did.
    """
    project_id = project["project_id"]
    region = project.get("region", "europe-west3")
    overrides = load_event_manifest().get("quota_overrides", [])
    if not overrides:
        return

    for override in overrides:
        service = override.get("service")
        metric = override.get("metric")
        unit = override.get("unit")
        value = override.get("value")
        if not all([service, metric, unit]) or value is None:
            logging.warning(f"[{project_id}] Skipping malformed quota_override entry: {override}")
            continue

        cmd = [
            "gcloud", "alpha", "services", "quota", "update",
            f"--service={service}",
            f"--consumer=projects/{project_id}",
            f"--metric={metric}",
            f"--unit={unit}",
            f"--value={value}",
            "--force"
        ]
        # `unit` is a literal template such as "1/{project}/{region}". The real
        # region is supplied as a DIMENSION, never substituted into the unit --
        # doing the latter addresses a bucket that does not exist and the API
        # rejects it.
        dimension = override.get("dimension")
        if dimension:
            cmd.insert(-1, f"--dimensions={dimension}={region}")

        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0:
            logging.info(f"[{project_id}] Capped {metric} at {value}.")
            print(f"  {COLOR_GREEN}✔{COLOR_RESET} [{project_id}] Capped {metric} at {value}")
        else:
            detail = (res.stderr or res.stdout).strip().splitlines()
            detail = detail[-1] if detail else "no output"
            logging.warning(f"[{project_id}] Could not cap {metric} at {value}: {detail}")
            print(f"  {COLOR_YELLOW}⚠{COLOR_RESET} [{project_id}] Quota cap on {metric} not applied: {detail}")


def parse_and_validate_projects(auto_yes=False):
    """
    Parses projects.txt, lints syntax/formats, and performs a fast reachability
    probe for each project. Partitions projects into valid and invalid sets.
    """
    logging.info(f"Parsing and validating projects file: {PROJECTS_FILE}")
    if not os.path.exists(PROJECTS_FILE):
        logging.error(f"Projects file not found: {PROJECTS_FILE}")
        print(f"{COLOR_RED}❌ ERROR: {PROJECTS_FILE} not found.{COLOR_RESET}")
        print("Copy projects.txt.example to projects.txt and populate your target GCP projects.")
        sys.exit(1)
        
    raw_entries = []
    with open(PROJECTS_FILE, "r") as f:
        for line_num, line in enumerate(f, 1):
            line_clean = line.split("#")[0].strip()
            if not line_clean:
                continue
            parts = [p.strip() for p in line_clean.split(",")]
            if len(parts) < 2:
                print(f"{COLOR_YELLOW}⚠️ Warning: Line {line_num} in {PROJECTS_FILE} is malformed (needs: PROJECT_ID, PARTICIPANT_EMAIL). Skipping.{COLOR_RESET}")
                continue
                
            project_id = parts[0]
            iap_member = parts[1]
            if "@" in iap_member and not any(iap_member.startswith(prefix) for prefix in ["user:", "group:", "serviceAccount:", "domain:"]):
                iap_member = f"user:{iap_member}"
            region = parts[2] if len(parts) >= 3 and parts[2] else "europe-west3"
            role, city, extras = parse_trailing_fields(parts[3:])

            raw_entries.append({
                "project_id": project_id,
                "user": iap_member,
                "region": region,
                "role": role,
                "city": city,
                "extras": extras,
                "line_num": line_num
            })
            
    if not raw_entries:
        print(f"{COLOR_RED}❌ ERROR: No valid project mappings found in {PROJECTS_FILE}.{COLOR_RESET}")
        sys.exit(1)
        
    print(f"\n📡 Probing {len(raw_entries)} project(s) for reachability and IAM authorization...")
    
    valid_projects = []
    invalid_projects = []
    
    # Fast synchronous reachability probe
    for entry in raw_entries:
        pid = entry["project_id"]
        
        # Heuristic 1: Detect username prefix mistake (devstar123 vs data-forge26tst-123)
        if re.match(r"^devstar\d+$", pid, re.IGNORECASE):
            suffix = pid.lower().replace("devstar", "")
            reason = f"Looks like a lab participant username ('{pid}') rather than a GCP Project ID. Did you mean 'data-forge26tst-{suffix}'?"
            invalid_projects.append((entry, reason))
            continue
            
        # Probe GCP Project describe
        probe_cmd = ["gcloud", "projects", "describe", pid, "--format=value(projectNumber)"]
        probe_res = subprocess.run(probe_cmd, capture_output=True, text=True)
        
        if probe_res.returncode == 0 and probe_res.stdout.strip():
            entry["project_number"] = probe_res.stdout.strip()
            valid_projects.append(entry)
        else:
            err_text = probe_res.stderr.strip()
            if "not found" in err_text.lower() or "404" in err_text:
                reason = "Project does not exist in Google Cloud. Verify project ID spelling."
            elif "permission denied" in err_text.lower() or "403" in err_text:
                reason = "Permission denied. Active account lacks Viewer/Owner permission on this project."
            else:
                reason = f"Describe failed: {err_text[:100]}"
            invalid_projects.append((entry, reason))
            
    if invalid_projects:
        print(f"\n{COLOR_YELLOW}================================================================={COLOR_RESET}")
        print(f"{COLOR_YELLOW}⚠️  PROJECT REACHABILITY WARNINGS ({len(invalid_projects)} Inaccessible / Invalid){COLOR_RESET}")
        print(f"{COLOR_YELLOW}================================================================={COLOR_RESET}")
        for entry, reason in invalid_projects:
            print(f"  • {COLOR_BOLD}{entry['project_id']}{COLOR_RESET} (Line {entry['line_num']}): {COLOR_RED}{reason}{COLOR_RESET}")
        print(f"{COLOR_YELLOW}================================================================={COLOR_RESET}\n")
        
        if not valid_projects:
            print(f"{COLOR_RED}❌ ERROR: None of the projects in {PROJECTS_FILE} are valid or accessible. Aborting.{COLOR_RESET}")
            sys.exit(1)
            
        if not auto_yes:
            prompt_msg = f"Proceed with provisioning the {COLOR_GREEN}{len(valid_projects)} valid project(s){COLOR_RESET} and skip the {COLOR_RED}{len(invalid_projects)} invalid one(s){COLOR_RESET}? (y/n): "
            choice = input(prompt_msg).strip().lower()
            if choice not in ("y", "yes"):
                print("Deployment aborted by user. Please correct projects.txt and try again.")
                sys.exit(0)
        else:
            print(f"⚡ Headless mode: Proceeding with {len(valid_projects)} valid project(s) (Skipping {len(invalid_projects)} invalid).")
            
    return valid_projects, invalid_projects

def clear_stale_tf_lock(project_id, state_prefix="terraform/state"):
    """Removes any stale Terraform state lock in GCS to prevent 412 Precondition errors."""
    lock_uri = f"gs://{project_id}-tfstate/{state_prefix}/default.tflock"
    logging.info(f"[{project_id}] Checking for stale Terraform state lock...")
    cmd = ["gcloud", "storage", "rm", lock_uri, f"--project={project_id}"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode == 0:
        logging.info(f"[{project_id}] Successfully removed stale state lock: {lock_uri}")
    else:
        logging.debug(f"[{project_id}] No active or stale state lock found.")
    return True, None

def prepare_single_project(project):
    """Ensures compute and resource manager APIs are enabled and GCE SA has Owner permissions."""
    project_id = project["project_id"]
    logging.info(f"[{project_id}] Starting prerequisites preparation...")
    
    try:
        # 1. Enable Required Core APIs
        for api in ["compute.googleapis.com", "cloudresourcemanager.googleapis.com"]:
            logging.info(f"[{project_id}] Enabling API: {api}")
            cmd = ["gcloud", "services", "enable", api, f"--project={project_id}"]
            for attempt in range(4):
                res = subprocess.run(cmd, capture_output=True, text=True)
                if res.returncode == 0:
                    break
                if is_retryable_error(res.stderr) and attempt < 3:
                    sleep_time = min(40, (2 ** attempt) * 3 + random.uniform(1, 3))
                    logging.info(f"[{project_id}] API {api} retryable error ({res.stderr.strip()}). Retrying in {sleep_time:.1f}s... (Attempt {attempt+1}/4)")
                    time.sleep(sleep_time)
                    continue
                cat, badge, desc, fix = classify_error(res.stderr)
                raise Exception(f"Failed to enable API {api} [{badge}]: {res.stderr.strip()}")
                
        # 2. Get Project Number (cached if already probed)
        project_number = project.get("project_number")
        if not project_number:
            logging.info(f"[{project_id}] Retrieving project number...")
            cmd = ["gcloud", "projects", "describe", project_id, "--format=value(projectNumber)"]
            for attempt in range(3):
                res = subprocess.run(cmd, capture_output=True, text=True)
                if res.returncode == 0 and res.stdout.strip():
                    project_number = res.stdout.strip()
                    break
                if is_retryable_error(res.stderr) and attempt < 2:
                    time.sleep((2 ** attempt) * 2 + random.uniform(1, 2))
                    continue
                cat, badge, desc, fix = classify_error(res.stderr)
                raise Exception(f"Failed to describe project [{badge}]: {res.stderr.strip()}")
        
        # 3. Configure Compute Engine Service Account IAM Bindings
        gce_sa = f"serviceAccount:{project_number}-compute@developer.gserviceaccount.com"
        for role in ["roles/owner", "roles/storage.objectAdmin"]:
            logging.info(f"[{project_id}] Granting {role} to Compute Engine Service Account...")
            cmd = [
                "gcloud", "projects", "add-iam-policy-binding", project_id,
                f"--member={gce_sa}", f"--role={role}"
            ]
            for attempt in range(4):
                res = subprocess.run(cmd, capture_output=True, text=True)
                if res.returncode == 0:
                    break
                if is_retryable_error(res.stderr) and attempt < 3:
                    sleep_time = min(40, (2 ** attempt) * 3 + random.uniform(1, 3))
                    time.sleep(sleep_time)
                    continue
                cat, badge, desc, fix = classify_error(res.stderr)
                raise Exception(f"Failed to grant {role} [{badge}]: {res.stderr.strip()}")
                
        # 4. Create Terraform remote state GCS bucket
        state_bucket = f"gs://{project_id}-tfstate"
        check_cmd = ["gcloud", "storage", "buckets", "describe", state_bucket, f"--project={project_id}"]
        res = subprocess.run(check_cmd, capture_output=True, text=True)
        if res.returncode != 0:
            create_cmd = [
                "gcloud", "storage", "buckets", "create", state_bucket,
                f"--project={project_id}", f"--location={project.get('region', 'europe-west3')}",
                "--uniform-bucket-level-access"
            ]
            for attempt in range(4):
                res = subprocess.run(create_cmd, capture_output=True, text=True)
                if res.returncode == 0 or "already exists" in res.stderr.lower() or "409" in res.stderr:
                    break
                if is_retryable_error(res.stderr) and attempt < 3:
                    time.sleep((2 ** attempt) * 3 + random.uniform(1, 2))
                    continue
                cat, badge, desc, fix = classify_error(res.stderr)
                raise Exception(f"Failed to create GCS state bucket {state_bucket} [{badge}]: {res.stderr.strip()}")
            
            # Enable versioning
            versioning_cmd = [
                "gcloud", "storage", "buckets", "update", state_bucket,
                f"--project={project_id}", "--versioning"
            ]
            for attempt in range(3):
                res = subprocess.run(versioning_cmd, capture_output=True, text=True)
                if res.returncode == 0:
                    break
                if is_retryable_error(res.stderr) and attempt < 2:
                    time.sleep(2)
                    continue
            
        # 5. Apply lab-declared consumer quota caps.
        #
        # Deliberately before Terraform: a cap applied afterwards would not
        # prevent the very first over-allocation it exists to prevent.
        apply_quota_overrides(project)

        # 6. Clear stale state locks
        clear_stale_tf_lock(project_id)
        return True, None
    except Exception as e:
        logging.error(f"[{project_id}] Failed during prerequisites: {e}")
        return False, str(e)

def get_active_account() -> str:
    """Returns the active gcloud account, or an empty string if unset."""
    res = subprocess.run(["gcloud", "config", "get-value", "account"], capture_output=True, text=True)
    account = res.stdout.strip()
    return "" if account in ("", "(unset)") else account


def _print_token(cmd: list[str]) -> str:
    """Runs a gcloud print-access-token command; returns the token or ''."""
    try:
        res = subprocess.run(cmd, capture_output=True, text=True)
    except OSError as e:
        logging.debug(f"{' '.join(cmd)} failed to start: {e}")
        return ""
    return res.stdout.strip() if res.returncode == 0 else ""


def token_email(token: str) -> str | None:
    """Returns the email an OAuth2 access token was issued to, or None if unknown.

    Uses the public tokeninfo endpoint. None covers every failure (network,
    token without the email scope, malformed reply) so callers can fall back
    to the previous behaviour instead of aborting.
    """
    import urllib.parse
    import urllib.request
    url = "https://oauth2.googleapis.com/tokeninfo?" + urllib.parse.urlencode({"access_token": token})
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            email = json.loads(resp.read().decode()).get("email")
    except (OSError, ValueError) as e:
        logging.debug(f"tokeninfo lookup failed: {e}")
        return None
    return email.strip() if isinstance(email, str) and email.strip() else None


# Which token source matches the active gcloud account ("adc" or "gcloud").
# Decided once per run: identities do not change mid-run, tokens do.
_TOKEN_SOURCE: str | None = None
_ADC_CMD = ["gcloud", "auth", "application-default", "print-access-token"]
_GCLOUD_CMD = ["gcloud", "auth", "print-access-token"]


def get_active_oauth_token() -> str:
    """Returns an OAuth2 access token for the ACTIVE gcloud account.

    The ADC token is preferred because some user accounts (e.g. corp accounts
    behind certificate-based access) get a gcloud CLI token that raw REST
    calls reject. But ADC is often a different identity than the active
    gcloud account. Using it then made Terraform act as that other identity
    while the pre-flight reported the gcloud account (AGENTS.md section 8,
    item 5). So the ADC token is used only when it belongs to the active
    account, or when that cannot be determined. On a confirmed mismatch the
    gcloud CLI token is used instead.

    Returns:
        A non-empty access token.

    Raises:
        subprocess.CalledProcessError: If no token can be obtained at all.
    """
    global _TOKEN_SOURCE
    if _TOKEN_SOURCE == "gcloud":
        token = _print_token(_GCLOUD_CMD)
        if token:
            return token
    elif _TOKEN_SOURCE == "adc":
        token = _print_token(_ADC_CMD)
        if token:
            return token
    else:
        adc = _print_token(_ADC_CMD)
        if adc:
            try:
                account = get_active_account()
            except (subprocess.CalledProcessError, OSError):
                account = ""
            adc_email = token_email(adc)
            if not account or adc_email is None or adc_email.lower() == account.lower():
                _TOKEN_SOURCE = "adc"
                return adc
            gcloud_token = _print_token(_GCLOUD_CMD)
            if gcloud_token:
                logging.warning(
                    f"ADC belongs to '{adc_email}' but the active gcloud account is "
                    f"'{account}'. Using the gcloud token so Terraform runs as '{account}'. "
                    f"Run `gcloud auth application-default login` as '{account}' to align them.")
                _TOKEN_SOURCE = "gcloud"
                return gcloud_token
            logging.warning(
                f"ADC belongs to '{adc_email}', not the active gcloud account '{account}', "
                f"and no gcloud token is available. Falling back to ADC.")
            return adc
    res = subprocess.run(_GCLOUD_CMD, capture_output=True, text=True, check=True)
    return res.stdout.strip()


def prepare_terraform_env(oauth_token):
    """
    Prepares environment variables for Terraform executions.
    Ensures credentials match the active gcloud account and auto-refresh during long operations.
    Falls back gracefully to standard ADC or OAuth access token.
    """
    env = os.environ.copy()
    env["TF_PLUGIN_CACHE_DIR"] = os.path.abspath(PLUGIN_CACHE_DIR)
    env["GOOGLE_OAUTH_ACCESS_TOKEN"] = oauth_token
    env["CLOUDSDK_AUTH_ACCESS_TOKEN"] = oauth_token
    # Remove file-based credentials so Terraform backend/provider uses the valid OAuth token directly
    env.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
    if uses_client_certificate():
        # The token is bound to the client certificate; without this the provider
        # and the GCS backend reject it with 401 ACCESS_TOKEN_TYPE_UNSUPPORTED.
        env.setdefault("GOOGLE_API_USE_CLIENT_CERTIFICATE", "true")
    return env


# Certificate-based access (CBA), e.g. a corp account on a managed workstation:
# gcloud presents the client certificate on its own, Terraform does not.
# Both values are resolved once; worker threads call the getters concurrently,
# so results are published under the lock and only once fully computed.
_CBA_LOCK = threading.RLock()
_USES_CLIENT_CERT: bool | None = None
_DEPLOYER_VAR_ARGS: list[str] | None = None


def uses_client_certificate() -> bool:
    """Returns True when gcloud is configured for certificate-based access.

    Decided once per run from `context_aware/use_client_certificate`.
    """
    global _USES_CLIENT_CERT
    with _CBA_LOCK:
        if _USES_CLIENT_CERT is None:
            try:
                res = subprocess.run(["gcloud", "config", "get-value", "context_aware/use_client_certificate"],
                                     capture_output=True, text=True, timeout=60)
                detected = res.returncode == 0 and res.stdout.strip().lower() == "true"
            except (OSError, subprocess.TimeoutExpired):
                detected = False
            _USES_CLIENT_CERT = detected
        return _USES_CLIENT_CERT


def _resolve_deployer_var_args() -> list[str]:
    """Computes deployer_var_args(); see there."""
    if not uses_client_certificate():
        return []
    root_tf = ""
    for path in ("main.tf", "variables.tf"):
        if os.path.exists(path):
            with open(path) as f:
                root_tf += f.read()
    if 'variable "deployer_member"' not in root_tf:
        return []  # an undeclared -var would make terraform fail
    if os.path.exists("terraform.tfvars"):
        with open("terraform.tfvars") as f:
            if re.search(r"^\s*deployer_member\s*=", f.read(), re.MULTILINE):
                return []  # the organizer's override wins
    account = get_active_account()
    if not account:
        return []
    kind = "serviceAccount" if account.endswith(".gserviceaccount.com") else "user"
    return ["-var", f"deployer_member={kind}:{account}"]


def deployer_var_args() -> list[str]:
    """Returns `-var deployer_member=...` for certificate-based-access accounts, else [].

    A CBA token has no userinfo email, so the root module's `deployer_member`
    local (scaffold.py compile_deployer_iam) cannot name the deploying identity.
    get_active_oauth_token() issues tokens for the active gcloud account, so that
    account is passed instead. Skipped when the root module declares no such
    variable or terraform.tfvars already sets it: an organizer override wins.
    Thread-safe; main() resolves it before any worker thread starts.

    Returns:
        A copy of the extra Terraform arguments (computed once per run).
    """
    global _DEPLOYER_VAR_ARGS
    with _CBA_LOCK:
        if _DEPLOYER_VAR_ARGS is None:
            _DEPLOYER_VAR_ARGS = _resolve_deployer_var_args()
        return list(_DEPLOYER_VAR_ARGS)


# --- Cloud Workstation health, guarded recreate, hand-off guard (#151) ---

# Every new gcloud/terraform call is bounded so one hung call cannot stall a worker thread.
GCLOUD_TIMEOUT = 120
TERRAFORM_INIT_TIMEOUT = 900
TERRAFORM_TIMEOUT = 5400  # one apply/plan; AlloyDB + Spanner applies take up to ~45 minutes

# Core workstation identifiers: 00-core-infra/main.tf, stitched by scaffold.py as `module "core"`.
WS_CLUSTER_ID = "workstation-cluster"
WS_CONFIG_ID = "workstation-config"
WS_ID = "my-workstation"
WS_CONFIG_ADDRESS = "module.core.google_workstations_workstation_config.default"
WS_ADDRESS = "module.core.google_workstations_workstation.default"

# Object in each participant state bucket; while it exists deploy.py refuses to apply.
HANDOFF_MARKER = "forge/handoff.json"

# Destroy error when a leftover workstation VM keeps the subnet in use, e.g.
# "... is already being used by 'projects/p/zones/z/instances/workstations-1a2b'".
WS_VM_IN_USE = re.compile(r"being used by\W+\S*instances/workstations-", re.IGNORECASE)

# FORGE_FAULT_INJECT_WS_DEGRADED=1 (tests only): report one degraded config per project per run.
_FAULT_INJECTED: set[str] = set()


def terraform_init_cmd(project_id: str, oauth_token: str, state_prefix: str = "terraform/state") -> list[str]:
    """Returns the `terraform init` argv for a participant project's GCS backend.

    Args:
        project_id: Participant project; its state bucket is `<project_id>-tfstate`.
        oauth_token: Access token pinned into the backend configuration.
        state_prefix: State object prefix inside the bucket.

    Returns:
        The argv list.
    """
    return [
        "terraform", "init", "-reconfigure",
        f"-backend-config=bucket={project_id}-tfstate",
        f"-backend-config=prefix={state_prefix}",
        f"-backend-config=access_token={oauth_token}",
    ]


def _run_gcloud_json(cmd: list[str], env: dict | None, timeout: int = GCLOUD_TIMEOUT) -> tuple[object, str]:
    """Runs a gcloud command that prints JSON.

    Args:
        cmd: Full gcloud argv (must request --format=json).
        env: Environment for the subprocess.
        timeout: Seconds before the call is abandoned.

    Returns:
        (parsed JSON or None, error text); the error text is empty on success.
    """
    try:
        res = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, f"timed out after {timeout}s: {' '.join(cmd[:4])}"
    if res.returncode != 0:
        return None, (res.stderr or res.stdout or f"exit {res.returncode}").strip()
    try:
        return json.loads(res.stdout or "null"), ""
    except ValueError:
        return None, "gcloud returned invalid JSON"


def inspect_workstation_config(project_id: str, region: str, env: dict | None,
                               attempts: int = 6, delay: float = 10.0) -> dict:
    """Classifies the core workstation config as HEALTHY, DEGRADED or UNKNOWN.

    Terraform reports success as soon as the create operation returns, even when
    the config comes back `degraded` (#151), so this asks the Workstations API.
    A failed lookup, or a config still reconciling after every attempt, is
    UNKNOWN and is never reported as healthy.

    Args:
        project_id: Participant project.
        region: Workstation cluster region.
        env: Environment for gcloud (should carry a fresh access token).
        attempts: Lookups before giving up.
        delay: Seconds between lookups.

    Returns:
        {"state": "HEALTHY" | "DEGRADED" | "UNKNOWN", "detail": str, "conditions": list[str]}
    """
    cmd = ["gcloud", "workstations", "configs", "describe", WS_CONFIG_ID,
           f"--cluster={WS_CLUSTER_ID}", f"--region={region}", f"--project={project_id}", "--format=json"]
    detail = "no lookup attempted"
    for attempt in range(attempts):
        cfg, err = _run_gcloud_json(cmd, env)
        if isinstance(cfg, dict):
            conditions = [f"code={c.get('code')} message={c.get('message')}"
                          for c in (cfg.get("conditions") or []) if isinstance(c, dict)]
            if cfg.get("degraded"):
                return {"state": "DEGRADED", "detail": "; ".join(conditions) or "degraded=true",
                        "conditions": conditions}
            if not cfg.get("reconciling"):
                return {"state": "HEALTHY", "detail": "degraded=false", "conditions": conditions}
            detail = "config still reconciling"
        else:
            detail = err or "empty response"
        if attempt < attempts - 1:
            time.sleep(delay)
    return {"state": "UNKNOWN", "detail": detail[:300], "conditions": []}


def workstation_in_state(workdir: str, env: dict) -> bool:
    """Returns True when the participant workstation already exists in Terraform state.

    Read before apply. A workstation that existed before this run may hold a
    participant's /home, so it is never recreated automatically. A state that
    cannot be read counts as "exists": when unsure, do nothing destructive.

    Args:
        workdir: Initialised per-project Terraform workdir.
        env: Terraform environment.

    Returns:
        True if the workstation is in state (or the state cannot be read).
    """
    try:
        res = subprocess.run(["terraform", "state", "list"], cwd=workdir, env=env,
                             capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return True
    if res.returncode != 0:
        # A first deploy has no state object yet; Terraform exits 1 with this
        # message. That is "not in state", not "unknown".
        return "no state file was found" not in f"{res.stderr}\n{res.stdout}".lower()
    return any(line.strip() == WS_ADDRESS for line in res.stdout.splitlines())


WS_RUNBOOK = """
RUNBOOK: '{config}' in {project} is degraded and its workstation existed before this run,
so /home may hold participant work. Nothing was changed automatically.
  1. If the participant's work matters, snapshot the home disk first:
       gcloud compute disks list --project={project} --filter="name~workstations"
       gcloud compute disks snapshot <disk> --zone=<zone> --project={project}
  2. Delete the degraded resources:
       gcloud workstations delete {ws} --cluster={cluster} --config={config} --region={region} --project={project} --quiet
       gcloud workstations configs delete {config} --cluster={cluster} --region={region} --project={project} --quiet
  3. Recreate them by running ./deploy.sh for this project only (projects.txt with just its row,
     plus the role=admin row if the event has one; add --force-after-handoff after hand-off).
  4. Restoring /home from the snapshot (gce_pd.source_snapshot) is untested: rehearse it first.
"""


def heal_degraded_workstation_config(project_id: str, region: str, workdir: str, env: dict,
                                     apply_cmd: list[str], log_f, ws_preexisted: bool,
                                     record: dict, max_attempts: int = 2) -> None:
    """Recreates a degraded workstation config, but only if this run created the workstation.

    A config cannot be fixed in place; recreating it also recreates the
    workstation (replace_triggered_by on the config uid), which deletes /home.
    That is harmless on a first deploy and is refused otherwise.

    Args:
        project_id: Participant project.
        region: Workstation region.
        workdir: Per-project Terraform workdir.
        env: Terraform environment (fresh token).
        apply_cmd: The project's terraform apply argv.
        log_f: Per-project apply log handle.
        ws_preexisted: Whether the workstation was in state before this apply.
        record: The project's build record; updated with heal fields.
        max_attempts: Recreate attempts.

    Raises:
        RuntimeError: When the config is not verifiably healthy afterwards, or
            the workstation pre-existed (the runbook is written to the log).
    """
    if ws_preexisted:
        log_f.write(WS_RUNBOOK.format(config=WS_CONFIG_ID, project=project_id, ws=WS_ID,
                                      cluster=WS_CLUSTER_ID, region=region))
        log_f.flush()
        record["workstation_health"] = "DEGRADED_NOT_RECREATED"
        raise RuntimeError(
            f"Error: Cloud Workstation config '{WS_CONFIG_ID}' is degraded (CONFIG_DEGRADED) in project "
            f"{project_id}; not recreated automatically because the workstation existed before this run "
            f"(runbook in the apply log).")
    replace_cmd = [*apply_cmd, f"-replace={WS_CONFIG_ADDRESS}"]
    # The backend pins the token from the first init (-backend-config=access_token),
    # which a long apply can outlive; re-init with the fresh token in `env`.
    init_cmd = terraform_init_cmd(project_id, env.get("GOOGLE_OAUTH_ACCESS_TOKEN", ""))
    for attempt in range(1, max_attempts + 1):
        record["heal_attempts"] = attempt
        msg = (f"⚠️ Recreating degraded workstation config (attempt {attempt}/{max_attempts}); "
               f"this run created the workstation, so no participant data exists yet.\n")
        log_f.write(msg)
        log_f.flush()
        logging.warning(f"[{project_id}] {msg.strip()}")
        try:
            rc = subprocess.run(init_cmd, cwd=workdir, env=env, stdout=log_f, stderr=log_f,
                                text=True, timeout=TERRAFORM_INIT_TIMEOUT).returncode
            if rc == 0:
                rc = subprocess.run(replace_cmd, cwd=workdir, env=env, stdout=log_f, stderr=log_f,
                                    text=True, timeout=TERRAFORM_TIMEOUT).returncode
        except subprocess.TimeoutExpired:
            rc = None
        if rc != 0:
            log_f.write(f"⚠️ Recreate attempt {attempt} failed ({'timeout' if rc is None else f'exit {rc}'}).\n")
            log_f.flush()
            continue
        health = inspect_workstation_config(project_id, region, env)
        record["workstation_detail"] = health["detail"]
        if health["state"] == "HEALTHY":
            record["workstation_health"] = "DEGRADED_RECREATED"
            log_f.write(f"✅ Workstation config healthy after recreate attempt {attempt}.\n")
            log_f.flush()
            return
        if health["state"] == "UNKNOWN":
            record["workstation_health"] = "UNKNOWN"
            raise RuntimeError(f"Error: workstation_health_unknown: '{WS_CONFIG_ID}' in project {project_id} "
                               f"could not be verified after recreate: {health['detail']}")
    record["workstation_health"] = "DEGRADED_UNRECOVERED"
    raise RuntimeError(
        f"Error: Cloud Workstation config '{WS_CONFIG_ID}' is still degraded (CONFIG_DEGRADED) in project "
        f"{project_id} after {record.get('heal_attempts', 0)} recreate attempt(s): "
        f"{record.get('workstation_detail', 'no detail')}")


def verify_workstation_after_apply(project_id: str, region: str, workdir: str, env: dict,
                                   apply_cmd: list[str], log_f, ws_preexisted: bool, record: dict) -> None:
    """Step 2b: fails the project unless the workstation config is verifiably healthy.

    Args:
        project_id: Participant project.
        region: Workstation region.
        workdir: Per-project Terraform workdir.
        env: Terraform environment (fresh token).
        apply_cmd: The project's terraform apply argv.
        log_f: Per-project apply log handle.
        ws_preexisted: Whether the workstation was in state before this apply.
        record: The project's build record; receives workstation_health/detail.

    Raises:
        RuntimeError: If the config is degraded and not recreated, or its health is unknown.
    """
    log_f.write("\n=== STEP 2b: CLOUD WORKSTATION CONFIG HEALTH CHECK ===\n")
    log_f.flush()
    health = inspect_workstation_config(project_id, region, env)
    if os.environ.get("FORGE_FAULT_INJECT_WS_DEGRADED") == "1" and project_id not in _FAULT_INJECTED:
        # Test only: exercises the recreate path once per project and run. Only here,
        # so --check-workstations (which inherits the variable) stays truthful.
        _FAULT_INJECTED.add(project_id)
        msg = "code=13 message=fault injected by FORGE_FAULT_INJECT_WS_DEGRADED=1 (test only)"
        health = {"state": "DEGRADED", "detail": msg, "conditions": [msg]}
    record["workstation_health"] = health["state"]
    record["workstation_detail"] = health["detail"]
    log_f.write(f"Workstation config '{WS_CONFIG_ID}': {health['state']} ({health['detail']})\n")
    log_f.flush()
    if health["state"] == "HEALTHY":
        return
    if health["state"] == "UNKNOWN":
        raise RuntimeError(f"Error: workstation_health_unknown: could not verify '{WS_CONFIG_ID}' in project "
                           f"{project_id}: {health['detail']}")
    heal_degraded_workstation_config(project_id, region, workdir, env, apply_cmd, log_f, ws_preexisted, record)


def cleanup_workstation_vms(project_id: str, env: dict | None, log_f) -> int:
    """Destroy path only: deletes leftover `workstations-*` VMs so network teardown is not blocked.

    Args:
        project_id: Project being destroyed.
        env: Environment for gcloud.
        log_f: Per-project destroy log handle.

    Returns:
        Number of VMs deleted.
    """
    data, err = _run_gcloud_json(["gcloud", "compute", "instances", "list", f"--project={project_id}",
                                  "--filter=name~'^workstations-'", "--format=json"], env)
    if not isinstance(data, list):
        if err:
            log_f.write(f"⚠️ Could not list workstation VMs before destroy: {err[:200]}\n")
        return 0
    deleted = 0
    for inst in data:
        name = inst.get("name", "")
        zone = (inst.get("zone") or "").rsplit("/", 1)[-1]
        if not name or not zone:
            continue
        log_f.write(f"🧹 Deleting leftover Cloud Workstation VM '{name}' ({zone}) before destroy...\n")
        log_f.flush()
        try:
            res = subprocess.run(["gcloud", "compute", "instances", "delete", name, f"--zone={zone}",
                                  f"--project={project_id}", "--quiet"],
                                 env=env, capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            log_f.write(f"⚠️ Deleting {name} timed out; destroy continues.\n")
            continue
        if res.returncode == 0:
            deleted += 1
        else:
            log_f.write(f"⚠️ Could not delete {name}: {(res.stderr or '').strip()[:200]}\n")
    return deleted


def handoff_marker_uri(project_id: str) -> str:
    """Returns the GCS URI of a project's hand-off marker."""
    return f"gs://{project_id}-tfstate/{HANDOFF_MARKER}"


def handoff_status(project_id: str) -> bool | None:
    """Returns True if the project was handed off, False if not, None if unknown."""
    try:
        res = subprocess.run(["gcloud", "storage", "ls", handoff_marker_uri(project_id), f"--project={project_id}"],
                             capture_output=True, text=True, timeout=GCLOUD_TIMEOUT)
    except subprocess.TimeoutExpired:
        return None
    if res.returncode == 0:
        return True
    text = f"{res.stderr}\n{res.stdout}".lower()
    if any(s in text for s in ("matched no objects", "not found", "404", "does not exist")):
        return False
    return None


def write_handoff_marker(project_id: str) -> tuple[bool, str]:
    """Writes the hand-off marker into the project's state bucket.

    Returns:
        (success, error text).
    """
    payload = json.dumps({
        "handed_off_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "meaning": "deploy.py refuses to apply while this object exists; destroy is unaffected.",
    })
    try:
        res = subprocess.run(["gcloud", "storage", "cp", "-", handoff_marker_uri(project_id), f"--project={project_id}"],
                             input=payload, capture_output=True, text=True, timeout=GCLOUD_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, "timed out"
    return res.returncode == 0, (res.stderr or "").strip()[:200]


def handoff_guard(projects: list[dict], max_workers: int) -> tuple[list[str], list[str]]:
    """Returns (handed_off_ids, unknown_ids) for the given projects."""
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as executor:
        results = list(executor.map(lambda p: (p["project_id"], handoff_status(p["project_id"])), projects))
    return ([pid for pid, s in results if s is True], [pid for pid, s in results if s is None])


def _start_probe(ws_args: list[str], env: dict, ws_state, timeout: int) -> str:
    """Starts a stopped workstation, waits for RUNNING, then stops it again."""
    try:
        res = subprocess.run(["gcloud", "workstations", "start", *ws_args], env=env,
                             capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "START_TIMEOUT"
    if res.returncode != 0:
        return f"START_FAILED ({(res.stderr or '').strip().splitlines()[-1:] or ['no detail']})"[:200]
    deadline = time.time() + timeout
    state = ws_state()
    while state != "STATE_RUNNING" and time.time() < deadline:
        time.sleep(15)
        state = ws_state()
    if state != "STATE_RUNNING":
        return f"START_FAILED (state {state})"
    try:
        stop = subprocess.run(["gcloud", "workstations", "stop", *ws_args], env=env,
                              capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return "STARTED (stop timed out; workstation left running)"
    return "STARTED_AND_STOPPED" if stop.returncode == 0 else "STARTED (stop failed; workstation left running)"


def check_workstation(project: dict, start: bool = False, start_timeout: int = 900) -> dict:
    """Read-only health check of one participant workstation, optionally with a start probe.

    The start probe is the only check that exercises VM provisioning, the step
    that failed in #151. It starts a stopped workstation, waits for RUNNING, and
    stops it again; an already running workstation is left alone.

    Args:
        project: Parsed projects.txt row.
        start: Whether to run the start probe.
        start_timeout: Seconds to wait for RUNNING.

    Returns:
        {"project_id", "config", "detail", "workstation", "start_probe", "ok"}
    """
    pid, region = project["project_id"], project["region"]
    env = prepare_terraform_env(get_active_oauth_token())
    result = {"project_id": pid, "config": "UNKNOWN", "detail": "", "workstation": "UNKNOWN",
              "start_probe": "SKIPPED", "ok": False}
    health = inspect_workstation_config(pid, region, env, attempts=3, delay=5)
    result["config"], result["detail"] = health["state"], health["detail"]
    ws_args = [WS_ID, f"--cluster={WS_CLUSTER_ID}", f"--config={WS_CONFIG_ID}", f"--region={region}", f"--project={pid}"]

    def ws_state() -> str:
        ws, err = _run_gcloud_json(["gcloud", "workstations", "describe", *ws_args, "--format=json"], env)
        return ws.get("state", "UNKNOWN") if isinstance(ws, dict) else f"UNKNOWN ({err[:80]})"

    result["workstation"] = ws_state()
    if start and health["state"] == "HEALTHY":
        if result["workstation"] == "STATE_RUNNING":
            result["start_probe"] = "ALREADY_RUNNING"
        else:
            result["start_probe"] = _start_probe(ws_args, env, ws_state, start_timeout)
    probe_ok = result["start_probe"] == "ALREADY_RUNNING" or result["start_probe"].startswith("STARTED")
    result["ok"] = health["state"] == "HEALTHY" and (not start or probe_ok)
    return result


def run_workstation_checks(projects: list[dict], start: bool, max_workers: int) -> int:
    """Runs check_workstation for every project, prints a table and writes a JSON summary.

    Returns:
        Number of projects that did not pass.
    """
    print(f"\n🩺 Checking {len(projects)} participant workstation(s)" + (" with a start probe" if start else "") + "...")
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as executor:
        results = list(executor.map(lambda p: check_workstation(p, start=start), projects))
    print(f"{'PROJECT ID':<28} | {'CONFIG':<9} | {'WORKSTATION':<16} | {'START PROBE':<22} | RESULT")
    print("-" * 100)
    for r in results:
        verdict = f"{COLOR_GREEN}OK{COLOR_RESET}" if r["ok"] else f"{COLOR_RED}FAIL{COLOR_RESET}"
        print(f"{r['project_id']:<28} | {r['config']:<9} | {r['workstation'][:16]:<16} | {r['start_probe'][:22]:<22} | {verdict}")
        if not r["ok"] and r["detail"]:
            print(f"{'':<28}   ↳ {r['detail'][:160]}")
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(os.path.join(LOG_DIR, "last_workstation_check.json"), "w") as f:
        json.dump({"start_probe": start, "projects": results}, f, indent=2)
    failed = sum(1 for r in results if not r["ok"])
    print(f"\n{'✔' if not failed else '❌'} {len(results) - failed}/{len(results)} workstation(s) passed. "
          f"Summary: {os.path.join(LOG_DIR, 'last_workstation_check.json')}")
    return failed


def summarize_plan(plan_json: dict) -> dict:
    """Condenses `terraform show -json` output to the changes a facilitator must see.

    Returns:
        {"changes": [{"address", "actions"}], "change_count": int, "workstation_replaced": bool}
    """
    changes = []
    for rc in plan_json.get("resource_changes") or []:
        actions = (rc.get("change") or {}).get("actions") or []
        if actions in (["no-op"], ["read"]):
            continue
        changes.append({"address": rc.get("address"), "actions": actions})
    replaced = any(c["address"] in (WS_ADDRESS, WS_CONFIG_ADDRESS) and "delete" in c["actions"] for c in changes)
    return {"changes": changes, "change_count": len(changes), "workstation_replaced": replaced}


def run_local_terraform_plan(project: dict, admin_project_id: str = "") -> dict:
    """Read-only `terraform plan` for one participant project.

    Shows what an apply would change, in particular whether it would replace
    the workstation (which deletes /home), without changing anything. Runs with
    -lock=false so it neither waits for nor leaves a state lock.

    Args:
        project: Parsed projects.txt row.
        admin_project_id: Admin project injected into participant roots, if any.

    Returns:
        {"project_id", "ok", "error", "change_count", "workstation_replaced", "changes"}
    """
    project_id = project["project_id"]
    workdir = os.path.join(WORKDIR_BASE, f"{project_id}.plan")
    log_file_path = os.path.join(LOG_DIR, f"{project_id}_plan.log")
    result = {"project_id": project_id, "ok": False, "error": "", "change_count": 0,
              "workstation_replaced": False, "changes": []}
    try:
        stage_workdir(workdir)
        token = get_active_oauth_token()
        env = prepare_terraform_env(token)
        with open(log_file_path, "w") as log_f:
            init = subprocess.run(terraform_init_cmd(project_id, token), cwd=workdir, env=env,
                                  stdout=log_f, stderr=log_f, text=True, timeout=TERRAFORM_INIT_TIMEOUT)
            if init.returncode != 0:
                raise RuntimeError(f"terraform init failed (see {log_file_path})")
            plan = subprocess.run(
                ["terraform", "plan", "-input=false", "-lock=false", "-out=tfplan",
                 "-var", f"project_id={project_id}", "-var", f"iap_member={project['user']}",
                 "-var", f"region={project['region']}", *admin_var_args(admin_project_id), *deployer_var_args()],
                cwd=workdir, env=env, stdout=log_f, stderr=log_f, text=True, timeout=TERRAFORM_TIMEOUT)
            if plan.returncode != 0:
                raise RuntimeError(f"terraform plan failed (see {log_file_path})")
        show = subprocess.run(["terraform", "show", "-json", "tfplan"], cwd=workdir, env=env,
                              capture_output=True, text=True, timeout=600)
        if show.returncode != 0:
            raise RuntimeError(f"terraform show failed: {(show.stderr or '').strip()[:200]}")
        result.update(summarize_plan(json.loads(show.stdout)))
        result["ok"] = True
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as e:
        result["error"] = str(e)[:300]
        logging.error(f"[{project_id}] plan failed: {e}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return result


def run_plans(projects: list[dict], admin_project_id: str, max_workers: int) -> int:
    """Plans every participant project, prints a table and writes last_plan_summary.json.

    Args:
        projects: Participant rows.
        admin_project_id: Admin project injected into participant roots, if any.
        max_workers: Parallel plans.

    Returns:
        Number of projects whose plan failed or would replace the workstation.
    """
    print(f"\n🔎 Planning {len(projects)} participant project(s) (read-only, nothing is changed)...")
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as executor:
        results = list(executor.map(lambda p: run_local_terraform_plan(p, admin_project_id), projects))
    print(f"{'PROJECT ID':<28} | {'CHANGES':>7} | WORKSTATION")
    print("-" * 80)
    for r in results:
        if not r["ok"]:
            print(f"{r['project_id']:<28} | {'-':>7} | {COLOR_RED}PLAN FAILED: {r['error'][:60]}{COLOR_RESET}")
        elif r["workstation_replaced"]:
            print(f"{r['project_id']:<28} | {r['change_count']:>7} | {COLOR_RED}REPLACED: apply would delete /home{COLOR_RESET}")
        else:
            print(f"{r['project_id']:<28} | {r['change_count']:>7} | kept")
    os.makedirs(LOG_DIR, exist_ok=True)
    summary_path = os.path.join(LOG_DIR, "last_plan_summary.json")
    with open(summary_path, "w") as f:
        json.dump({"projects": results}, f, indent=2)
    flagged = sum(1 for r in results if not r["ok"] or r["workstation_replaced"])
    print(f"\n{'✔' if not flagged else '❌'} {len(results) - flagged}/{len(results)} plan(s) OK. "
          f"Changes per project: {summary_path}; full plans: {LOG_DIR}/[PROJECT_ID]_plan.log")
    return flagged


def run_handoff(projects: list[dict], max_workers: int) -> int:
    """Writes the hand-off marker into every participant project's state bucket.

    Args:
        projects: Participant rows.
        max_workers: Parallel writes.

    Returns:
        Number of projects whose marker could not be written.
    """
    check_path = os.path.join(LOG_DIR, "last_workstation_check.json")
    try:
        with open(check_path) as f:
            last_check = json.load(f)
        probe_ok = last_check.get("start_probe") and all(p.get("ok") for p in last_check.get("projects", []))
    except (OSError, ValueError):
        probe_ok = False
    if not probe_ok:
        print(f"{COLOR_YELLOW}⚠️ No passing start probe on record ({check_path}). "
              f"Recommended first: ./deploy.sh --check-workstations --start{COLOR_RESET}")
    print(f"\n🤝 Marking {len(projects)} participant project(s) as handed off...")
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as executor:
        results = list(executor.map(lambda p: (p["project_id"], *write_handoff_marker(p["project_id"])), projects))
    failed = 0
    for pid, ok, err in results:
        if ok:
            print(f"  {COLOR_GREEN}✔{COLOR_RESET} {pid}: {handoff_marker_uri(pid)}")
        else:
            failed += 1
            print(f"  {COLOR_RED}❌{COLOR_RESET} {pid}: could not write the marker: {err}")
    if not failed:
        print("\n./deploy.sh now refuses to apply to these projects (override: --force-after-handoff).")
        print("Still available: ./deploy.sh --check-workstations, ./deploy.sh --plan, ./deploy.sh --destroy")
    return failed


def enforce_handoff_guard(projects: list[dict], max_workers: int, force: bool, action: str) -> None:
    """Exits the run if any project was handed off (or its status is unknown), unless forced.

    Args:
        projects: Participant rows.
        max_workers: Parallel marker lookups.
        force: --force-after-handoff was given.
        action: What would run, for the message (e.g. "apply").
    """
    handed_off, unknown = handoff_guard(projects, max_workers)
    if not handed_off and not unknown:
        return
    if force:
        print(f"{COLOR_YELLOW}⚠️ --force-after-handoff: running {action} on handed-off project(s) "
              f"{', '.join(handed_off + unknown)}.{COLOR_RESET}")
        logging.warning(f"--force-after-handoff {action}: {handed_off + unknown}")
        return
    print(f"\n{COLOR_RED}{COLOR_BOLD}❌ HAND-OFF GUARD: {action} refused.{COLOR_RESET}")
    if handed_off:
        print(f"  Handed off to participants: {', '.join(handed_off)}")
    if unknown:
        print(f"  Hand-off status could not be read (gs://<project>-tfstate/{HANDOFF_MARKER}): {', '.join(unknown)}")
    print("  An apply can replace workstations (deleting /home) and re-upload lab assets.")
    print("  See what would change: ./deploy.sh --plan    Override: --force-after-handoff")
    sys.exit(1)


def run_post_apply_patches(project_id, region, log_file):
    """Enables the AlloyDB Data API if the cluster and instance exist using the direct GCP REST API."""
    import urllib.request
    import urllib.error
    
    logging.info(f"[{project_id}] Running post-apply patches...")
    with open(log_file, "a") as f:
        f.write("\n=== STEP 3: RUNNING POST-DEPLOYMENT PATCHES ===\n")
        
        check_cmd = ["gcloud", "alloydb", "clusters", "describe", "search-cluster", f"--region={region}", f"--project={project_id}"]
        res = subprocess.run(check_cmd, capture_output=True, text=True)
        if res.returncode == 0:
            f.write("AlloyDB cluster 'search-cluster' found. Enabling AlloyDB Data API via GCP REST API...\n")
            try:
                token = get_active_oauth_token()
                url = f"https://alloydb.googleapis.com/v1alpha/projects/{project_id}/locations/{region}/clusters/search-cluster/instances/search-primary?updateMask=dataApiAccess"
                
                headers = {
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "X-Goog-User-Project": project_id
                }
                data = json.dumps({"dataApiAccess": "ENABLED"}).encode("utf-8")
                
                req = urllib.request.Request(url, data=data, headers=headers, method="PATCH")
                with urllib.request.urlopen(req) as response:
                    resp_data = response.read().decode("utf-8")
                    f.write(f"AlloyDB Data API Patch response:\n{resp_data}\n")
                    f.write("AlloyDB Data API enabled successfully.\n")
                    logging.info(f"[{project_id}] AlloyDB Data API enabled successfully.")
            except urllib.error.HTTPError as e:
                err_resp = e.read().decode("utf-8")
                f.write(f"⚠️ AlloyDB Data API Patch HTTP Error ({e.code}):\n{err_resp}\n")
                f.write("⚠️ Notice: Could not automatically enable AlloyDB Data API due to domain/auth policies on alpha endpoint. This is non-fatal; participants will activate it via user_guide.md during the lab.\n")
                logging.warning(f"[{project_id}] AlloyDB Data API patch skipped (HTTP {e.code}: domain restriction). Non-fatal.")
            except Exception as e:
                f.write(f"⚠️ AlloyDB Data API Patch Exception:\n{e}\n")
                f.write("⚠️ Notice: Could not automatically enable AlloyDB Data API. This is non-fatal; participants will activate it via user_guide.md during the lab.\n")
                logging.warning(f"[{project_id}] AlloyDB Data API patch skipped ({e}). Non-fatal.")
        else:
            f.write("No AlloyDB cluster found in deployment. Skipping Data API patch.\n")

def lock_down_admin_project(admin_project: dict) -> None:
    """Revokes owner/editor from the admin row's member, unless it is the deployer.

    The admin project is never handed to attendees. Lab-granted, resource-level
    access (e.g. Cloud Run invoker on a leaderboard) is untouched.

    Args:
        admin_project: The parsed `role=admin` row.
    """
    admin_id = admin_project["project_id"]
    user_member = admin_project.get("user")
    active_account = get_active_account()
    if not user_member or user_member == f"user:{active_account}":
        return
    for role in ["roles/owner", "roles/editor"]:
        cmd = [
            "gcloud", "projects", "remove-iam-policy-binding", admin_id,
            f"--member={user_member}", f"--role={role}", "--quiet"
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0:
            logging.info(f"[{admin_id}] Revoked {role} from {user_member}")
            print(f"  {COLOR_GREEN}✔{COLOR_RESET} [{admin_id}] Revoked {role} from {user_member}")


def stage_admin_workdir(workdir: str) -> None:
    """Recreates the admin Terraform workdir from 00-admin-infra and the lock file.

    Args:
        workdir: Destination directory; replaced if it already exists.

    Raises:
        RuntimeError: If 00-admin-infra is missing.
    """
    if not os.path.isdir(ADMIN_DIR):
        raise RuntimeError(f"{ADMIN_DIR}/ not found in this repository.")
    if os.path.exists(workdir):
        shutil.rmtree(workdir, ignore_errors=True)
    shutil.copytree(ADMIN_DIR, workdir)
    if os.path.exists(".terraform.lock.hcl"):
        shutil.copy2(".terraform.lock.hcl", os.path.join(workdir, ".terraform.lock.hcl"))


def admin_terraform_init(admin_id: str, oauth_token: str, workdir: str, env: dict,
                         log_f) -> subprocess.CompletedProcess:
    """Runs `terraform init` against the admin GCS backend with up to 3 attempts.

    Args:
        admin_id: Admin project ID (state lives in gs://<admin_id>-tfstate).
        oauth_token: Access token for the GCS backend.
        workdir: Staged admin Terraform directory.
        env: Environment from prepare_terraform_env().
        log_f: Open log file receiving Terraform output.

    Returns:
        The last attempt's CompletedProcess.
    """
    init_cmd = [
        "terraform", "init", "-reconfigure", "-input=false",
        f"-backend-config=bucket={admin_id}-tfstate",
        f"-backend-config=prefix={ADMIN_STATE_PREFIX}",
        f"-backend-config=access_token={oauth_token}"
    ]
    init_res = None
    for attempt in range(3):
        init_res = subprocess.run(init_cmd, cwd=workdir, env=env, stdout=log_f, stderr=log_f, text=True)
        if init_res.returncode == 0:
            break
        if attempt < 2:
            sleep_time = (2 ** attempt) * 4 + random.uniform(1, 3)
            log_f.write(f"\n⚠️ Admin Terraform Init failed (attempt {attempt+1}/3). Retrying in {sleep_time:.1f}s...\n")
            log_f.flush()
            time.sleep(sleep_time)
    return init_res


def run_admin_hooks(admin_project: dict, hook_key: str) -> None:
    """Runs the `admin_post_deploy` or `admin_pre_destroy` hooks from the manifest.

    Output is teed live: these hooks build containers and can run for minutes,
    and a silent terminal is indistinguishable from a hang.

    Args:
        admin_project: The parsed `role=admin` row.
        hook_key: "admin_post_deploy" or "admin_pre_destroy".

    Raises:
        RuntimeError: If a hook script is missing, times out, or exits non-zero.
    """
    hooks = load_event_manifest().get(hook_key, [])
    admin_id = admin_project["project_id"]
    repo_root = os.getcwd()
    for hook in hooks:
        script_abs = os.path.join(repo_root, hook["script"])
        name = hook.get("name", hook["script"])
        timeout = int(hook.get("timeout", 1800))
        if not os.path.isfile(script_abs):
            raise RuntimeError(f"{hook_key} hook '{name}' points at {hook['script']}, which does not exist.")

        print(f"\n  {COLOR_CYAN}▶{COLOR_RESET} {hook_key}: {COLOR_BOLD}{name}{COLOR_RESET}")
        env = os.environ.copy()
        env.update({
            "ADMIN_PROJECT_ID": admin_id,
            "GOOGLE_CLOUD_PROJECT": admin_id,
            "CLOUDSDK_CORE_PROJECT": admin_id,
            "REGION": admin_project.get("region", "europe-west3"),
            "EVENT_NAME": load_event_manifest().get("event_name", ""),
            "REPO_ROOT": repo_root,
            "PROJECTS_FILE": os.path.join(repo_root, PROJECTS_FILE),
        })
        log_file_path = os.path.join(LOG_DIR, f"{admin_id}_{hook_key}_{hook.get('lab_id', 'lab')}.log")
        with open(log_file_path, "w") as log_f:
            proc = subprocess.Popen(
                ["bash", os.path.basename(script_abs)],
                cwd=os.path.dirname(script_abs), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                start_new_session=True
            )

            # Streaming on a thread keeps the timeout enforceable: iterating
            # stdout in this thread would block until EOF, so a hook that hangs
            # silently would never time out.
            def _pump() -> None:
                for line in proc.stdout:
                    log_f.write(line)
                    print(f"    {COLOR_GRAY}{line.rstrip()}{COLOR_RESET}")

            pump = threading.Thread(target=_pump, daemon=True)
            pump.start()
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired as e:
                # Kill the whole session: children (gcloud, docker) would
                # otherwise keep the pipe open and outlive the hook.
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
                raise RuntimeError(f"{hook_key} hook '{name}' exceeded its {timeout}s timeout. Log: {log_file_path}") from e
            finally:
                pump.join(timeout=5)
                proc.stdout.close()
        if proc.returncode != 0:
            raise RuntimeError(f"{hook_key} hook '{name}' failed with exit code {proc.returncode}. Log: {log_file_path}")
        print(f"  {COLOR_GREEN}✔{COLOR_RESET} {hook_key} complete: {name}")


def deploy_admin_infra(admin_project: dict) -> None:
    """Phase 0: applies 00-admin-infra to the admin project, then post-deploy hooks.

    Runs before any participant project so that admin-side resources exist when
    participant modules reference them.

    Args:
        admin_project: The parsed `role=admin` row.

    Raises:
        RuntimeError: On prerequisite, init, apply or hook failure.
    """
    admin_id = admin_project["project_id"]
    region = admin_project.get("region", "europe-west3")
    workdir = os.path.join(WORKDIR_BASE, f"admin_{admin_id}")
    log_file_path = os.path.join(LOG_DIR, f"{admin_id}_admin_apply.log")
    labs = ", ".join(load_event_manifest().get("admin_labs", [])) or "selected labs"
    print(f"\n{COLOR_CYAN}{COLOR_BOLD}🏛 Phase 0: Provisioning Admin Project {admin_id} ({labs})...{COLOR_RESET}")

    prereq_ok, err = prepare_single_project(admin_project)
    if not prereq_ok:
        _, badge, _, _ = classify_error(err)
        raise RuntimeError(f"Admin project prerequisites failed [{badge}]: {err}")
    clear_stale_tf_lock(admin_id, ADMIN_STATE_PREFIX)
    lock_down_admin_project(admin_project)
    stage_admin_workdir(workdir)
    oauth_token = get_active_oauth_token()
    env = prepare_terraform_env(oauth_token)

    with open(log_file_path, "w") as log_f:
        log_f.write("=== PHASE 0: ADMIN PROJECT TERRAFORM INIT ===\n")
        if admin_terraform_init(admin_id, oauth_token, workdir, env, log_f).returncode != 0:
            raise RuntimeError(f"Admin Terraform init failed. Log: {log_file_path}")
        log_f.write("\n=== PHASE 0: ADMIN PROJECT TERRAFORM APPLY ===\n")
        log_f.flush()
        apply_cmd = ["terraform", "apply", "-var", f"project_id={admin_id}", "-var", f"region={region}", "-auto-approve"]
        apply_res = None
        for attempt in range(3):
            apply_res = subprocess.run(apply_cmd, cwd=workdir, env=env, stdout=log_f, stderr=log_f, text=True)
            if apply_res.returncode == 0 or attempt == 2:
                break
            with open(log_file_path, "r") as check_f:
                if not is_retryable_error(check_f.read()[-2000:]):
                    break
            sleep_time = min(60, (2 ** attempt) * 10 + random.uniform(2, 5))
            log_f.write(f"\n⚠️ Admin Terraform apply failed (attempt {attempt+1}/3). Retrying in {sleep_time:.1f}s...\n")
            log_f.flush()
            time.sleep(sleep_time)
        if apply_res.returncode != 0:
            with open(log_file_path, "r") as check_f:
                _, badge, _, _ = classify_error(check_f.read()[-2000:])
            raise RuntimeError(f"Admin Terraform apply failed [{badge}]. Log: {log_file_path}")

    run_admin_hooks(admin_project, "admin_post_deploy")
    print(f"  {COLOR_GREEN}✔{COLOR_RESET} Phase 0 complete: admin project {admin_id} is live.")


def destroy_admin_infra(admin_project: dict) -> bool:
    """Runs pre-destroy hooks, destroys 00-admin-infra, then removes its state.

    State and workdir are deleted only after a successful destroy; on any
    failure they are kept so a rerun of `./deploy.sh --destroy` can finish.

    Args:
        admin_project: The parsed `role=admin` row.

    Returns:
        True on a complete teardown, False otherwise.
    """
    admin_id = admin_project["project_id"]
    region = admin_project.get("region", "europe-west3")
    workdir = os.path.join(WORKDIR_BASE, f"admin_{admin_id}")
    log_file_path = os.path.join(LOG_DIR, f"{admin_id}_admin_destroy.log")
    print(f"\n{COLOR_RED}{COLOR_BOLD}🔥 Destroying Admin Project infrastructure ({admin_id})...{COLOR_RESET}")

    try:
        run_admin_hooks(admin_project, "admin_pre_destroy")
    except RuntimeError as e:
        print(f"  {COLOR_RED}✘ {e}{COLOR_RESET}")
        print(f"  {COLOR_RED}Admin Terraform destroy NOT attempted; state kept in gs://{admin_id}-tfstate.{COLOR_RESET}")
        logging.error(f"[{admin_id}] admin_pre_destroy failed: {e}")
        return False

    clear_stale_tf_lock(admin_id, ADMIN_STATE_PREFIX)
    oauth_token = get_active_oauth_token()
    env = prepare_terraform_env(oauth_token)
    stage_admin_workdir(workdir)
    with open(log_file_path, "w") as log_f:
        log_f.write("=== ADMIN PROJECT TERRAFORM INIT (DESTROY) ===\n")
        rc = admin_terraform_init(admin_id, oauth_token, workdir, env, log_f).returncode
        if rc == 0:
            log_f.write("\n=== ADMIN PROJECT TERRAFORM DESTROY ===\n")
            log_f.flush()
            destroy_cmd = ["terraform", "destroy", "-var", f"project_id={admin_id}", "-var", f"region={region}", "-auto-approve"]
            rc = subprocess.run(destroy_cmd, cwd=workdir, env=env, stdout=log_f, stderr=log_f, text=True).returncode

    if rc != 0:
        print(f"  {COLOR_RED}✘ Admin project {admin_id} teardown FAILED. State kept in gs://{admin_id}-tfstate; "
              f"fix the error and rerun ./deploy.sh --destroy.{COLOR_RESET}")
        print(f"    Log: {log_file_path}")
        logging.error(f"[{admin_id}] Admin destroy failed (rc={rc}); state bucket and workdir preserved.")
        return False

    subprocess.run(
        ["gcloud", "storage", "rm", "--recursive", f"gs://{admin_id}-tfstate", f"--project={admin_id}", "--quiet"],
        capture_output=True, text=True
    )
    shutil.rmtree(workdir, ignore_errors=True)
    print(f"  {COLOR_GREEN}✔{COLOR_RESET} Admin project {admin_id} infrastructure destroyed.")
    return True


def admin_var_args(admin_project_id: str) -> list[str]:
    """Returns the `-var` arguments that inject the admin project into participant roots."""
    return ["-var", f"admin_project_id={admin_project_id}"] if admin_project_id else []


WORKDIR_ITEMS = ("00-core-infra", "labs", "main.tf", "variables.tf", "terraform.tfvars",
                 "bootstrapping.sh", "event-manifest.json", ".terraform.lock.hcl")


def build_assets_zip(zip_path: str) -> None:
    """Writes the participant assets bundle (manifest, bootstrap script, labs/) to zip_path."""
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for item in ["event-manifest.json", "bootstrapping.sh"]:
            if os.path.exists(item):
                zipf.write(item, item)
        if os.path.exists("labs"):
            for root, dirs, files in os.walk("labs"):
                for file in files:
                    file_path = os.path.join(root, file)
                    zipf.write(file_path, file_path)


def stage_workdir(workdir: str) -> None:
    """Recreates an isolated per-project Terraform workdir from the event repo.

    Args:
        workdir: Directory to (re)create; any previous content is removed.
    """
    if os.path.exists(workdir):
        shutil.rmtree(workdir, ignore_errors=True)
    os.makedirs(workdir, exist_ok=True)
    for item in WORKDIR_ITEMS:
        if os.path.exists(item):
            if os.path.isdir(item):
                shutil.copytree(item, os.path.join(workdir, item))
            else:
                shutil.copy(item, os.path.join(workdir, item))
    shared_assets_zip = os.path.join(WORKDIR_BASE, "assets.zip")
    assets_zip_path = os.path.join(workdir, "assets.zip")
    if os.path.exists(shared_assets_zip):
        shutil.copy(shared_assets_zip, assets_zip_path)
    else:
        build_assets_zip(assets_zip_path)


def run_local_terraform_apply(project, builds, index, admin_project_id=""):
    """Executes a local Terraform init & apply in an isolated workspace for a single project."""
    project_id = project["project_id"]
    iap_member = project["user"]
    region = project["region"]
    workdir = os.path.join(WORKDIR_BASE, project_id)
    log_file_path = os.path.join(LOG_DIR, f"{project_id}_apply.log")
    
    try:
        stage_workdir(workdir)
                    
        oauth_token = get_active_oauth_token()
        env = prepare_terraform_env(oauth_token)
        
        # Step 1: Terraform Initialization
        builds[index]["status"] = "INIT"
        with open(log_file_path, "w") as log_f:
            log_f.write("=== STEP 1: TERRAFORM INITIALIZATION ===\n")
            log_f.flush()
            
            init_cmd = terraform_init_cmd(project_id, oauth_token)
            init_res = None
            for attempt in range(3):
                init_res = subprocess.run(
                    init_cmd,
                    cwd=workdir, env=env, stdout=log_f, stderr=log_f, text=True
                )
                if init_res.returncode == 0:
                    break
                if attempt < 2:
                    sleep_time = (2 ** attempt) * 4 + random.uniform(1, 3)
                    log_f.write(f"\n⚠️ Terraform Init failed (attempt {attempt+1}/3). Retrying in {sleep_time:.1f}s...\n")
                    log_f.flush()
                    time.sleep(sleep_time)
                    
            if init_res.returncode != 0:
                with open(log_file_path, "r") as check_f:
                    log_text = check_f.read()
                cat, badge, desc, fix = classify_error(log_text)
                builds[index]["failure_category"] = cat
                builds[index]["failure_badge"] = badge
                builds[index]["failure_detail"] = first_error_line(log_text)
                builds[index]["failure_advice"] = fix
                raise Exception(f"Terraform Init failed [{badge}]")
                
            # Check for and push any errored.tfstate from a prior state-upload interruption
            errored_state = os.path.join(workdir, "errored.tfstate")
            if os.path.exists(errored_state):
                log_f.write("\n⚠️ Detected 'errored.tfstate' from prior interrupted state upload. Pushing state...\n")
                log_f.flush()
                push_res = subprocess.run(
                    ["terraform", "state", "push", "errored.tfstate"],
                    cwd=workdir, env=env, stdout=log_f, stderr=log_f, text=True
                )
                if push_res.returncode == 0:
                    log_f.write("✔ State recovery successful! Removed errored.tfstate.\n")
                    os.remove(errored_state)
                else:
                    log_f.write("⚠️ State push failed. Proceeding with apply.\n")
                log_f.flush()

            # B5: read BEFORE apply. A workstation that already existed may hold a
            # participant's /home, so Step 2b never recreates it automatically.
            ws_preexisted = workstation_in_state(workdir, env)
            builds[index]["workstation_preexisted"] = ws_preexisted

            # Step 2: Terraform Apply
            builds[index]["status"] = "APPLY"
            log_f.write("\n=== STEP 2: TERRAFORM DEPLOYMENT ===\n")
            log_f.flush()
            
            apply_cmd = [
                "terraform", "apply",
                "-var", f"project_id={project_id}",
                "-var", f"iap_member={iap_member}",
                "-var", f"region={region}",
                *admin_var_args(admin_project_id),
                *deployer_var_args(),
                "-auto-approve"
            ]
            
            apply_res = None
            for attempt in range(3):
                apply_res = subprocess.run(
                    apply_cmd,
                    cwd=workdir, env=env, stdout=log_f, stderr=log_f, text=True
                )
                if apply_res.returncode == 0:
                    break
                if attempt < 2:
                    with open(log_file_path, "r") as check_f:
                        log_tail = check_f.read()[-2000:]
                    if not is_retryable_error(log_tail):
                        log_f.write(f"\n❌ Non-retryable error detected. Failing fast.\n")
                        break
                    sleep_time = min(60, (2 ** attempt) * 10 + random.uniform(2, 5))
                    log_f.write(f"\n⚠️ Terraform Apply failed (attempt {attempt+1}/3). Retrying in {sleep_time:.1f}s...\n")
                    log_f.flush()
                    time.sleep(sleep_time)
                    
            if apply_res.returncode != 0:
                with open(log_file_path, "r") as check_f:
                    log_text = check_f.read()
                cat, badge, desc, fix = classify_error(log_text)
                builds[index]["failure_category"] = cat
                builds[index]["failure_badge"] = badge
                builds[index]["failure_detail"] = first_error_line(log_text)
                builds[index]["failure_advice"] = fix
                raise Exception(f"Terraform Apply failed [{badge}]")

            # Step 2b: Terraform reports success even when the workstation config
            # comes back degraded (#151). Fresh token: the apply may have outlived
            # the one it started with.
            builds[index]["status"] = "WS_CHECK"
            ws_env = prepare_terraform_env(get_active_oauth_token())
            verify_workstation_after_apply(project_id, region, workdir, ws_env, apply_cmd,
                                           log_f, ws_preexisted, builds[index])
                
        # Step 3: Run Post-Apply Patches (AlloyDB Data API)
        builds[index]["status"] = "POST_PATCH"
        run_post_apply_patches(project_id, region, log_file_path)
        
        builds[index]["status"] = "SUCCESS"
        shutil.rmtree(workdir, ignore_errors=True)
    except Exception as e:
        logging.error(f"[{project_id}] Local provisioning failed: {e}")
        cat, badge, desc, fix = classify_error(str(e))
        builds[index]["status"] = f"FAILED: {badge}"
        builds[index]["error"] = str(e)
        # main() pre-fills these keys with None, so test the value, not the key.
        if not builds[index].get("failure_category"):
            builds[index]["failure_category"] = cat
            builds[index]["failure_badge"] = badge
            builds[index]["failure_detail"] = first_error_line(str(e))
            builds[index]["failure_advice"] = fix
        with open(log_file_path, "a") as log_f:
            log_f.write(f"\n❌ EXCEPTION: {e}\n")

def run_local_terraform_destroy(project, builds, index, admin_project_id=""):
    """Executes a local Terraform init & destroy in an isolated workspace for a single project."""
    project_id = project["project_id"]
    iap_member = project["user"]
    region = project["region"]
    workdir = os.path.join(WORKDIR_BASE, project_id)
    log_file_path = os.path.join(LOG_DIR, f"{project_id}_destroy.log")
    
    try:
        os.makedirs(workdir, exist_ok=True)
        
        for item in ["00-core-infra", "labs", "main.tf", "variables.tf", "terraform.tfvars", "bootstrapping.sh", "event-manifest.json", ".terraform.lock.hcl"]:
            if os.path.exists(item):
                if os.path.isdir(item):
                    shutil.copytree(item, os.path.join(workdir, item), dirs_exist_ok=True)
                else:
                    shutil.copy(item, os.path.join(workdir, item))

                    
        oauth_token = get_active_oauth_token()
        env = prepare_terraform_env(oauth_token)
        
        # Step 1: Terraform Initialization for Destroy
        builds[index]["status"] = "INIT"
        with open(log_file_path, "w") as log_f:
            log_f.write("=== STEP 1: TERRAFORM INITIALIZATION (FOR DESTROY) ===\n")
            log_f.flush()
            
            init_cmd = [
                "terraform", "init", "-reconfigure",
                f"-backend-config=bucket={project_id}-tfstate",
                "-backend-config=prefix=terraform/state",
                f"-backend-config=access_token={oauth_token}"
            ]
            init_res = None
            for attempt in range(3):
                init_res = subprocess.run(
                    init_cmd,
                    cwd=workdir, env=env, stdout=log_f, stderr=log_f, text=True
                )
                if init_res.returncode == 0:
                    break
                if attempt < 2:
                    sleep_time = (2 ** attempt) * 4 + random.uniform(1, 3)
                    log_f.write(f"\n⚠️ Terraform Init failed (attempt {attempt+1}/3). Retrying in {sleep_time:.1f}s...\n")
                    log_f.flush()
                    time.sleep(sleep_time)
                    
            if init_res.returncode != 0:
                with open(log_file_path, "r") as check_f:
                    log_text = check_f.read()
                cat, badge, desc, fix = classify_error(log_text)
                builds[index]["failure_category"] = cat
                builds[index]["failure_badge"] = badge
                builds[index]["failure_detail"] = first_error_line(log_text)
                builds[index]["failure_advice"] = fix
                raise Exception(f"Terraform Init failed [{badge}]")
                
            # Step 2: Terraform Destroy
            builds[index]["status"] = "DESTROY"
            log_f.write("\n=== STEP 2: TERRAFORM DESTROY ===\n")
            log_f.flush()
            
            destroy_cmd = [
                "terraform", "destroy",
                "-var", f"project_id={project_id}",
                "-var", f"iap_member={iap_member}",
                "-var", f"region={region}",
                *admin_var_args(admin_project_id),
                *deployer_var_args(),
                "-auto-approve"
            ]
            
            # Pre-heal: AlloyDB users cannot be dropped via SQL DROP ROLE if database objects depend on them;
            # removing them from state allows the AlloyDB cluster deletion to destroy them cleanly without dependency errors.
            list_res = subprocess.run(["terraform", "state", "list"], cwd=workdir, env=env, capture_output=True, text=True)
            if list_res.returncode == 0:
                for s_line in list_res.stdout.splitlines():
                    if "google_alloydb_user." in s_line:
                        subprocess.run(["terraform", "state", "rm", s_line.strip()], cwd=workdir, env=env, capture_output=True)
            
            destroy_res = None
            for attempt in range(3):
                destroy_res = subprocess.run(
                    destroy_cmd,
                    cwd=workdir, env=env, stdout=log_f, stderr=log_f, text=True
                )
                if destroy_res.returncode == 0:
                    break
                if attempt < 2:
                    # Auto-heal: AlloyDB users cannot be dropped via SQL DROP ROLE if objects depend on them;
                    # removing them from state allows the AlloyDB cluster deletion to destroy them cleanly.
                    list_res = subprocess.run(["terraform", "state", "list"], cwd=workdir, env=env, capture_output=True, text=True)
                    if list_res.returncode == 0:
                        for s_line in list_res.stdout.splitlines():
                            if "google_alloydb_user." in s_line:
                                subprocess.run(["terraform", "state", "rm", s_line.strip()], cwd=workdir, env=env, capture_output=True)
                    # A leftover Cloud Workstation VM can keep the subnet in use. Delete
                    # `workstations-*` VMs only when the failed attempt names one.
                    with open(log_file_path, "r") as check_f:
                        if WS_VM_IN_USE.search(check_f.read()[-6000:]):
                            removed = cleanup_workstation_vms(project_id, env, log_f)
                            log_f.write(f"🧹 Deleted {removed} leftover workstation VM(s) blocking the destroy.\n")
                    sleep_time = min(60, (2 ** attempt) * 10 + random.uniform(2, 5))
                    log_f.write(f"\n⚠️ Terraform Destroy failed (attempt {attempt+1}/3). Retrying in {sleep_time:.1f}s (auto-healing state)...\n")
                    log_f.flush()
                    time.sleep(sleep_time)
                    
            if destroy_res.returncode != 0:
                with open(log_file_path, "r") as check_f:
                    log_text = check_f.read()
                cat, badge, desc, fix = classify_error(log_text)
                builds[index]["failure_category"] = cat
                builds[index]["failure_badge"] = badge
                builds[index]["failure_detail"] = first_error_line(log_text)
                builds[index]["failure_advice"] = fix
                raise Exception(f"Terraform Destroy failed [{badge}]")
                
        # Step 3: State Bucket Cleanup
        builds[index]["status"] = "CLEANUP"
        state_bucket = f"gs://{project_id}-tfstate"
        del_cmd = ["gcloud", "storage", "rm", "--recursive", state_bucket, f"--project={project_id}", "--quiet"]
        subprocess.run(del_cmd, capture_output=True, text=True)
        
        builds[index]["status"] = "DESTROYED"
        shutil.rmtree(workdir, ignore_errors=True)
    except Exception as e:
        logging.error(f"[{project_id}] Local teardown failed: {e}")
        cat, badge, desc, fix = classify_error(str(e))
        builds[index]["status"] = f"FAILED: {badge}"
        builds[index]["error"] = str(e)
        # main() pre-fills these keys with None, so test the value, not the key.
        if not builds[index].get("failure_category"):
            builds[index]["failure_category"] = cat
            builds[index]["failure_badge"] = badge
            builds[index]["failure_detail"] = first_error_line(str(e))
            builds[index]["failure_advice"] = fix
        with open(log_file_path, "a") as log_f:
            log_f.write(f"\n❌ EXCEPTION: {e}\n")

def write_run_summary(builds, is_destroy=False):
    """Writes deploy_logs/last_run_summary.json: one record per project.

    Consumers (tests/agentic_e2e.py) read this instead of scraping the
    coloured console dashboard.

    Args:
        builds: The per-project build records.
        is_destroy: True for a teardown run.
    """
    summary = {
        "mode": "destroy" if is_destroy else "apply",
        "projects": [
            {
                "project_id": b.get("project_id"),
                "status": b.get("status"),
                "failure_category": b.get("failure_category"),
                "failure_badge": b.get("failure_badge"),
                "failure_detail": b.get("failure_detail"),
                "failure_advice": b.get("failure_advice"),
                "workstation_health": b.get("workstation_health"),
                "workstation_detail": b.get("workstation_detail"),
                "workstation_preexisted": b.get("workstation_preexisted"),
                "heal_attempts": b.get("heal_attempts", 0),
            }
            for b in builds
        ],
    }
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, "last_run_summary.json"), "w") as f:
            json.dump(summary, f, indent=2)
    except OSError as e:
        logging.error(f"Could not write run summary: {e}")


def print_remediation_matrix(builds, is_destroy=False):
    """Renders an actionable post-mortem remediation matrix for any failed projects."""
    failed_builds = [b for b in builds if b["status"].startswith("FAILED") or b["status"].startswith("PREREQ_FAILED")]
    if not failed_builds:
        return
        
    print(f"\n{COLOR_RED}{COLOR_BOLD}===================================================================================================={COLOR_RESET}")
    print(f"{COLOR_RED}{COLOR_BOLD}❌ DEPLOYMENT FAILURES DETECTED - REMEDIATION MATRIX:{COLOR_RESET}")
    print(f"{COLOR_RED}{COLOR_BOLD}===================================================================================================={COLOR_RESET}")
    print(f"{COLOR_BOLD}{'PROJECT ID':<24} | {'FAILURE BADGE':<18} | {'ACTIONABLE RESOLUTION ADVICE':<52}{COLOR_RESET}")
    print(f"----------------------------------------------------------------------------------------------------")
    
    for b in failed_builds:
        pid = b["project_id"]
        # Keys may exist with a None value (pre-filled by main()), so `or`, not a .get default.
        badge = b.get("failure_badge") or "Failed"
        advice = b.get("failure_advice") or "Inspect individual log file for error details."
        print(f"{COLOR_RED}{pid:<24}{COLOR_RESET} | {COLOR_YELLOW}{badge:<18}{COLOR_RESET} | {advice:<52}")
        if b.get("failure_detail"):
            print(f"{'':<24}   ↳ first error: {b['failure_detail']}")
        
    print(f"{COLOR_RED}{COLOR_BOLD}===================================================================================================={COLOR_RESET}")
    log_type = "destroy" if is_destroy else "apply"
    print(f"📂 Detailed per-project logs are available in: {COLOR_BOLD}{LOG_DIR}/[PROJECT_ID]_{log_type}.log{COLOR_RESET}\n")

def run_live_dashboard(builds, is_destroy=False):
    """Renders a real-time console cockpit dashboard in the main thread."""
    total = len(builds)
    max_display_rows = 25
    
    while True:
        # Calculate statistics
        queued = sum(1 for b in builds if b["status"] == "QUEUED")
        init = sum(1 for b in builds if b["status"] == "INIT")
        apply_or_destroy = sum(1 for b in builds if b["status"] in ("APPLY", "DESTROY", "WS_CHECK"))
        patch_or_cleanup = sum(1 for b in builds if b["status"] in ("POST_PATCH", "CLEANUP"))
        success = sum(1 for b in builds if b["status"] in ("SUCCESS", "DESTROYED"))
        failed = sum(1 for b in builds if b["status"].startswith("FAILED") or b["status"].startswith("PREREQ_FAILED"))
        
        # Render Dashboard
        os.system("clear" if os.name != "nt" else "cls")
        print(DESTROY_BANNER if is_destroy else BANNER)
        print(f"📝 Main Log File: {COLOR_BOLD}{LOG_FILE}{COLOR_RESET}")
        log_type = "destroy" if is_destroy else "apply"
        print(f"📂 Individual Run Logs: {COLOR_BOLD}{LOG_DIR}/[PROJECT_ID]_{log_type}.log{COLOR_RESET}\n")
        
        print(f"{COLOR_BOLD}Parallel Local Thread Summary:{COLOR_RESET}")
        print(f"  Total Projects : {total}")
        if is_destroy:
            print(f"  {COLOR_GRAY}Queued: {queued}{COLOR_RESET} | {COLOR_CYAN}Init: {init}{COLOR_RESET} | {COLOR_YELLOW}Destroy: {apply_or_destroy}{COLOR_RESET} | {COLOR_YELLOW}Cleanup: {patch_or_cleanup}{COLOR_RESET} | {COLOR_GREEN}Destroyed: {success}{COLOR_RESET} | {COLOR_RED}Failed: {failed}{COLOR_RESET}")
        else:
            print(f"  {COLOR_GRAY}Queued: {queued}{COLOR_RESET} | {COLOR_CYAN}Init: {init}{COLOR_RESET} | {COLOR_YELLOW}Apply: {apply_or_destroy}{COLOR_RESET} | {COLOR_YELLOW}Patch: {patch_or_cleanup}{COLOR_RESET} | {COLOR_GREEN}Success: {success}{COLOR_RESET} | {COLOR_RED}Failed: {failed}{COLOR_RESET}")
        print("=================================================================")
        print(f"{COLOR_BOLD}{'PROJECT ID':<25} | {'PARTICIPANT':<25} | {'CURRENT STEP / STATUS':<25}{COLOR_RESET}")
        print("-----------------------------------------------------------------")
        
        if total <= max_display_rows:
            display_builds = builds
        else:
            active_statuses = ("INIT", "APPLY", "WS_CHECK", "POST_PATCH", "DESTROY", "CLEANUP")
            active_builds = [b for b in builds if b["status"] in active_statuses]
            failed_builds = [b for b in builds if b["status"].startswith("FAILED") or b["status"].startswith("PREREQ_FAILED")]
            other_builds = [b for b in builds if b["status"] in ("SUCCESS", "DESTROYED", "QUEUED")]
            
            display_builds = []
            display_builds.extend(active_builds[:15])
            display_builds.extend(failed_builds[:10])
            
            remaining_slots = max_display_rows - len(display_builds)
            if remaining_slots > 0:
                display_builds.extend(other_builds[:remaining_slots])
                
        for b in display_builds:
            proj = b["project_id"]
            user = b["user"].replace("user:", "")
            status = b["status"]
            
            if status == "SUCCESS" and b.get("workstation_health") == "DEGRADED_RECREATED":
                status_str = f"{COLOR_GREEN}✔ SUCCESS{COLOR_RESET} {COLOR_YELLOW}(WS recreated){COLOR_RESET}"
            elif status in ("SUCCESS", "DESTROYED"):
                status_str = f"{COLOR_GREEN}✔ {status}{COLOR_RESET}"
            elif status == "INIT":
                status_str = f"{COLOR_CYAN}⏳ TERRAFORM INIT{COLOR_RESET}"
            elif status == "APPLY":
                status_str = f"{COLOR_YELLOW}⏳ TERRAFORM APPLY{COLOR_RESET}"
            elif status == "WS_CHECK":
                status_str = f"{COLOR_YELLOW}⏳ WORKSTATION CHECK{COLOR_RESET}"
            elif status == "DESTROY":
                status_str = f"{COLOR_YELLOW}⏳ TERRAFORM DESTROY{COLOR_RESET}"
            elif status == "POST_PATCH":
                status_str = f"{COLOR_YELLOW}⏳ POST-CONFIG PATCH{COLOR_RESET}"
            elif status == "CLEANUP":
                status_str = f"{COLOR_YELLOW}⏳ CLEANING STATE{COLOR_RESET}"
            elif status == "QUEUED":
                status_str = f"{COLOR_GRAY}💤 QUEUED{COLOR_RESET}"
            elif "failure_badge" in b and b["failure_badge"]:
                status_str = f"{COLOR_RED}❌ {b['failure_badge']}{COLOR_RESET}"
            elif status.startswith("PREREQ_FAILED"):
                badge = b.get("failure_badge") or "Prereq Failed"
                status_str = f"{COLOR_RED}❌ {badge}{COLOR_RESET}"
            else:
                badge = b.get("failure_badge") or "Failed"
                status_str = f"{COLOR_RED}❌ {badge}{COLOR_RESET}"
                
            print(f"{proj:<25} | {user:<25} | {status_str:<25}")
            
        if total > max_display_rows:
            print("-----------------------------------------------------------------")
            print(f"{COLOR_GRAY}  [Displaying {len(display_builds)} of {total} projects - Check {STATE_FILE} for full status list]{COLOR_RESET}")
        print("=================================================================")
        
        # Persist status to disk
        state_payload = {
            "event_timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "builds": builds
        }
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(state_payload, f, indent=2)
        except Exception:
            pass
            
        # Check if all threads have completed
        all_done = all(b["status"] in ("SUCCESS", "DESTROYED") or b["status"].startswith("FAILED") or b["status"].startswith("PREREQ_FAILED") for b in builds)
        if all_done:
            break
            
        time.sleep(2)
        
    write_run_summary(builds, is_destroy)
    if is_destroy:
        print("\n🎉 Local Parallel Teardown Complete!")
        print(f"  -> Successfully Destroyed : {COLOR_GREEN}{success}{COLOR_RESET}")
        print(f"  -> Failed Teardowns       : {COLOR_RED}{failed}{COLOR_RESET}")
        if failed > 0:
            print_remediation_matrix(builds, is_destroy=True)
        else:
            print(f"\n{COLOR_GREEN}✔ All project infrastructure successfully destroyed! Sandbox environments are clean.{COLOR_RESET}")
    else:
        print("\n🎉 Local Parallel Provisioning Complete!")
        print(f"  -> Successfully Deployed : {COLOR_GREEN}{success}{COLOR_RESET}")
        print(f"  -> Failed Deployments     : {COLOR_RED}{failed}{COLOR_RESET}")
        recreated = [b["project_id"] for b in builds if b.get("workstation_health") == "DEGRADED_RECREATED"]
        if recreated:
            print(f"  {COLOR_YELLOW}⚠️ Workstation config came back degraded and was recreated in: "
                  f"{', '.join(recreated)} (details in each apply log){COLOR_RESET}")
        if failed > 0:
            print_remediation_matrix(builds, is_destroy=False)
        else:
            print(f"\n{COLOR_GREEN}✔ All projects successfully provisioned.{COLOR_RESET}")
            print(f"   Before hand-off: ./deploy.sh --check-workstations --start, then ./deploy.sh --handoff")
    return failed

def main():
    parser = argparse.ArgumentParser(description="Data Forge Parallel Provisioning & Teardown Engine")
    parser.add_argument("action", nargs="?", default="apply", choices=["apply", "destroy"], help="Action to perform: apply or destroy (default: apply)")
    parser.add_argument("--max-workers", "-w", type=int, default=int(os.environ.get("MAX_WORKERS", 20)), help="Maximum concurrent threads (default: 20)")
    parser.add_argument("--yes", "-y", action="store_true", help="Automatic yes to prompts for headless execution")
    parser.add_argument("--destroy", "-d", action="store_true", help="Destroy infrastructure across all projects")
    parser.add_argument("--plan", action="store_true",
                        help="Read-only: terraform plan per participant project. Exits 1 if a plan fails or "
                             "would replace a workstation (which deletes the participant's /home).")
    parser.add_argument("--check-workstations", action="store_true",
                        help="Read-only: report each participant workstation config's health (no Terraform).")
    parser.add_argument("--start", action="store_true",
                        help="With --check-workstations: start each stopped workstation, wait for RUNNING, "
                             "then stop it again. Run once before hand-off.")
    parser.add_argument("--handoff", action="store_true",
                        help="Mark every participant project as handed off; later applies are refused.")
    parser.add_argument("--force-after-handoff", action="store_true",
                        help="Apply (or start-probe) even though projects were handed off. An apply can "
                             "replace workstations and delete participants' /home.")
    args = parser.parse_args()
    
    if args.action == "destroy":
        args.destroy = True
    modes = [flag for flag, on in (("--destroy", args.destroy), ("--plan", args.plan),
                                   ("--check-workstations", args.check_workstations),
                                   ("--handoff", args.handoff)) if on]
    if len(modes) > 1:
        parser.error(f"choose only one of: {', '.join(modes)}")
    if args.start and not args.check_workstations:
        parser.error("--start requires --check-workstations")
    
    setup_logging()
    print(DESTROY_BANNER if args.destroy else BANNER)
    print(f"📝 Logging execution orchestration to: {COLOR_BOLD}{LOG_FILE}{COLOR_RESET}")
    print(f"⚡ Max Concurrency: {COLOR_BOLD}{args.max_workers} worker threads{COLOR_RESET}")
    
    # 1. Pre-flight Environment & Authentication Guard
    check_environment_and_auth()
    
    # 2. Parse, Lint, and Probe Projects
    projects, invalid_projects = parse_and_validate_projects(auto_yes=args.yes)
    print(f"Found {len(projects)} active & verified project mapping(s) in {PROJECTS_FILE}.")

    # 2.5 Admin Project topology: exactly one role=admin row iff the event needs it
    admin_required = requires_admin_project()
    topology_errors = validate_admin_topology(projects, invalid_projects, admin_required)
    if topology_errors:
        print(f"\n{COLOR_RED}{COLOR_BOLD}❌ PRE-FLIGHT ERROR: Admin Project configuration{COLOR_RESET}")
        for err in topology_errors:
            print(f"  • {err}")
        print(f"\n👉 In {PROJECTS_FILE}, mark exactly one project with `role=admin`:")
        print(f"   {COLOR_GREEN}<admin-project-id>, <facilitator-email>, <region>, role=admin{COLOR_RESET}")
        sys.exit(1)
    admin_rows, participant_projects = split_projects_by_role(projects)
    admin_project = admin_rows[0] if admin_required else None
    active_admin_id = admin_project["project_id"] if admin_project else ""
    if admin_rows and not admin_required:
        print(f"{COLOR_YELLOW}⚠️ Ignoring role=admin row(s) {', '.join(p['project_id'] for p in admin_rows)}: "
              f"no selected lab uses an admin project.{COLOR_RESET}")
    if admin_project:
        print(f"{COLOR_YELLOW}🏛 Admin Project: {COLOR_BOLD}{active_admin_id}{COLOR_RESET}{COLOR_YELLOW} "
              f"(NEVER distribute its credentials to participants){COLOR_RESET}")

    # Read-only check and hand-off marking: no Terraform below this point for them.
    if args.check_workstations:
        if args.start:
            # The probe stops what it started; after hand-off that could cut off a participant.
            enforce_handoff_guard(participant_projects, args.max_workers, args.force_after_handoff,
                                  "--check-workstations --start")
        sys.exit(1 if run_workstation_checks(participant_projects, args.start, min(args.max_workers, 10)) else 0)
    if args.handoff:
        sys.exit(1 if run_handoff(participant_projects, args.max_workers) else 0)
    if not args.destroy and not args.plan:
        # Before Phase 0: a refused apply must not touch the admin project either.
        enforce_handoff_guard(participant_projects, args.max_workers, args.force_after_handoff, "apply")
    # Resolve once here; the per-project worker threads below only read the cached value.
    extra_vars = deployer_var_args()
    if extra_vars:
        print(f"🔐 Certificate-based access: Terraform presents the client certificate; {extra_vars[1]}")

    if not args.destroy:
        total, used, free = shutil.disk_usage(".")
        free_gb = free / (1024 ** 3)
        estimated_needed_gb = 0.5 + (len(participant_projects) * 0.015)
        
        if free_gb < estimated_needed_gb:
            print(f"\n{COLOR_RED}❌ ERROR: Insufficient disk space.{COLOR_RESET}")
            print(f"  Available: {free_gb:.2f} GB")
            print(f"  Estimated Required: {estimated_needed_gb:.2f} GB")
            sys.exit(1)
        elif free_gb < 5.0:
            print(f"\n{COLOR_YELLOW}⚠️ WARNING: Low disk space ({free_gb:.2f} GB free). Continuing anyway...{COLOR_RESET}")
            
    os.makedirs(PLUGIN_CACHE_DIR, exist_ok=True)
    os.makedirs(WORKDIR_BASE, exist_ok=True)
    
    action_str = "DESTROY" if args.destroy else "PROVISION"
    if not args.yes and not args.plan:
        scope_str = f"{len(participant_projects)} participant project(s)"
        if admin_project:
            scope_str += f" + Admin Project {active_admin_id}"
        confirm = input(f"\nReady to launch local parallel Terraform {action_str} loops across {scope_str} (Max Workers: {args.max_workers})? (y/n): ").strip().lower()
        if confirm not in ("y", "yes"):
            print("Aborted.")
            sys.exit(0)
            
    print(f"\n📦 Step 1: Pre-warming Terraform provider cache sequentially...")
    logging.info("Starting sequential pre-warm of Terraform provider plugin cache...")
    
    env = os.environ.copy()
    env["TF_PLUGIN_CACHE_DIR"] = os.path.abspath(PLUGIN_CACHE_DIR)
    
    init_cmd = ["terraform", "init", "-backend=false", "-upgrade=false"]
    res = subprocess.run(init_cmd, env=env, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"{COLOR_RED}❌ ERROR: Failed to pre-warm Terraform provider cache.{COLOR_RESET}")
        print(res.stderr)
        logging.error(f"Sequential pre-warm failed: {res.stderr}")
        sys.exit(1)
        
    print(f"  {COLOR_GREEN}✔{COLOR_RESET} Provider plugins cached and .terraform.lock.hcl verified.")

    # Phase 0: the admin project must exist before any participant references it.
    if admin_project and not args.destroy and not args.plan:
        try:
            deploy_admin_infra(admin_project)
        except RuntimeError as e:
            print(f"\n{COLOR_RED}❌ Phase 0 failed: {e}{COLOR_RESET}")
            print("   No participant project was touched.")
            logging.error(f"Phase 0 Admin Project provisioning failed: {e}")
            sys.exit(1)
    
    if not args.destroy:
        shared_assets_zip = os.path.join(WORKDIR_BASE, "assets.zip")
        build_assets_zip(shared_assets_zip)
        print(f"  {COLOR_GREEN}✔{COLOR_RESET} Shared assets bundle generated: {shared_assets_zip}")

    if args.plan:
        # Read-only: skips Step 2, which enables APIs and grants IAM.
        sys.exit(1 if run_plans(participant_projects, active_admin_id, args.max_workers) else 0)
        
    print(f"\n⚙ Step 2: Verifying & preparing remote GCS state buckets in parallel...")
    
    prereq_results = []
    with ThreadPoolExecutor(max_workers=args.max_workers) as executor:
        if args.destroy:
            futures = {executor.submit(clear_stale_tf_lock, p["project_id"]): p for p in participant_projects}
        else:
            futures = {executor.submit(prepare_single_project, p): p for p in participant_projects}
        for future in futures:
            project = futures[future]
            success, err = future.result()
            if success:
                print(f"  {COLOR_GREEN}✔{COLOR_RESET} {project['project_id']}: State bucket & permissions verified.")
                prereq_results.append((project, True, None, None, None, None))
            else:
                cat, badge, desc, fix = classify_error(err)
                print(f"  {COLOR_RED}❌{COLOR_RESET} {project['project_id']}: Verification failed [{badge}]: {err}")
                prereq_results.append((project, False, err, cat, badge, fix))
                
    print(f"\n🚀 Step 3: Launching parallel local {action_str.lower()}ing threads...")
    
    builds = []
    for project, success, err, cat, badge, fix in prereq_results:
        builds.append({
            "project_id": project["project_id"],
            "user": project["user"],
            "region": project["region"],
            "role": project.get("role", "participant"),
            "status": "QUEUED" if success else f"PREREQ_FAILED: {badge}",
            "error": err,
            "failure_category": cat,
            "failure_badge": badge,
            "failure_advice": fix
        })
        
    with ThreadPoolExecutor(max_workers=args.max_workers) as executor:
        for idx, item in enumerate(prereq_results):
            project, success, err, cat, badge, fix = item
            if success:
                if args.destroy:
                    executor.submit(run_local_terraform_destroy, project, builds, idx, active_admin_id)
                else:
                    executor.submit(run_local_terraform_apply, project, builds, idx, active_admin_id)
                
        print(f"⏳ Entering Live Cockpit Dashboard (Parallel {action_str.lower()} threads running)...")
        failed = run_live_dashboard(builds, is_destroy=args.destroy)

    # Admin teardown runs last, and only once no participant can still reference it.
    if args.destroy and admin_project:
        blockers = admin_teardown_blockers(builds, invalid_projects)
        if blockers:
            print(f"\n{COLOR_YELLOW}⚠️ Admin Project {active_admin_id} NOT destroyed: participant teardown incomplete.{COLOR_RESET}")
            for blocker in blockers:
                print(f"  • {blocker}")
            print("   Fix the participants above and rerun ./deploy.sh --destroy.")
            sys.exit(1)
        if not destroy_admin_infra(admin_project):
            sys.exit(1)

    if failed > 0:
        sys.exit(1)

if __name__ == "__main__":
    main()
