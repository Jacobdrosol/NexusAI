"""Repo freshness tracking to bound how often bots pull + re-ingest.

Bots need up-to-date repo context when they pick up a ticket, but running
`git pull` + re-ingesting on every task would hammer the network and the
vault. This module tracks when a project's repo was last pulled/ingested
and enforces a cooldown window so refreshes happen at most once per
window (default 30 minutes), while still guaranteeing a refresh when the
repo has never been synced or the window has elapsed.

State is stored in the project's settings_overrides.repo_workspace so it
survives restarts and is visible in the dashboard.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

_DEFAULT_COOLDOWN_MINUTES = int(os.environ.get("NEXUSAI_REPO_FRESHNESS_COOLDOWN_MINUTES", "30"))
_DEFAULT_MAX_AGE_HOURS = int(os.environ.get("NEXUSAI_REPO_FRESHNESS_MAX_AGE_HOURS", "24"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _cooldown_minutes() -> int:
    return max(1, _DEFAULT_COOLDOWN_MINUTES)


def _max_age_hours() -> int:
    return max(1, _DEFAULT_MAX_AGE_HOURS)


def repo_freshness_state(project: Any) -> Dict[str, Any]:
    """Return the stored freshness state for a project's repo workspace."""
    settings = project.settings_overrides if isinstance(project.settings_overrides, dict) else {}
    cfg = settings.get("repo_workspace") if isinstance(settings.get("repo_workspace"), dict) else {}
    freshness = cfg.get("freshness") if isinstance(cfg.get("freshness"), dict) else {}
    return {
        "last_pull_at": str(freshness.get("last_pull_at") or "").strip() or None,
        "last_ingest_at": str(freshness.get("last_ingest_at") or "").strip() or None,
        "last_pull_commit": str(freshness.get("last_pull_commit") or "").strip() or None,
        "last_pull_status": str(freshness.get("last_pull_status") or "").strip() or None,
        "cooldown_minutes": int(freshness.get("cooldown_minutes") or _cooldown_minutes()),
    }


def should_refresh_repo(project: Any) -> Dict[str, Any]:
    """Decide whether a project's repo needs a pull + re-ingest.

    Returns a dict with ``refresh`` (bool) and ``reason`` (str). A refresh
    is needed when:
      - the repo has never been pulled, or
      - the last pull is older than the max age, or
      - the cooldown window has elapsed since the last pull.
    """
    state = repo_freshness_state(project)
    last_pull = _parse_iso(state["last_pull_at"])
    now = datetime.now(timezone.utc)

    if last_pull is None:
        return {"refresh": True, "reason": "never_synced", "state": state}

    age = now - last_pull
    if age > timedelta(hours=_max_age_hours()):
        return {"refresh": True, "reason": "max_age_exceeded", "age_hours": age.total_seconds() / 3600, "state": state}

    cooldown = timedelta(minutes=state["cooldown_minutes"])
    if age >= cooldown:
        return {"refresh": True, "reason": "cooldown_elapsed", "age_minutes": age.total_seconds() / 60, "state": state}

    return {
        "refresh": False,
        "reason": "within_cooldown",
        "age_minutes": age.total_seconds() / 60,
        "cooldown_minutes": state["cooldown_minutes"],
        "state": state,
    }


def mark_repo_pulled(project: Any, *, commit: Optional[str] = None, status: str = "ok") -> Dict[str, Any]:
    """Return the freshness patch to merge into settings_overrides.repo_workspace."""
    now = _now_iso()
    return {
        "freshness": {
            "last_pull_at": now,
            "last_pull_commit": str(commit or "").strip() or None,
            "last_pull_status": status,
            "cooldown_minutes": _cooldown_minutes(),
        }
    }


def mark_repo_ingested(project: Any) -> Dict[str, Any]:
    """Return the freshness patch marking the vault ingest as complete."""
    return {"freshness": {"last_ingest_at": _now_iso()}}


def feature_branch_name(ticket_external_id: str, title: str = "", max_len: int = 60) -> str:
    """Build a safe feature branch name for a ticket.

    e.g. feature/42-fix-login-bug
    """
    slug = re.sub(r"[^a-z0-9]+", "-", str(title or "").lower()).strip("-")
    base = f"feature/{str(ticket_external_id or '').strip()}"
    if slug:
        base += f"-{slug}"
    return base[:max_len].rstrip("-")


def feature_branch_commands(branch: str, *, base_branch: Optional[str] = None) -> list[list[str]]:
    """Return git command sequences to create a feature branch off base.

    Returns a list of command lists (each run in order):
      - ensure we're on the base branch (or stay put if none)
      - create + switch to the feature branch
    """
    commands: list[list[str]] = []
    if base_branch:
        commands.append(["git", "checkout", base_branch])
    commands.append(["git", "checkout", "-b", branch])
    return commands