"""
Three-layer orchestration model:
- Layer A: OrchestrationTemplate (public, platform-owned)
- Layer B: PipelineBinding (user/private-owned)
- Layer C: RunContract (per-assignment, compiled)

Private bot configs are NOT stored here. The template owns the grammar;
users supply bot packs that bind to template roles.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite

from control_plane.sqlite_helpers import open_sqlite

_DEFAULT_DB_PATH = str(Path(__file__).parent.parent.parent / "data" / "nexusai.db")

_CREATE_TEMPLATES = """
CREATE TABLE IF NOT EXISTS orchestration_templates (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    version TEXT NOT NULL DEFAULT 'v1',
    description TEXT NOT NULL DEFAULT '',
    stage_roles_json TEXT NOT NULL DEFAULT '[]',
    graph_shape_json TEXT NOT NULL DEFAULT '{}',
    fan_out_rules_json TEXT NOT NULL DEFAULT '[]',
    join_rules_json TEXT NOT NULL DEFAULT '[]',
    retry_routes_json TEXT NOT NULL DEFAULT '[]',
    escalation_routes_json TEXT NOT NULL DEFAULT '[]',
    required_outputs_json TEXT NOT NULL DEFAULT '{}',
    terminal_conditions_json TEXT NOT NULL DEFAULT '{}',
    allowed_override_fields_json TEXT NOT NULL DEFAULT '[]',
    is_builtin INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_CREATE_BINDINGS = """
CREATE TABLE IF NOT EXISTS pipeline_bindings (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    template_id TEXT NOT NULL,
    owner_id TEXT,
    description TEXT NOT NULL DEFAULT '',
    role_map_json TEXT NOT NULL DEFAULT '{}',
    default_stage_configs_json TEXT NOT NULL DEFAULT '{}',
    default_connection_requirements_json TEXT NOT NULL DEFAULT '[]',
    default_context_requirements_json TEXT NOT NULL DEFAULT '[]',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_CREATE_RUN_CONTRACTS = """
CREATE TABLE IF NOT EXISTS run_contracts (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE,
    template_id TEXT,
    binding_id TEXT,
    project_id TEXT,
    conversation_id TEXT,
    assignment_text TEXT NOT NULL DEFAULT '',
    operator_brief TEXT NOT NULL DEFAULT '',
    stage_overrides_json TEXT NOT NULL DEFAULT '{}',
    connection_bindings_json TEXT NOT NULL DEFAULT '{}',
    context_pack_ids_json TEXT NOT NULL DEFAULT '[]',
    expected_deliverables_json TEXT NOT NULL DEFAULT '[]',
    success_criteria_json TEXT NOT NULL DEFAULT '[]',
    quality_gates_json TEXT NOT NULL DEFAULT '[]',
    test_suite_ids_json TEXT NOT NULL DEFAULT '[]',
    runtime_policy_json TEXT NOT NULL DEFAULT '{}',
    created_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_CREATE_TEMPLATE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_templates_name ON orchestration_templates(name)",
    "CREATE INDEX IF NOT EXISTS idx_bindings_template ON pipeline_bindings(template_id)",
    "CREATE INDEX IF NOT EXISTS idx_bindings_owner ON pipeline_bindings(owner_id)",
    "CREATE INDEX IF NOT EXISTS idx_run_contracts_run ON run_contracts(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_run_contracts_binding ON run_contracts(binding_id)",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_path() -> str:
    db_url = str(os.environ.get("DATABASE_URL", "") or "").strip()
    if db_url.startswith("sqlite:///"):
        return db_url[len("sqlite:///"):]
    return _DEFAULT_DB_PATH


def _dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


def _loads(raw: Any, default: Any) -> Any:
    text = str(raw or "").strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except Exception:
        return default


class OrchestrationTemplateStore:
    """
    Stores public, platform-owned orchestration templates and user pipeline bindings.
    Also stores per-run contracts compiled from template + binding + overrides.
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = db_path or _db_path()
        self._ready = False

    async def _ensure_db(self) -> None:
        if self._ready:
            return
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        async with open_sqlite(self._db_path) as db:
            await db.execute(_CREATE_TEMPLATES)
            await db.execute(_CREATE_BINDINGS)
            await db.execute(_CREATE_RUN_CONTRACTS)
            for stmt in _CREATE_TEMPLATE_INDEXES:
                await db.execute(stmt)
            await db.commit()
            # Seed built-in templates
            await self._seed_builtin_templates(db)
            await db.commit()
        self._ready = True

    async def _seed_builtin_templates(self, db: aiosqlite.Connection) -> None:
        """Seed the built-in PM Software Delivery template if not present."""
        async with db.execute("SELECT id FROM orchestration_templates WHERE id = 'pm_software_delivery_v1' LIMIT 1") as cursor:
            row = await cursor.fetchone()
        if row is not None:
            return
        now = _now()
        stage_roles = [
            "planner", "research_repo", "research_data", "research_web",
            "engineer", "coder", "tester", "security_reviewer",
            "database_engineer", "ui_tester", "final_qc"
        ]
        graph_shape = {
            "lanes": [
                {"id": "planner", "role": "planner", "depends_on": []},
                {"id": "research_repo", "role": "research_repo", "depends_on": ["planner"]},
                {"id": "research_data", "role": "research_data", "depends_on": ["planner"]},
                {"id": "research_web", "role": "research_web", "depends_on": ["planner"]},
                {"id": "engineer", "role": "engineer", "depends_on": ["research_repo", "research_data", "research_web"], "join_id": "research_join"},
                {"id": "coder", "role": "coder", "depends_on": ["engineer"], "fan_out_id": "coder_fan_out"},
                {"id": "tester", "role": "tester", "depends_on": ["coder"], "branch_local": True},
                {"id": "security_reviewer", "role": "security_reviewer", "depends_on": ["tester"], "branch_local": True},
                {"id": "database_engineer", "role": "database_engineer", "depends_on": ["security_reviewer"], "join_id": "security_join"},
                {"id": "ui_tester", "role": "ui_tester", "depends_on": ["database_engineer"]},
                {"id": "final_qc", "role": "final_qc", "depends_on": ["ui_tester"]},
            ]
        }
        join_rules = [
            {
                "join_id": "research_join",
                "required_upstream_roles": ["research_repo", "research_data", "research_web"],
                "expected_branch_count": 3,
                "acceptable_terminal_statuses": ["passed", "skipped"],
                "downstream_unlock_role": "engineer",
            },
            {
                "join_id": "security_join",
                "required_upstream_roles": ["security_reviewer"],
                "expected_branch_count": -1,
                "acceptable_terminal_statuses": ["passed", "skipped"],
                "downstream_unlock_role": "database_engineer",
            },
        ]
        fan_out_rules = [
            {
                "fan_out_id": "coder_fan_out",
                "source_role": "engineer",
                "source_output_field": "workstreams",
                "min_branch_count": 1,
                "max_branch_count": 10,
                "empty_result_behavior": "fail_explicit",
            }
        ]
        retry_routes = [
            {"from_role": "tester", "on_status": "failed", "retry_target": "coder", "max_retries": 2, "on_exhausted": "engineer"},
            {"from_role": "security_reviewer", "on_status": "failed", "retry_target": "coder", "max_retries": 2, "on_exhausted": "engineer"},
            {"from_role": "final_qc", "on_status": "failed", "retry_target": "planner", "max_retries": 1, "on_exhausted": "operator_input"},
        ]
        escalation_routes = [
            {"from_role": "coder", "on_condition": "branch_retry_exhausted", "escalate_to": "engineer"},
            {"from_role": "engineer", "on_condition": "workstream_count_zero", "escalate_to": "operator_input"},
        ]
        terminal_conditions = {
            "terminal_role": "final_qc",
            "requires_all_joins_resolved": True,
            "requires_deliverables": True,
            "requires_test_suite_pass": False,
        }
        allowed_override_fields = [
            "enabled", "mode", "instruction_suffix", "connection_binding",
            "output_mode", "expected_deliverables", "tool_policy", "test_policy"
        ]
        await db.execute(
            """
            INSERT INTO orchestration_templates (
                id, name, version, description, stage_roles_json, graph_shape_json,
                fan_out_rules_json, join_rules_json, retry_routes_json, escalation_routes_json,
                required_outputs_json, terminal_conditions_json, allowed_override_fields_json,
                is_builtin, metadata_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "pm_software_delivery_v1",
                "PM Software Delivery",
                "v1",
                "Deterministic PM software delivery pipeline: planner → research → engineer → coder → tester → security → database → UI → final QC",
                _dumps(stage_roles),
                _dumps(graph_shape),
                _dumps(fan_out_rules),
                _dumps(join_rules),
                _dumps(retry_routes),
                _dumps(escalation_routes),
                _dumps({}),
                _dumps(terminal_conditions),
                _dumps(allowed_override_fields),
                1,
                _dumps({"source": "builtin"}),
                now, now,
            )
        )
        # Also seed course_generation_v1
        async with db.execute("SELECT id FROM orchestration_templates WHERE id = 'course_generation_v1' LIMIT 1") as cursor:
            row2 = await cursor.fetchone()
        if row2 is None:
            await db.execute(
                """
                INSERT INTO orchestration_templates (
                    id, name, version, description, stage_roles_json, graph_shape_json,
                    fan_out_rules_json, join_rules_json, retry_routes_json, escalation_routes_json,
                    required_outputs_json, terminal_conditions_json, allowed_override_fields_json,
                    is_builtin, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "course_generation_v1",
                    "Course Generation",
                    "v1",
                    "AI-powered course generation pipeline with research, content generation, review, and QC stages",
                    _dumps(["researcher", "content_writer", "reviewer", "editor", "qc_validator"]),
                    _dumps({
                        "lanes": [
                            {"id": "researcher", "role": "researcher", "depends_on": []},
                            {"id": "content_writer", "role": "content_writer", "depends_on": ["researcher"]},
                            {"id": "reviewer", "role": "reviewer", "depends_on": ["content_writer"]},
                            {"id": "editor", "role": "editor", "depends_on": ["reviewer"]},
                            {"id": "qc_validator", "role": "qc_validator", "depends_on": ["editor"]},
                        ]
                    }),
                    _dumps([]),
                    _dumps([]),
                    _dumps([]),
                    _dumps([]),
                    _dumps({}),
                    _dumps({"terminal_role": "qc_validator", "requires_deliverables": True}),
                    _dumps(allowed_override_fields),
                    1,
                    _dumps({"source": "builtin"}),
                    now, now,
                )
            )

    # ── Template CRUD ──────────────────────────────────────────────────────────

    async def create_template(self, *, name: str, version: str = "v1", description: str = "",
        stage_roles: Optional[List[str]] = None, graph_shape: Optional[Dict[str, Any]] = None,
        fan_out_rules: Optional[List[Dict[str, Any]]] = None,
        join_rules: Optional[List[Dict[str, Any]]] = None,
        retry_routes: Optional[List[Dict[str, Any]]] = None,
        escalation_routes: Optional[List[Dict[str, Any]]] = None,
        terminal_conditions: Optional[Dict[str, Any]] = None,
        allowed_override_fields: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        await self._ensure_db()
        now = _now()
        tid = str(uuid.uuid4())
        async with open_sqlite(self._db_path) as db:
            await db.execute(
                """INSERT INTO orchestration_templates (
                    id, name, version, description, stage_roles_json, graph_shape_json,
                    fan_out_rules_json, join_rules_json, retry_routes_json, escalation_routes_json,
                    required_outputs_json, terminal_conditions_json, allowed_override_fields_json,
                    is_builtin, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (tid, name, version, description,
                 _dumps(stage_roles or []), _dumps(graph_shape or {}),
                 _dumps(fan_out_rules or []), _dumps(join_rules or []),
                 _dumps(retry_routes or []), _dumps(escalation_routes or []),
                 _dumps({}), _dumps(terminal_conditions or {}),
                 _dumps(allowed_override_fields or []), 0,
                 _dumps(metadata or {}), now, now)
            )
            await db.commit()
        return await self.get_template(tid)  # type: ignore

    async def get_template(self, template_id: str) -> Optional[Dict[str, Any]]:
        await self._ensure_db()
        async with open_sqlite(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM orchestration_templates WHERE id = ? LIMIT 1", (template_id,)) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_template(row)

    async def list_templates(self, *, limit: int = 100) -> List[Dict[str, Any]]:
        await self._ensure_db()
        async with open_sqlite(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM orchestration_templates ORDER BY is_builtin DESC, name ASC LIMIT ?", (limit,)) as cursor:
                rows = await cursor.fetchall()
        return [self._row_to_template(r) for r in rows]

    def _row_to_template(self, row: aiosqlite.Row) -> Dict[str, Any]:
        return {
            "id": str(row["id"]),
            "name": str(row["name"] or ""),
            "version": str(row["version"] or "v1"),
            "description": str(row["description"] or ""),
            "stage_roles": _loads(row["stage_roles_json"], []),
            "graph_shape": _loads(row["graph_shape_json"], {}),
            "fan_out_rules": _loads(row["fan_out_rules_json"], []),
            "join_rules": _loads(row["join_rules_json"], []),
            "retry_routes": _loads(row["retry_routes_json"], []),
            "escalation_routes": _loads(row["escalation_routes_json"], []),
            "terminal_conditions": _loads(row["terminal_conditions_json"], {}),
            "allowed_override_fields": _loads(row["allowed_override_fields_json"], []),
            "is_builtin": bool(row["is_builtin"]),
            "metadata": _loads(row["metadata_json"], {}),
            "created_at": str(row["created_at"] or ""),
            "updated_at": str(row["updated_at"] or ""),
        }

    # ── Binding CRUD ───────────────────────────────────────────────────────────

    async def create_binding(self, *, name: str, template_id: str, owner_id: Optional[str] = None,
        description: str = "", role_map: Optional[Dict[str, str]] = None,
        default_stage_configs: Optional[Dict[str, Any]] = None,
        default_connection_requirements: Optional[List[Dict[str, Any]]] = None,
        default_context_requirements: Optional[List[Dict[str, Any]]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        await self._ensure_db()
        now = _now()
        bid = str(uuid.uuid4())
        async with open_sqlite(self._db_path) as db:
            await db.execute(
                """INSERT INTO pipeline_bindings (
                    id, name, template_id, owner_id, description,
                    role_map_json, default_stage_configs_json,
                    default_connection_requirements_json, default_context_requirements_json,
                    metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (bid, name, template_id, owner_id or None, description,
                 _dumps(role_map or {}), _dumps(default_stage_configs or {}),
                 _dumps(default_connection_requirements or []),
                 _dumps(default_context_requirements or []),
                 _dumps(metadata or {}), now, now)
            )
            await db.commit()
        return await self.get_binding(bid)  # type: ignore

    async def get_binding(self, binding_id: str) -> Optional[Dict[str, Any]]:
        await self._ensure_db()
        async with open_sqlite(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM pipeline_bindings WHERE id = ? LIMIT 1", (binding_id,)) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_binding(row)

    async def list_bindings(self, *, template_id: Optional[str] = None, owner_id: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        await self._ensure_db()
        clauses, params = [], []
        if template_id:
            clauses.append("template_id = ?"); params.append(template_id)
        if owner_id:
            clauses.append("owner_id = ?"); params.append(owner_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        async with open_sqlite(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(f"SELECT * FROM pipeline_bindings {where} ORDER BY name ASC LIMIT ?", tuple(params) + (limit,)) as cursor:
                rows = await cursor.fetchall()
        return [self._row_to_binding(r) for r in rows]

    def _row_to_binding(self, row: aiosqlite.Row) -> Dict[str, Any]:
        return {
            "id": str(row["id"]),
            "name": str(row["name"] or ""),
            "template_id": str(row["template_id"] or ""),
            "owner_id": str(row["owner_id"] or "") or None,
            "description": str(row["description"] or ""),
            "role_map": _loads(row["role_map_json"], {}),
            "default_stage_configs": _loads(row["default_stage_configs_json"], {}),
            "default_connection_requirements": _loads(row["default_connection_requirements_json"], []),
            "default_context_requirements": _loads(row["default_context_requirements_json"], []),
            "metadata": _loads(row["metadata_json"], {}),
            "created_at": str(row["created_at"] or ""),
            "updated_at": str(row["updated_at"] or ""),
        }

    # ── RunContract CRUD ───────────────────────────────────────────────────────

    async def create_run_contract(self, *, run_id: str, template_id: Optional[str] = None,
        binding_id: Optional[str] = None, project_id: Optional[str] = None,
        conversation_id: Optional[str] = None, assignment_text: str = "",
        operator_brief: str = "", stage_overrides: Optional[Dict[str, Any]] = None,
        connection_bindings: Optional[Dict[str, Any]] = None,
        context_pack_ids: Optional[List[str]] = None,
        expected_deliverables: Optional[List[str]] = None,
        success_criteria: Optional[List[str]] = None,
        quality_gates: Optional[List[Dict[str, Any]]] = None,
        test_suite_ids: Optional[List[str]] = None,
        runtime_policy: Optional[Dict[str, Any]] = None,
        created_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        await self._ensure_db()
        now = _now()
        cid = str(uuid.uuid4())
        async with open_sqlite(self._db_path) as db:
            await db.execute(
                """INSERT INTO run_contracts (
                    id, run_id, template_id, binding_id, project_id, conversation_id,
                    assignment_text, operator_brief, stage_overrides_json,
                    connection_bindings_json, context_pack_ids_json,
                    expected_deliverables_json, success_criteria_json,
                    quality_gates_json, test_suite_ids_json, runtime_policy_json,
                    created_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (cid, run_id, template_id or None, binding_id or None,
                 project_id or None, conversation_id or None,
                 assignment_text, operator_brief,
                 _dumps(stage_overrides or {}),
                 _dumps(connection_bindings or {}),
                 _dumps(context_pack_ids or []),
                 _dumps(expected_deliverables or []),
                 _dumps(success_criteria or []),
                 _dumps(quality_gates or []),
                 _dumps(test_suite_ids or []),
                 _dumps(runtime_policy or {}),
                 created_by or None, now, now)
            )
            await db.commit()
        return await self.get_run_contract_by_run_id(run_id)  # type: ignore

    async def get_run_contract_by_run_id(self, run_id: str) -> Optional[Dict[str, Any]]:
        await self._ensure_db()
        async with open_sqlite(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM run_contracts WHERE run_id = ? LIMIT 1", (run_id,)) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_contract(row)

    def _row_to_contract(self, row: aiosqlite.Row) -> Dict[str, Any]:
        return {
            "id": str(row["id"]),
            "run_id": str(row["run_id"] or ""),
            "template_id": str(row["template_id"] or "") or None,
            "binding_id": str(row["binding_id"] or "") or None,
            "project_id": str(row["project_id"] or "") or None,
            "conversation_id": str(row["conversation_id"] or "") or None,
            "assignment_text": str(row["assignment_text"] or ""),
            "operator_brief": str(row["operator_brief"] or ""),
            "stage_overrides": _loads(row["stage_overrides_json"], {}),
            "connection_bindings": _loads(row["connection_bindings_json"], {}),
            "context_pack_ids": _loads(row["context_pack_ids_json"], []),
            "expected_deliverables": _loads(row["expected_deliverables_json"], []),
            "success_criteria": _loads(row["success_criteria_json"], []),
            "quality_gates": _loads(row["quality_gates_json"], []),
            "test_suite_ids": _loads(row["test_suite_ids_json"], []),
            "runtime_policy": _loads(row["runtime_policy_json"], {}),
            "created_by": str(row["created_by"] or "") or None,
            "created_at": str(row["created_at"] or ""),
            "updated_at": str(row["updated_at"] or ""),
        }

    def compile_run_contract(
        self,
        *,
        template: Optional[Dict[str, Any]] = None,
        binding: Optional[Dict[str, Any]] = None,
        overrides: Optional[Dict[str, Any]] = None,
        assignment_text: str = "",
        operator_brief: str = "",
    ) -> Dict[str, Any]:
        """
        Compile a RunContract from template + binding + overrides.
        This is the assignment preview / compile step.

        Merges: template defaults → binding defaults → per-run overrides.
        Does NOT require saving to DB - use create_run_contract for persistence.
        """
        t = template or {}
        b = binding or {}
        o = overrides or {}

        # Stage overrides: template allowed fields → binding defaults → per-run overrides
        binding_configs = b.get("default_stage_configs") or {}
        run_overrides = o.get("stage_overrides") or {}
        merged_overrides: Dict[str, Any] = {}
        allowed_fields = t.get("allowed_override_fields") or []
        for stage_role, config in {**binding_configs, **run_overrides}.items():
            if isinstance(config, dict):
                filtered = {k: v for k, v in config.items() if not allowed_fields or k in allowed_fields}
                merged_overrides[stage_role] = filtered

        return {
            "template_id": t.get("id"),
            "binding_id": b.get("id"),
            "project_id": o.get("project_id"),
            "conversation_id": o.get("conversation_id"),
            "assignment_text": assignment_text,
            "operator_brief": operator_brief,
            "stage_roles": t.get("stage_roles") or [],
            "graph_shape": t.get("graph_shape") or {},
            "join_rules": t.get("join_rules") or [],
            "fan_out_rules": t.get("fan_out_rules") or [],
            "retry_routes": t.get("retry_routes") or [],
            "escalation_routes": t.get("escalation_routes") or [],
            "terminal_conditions": t.get("terminal_conditions") or {},
            "role_map": b.get("role_map") or {},
            "stage_overrides": merged_overrides,
            "connection_bindings": o.get("connection_bindings") or {},
            "context_pack_ids": o.get("context_pack_ids") or [],
            "expected_deliverables": o.get("expected_deliverables") or [],
            "success_criteria": o.get("success_criteria") or [],
            "quality_gates": o.get("quality_gates") or [],
            "test_suite_ids": o.get("test_suite_ids") or [],
            "runtime_policy": o.get("runtime_policy") or {},
        }
