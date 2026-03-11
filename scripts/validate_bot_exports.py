#!/usr/bin/env python3
"""Validate NexusAI bot export files for trigger graph integrity."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Sequence


@dataclass
class BotExport:
    bot_id: str
    name: str
    path: Path
    triggers: List[Dict[str, Any]]
    workflow_mismatch: bool


def _is_template(value: Any) -> bool:
    return isinstance(value, str) and "{{" in value and "}}" in value


def _normalize_triggers(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _load_export(path: Path) -> BotExport:
    raw = json.loads(path.read_text(encoding="utf-8"))
    bot = raw.get("bot") or {}
    bot_id = str(bot.get("id") or "").strip()
    name = str(bot.get("name") or bot_id)

    workflow_triggers = _normalize_triggers((bot.get("workflow") or {}).get("triggers"))
    routing_triggers = _normalize_triggers(
        ((bot.get("routing_rules") or {}).get("workflow") or {}).get("triggers")
    )

    if workflow_triggers:
        selected = workflow_triggers
    else:
        selected = routing_triggers

    workflow_mismatch = bool(workflow_triggers and routing_triggers and workflow_triggers != routing_triggers)
    return BotExport(
        bot_id=bot_id,
        name=name,
        path=path,
        triggers=selected,
        workflow_mismatch=workflow_mismatch,
    )


def _validate(
    exports: Sequence[BotExport],
    terminal_bots: Sequence[str],
    strict_dead_ends: bool,
) -> int:
    exit_code = 0
    bot_ids = {item.bot_id for item in exports}
    terminal = {item for item in terminal_bots if item}

    print("Trigger graph")
    for item in sorted(exports, key=lambda x: x.bot_id):
        if not item.triggers:
            print(f"- {item.bot_id} -> (none)")
            continue
        for trigger in item.triggers:
            target = trigger.get("target_bot_id")
            trigger_id = trigger.get("id")
            join_expected = trigger.get("join_expected_field")
            fanout = trigger.get("fan_out_field")
            print(
                f"- {item.bot_id} --[{trigger_id} join={join_expected!r} fan_out={fanout!r}]--> {target}"
            )

    print("\nValidation")
    for item in sorted(exports, key=lambda x: x.bot_id):
        if not item.bot_id:
            print(f"ERROR: missing bot.id in {item.path}")
            exit_code = 1
            continue
        if item.workflow_mismatch:
            print(
                f"WARNING: workflow trigger mismatch between bot.workflow and routing_rules.workflow in {item.path}"
            )
        for trigger in item.triggers:
            trigger_id = str(trigger.get("id") or "<missing-id>")
            target = trigger.get("target_bot_id")
            if not target:
                print(f"ERROR: {item.bot_id}:{trigger_id} is missing target_bot_id")
                exit_code = 1
                continue
            if isinstance(target, str) and not _is_template(target) and target not in bot_ids:
                print(
                    f"ERROR: {item.bot_id}:{trigger_id} target_bot_id '{target}' does not exist in exports"
                )
                exit_code = 1
            uses_join = any(
                bool(str(trigger.get(field) or "").strip())
                for field in (
                    "join_expected_field",
                    "join_group_field",
                    "join_items_alias",
                    "join_result_field",
                    "join_result_items_alias",
                    "join_sort_field",
                )
            )
            uses_fanout = bool(str(trigger.get("fan_out_field") or "").strip())
            if uses_join and uses_fanout:
                print(
                    f"WARNING: {item.bot_id}:{trigger_id} mixes fan-out and join fields; verify this is intentional."
                )

    for item in sorted(exports, key=lambda x: x.bot_id):
        if item.bot_id in terminal:
            continue
        has_static_targets = any(
            isinstance(trigger.get("target_bot_id"), str)
            and str(trigger.get("target_bot_id")).strip()
            and not _is_template(trigger.get("target_bot_id"))
            for trigger in item.triggers
        )
        if not has_static_targets and not item.triggers:
            message = f"WARNING: {item.bot_id} has no downstream trigger"
            if strict_dead_ends:
                print(f"ERROR: {message[9:]}")
                exit_code = 1
            else:
                print(message)

    if exit_code == 0:
        print("OK: export set validated without hard errors.")
    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate NexusAI bot export trigger graph.")
    parser.add_argument(
        "exports_dir",
        type=Path,
        help="Directory containing *.bot.json export files.",
    )
    parser.add_argument(
        "--terminal-bot",
        action="append",
        default=["course-globeiq-importer"],
        help="Bot id allowed to have no downstream triggers (repeatable).",
    )
    parser.add_argument(
        "--strict-dead-ends",
        action="store_true",
        help="Treat non-terminal dead-end bots as validation errors.",
    )
    args = parser.parse_args()

    directory = args.exports_dir
    if not directory.exists() or not directory.is_dir():
        print(f"ERROR: exports directory not found: {directory}", file=sys.stderr)
        return 2

    files = sorted(directory.glob("*.bot.json"))
    if not files:
        print(f"ERROR: no *.bot.json files found in {directory}", file=sys.stderr)
        return 2

    exports = [_load_export(path) for path in files]
    return _validate(exports, args.terminal_bot, args.strict_dead_ends)


if __name__ == "__main__":
    raise SystemExit(main())
