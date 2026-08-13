"""Tests for deterministic plan QC and handoff extraction."""

import pytest

from control_plane.plan_qc import (
    PlanQCError,
    extract_handoff_map,
    qc_report_summary,
    validate_plan_structure,
)


def _valid_plan():
    return {
        "steps": [
            {
                "id": "research",
                "bot_id": "bot-research",
                "step_kind": "planning",
                "instruction": "Research the issue",
                "deliverables": ["brief"],
                "acceptance_criteria": ["brief covers root cause"],
                "depends_on": [],
            },
            {
                "id": "implement",
                "bot_id": "bot-coder",
                "step_kind": "repo_change",
                "instruction": "Implement the fix",
                "deliverables": ["patch", "tests"],
                "acceptance_criteria": ["tests pass"],
                "depends_on": ["research"],
            },
        ]
    }


def test_valid_plan_passes():
    report = validate_plan_structure(_valid_plan())
    assert report["ok"] is True
    assert report["step_count"] == 2
    assert report["bot_ids"] == ["bot-coder", "bot-research"]


def test_missing_steps_raises():
    with pytest.raises(PlanQCError):
        validate_plan_structure({"steps": []})


def test_steps_not_list_raises():
    with pytest.raises(PlanQCError):
        validate_plan_structure({"steps": "nope"})


def test_missing_required_fields():
    report = validate_plan_structure({"steps": [{"instruction": "x"}]})
    assert report["ok"] is False
    assert any("missing required" in e for e in report["errors"])


def test_bot_not_allowed_fails():
    plan = _valid_plan()
    report = validate_plan_structure(plan, allowed_bot_ids=["bot-research"])
    assert report["ok"] is False
    assert any("outside the allowed set" in e for e in report["errors"])


def test_unknown_step_kind_warns_not_fails():
    plan = _valid_plan()
    plan["steps"][0]["step_kind"] = "mystery"
    report = validate_plan_structure(plan)
    assert report["ok"] is True
    assert any("unknown step_kind" in w for w in report["warnings"])


def test_missing_dependency_fails():
    plan = _valid_plan()
    plan["steps"][1]["depends_on"] = ["ghost"]
    report = validate_plan_structure(plan)
    assert report["ok"] is False
    assert any("unknown step" in e for e in report["errors"])


def test_cycle_fails():
    plan = {
        "steps": [
            {"id": "a", "bot_id": "b1", "step_kind": "planning", "depends_on": ["b"]},
            {"id": "b", "bot_id": "b2", "step_kind": "planning", "depends_on": ["a"]},
        ]
    }
    report = validate_plan_structure(plan)
    assert report["ok"] is False
    assert any("cycle" in e for e in report["errors"])


def test_no_acceptance_criteria_warns():
    plan = _valid_plan()
    plan["steps"][0]["acceptance_criteria"] = []
    report = validate_plan_structure(plan)
    assert report["ok"] is True
    assert any("no acceptance criteria" in w for w in report["warnings"])


def test_extract_handoff_map():
    plan = _valid_plan()
    handoff = extract_handoff_map(plan)
    assert set(handoff.keys()) == {"research", "implement"}
    assert handoff["implement"]["bot_id"] == "bot-coder"
    assert handoff["implement"]["step_kind"] == "repo_change"
    assert handoff["implement"]["deliverables"] == ["patch", "tests"]
    assert handoff["implement"]["depends_on"] == ["research"]


def test_extract_handoff_map_bad_plan():
    assert extract_handoff_map(None) == {}
    assert extract_handoff_map({"steps": "x"}) == {}
    assert extract_handoff_map({"steps": ["not-dict"]}) == {}


def test_qc_report_summary():
    report = validate_plan_structure(_valid_plan())
    summary = qc_report_summary(report)
    assert "2 step(s)" in summary
    assert "[OK]" in summary