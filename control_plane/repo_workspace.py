from __future__ import annotations

import asyncio
import base64
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int((os.environ.get(name, "") or "").strip() or default)
    except Exception:
        value = default
    return max(minimum, min(value, maximum))


def normalize_workspace_root(path_value: Optional[str]) -> Optional[Path]:
    raw = str(path_value or "").strip()
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        return None
    try:
        return candidate.resolve(strict=False)
    except Exception:
        return None


def is_within_workspace(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except Exception:
        return False


def build_github_http_auth_header(token: str) -> str:
    # GitHub accepts x-access-token basic auth over HTTPS.
    raw = f"x-access-token:{token}".encode("utf-8")
    encoded = base64.b64encode(raw).decode("ascii")
    return f"AUTHORIZATION: basic {encoded}"


def _redact_arg(arg: str) -> str:
    lowered = arg.lower()
    if "http.extraheader=" in lowered and "authorization:" in lowered:
        key = arg.split("=", 1)[0]
        return f"{key}=REDACTED"
    return arg


def redact_command(args: Iterable[str]) -> List[str]:
    return [_redact_arg(str(a)) for a in args]


async def run_command(
    args: List[str],
    *,
    cwd: Path,
    timeout_seconds: Optional[int] = None,
    env_overrides: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    timeout = timeout_seconds
    if timeout is None:
        timeout = _env_int("NEXUSAI_REPO_WORKSPACE_DEFAULT_TIMEOUT_SECONDS", 120, minimum=5, maximum=3600)
    timeout = max(1, int(timeout))
    max_output_chars = _env_int(
        "NEXUSAI_REPO_WORKSPACE_MAX_OUTPUT_CHARS",
        20000,
        minimum=1000,
        maximum=200000,
    )

    env = dict(os.environ)
    if env_overrides:
        env.update({str(k): str(v) for k, v in env_overrides.items()})

    def _runner() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(a) for a in args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env=env,
        )

    try:
        proc = await asyncio.to_thread(_runner)
        stdout = (proc.stdout or "")[:max_output_chars]
        stderr = (proc.stderr or "")[:max_output_chars]
        return {
            "ok": proc.returncode == 0,
            "returncode": int(proc.returncode),
            "stdout": stdout,
            "stderr": stderr,
            "command": redact_command(args),
            "timeout_seconds": timeout,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "returncode": None,
            "stdout": str(exc.stdout or "")[:max_output_chars],
            "stderr": str(exc.stderr or "")[:max_output_chars],
            "command": redact_command(args),
            "timeout_seconds": timeout,
            "error": f"command timed out after {timeout} seconds",
        }
    except FileNotFoundError:
        return {
            "ok": False,
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "command": redact_command(args),
            "timeout_seconds": timeout,
            "error": "command not found",
        }
    except Exception as exc:
        return {
            "ok": False,
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "command": redact_command(args),
            "timeout_seconds": timeout,
            "error": str(exc),
        }
