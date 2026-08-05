#!/usr/bin/env python3
"""Summarize bot tooling readiness from exported control-plane snapshots."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from dashboard.bot_tooling_status import build_bot_tooling_status


def _load_json(path: Path | None, default: Any) -> Any:
    if path is None:
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _as_bots(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("bots", "items", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _as_workers(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("workers", "items", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _print_text(status: dict[str, Any], *, limit: int) -> None:
    summary = status.get("summary") if isinstance(status.get("summary"), dict) else {}
    print("Bot tooling readiness")
    print(f"- total bots: {summary.get('total', 0)}")
    print(f"- ready: {summary.get('ready', 0)}")
    print(f"- blocked: {summary.get('blocked', 0)}")
    print(f"- disabled: {summary.get('disabled', 0)}")
    print(f"- disabled needing fixes: {summary.get('disabled_activation_blocker_bot_count', 0)}")
    print(f"- degraded worker probes: {summary.get('degraded_worker_probe_count', 0)}")
    action = summary.get("recommended_action") if isinstance(summary.get("recommended_action"), dict) else {}
    if action:
        print(f"- recommended action: {action.get('label', 'inspect')} - {action.get('detail', '')}")

    groups = status.get("blocked_groups") if isinstance(status.get("blocked_groups"), list) else []
    if groups:
        print("\nBlocked groups")
    for group in groups:
        category = group.get("label") or group.get("category") or "Unknown"
        bots = group.get("bots") if isinstance(group.get("bots"), list) else []
        group_action = group.get("recommended_action") if isinstance(group.get("recommended_action"), dict) else {}
        print(f"- {category}: {len(bots)} bot(s)")
        if group_action:
            print(f"  action: {group_action.get('label', 'inspect')} - {group_action.get('detail', '')}")
        for bot in bots[:limit]:
            messages = bot.get("blocking_messages") if isinstance(bot.get("blocking_messages"), list) else []
            message = f" - {messages[0]}" if messages else ""
            print(f"  - {bot.get('bot_id') or bot.get('name')}{message}")


def summarize_snapshot(
    *,
    bots_path: Path,
    readiness_path: Path,
    workers_path: Path | None = None,
    worker_probes_path: Path | None = None,
) -> dict[str, Any]:
    return build_bot_tooling_status(
        bots=_as_bots(_load_json(bots_path, [])),
        readiness_payload=_load_json(readiness_path, {}),
        workers=_as_workers(_load_json(workers_path, [])),
        worker_probes_payload=_load_json(worker_probes_path, {}),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarize NexusAI bot tooling readiness snapshots.")
    parser.add_argument("--bots", required=True, type=Path, help="Path to exported bot list JSON.")
    parser.add_argument("--readiness", required=True, type=Path, help="Path to bot readiness JSON.")
    parser.add_argument("--workers", type=Path, help="Optional worker list JSON.")
    parser.add_argument("--worker-probes", type=Path, help="Optional worker probe JSON.")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--limit", type=int, default=5, help="Maximum blocked bots to print per group.")
    args = parser.parse_args(argv)

    status = summarize_snapshot(
        bots_path=args.bots,
        readiness_path=args.readiness,
        workers_path=args.workers,
        worker_probes_path=args.worker_probes,
    )
    if args.format == "json":
        print(json.dumps(status, indent=2, sort_keys=True))
    else:
        _print_text(status, limit=max(0, int(args.limit)))
    return 1 if int((status.get("summary") or {}).get("blocked", 0) or 0) else 0


if __name__ == "__main__":
    sys.exit(main())
