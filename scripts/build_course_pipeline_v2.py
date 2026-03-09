from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "course_pipeline_v2"

EMPTY_REVISION = {"attempted_before": False, "qc_summary": "", "issues": [], "fix_instructions": []}
LAUNCH = {"enabled": False, "label": "", "description": "", "payload": {}, "priority": 0, "show_on_overview": False, "show_on_tasks": False, "project_id": ""}


def backend(model: str, max_tokens: int, provider: str = "ollama_cloud", api_key_ref: str = "Ollama_Cloud1") -> list[dict]:
    return [{"type": "cloud_api", "provider": provider, "model": model, "api_key_ref": api_key_ref, "worker_id": None, "gpu_id": None, "params": {"temperature": None, "max_tokens": max_tokens, "top_p": None, "num_ctx": None, "num_gpu": None, "main_gpu": None, "num_thread": None, "repeat_penalty": None}}]


def custom_backend(provider: str) -> list[dict]:
    return [{"type": "custom", "provider": provider, "model": "deterministic-runtime", "api_key_ref": None, "worker_id": None, "gpu_id": None, "params": None}]


def trig(trigger_id: str, target_bot_id: str, **kwargs) -> dict:
    base = {"id": trigger_id, "title": trigger_id, "enabled": True, "event": "task_completed", "condition": "has_result", "target_bot_id": target_bot_id, "result_field": None, "result_equals": None, "payload_template": None, "fan_out_field": None, "fan_out_alias": None, "fan_out_index_alias": None, "join_group_field": None, "join_expected_field": None, "join_items_alias": None, "join_result_field": None, "join_result_items_alias": None, "join_sort_field": None, "inherit_metadata": True}
    base.update(kwargs)
    return base


def ic(description: str, required: list[str], non_empty: list[str], **kwargs) -> dict:
    payload = {"enabled": True, "format": "json_object", "description": description, "required_fields": required, "non_empty_fields": non_empty}
    payload.update(kwargs)
    return payload


def oc(description: str, required: list[str], non_empty: list[str], *, mode: str = "model_output", template=None) -> dict:
    payload = {"enabled": True, "mode": mode, "format": "json_object", "description": description, "required_fields": required, "non_empty_fields": non_empty, "defaults_template": {}, "fallback_mode": "disabled"}
    if template is not None:
        payload["template"] = template
    return payload


def rules(input_contract: dict, output_contract: dict, *, input_transform=None) -> dict:
    return {"input_contract": input_contract, "input_transform": input_transform or {"enabled": False, "description": "", "template": None}, "output_contract": output_contract, "launch_profile": deepcopy(LAUNCH)}


def bundle(bot_id: str, name: str, role: str, prompt: str, backends: list[dict], routing_rules: dict, workflow: dict) -> dict:
    rr = dict(routing_rules)
    rr["workflow"] = workflow
    return {"schema_version": "nexusai.bot-export.v1", "exported_at": datetime.now(timezone.utc).isoformat(), "bot": {"id": bot_id, "name": name, "role": role, "priority": 2, "enabled": True, "system_prompt": prompt, "backends": backends, "routing_rules": rr, "workflow": workflow}, "connections": []}


def intake_fields() -> list[dict]:
    return [
        {"key": "topic", "label": "Topic", "required": True, "type": "text"},
        {"key": "subject", "label": "Subject", "required": True, "type": "text"},
        {"key": "scope", "label": "Scope", "required": True, "type": "textarea"},
        {"key": "audience", "label": "Audience", "required": True, "type": "text"},
        {"key": "level", "label": "Level", "required": True, "type": "select", "options": [{"label": "Beginner", "value": "Beginner"}, {"label": "Intermediate", "value": "Intermediate"}, {"label": "Advanced", "value": "Advanced"}]},
        {"key": "estimated_hours", "label": "Estimated Hours", "required": True, "type": "number"},
        {"key": "language", "label": "Language", "required": True, "type": "text"},
        {"key": "goals_json", "label": "Goals JSON", "required": True, "type": "textarea"},
        {"key": "units_json", "label": "Units JSON", "type": "textarea"},
        {"key": "constraints_json", "label": "Constraints JSON", "type": "textarea"},
        {"key": "preferred_voice", "label": "Preferred Voice", "type": "text"},
        {"key": "tone", "label": "Tone", "type": "textarea"},
        {"key": "tags_json", "label": "Tags JSON", "type": "textarea"},
        {"key": "notes", "label": "Notes", "type": "textarea"},
        {"key": "textbook_references_json", "label": "Textbook References JSON", "type": "textarea"},
        {"key": "product_references_json", "label": "Product References JSON", "type": "textarea"},
        {"key": "premium_features_json", "label": "Premium Features JSON", "type": "textarea"},
        {"key": "allowed_lesson_blocks_json", "label": "Allowed Lesson Blocks JSON", "type": "textarea"},
        {"key": "content_builder_concurrency", "label": "Content Builder Concurrency", "type": "number"},
        {"key": "assessment_settings_json", "label": "Assessment Settings JSON", "type": "textarea"},
        {"key": "badge_settings_json", "label": "Badge Settings JSON", "type": "textarea"},
        {"key": "question_bank_settings_json", "label": "Question Bank Settings JSON", "type": "textarea"},
        {"key": "generate_documentation", "label": "Generate Documentation", "type": "checkbox"},
        {"key": "import_connection_name", "label": "Import Connection Name", "type": "text"},
        {"key": "platform_import_actions_json", "label": "Platform Import Actions JSON", "type": "textarea"},
        {"key": "vault_namespaces_json", "label": "Vault Namespaces JSON", "type": "textarea"},
    ]


def intake_defaults() -> dict:
    return {
        "allowed_lesson_blocks_json": json.dumps(["AdvancedTitle", "AdvancedSubheader", "AdvancedParagraph", "AdvancedQuote", "AdvancedTable", "AdvancedFootnote", "image", "video"]),
        "assessment_settings_json": json.dumps({"quiz": {"easy": 3, "medium": 4, "hard": 2, "apply": 1, "timeLimitSec": None, "passThresholdPct": 70, "allowReview": True}, "test": {"easy": 6, "medium": 7, "hard": 5, "apply": 2, "timeLimitSec": None, "passThresholdPct": 70, "allowReview": False}}),
        "badge_settings_json": json.dumps({"enabled": True, "is_platform_wide": False, "badges": []}),
        "content_builder_concurrency": 4,
        "constraints_json": "[]",
        "generate_documentation": True,
        "platform_import_actions_json": "[]",
        "premium_features_json": json.dumps({"includeCapstone": False, "includeGradedProjects": False, "includeRubrics": True, "includeCharacterPack": False, "enableAiGrading": False}),
        "question_bank_settings_json": json.dumps({"easy": 10, "medium": 10, "hard": 10, "real_world_apply": 5, "total_questions": 35, "mcq_ratio": 0.3, "true_false_ratio": 0.2, "free_input_ratio": 0.5, "per_section": True}),
        "vault_namespaces_json": "[]",
        "workflow_type": "course_generation",
    }


def retry_payload() -> dict:
    return {"attempted_before": True, "qc_summary": "{{source_result.summary}}", "issues": "{{source_result.issues}}", "fix_instructions": "{{source_result.fix_instructions}}"}


def build() -> list[dict]:
    bots = []
    bots.append(bundle("course-intake", "Course Intake", "course-intake", "Normalize the launch payload into the configured output schema.", backend("gpt-oss:120b-cloud", 4096), rules(
        ic("Collect a complete course brief. Arrays and nested objects must be entered as JSON.", ["topic", "subject", "scope", "audience", "level", "estimated_hours", "language", "goals_json"], ["topic", "subject", "scope", "audience", "level", "language", "goals_json", "allowed_lesson_blocks_json", "assessment_settings_json", "question_bank_settings_json"], form_fields=intake_fields(), default_payload=intake_defaults()),
        oc("Deterministically normalize the raw course brief.", ["workflow_type", "course_brief", "generation_settings", "normalization_notes"], ["workflow_type", "course_brief.topic", "course_brief.subject", "course_brief.scope", "course_brief.audience", "course_brief.level", "course_brief.language", "course_brief.goals", "generation_settings.allowed_lesson_blocks", "generation_settings.assessment_settings", "generation_settings.question_bank_settings"], mode="payload_transform", template={"workflow_type": "{{payload.workflow_type}}", "course_brief": {"topic": "{{payload.topic}}", "subject": "{{payload.subject}}", "scope": "{{payload.scope}}", "audience": "{{payload.audience}}", "level": "{{payload.level}}", "estimated_hours": "{{payload.estimated_hours}}", "language": "{{payload.language}}", "goals": "{{json:payload.goals_json}}", "units": "{{json:payload.units_json}}", "constraints": "{{json:payload.constraints_json}}", "preferred_voice": "{{payload.preferred_voice}}", "tone": "{{payload.tone}}", "tags": "{{json:payload.tags_json}}", "notes": "{{payload.notes}}", "textbook_references": "{{json:payload.textbook_references_json}}", "product_references": "{{json:payload.product_references_json}}", "premium_features": "{{json:payload.premium_features_json}}"}, "generation_settings": {"allowed_lesson_blocks": "{{json:payload.allowed_lesson_blocks_json}}", "content_builder_concurrency": "{{payload.content_builder_concurrency}}", "assessment_settings": "{{json:payload.assessment_settings_json}}", "badge_settings": "{{json:payload.badge_settings_json}}", "question_bank_settings": "{{json:payload.question_bank_settings_json}}", "generate_documentation": "{{payload.generate_documentation}}", "import_connection_name": "{{payload.import_connection_name}}", "platform_import_actions": "{{json:payload.platform_import_actions_json}}", "vault_namespaces": "{{json:payload.vault_namespaces_json}}"}, "normalization_notes": []})),
        {"notes": "Normalize the course brief before any generative work begins.", "triggers": [trig("to-course-outline", "course-outline", payload_template={"workflow_type": "{{source_result.workflow_type}}", "course_brief": "{{source_result.course_brief}}", "generation_settings": "{{source_result.generation_settings}}", "revision_context": deepcopy(EMPTY_REVISION)})]}))

    bots.append(bundle("course-outline", "Course Outline", "course-outline", "Design the complete course shell and course outline. Return JSON only. Preserve provided unit intent and do not reduce scope.", backend("gpt-oss:120b-cloud", 8192), rules(
        ic("Build the course shell and the full unit outline from a normalized course brief.", ["workflow_type", "course_brief", "generation_settings", "revision_context"], ["workflow_type", "course_brief.topic", "course_brief.subject", "course_brief.scope", "course_brief.goals", "generation_settings.allowed_lesson_blocks"]),
        oc("Return the complete course shell and outline as strict JSON.", ["course_shell", "course_structure", "design_rationale"], ["course_shell.title", "course_shell.summary", "course_shell.goals", "course_structure.unit_count", "course_structure.units"])),
        {"notes": "Generate the course outline, then route it to outline QC.", "triggers": [trig("to-course-outline-qc", "course-outline-qc", payload_template={"workflow_type": "{{source_payload.workflow_type}}", "course_brief": "{{source_payload.course_brief}}", "generation_settings": "{{source_payload.generation_settings}}", "course_shell": "{{source_result.course_shell}}", "course_structure": "{{source_result.course_structure}}", "design_rationale": "{{source_result.design_rationale}}", "retry_payload": {"workflow_type": "{{source_payload.workflow_type}}", "course_brief": "{{source_payload.course_brief}}", "generation_settings": "{{source_payload.generation_settings}}", "revision_context": "{{source_payload.revision_context}}"}})]}))

    bots.append(bundle("course-outline-qc", "Course Outline QC", "course-outline-qc", "Validate one generated course shell and course outline. Fail shallow, generic, inconsistent, or incomplete structures. Return JSON only.", backend("gpt-oss:120b-cloud", 4096), rules(
        ic("Validate the generated course shell and outline before unit fan-out.", ["workflow_type", "course_brief", "generation_settings", "course_shell", "course_structure", "retry_payload"], ["course_shell.title", "course_structure.units", "retry_payload.course_brief.goals"]),
        oc("Return a real QC decision for the course outline.", ["qc_status", "summary", "issues", "fix_instructions", "approved_course_shell", "approved_units", "retry_target_bot_id"], ["qc_status", "summary", "approved_course_shell.title", "approved_units", "retry_target_bot_id"])),
        {"notes": "Pass fan-outs units; fail retries the outline with QC guidance.", "triggers": [trig("outline-pass-to-unit-builder", "course-unit-builder", result_field="qc_status", result_equals="pass", fan_out_field="source_result.approved_units", fan_out_alias="unit", fan_out_index_alias="unit_index", payload_template={"workflow_type": "{{source_payload.workflow_type}}", "course_brief": "{{source_payload.course_brief}}", "generation_settings": "{{source_payload.generation_settings}}", "course_shell": "{{source_result.approved_course_shell}}", "revision_context": deepcopy(EMPTY_REVISION)}), trig("outline-fail-retry", "{{result.retry_target_bot_id}}", result_field="qc_status", result_equals="fail", payload_template={"workflow_type": "{{source_payload.retry_payload.workflow_type}}", "course_brief": "{{source_payload.retry_payload.course_brief}}", "generation_settings": "{{source_payload.retry_payload.generation_settings}}", "revision_context": retry_payload()})]}))

    bots.append(bundle("course-unit-builder", "Unit Blueprint Builder", "course-unit-builder", "Design one unit blueprint at a time. Return JSON only. Do not write lesson bodies.", backend("qwen3.5:397b-cloud", 6144), rules(
        ic("Expand one approved outline unit into a strict unit blueprint.", ["workflow_type", "course_brief", "generation_settings", "course_shell", "unit", "revision_context"], ["course_shell.title", "unit.title", "unit.goals", "unit.lessons", "generation_settings.assessment_settings"]),
        oc("Return one unit blueprint with lesson plans and assessment targets.", ["unit_blueprint", "builder_notes"], ["unit_blueprint.unit_number", "unit_blueprint.title", "unit_blueprint.overview", "unit_blueprint.unit_goals", "unit_blueprint.lesson_plans", "unit_blueprint.assessment_plan.quiz", "unit_blueprint.assessment_plan.test"])),
        {"notes": "Fan out each unit blueprint into lesson writing tasks.", "triggers": [trig("unit-to-lesson-writer", "course-lesson-writer", fan_out_field="source_result.unit_blueprint.lesson_plans", fan_out_alias="lesson_plan", fan_out_index_alias="lesson_index", payload_template={"workflow_type": "{{source_payload.workflow_type}}", "course_brief": "{{source_payload.course_brief}}", "generation_settings": "{{source_payload.generation_settings}}", "course_shell": "{{source_payload.course_shell}}", "unit_blueprint": "{{source_result.unit_blueprint}}", "course_expected_unit_count": "{{source_payload.fanout_count}}", "revision_context": deepcopy(EMPTY_REVISION)})]}))

    bots.append(bundle("course-lesson-writer", "Lesson Writer", "course-lesson-writer", "Write one complete lesson at a time. Return JSON only. Use only allowed lesson block types and do not cut the lesson short.", backend("qwen3.5:397b-cloud", 12288), rules(
        ic("Write one complete lesson from a unit blueprint lesson plan.", ["workflow_type", "course_brief", "generation_settings", "course_shell", "unit_blueprint", "lesson_plan", "course_expected_unit_count", "revision_context"], ["course_shell.title", "unit_blueprint.title", "lesson_plan.title", "lesson_plan.objective", "generation_settings.allowed_lesson_blocks"]),
        oc("Return one fully written lesson with structured blocks and asset requests.", ["lesson_output"], ["lesson_output.unit_number", "lesson_output.lesson_number", "lesson_output.title", "lesson_output.objective", "lesson_output.blocks", "lesson_output.summary"])),
        {"notes": "Route each drafted lesson into lesson QC.", "triggers": [trig("lesson-to-qc", "course-lesson-qc", payload_template={"workflow_type": "{{source_payload.workflow_type}}", "course_brief": "{{source_payload.course_brief}}", "generation_settings": "{{source_payload.generation_settings}}", "course_shell": "{{source_payload.course_shell}}", "unit_blueprint": "{{source_payload.unit_blueprint}}", "lesson_plan": "{{source_payload.lesson_plan}}", "lesson_output": "{{source_result.lesson_output}}", "course_expected_unit_count": "{{source_payload.course_expected_unit_count}}", "unit_expected_lesson_count": "{{source_payload.fanout_count}}", "retry_payload": {"workflow_type": "{{source_payload.workflow_type}}", "course_brief": "{{source_payload.course_brief}}", "generation_settings": "{{source_payload.generation_settings}}", "course_shell": "{{source_payload.course_shell}}", "unit_blueprint": "{{source_payload.unit_blueprint}}", "lesson_plan": "{{source_payload.lesson_plan}}", "course_expected_unit_count": "{{source_payload.course_expected_unit_count}}", "revision_context": "{{source_payload.revision_context}}"}})]}))

    bots.append(bundle("course-lesson-qc", "Lesson QC", "course-lesson-qc", "Validate one written lesson. Fail shallow, invalid, or disallowed structures. Return JSON only.", backend("gpt-oss:120b-cloud", 4096), rules(
        ic("Validate a drafted lesson before asset planning.", ["workflow_type", "course_brief", "generation_settings", "course_shell", "unit_blueprint", "lesson_plan", "lesson_output", "course_expected_unit_count", "unit_expected_lesson_count", "retry_payload"], ["lesson_plan.title", "lesson_output.blocks", "lesson_output.summary", "generation_settings.allowed_lesson_blocks"]),
        oc("Return a real QC decision for one lesson.", ["qc_status", "summary", "issues", "fix_instructions", "approved_lesson_asset_package", "retry_target_bot_id"], ["qc_status", "summary", "approved_lesson_asset_package.lesson_ref.lesson_number", "approved_lesson_asset_package.lesson_output.blocks", "retry_target_bot_id"])),
        {"notes": "Passing advances to asset planning; failing retries the lesson writer.", "triggers": [trig("lesson-pass-to-image-planner", "course-image-planner", result_field="qc_status", result_equals="pass", payload_template={"workflow_type": "{{source_payload.workflow_type}}", "course_brief": "{{source_payload.course_brief}}", "generation_settings": "{{source_payload.generation_settings}}", "course_shell": "{{source_payload.course_shell}}", "unit_blueprint": "{{source_payload.unit_blueprint}}", "approved_lesson_asset_package": "{{source_result.approved_lesson_asset_package}}", "course_expected_unit_count": "{{source_payload.course_expected_unit_count}}", "unit_expected_lesson_count": "{{source_payload.unit_expected_lesson_count}}"}), trig("lesson-fail-retry", "{{result.retry_target_bot_id}}", result_field="qc_status", result_equals="fail", payload_template={"workflow_type": "{{source_payload.retry_payload.workflow_type}}", "course_brief": "{{source_payload.retry_payload.course_brief}}", "generation_settings": "{{source_payload.retry_payload.generation_settings}}", "course_shell": "{{source_payload.retry_payload.course_shell}}", "unit_blueprint": "{{source_payload.retry_payload.unit_blueprint}}", "lesson_plan": "{{source_payload.retry_payload.lesson_plan}}", "course_expected_unit_count": "{{source_payload.retry_payload.course_expected_unit_count}}", "revision_context": retry_payload()})]}))

    bots.append(bundle("course-image-planner", "Lesson Asset Planner", "course-image-planner", "Plan lesson assets for one approved lesson. Return JSON only. Prefer traceable sources and explicit licensing notes.", backend("glm-5:cloud", 4096), rules(
        ic("Plan lesson images and media for one approved lesson package.", ["workflow_type", "course_brief", "generation_settings", "course_shell", "unit_blueprint", "approved_lesson_asset_package", "course_expected_unit_count", "unit_expected_lesson_count"], ["approved_lesson_asset_package.lesson_ref.lesson_number", "approved_lesson_asset_package.lesson_output.blocks", "unit_blueprint.title"]),
        oc("Return one approved lesson delivery package that includes the asset plan.", ["approved_lesson_delivery_package", "warnings"], ["approved_lesson_delivery_package.lesson_ref.lesson_number", "approved_lesson_delivery_package.lesson_output.blocks", "approved_lesson_delivery_package.asset_plan.coverage_status"])),
        {"notes": "Join all approved lesson delivery packages for the unit before packaging.", "triggers": [trig("image-plan-to-unit-aggregator", "unit-lesson-aggregator", payload_template={"workflow_type": "{{source_payload.workflow_type}}", "course_brief": "{{source_payload.course_brief}}", "generation_settings": "{{source_payload.generation_settings}}", "course_shell": "{{source_payload.course_shell}}", "unit_blueprint": "{{source_payload.unit_blueprint}}", "course_expected_unit_count": "{{source_payload.course_expected_unit_count}}", "unit_expected_lesson_count": "{{source_payload.unit_expected_lesson_count}}"}, join_group_field="unit_blueprint.unit_number", join_expected_field="unit_expected_lesson_count", join_items_alias="lesson_delivery_tasks", join_result_field="source_result.approved_lesson_delivery_package", join_result_items_alias="approved_lesson_delivery_packages", join_sort_field="source_result.approved_lesson_delivery_package.lesson_ref.lesson_number")]}))

    bots.append(bundle("unit-lesson-aggregator", "Unit Lesson Aggregator", "unit-lesson-aggregator", "Package one complete unit deterministically from approved lesson delivery packages.", backend("gpt-oss:120b-cloud", 4096), rules(
        ic("Join all approved lesson delivery packages for one unit.", ["workflow_type", "course_brief", "generation_settings", "course_shell", "unit_blueprint", "course_expected_unit_count", "unit_expected_lesson_count", "approved_lesson_delivery_packages"], ["unit_blueprint.title", "approved_lesson_delivery_packages"]),
        oc("Deterministically assemble one complete unit package from the joined lesson delivery packages.", ["unit_package", "packaging_notes"], ["unit_package.unit_number", "unit_package.title", "unit_package.lessons", "unit_package.lesson_count"], mode="payload_transform", template={"unit_package": {"unit_number": "{{payload.unit_blueprint.unit_number}}", "title": "{{payload.unit_blueprint.title}}", "overview": "{{payload.unit_blueprint.overview}}", "unit_goals": "{{payload.unit_blueprint.unit_goals}}", "lessons": "{{payload.approved_lesson_delivery_packages}}", "lesson_count": "{{payload.join_count}}", "assessment_plan": "{{payload.unit_blueprint.assessment_plan}}", "sequencing_notes": "{{payload.unit_blueprint.sequencing_notes}}"}, "packaging_notes": []})),
        {"notes": "Forward each unit package to question-bank generation.", "triggers": [trig("unit-package-to-question-bank", "unit-question-bank", payload_template={"workflow_type": "{{source_payload.workflow_type}}", "course_brief": "{{source_payload.course_brief}}", "generation_settings": "{{source_payload.generation_settings}}", "course_shell": "{{source_payload.course_shell}}", "course_expected_unit_count": "{{source_payload.course_expected_unit_count}}", "unit_package": "{{source_result.unit_package}}", "revision_context": deepcopy(EMPTY_REVISION)})]}))

    bots.append(bundle("unit-question-bank", "Unit Question Bank", "unit-question-bank", "Generate one question bank for one completed unit package. Return JSON only and cover the unit broadly.", backend("gpt-oss:120b-cloud", 8192), rules(
        ic("Generate one unit question bank from one completed unit package.", ["workflow_type", "course_brief", "generation_settings", "course_shell", "course_expected_unit_count", "unit_package", "revision_context"], ["unit_package.title", "unit_package.lessons", "generation_settings.question_bank_settings"]),
        oc("Return one unit question bank with quiz and test blueprints.", ["unit_question_bank", "author_notes"], ["unit_question_bank.unit_number", "unit_question_bank.questions", "unit_question_bank.quiz_blueprint.question_refs", "unit_question_bank.test_blueprint.question_refs"])),
        {"notes": "Route the generated unit question bank into QC.", "triggers": [trig("unit-question-bank-to-qc", "unit-question-bank-qc", payload_template={"workflow_type": "{{source_payload.workflow_type}}", "course_brief": "{{source_payload.course_brief}}", "generation_settings": "{{source_payload.generation_settings}}", "course_shell": "{{source_payload.course_shell}}", "course_expected_unit_count": "{{source_payload.course_expected_unit_count}}", "unit_package": "{{source_payload.unit_package}}", "unit_question_bank": "{{source_result.unit_question_bank}}", "retry_payload": {"workflow_type": "{{source_payload.workflow_type}}", "course_brief": "{{source_payload.course_brief}}", "generation_settings": "{{source_payload.generation_settings}}", "course_shell": "{{source_payload.course_shell}}", "course_expected_unit_count": "{{source_payload.course_expected_unit_count}}", "unit_package": "{{source_payload.unit_package}}", "revision_context": "{{source_payload.revision_context}}"}})]}))

    bots.append(bundle("unit-question-bank-qc", "Unit Question Bank QC", "unit-question-bank-qc", "Validate one unit question bank. Fail weak coverage, correctness, difficulty distribution, or blueprint alignment. Return JSON only.", backend("gpt-oss:120b-cloud", 4096), rules(
        ic("Validate one unit question bank before course aggregation.", ["workflow_type", "course_brief", "generation_settings", "course_shell", "course_expected_unit_count", "unit_package", "unit_question_bank", "retry_payload"], ["unit_package.lessons", "unit_question_bank.questions", "retry_payload.unit_package.lessons"]),
        oc("Return a real QC decision for one unit package.", ["qc_status", "summary", "issues", "fix_instructions", "approved_unit_package", "retry_target_bot_id"], ["qc_status", "summary", "approved_unit_package.unit_package.lessons", "approved_unit_package.unit_question_bank.questions", "retry_target_bot_id"])),
        {"notes": "Passing contributes one approved unit package to the course join; failing retries question-bank generation.", "triggers": [trig("unit-pass-to-course-aggregator", "course-aggregator", result_field="qc_status", result_equals="pass", payload_template={"workflow_type": "{{source_payload.workflow_type}}", "course_brief": "{{source_payload.course_brief}}", "generation_settings": "{{source_payload.generation_settings}}", "course_shell": "{{source_payload.course_shell}}", "course_expected_unit_count": "{{source_payload.course_expected_unit_count}}"}, join_expected_field="course_expected_unit_count", join_items_alias="approved_unit_tasks", join_result_field="source_result.approved_unit_package", join_result_items_alias="approved_unit_packages", join_sort_field="source_result.approved_unit_package.unit_package.unit_number"), trig("unit-question-bank-fail-retry", "{{result.retry_target_bot_id}}", result_field="qc_status", result_equals="fail", payload_template={"workflow_type": "{{source_payload.retry_payload.workflow_type}}", "course_brief": "{{source_payload.retry_payload.course_brief}}", "generation_settings": "{{source_payload.retry_payload.generation_settings}}", "course_shell": "{{source_payload.retry_payload.course_shell}}", "course_expected_unit_count": "{{source_payload.retry_payload.course_expected_unit_count}}", "unit_package": "{{source_payload.retry_payload.unit_package}}", "revision_context": retry_payload()})]}))

    bots.append(bundle("course-aggregator", "Course Aggregator", "course-aggregator", "Package the complete course deterministically from approved unit packages.", backend("gpt-oss:120b-cloud", 4096), rules(
        ic("Join all approved unit packages into one complete course package.", ["workflow_type", "course_brief", "generation_settings", "course_shell", "course_expected_unit_count", "approved_unit_packages"], ["course_shell.title", "approved_unit_packages"]),
        oc("Deterministically assemble the course package from the joined unit packages.", ["course_package", "aggregation_notes"], ["course_package.course_shell.title", "course_package.units", "course_package.course_manifest.unit_count"], mode="payload_transform", template={"course_package": {"course_shell": "{{payload.course_shell}}", "units": "{{payload.approved_unit_packages}}", "course_manifest": {"workflow_type": "{{payload.workflow_type}}", "unit_count": "{{payload.join_count}}", "documentation_enabled": "{{payload.generation_settings.generate_documentation}}", "badge_enabled": "{{payload.generation_settings.badge_settings.enabled}}"}}, "aggregation_notes": []})),
        {"notes": "Send the assembled course package to badge design.", "triggers": [trig("course-package-to-badge-designer", "course-badge-designer", payload_template={"workflow_type": "{{source_payload.workflow_type}}", "course_brief": "{{source_payload.course_brief}}", "generation_settings": "{{source_payload.generation_settings}}", "course_package": "{{source_result.course_package}}", "revision_context": deepcopy(EMPTY_REVISION)})]}))

    bots.append(bundle("course-badge-designer", "Course Badge Designer", "course-badge-designer", "Design one course badge specification. Return JSON only. If badges are disabled, return an explicit skipped status.", backend("glm-5:cloud", 4096), rules(
        ic("Design one badge specification for the finished course package.", ["workflow_type", "course_brief", "generation_settings", "course_package", "revision_context"], ["course_package.course_shell.title", "generation_settings.badge_settings"]),
        oc("Return one badge specification or an explicit skipped status.", ["badge_status", "badge_spec", "warnings"], ["badge_status", "badge_spec.name", "badge_spec.image_prompt"])),
        {"notes": "Forward the badge specification into deterministic final packaging.", "triggers": [trig("badge-to-course-packager", "course-packager", payload_template={"workflow_type": "{{source_payload.workflow_type}}", "course_brief": "{{source_payload.course_brief}}", "generation_settings": "{{source_payload.generation_settings}}", "course_package": "{{source_payload.course_package}}", "badge_status": "{{source_result.badge_status}}", "badge_spec": "{{source_result.badge_spec}}", "badge_warnings": "{{source_result.warnings}}", "retry_payload": {"workflow_type": "{{source_payload.workflow_type}}", "course_brief": "{{source_payload.course_brief}}", "generation_settings": "{{source_payload.generation_settings}}", "course_package": "{{source_payload.course_package}}", "revision_context": "{{source_payload.revision_context}}"}})]}))

    bots.append(bundle("course-packager", "Course Packager", "course-packager", "Assemble the final delivery package deterministically from the approved course package and badge specification.", backend("gpt-oss:120b-cloud", 4096), rules(
        ic("Package the approved course package and badge specification into one delivery package.", ["workflow_type", "course_brief", "generation_settings", "course_package", "badge_status", "badge_spec", "badge_warnings", "retry_payload"], ["course_package.course_shell.title", "badge_status", "badge_spec.name"]),
        oc("Deterministically assemble the final delivery package for final QC and import execution.", ["delivery_package", "packaging_notes"], ["delivery_package.course_package.course_shell.title", "delivery_package.badge.badge_spec.name", "delivery_package.import_manifest.workflow_type"], mode="payload_transform", template={"delivery_package": {"course_package": "{{payload.course_package}}", "badge": {"badge_status": "{{payload.badge_status}}", "badge_spec": "{{payload.badge_spec}}", "warnings": "{{payload.badge_warnings}}"}, "import_manifest": {"workflow_type": "{{payload.workflow_type}}", "import_connection_name": "{{payload.generation_settings.import_connection_name}}", "platform_import_actions": "{{payload.generation_settings.platform_import_actions}}"}}, "packaging_notes": []})),
        {"notes": "Send the delivery package into final QC.", "triggers": [trig("to-course-final-qc", "course-final-qc", payload_template={"workflow_type": "{{source_payload.workflow_type}}", "course_brief": "{{source_payload.course_brief}}", "generation_settings": "{{source_payload.generation_settings}}", "delivery_package": "{{source_result.delivery_package}}", "retry_payload": "{{source_payload.retry_payload}}"})]}))

    bots.append(bundle("course-final-qc", "Course Final QC", "course-final-qc", "Validate the final delivery package for import readiness. Return JSON only. If platform import actions are missing or empty, the review must fail.", backend("gpt-oss:120b-cloud", 4096), rules(
        ic("Validate the final delivery package before import execution.", ["workflow_type", "course_brief", "generation_settings", "delivery_package", "retry_payload"], ["delivery_package.course_package.course_shell.title", "delivery_package.badge.badge_spec.name", "delivery_package.import_manifest.workflow_type"]),
        oc("Return a real QC decision for the final delivery package.", ["qc_status", "summary", "issues", "fix_instructions", "approved_delivery_package"], ["qc_status", "summary", "approved_delivery_package.course_package.course_shell.title"])),
        {"notes": "Only a passing final QC may execute the import adapter.", "triggers": [trig("final-pass-to-importer", "course-globeiq-importer", result_field="qc_status", result_equals="pass", payload_template={"workflow_type": "{{source_payload.workflow_type}}", "generation_settings": "{{source_payload.generation_settings}}", "delivery_package": "{{source_result.approved_delivery_package}}", "import_connection_name": "{{source_payload.generation_settings.import_connection_name}}"})]}))

    bots.append(bundle("course-globeiq-importer", "Course Importer", "course-importer", "Execute the configured platform import actions deterministically.", custom_backend("http_connection"), rules(
        ic("Execute the configured platform import actions against one attached HTTP connection.", ["workflow_type", "generation_settings", "delivery_package", "import_connection_name"], ["workflow_type", "delivery_package.course_package.course_shell.title", "import_connection_name", "generation_settings.platform_import_actions"]),
        oc("Return the actual import execution result from the configured HTTP connection actions.", ["import_status", "connection_name", "completed_actions", "failed_actions", "action_results", "warnings", "errors"], ["import_status", "connection_name", "action_results"]),
        input_transform={"enabled": True, "description": "Render the configured platform import action templates into concrete HTTP actions.", "template": {"connection": {"name": "{{payload.import_connection_name}}"}, "continue_on_error": False, "connection_actions": "{{render:payload.generation_settings.platform_import_actions}}"}}),
        {"notes": "Import execution is terminal.", "triggers": []}))
    return bots


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest = {"schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(), "bots": []}
    for item in build():
        bot = item["bot"]
        file_name = f"{bot['id']}.bot.json"
        (OUT_DIR / file_name).write_text(json.dumps(item, indent=2, sort_keys=True), encoding="utf-8")
        manifest["bots"].append({"id": bot["id"], "name": bot["name"], "role": bot["role"], "file": file_name})
    (OUT_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")


if __name__ == "__main__":
    main()
