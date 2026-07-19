from pathlib import Path

from scripts.verify_public_release import find_public_release_violations


def test_tracked_repository_files_are_safe_to_publish() -> None:
    repo_root = Path(__file__).resolve().parents[1]

    assert find_public_release_violations(repo_root) == []
