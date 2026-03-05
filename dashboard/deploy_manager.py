"""Deployment status and execution manager for dashboard-triggered updates."""
from __future__ import annotations

import json
import os
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class DeployGate:
    ok: bool
    reason: str | None = None


class DeployManager:
    """Tracks deploy status and runs a configured deploy command asynchronously."""

    _instance: "DeployManager | None" = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        self._repo_root = repo_root
        self._data_dir = repo_root / "data"
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._status_path = self._data_dir / "deploy_status.json"
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._state = self._load_state()

    @classmethod
    def instance(cls) -> "DeployManager":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def _default_state(self) -> dict[str, Any]:
        return {
            "state": "idle",
            "deployed_commit": None,
            "started_at": None,
            "finished_at": None,
            "last_error": None,
            "last_run_by": None,
            "log_tail": [],
        }

    def _load_state(self) -> dict[str, Any]:
        if not self._status_path.exists():
            return self._default_state()
        try:
            raw = json.loads(self._status_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return self._default_state()
            merged = self._default_state()
            merged.update(raw)
            if not isinstance(merged.get("log_tail"), list):
                merged["log_tail"] = []
            return merged
        except Exception:
            return self._default_state()

    def _save_state(self) -> None:
        self._status_path.write_text(
            json.dumps(self._state, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def _append_log(self, line: str) -> None:
        logs = self._state.setdefault("log_tail", [])
        logs.append(line)
        self._state["log_tail"] = logs[-200:]
        self._save_state()

    def _run_git(self, args: list[str]) -> str | None:
        try:
            cp = subprocess.run(
                ["git", *args],
                cwd=str(self._repo_root),
                capture_output=True,
                text=True,
                check=True,
            )
            return (cp.stdout or "").strip()
        except Exception:
            return None

    def _current_commit(self) -> str | None:
        return self._run_git(["rev-parse", "HEAD"])

    def _origin_main_commit(self, do_fetch: bool) -> str | None:
        if do_fetch:
            self._run_git(["fetch", "origin", "main"])
        return self._run_git(["rev-parse", "origin/main"])

    def _deploy_gate(self) -> DeployGate:
        if os.environ.get("NEXUSAI_DEPLOY_ENABLE", "").strip() != "1":
            return DeployGate(False, "Deploy API is disabled. Set NEXUSAI_DEPLOY_ENABLE=1.")

        run_cmd = os.environ.get("NEXUSAI_DEPLOY_RUN_CMD", "").strip()
        if not run_cmd:
            return DeployGate(
                False,
                "No deploy command configured. Set NEXUSAI_DEPLOY_RUN_CMD to a safe blue/green command.",
            )

        strategy = os.environ.get("NEXUSAI_DEPLOY_STRATEGY", "").strip().lower()
        if strategy != "bluegreen":
            return DeployGate(
                False,
                "Only blue/green strategy is allowed. Set NEXUSAI_DEPLOY_STRATEGY=bluegreen.",
            )

        return DeployGate(True)

    def _active_color(self) -> str:
        color_file = self._data_dir / "active_color.txt"
        try:
            val = color_file.read_text(encoding="utf-8").strip().lower()
            if val in {"blue", "green"}:
                return val
        except Exception:
            pass
        return "unknown"

    def status(self, refresh_remote: bool = False) -> dict[str, Any]:
        with self._lock:
            local_commit = self._current_commit()
            remote_commit = self._origin_main_commit(refresh_remote)
            deployed_commit = self._state.get("deployed_commit")
            running = bool(self._thread and self._thread.is_alive())
            gate = self._deploy_gate()
            active_color = self._active_color()
            if active_color == "blue":
                next_color = "green"
            elif active_color == "green":
                next_color = "blue"
            else:
                next_color = "unknown"
            commits_differ = bool(local_commit and remote_commit and local_commit != remote_commit)
            return {
                **self._state,
                "state": "running" if running else self._state.get("state", "idle"),
                "local_commit": local_commit or "unknown",
                "remote_commit": remote_commit or "unknown",
                "active_color": active_color,
                "next_color": next_color,
                "commits_differ": commits_differ,
                "deployed_matches_local": bool(deployed_commit and local_commit and deployed_commit == local_commit),
                "deploy_allowed": gate.ok and not running,
                "deploy_blocked_reason": gate.reason if (not gate.ok) else None,
            }

    def start(self, requested_by: str) -> tuple[bool, str]:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False, "Deploy already running."
            gate = self._deploy_gate()
            if not gate.ok:
                return False, gate.reason or "Deploy is blocked."
            thread = threading.Thread(
                target=self._run_deploy,
                kwargs={"requested_by": requested_by},
                daemon=True,
            )
            self._thread = thread
            thread.start()
            return True, "Deploy started."

    def _run_deploy(self, requested_by: str) -> None:
        run_cmd = os.environ.get("NEXUSAI_DEPLOY_RUN_CMD", "").strip()
        with self._lock:
            self._state["state"] = "running"
            self._state["started_at"] = _utc_now()
            self._state["finished_at"] = None
            self._state["last_error"] = None
            self._state["last_run_by"] = requested_by
            self._state["log_tail"] = []
            self._append_log("deploy: started")
            self._append_log(f"deploy: requested_by={requested_by}")
            self._append_log("deploy: strategy=bluegreen")

        try:
            proc = subprocess.Popen(
                run_cmd,
                cwd=str(self._repo_root),
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                cleaned = line.rstrip()
                if not cleaned:
                    continue
                with self._lock:
                    self._append_log(cleaned)
            rc = proc.wait()
            if rc != 0:
                raise RuntimeError(f"Deploy command exited with code {rc}.")

            local_commit = self._current_commit()
            with self._lock:
                self._state["state"] = "succeeded"
                self._state["finished_at"] = _utc_now()
                if local_commit:
                    self._state["deployed_commit"] = local_commit
                self._append_log("deploy: completed successfully")
        except Exception as exc:
            with self._lock:
                self._state["state"] = "failed"
                self._state["finished_at"] = _utc_now()
                self._state["last_error"] = str(exc)
                self._append_log(f"deploy: failed: {exc}")
        finally:
            with self._lock:
                self._save_state()
