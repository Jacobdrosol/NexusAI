"""Unit tests for ProjectRegistry."""

import pytest

from shared.exceptions import ProjectNotFoundError
from shared.models import Project


@pytest.mark.anyio
async def test_register_and_get(tmp_path):
    from control_plane.registry.project_registry import ProjectRegistry

    reg = ProjectRegistry(db_path=str(tmp_path / "projects.db"))
    p = Project(id="p1", name="Project 1", mode="isolated")
    await reg.register(p)
    result = await reg.get("p1")
    assert result.id == "p1"


@pytest.mark.anyio
async def test_get_not_found(tmp_path):
    from control_plane.registry.project_registry import ProjectRegistry

    reg = ProjectRegistry(db_path=str(tmp_path / "projects.db"))
    with pytest.raises(ProjectNotFoundError):
        await reg.get("missing")


@pytest.mark.anyio
async def test_add_bridge_bidirectional(tmp_path):
    from control_plane.registry.project_registry import ProjectRegistry

    reg = ProjectRegistry(db_path=str(tmp_path / "projects.db"))
    await reg.register(Project(id="p1", name="One", mode="bridged"))
    await reg.register(Project(id="p2", name="Two", mode="bridged"))

    await reg.add_bridge("p1", "p2")

    p1 = await reg.get("p1")
    p2 = await reg.get("p2")
    assert p1.bridge_project_ids == ["p2"]
    assert p2.bridge_project_ids == ["p1"]


@pytest.mark.anyio
async def test_add_bridge_rejects_isolated_projects(tmp_path):
    from control_plane.registry.project_registry import ProjectRegistry

    reg = ProjectRegistry(db_path=str(tmp_path / "projects.db"))
    await reg.register(Project(id="p1", name="One", mode="isolated"))
    await reg.register(Project(id="p2", name="Two", mode="bridged"))

    with pytest.raises(ValueError):
        await reg.add_bridge("p1", "p2")
