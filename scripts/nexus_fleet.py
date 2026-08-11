#!/usr/bin/env python3
"""nexus-fleet: controlled start/stop/restart/status for NexusAI worker fleets.

This tool reads a rendered fleet summary.json (produced by render_worker_fleet.py)
and the associated docker-compose.worker-node.generated.yml to provide selective
control over worker containers by restart_policy group.

Usage:
    nexus-fleet status [--compose FILE] [--summary FILE]
    nexus-fleet start  [--compose FILE] [--summary FILE] [--group GROUP] [--worker ID]
    nexus-fleet stop   [--compose FILE] [--summary FILE] [--group GROUP] [--worker ID]
    nexus-fleet restart [--compose FILE] [--summary FILE] [--group GROUP] [--worker ID]

Groups:
    auto      - workers with restart_policy "auto" (restart on reboot unless stopped)
    always    - workers with restart_policy "always" (unconditionally restart)
    manual    - workers with restart_policy "manual" (never auto-restart)
    critical  - workers with restart_policy "always" (alias for always)
    all       - all workers regardless of restart_policy

If neither --group nor --worker is specified, the default group is "all" for
status, and "auto" for start/stop/restart (to avoid accidentally starting
manual workers).

Examples:
    # See what's running
    nexus-fleet status

    # After a reboot, bring up auto-restart workers
    nexus-fleet start --group auto

    # Bring up critical (always) workers only
    nexus-fleet start --group critical

    # Stop a specific worker
    nexus-fleet stop --worker content-repair-01

    # Restart all workers in the manual group
    nexus-fleet restart --group manual
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

DEFAULT_COMPOSE_FILE = "docker-compose.worker-node.generated.yml"
DEFAULT_SUMMARY_FILE = "summary.json"
VALID_GROUPS = {"auto", "always", "manual", "critical", "all"}
RESTART_POLICY_GROUPS = {"auto", "always", "manual"}


def _find_file(explicit: str | None, default_name: str, search_dirs: list[Path]) -> Path:
    if explicit:
        p = Path(explicit).resolve()
        if not p.exists():
            print(f"Error: {p} not found", file=sys.stderr)
            sys.exit(1)
        return p
    for d in search_dirs:
        candidate = d / default_name
        if candidate.exists():
            return candidate.resolve()
    print(f"Error: could not find {default_name} in {search_dirs}", file=sys.stderr)
    sys.exit(1)


def load_summary(summary_path: Path) -> dict[str, Any]:
    with summary_path.open(encoding="utf-8") as f:
        return json.load(f)


def workers_in_group(summary: dict[str, Any], group: str) -> list[dict[str, Any]]:
    workers = summary.get("workers", [])
    if group == "all":
        return workers
    if group == "critical":
        group = "always"
    return [w for w in workers if w.get("restart_policy") == group]


def filter_by_worker(workers: list[dict[str, Any]], worker_id: str) -> list[dict[str, Any]]:
    matched = [w for w in workers if w["id"] == worker_id or w["service"] == worker_id]
    if not matched:
        print(f"Error: worker '{worker_id}' not found in summary", file=sys.stderr)
        sys.exit(1)
    return matched


def docker_compose_cmd(compose_path: Path, subcmd: str, services: list[str], dry_run: bool = False) -> list[str]:
    cmd = ["docker", "compose", "-f", str(compose_path)]
    if subcmd in ("up",):
        cmd.append("up")
        cmd.append("-d")
    elif subcmd in ("down", "stop", "restart", "start"):
        cmd.append(subcmd)
    else:
        cmd.append(subcmd)
    cmd.extend(services)
    return cmd


def run_cmd(cmd: list[str], dry_run: bool = False) -> int:
    if dry_run:
        print("  [dry-run] " + " ".join(cmd))
        return 0
    result = subprocess.run(cmd, capture_output=False, check=False)
    return result.returncode


def cmd_status(compose_path: Path, summary: dict[str, Any]) -> int:
    workers = summary.get("workers", [])
    if not workers:
        print("No workers found in summary.")
        return 0

    ps_result = subprocess.run(
        ["docker", "compose", "-f", str(compose_path), "ps", "--format", "json"],
        capture_output=True,
        text=True,
        check=False,
    )

    container_status: dict[str, dict[str, Any]] = {}
    if ps_result.returncode == 0:
        for line in ps_result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            try:
                info = json.loads(line)
                service = info.get("Service", "")
                container_status[service] = info
            except json.JSONDecodeError:
                continue

    by_policy: dict[str, list[dict[str, Any]]] = {}
    for w in workers:
        policy = w.get("restart_policy", "auto")
        by_policy.setdefault(policy, []).append(w)

    print(f"{'SERVICE':<50} {'POLICY':<8} {'STATE':<12} {'STATUS'}")
    print("-" * 90)
    for policy in sorted(by_policy):
        for w in sorted(by_policy[policy], key=lambda x: x["service"]):
            svc = w["service"]
            cs = container_status.get(svc)
            if cs:
                state = cs.get("State", "?")
                status = cs.get("Status", "?")
            else:
                state = "absent"
                status = "not created"
            print(f"{svc:<50} {policy:<8} {state:<12} {status}")

    print()
    totals = {}
    for policy, ws in by_policy.items():
        running = sum(1 for w in ws if container_status.get(w["service"], {}).get("State") == "running")
        totals[policy] = (running, len(ws))
    for policy in sorted(totals):
        running, total = totals[policy]
        print(f"  {policy}: {running}/{total} running")
    return 0


def cmd_start_stop_restart(
    compose_path: Path,
    summary: dict[str, Any],
    action: str,
    group: str | None,
    worker_id: str | None,
    dry_run: bool,
) -> int:
    if group and worker_id:
        print("Error: --group and --worker are mutually exclusive", file=sys.stderr)
        return 1

    if worker_id:
        all_workers = summary.get("workers", [])
        selected = filter_by_worker(all_workers, worker_id)
    else:
        effective_group = group or "auto"
        if effective_group not in VALID_GROUPS:
            print(f"Error: invalid group '{effective_group}'. Choose from: {sorted(VALID_GROUPS)}", file=sys.stderr)
            return 1
        selected = workers_in_group(summary, effective_group)

    if not selected:
        print(f"No workers matched. (group={group or 'auto'}, worker={worker_id})")
        return 0

    services = [w["service"] for w in selected]
    print(f"Action: {action} | Workers: {len(selected)}")
    for w in selected:
        print(f"  {w['service']:<50} policy={w.get('restart_policy', 'auto')}")

    if action == "stop":
        cmd = docker_compose_cmd(compose_path, "stop", services, dry_run)
    elif action == "start":
        cmd = docker_compose_cmd(compose_path, "start", services, dry_run)
    elif action == "restart":
        cmd = docker_compose_cmd(compose_path, "restart", services, dry_run)
    elif action == "up":
        cmd = docker_compose_cmd(compose_path, "up", services, dry_run)
    else:
        print(f"Error: unknown action '{action}'", file=sys.stderr)
        return 1

    print()
    return run_cmd(cmd, dry_run)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Controlled start/stop/restart/status for NexusAI worker fleets.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--compose",
        default=None,
        help=f"Path to the rendered compose file (default: {DEFAULT_COMPOSE_FILE} in the summary dir)",
    )
    parser.add_argument(
        "--summary",
        default=None,
        help=f"Path to the rendered summary.json (default: {DEFAULT_SUMMARY_FILE} in cwd or parent)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print commands without executing")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Show status of all workers grouped by restart_policy")

    for cmd_name in ("start", "stop", "restart"):
        p = sub.add_parser(cmd_name, help=f"{cmd_name} workers by group or name")
        p.add_argument(
            "--group",
            default=None,
            help=f"Restart policy group: {sorted(VALID_GROUPS)}. Default for start/stop/restart: auto",
        )
        p.add_argument("--worker", default=None, help="Specific worker id or service name")

    args = parser.parse_args()

    search_dirs = [Path.cwd(), Path.cwd().parent]
    summary_path = _find_file(args.summary, DEFAULT_SUMMARY_FILE, search_dirs)

    compose_default_dir = summary_path.parent
    compose_path = _find_file(args.compose, DEFAULT_COMPOSE_FILE, [compose_default_dir])

    summary = load_summary(summary_path)

    if args.command == "status":
        return cmd_status(compose_path, summary)
    else:
        return cmd_start_stop_restart(
            compose_path,
            summary,
            args.command,
            args.group,
            args.worker,
            args.dry_run,
        )


if __name__ == "__main__":
    sys.exit(main())