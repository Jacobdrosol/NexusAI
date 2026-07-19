from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_ci_workflow_is_discoverable_and_runs_public_release_hygiene() -> None:
    workflow_dir = ROOT / ".github" / "workflows"
    workflow = workflow_dir / "ci.yml"

    assert workflow.is_file()
    assert not (workflow_dir / "ci.txt").exists()
    assert "python scripts/verify_public_release.py" in workflow.read_text(encoding="utf-8")
