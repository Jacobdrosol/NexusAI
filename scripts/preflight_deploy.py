"""Preflight checks for blue/green deployment readiness."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _ok(msg: str) -> None:
    print(f"[OK] {msg}")


def _fail(msg: str) -> None:
    print(f"[FAIL] {msg}")


def _warn(msg: str) -> None:
    print(f"[WARN] {msg}")


def _run(cmd: list[str]) -> tuple[bool, str]:
    try:
        cp = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, check=True)
        return True, (cp.stdout or "").strip()
    except Exception as exc:
        return False, str(exc)


def main() -> int:
    errors = 0

    required_files = [
        ROOT / "docker-compose.bluegreen.yml",
        ROOT / "scripts" / "deploy-bluegreen.sh",
        ROOT / "scripts" / "switch-dashboard-color.sh",
        ROOT / "deploy" / "nginx" / "default.conf",
        ROOT / "deploy" / "nginx" / "default.blue.conf",
        ROOT / "deploy" / "nginx" / "default.green.conf",
    ]
    for path in required_files:
        if path.exists():
            _ok(f"found {path.relative_to(ROOT)}")
        else:
            _fail(f"missing {path.relative_to(ROOT)}")
            errors += 1

    if shutil.which("docker"):
        _ok("docker binary found")
    else:
        _fail("docker binary not found in PATH")
        errors += 1

    ok, out = _run(["docker", "compose", "version"])
    if ok:
        _ok("docker compose is available")
    else:
        _fail(f"docker compose not available: {out}")
        errors += 1

    ok, out = _run(["docker", "compose", "-f", "docker-compose.bluegreen.yml", "config"])
    if ok:
        _ok("docker-compose.bluegreen.yml parses successfully")
    else:
        _fail(f"blue/green compose parse failed: {out}")
        errors += 1

    env_path = ROOT / ".env"
    if not env_path.exists():
        _warn(".env not found; copy from .env.example before deploying")
    else:
        content = env_path.read_text(encoding="utf-8", errors="ignore")
        for key in [
            "NEXUSAI_DEPLOY_ENABLE",
            "NEXUSAI_DEPLOY_STRATEGY",
            "NEXUSAI_DEPLOY_RUN_CMD",
            "NEXUSAI_BLUEGREEN_SWITCH_CMD",
        ]:
            if key in content:
                _ok(f".env contains {key}")
            else:
                _warn(f".env missing {key}")

    if errors:
        print(f"\nPreflight failed with {errors} error(s).")
        return 1

    print("\nPreflight passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
