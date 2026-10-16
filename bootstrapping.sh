#!/usr/bin/env bash
# ==============================================================================
# Headless Hackathon Engine: Dynamic Workstation Bootstrapping Coordinator
#
# This script runs on EVERY workstation start: the container entrypoint
# re-downloads assets.zip and calls it on each boot. The first boot hydrates
# everything. Later boots must never destroy participant state:
#   * lab files that already exist are left alone (participants' code edits survive)
#   * MCP servers are merged by id and existing entries always win (the manifest
#     entries are placeholders that lab init.sh scripts replace with working ones)
#   * the gcloud config keeps its account; only a missing project is filled in
#   * ownership is fixed only for what this boot wrote
# FORGE_REPO_DIR and FORGE_HOME override the default paths (used by unit tests).
# ==============================================================================
set -euo pipefail

TEMP_CLONE_DIR="${FORGE_REPO_DIR:-/tmp/event-repo}"
TARGET_HOME="${FORGE_HOME:-/home/user}"

echo "================================================================="
echo "⚙ Starting Dynamic Workstation Bootstrapping..."
echo "================================================================="

# Ensure the assets zip was extracted
if [[ ! -d "${TEMP_CLONE_DIR}" ]]; then
  echo "❌ ERROR: Assets directory ${TEMP_CLONE_DIR} not found. Did the zip extraction fail?"
  exit 1
fi

MANIFEST_FILE="${TEMP_CLONE_DIR}/event-manifest.json"
if [[ ! -f "${MANIFEST_FILE}" ]]; then
  echo "❌ ERROR: event-manifest.json not found in the cloned repository."
  exit 1
fi

# --- 2. Manifest-Driven Lab and Skills Hydration (Inline Python) ---
echo "📦 Hydrating selected labs and agent skills from manifest..."

python3 - "${MANIFEST_FILE}" "${TEMP_CLONE_DIR}" "${TARGET_HOME}" <<'PYEOF'
import configparser
import json
import os
import shutil
import subprocess
import sys
import time

manifest_path, repo_dir, target_home = sys.argv[1:4]

state_dir = os.path.join(target_home, ".forge")
state_file = os.path.join(state_dir, "bootstrap_state.json")
done_marker = os.path.join(target_home, ".bootstrapping_done")
# A home that was bootstrapped before (by this or an older script) is a restart.
first_boot = not os.path.exists(state_file) and not os.path.exists(done_marker)
written = []  # paths created or overwritten by THIS boot; only these get chowned


def copy_file(src, dst):
    """copytree copy_function: later boots never overwrite an existing file."""
    if not first_boot and os.path.exists(dst):
        return dst
    shutil.copy2(src, dst)
    written.append(dst)
    return dst


with open(manifest_path, 'r') as f:
    manifest = json.load(f)

mode = "first boot" if first_boot else "restart: existing participant files are preserved"
print(f"Processing event: {manifest.get('event_name', 'Unnamed Event')} ({mode})")

selected_labs = manifest.get('selected_labs', [])
if not selected_labs:
    print("⚠ No labs selected in the manifest!")
    sys.exit(0)

for lab in selected_labs:
    lab_id = lab.get('id')
    lab_name = lab.get('name')
    assets_src = lab.get('assets_src')
    dest_dir = lab.get('dest_dir')
    skills = lab.get('skills', [])

    print(f"\nInstalling {lab_name}...")

    # 1. Copy Lab Assets/Source
    if assets_src and dest_dir:
        src_path = os.path.join(repo_dir, assets_src)
        dest_path = os.path.join(target_home, dest_dir)

        # Check if a nested "src" directory exists inside the lab source
        lab_src_path = os.path.join(src_path, "src")
        actual_src_path = lab_src_path if os.path.exists(lab_src_path) else src_path

        if os.path.exists(actual_src_path):
            print(f"  -> Copying assets from {actual_src_path} to {dest_path}")
            shutil.copytree(actual_src_path, dest_path, ignore=shutil.ignore_patterns("infra"),
                            dirs_exist_ok=True, copy_function=copy_file)
        else:
            print(f"  ⚠ WARNING: Lab assets source path not found: {src_path}")

        # 2. Copy Agent Skills (if any)
        for skill in skills:
            skill_name = skill.get('name')
            skill_src_path = os.path.join(repo_dir, skill.get('src'))
            skill_dest_dir = os.path.join(dest_path, ".agents", "skills", skill_name)
            os.makedirs(skill_dest_dir, exist_ok=True)
            skill_dest_path = os.path.join(skill_dest_dir, "SKILL.md")

            if not os.path.exists(skill_src_path):
                print(f"  ⚠ WARNING: Agent skill source path not found: {skill_src_path}")
            elif first_boot or not os.path.exists(skill_dest_path):
                print(f"  -> Copying agent skill {skill_name} to {skill_dest_path}")
                copy_file(skill_src_path, skill_dest_path)
    else:
        print(f"  ⚠ WARNING: Missing asset source or destination directory for lab: {lab_id}")

# --- 3. Dynamic Model Context Protocol (MCP) & IDE Configuration ---
mcp_servers = manifest.get('mcp_servers', [])


def resolve_project_id():
    # 1. Check environment variable
    pid = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT_ID")
    if pid and pid != "CURRENT_PROJECT":
        return pid

    # 2. Check Application Default Credentials
    adc_path = os.path.join(target_home, ".config", "gcloud", "application_default_credentials.json")
    if os.path.exists(adc_path):
        try:
            with open(adc_path, "r") as f:
                quota_role_pid = json.load(f).get("quota_project_id")
            if quota_role_pid:
                return quota_role_pid
        except (OSError, ValueError) as e:
            print(f"  ⚠ Could not read ADC file for the project id: {e}")

    # 3. Check active gcloud config
    try:
        result = subprocess.run(["gcloud", "config", "get-value", "project"],
                                capture_output=True, text=True, check=True, timeout=60)
        gcloud_pid = result.stdout.strip()
        if gcloud_pid and gcloud_pid != "CURRENT_PROJECT":
            return gcloud_pid
    except (OSError, subprocess.SubprocessError) as e:
        print(f"  ⚠ gcloud project lookup failed: {e}")

    return "CURRENT_PROJECT"


def merge_mcp_config(path, servers):
    """Adds manifest servers whose id is missing; never overwrites an existing entry."""
    config = {}
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                config = json.load(f)
        except (OSError, ValueError) as e:
            print(f"  ⚠ {path} is unreadable ({e}); saved a .bak copy and rebuilding it")
            shutil.copy2(path, path + ".bak")
            config = {}
    if not isinstance(config, dict):
        config = {}
    current = config.get("mcpServers")
    if not isinstance(current, dict):
        current = {}
    added = [sid for sid in servers if sid not in current]
    for sid in added:
        current[sid] = servers[sid]
    config["mcpServers"] = current
    if added or not os.path.exists(path):
        with open(path, "w") as f:
            json.dump(config, f, indent=2)
        written.append(path)
    return added


project_id = resolve_project_id()
print(f"\n⚙ Dynamic IDE Configuration:")
print(f"  -> Resolved active GCP Project ID: {project_id}")

# Target paths
gemini_config_dir = os.path.join(target_home, ".gemini", "config")
agy_config_dir = os.path.join(target_home, ".gemini", "antigravity-cli")
plugins_dir = os.path.join(target_home, ".gemini", "antigravity-cli", "plugins")

for d in (gemini_config_dir, agy_config_dir, plugins_dir):
    os.makedirs(d, exist_ok=True)

# Build dynamic mcpServers dict
mcp_servers_dict = {}
for server in mcp_servers:
    server_id = server.get('id')
    server_type = server.get('type', 'server')
    server_config = server.get('config', {})
    print(f"  -> Wiring MCP Server: {server_id} ({server_type})")
    mcp_servers_dict[server_id] = server_config

    if server_type == "plugin":
        plugin_path = os.path.join(plugins_dir, f"{server_id}.json")
        if first_boot or not os.path.exists(plugin_path):
            with open(plugin_path, "w") as f:
                json.dump(server_config, f, indent=2)
            written.append(plugin_path)

for cfg_dir in (gemini_config_dir, agy_config_dir):
    added = merge_mcp_config(os.path.join(cfg_dir, "mcp_config.json"), mcp_servers_dict)
    if not first_boot and added:
        print(f"  -> Added missing MCP server(s) to {cfg_dir}: {', '.join(added)}")

# Write general config.json (only when absent; the participant may have changed it)
cli_config = {
    "project": project_id,
    "project_id": project_id,
    "location": "global",
    "theme": "dark",
    "terms_accepted": True
}
for cfg_dir in (gemini_config_dir, agy_config_dir):
    cfg_path = os.path.join(cfg_dir, "config.json")
    if not os.path.exists(cfg_path):
        with open(cfg_path, "w") as f:
            json.dump(cli_config, f, indent=2)
        written.append(cfg_path)

# gcloud default configuration: fill in a missing project, keep everything else
# (in particular [core] account, which a restart used to wipe).
gcloud_config_dir = os.path.join(target_home, ".config", "gcloud", "configurations")
os.makedirs(gcloud_config_dir, exist_ok=True)
gcloud_cfg_path = os.path.join(gcloud_config_dir, "config_default")
gcloud_cfg = configparser.ConfigParser(interpolation=None)
if os.path.exists(gcloud_cfg_path):
    gcloud_cfg.read(gcloud_cfg_path)
if not gcloud_cfg.has_section("core"):
    gcloud_cfg.add_section("core")
if gcloud_cfg.get("core", "project", fallback="") in ("", "CURRENT_PROJECT") \
        or not os.path.exists(gcloud_cfg_path):
    gcloud_cfg.set("core", "project", project_id)
    with open(gcloud_cfg_path, "w") as f:
        gcloud_cfg.write(f)
    written.append(gcloud_cfg_path)

# Boot bookkeeping: tells the next boot it is a restart.
os.makedirs(state_dir, exist_ok=True)
state = {}
if os.path.exists(state_file):
    try:
        with open(state_file, "r") as f:
            state = json.load(f)
    except (OSError, ValueError) as e:
        print(f"  ⚠ Bootstrap state unreadable ({e}); starting a new one")
now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
state["boots"] = int(state.get("boots", 0)) + 1
state.setdefault("first_boot_at", now)
state["last_boot_at"] = now
state["event_name"] = manifest.get("event_name", "")
with open(state_file, "w") as f:
    json.dump(state, f, indent=2)
written.append(state_file)

# Ownership: the whole home on the first boot, otherwise only what this boot wrote
# (plus the directories above it), so files added on a later boot are not left root-owned.
if first_boot:
    res = subprocess.run(["chown", "-R", "1000:1000", target_home], capture_output=True, text=True)
    if res.returncode != 0:
        print(f"  ⚠ chown -R {target_home} failed: {res.stderr.strip()[:200]}")
else:
    paths = set(written)
    home = os.path.abspath(target_home)
    for p in written:
        parent = os.path.dirname(os.path.abspath(p))
        while parent.startswith(home + os.sep):
            paths.add(parent)
            parent = os.path.dirname(parent)
    failed = 0
    for p in sorted(paths):
        try:
            os.chown(p, 1000, 1000)
        except OSError:
            failed += 1
    if failed:
        print(f"  ⚠ chown skipped for {failed} of {len(paths)} path(s) (not running as root?)")

print(f"✔ Dynamic IDE and MCP configurations written ({len(written)} path(s) written this boot).")
print("\n✔ Manifest processing completed successfully.")
PYEOF

# --- 3. Configure User .bashrc for Workspace Active Project & Authentication Reminders ---
BASHRC="${TARGET_HOME}/.bashrc"
if ! grep -q "GCP_WORKSPACE_SETUP" "${BASHRC}" 2>/dev/null; then
    echo "⚙ Configuring user .bashrc for GCP workspace automatic project routing..."
    cat << 'EOF' >> "${BASHRC}"

# >>> GCP_WORKSPACE_SETUP >>>
# Set the default project automatically if needed
if [ "$(gcloud config get-value project 2>/dev/null)" = "CURRENT_PROJECT" ] || [ -z "$(gcloud config get-value project 2>/dev/null)" ]; then
    DET_PROJECT_ID=$(jq -r '.project // empty' /home/user/.gemini/config/config.json 2>/dev/null)
    if [ -n "$DET_PROJECT_ID" ]; then
        gcloud config set project "$DET_PROJECT_ID" &>/dev/null
    fi
fi

# Remind user to authenticate if not already done
if [ -z "$(gcloud config get-value account 2>/dev/null)" ] || [ ! -f ~/.config/gcloud/application_default_credentials.json ]; then
    echo ""
    echo "🚀 Welcome to the Data Forge! 🚀"
    echo "Your custom data cloud experience, built by you."
    echo "Let's wire things up so you can build some database magic."
    echo "Run this command to unlock your Google Cloud authentication gateway:"
    echo "    gcloud auth login --update-adc"
    echo "Go ahead, you've got this! 💪"
    echo ""
fi
# <<< GCP_WORKSPACE_SETUP <<<
EOF
fi

# --- 4. Clean Up and Fix Permissions ---
echo "🧹 Cleaning up temporary git clone..."
rm -rf "${TEMP_CLONE_DIR}"

# Other image layouts use these homes; only consider them for the real home path.
EXTRA_HOMES=()
if [[ "${TARGET_HOME}" == "/home/user" ]]; then
    EXTRA_HOMES=(/home/student /home/jupyter)
fi

# Make all lab verification scripts executable
find "${TARGET_HOME}" ${EXTRA_HOMES[@]+"${EXTRA_HOMES[@]}"} -name "verify.sh" -exec chmod +x {} + 2>/dev/null || true

# Signal bootstrapping completion
touch "${TARGET_HOME}/.bootstrapping_done"
for extra in ${EXTRA_HOMES[@]+"${EXTRA_HOMES[@]}"}; do
    touch "${extra}/.bootstrapping_done" 2>/dev/null || true
done

echo "================================================================="
echo "🎉 Dynamic Bootstrapping Completed Successfully!"
echo "================================================================="
