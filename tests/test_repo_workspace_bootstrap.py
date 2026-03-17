from pathlib import Path
import sys

from control_plane.api import projects as projects_module


def test_python_bootstrap_uses_current_interpreter() -> None:
    specs = projects_module._bootstrap_command_specs(Path.cwd(), ["python"])
    create_spec = next(spec for spec in specs if spec.get("label") == "python_venv_create")
    assert create_spec["command"][0] == sys.executable


def test_allowed_workspace_commands_include_current_interpreter_name() -> None:
    allowed = projects_module._allowed_workspace_commands()
    assert Path(sys.executable).name.lower() in allowed or Path(sys.executable).stem.lower() in allowed
