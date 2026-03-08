from __future__ import annotations

from copy import deepcopy
from typing import Any


def _clone_backends(source_bot: dict[str, Any] | None) -> list[dict[str, Any]]:
    backends = source_bot.get("backends") if isinstance(source_bot, dict) else []
    if not isinstance(backends, list):
        return []
    return [deepcopy(backend) for backend in backends if isinstance(backend, dict)]


def _source_hint(source_bot: dict[str, Any] | None) -> str:
    if not isinstance(source_bot, dict):
        return ""
    return str(source_bot.get("name") or source_bot.get("id") or "").strip()


def _bot(
    *,
    project_id: str,
    suffix: str,
    name: str,
    role: str,
    prompt: str,
    backends: list[dict[str, Any]],
    workflow: dict[str, Any] | None = None,
    priority: int = 100,
) -> dict[str, Any]:
    return {
        "id": f"{project_id}-{suffix}",
        "name": name,
        "role": role,
        "priority": priority,
        "enabled": True,
        "system_prompt": prompt.strip(),
        "backends": deepcopy(backends),
        "routing_rules": {"project_template": True, "project_id": project_id},
        "workflow": workflow or {"triggers": []},
    }


def _pass_trigger(trigger_id: str, target_bot_id: str, *, title: str) -> dict[str, Any]:
    return {
        "id": trigger_id,
        "title": title,
        "event": "task_completed",
        "target_bot_id": target_bot_id,
        "enabled": True,
        "condition": "has_result",
        "result_field": "qc_status",
        "result_equals": "pass",
        "inherit_metadata": True,
    }


def _fail_trigger(trigger_id: str, *, title: str) -> dict[str, Any]:
    return {
        "id": trigger_id,
        "title": title,
        "event": "task_completed",
        "target_bot_id": "{{source_bot_id}}",
        "enabled": True,
        "condition": "has_result",
        "result_field": "qc_status",
        "result_equals": "fail",
        "inherit_metadata": True,
    }


def build_course_generation_template(project_id: str, source_bot: dict[str, Any] | None) -> dict[str, Any]:
    backends = _clone_backends(source_bot)
    source_hint = _source_hint(source_bot)
    source_line = f"Base runtime inherited from source bot: {source_hint}." if source_hint else ""

    shell_id = f"{project_id}-course-shell-designer"
    shell_qc_id = f"{project_id}-course-shell-qc"
    structure_id = f"{project_id}-course-structure-architect"
    structure_qc_id = f"{project_id}-course-structure-qc"
    unit_id = f"{project_id}-course-unit-designer"
    unit_qc_id = f"{project_id}-course-unit-qc"
    lesson_id = f"{project_id}-course-lesson-writer"
    lesson_qc_id = f"{project_id}-course-lesson-qc"
    image_id = f"{project_id}-course-image-researcher"
    image_qc_id = f"{project_id}-course-image-qc"
    question_id = f"{project_id}-course-question-bank"
    question_qc_id = f"{project_id}-course-question-qc"
    badge_id = f"{project_id}-course-badge-designer"
    badge_qc_id = f"{project_id}-course-badge-qc"
    finish_id = f"{project_id}-course-finisher"

    bots = [
        _bot(
            project_id=project_id,
            suffix="course-shell-designer",
            name="Course Shell Designer",
            role="course_orchestrator",
            priority=10,
            backends=backends,
            prompt=f"""
You design the top-level course brief into a production-ready course shell.
Input should describe topic, learner audience, level, scope, delivery constraints, premium flags, and any textbook or curriculum references.
Output JSON only with:
- course_title
- short_description
- long_description
- audience
- level
- estimated_hours
- learning_outcomes
- prerequisites
- course_metadata
- qc_ready_summary
- report
The report field must be a concise markdown summary of what you designed and the assumptions you made.
Do not create units or lessons yet. {source_line}
""",
            workflow={"triggers": [{"id": "course-shell-to-qc", "title": "QC course shell", "event": "task_completed", "target_bot_id": shell_qc_id, "enabled": True, "condition": "has_result", "inherit_metadata": True}]},
        ),
        _bot(
            project_id=project_id,
            suffix="course-shell-qc",
            name="Course Shell QC",
            role="quality_control",
            priority=11,
            backends=backends,
            prompt="""
You quality-check course shell output.
Return JSON only with:
- qc_status: pass or fail
- issues: array
- strengths: array
- fixes_required: array
- report: markdown summary
Pass only if the shell is coherent, aligned to audience/level, and specific enough for downstream structure design.
""",
            workflow={"triggers": [
                _pass_trigger("course-shell-pass", structure_id, title="Proceed to structure"),
                _fail_trigger("course-shell-fail", title="Return shell for revision"),
            ]},
        ),
        _bot(
            project_id=project_id,
            suffix="course-structure-architect",
            name="Course Structure Architect",
            role="course_structure",
            priority=20,
            backends=backends,
            prompt="""
You convert a validated course shell into a course structure.
Output JSON only with:
- units: array of units with titles, goals, lesson_count, lesson_types
- sequencing_rationale
- pacing_notes
- assessment_strategy
- report
The structure must support end-to-end course generation and downstream lesson writing.
""",
            workflow={"triggers": [{"id": "course-structure-to-qc", "title": "QC structure", "event": "task_completed", "target_bot_id": structure_qc_id, "enabled": True, "condition": "has_result", "inherit_metadata": True}]},
        ),
        _bot(
            project_id=project_id,
            suffix="course-structure-qc",
            name="Course Structure QC",
            role="quality_control",
            priority=21,
            backends=backends,
            prompt="""
You quality-check course structure output.
Return JSON only with qc_status, issues, strengths, fixes_required, and report.
Pass only if unit sequencing, coverage, and lesson distribution are coherent.
""",
            workflow={"triggers": [
                _pass_trigger("course-structure-pass", unit_id, title="Proceed to unit design"),
                _fail_trigger("course-structure-fail", title="Return structure for revision"),
            ]},
        ),
        _bot(
            project_id=project_id,
            suffix="course-unit-designer",
            name="Course Unit Designer",
            role="course_units",
            priority=30,
            backends=backends,
            prompt="""
You expand the course structure into fully specified unit packages.
Output JSON only with:
- units: array with objectives, vocabulary, required concepts, lesson briefs, dependencies
- spiral_learning_notes
- assessment_hooks
- report
Prepare enough structure for each lesson to be written independently.
""",
            workflow={"triggers": [{"id": "course-units-to-qc", "title": "QC units", "event": "task_completed", "target_bot_id": unit_qc_id, "enabled": True, "condition": "has_result", "inherit_metadata": True}]},
        ),
        _bot(
            project_id=project_id,
            suffix="course-unit-qc",
            name="Course Unit QC",
            role="quality_control",
            priority=31,
            backends=backends,
            prompt="""
You quality-check unit packages.
Return JSON only with qc_status, issues, strengths, fixes_required, and report.
Pass only if unit objectives, lesson briefs, dependencies, and assessment hooks are complete and internally consistent.
""",
            workflow={"triggers": [
                _pass_trigger("course-unit-pass", lesson_id, title="Proceed to lesson writing"),
                _fail_trigger("course-unit-fail", title="Return unit design for revision"),
            ]},
        ),
        _bot(
            project_id=project_id,
            suffix="course-lesson-writer",
            name="Course Lesson Writer",
            role="course_lessons",
            priority=40,
            backends=backends,
            prompt="""
You write every lesson in the course.
Output JSON only with:
- lessons: array with title, unit_title, objectives, outline, teaching_blocks, activities, checks_for_understanding, homework_or_extension, image_needs
- lesson_generation_notes
- report
Each lesson must be individually shippable and detailed enough for later import into GlobeIQ or a lesson builder.
""",
            workflow={"triggers": [{"id": "course-lessons-to-qc", "title": "QC lessons", "event": "task_completed", "target_bot_id": lesson_qc_id, "enabled": True, "condition": "has_result", "inherit_metadata": True}]},
        ),
        _bot(
            project_id=project_id,
            suffix="course-lesson-qc",
            name="Course Lesson QC",
            role="quality_control",
            priority=41,
            backends=backends,
            prompt="""
You quality-check generated lessons.
Return JSON only with qc_status, issues, strengths, fixes_required, and report.
Pass only if the lessons are accurate, paced correctly, and ready for assets/question-bank generation.
""",
            workflow={"triggers": [
                _pass_trigger("course-lesson-pass", image_id, title="Proceed to image research"),
                _fail_trigger("course-lesson-fail", title="Return lesson writing for revision"),
            ]},
        ),
        _bot(
            project_id=project_id,
            suffix="course-image-researcher",
            name="Course Image Researcher",
            role="course_assets",
            priority=50,
            backends=backends,
            prompt="""
You create a course image plan instead of hand-waving visual needs.
Output JSON only with:
- course_cover_image
- lesson_images: array with lesson title, image purpose, search brief, alt_text, citation_or_source_plan, generation_prompt
- question_bank_images: array with question reference and visual brief
- asset_manifest
- report
Prioritize educational clarity and reusable prompts/specs over generic stock-photo language.
""",
            workflow={"triggers": [{"id": "course-images-to-qc", "title": "QC images", "event": "task_completed", "target_bot_id": image_qc_id, "enabled": True, "condition": "has_result", "inherit_metadata": True}]},
        ),
        _bot(
            project_id=project_id,
            suffix="course-image-qc",
            name="Course Image QC",
            role="quality_control",
            priority=51,
            backends=backends,
            prompt="""
You quality-check course image plans.
Return JSON only with qc_status, issues, strengths, fixes_required, and report.
Pass only if the asset plan is specific, educationally relevant, and grounded enough for actual sourcing or generation.
""",
            workflow={"triggers": [
                _pass_trigger("course-image-pass", question_id, title="Proceed to question bank"),
                _fail_trigger("course-image-fail", title="Return image planning for revision"),
            ]},
        ),
        _bot(
            project_id=project_id,
            suffix="course-question-bank",
            name="Course Question Bank Builder",
            role="course_assessment",
            priority=60,
            backends=backends,
            prompt="""
You build the course question bank.
Output JSON only with:
- questions: array with prompt, answer, distractors, explanation, difficulty, lesson_reference, includes_image, image_brief_if_needed
- coverage_map
- answer_key
- report
Include image-backed questions where visuals materially improve understanding.
""",
            workflow={"triggers": [{"id": "course-questions-to-qc", "title": "QC question bank", "event": "task_completed", "target_bot_id": question_qc_id, "enabled": True, "condition": "has_result", "inherit_metadata": True}]},
        ),
        _bot(
            project_id=project_id,
            suffix="course-question-qc",
            name="Course Question QC",
            role="quality_control",
            priority=61,
            backends=backends,
            prompt="""
You quality-check the course question bank.
Return JSON only with qc_status, issues, strengths, fixes_required, and report.
Pass only if the questions are accurate, aligned to lessons, and include correct answers/explanations.
""",
            workflow={"triggers": [
                _pass_trigger("course-question-pass", badge_id, title="Proceed to badge design"),
                _fail_trigger("course-question-fail", title="Return question bank for revision"),
            ]},
        ),
        _bot(
            project_id=project_id,
            suffix="course-badge-designer",
            name="Course Badge Designer",
            role="course_badges",
            priority=70,
            backends=backends,
            prompt="""
You define badge and completion mark assets for the course.
Output JSON only with:
- badges: array with badge_name, usage, concept, visual_direction, svg_or_graphic_prompt, color_system
- completion_certificate_mark
- lesson_badge_system
- report
Design for consistency with the course theme and with reusable asset-generation guidance.
""",
            workflow={"triggers": [{"id": "course-badges-to-qc", "title": "QC badges", "event": "task_completed", "target_bot_id": badge_qc_id, "enabled": True, "condition": "has_result", "inherit_metadata": True}]},
        ),
        _bot(
            project_id=project_id,
            suffix="course-badge-qc",
            name="Course Badge QC",
            role="quality_control",
            priority=71,
            backends=backends,
            prompt="""
You quality-check badge and completion asset specs.
Return JSON only with qc_status, issues, strengths, fixes_required, and report.
Pass only if the asset specifications are coherent, brandable, and specific enough for generation.
""",
            workflow={"triggers": [
                _pass_trigger("course-badge-pass", finish_id, title="Proceed to course finalizer"),
                _fail_trigger("course-badge-fail", title="Return badge design for revision"),
            ]},
        ),
        _bot(
            project_id=project_id,
            suffix="course-finisher",
            name="Course Package Finisher",
            role="course_finisher",
            priority=80,
            backends=backends,
            prompt="""
You assemble the full course package from upstream stage outputs.
Output JSON only with:
- course_package_summary
- publish_checklist
- missing_items
- artifacts: array of file-style deliverables or report items
- report
The report must summarize the entire pipeline outcome, remaining gaps, and whether the course is ready for import or human review.
""",
        ),
    ]
    return {
        "template_id": "course_generation_pipeline",
        "display_name": "Course Generation Pipeline",
        "description": "End-to-end course shell, structure, units, lessons, images, question bank, badges, and final packaging with QC at every major step.",
        "bots": bots,
        "entry_bot_id": shell_id,
    }


def build_pr_review_template(project_id: str, source_bot: dict[str, Any] | None) -> dict[str, Any]:
    backends = _clone_backends(source_bot)
    source_hint = _source_hint(source_bot)
    source_line = f"Base runtime inherited from source bot: {source_hint}." if source_hint else ""
    triage_id = f"{project_id}-pr-triage"
    review_id = f"{project_id}-pr-reviewer"
    qc_id = f"{project_id}-pr-review-qc"
    report_id = f"{project_id}-pr-reporter"

    bots = [
        _bot(
            project_id=project_id,
            suffix="pr-triage",
            name="PR Triage Bot",
            role="pr_triage",
            priority=90,
            backends=backends,
            prompt=f"""
You triage pull request work into a review plan.
Input may include PR metadata, diff context, issue links, and review goals.
Output JSON only with:
- risk_summary
- review_plan
- hotspots
- required_checks
- report
Your report should explain what the downstream PR reviewer must inspect. {source_line}
""",
            workflow={"triggers": [{"id": "pr-triage-to-review", "title": "Run PR review", "event": "task_completed", "target_bot_id": review_id, "enabled": True, "condition": "has_result", "inherit_metadata": True}]},
        ),
        _bot(
            project_id=project_id,
            suffix="pr-reviewer",
            name="PR Reviewer Bot",
            role="pr_reviewer",
            priority=91,
            backends=backends,
            prompt="""
You conduct a true PR review.
Output JSON only with:
- summary
- findings: array with severity, file, line, title, explanation
- open_questions
- follow_up_tasks
- qc_ready_summary
- report
Review for correctness, regressions, missing tests, risky migrations, broken orchestration, and deployment hazards.
""",
            workflow={"triggers": [{"id": "pr-review-to-qc", "title": "QC PR review", "event": "task_completed", "target_bot_id": qc_id, "enabled": True, "condition": "has_result", "inherit_metadata": True}]},
        ),
        _bot(
            project_id=project_id,
            suffix="pr-review-qc",
            name="PR Review QC",
            role="quality_control",
            priority=92,
            backends=backends,
            prompt="""
You quality-check PR reviews.
Return JSON only with qc_status, issues, strengths, fixes_required, and report.
Pass only if findings are evidence-based, severity-ranked, and actionable.
""",
            workflow={"triggers": [
                _pass_trigger("pr-review-pass", report_id, title="Publish PR report"),
                _fail_trigger("pr-review-fail", title="Return PR review for revision"),
            ]},
        ),
        _bot(
            project_id=project_id,
            suffix="pr-reporter",
            name="PR Report Bot",
            role="pr_reporting",
            priority=93,
            backends=backends,
            prompt="""
You convert PR review output into an operator-ready report.
Output JSON only with:
- executive_summary
- blocking_findings
- non_blocking_findings
- recommended_actions
- artifacts: array of report-style outputs
- report
The report should be concise, structured, and suitable for handing back to a human operator.
""",
        ),
    ]
    return {
        "template_id": "pr_review_pipeline",
        "display_name": "PR Review Pipeline",
        "description": "PR triage, deep review, QC, and final human-readable report generation.",
        "bots": bots,
        "entry_bot_id": triage_id,
    }


def available_workflow_templates(project_id: str, source_bot: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [
        build_course_generation_template(project_id, source_bot),
        build_pr_review_template(project_id, source_bot),
    ]

