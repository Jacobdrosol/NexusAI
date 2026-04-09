"""Deployment status and execution manager for dashboard-triggered updates."""
from __future__ import annotations

import json
import os
import subprocess
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso_utc(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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
            "run_id": None,
            "deployed_commit": None,
            "started_at": None,
            "finished_at": None,
            "log_updated_at": None,
            "last_error": None,
            "last_run_by": None,
            "log_tail": [],
            "log_cleared_at": None,
            "runner_container_name": None,
            "runner_log_line_count": 0,
        }

    def _normalize_state(self, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            return self._default_state()
        merged = self._default_state()
        merged.update(raw)
        if not isinstance(merged.get("log_tail"), list):
            merged["log_tail"] = []
        return merged

    def _load_state(self) -> dict[str, Any]:
        if not self._status_path.exists():
            return self._default_state()
        try:
            raw = json.loads(self._status_path.read_text(encoding="utf-8"))
            return self._normalize_state(raw)
        except Exception:
            return self._default_state()

    def _refresh_state_from_disk(self) -> None:
        if not self._status_path.exists():
            self._state = self._default_state()
            return
        try:
            raw = json.loads(self._status_path.read_text(encoding="utf-8"))
            self._state = self._normalize_state(raw)
        except Exception:
            self._state = self._default_state()

    def _save_state(self) -> None:
        self._status_path.write_text(
            json.dumps(self._state, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def _append_log(self, line: str) -> None:
        logs = self._state.setdefault("log_tail", [])
        stamped = f"[{_utc_now()}] {line}"
        logs.append(stamped)
        self._state["log_tail"] = logs[-200:]
        self._state["log_updated_at"] = _utc_now()
        self._save_state()

    def clear_log(self) -> None:
        with self._lock:
            self._refresh_state_from_disk()
            self._state["log_tail"] = []
            self._state["log_updated_at"] = _utc_now()
            self._state["log_cleared_at"] = self._state["log_updated_at"]
            self._save_state()

    def _run_git(self, args: list[str]) -> tuple[str | None, str | None]:
        try:
            cp = subprocess.run(
                ["git", *args],
                cwd=str(self._repo_root),
                capture_output=True,
                text=True,
                check=True,
            )
            return (cp.stdout or "").strip(), None
        except subprocess.CalledProcessError as exc:
            err = (exc.stderr or exc.stdout or str(exc)).strip()
            return None, err or "git command failed"
        except Exception as exc:
            return None, str(exc)

    def _current_commit(self) -> str | None:
        out, _ = self._run_git(["rev-parse", "HEAD"])
        return out

    def _origin_main_commit(self, do_fetch: bool) -> tuple[str | None, str | None]:
        fetch_error: str | None = None
        if do_fetch:
            _, fetch_error = self._run_git(["fetch", "origin", "main"])
        rev, rev_error = self._run_git(["rev-parse", "origin/main"])
        return rev, (fetch_error or rev_error)

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

    def _use_subcontainer_runner(self) -> bool:
        return str(os.environ.get("NEXUSAI_DEPLOY_RUN_IN_SUBCONTAINER", "1")).strip().lower() not in {
            "",
            "0",
            "false",
            "no",
            "off",
        }

    def _runner_container_name(self) -> str:
        return str(self._state.get("runner_container_name") or "").strip()

    def _runner_status(self, runner_name: str) -> tuple[str | None, int | None, str | None]:
        if not runner_name:
            return None, None, None
        try:
            cp = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Status}}|{{.State.ExitCode}}", runner_name],
                capture_output=True,
                text=True,
                check=False,
            )
        except Exception as exc:
            return None, None, str(exc)
        if cp.returncode != 0:
            err = (cp.stderr or cp.stdout or "").strip() or "runner inspect failed"
            return None, None, err
        raw = (cp.stdout or "").strip()
        status = raw
        exit_code: int | None = None
        if "|" in raw:
            status_part, exit_part = raw.split("|", 1)
            status = status_part.strip()
            try:
                exit_code = int(str(exit_part or "").strip())
            except Exception:
                exit_code = None
        return status or None, exit_code, None

    def _detect_runner_image(self) -> str | None:
        explicit = str(os.environ.get("NEXUSAI_DEPLOY_RUNNER_IMAGE", "") or "").strip()
        if explicit:
            return explicit
        hostname = str(os.environ.get("HOSTNAME", "") or "").strip()
        if hostname:
            try:
                cp = subprocess.run(
                    ["docker", "inspect", "-f", "{{.Config.Image}}", hostname],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if cp.returncode == 0:
                    detected = str(cp.stdout or "").strip()
                    if detected:
                        return detected
            except Exception:
                pass
        active = self._active_color()
        candidates = []
        if active in {"blue", "green"}:
            candidates.append(f"nexusai-dashboard_{active}:latest")
        candidates.extend(["nexusai-dashboard-blue:latest", "nexusai-dashboard-green:latest"])
        seen: set[str] = set()
        for image in candidates:
            if image in seen:
                continue
            seen.add(image)
            cp = subprocess.run(
                ["docker", "image", "inspect", image],
                capture_output=True,
                text=True,
                check=False,
            )
            if cp.returncode == 0:
                return image
        return None

    def _launch_subcontainer_runner(self, *, run_id: str, run_cmd: str) -> tuple[str | None, str | None]:
        runner_image = self._detect_runner_image()
        if not runner_image:
            return None, "could not determine deploy runner image (set NEXUSAI_DEPLOY_RUNNER_IMAGE)"
        host_repo_root = str(os.environ.get("NEXUSAI_DEPLOY_HOST_REPO_ROOT", "/opt/NexusAI") or "").strip() or "/opt/NexusAI"
        runner_name = f"nexus-deploy-runner-{run_id[:12]}"
        subprocess.run(["docker", "rm", "-f", runner_name], capture_output=True, text=True, check=False)
        cmd = [
            "docker",
            "run",
            "-d",
            "--name",
            runner_name,
            "--label",
            f"nexusai.deploy.run_id={run_id}",
            "-v",
            "/var/run/docker.sock:/var/run/docker.sock",
            "-v",
            f"{host_repo_root}:{host_repo_root}",
            "-w",
            host_repo_root,
        ]
        for key, value in os.environ.items():
            if key == "NEXUSAI_DEPLOY_RUN_IN_SUBCONTAINER":
                continue
            if key.startswith("NEXUSAI_") or key == "COMPOSE_PROJECT_NAME":
                cmd.extend(["-e", f"{key}={value}"])
        cmd.extend([runner_image, "sh", "-lc", run_cmd])
        cp = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if cp.returncode != 0:
            detail = (cp.stderr or cp.stdout or "").strip() or "failed to launch deploy runner container"
            return None, detail
        return runner_name, None

    def _sync_subcontainer_runner(self) -> None:
        runner_name = self._runner_container_name()
        if not runner_name:
            return
        logs_cp = subprocess.run(
            ["docker", "logs", "--tail", "400", runner_name],
            capture_output=True,
            text=True,
            check=False,
        )
        raw_logs = (logs_cp.stdout or "") + ("\n" + logs_cp.stderr if logs_cp.stderr else "")
        lines = [str(line).rstrip() for line in raw_logs.splitlines() if str(line).strip()]
        seen_count = int(self._state.get("runner_log_line_count") or 0)
        if seen_count < 0 or seen_count > len(lines):
            seen_count = 0
        for line in lines[seen_count:]:
            self._append_log(line)
        self._state["runner_log_line_count"] = len(lines)
        self._save_state()

        status, exit_code, inspect_err = self._runner_status(runner_name)
        if status in {"running", "created", "restarting"}:
            return
        if status in {"exited", "dead"}:
            local_commit = self._current_commit()
            self._state["state"] = "succeeded" if int(exit_code or 1) == 0 else "failed"
            self._state["finished_at"] = _utc_now()
            if int(exit_code or 1) == 0 and local_commit:
                self._state["deployed_commit"] = local_commit
                self._state["last_error"] = None
            else:
                self._state["last_error"] = f"Deploy runner exited with code {int(exit_code or 1)}."
            self._append_log(f"deploy: runner container exited with code {int(exit_code or 1)}")
            subprocess.run(["docker", "rm", "-f", runner_name], capture_output=True, text=True, check=False)
            self._state["runner_container_name"] = None
            self._state["runner_log_line_count"] = 0
            self._save_state()
            return
        if inspect_err:
            self._append_log(f"deploy: runner inspect warning: {inspect_err}")

    def _recover_stale_running_state(self) -> None:
        if str(self._state.get("state") or "").strip().lower() != "running":
            return
        runner_name = self._runner_container_name()
        if runner_name:
            status, _exit_code, _err = self._runner_status(runner_name)
            if status in {"running", "created", "restarting"}:
                return
        if self._thread and self._thread.is_alive():
            return
        now = datetime.now(timezone.utc)
        started_at = _parse_iso_utc(self._state.get("started_at"))
        log_updated_at = _parse_iso_utc(self._state.get("log_updated_at"))
        last_activity = log_updated_at or started_at
        # If there has been no runner activity for 90s and no live thread,
        # treat the run as stale so the UI is no longer stuck in "running".
        if last_activity is not None:
            stale_seconds = (now - last_activity).total_seconds()
            if stale_seconds < 90:
                return
        self._state["state"] = "failed"
        self._state["finished_at"] = _utc_now()
        if not str(self._state.get("last_error") or "").strip():
            self._state["last_error"] = (
                "Deploy runner process is no longer active. "
                "This usually means the runner container terminated before completion."
            )
        self._append_log("deploy: stale running state detected; marked as failed")

    def status(self, refresh_remote: bool = False) -> dict[str, Any]:
        with self._lock:
            self._refresh_state_from_disk()
            if self._use_subcontainer_runner():
                self._sync_subcontainer_runner()
            self._recover_stale_running_state()
            local_commit = self._current_commit()
            remote_commit, remote_error = self._origin_main_commit(refresh_remote)
            deployed_commit = self._state.get("deployed_commit")
            running = bool(self._thread and self._thread.is_alive())
            runner_name = self._runner_container_name()
            if runner_name:
                status, _exit_code, _err = self._runner_status(runner_name)
                if status in {"running", "created", "restarting"}:
                    running = True
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
                "remote_check_error": remote_error,
                "active_color": active_color,
                "next_color": next_color,
                "commits_differ": commits_differ,
                "deployed_matches_local": bool(deployed_commit and local_commit and deployed_commit == local_commit),
                "deploy_allowed": gate.ok and not running,
                "deploy_blocked_reason": gate.reason if (not gate.ok) else None,
            }

    def start(self, requested_by: str) -> tuple[bool, str]:
        with self._lock:
            self._refresh_state_from_disk()
            if self._use_subcontainer_runner():
                self._sync_subcontainer_runner()
            self._recover_stale_running_state()
            if self._thread and self._thread.is_alive():
                return False, "Deploy already running."
            runner_name = self._runner_container_name()
            if runner_name:
                status, _exit_code, _err = self._runner_status(runner_name)
                if status in {"running", "created", "restarting"}:
                    return False, "Deploy already running."
            gate = self._deploy_gate()
            if not gate.ok:
                return False, gate.reason or "Deploy is blocked."
            if self._use_subcontainer_runner():
                run_cmd = os.environ.get("NEXUSAI_DEPLOY_RUN_CMD", "").strip()
                run_id = str(uuid.uuid4())
                self._state["state"] = "running"
                self._state["run_id"] = run_id
                self._state["started_at"] = _utc_now()
                self._state["finished_at"] = None
                self._state["last_error"] = None
                self._state["last_run_by"] = requested_by
                self._state["log_tail"] = []
                self._state["log_cleared_at"] = None
                self._state["runner_container_name"] = None
                self._state["runner_log_line_count"] = 0
                self._save_state()
                self._append_log(f"deploy: started run_id={run_id}")
                self._append_log(f"deploy: requested_by={requested_by}")
                self._append_log("deploy: strategy=bluegreen")
                runner_name, launch_error = self._launch_subcontainer_runner(run_id=run_id, run_cmd=run_cmd)
                if not runner_name:
                    self._state["state"] = "failed"
                    self._state["finished_at"] = _utc_now()
                    self._state["last_error"] = launch_error or "failed to launch deploy runner"
                    self._append_log(f"deploy: failed to launch runner: {self._state['last_error']}")
                    self._save_state()
                    return False, self._state["last_error"]
                self._state["runner_container_name"] = runner_name
                self._state["runner_log_line_count"] = 0
                self._append_log(f"deploy: runner container launched: {runner_name}")
                self._save_state()
                return True, "Deploy started."
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
            self._refresh_state_from_disk()
            self._state["state"] = "running"
            self._state["run_id"] = str(uuid.uuid4())
            self._state["started_at"] = _utc_now()
            self._state["finished_at"] = None
            self._state["last_error"] = None
            self._state["last_run_by"] = requested_by
            self._state["log_tail"] = []
            self._state["log_cleared_at"] = None
            self._save_state()
            self._append_log(f"deploy: started run_id={self._state['run_id']}")
            self._append_log(f"deploy: requested_by={requested_by}")
            self._append_log("deploy: strategy=bluegreen")

        try:
            deploy_env = os.environ.copy()
            allow_stop_previous = str(
                os.environ.get("NEXUSAI_DEPLOY_STOP_PREVIOUS_COLOR_FROM_DASHBOARD", "0")
            ).strip().lower() in {"1", "true", "yes", "on"}
            if not allow_stop_previous:
                deploy_env["NEXUSAI_STOP_PREVIOUS_COLOR"] = "0"
                with self._lock:
                    self._append_log(
                        "[deploy-manager] dashboard mode: forcing NEXUSAI_STOP_PREVIOUS_COLOR=0 "
                        "to avoid terminating the running deploy container"
                    )
            proc = subprocess.Popen(
                run_cmd,
                cwd=str(self._repo_root),
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=deploy_env,
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
