import json
from unittest.mock import AsyncMock

import pytest

from control_plane.chat.pm_orchestrator import PMOrchestrator
from shared.exceptions import BotNotFoundError
from shared.models import Bot, Task, TaskMetadata


def _bot(*, bot_id: str, name: str, role: str, priority: int = 0, enabled: bool = True) -> Bot:
    return Bot(
        id=bot_id,
        name=name,
        role=role,
        backends=[],
        priority=priority,
        enabled=enabled,
    )


def test_pick_target_bot_avoids_media_planner_for_researcher_role() -> None:
    orchestrator = PMOrchestrator(bot_registry=None, scheduler=None, task_manager=None, chat_manager=None)
    bots = [
        _bot(bot_id="pm-main", name="PM Main", role="project-manager", priority=50),
        _bot(bot_id="course-image-planner", name="Course Image Planner", role="planner", priority=100),
        _bot(bot_id="pm-coder", name="PM Coder", role="coder", priority=80),
    ]

    picked = orchestrator._pick_target_bot(bots, role_hint="researcher", pm_bot_id="pm-main")
    assert picked.id == "pm-coder"


def test_pick_target_bot_still_selects_research_bot_when_available() -> None:
    orchestrator = PMOrchestrator(bot_registry=None, scheduler=None, task_manager=None, chat_manager=None)
    bots = [
        _bot(bot_id="pm-main", name="PM Main", role="project-manager", priority=50),
        _bot(bot_id="research-bot", name="Requirements Analyst", role="researcher", priority=40),
        _bot(bot_id="course-image-planner", name="Course Image Planner", role="planner", priority=100),
    ]

    picked = orchestrator._pick_target_bot(bots, role_hint="researcher", pm_bot_id="pm-main")
    assert picked.id == "research-bot"


def test_pick_target_bot_avoids_media_planner_for_planning_role() -> None:
    orchestrator = PMOrchestrator(bot_registry=None, scheduler=None, task_manager=None, chat_manager=None)
    bots = [
        _bot(bot_id="pm-main", name="PM Main", role="project-manager", priority=50),
        _bot(bot_id="course-image-planner", name="Course Image Planner", role="planner", priority=100),
        _bot(bot_id="pm-coder", name="PM Coder", role="coder", priority=80),
    ]

    picked = orchestrator._pick_target_bot(bots, role_hint="planner", pm_bot_id="pm-main")
    assert picked.id == "pm-coder"


def test_pick_target_bot_prefers_exact_role_match_over_pattern_match() -> None:
    """Ensure pm-coder (role='coder') is selected over pm-database-engineer (role='dba-sql') for coder role."""
    orchestrator = PMOrchestrator(bot_registry=None, scheduler=None, task_manager=None, chat_manager=None)
    bots = [
        _bot(bot_id="pm-main", name="PM Main", role="project-manager", priority=50),
        _bot(bot_id="pm-database-engineer", name="PM Database Engineer", role="dba-sql", priority=76),
        _bot(bot_id="pm-coder", name="PM Coder", role="coder", priority=85),
    ]

    # 'coder' role hint should match pm-coder exactly, not pm-database-engineer
    picked = orchestrator._pick_target_bot(bots, role_hint="coder", pm_bot_id="pm-main")
    assert picked.id == "pm-coder"
    assert picked.role == "coder"


def test_get_bot_by_id_returns_exact_bot() -> None:
    """Ensure _get_bot_by_id returns the exact bot when ID matches."""
    orchestrator = PMOrchestrator(bot_registry=None, scheduler=None, task_manager=None, chat_manager=None)
    bots = [
        _bot(bot_id="pm-main", name="PM Main", role="project-manager", priority=50),
        _bot(bot_id="pm-coder", name="PM Coder", role="coder", priority=85),
        _bot(bot_id="pm-tester", name="PM Tester", role="tester", priority=80),
    ]

    # Should return exact bot by ID
    picked = orchestrator._get_bot_by_id(bots, "pm-tester")
    assert picked is not None
    assert picked.id == "pm-tester"
    assert picked.name == "PM Tester"


def test_get_bot_by_id_returns_none_when_not_found() -> None:
    """Ensure _get_bot_by_id returns None when bot ID doesn't exist."""
    orchestrator = PMOrchestrator(bot_registry=None, scheduler=None, task_manager=None, chat_manager=None)
    bots = [
        _bot(bot_id="pm-main", name="PM Main", role="project-manager", priority=50),
        _bot(bot_id="pm-coder", name="PM Coder", role="coder", priority=85),
    ]

    # Should return None for non-existent bot
    picked = orchestrator._get_bot_by_id(bots, "nonexistent-bot")
    assert picked is None


def test_pick_target_bot_excludes_database_bots_for_coder_role() -> None:
    """Even without exact role match, database bots should not be selected for coder role."""
    orchestrator = PMOrchestrator(bot_registry=None, scheduler=None, task_manager=None, chat_manager=None)
    bots = [
        _bot(bot_id="pm-main", name="PM Main", role="project-manager", priority=50),
        _bot(bot_id="pm-database-engineer", name="PM Database Engineer", role="dba-sql", priority=100),
        _bot(bot_id="dev-bot", name="Dev Bot", role="developer", priority=80),
    ]

    # Should prefer dev-bot over higher-priority database engineer
    picked = orchestrator._pick_target_bot(bots, role_hint="coder", pm_bot_id="pm-main")
    assert picked.id == "dev-bot"
    assert "database" not in picked.name.lower()


def test_pick_target_bot_selects_database_engineer_for_dba_role() -> None:
    """Database engineer should be selected for dba role."""
    orchestrator = PMOrchestrator(bot_registry=None, scheduler=None, task_manager=None, chat_manager=None)
    bots = [
        _bot(bot_id="pm-main", name="PM Main", role="project-manager", priority=50),
        _bot(bot_id="pm-database-engineer", name="PM Database Engineer", role="dba-sql", priority=76),
        _bot(bot_id="pm-coder", name="PM Coder", role="coder", priority=85),
    ]

    picked = orchestrator._pick_target_bot(bots, role_hint="dba", pm_bot_id="pm-main")
    assert picked.id == "pm-database-engineer"


def test_truncation_hint_detects_finish_reason_length() -> None:
    orchestrator = PMOrchestrator(bot_registry=None, scheduler=None, task_manager=None, chat_manager=None)
    hint = orchestrator._truncation_hint(
        {
            "output": "partial output",
            "usage": {"completion_tokens": 4096},
            "finish_reason": "length",
        }
    )
    assert "token limit" in hint.lower()


def test_truncation_hint_ignores_high_completion_tokens_without_finish_reason() -> None:
    """High completion_tokens alone should NOT trigger truncation hint.
    
    Only explicit finish_reason (length, max_tokens, etc.) indicates truncation.
    Models can legitimately produce long outputs within their max_tokens budget.
    """
    orchestrator = PMOrchestrator(bot_registry=None, scheduler=None, task_manager=None, chat_manager=None)
    # High completion_tokens without finish_reason = valid long output, NOT truncated
    hint = orchestrator._truncation_hint(
        {
            "output": "This is a complete, long output that ended naturally.",
            "usage": {"completion_tokens": 50000},  # Well above old 4096 threshold
        }
    )
    assert hint == ""  # No truncation hint - output is complete


def test_normalize_step_kind_infers_repo_change_from_deliverable_paths() -> None:
    orchestrator = PMOrchestrator(bot_registry=None, scheduler=None, task_manager=None, chat_manager=None)

    step_kind = orchestrator._normalize_step_kind(
        "",
        title="Build lesson blocks",
        instruction="Implement the feature",
        role_hint="coder",
        deliverables=["src/lesson_blocks/math_block.py", "tests/lesson_blocks/test_math_block.py"],
    )

    assert step_kind == "repo_change"


def test_parse_plan_json_backfills_step_kind_and_evidence_requirements() -> None:
    orchestrator = PMOrchestrator(bot_registry=None, scheduler=None, task_manager=None, chat_manager=None)

    parsed = orchestrator._parse_plan_json(
        """
        {
          "steps": [
            {
              "id": "step_1",
              "title": "Run tests",
              "instruction": "Execute pytest and report coverage",
              "role_hint": "tester",
              "deliverables": ["coverage.xml"]
            }
          ]
        }
        """
    )

    assert parsed is not None
    assert parsed["steps"][0]["step_kind"] == "test_execution"
    assert parsed["steps"][0]["evidence_requirements"]
    assert "Executed test command output" in parsed["steps"][0]["evidence_requirements"][0]


def test_parse_plan_json_preserves_explicit_bot_id() -> None:
    orchestrator = PMOrchestrator(bot_registry=None, scheduler=None, task_manager=None, chat_manager=None)

    parsed = orchestrator._parse_plan_json(
        """
        {
          "steps": [
            {
              "id": "step_1",
              "title": "Implement feature",
              "instruction": "Update the code path",
              "bot_id": "pm-coder",
              "role_hint": "coder",
              "deliverables": ["src/app.py"]
            }
          ]
        }
        """
    )

    assert parsed is not None
    assert parsed["steps"][0]["bot_id"] == "pm-coder"


def test_heuristic_plan_prefers_standard_pm_bot_ids_when_present() -> None:
    orchestrator = PMOrchestrator(bot_registry=None, scheduler=None, task_manager=None, chat_manager=None)
    bots = [
        _bot(bot_id="pm-orchestrator", name="PM Orchestrator", role="pm", priority=100),
        _bot(bot_id="pm-research-analyst", name="PM Research Analyst", role="researcher", priority=70),
        _bot(bot_id="pm-coder", name="PM Coder", role="coder", priority=85),
        _bot(bot_id="pm-tester", name="PM Tester", role="tester", priority=80),
        _bot(bot_id="pm-security-reviewer", name="PM Security Reviewer", role="security-reviewer", priority=78),
    ]

    plan = orchestrator._heuristic_plan("Implement a feature", bots)

    assert plan["steps"][0]["bot_id"] == "pm-research-analyst"
    assert plan["steps"][1]["bot_id"] == "pm-coder"
    assert plan["steps"][2]["bot_id"] == "pm-tester"
    assert plan["steps"][3]["bot_id"] == "pm-security-reviewer"


@pytest.mark.anyio
async def test_build_plan_falls_back_when_llm_starts_with_engineer_and_research_bot_exists() -> None:
    scheduler = type(
        "Scheduler",
        (),
        {
            "schedule": AsyncMock(
                return_value={
                    "output": json.dumps(
                        {
                            "steps": [
                                {
                                    "id": "step_1",
                                    "title": "Design architecture",
                                    "instruction": "Plan the implementation.",
                                    "bot_id": "pm-engineer",
                                    "role_hint": "engineer",
                                    "step_kind": "planning",
                                    "deliverables": ["docs/design.md"],
                                },
                                {
                                    "id": "step_2",
                                    "title": "Implement code",
                                    "instruction": "Write the feature.",
                                    "bot_id": "pm-coder",
                                    "role_hint": "coder",
                                    "step_kind": "repo_change",
                                    "deliverables": ["src/feature.cs"],
                                },
                            ]
                        }
                    )
                }
            )
        },
    )()
    bots = [
        _bot(bot_id="pm-orchestrator", name="PM Orchestrator", role="pm", priority=100),
        _bot(bot_id="pm-research-analyst", name="PM Research Analyst", role="researcher", priority=70),
        _bot(bot_id="pm-engineer", name="PM Engineer", role="engineer", priority=82),
        _bot(bot_id="pm-coder", name="PM Coder", role="coder", priority=85),
    ]
    orchestrator = PMOrchestrator(bot_registry=None, scheduler=scheduler, task_manager=None, chat_manager=None)

    plan = await orchestrator._build_plan(
        instruction="Build lesson blocks",
        pm_bot_id="pm-orchestrator",
        bots=bots,
        context_items=[],
    )

    assert plan["steps"][0]["bot_id"] == "pm-research-analyst"


@pytest.mark.anyio
async def test_build_plan_uses_fixed_standard_pm_sequence_when_pack_is_present() -> None:
    scheduler = type(
        "Scheduler",
        (),
        {
            "schedule": AsyncMock(
                return_value={
                    "output": json.dumps(
                        {
                            "steps": [
                                {
                                    "id": "step_1",
                                    "title": "Wrong start",
                                    "instruction": "Do implementation first.",
                                    "bot_id": "pm-coder",
                                    "role_hint": "coder",
                                    "step_kind": "repo_change",
                                    "deliverables": ["src/feature.cs"],
                                }
                            ]
                        }
                    )
                }
            )
        },
    )()
    bots = [
        _bot(bot_id="pm-orchestrator", name="PM Orchestrator", role="pm", priority=100),
        _bot(bot_id="pm-research-analyst", name="PM Research Analyst", role="researcher", priority=70),
        _bot(bot_id="pm-engineer", name="PM Engineer", role="engineer", priority=82),
        _bot(bot_id="pm-coder", name="PM Coder", role="coder", priority=85),
        _bot(bot_id="pm-tester", name="PM Tester", role="tester", priority=80),
        _bot(bot_id="pm-security-reviewer", name="PM Security Reviewer", role="security-reviewer", priority=78),
        _bot(bot_id="pm-database-engineer", name="PM Database Engineer", role="dba-sql", priority=76),
        _bot(bot_id="pm-ui-tester", name="PM UI Tester", role="ui-tester", priority=77),
        _bot(bot_id="pm-final-qc", name="PM Final QC", role="final-qc", priority=90),
    ]
    orchestrator = PMOrchestrator(bot_registry=None, scheduler=scheduler, task_manager=None, chat_manager=None)

    plan = await orchestrator._build_plan(
        instruction="Build lesson blocks",
        pm_bot_id="pm-orchestrator",
        bots=bots,
        context_items=[],
    )

    assert [step["bot_id"] for step in plan["steps"]] == [
        "pm-research-analyst",
        "pm-research-analyst",
        "pm-research-analyst",
        "pm-engineer",
    ]
    assert plan["steps"][3]["depends_on"] == ["step_1_code", "step_1_data", "step_1_online"]
    assert "Implementation workstream list for coder fan-out" in plan["steps"][3]["deliverables"]


def test_normalize_evidence_requirements_downgrades_spec_file_commit_claims() -> None:
    orchestrator = PMOrchestrator(bot_registry=None, scheduler=None, task_manager=None, chat_manager=None)

    requirements = orchestrator._normalize_evidence_requirements(
        step_kind="specification",
        deliverables=["docs/lesson_blocks_design.md", "docs/lesson_blocks_flow.png"],
        evidence_requirements=[
            "Design document stored at docs/lesson_blocks_design.md",
            "Diagram attached to the document",
        ],
    )

    assert requirements[0] == "Proposed repo file artifacts for each listed deliverable"
    assert "Deliverable: path" in requirements[1]


def test_normalize_evidence_requirements_downgrades_planning_link_claims() -> None:
    orchestrator = PMOrchestrator(bot_registry=None, scheduler=None, task_manager=None, chat_manager=None)

    requirements = orchestrator._normalize_evidence_requirements(
        step_kind="planning",
        deliverables=["GitHub issue tracker entries"],
        evidence_requirements=[
            "URLs of created GitHub issues",
            "Milestone and project board links",
        ],
    )

    assert requirements[0] == "Proposed issue, milestone, or board definitions"
    assert "non-placeholder links" in requirements[1]


def test_normalize_evidence_requirements_downgrades_spec_issue_link_claims() -> None:
    orchestrator = PMOrchestrator(bot_registry=None, scheduler=None, task_manager=None, chat_manager=None)

    requirements = orchestrator._normalize_evidence_requirements(
        step_kind="specification",
        deliverables=["Issue definitions (markdown or JSON)"],
        evidence_requirements=[
            "URL of the created GitHub issue",
            "Attached specification document (Markdown) in the issue",
        ],
    )

    assert requirements[0] == "Proposed issue, milestone, or board definitions"
    assert "non-placeholder links" in requirements[1]


def test_normalize_deliverables_for_spec_step_rewrites_placeholders_and_binary_assets() -> None:
    orchestrator = PMOrchestrator(bot_registry=None, scheduler=None, task_manager=None, chat_manager=None)

    deliverables = orchestrator._normalize_deliverables_for_step(
        step_kind="specification",
        deliverables=[
            "docs/lesson_blocks_math_geometry_spec.md",
            "docs/lesson_blocks_flow.png",
            "GitHub issues: #101 (Triangle), #102 (Circle), #103 (Polygon)",
        ],
    )

    assert "docs/lesson_blocks_math_geometry_spec.md" in deliverables
    assert "docs/lesson_blocks_flow.mermaid.md" in deliverables
    assert "Issue definitions (markdown or JSON)" in deliverables
    assert all("png" not in item.lower() for item in deliverables)
    assert all("github issue" not in item.lower() for item in deliverables)


def test_normalize_deliverables_for_planning_step_rewrites_readme_placeholder() -> None:
    orchestrator = PMOrchestrator(bot_registry=None, scheduler=None, task_manager=None, chat_manager=None)

    deliverables = orchestrator._normalize_deliverables_for_step(
        step_kind="planning",
        deliverables=[
            "docs/roadmap/geometry_lessons_roadmap.md",
            "README.md section placeholder",
        ],
    )

    assert "docs/roadmap/geometry_lessons_roadmap.md" in deliverables
    assert "README.md update proposal" in deliverables


def test_infer_step_kind_prefers_planning_for_issue_tracker_steps() -> None:
    orchestrator = PMOrchestrator(bot_registry=None, scheduler=None, task_manager=None, chat_manager=None)

    step_kind = orchestrator._normalize_step_kind(
        "",
        title="Create Tracking Issues in the Repository",
        instruction="Create issue tracker entries and roadmap updates.",
        role_hint="coder",
        deliverables=["docs/issue_tracker_geometry.md"],
    )

    assert step_kind == "planning"


def test_infer_step_kind_prefers_planning_for_research_issue_steps() -> None:
    orchestrator = PMOrchestrator(bot_registry=None, scheduler=None, task_manager=None, chat_manager=None)

    step_kind = orchestrator._normalize_step_kind(
        "",
        title="Define requirements and create tracking issue",
        instruction="Write the requirements summary and create a tracking issue proposal.",
        role_hint="researcher",
        deliverables=["Issue definitions (markdown or JSON)"],
    )

    assert step_kind == "planning"


def test_normalize_step_kind_overrides_explicit_repo_change_for_issue_tracker_step() -> None:
    orchestrator = PMOrchestrator(bot_registry=None, scheduler=None, task_manager=None, chat_manager=None)

    step_kind = orchestrator._normalize_step_kind(
        "repo_change",
        title="Create tracked Git issues for each lesson block",
        instruction="Create tracked Git issues for each lesson block and link them to the spec.",
        role_hint="coder",
        deliverables=["List of created issue IDs and URLs"],
    )

    assert step_kind == "planning"


def test_normalize_deliverables_for_test_step_removes_release_side_effects() -> None:
    orchestrator = PMOrchestrator(bot_registry=None, scheduler=None, task_manager=None, chat_manager=None)

    deliverables = orchestrator._normalize_deliverables_for_step(
        step_kind="test_execution",
        deliverables=[
            "tests/ directory updates",
            "Merged pull request",
            "Git tag vX.Y.Z",
            "Release notes in CHANGELOG.md",
        ],
    )

    assert "tests/ directory updates" in deliverables
    assert all("pull request" not in item.lower() for item in deliverables)
    assert all("git tag" not in item.lower() for item in deliverables)
    assert all("changelog" not in item.lower() for item in deliverables)


def test_normalize_deliverables_for_test_step_rewrites_ci_run_links() -> None:
    orchestrator = PMOrchestrator(bot_registry=None, scheduler=None, task_manager=None, chat_manager=None)

    deliverables = orchestrator._normalize_deliverables_for_step(
        step_kind="test_execution",
        deliverables=[
            "GitHub Actions run #<run_id> (link)",
            "coverage/geometry_coverage.xml",
        ],
    )

    assert "Test run log artifact" in deliverables
    assert "coverage/geometry_coverage.xml" in deliverables
    assert all("github actions" not in item.lower() for item in deliverables)


def test_normalize_deliverables_for_repo_change_and_release_steps_drop_placeholders() -> None:
    orchestrator = PMOrchestrator(bot_registry=None, scheduler=None, task_manager=None, chat_manager=None)

    repo_deliverables = orchestrator._normalize_deliverables_for_step(
        step_kind="repo_change",
        deliverables=[
            "src/lessons/math_geometry/__init__.py",
            "Pull request #<number>",
        ],
    )
    release_deliverables = orchestrator._normalize_deliverables_for_step(
        step_kind="release",
        deliverables=[
            "Git tag vX.Y.Z",
            "Pull request #<number> (merged after approval)",
            "Updated CHANGELOG.md",
        ],
    )

    assert "src/lessons/math_geometry/__init__.py" in repo_deliverables
    assert all("pull request" not in item.lower() for item in repo_deliverables)
    assert "Release tag proposal" in release_deliverables
    assert "Release readiness summary" in release_deliverables
    assert "Updated CHANGELOG.md" in release_deliverables


def test_normalize_evidence_requirements_rewrites_ci_link_requirements_for_test_steps() -> None:
    orchestrator = PMOrchestrator(bot_registry=None, scheduler=None, task_manager=None, chat_manager=None)

    requirements = orchestrator._normalize_evidence_requirements(
        step_kind="test_execution",
        deliverables=["Test run log artifact", "coverage/geometry_coverage.xml"],
        evidence_requirements=[
            "CI run logs (GitHub Actions) showing all stages passed",
            "Coverage report file: coverage/geometry_coverage.xml",
        ],
    )

    assert requirements[0] == "Executed test command output"
    assert "Coverage report file or test run log artifact" in requirements[1]


def test_build_step_instruction_requires_deliverable_file_format() -> None:
    orchestrator = PMOrchestrator(bot_registry=None, scheduler=None, task_manager=None, chat_manager=None)

    instruction = orchestrator._build_step_instruction(
        base_instruction="Write the design document.",
        step_kind="specification",
        deliverables=["docs/lesson_blocks_design.md"],
        evidence_requirements=["Proposed repo file artifacts for each listed deliverable"],
    )

    assert "Deliverables: docs/lesson_blocks_design.md" in instruction
    assert "Deliverable: path" in instruction
    assert "`artifacts` array" in instruction
    assert "The repo profile is authoritative" in instruction
    assert "Never invent placeholders" in instruction


def test_build_step_instruction_mentions_diagram_source_and_concise_spec() -> None:
    orchestrator = PMOrchestrator(bot_registry=None, scheduler=None, task_manager=None, chat_manager=None)

    instruction = orchestrator._build_step_instruction(
        base_instruction="Write the design document.",
        step_kind="specification",
        deliverables=["docs/lesson_blocks_flow.mermaid.md"],
        evidence_requirements=["Proposed repo file artifacts for each listed deliverable"],
    )

    assert "Mermaid or markdown diagram source" in instruction
    assert "Keep the artifact concise" in instruction


def test_build_step_instruction_for_test_execution_demands_command_and_report_artifacts() -> None:
    orchestrator = PMOrchestrator(bot_registry=None, scheduler=None, task_manager=None, chat_manager=None)

    instruction = orchestrator._build_step_instruction(
        base_instruction="Execute the tests.",
        step_kind="test_execution",
        deliverables=["coverage/report.txt"],
        evidence_requirements=["Executed test command output", "Coverage report file or test run log artifact"],
    )

    assert "Executed Commands" in instruction
    assert "Deliverable: path" in instruction
    assert "`artifacts` array" in instruction
    assert "mocked, representative, or checklist-only" in instruction


def test_build_step_instruction_treats_repo_profile_context_as_authoritative() -> None:
    orchestrator = PMOrchestrator(bot_registry=None, scheduler=None, task_manager=None, chat_manager=None)

    instruction = orchestrator._build_step_instruction(
        base_instruction="Summarize the requirements.",
        step_kind="specification",
        deliverables=["Requirements summary artifact"],
        evidence_requirements=["Requirements artifact"],
        context_items=["[repo-profile] Workspace stack summary\nLikely primary stack: dotnet"],
    )

    assert "repo-profile context above is authoritative" in instruction
    assert "Do not say the stack is unknown" in instruction


def test_build_step_instruction_for_repo_change_without_explicit_paths_still_requires_artifacts() -> None:
    orchestrator = PMOrchestrator(bot_registry=None, scheduler=None, task_manager=None, chat_manager=None)

    instruction = orchestrator._build_step_instruction(
        base_instruction="Implement the approved solution.",
        step_kind="repo_change",
        deliverables=["Repo file artifacts for implementation", "Implementation notes"],
        evidence_requirements=["Proposed repo file artifacts or code patches"],
        context_items=["[repo-profile] Workspace stack summary\nLikely primary stack: .NET"],
    )

    assert "This is a repo-change step" in instruction
    assert "non-empty `artifacts` array" in instruction
    assert "Do not return only summaries" in instruction


def test_expand_test_execution_steps_splits_test_file_creation_from_execution() -> None:
    orchestrator = PMOrchestrator(bot_registry=None, scheduler=None, task_manager=None, chat_manager=None)

    expanded = orchestrator._expand_test_execution_steps(
        {
            "steps": [
                {
                    "id": "step_1",
                    "title": "Write and Execute Tests",
                    "instruction": "Create tests and run them.",
                    "role_hint": "tester",
                    "step_kind": "test_execution",
                    "depends_on": ["step_0"],
                    "deliverables": [
                        "tests/test_geometry.py",
                        "tests/test_math.py",
                        "reports/test_report.xml",
                        "reports/coverage_summary.txt",
                    ],
                    "acceptance_criteria": ["Tests pass"],
                    "quality_gates": ["Coverage >= 90%"],
                }
            ]
        }
    )

    assert len(expanded["steps"]) == 2
    create_step, execute_step = expanded["steps"]
    assert create_step["step_kind"] == "repo_change"
    assert create_step["deliverables"] == ["tests/test_geometry.py", "tests/test_math.py"]
    assert create_step["depends_on"] == ["step_0"]
    assert execute_step["step_kind"] == "test_execution"
    assert execute_step["deliverables"] == ["reports/test_report.xml", "reports/coverage_summary.txt"]
    assert execute_step["depends_on"] == ["step_1_create_tests"]


def test_expand_test_execution_steps_recognizes_dotnet_test_project_paths() -> None:
    orchestrator = PMOrchestrator(bot_registry=None, scheduler=None, task_manager=None, chat_manager=None)

    expanded = orchestrator._expand_test_execution_steps(
        {
            "steps": [
                {
                    "id": "step_1",
                    "title": "Create and run .NET tests",
                    "instruction": "Add xUnit coverage and execute it.",
                    "role_hint": "tester",
                    "step_kind": "test_execution",
                    "depends_on": [],
                    "deliverables": [
                        "GlobeIQ.Server.Tests/Geometry/GeometryLessonServiceTests.cs",
                        "GlobeIQ.WebApp.Tests/Pages/GeometryLessonTests.cs",
                        "TestResults.xml",
                        "CoverageReport.xml",
                    ],
                    "acceptance_criteria": ["Tests pass"],
                    "quality_gates": ["Coverage report is produced"],
                }
            ]
        }
    )

    assert len(expanded["steps"]) == 2
    create_step, execute_step = expanded["steps"]
    assert create_step["deliverables"] == [
        "GlobeIQ.Server.Tests/Geometry/GeometryLessonServiceTests.cs",
        "GlobeIQ.WebApp.Tests/Pages/GeometryLessonTests.cs",
    ]
    assert execute_step["deliverables"] == ["TestResults.xml", "CoverageReport.xml"]


def test_sanitize_plan_for_operator_scope_removes_issue_planning_and_ci_workflow_steps() -> None:
    orchestrator = PMOrchestrator(bot_registry=None, scheduler=None, task_manager=None, chat_manager=None)

    sanitized = orchestrator._sanitize_plan_for_operator_scope(
        {
            "steps": [
                {
                    "id": "step_1",
                    "title": "Create GitHub Issues and Project Board Items",
                    "instruction": "Create GitHub issues and add them to the project board.",
                    "role_hint": "researcher",
                    "step_kind": "planning",
                    "depends_on": [],
                    "deliverables": ["Issue definitions (markdown or JSON)", "Project board proposal (markdown)"],
                    "acceptance_criteria": ["Issue tracker entries are ready"],
                    "quality_gates": [],
                    "evidence_requirements": ["Proposed issue, milestone, or board definitions"],
                },
                {
                    "id": "step_2",
                    "title": "Write Unit Tests & Integrate with CI",
                    "instruction": "Create tests and update the CI workflow to run them.",
                    "role_hint": "tester",
                    "step_kind": "test_execution",
                    "depends_on": ["step_1"],
                    "deliverables": [
                        "tests/LessonBlocks/GeometryLessonTests.cs",
                        "tests/LessonBlocks/MathematicsLessonTests.cs",
                        ".github/workflows/ci.yml",
                    ],
                    "acceptance_criteria": ["CI pipeline completes without errors", "Tests pass locally"],
                    "quality_gates": ["CI is green"],
                    "evidence_requirements": ["Executed test command output", "Coverage report artifact"],
                },
            ]
        },
        instruction="Implement mathematics and geometry lesson blocks.",
    )

    assert len(sanitized["steps"]) == 1
    step = sanitized["steps"][0]
    assert step["id"] == "step_2"
    assert step["depends_on"] == []
    assert step["step_kind"] == "test_execution"
    assert ".github/workflows/ci.yml" not in step["deliverables"]
    assert "tests/LessonBlocks/GeometryLessonTests.cs" in step["deliverables"]
    assert "tests/LessonBlocks/MathematicsLessonTests.cs" in step["deliverables"]
    assert "ci" not in step["title"].lower()
    assert "workflow" not in step["instruction"].lower()
    assert all("ci" not in item.lower() for item in step["acceptance_criteria"])
    assert all("ci" not in item.lower() for item in step["quality_gates"])


def test_sanitize_plan_for_operator_scope_converts_release_step_to_review_summary() -> None:
    orchestrator = PMOrchestrator(bot_registry=None, scheduler=None, task_manager=None, chat_manager=None)

    sanitized = orchestrator._sanitize_plan_for_operator_scope(
        {
            "steps": [
                {
                    "id": "step_5",
                    "title": "Code Review, Merge, and Release",
                    "instruction": "Review the changes, merge the pull request, tag the release, and update the changelog.",
                    "role_hint": "security-reviewer",
                    "step_kind": "release",
                    "depends_on": ["step_4"],
                    "deliverables": ["Review findings", "CHANGELOG.md entry", "Git tag vX.Y.Z"],
                    "acceptance_criteria": ["Pull request approved and merged"],
                    "quality_gates": ["Release complete"],
                    "evidence_requirements": ["Pull request, merge, or release artifact"],
                }
            ]
        },
        instruction="Implement mathematics and geometry lesson blocks.",
    )

    assert len(sanitized["steps"]) == 1
    step = sanitized["steps"][0]
    assert step["step_kind"] == "review"
    assert step["title"] == "Finalize verification summary"
    assert "merge" not in step["instruction"].lower()
    assert "deploy" not in step["instruction"].lower()
    assert step["deliverables"] == ["Review findings", "Final verification summary"]


def test_build_step_instruction_injects_context_items_at_top() -> None:
    orchestrator = PMOrchestrator(bot_registry=None, scheduler=None, task_manager=None, chat_manager=None)

    instruction = orchestrator._build_step_instruction(
        base_instruction="Implement the math geometry blocks",
        step_kind="repo_change",
        deliverables=["src/lesson_blocks/math_block.py"],
        evidence_requirements=["Proposed file contents"],
        context_items=[
            "[repo-profile] Workspace stack summary\nLikely primary stack: .NET / ASP.NET Razor",
            "[vault] Some vault context",
        ],
    )

    # Context should be at the top
    assert instruction.startswith("Context:")
    assert "[repo-profile]" in instruction
    assert ".NET" in instruction
    assert "Deliverables:" in instruction
    assert "src/lesson_blocks/math_block.py" in instruction


def test_build_step_instruction_without_context_items_works_normally() -> None:
    orchestrator = PMOrchestrator(bot_registry=None, scheduler=None, task_manager=None, chat_manager=None)

    instruction = orchestrator._build_step_instruction(
        base_instruction="Implement the feature",
        step_kind="repo_change",
        deliverables=["src/file.py"],
        evidence_requirements=None,
        context_items=None,
    )

    assert "Context:" not in instruction
    assert "Implement the feature" in instruction
    assert "Deliverables:" in instruction


async def test_wait_for_completion_labels_chat_preview_truncation() -> None:
    long_output = "A" * 260
    completed_task = Task(
        id="task-1",
        bot_id="pm-coder",
        payload={"title": "Implement code"},
        status="completed",
        result={"output": long_output},
        created_at="2026-03-16T18:22:49+00:00",
        updated_at="2026-03-16T18:23:49+00:00",
    )
    task_manager = type("TaskManager", (), {"get_task": AsyncMock(return_value=completed_task)})()
    orchestrator = PMOrchestrator(
        bot_registry=None,
        scheduler=None,
        task_manager=task_manager,
        chat_manager=None,
    )

    completion = await orchestrator.wait_for_completion(
        {"tasks": [{"id": "task-1"}]},
        poll_interval_seconds=0.0,
        max_wait_seconds=0.0,
    )

    assert "Output Preview:" in completion["summary_text"]
    assert "preview truncated" in completion["summary_text"].lower()


@pytest.mark.anyio
async def test_orchestrate_assignment_fails_closed_for_unknown_explicit_bot_id() -> None:
    bots = [
        _bot(bot_id="pm-orchestrator", name="PM Orchestrator", role="pm", priority=100),
        _bot(bot_id="pm-coder", name="PM Coder", role="coder", priority=85),
    ]
    bot_registry = type("BotRegistry", (), {"list": AsyncMock(return_value=bots)})()
    task_manager = type("TaskManager", (), {"create_task": AsyncMock()})()
    orchestrator = PMOrchestrator(
        bot_registry=bot_registry,
        scheduler=None,
        task_manager=task_manager,
        chat_manager=None,
    )
    orchestrator._build_plan = AsyncMock(
        return_value={
            "steps": [
                {
                    "id": "step_1",
                    "title": "Implement feature",
                    "instruction": "Do the work",
                    "bot_id": "missing-bot",
                    "role_hint": "coder",
                    "depends_on": [],
                    "acceptance_criteria": [],
                    "deliverables": [],
                    "quality_gates": [],
                    "evidence_requirements": [],
                }
            ],
            "global_acceptance_criteria": [],
            "global_quality_gates": [],
            "risks": [],
        }
    )

    with pytest.raises(BotNotFoundError, match="missing-bot"):
        await orchestrator.orchestrate_assignment(
            conversation_id="conv-1",
            instruction="Implement feature",
        )


async def test_wait_for_completion_marks_snapshot_when_timeout_reached() -> None:
    running_task = Task(
        id="task-1",
        bot_id="pm-coder",
        payload={"title": "Implement code"},
        status="running",
        created_at="2026-03-16T18:22:49+00:00",
        updated_at="2026-03-16T18:22:49+00:00",
    )
    task_manager = type("TaskManager", (), {"get_task": AsyncMock(return_value=running_task)})()
    orchestrator = PMOrchestrator(
        bot_registry=None,
        scheduler=None,
        task_manager=task_manager,
        chat_manager=None,
    )

    completion = await orchestrator.wait_for_completion(
        {"tasks": [{"id": "task-1"}]},
        poll_interval_seconds=0.0,
        max_wait_seconds=0.0,
    )

    assert completion["all_terminal"] is False
    assert "snapshot summary" in completion["summary_text"].lower()
    assert "check the dag or tasks page" in completion["summary_text"].lower()


@pytest.mark.anyio
async def test_wait_for_completion_tracks_trigger_spawned_orchestration_tasks() -> None:
    engineer_task = Task(
        id="task-engineer",
        bot_id="pm-engineer",
        payload={"title": "Plan architecture and implementation sequence"},
        metadata=TaskMetadata(source="chat_assign", orchestration_id="orch-dynamic", step_id="step_2"),
        status="completed",
        created_at="2026-03-16T18:22:49+00:00",
        updated_at="2026-03-16T18:23:49+00:00",
        result={"status": "complete"},
    )
    coder_task = Task(
        id="task-coder-1",
        bot_id="pm-coder",
        payload={"title": "Implement routing workstream"},
        metadata=TaskMetadata(source="bot_trigger", orchestration_id="orch-dynamic", parent_task_id="task-engineer"),
        status="completed",
        created_at="2026-03-16T18:24:00+00:00",
        updated_at="2026-03-16T18:25:00+00:00",
        result={"status": "complete"},
    )
    task_manager = type(
        "TaskManager",
        (),
        {
            "list_tasks": AsyncMock(return_value=[engineer_task, coder_task]),
            "get_task": AsyncMock(side_effect=lambda task_id: engineer_task if task_id == "task-engineer" else coder_task),
        },
    )()
    orchestrator = PMOrchestrator(
        bot_registry=None,
        scheduler=None,
        task_manager=task_manager,
        chat_manager=None,
    )

    completion = await orchestrator.wait_for_completion(
        {"orchestration_id": "orch-dynamic", "tasks": [{"id": "task-engineer"}]},
        poll_interval_seconds=0.0,
        max_wait_seconds=0.01,
    )

    assert completion["all_terminal"] is True
    assert completion["task_count"] == 2
    assert "Assignment summary (2 tasks):" in completion["summary_text"]
    assert "Implement routing workstream" in completion["summary_text"]
