#!/usr/bin/env python3
"""Event-day Cloud Workstations Warm-Up & Sanity Check Tool.

Generic across all Data Forge deployments that include `00-core-infra`
(Cloud Workstations). Reads `projects.txt` using the same row grammar as
`deploy.py` (supporting 2-column `PROJECT_ID, USER` rows defaulting to
`europe-west3` as well as explicit regions and `role=admin` filtering).

Unlike `deploy.py --check-workstations --start` (which starts a VM and immediately
stops it again as a pre-handoff probe), this script:
  1. Optionally patches `idleTimeout` (default 28800s / 8h) in-place on live
     `workstation-config` resources across all participant projects.
  2. Starts all participant workstations in staggered batches (`--max-workers 6`
     with jitter) to prevent simultaneous GCE `e2-standard-4` zone allocation spikes,
     and leaves them in `STATE_RUNNING` for the event day.
  3. Supports `--status` (read-only audit of config health, timeouts, and VM state)
     and `--stop` (post-event shutdown).
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import subprocess
import sys
import time
import deploy as d

DEFAULT_IDLE_TIMEOUT = "28800s"  # 8 hours


def normalize_timeout(val: str) -> str:
  val = val.strip()
  return val if val.endswith("s") else f"{val}s"


def get_participant_projects() -> list[dict]:
  """Parses participant rows from projects.txt using deploy.py's exact grammar."""
  if not os.path.exists(d.PROJECTS_FILE):
    print(f"{d.COLOR_RED}❌ ERROR: {d.PROJECTS_FILE} not found.{d.COLOR_RESET}")
    sys.exit(1)

  participants = []
  with open(d.PROJECTS_FILE, "r", encoding="utf-8") as f:
    for line_num, raw in enumerate(f, 1):
      line = raw.split("#", 1)[0].strip()
      if not line:
        continue
      parts = [p.strip() for p in line.split(",")]
      if len(parts) < 2:
        continue
      pid = parts[0]
      user = parts[1]
      region = parts[2] if len(parts) >= 3 and parts[2] else "europe-west3"
      role, _, _ = d.parse_trailing_fields(parts[3:])
      if role != "admin":
        participants.append({
            "project_id": pid,
            "user": user,
            "region": region,
            "role": role,
            "line_num": line_num,
        })
  return participants


def describe_config(project_id: str, region: str, env: dict) -> dict:
  cmd = [
      "gcloud", "workstations", "configs", "describe", d.WS_CONFIG_ID,
      f"--cluster={d.WS_CLUSTER_ID}",
      f"--region={region}",
      f"--project={project_id}",
      "--format=json",
  ]
  data, err = d._run_gcloud_json(cmd, env, timeout=45)
  if not isinstance(data, dict):
    return {"error": err[:160]}
  return data


def patch_config_timeout(
    project_id: str,
    region: str,
    env: dict,
    idle_timeout: str = DEFAULT_IDLE_TIMEOUT,
) -> tuple[bool, str]:
  """Updates idleTimeout in-place via gcloud workstations configs update."""
  idle_timeout = normalize_timeout(idle_timeout)
  cfg = describe_config(project_id, region, env)
  if "error" in cfg:
    return False, f"describe failed: {cfg['error']}"
  current_idle = cfg.get("idleTimeout", "")
  if current_idle == idle_timeout and not cfg.get("degraded", False):
    return True, f"already {idle_timeout} (READY)"

  idle_seconds = idle_timeout.rstrip("s")
  cmd = [
      "gcloud", "workstations", "configs", "update", d.WS_CONFIG_ID,
      f"--cluster={d.WS_CLUSTER_ID}",
      f"--region={region}",
      f"--project={project_id}",
      f"--idle-timeout={idle_seconds}",
      "--async",
      "--quiet",
  ]
  try:
    res = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
      return False, (res.stderr or res.stdout or "update failed").strip()[:160]
    return True, f"updated {current_idle} -> {idle_timeout} (async issued)"
  except subprocess.TimeoutExpired:
    return False, "timeout updating config"


def describe_workstation(project_id: str, region: str, env: dict) -> dict:
  cmd = [
      "gcloud", "workstations", "describe", d.WS_ID,
      f"--cluster={d.WS_CLUSTER_ID}",
      f"--config={d.WS_CONFIG_ID}",
      f"--region={region}",
      f"--project={project_id}",
      "--format=json",
  ]
  data, err = d._run_gcloud_json(cmd, env, timeout=45)
  if not isinstance(data, dict):
    return {"error": err[:160]}
  return data


def warmup_single_project(
    project: dict,
    idx: int,
    mode: str,
    patch_timeout: bool,
    target_idle_timeout: str,
    start_timeout: int,
    env: dict,
) -> dict:
  """Audits, optionally patches timeout, and starts/stops a single workstation."""
  pid = project["project_id"]
  region = project["region"]
  target_idle_timeout = normalize_timeout(target_idle_timeout)
  # Stagger worker startup slightly to avoid bursting the API / zone allocator
  if mode == "start":
    time.sleep((idx % 6) * 1.5)

  out = {
      "project_id": pid,
      "region": region,
      "config_state": "UNKNOWN",
      "idle_timeout": "UNKNOWN",
      "running_timeout": "UNKNOWN",
      "timeout_action": "SKIPPED",
      "ws_state": "UNKNOWN",
      "action_result": "NONE",
      "ok": False,
  }

  if patch_timeout:
    ok_patch, msg = patch_config_timeout(pid, region, env, target_idle_timeout)
    out["timeout_action"] = msg
    if not ok_patch:
      out["action_result"] = f"PATCH_FAILED: {msg}"
      return out

  cfg = describe_config(pid, region, env)
  if "error" in cfg:
    out["config_state"] = "ERROR"
    out["action_result"] = cfg["error"]
    return out

  degraded = bool(cfg.get("degraded", False))
  reconciling = bool(cfg.get("reconciling", False))
  c_state = "DEGRADED" if degraded else ("RECONCILING" if reconciling else "HEALTHY")
  out["config_state"] = c_state
  out["idle_timeout"] = cfg.get("idleTimeout", "UNKNOWN")
  out["running_timeout"] = cfg.get("runningTimeout", "UNKNOWN")

  ws = describe_workstation(pid, region, env)
  if "error" in ws:
    out["ws_state"] = "ERROR"
    out["action_result"] = ws["error"]
    return out

  ws_state = ws.get("state", "UNKNOWN")
  out["ws_state"] = ws_state

  if mode == "status":
    out["action_result"] = "AUDIT_ONLY"
    timeout_ok = (not patch_timeout) or (out["idle_timeout"] == target_idle_timeout) or reconciling
    out["ok"] = (not degraded and timeout_ok)
    return out

  ws_args = [
      d.WS_ID,
      f"--cluster={d.WS_CLUSTER_ID}",
      f"--config={d.WS_CONFIG_ID}",
      f"--region={region}",
      f"--project={pid}",
  ]

  if mode == "start":
    if ws_state == "STATE_RUNNING":
      out["action_result"] = "ALREADY_RUNNING"
      out["ok"] = True
      return out

    # Start and leave RUNNING
    t0 = time.time()
    for _ in range(1, 3):
      subprocess.run(
          ["gcloud", "workstations", "start", *ws_args, "--async", "--quiet"],
          env=env, capture_output=True, text=True, timeout=60,
      )
      deadline = time.time() + start_timeout
      while time.time() < deadline:
        time.sleep(15)
        cur = describe_workstation(pid, region, env).get("state", "UNKNOWN")
        out["ws_state"] = cur
        if cur == "STATE_RUNNING":
          elapsed = int(time.time() - t0)
          out["action_result"] = f"STARTED ({elapsed}s) - KEPT ONLINE"
          out["ok"] = True
          return out
      # If still not running after attempt 1, retry start once
    out["action_result"] = f"START_TIMEOUT (last={out['ws_state']})"
    out["ok"] = False
    return out

  if mode == "stop":
    if ws_state == "STATE_STOPPED":
      out["action_result"] = "ALREADY_STOPPED"
      out["ok"] = True
      return out
    res = subprocess.run(
        ["gcloud", "workstations", "stop", *ws_args, "--quiet"],
        env=env, capture_output=True, text=True, timeout=180,
    )
    ws_after = describe_workstation(pid, region, env).get("state", "UNKNOWN")
    out["ws_state"] = ws_after
    out["ok"] = (res.returncode == 0)
    out["action_result"] = "STOPPED" if out["ok"] else (res.stderr or "STOP_FAILED")[:100]
    return out

  return out


def main() -> int:
  parser = argparse.ArgumentParser(description="Warm up and audit Cloud Workstations for event day")
  group = parser.add_mutually_exclusive_group()
  group.add_argument("--status", action="store_true", help="Read-only status check of all participant workstations")
  group.add_argument("--start", action="store_true", help="Start all participant workstations and keep them RUNNING")
  group.add_argument("--stop", action="store_true", help="Stop all participant workstations")
  parser.add_argument("--patch-timeout", action="store_true", help="Patch workstation-config idleTimeout in-place")
  parser.add_argument("--idle-timeout", default=DEFAULT_IDLE_TIMEOUT, help="Target idle timeout when --patch-timeout is set (default: 28800s / 8h)")
  parser.add_argument("--max-workers", "-w", type=int, default=6, help="Concurrent threads (default 6 to prevent GCE stockout spikes)")
  parser.add_argument("--start-timeout", type=int, default=420, help="Seconds to wait per VM to reach STATE_RUNNING")
  args = parser.parse_args()

  mode = "start" if args.start else ("stop" if args.stop else "status")
  participants = get_participant_projects()
  print(
      f"\n🚀 Cloud Workstations Event Manager | Mode: {mode.upper()} | "
      f"Patch Timeout ({normalize_timeout(args.idle_timeout)}): {args.patch_timeout} | "
      f"Projects: {len(participants)} | Workers: {args.max_workers}",
      flush=True,
  )
  print("🔑 Verifying active gcloud authentication...", flush=True)
  try:
    probe = subprocess.run(
        ["gcloud", "auth", "print-access-token", "--quiet"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if probe.returncode != 0 or not probe.stdout.strip():
      err_msg = (probe.stderr or probe.stdout or "Unknown auth error").strip()
      print(
          f"\n{d.COLOR_RED}❌ gcloud authentication failed or expired:{d.COLOR_RESET}\n{err_msg}\n"
          f"Run `gcloud auth login` in your terminal first, then re-run `./warmup.sh`.",
          flush=True,
      )
      return 1
  except subprocess.TimeoutExpired:
    print(
        f"\n{d.COLOR_RED}❌ `gcloud auth print-access-token` timed out after 20s.{d.COLOR_RESET}\n"
        f"Check your active credentials or security key touch prompt, and run "
        f"`gcloud auth login` and `gcloud auth print-access-token` in your terminal first.",
        flush=True,
    )
    return 1

  # Use native gcloud environment rather than pinning a static CLOUDSDK_AUTH_ACCESS_TOKEN
  # that expires after 60 minutes during long multi-wave runs.
  env = os.environ.copy()
  existing_metrics = env.get("CLOUDSDK_METRICS_ENVIRONMENT", "")
  env["CLOUDSDK_METRICS_ENVIRONMENT"] = (f"{existing_metrics} datacloud.antigravity").strip()
  target_idle = normalize_timeout(args.idle_timeout)
  print("✔ Authentication verified. Dispatching workers...\n", flush=True)

  results = []
  with ThreadPoolExecutor(max_workers=max(1, args.max_workers)) as pool:
    futs = {
        pool.submit(
            warmup_single_project,
            p,
            i,
            mode,
            args.patch_timeout,
            target_idle,
            args.start_timeout,
            env,
        ): p["project_id"]
        for i, p in enumerate(participants)
    }
    for fut in as_completed(futs):
      r = fut.result()
      results.append(r)
      badge = "✔" if r["ok"] else "✘"
      print(
          f"  {badge} {r['project_id']:<24} | cfg={r['config_state']:<11} | "
          f"idle={r['idle_timeout']:<7} | ws={r['ws_state']:<14} | "
          f"{r['timeout_action']} | {r['action_result']}",
          flush=True,
      )

  results.sort(key=lambda x: x["project_id"])
  os.makedirs(d.LOG_DIR, exist_ok=True)
  out_path = os.path.join(d.LOG_DIR, "warmup_status.json")
  with open(out_path, "w", encoding="utf-8") as f:
    json.dump(
        {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "mode": mode,
            "results": results,
        },
        f,
        indent=2,
    )

  passed = sum(1 for r in results if r["ok"])
  failed = len(results) - passed
  print("\n" + "=" * 105)
  print(f"Summary: {passed}/{len(results)} succeeded, {failed} failed. Report saved to {out_path}")
  print("=" * 105)
  return 0 if failed == 0 else 1


if __name__ == "__main__":
  sys.exit(main())
