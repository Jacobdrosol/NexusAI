from pathlib import Path
import subprocess

from scripts.verify_public_release import find_public_release_violations


def test_tracked_repository_files_are_safe_to_publish() -> None:
    repo_root = Path(__file__).resolve().parents[1]

    assert find_public_release_violations(repo_root) == []


def _tracked_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test User"], check=True)
    return tmp_path


def test_release_hygiene_rejects_tracked_database_files(tmp_path: Path) -> None:
    repo_root = _tracked_repo(tmp_path)
    (repo_root / "scratch.db").write_bytes(b"SQLite format 3\x00")
    subprocess.run(["git", "-C", str(repo_root), "add", "scratch.db"], check=True)

    assert find_public_release_violations(repo_root) == ["tracked database file: scratch.db"]


def test_release_hygiene_rejects_tracked_temporary_probe_files(tmp_path: Path) -> None:
    repo_root = _tracked_repo(tmp_path)
    probe_dir = repo_root / "tmp_pm_probe"
    probe_dir.mkdir()
    (probe_dir / "notes.txt").write_text("temporary", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_root), "add", "tmp_pm_probe/notes.txt"], check=True)

    assert find_public_release_violations(repo_root) == [
        "tracked temporary probe file: tmp_pm_probe/notes.txt"
    ]
