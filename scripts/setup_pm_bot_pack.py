#!/usr/bin/env python3
"""Generate and optionally apply a PM/coding bot pack using Ollama Cloud backends.

Usage examples:
  py scripts/setup_pm_bot_pack.py --export-dir "C:\\tmp\\pm-pack"
  py scripts/setup_pm_bot_pack.py --apply --base-url http://127.0.0.1:8000 --api-token <token>
  py scripts/setup_pm_bot_pack.py --export-dir "C:\\tmp\\pm-pack" --apply
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple
from urllib import error, request


DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_API_KEY_REF = "Ollama_Cloud1"


@dataclass(frozen=True)
class BotSpec:
    bot_id: str
    name: str
    role: str
    model: str
    priority: int
    system_prompt: str
    max_tokens: int = 4096
    temperature: float = 0.1
    output_contract_json: bool = False


def _pm_specs() -> List[BotSpec]:
    return [
        BotSpec(
            bot_id="pm-orchestrator",
            name="PM Orchestrator",
            role="pm",
            model="gpt-oss:120b-cloud",
            priority=100,
            max_tokens=4096,
            temperature=0.05,
            output_contract_json=True,
            system_prompt=(
                "You are a deterministic project manager orchestrator.\n"
                "Given an implementation request, return JSON only with:\n"
                "{\n"
                '  "global_acceptance_criteria": [],\n'
                '  "global_quality_gates": [],\n'
                '  "risks": [],\n'
                '  "steps": [\n'
                "    {\n"
                '      "id": "step_1",\n'
                '      "title": "",\n'
                '      "instruction": "",\n'
                '      "role_hint": "researcher|coder|tester|reviewer|security|dba",\n'
                '      "depends_on": [],\n'
                '      "acceptance_criteria": [],\n'
                '      "deliverables": [],\n'
                '      "quality_gates": []\n'
                "    }\n"
                "  ]\n"
                "}\n"
                "No markdown. No prose outside JSON."
            ),
        ),
        BotSpec(
            bot_id="pm-research-analyst",
            name="PM Research Analyst",
            role="researcher",
            model="qwen3.5:397b-cloud",
            priority=70,
            system_prompt=(
                "You are a research/analysis bot.\n"
                "Turn the task payload into clear requirements, assumptions, and edge cases.\n"
                "Prioritize implementation clarity and testability."
            ),
        ),
        BotSpec(
            bot_id="pm-coder",
            name="PM Coder",
            role="coder",
            model="qwen3.5:397b-cloud",
            priority=85,
            system_prompt=(
                "You are a coding bot.\n"
                "Use acceptance criteria and quality gates from payload as non-negotiable constraints.\n"
                "Return implementation-focused output with concise risk notes."
            ),
        ),
        BotSpec(
            bot_id="pm-tester",
            name="PM Tester",
            role="tester",
            model="gpt-oss:120b-cloud",
            priority=80,
            system_prompt=(
                "You are a QA/testing bot.\n"
                "Design and run validation against acceptance criteria.\n"
                "Report failing scenarios, regression risk, and required fixes."
            ),
        ),
        BotSpec(
            bot_id="pm-security-reviewer",
            name="PM Security Reviewer",
            role="security-reviewer",
            model="gpt-oss:120b-cloud",
            priority=78,
            system_prompt=(
                "You are a security and quality reviewer.\n"
                "Focus on security leaks, authz/authn issues, injection paths, data exposure, and runtime risks.\n"
                "Return findings ordered by severity."
            ),
        ),
        BotSpec(
            bot_id="pm-database-engineer",
            name="PM Database Engineer",
            role="dba-sql",
            model="qwen3.5:397b-cloud",
            priority=76,
            system_prompt=(
                "You are a database engineer bot.\n"
                "Design schema/query/data migration updates with rollback and data integrity safeguards.\n"
                "Call out lock/contention and compatibility risk explicitly."
            ),
        ),
    ]


def _backend_payload(spec: BotSpec, api_key_ref: str) -> Dict[str, Any]:
    return {
        "type": "cloud_api",
        "provider": "ollama_cloud",
        "model": spec.model,
        "api_key_ref": api_key_ref,
        "params": {
            "temperature": spec.temperature,
            "max_tokens": spec.max_tokens,
        },
    }


def _routing_rules_payload(spec: BotSpec) -> Dict[str, Any]:
    output_contract: Dict[str, Any] = {
        "enabled": bool(spec.output_contract_json),
        "description": (
            "Deterministic PM plan output."
            if spec.output_contract_json
            else "Optional free-form output."
        ),
        "mode": "model_output",
        "format": "json_object" if spec.output_contract_json else "any",
        "required_fields": ["steps"] if spec.output_contract_json else [],
        "non_empty_fields": ["steps"] if spec.output_contract_json else [],
        "template": {},
        "defaults_template": {},
        "fallback_mode": "disabled",
    }
    return {
        "input_contract": {
            "description": "",
            "default_payload": {},
            "form_fields": [],
        },
        "input_transform": {
            "enabled": False,
            "description": "",
            "template": {},
        },
        "launch_profile": {
            "enabled": True,
            "label": spec.name,
            "description": "",
            "payload": {},
            "priority": None,
            "project_id": None,
            "show_on_overview": True,
            "show_on_tasks": True,
        },
        "output_contract": output_contract,
        "workflow": {
            "notes": "",
            "triggers": [],
        },
    }


def _bot_payload(spec: BotSpec, api_key_ref: str) -> Dict[str, Any]:
    return {
        "id": spec.bot_id,
        "name": spec.name,
        "role": spec.role,
        "system_prompt": spec.system_prompt,
        "priority": spec.priority,
        "enabled": True,
        "backends": [_backend_payload(spec, api_key_ref)],
        "routing_rules": _routing_rules_payload(spec),
        "workflow": {
            "notes": "",
            "triggers": [],
        },
    }


def _bundle_payload(spec: BotSpec, api_key_ref: str) -> Dict[str, Any]:
    return {
        "schema_version": "nexusai.bot-export.v1",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "bot": _bot_payload(spec, api_key_ref),
        "connections": [],
    }


def _http_json(
    method: str,
    url: str,
    *,
    token: str = "",
    body: Dict[str, Any] | None = None,
) -> Tuple[int, Dict[str, Any] | List[Any] | None]:
    headers = {"Content-Type": "application/json"}
    token_clean = token.strip()
    if token_clean:
        headers["X-Nexus-API-Key"] = token_clean
        headers["Authorization"] = f"Bearer {token_clean}"
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = request.Request(url, method=method.upper(), headers=headers, data=data)
    try:
        with request.urlopen(req, timeout=45) as resp:
            raw = resp.read().decode("utf-8", errors="replace").strip()
            payload = json.loads(raw) if raw else None
            return int(resp.status), payload
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace").strip()
        payload = None
        if raw:
            try:
                payload = json.loads(raw)
            except Exception:
                payload = {"detail": raw}
        return int(exc.code), payload


def _upsert_bot(base_url: str, token: str, bot_payload: Dict[str, Any], dry_run: bool) -> str:
    bot_id = str(bot_payload.get("id") or "").strip()
    if not bot_id:
        raise ValueError("bot payload missing id")
    get_url = f"{base_url.rstrip('/')}/v1/bots/{bot_id}"
    create_url = f"{base_url.rstrip('/')}/v1/bots"
    update_url = f"{base_url.rstrip('/')}/v1/bots/{bot_id}"

    if dry_run:
        return "dry-run"

    status, _ = _http_json("GET", get_url, token=token)
    if status == 200:
        put_status, put_payload = _http_json("PUT", update_url, token=token, body=bot_payload)
        if put_status != 200:
            raise RuntimeError(f"PUT {update_url} failed ({put_status}): {put_payload}")
        return "updated"
    if status not in {404}:
        raise RuntimeError(f"GET {get_url} failed ({status})")

    post_status, post_payload = _http_json("POST", create_url, token=token, body=bot_payload)
    if post_status != 200:
        raise RuntimeError(f"POST {create_url} failed ({post_status}): {post_payload}")
    return "created"


def _write_exports(bundles: Iterable[Tuple[BotSpec, Dict[str, Any]]], export_dir: Path) -> List[Path]:
    export_dir.mkdir(parents=True, exist_ok=True)
    outputs: List[Path] = []
    for spec, bundle in bundles:
        target = export_dir / f"{spec.bot_id}.bot.json"
        target.write_text(json.dumps(bundle, indent=2) + "\n", encoding="utf-8")
        outputs.append(target)
    return outputs


def main() -> int:
    parser = argparse.ArgumentParser(description="Setup PM bot pack using Ollama Cloud models.")
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"Control plane base URL (default: {DEFAULT_BASE_URL}).",
    )
    parser.add_argument(
        "--api-token",
        default=os.environ.get("CONTROL_PLANE_API_TOKEN", ""),
        help="Control plane API token (or set CONTROL_PLANE_API_TOKEN).",
    )
    parser.add_argument(
        "--api-key-ref",
        default=DEFAULT_API_KEY_REF,
        help=f"Ollama Cloud API key reference to store on each bot backend (default: {DEFAULT_API_KEY_REF}).",
    )
    parser.add_argument(
        "--export-dir",
        type=Path,
        default=None,
        help="Optional directory to write importable *.bot.json bundles.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply pack to control plane via /v1/bots create/update.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="When used with --apply, print actions without mutating bots.",
    )
    args = parser.parse_args()

    specs = _pm_specs()
    bundles = [(spec, _bundle_payload(spec, args.api_key_ref)) for spec in specs]

    if args.export_dir is not None:
        outputs = _write_exports(bundles, args.export_dir)
        print(f"Wrote {len(outputs)} bot export file(s) to: {args.export_dir}")
        for path in outputs:
            print(f" - {path}")

    if args.apply:
        if not args.base_url.strip():
            print("ERROR: --base-url is required when --apply is used.")
            return 2
        for spec, bundle in bundles:
            action = _upsert_bot(
                base_url=args.base_url,
                token=args.api_token,
                bot_payload=bundle["bot"],
                dry_run=args.dry_run,
            )
            print(f"{spec.bot_id}: {action}")
        print("PM bot pack apply complete.")
    elif args.export_dir is None:
        print("No action taken. Use --export-dir and/or --apply.")
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

