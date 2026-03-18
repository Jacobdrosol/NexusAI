from pathlib import Path
import sys

from control_plane.api import projects as projects_module


def test_python_bootstrap_uses_current_interpreter() -> None:
    specs = projects_module._bootstrap_command_specs(Path.cwd(), ["python"])
    create_spec = next(spec for spec in specs if spec.get("label") == "python_venv_create")
    assert create_spec["command"][0] == sys.executable


def test_python_bootstrap_installs_pytest_and_cov_tools() -> None:
    specs = projects_module._bootstrap_command_specs(Path.cwd(), ["python"])
    install_spec = next(spec for spec in specs if spec.get("label") == "python_install_test_tools")

    assert install_spec["command"][-2:] == ["pytest", "pytest-cov"]


def test_allowed_workspace_commands_include_current_interpreter_name() -> None:
    allowed = projects_module._allowed_workspace_commands()
    assert Path(sys.executable).name.lower() in allowed or Path(sys.executable).stem.lower() in allowed


def test_allowed_workspace_commands_include_cpp_test_tool() -> None:
    allowed = projects_module._allowed_workspace_commands()
    assert "ctest" in allowed


def test_detect_bootstrap_languages_includes_go_rust_and_cpp(tmp_path) -> None:
    (tmp_path / "go.mod").write_text("module example.com/demo\n", encoding="utf-8")
    (tmp_path / "Cargo.toml").write_text("[package]\nname='demo'\nversion='0.1.0'\n", encoding="utf-8")
    (tmp_path / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.20)\n", encoding="utf-8")

    detected = projects_module._detect_bootstrap_languages(tmp_path)

    assert "go" in detected
    assert "rust" in detected
    assert "cpp" in detected


def test_bootstrap_specs_include_go_rust_and_cpp_steps(tmp_path) -> None:
    (tmp_path / "go.mod").write_text("module example.com/demo\n", encoding="utf-8")
    (tmp_path / "Cargo.toml").write_text("[package]\nname='demo'\nversion='0.1.0'\n", encoding="utf-8")
    (tmp_path / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.20)\n", encoding="utf-8")

    specs = projects_module._bootstrap_command_specs(tmp_path, ["go", "rust", "cpp"])
    labels = [str(spec.get("label") or "") for spec in specs]

    assert "go_mod_download" in labels
    assert "cargo_fetch" in labels
    assert "cpp_cmake_configure" in labels
    assert "cpp_cmake_build" in labels
