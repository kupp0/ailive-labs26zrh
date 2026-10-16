#!/usr/bin/env bash
# ==============================================================================
# Headless Hackathon Engine: Shell Entrypoint Wrapper for deploy.py
# ==============================================================================
set -euo pipefail

# Ensure we run from the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

# Check for required CLI binaries
for tool in python3 terraform gcloud; do
  if ! command -v "${tool}" &>/dev/null; then
    echo "❌ ERROR: '${tool}' is required but not installed or not in PATH."
    echo "Please install ${tool} on your machine to run the orchestrator."
    exit 1
  fi
done

# Delegate execution to the Python orchestrator
exec python3 deploy.py "$@"

