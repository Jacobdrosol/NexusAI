"""Tests for repo freshness gating and feature branch helpers."""

from datetime import datetime, timedelta, timezone

import pytest

from control_plane.repo_freshness import (
    feature_branch_commands,
    feature_branch_name,
    mark_repo_ingested,
    mark_repo_pulled,
    repo_freshness_state,
    should_refresh_repo,
)


def _project_with_freshness(freshness):
    return type("P", (), {"settings_overrides": {"repo_workspace": {"freshness": freshness}}})()


def _iso(dt):
    return dt.isoformat()


def test_never_synced_requires_refresh():
    project = type("P", (), {"settings_overrides": {}})()
    decision = should_refresh_repo(project)
    assert decision["refresh"] is True
    assert decision["reason"] == "never_synced"


def test_within_cooldown_no_refresh():
    now = datetime.now(timezone.utc)
    project = _project_with_freshness({"last_pull_at": _iso(now - timedelta(minutes=5))})
    decision = should_refresh_repo(project)
    assert decision["refresh"] is False
    assert decision["reason"] == "within_cooldown"


def test_cooldown_elapsed_requires_refresh():
    now = datetime.now(timezone.utc)
    project = _project_with_freshness({"last_pull_at": _iso(now - timedelta(minutes=45))})
    decision = should_refresh_repo(project)
    assert decision["refresh"] is True
    assert decision["reason"] == "cooldown_elapsed"


def test_max_age_exceeded_requires_refresh():
    now = datetime.now(timezone.utc)
    project = _project_with_freshness({"last_pull_at": _iso(now - timedelta(hours=30))})
    decision = should_refresh_repo(project)
    assert decision["refresh"] is True
    assert decision["reason"] == "max_age_exceeded"


def test_mark_pulled_patch():
    project = type("P", (), {"settings_overrides": {}})()
    patch = mark_repo_pulled(project, commit="abc123", status="ok")
    assert patch["freshness"]["last_pull_commit"] == "abc123"
    assert patch["freshness"]["last_pull_status"] == "ok"
    assert patch["freshness"]["last_pull_at"]


def test_mark_ingested_patch():
    project = type("P", (), {"settings_overrides": {}})()
    patch = mark_repo_ingested(project)
    assert patch["freshness"]["last_ingest_at"]


def test_feature_branch_name():
    assert feature_branch_name("42", "Fix login bug") == "feature/42-fix-login-bug"
    assert feature_branch_name("7", "") == "feature/7"
    assert feature_branch_name("99", "Weird Title!!") == "feature/99-weird-title"


def test_feature_branch_commands():
    cmds = feature_branch_commands("feature/42-fix", base_branch="main")
    assert cmds == [["git", "checkout", "main"], ["git", "checkout", "-b", "feature/42-fix"]]
    cmds2 = feature_branch_commands("feature/42-fix")
    assert cmds2 == [["git", "checkout", "-b", "feature/42-fix"]]