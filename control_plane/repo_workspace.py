from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timezone
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover - psutil is available in normal runtime
    psutil = None


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


def _collect_tree_usage(pid: int) -> Dict[str, float]:
    if psutil is None:
        return {}
    try:
        root = psutil.Process(pid)
    except Exception:
        return {}
    processes = [root]
    try:
        processes.extend(root.children(recursive=True))
    except Exception:
        pass

    rss = 0
    vms = 0
    cpu_user = 0.0
    cpu_system = 0.0
    io_read = 0
    io_write = 0
    for proc in processes:
        try:
            mi = proc.memory_info()
            rss += int(getattr(mi, "rss", 0) or 0)
            vms += int(getattr(mi, "vms", 0) or 0)
        except Exception:
            pass
        try:
            ct = proc.cpu_times()
            cpu_user += float(getattr(ct, "user", 0.0) or 0.0)
            cpu_system += float(getattr(ct, "system", 0.0) or 0.0)
        except Exception:
            pass
        try:
            io = proc.io_counters()
            io_read += int(getattr(io, "read_bytes", 0) or 0)
            io_write += int(getattr(io, "write_bytes", 0) or 0)
        except Exception:
            pass

    return {
        "rss_bytes": float(rss),
        "vms_bytes": float(vms),
        "cpu_user_seconds": float(cpu_user),
        "cpu_system_seconds": float(cpu_system),
        "io_read_bytes": float(io_read),
        "io_write_bytes": float(io_write),
    }


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

    sample_interval_ms = _env_int(
        "NEXUSAI_REPO_WORKSPACE_METRICS_SAMPLE_MS",
        200,
        minimum=50,
        maximum=5000,
    )

    def _runner() -> Dict[str, Any]:
        started_at = datetime.now(timezone.utc).isoformat()
        start_monotonic = time.monotonic()
        command = [str(a) for a in args]
        proc = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )

        monitor_stop = threading.Event()
        monitor_metrics: Dict[str, float] = {
            "peak_rss_bytes": 0.0,
            "peak_vms_bytes": 0.0,
            "peak_cpu_user_seconds": 0.0,
            "peak_cpu_system_seconds": 0.0,
            "peak_io_read_bytes": 0.0,
            "peak_io_write_bytes": 0.0,
            "samples": 0.0,
        }

        def _monitor() -> None:
            if psutil is None:
                return
            interval = max(0.05, float(sample_interval_ms) / 1000.0)
            while not monitor_stop.is_set():
                snapshot = _collect_tree_usage(proc.pid)
                if snapshot:
                    monitor_metrics["peak_rss_bytes"] = max(
                        monitor_metrics["peak_rss_bytes"],
                        float(snapshot.get("rss_bytes") or 0.0),
                    )
                    monitor_metrics["peak_vms_bytes"] = max(
                        monitor_metrics["peak_vms_bytes"],
                        float(snapshot.get("vms_bytes") or 0.0),
                    )
                    monitor_metrics["peak_cpu_user_seconds"] = max(
                        monitor_metrics["peak_cpu_user_seconds"],
                        float(snapshot.get("cpu_user_seconds") or 0.0),
                    )
                    monitor_metrics["peak_cpu_system_seconds"] = max(
                        monitor_metrics["peak_cpu_system_seconds"],
                        float(snapshot.get("cpu_system_seconds") or 0.0),
                    )
                    monitor_metrics["peak_io_read_bytes"] = max(
                        monitor_metrics["peak_io_read_bytes"],
                        float(snapshot.get("io_read_bytes") or 0.0),
                    )
                    monitor_metrics["peak_io_write_bytes"] = max(
                        monitor_metrics["peak_io_write_bytes"],
                        float(snapshot.get("io_write_bytes") or 0.0),
                    )
                    monitor_metrics["samples"] = monitor_metrics["samples"] + 1.0
                if proc.poll() is not None:
                    break
                time.sleep(interval)

        monitor_thread = threading.Thread(target=_monitor, daemon=True)
        monitor_thread.start()

        timed_out = False
        timeout_error = ""
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            timeout_error = f"command timed out after {timeout} seconds"
            try:
                proc.kill()
            except Exception:
                pass
            stdout, stderr = proc.communicate()
        finally:
            monitor_stop.set()
            monitor_thread.join(timeout=2.0)

        end_monotonic = time.monotonic()
        finished_at = datetime.now(timezone.utc).isoformat()
        wall_time_ms = max(0, int((end_monotonic - start_monotonic) * 1000))
        result = {
            "ok": (proc.returncode == 0) and (not timed_out),
            "returncode": int(proc.returncode) if proc.returncode is not None else None,
            "stdout": (stdout or "")[:max_output_chars],
            "stderr": (stderr or "")[:max_output_chars],
            "command": redact_command(command),
            "timeout_seconds": timeout,
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_ms": wall_time_ms,
            "resource_usage": {
                "wall_time_ms": wall_time_ms,
                "cpu_user_seconds": round(float(monitor_metrics["peak_cpu_user_seconds"]), 6),
                "cpu_system_seconds": round(float(monitor_metrics["peak_cpu_system_seconds"]), 6),
                "peak_rss_bytes": int(monitor_metrics["peak_rss_bytes"]),
                "peak_vms_bytes": int(monitor_metrics["peak_vms_bytes"]),
                "io_read_bytes": int(monitor_metrics["peak_io_read_bytes"]),
                "io_write_bytes": int(monitor_metrics["peak_io_write_bytes"]),
                "sample_count": int(monitor_metrics["samples"]),
            },
        }
        if timed_out:
            result["error"] = timeout_error
        return result

    try:
        return await asyncio.to_thread(_runner)
    except FileNotFoundError:
        return {
            "ok": False,
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "command": redact_command(args),
            "timeout_seconds": timeout,
            "error": "command not found",
            "resource_usage": {"wall_time_ms": 0},
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
            "resource_usage": {"wall_time_ms": 0},
        }
