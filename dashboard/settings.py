"""Settings Blueprint for the NexusAI Dashboard (Flask).

Provides:
  - GET  /settings                – settings UI page (admin only)
  - GET  /api/settings            – list all settings (secrets masked)
  - GET  /api/settings/export/yaml – download settings as YAML
  - GET  /api/settings/export/json – download settings as JSON
  - POST /api/settings/import     – import from uploaded YAML/JSON file
  - GET  /api/settings/<key>      – get single setting
  - POST /api/settings            – bulk update (admin only)
  - PUT  /api/settings/<key>      – update single setting (admin only)
"""

from __future__ import annotations

import json
import logging
import os
import platform
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import yaml
from flask import (
    Blueprint,
    abort,
    jsonify,
    make_response,
    render_template,
    request,
)
from flask_login import current_user, login_required

from dashboard.deploy_manager import DeployManager
from shared.settings_manager import SettingsManager

logger = logging.getLogger(__name__)

bp = Blueprint("settings", __name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CATEGORY_ORDER = ["general", "auth", "llm", "logging", "advanced"]
_CATEGORY_LABELS: Dict[str, str] = {
    "general": "General",
    "auth": "Auth",
    "llm": "LLM / Workers",
    "logging": "Logging",
    "advanced": "Advanced",
}


def _get_mgr() -> SettingsManager:
    """Return the shared SettingsManager singleton."""
    return SettingsManager.instance()


def _group_by_category(
    all_settings: Dict[str, Dict[str, Any]],
) -> list[Dict[str, Any]]:
    """Group settings rows by category, preserving the canonical order."""
    groups: Dict[str, list] = {cat: [] for cat in _CATEGORY_ORDER}
    for key, meta in all_settings.items():
        cat = meta.get("category", "general")
        if cat not in groups:
            groups[cat] = []
        groups[cat].append({"key": key, **meta})
    return [
        {
            "id": cat,
            "label": _CATEGORY_LABELS.get(cat, cat.title()),
            "settings": groups[cat],
        }
        for cat in _CATEGORY_ORDER
        if groups.get(cat)
    ]


def _require_admin() -> None:
    """Abort with 403 if the current user is not an admin."""
    if not current_user.is_authenticated or current_user.role != "admin":
        abort(403)


_TOOL_CHECK_TIMEOUT_SECONDS = 20
_TOOL_INSTALL_TIMEOUT_SECONDS = 1800
_WINGET_FLAGS = [
    "--accept-package-agreements",
    "--accept-source-agreements",
    "--silent",
]

_PERSISTENT_RUNTIME_TOOLCHAINS: dict[str, list[str]] = {
    "code_exec_dotnet": ["dotnet"],
    "test_runner_dotnet_test": ["dotnet"],
    "code_exec_node": ["node"],
    "test_runner_jest": ["node"],
    "ui_browser": ["node", "playwright"],
    "code_exec_go": ["go"],
    "code_exec_rust": ["rust"],
    "test_runner_cargo_test": ["rust"],
    "code_exec_cpp": ["cpp"],
}


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _env_file_path() -> Path:
    return _repo_root() / ".env"


def _compose_core_args() -> list[str]:
    project_name = (os.environ.get("NEXUSAI_COMPOSE_PROJECT_NAME") or "nexusai").strip() or "nexusai"
    return [
        "docker",
        "compose",
        "-p",
        project_name,
        "-f",
        "docker-compose.yml",
    ]


def _compose_project_name() -> str:
    return (os.environ.get("NEXUSAI_COMPOSE_PROJECT_NAME") or "nexusai").strip() or "nexusai"


def _persistent_toolchains_for_tool(tool_id: str) -> list[str]:
    if not platform.system().lower().startswith("linux"):
        return []
    return list(_PERSISTENT_RUNTIME_TOOLCHAINS.get(tool_id, []))


def _configured_runtime_toolchains() -> list[str]:
    env_path = _env_file_path()
    raw = os.environ.get("NEXUSAI_REPO_RUNTIME_TOOLCHAINS", "").strip()
    if env_path.exists():
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                if line.startswith("NEXUSAI_REPO_RUNTIME_TOOLCHAINS="):
                    raw = line.split("=", 1)[1].strip()
                    break
        except Exception:
            pass
    if not raw:
        return []
    return [token.strip().lower() for token in raw.split(",") if token.strip()]


def _write_env_key(key: str, value: str) -> None:
    env_path = _env_file_path()
    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
    replaced = False
    new_lines: list[str] = []
    for line in lines:
        if line.startswith(f"{key}="):
            new_lines.append(f"{key}={value}")
            replaced = True
        else:
            new_lines.append(line)
    if not replaced:
        new_lines.append(f"{key}={value}")
    env_path.write_text("\n".join(new_lines).rstrip() + "\n", encoding="utf-8")


def _configure_persistent_runtime_toolchains(tool_id: str) -> list[str]:
    configured = _configured_runtime_toolchains()
    merged = list(configured)
    for token in _persistent_toolchains_for_tool(tool_id):
        if token not in merged:
            merged.append(token)
    _write_env_key("NEXUSAI_REPO_RUNTIME_TOOLCHAINS", ",".join(merged))
    return merged


def _check_control_plane_runtime(check_command: str | None) -> dict[str, Any] | None:
    if not check_command:
        return None
    try:
        container_lookup = subprocess.run(
            [
                "docker",
                "ps",
                "-q",
                "--filter",
                f"label=com.docker.compose.project={_compose_project_name()}",
                "--filter",
                "label=com.docker.compose.service=control_plane",
            ],
            cwd=str(_repo_root()),
            capture_output=True,
            text=True,
            timeout=_TOOL_CHECK_TIMEOUT_SECONDS,
            check=False,
        )
        container_id = (container_lookup.stdout or "").strip().splitlines()
        if not container_id:
            return None
        target_container = container_id[0].strip()
        completed = subprocess.run(
            ["docker", "exec", target_container, "sh", "-lc", check_command],
            cwd=str(_repo_root()),
            capture_output=True,
            text=True,
            timeout=_TOOL_CHECK_TIMEOUT_SECONDS,
            check=False,
        )
    except Exception:
        return None
    output = (completed.stdout or "") + ("\n" if completed.stdout and completed.stderr else "") + (completed.stderr or "")
    output = output.strip()
    if completed.returncode == 0:
        summary = output.splitlines()[0] if output else "Command succeeded."
        return {"status": "installed", "ok": True, "summary": summary, "output": output}
    status = _classify_check_failure(output)
    summary = output.splitlines()[0] if output else "Command exited with a non-zero status."
    return {"status": status, "ok": False, "summary": summary, "output": output}


def _load_enabled_tool_ids() -> list[str]:
    from shared.tool_catalog import default_enabled_tools

    mgr = _get_mgr()
    raw = mgr.get("enabled_tools")
    try:
        return json.loads(raw) if isinstance(raw, str) and raw else default_enabled_tools()
    except Exception:
        return default_enabled_tools()


def _save_enabled_tool_ids(enabled_ids: list[str]) -> None:
    mgr = _get_mgr()
    changed_by = getattr(current_user, "email", "api")
    mgr.set("enabled_tools", json.dumps(enabled_ids), changed_by)


def _tool_install_plan(tool_id: str) -> dict[str, Any] | None:
    is_windows = platform.system().lower().startswith("win")
    is_linux = platform.system().lower().startswith("linux")
    linux_pkg_prefix = (
        "if [ \"$(id -u)\" -eq 0 ]; then PKG_PREFIX=''; "
        "elif command -v sudo >/dev/null 2>&1; then PKG_PREFIX='sudo '; "
        "else echo 'This installer requires root or sudo on the Linux host.'; exit 1; fi; "
    )
    linux_pkg_install = (
        linux_pkg_prefix +
        "if command -v apt-get >/dev/null 2>&1; then ${{PKG_PREFIX}}apt-get update && ${{PKG_PREFIX}}apt-get install -y {apt_packages}; "
        "elif command -v dnf >/dev/null 2>&1; then ${{PKG_PREFIX}}dnf install -y {dnf_packages}; "
        "elif command -v yum >/dev/null 2>&1; then ${{PKG_PREFIX}}yum install -y {yum_packages}; "
        "else echo 'No supported Linux package manager found for {label}'; exit 1; fi"
    )
    linux_fetch_script = (
        "if command -v curl >/dev/null 2>&1; then curl -fsSL {url} -o {dest}; "
        "elif command -v wget >/dev/null 2>&1; then wget -qO {dest} {url}; "
        "else echo 'curl or wget is required to download installer assets.'; exit 1; fi"
    )
    python_download_commands = {
        "dotnet": [
            sys.executable,
            "-c",
            (
                "import urllib.request; "
                "urllib.request.urlretrieve('https://dot.net/v1/dotnet-install.sh', '/tmp/dotnet-install.sh')"
            ),
        ],
        "rustup": [
            sys.executable,
            "-c",
            (
                "import urllib.request; "
                "urllib.request.urlretrieve('https://sh.rustup.rs', '/tmp/rustup-init.sh')"
            ),
        ],
    }
    if is_windows:
        winget = {
            "code_exec_python": {
                "label": "Install Python 3.12",
                "notes": "Uses winget to install Python 3.12 on this machine.",
                "commands": [["winget", "install", "--id", "Python.Python.3.12", "-e", *_WINGET_FLAGS]],
            },
            "code_exec_dotnet": {
                "label": "Install .NET SDK 8",
                "notes": "Uses winget to install the .NET SDK required for C# build and test tasks.",
                "commands": [["winget", "install", "--id", "Microsoft.DotNet.SDK.8", "-e", *_WINGET_FLAGS]],
            },
            "test_runner_dotnet_test": {
                "label": "Install .NET SDK 8",
                "notes": "dotnet test is included with the .NET SDK.",
                "commands": [["winget", "install", "--id", "Microsoft.DotNet.SDK.8", "-e", *_WINGET_FLAGS]],
            },
            "code_exec_node": {
                "label": "Install Node.js LTS",
                "notes": "Uses winget to install the Node.js LTS runtime.",
                "commands": [["winget", "install", "--id", "OpenJS.NodeJS.LTS", "-e", *_WINGET_FLAGS]],
            },
            "devops_git": {
                "label": "Install Git",
                "notes": "Uses winget to install Git for Windows.",
                "commands": [["winget", "install", "--id", "Git.Git", "-e", *_WINGET_FLAGS]],
            },
            "code_exec_go": {
                "label": "Install Go",
                "notes": "Uses winget to install the Go toolchain.",
                "commands": [["winget", "install", "--id", "GoLang.Go", "-e", *_WINGET_FLAGS]],
            },
            "code_exec_rust": {
                "label": "Install Rust",
                "notes": "Uses winget to install rustup and the Rust toolchain.",
                "commands": [["winget", "install", "--id", "Rustlang.Rustup", "-e", *_WINGET_FLAGS]],
            },
            "test_runner_cargo_test": {
                "label": "Install Rust",
                "notes": "cargo test is included with the Rust toolchain.",
                "commands": [["winget", "install", "--id", "Rustlang.Rustup", "-e", *_WINGET_FLAGS]],
            },
            "code_exec_java": {
                "label": "Install Temurin JDK 17",
                "notes": "Uses winget to install the JDK required for JVM tasks.",
                "commands": [["winget", "install", "--id", "EclipseAdoptium.Temurin.17.JDK", "-e", *_WINGET_FLAGS]],
            },
            "test_runner_junit": {
                "label": "Install Temurin JDK 17",
                "notes": "JUnit tasks require a JDK. Build tooling remains project-specific.",
                "commands": [["winget", "install", "--id", "EclipseAdoptium.Temurin.17.JDK", "-e", *_WINGET_FLAGS]],
            },
        }
        if tool_id in winget:
            return winget[tool_id]
    if is_linux:
        linux = {
            "code_exec_node": {
                "label": "Install Node.js and npm",
                "notes": "Uses the system package manager to install Node.js and npm on this Linux host.",
                "commands": [[
                    "bash",
                    "-lc",
                    "set -e; " + linux_pkg_install.format(apt_packages="nodejs npm", dnf_packages="nodejs npm", yum_packages="nodejs npm", label="Node.js"),
                ]],
            },
            "ui_browser": {
                "label": "Install Playwright runtime",
                "notes": "Installs Node.js if needed, then installs Playwright globally and downloads browser dependencies.",
                "commands": [
                    [
                        "bash",
                        "-lc",
                        "set -e; " + linux_pkg_install.format(apt_packages="nodejs npm", dnf_packages="nodejs npm", yum_packages="nodejs npm", label="Node.js"),
                    ],
                    ["bash", "-lc", "set -e; mkdir -p /tmp/nexusai-playwright && cd /tmp/nexusai-playwright && npm init -y >/dev/null 2>&1 || true && npm install playwright"],
                    ["bash", "-lc", "set -e; cd /tmp/nexusai-playwright && npx playwright install --with-deps chromium"],
                ],
            },
            "devops_git": {
                "label": "Install Git",
                "notes": "Uses the system package manager to install Git on this Linux host.",
                "commands": [[
                    "bash",
                    "-lc",
                    "set -e; " + linux_pkg_install.format(apt_packages="git", dnf_packages="git", yum_packages="git", label="Git"),
                ]],
            },
            "code_exec_go": {
                "label": "Install Go",
                "notes": "Uses the system package manager to install Go on this Linux host.",
                "commands": [[
                    "bash",
                    "-lc",
                    "set -e; " + linux_pkg_install.format(apt_packages="golang-go", dnf_packages="golang", yum_packages="golang", label="Go"),
                ]],
            },
            "code_exec_rust": {
                "label": "Install Rust",
                "notes": "Uses rustup to install the Rust toolchain into the current user profile.",
                "commands": [
                    python_download_commands["rustup"],
                    ["bash", "-lc", "set -e; sh /tmp/rustup-init.sh -y --profile minimal"],
                ],
            },
            "test_runner_cargo_test": {
                "label": "Install Rust",
                "notes": "cargo test is included with the Rust toolchain.",
                "commands": [
                    python_download_commands["rustup"],
                    ["bash", "-lc", "set -e; sh /tmp/rustup-init.sh -y --profile minimal"],
                ],
            },
            "code_exec_java": {
                "label": "Install OpenJDK 17",
                "notes": "Uses the system package manager to install a JDK on this Linux host.",
                "commands": [[
                    "bash",
                    "-lc",
                    "set -e; " + linux_pkg_install.format(apt_packages="openjdk-17-jdk", dnf_packages="java-17-openjdk-devel", yum_packages="java-17-openjdk-devel", label="Java"),
                ]],
            },
            "test_runner_junit": {
                "label": "Install OpenJDK 17",
                "notes": "JUnit tasks require a JDK. Build tooling remains project-specific.",
                "commands": [[
                    "bash",
                    "-lc",
                    "set -e; " + linux_pkg_install.format(apt_packages="openjdk-17-jdk", dnf_packages="java-17-openjdk-devel", yum_packages="java-17-openjdk-devel", label="Java"),
                ]],
            },
            "code_exec_dotnet": {
                "label": "Install .NET SDK 8",
                "notes": "Uses the official dotnet-install script to install the SDK into the current user profile.",
                "commands": [
                    ["bash", "-lc", "set -e; " + linux_pkg_install.format(apt_packages="curl", dnf_packages="curl", yum_packages="curl", label="curl")],
                    python_download_commands["dotnet"],
                    ["bash", "-lc", "set -e; mkdir -p \"$HOME/.dotnet\"; bash /tmp/dotnet-install.sh --channel 8.0 --install-dir \"$HOME/.dotnet\""],
                ],
            },
            "test_runner_dotnet_test": {
                "label": "Install .NET SDK 8",
                "notes": "dotnet test is included with the .NET SDK.",
                "commands": [
                    ["bash", "-lc", "set -e; " + linux_pkg_install.format(apt_packages="curl", dnf_packages="curl", yum_packages="curl", label="curl")],
                    python_download_commands["dotnet"],
                    ["bash", "-lc", "set -e; mkdir -p \"$HOME/.dotnet\"; bash /tmp/dotnet-install.sh --channel 8.0 --install-dir \"$HOME/.dotnet\""],
                ],
            },
        }
        if tool_id in linux:
            return linux[tool_id]

    pip_tools = {
        "test_runner_pytest": {
            "label": "Install pytest",
            "notes": "Installs pytest into the Python environment used by the dashboard host.",
            "commands": [[sys.executable, "-m", "pip", "install", "pytest"]],
        }
    }
    return pip_tools.get(tool_id)


def _classify_check_failure(output: str) -> str:
    lowered = output.lower()
    missing_markers = [
        "not recognized as an internal or external command",
        "command not found",
        "not found",
        "no such file",
        "is not recognized",
    ]
    if any(marker in lowered for marker in missing_markers):
        return "missing"
    return "error"


def _check_tool_availability(check_command: str | None) -> dict[str, Any]:
    if not check_command:
        return {
            "status": "unverified",
            "ok": None,
            "summary": "No automatic check is defined for this tool.",
            "output": "",
        }
    try:
        env = os.environ.copy()
        home = Path.home()
        extra_paths = [
            str(home / ".dotnet"),
            str(home / ".cargo" / "bin"),
            str(home / ".local" / "bin"),
        ]
        env["PATH"] = os.pathsep.join([*extra_paths, env.get("PATH", "")])
        completed = subprocess.run(
            check_command,
            capture_output=True,
            text=True,
            shell=True,
            env=env,
            timeout=_TOOL_CHECK_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "error",
            "ok": False,
            "summary": f"Check timed out after {_TOOL_CHECK_TIMEOUT_SECONDS} seconds.",
            "output": "",
        }
    output = (completed.stdout or "") + ("\n" if completed.stdout and completed.stderr else "") + (completed.stderr or "")
    output = output.strip()
    if completed.returncode == 0:
        summary = output.splitlines()[0] if output else "Command succeeded."
        return {"status": "installed", "ok": True, "summary": summary, "output": output}
    status = _classify_check_failure(output)
    summary = output.splitlines()[0] if output else "Command exited with a non-zero status."
    return {"status": status, "ok": False, "summary": summary, "output": output}


def _tool_runtime_status(tool: Any) -> dict[str, Any]:
    plan = _tool_install_plan(tool.id)
    persistent_toolchains = _persistent_toolchains_for_tool(tool.id)
    if persistent_toolchains:
        configured = _configured_runtime_toolchains()
        check = _check_control_plane_runtime(tool.check_command)
        if check is None:
            missing = [token for token in persistent_toolchains if token not in configured]
            if missing:
                check = {
                    "status": "missing",
                    "ok": False,
                    "summary": "Not configured for the control_plane runtime image.",
                    "output": "",
                }
            else:
                check = {
                    "status": "configured",
                    "ok": None,
                    "summary": "Configured for the control_plane runtime image. Deploy to apply or refresh runtime status.",
                    "output": "",
                }
    else:
        check = _check_tool_availability(tool.check_command)
    return {
        **check,
        "install_supported": plan is not None,
        "install_label": plan["label"] if plan else None,
        "install_notes": plan["notes"] if plan else None,
        "install_mode": "runtime_deploy" if persistent_toolchains else "dashboard_host",
        "configured_toolchains": persistent_toolchains,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


class ToolInstallManager:
    """Runs curated tool installs asynchronously and exposes pollable status."""

    _instance: "ToolInstallManager | None" = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._tool_runs: dict[str, str] = {}
        self._data_dir = Path(__file__).resolve().parent.parent / "data" / "tool_installs"
        self._data_dir.mkdir(parents=True, exist_ok=True)

    def _job_path(self, tool_id: str) -> Path:
        safe_tool_id = tool_id.replace("/", "_")
        return self._data_dir / f"{safe_tool_id}.json"

    def _save_job(self, job: dict[str, Any]) -> None:
        path = self._job_path(job["tool_id"])
        path.write_text(json.dumps(job, indent=2, sort_keys=True), encoding="utf-8")

    def _load_job_for_tool(self, tool_id: str) -> dict[str, Any] | None:
        path = self._job_path(tool_id)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else None
        except Exception:
            return None

    @classmethod
    def instance(cls) -> "ToolInstallManager":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def _job_snapshot(self, run_id: str) -> dict[str, Any] | None:
        job = self._jobs.get(run_id)
        return dict(job) if job else None

    def latest_for_tool(self, tool_id: str) -> dict[str, Any] | None:
        with self._lock:
            run_id = self._tool_runs.get(tool_id)
            if run_id:
                snap = self._job_snapshot(run_id)
                if snap is not None:
                    return snap
            return self._load_job_for_tool(tool_id)

    def status(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._job_snapshot(run_id)

    def start(self, tool: Any, plan: dict[str, Any], enable_callback) -> tuple[bool, dict[str, Any]]:
        with self._lock:
            existing_id = self._tool_runs.get(tool.id)
            if existing_id:
                existing = self._jobs.get(existing_id)
                if existing and existing.get("state") == "running":
                    return False, dict(existing)
            run_id = str(uuid.uuid4())
            job = {
                "run_id": run_id,
                "tool_id": tool.id,
                "tool_name": tool.name,
                "state": "running",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "finished_at": None,
                "current_step": 0,
                "total_steps": len(plan["commands"]),
                "command_log": [],
                "last_error": None,
                "tool_status": None,
                "enabled": False,
            }
            self._jobs[run_id] = job
            self._tool_runs[tool.id] = run_id
            self._save_job(job)
            thread = threading.Thread(
                target=self._run_install,
                args=(run_id, tool, plan, enable_callback),
                daemon=True,
                name=f"tool-install-{tool.id}",
            )
            thread.start()
            return True, dict(job)

    def _run_install(self, run_id: str, tool: Any, plan: dict[str, Any], enable_callback) -> None:
        try:
            for index, command in enumerate(plan["commands"], start=1):
                with self._lock:
                    job = self._jobs[run_id]
                    job["current_step"] = index
                    self._save_job(job)
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=_TOOL_INSTALL_TIMEOUT_SECONDS,
                    check=False,
                )
                output = (completed.stdout or "") + ("\n" if completed.stdout and completed.stderr else "") + (completed.stderr or "")
                with self._lock:
                    job = self._jobs[run_id]
                    job["command_log"].append(
                        {
                            "command": command,
                            "returncode": completed.returncode,
                            "output": output.strip(),
                        }
                    )
                    self._save_job(job)
                if completed.returncode != 0:
                    raise RuntimeError("Installer command failed.")
            status = _tool_runtime_status(tool)
            if status["status"] != "installed":
                raise RuntimeError("Installation finished but the tool still appears unavailable.")
            enable_callback(tool.id)
            with self._lock:
                job = self._jobs[run_id]
                job["state"] = "succeeded"
                job["finished_at"] = datetime.now(timezone.utc).isoformat()
                job["tool_status"] = status
                job["enabled"] = True
                self._save_job(job)
        except subprocess.TimeoutExpired:
            with self._lock:
                job = self._jobs[run_id]
                job["state"] = "failed"
                job["finished_at"] = datetime.now(timezone.utc).isoformat()
                job["last_error"] = f"Installer timed out after {_TOOL_INSTALL_TIMEOUT_SECONDS} seconds."
                self._save_job(job)
        except Exception as exc:
            with self._lock:
                job = self._jobs[run_id]
                job["state"] = "failed"
                job["finished_at"] = datetime.now(timezone.utc).isoformat()
                job["last_error"] = str(exc)
                self._save_job(job)


# ---------------------------------------------------------------------------
# UI route
# ---------------------------------------------------------------------------

@bp.get("/settings")
@login_required
def settings_page() -> str:
    """Render the settings management page (admin only)."""
    _require_admin()
    mgr = _get_mgr()
    all_settings = mgr.get_all(mask_secrets=False)
    audit_log = mgr.get_audit_log(50)
    groups = _group_by_category(all_settings)
    from dashboard.cp_client import get_cp_client

    cp = get_cp_client()
    if cp.health():
        api_keys = cp.list_keys() or []
        model_catalog = cp.list_models() or []
        projects = cp.list_projects() or []
    else:
        api_keys = []
        model_catalog = []
        projects = []
    deploy_status = DeployManager.instance().status(refresh_remote=False)
    return render_template(
        "settings.html",
        groups=groups,
        audit_log=audit_log,
        api_keys=api_keys,
        model_catalog=model_catalog,
        projects=projects,
        deploy_status=deploy_status,
        active_page="settings",
    )


# ---------------------------------------------------------------------------
# API routes — fixed paths before parameterised ones
# ---------------------------------------------------------------------------

@bp.get("/api/settings/export/yaml")
@login_required
def export_yaml():
    """Download all settings as a YAML file (secrets masked)."""
    _require_admin()
    mgr = _get_mgr()
    content = mgr.export_yaml()
    resp = make_response(content)
    resp.headers["Content-Type"] = "application/x-yaml"
    resp.headers["Content-Disposition"] = "attachment; filename=nexusai_settings.yaml"
    return resp


@bp.get("/api/settings/export/json")
@login_required
def export_json_endpoint():
    """Download all settings as a JSON file (secrets masked)."""
    _require_admin()
    mgr = _get_mgr()
    content = mgr.export_json()
    resp = make_response(content)
    resp.headers["Content-Type"] = "application/json"
    resp.headers["Content-Disposition"] = "attachment; filename=nexusai_settings.json"
    return resp


@bp.post("/api/settings/import")
@login_required
def import_settings():
    """Import settings from an uploaded YAML or JSON file."""
    _require_admin()
    if "file" not in request.files:
        return jsonify({"error": "No file provided."}), 400
    file = request.files["file"]
    filename = file.filename or ""
    raw = file.read()
    try:
        if filename.endswith((".yaml", ".yml")):
            data = yaml.safe_load(raw)
        else:
            data = json.loads(raw)
    except Exception as exc:
        return jsonify({"error": f"Failed to parse file: {exc}"}), 400
    if not isinstance(data, dict):
        return jsonify({"error": "Imported file must be a JSON/YAML object."}), 400
    mgr = _get_mgr()
    changed_by = getattr(current_user, "email", "import")
    mgr.import_from_dict(data, changed_by)
    return jsonify({"status": "ok", "imported": len(data)})


@bp.get("/api/settings")
@login_required
def list_settings():
    """Return all settings with secrets masked."""
    _require_admin()
    mgr = _get_mgr()
    return jsonify(mgr.get_all(mask_secrets=True))


@bp.get("/api/settings/keys")
@login_required
def list_api_keys():
    _require_admin()
    from dashboard.cp_client import get_cp_client

    keys = get_cp_client().list_keys()
    if keys is None:
        return jsonify({"error": "control plane unavailable"}), 502
    return jsonify(keys)


@bp.post("/api/settings/keys")
@login_required
def create_or_update_api_key():
    _require_admin()
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    provider = (body.get("provider") or "").strip()
    value = body.get("value") or ""
    if not name or not provider or not value:
        return jsonify({"error": "name, provider, and value are required"}), 400
    from dashboard.cp_client import get_cp_client

    result = get_cp_client().upsert_key(name=name, provider=provider, value=value)
    if result is None:
        return jsonify({"error": "control plane unavailable"}), 502
    return jsonify(result), 201


@bp.delete("/api/settings/keys/<name>")
@login_required
def delete_api_key(name: str):
    _require_admin()
    from dashboard.cp_client import get_cp_client

    ok = get_cp_client().delete_key(name)
    if not ok:
        return jsonify({"error": "delete failed"}), 502
    return "", 204


@bp.get("/api/settings/models")
@login_required
def list_model_catalog():
    _require_admin()
    from dashboard.cp_client import get_cp_client

    models = get_cp_client().list_models()
    if models is None:
        return jsonify({"error": "control plane unavailable"}), 502
    return jsonify(models)


@bp.post("/api/settings/models")
@login_required
def create_catalog_model():
    _require_admin()
    body = request.get_json(silent=True) or {}
    model_id = (body.get("id") or "").strip()
    name = (body.get("name") or "").strip()
    provider = (body.get("provider") or "").strip()
    if not model_id or not name or not provider:
        return jsonify({"error": "id, name, and provider are required"}), 400
    payload = {
        "id": model_id,
        "name": name,
        "provider": provider,
        "context_window": body.get("context_window"),
        "capabilities": body.get("capabilities", []),
        "input_cost_per_1k": body.get("input_cost_per_1k"),
        "output_cost_per_1k": body.get("output_cost_per_1k"),
        "notes": body.get("notes"),
        "enabled": bool(body.get("enabled", True)),
    }
    from dashboard.cp_client import get_cp_client

    created = get_cp_client().create_model(payload)
    if created is None:
        return jsonify({"error": "control plane unavailable"}), 502
    return jsonify(created), 201


@bp.delete("/api/settings/models/<model_id>")
@login_required
def delete_catalog_model(model_id: str):
    _require_admin()
    from dashboard.cp_client import get_cp_client

    ok = get_cp_client().delete_model(model_id)
    if not ok:
        return jsonify({"error": "delete failed"}), 502
    return "", 204


@bp.get("/api/settings/models/ollama-cloud-available")
@login_required
def list_ollama_cloud_available_models():
    """Proxy to /v1/models/ollama-cloud/available on the control plane.

    Returns the list of model name strings actually registered on the Ollama Cloud endpoint.
    """
    _require_admin()
    from dashboard.cp_client import get_cp_client

    result = get_cp_client().fetch_ollama_cloud_available()
    if result is None:
        return jsonify({"error": "control plane unavailable"}), 502
    return jsonify(result)


@bp.get("/api/settings/projects")
@login_required
def list_projects():
    _require_admin()
    from dashboard.cp_client import get_cp_client

    projects = get_cp_client().list_projects()
    if projects is None:
        return jsonify({"error": "control plane unavailable"}), 502
    return jsonify(projects)


@bp.get("/api/settings/deploy/status")
@login_required
def deploy_status():
    _require_admin()
    refresh_remote = request.args.get("fetch", "0") in {"1", "true", "yes"}
    return jsonify(DeployManager.instance().status(refresh_remote=refresh_remote))


@bp.post("/api/settings/deploy/check")
@login_required
def deploy_check():
    _require_admin()
    return jsonify(DeployManager.instance().status(refresh_remote=True))


@bp.post("/api/settings/deploy/run")
@login_required
def deploy_run():
    _require_admin()
    who = getattr(current_user, "email", "admin")
    ok, message = DeployManager.instance().start(requested_by=who)
    if not ok:
        return jsonify({"status": "blocked", "error": message}), 409
    return jsonify({"status": "started", "message": message}), 202


@bp.post("/api/settings/deploy/log/clear")
@login_required
def deploy_log_clear():
    _require_admin()
    manager = DeployManager.instance()
    manager.clear_log()
    return jsonify({"status": "ok", "deploy_status": manager.status(refresh_remote=False)})


# ---------------------------------------------------------------------------
# Tool Catalog endpoints
# ---------------------------------------------------------------------------

@bp.get("/api/settings/tools")
@login_required
def list_tools():
    """Return the full tool catalog with per-tool enabled status."""
    _require_admin()
    from shared.tool_catalog import (
        CATEGORY_LABELS,
        TOOL_CATALOG,
        TOOL_CATEGORIES,
        TOOL_PRESETS,
    )

    enabled_ids = set(_load_enabled_tool_ids())

    tools_out = [
        {
            "id": t.id,
            "name": t.name,
            "category": t.category,
            "category_label": CATEGORY_LABELS.get(t.category, t.category.title()),
            "description": t.description,
            "check_command": t.check_command,
            "install_hint": t.install_hint,
            "default_enabled": t.default_enabled,
            "enabled": t.id in enabled_ids,
            "install_supported": _tool_install_plan(t.id) is not None,
            "install_mode": "runtime_deploy" if _persistent_toolchains_for_tool(t.id) else "dashboard_host",
            "presets": t.presets,
        }
        for t in TOOL_CATALOG
    ]
    return jsonify(
        {
            "tools": tools_out,
            "categories": [
                {"id": c, "label": CATEGORY_LABELS.get(c, c.title())}
                for c in TOOL_CATEGORIES
            ],
            "presets": [
                {"id": k, "label": v["label"], "description": v["description"]}
                for k, v in TOOL_PRESETS.items()
            ],
            "enabled_count": sum(1 for t in tools_out if t["enabled"]),
            "total_count": len(tools_out),
        }
    )


@bp.put("/api/settings/tools")
@login_required
def update_tools_bulk():
    """Bulk-update enabled tools: body ``{\"enabled_tools\": [\"id1\", \"id2\", ...]}``."""
    _require_admin()
    from shared.tool_catalog import TOOL_CATALOG_BY_ID

    body = request.get_json(silent=True)
    if not isinstance(body, dict) or "enabled_tools" not in body:
        return jsonify({"error": "Body must contain an 'enabled_tools' list."}), 400
    raw_ids = body["enabled_tools"]
    if not isinstance(raw_ids, list):
        return jsonify({"error": "'enabled_tools' must be a list of tool ID strings."}), 400
    valid_ids = [i for i in raw_ids if isinstance(i, str) and i in TOOL_CATALOG_BY_ID]
    _save_enabled_tool_ids(valid_ids)
    return jsonify({"status": "ok", "enabled_tools": valid_ids})


@bp.put("/api/settings/tools/<tool_id>")
@login_required
def update_tool(tool_id: str):
    """Toggle a single tool on or off: body ``{\"enabled\": true|false}``."""
    _require_admin()
    from shared.tool_catalog import TOOL_CATALOG_BY_ID

    if tool_id not in TOOL_CATALOG_BY_ID:
        return jsonify({"error": f"Unknown tool ID '{tool_id}'."}), 404
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or "enabled" not in body:
        return jsonify({"error": "Body must contain an 'enabled' boolean."}), 400
    enabled_ids = _load_enabled_tool_ids()
    if body["enabled"]:
        if tool_id not in enabled_ids:
            enabled_ids.append(tool_id)
    else:
        enabled_ids = [i for i in enabled_ids if i != tool_id]
    _save_enabled_tool_ids(enabled_ids)
    return jsonify({"status": "ok", "tool_id": tool_id, "enabled": bool(body["enabled"])})


@bp.post("/api/settings/tools/preset/<preset_id>")
@login_required
def apply_tool_preset(preset_id: str):
    """Apply a tool preset: replaces enabled_tools with the preset's tool list."""
    _require_admin()
    from shared.tool_catalog import TOOL_PRESETS, tools_for_preset

    if preset_id not in TOOL_PRESETS:
        return jsonify({"error": f"Unknown preset '{preset_id}'."}), 404
    tool_ids = tools_for_preset(preset_id)
    _save_enabled_tool_ids(tool_ids)
    return jsonify({"status": "ok", "preset": preset_id, "enabled_tools": tool_ids})


@bp.post("/api/settings/tools/test")
@login_required
def test_tools():
    """Run availability checks for enabled tools, or all tools when requested."""
    _require_admin()
    from shared.tool_catalog import TOOL_CATALOG, TOOL_CATALOG_BY_ID

    body = request.get_json(silent=True) or {}
    scope = str(body.get("scope") or "enabled").strip().lower()
    tool_id = str(body.get("tool_id") or "").strip()
    enabled_ids = set(_load_enabled_tool_ids())
    if tool_id:
        tool = TOOL_CATALOG_BY_ID.get(tool_id)
        if tool is None:
            return jsonify({"error": f"Unknown tool ID '{tool_id}'."}), 404
        selected_tools = [tool]
        scope = "single"
    elif scope == "all":
        selected_tools = TOOL_CATALOG
    else:
        selected_tools = [tool for tool in TOOL_CATALOG if tool.id in enabled_ids]
    statuses = []
    for tool in selected_tools:
        statuses.append(
            {
                "id": tool.id,
                "enabled": tool.id in enabled_ids,
                "check_command": tool.check_command,
                **_tool_runtime_status(tool),
            }
        )
    if scope == "enabled" and not statuses:
        for tool in TOOL_CATALOG_BY_ID.values():
            statuses.append(
                {
                    "id": tool.id,
                    "enabled": False,
                    "check_command": tool.check_command,
                    **_tool_runtime_status(tool),
                }
            )
        scope = "all"
    return jsonify(
        {
            "scope": scope,
            "statuses": statuses,
            "checked_count": len(statuses),
        }
    )


@bp.post("/api/settings/tools/install/<tool_id>")
@login_required
def install_tool(tool_id: str):
    """Queue a supported tool install on the dashboard host and return immediately."""
    _require_admin()
    from shared.tool_catalog import TOOL_CATALOG_BY_ID

    tool = TOOL_CATALOG_BY_ID.get(tool_id)
    if tool is None:
        return jsonify({"error": f"Unknown tool ID '{tool_id}'."}), 404
    plan = _tool_install_plan(tool_id)
    if plan is None:
        return (
            jsonify(
                {
                    "error": f"No curated installer is available for '{tool_id}'.",
                    "install_hint": tool.install_hint,
                }
            ),
            400,
        )
    persistent_toolchains = _persistent_toolchains_for_tool(tool_id)
    if persistent_toolchains:
        configured = _configure_persistent_runtime_toolchains(tool_id)
        job = {
            "run_id": str(uuid.uuid4()),
            "tool_id": tool_id,
            "tool_name": tool.name,
            "state": "configured",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "current_step": 1,
            "total_steps": 1,
            "command_log": [],
            "last_error": None,
            "tool_status": {
                "status": "configured",
                "summary": "Configured persistent control_plane runtime toolchains. Redeploy to rebuild the runtime image.",
                "install_notes": "This tool is baked into the control_plane image, not installed in the dashboard container.",
            },
            "enabled": True,
            "deploy_required": True,
            "configured_toolchains": configured,
        }
        ToolInstallManager.instance()._save_job(job)
        return jsonify(job), 202

    def _enable(tool_name: str) -> None:
        enabled_ids = _load_enabled_tool_ids()
        if tool_name not in enabled_ids:
            enabled_ids.append(tool_name)
            _save_enabled_tool_ids(enabled_ids)

    started, job = ToolInstallManager.instance().start(tool, plan, _enable)
    if not started:
        return jsonify(job), 202
    return jsonify(job), 202


@bp.get("/api/settings/tools/install/<tool_id>/status")
@login_required
def tool_install_status(tool_id: str):
    """Return the most recent install status for a given tool, if any."""
    _require_admin()
    job = ToolInstallManager.instance().latest_for_tool(tool_id)
    if job is None:
        return jsonify({"error": f"No install job found for '{tool_id}'."}), 404
    return jsonify(job)


@bp.get("/api/settings/<key>")
@login_required
def get_setting(key: str):
    """Return a single setting (secret values are masked)."""
    _require_admin()
    mgr = _get_mgr()
    all_settings = mgr.get_all(mask_secrets=True)
    if key not in all_settings:
        return jsonify({"error": f"Setting '{key}' not found."}), 404
    return jsonify(all_settings[key])


@bp.post("/api/settings")
@login_required
def bulk_update_settings():
    """Bulk-update settings from a JSON body ``{key: value, ...}``."""
    _require_admin()
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "Request body must be a JSON object."}), 400
    mgr = _get_mgr()
    changed_by = getattr(current_user, "email", "api")
    mgr.import_from_dict(body, changed_by)
    return jsonify({"status": "ok", "updated": len(body)})


@bp.put("/api/settings/<key>")
@login_required
def update_setting(key: str):
    """Update a single setting value."""
    _require_admin()
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or "value" not in body:
        return jsonify({"error": "Body must contain a 'value' field."}), 400
    mgr = _get_mgr()
    changed_by = getattr(current_user, "email", "api")
    mgr.set(key, body["value"], changed_by)
    return jsonify({"status": "ok", "key": key})

