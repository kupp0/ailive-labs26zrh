#!/usr/bin/env bash
# ==============================================================================
# Event-Day Cloud Workstations Warm-Up & Idle Timeout Manager
#
# Generic across all Data Forge deployments with Cloud Workstations (00-core-infra).
#
# Usage:
#   ./warmup.sh                # Default: Patch 8h idle timeout + start all VMs
#   ./warmup.sh --status       # Read-only status check of all participant workstations
#   ./warmup.sh --patch-only   # Patch idle_timeout=28800s (8h) without starting VMs
#   ./warmup.sh --stop         # Stop all participant workstations after the event
# ==============================================================================
set -euo pipefail

cd "$(dirname "$0")"

if [[ "${1:-}" == "--patch-only" ]]; then
    shift
    exec python3 warmup_workstations.py --status --patch-timeout "$@"
elif [[ " $* " == *" --status "* ]] || [[ " $* " == *" --stop "* ]] || [[ " $* " == *" --start "* ]] || [[ " $* " == *" -h "* ]] || [[ " $* " == *" --help "* ]]; then
    exec python3 warmup_workstations.py "$@"
else
    exec python3 warmup_workstations.py --patch-timeout --start "$@"
fi
