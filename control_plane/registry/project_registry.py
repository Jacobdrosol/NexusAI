import asyncio
import json
import os
from pathlib import Path
from typing import Dict, List, Optional

import aiosqlite

from shared.exceptions import ProjectNotFoundError
from shared.models import Project

_DEFAULT_DB_PATH = str(Path(__file__).parent.parent.parent / "data" / "nexusai.db")

_CREATE_PROJECTS = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL
)
"""


class ProjectRegistry:
    def __init__(self, db_path: Optional[str] = None) -> None:
        self._projects: Dict[str, Project] = {}
        self._lock = asyncio.Lock()
        self._init_lock = asyncio.Lock()
        self._db_ready = False

        if db_path is not None:
            self._db_path = db_path
        else:
            db_url = os.environ.get("DATABASE_URL", "")
            if db_url.startswith("sqlite:///"):
                self._db_path = db_url[len("sqlite:///"):]
            else:
                self._db_path = _DEFAULT_DB_PATH

    async def _ensure_db(self) -> None:
        if self._db_ready:
            return
        async with self._init_lock:
            if self._db_ready:
                return
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await db.execute(_CREATE_PROJECTS)
                await db.commit()
                async with db.execute("SELECT id, data FROM projects") as cursor:
                    rows = await cursor.fetchall()
                    for row in rows:
                        self._projects[row["id"]] = Project.model_validate(json.loads(row["data"]))
            self._db_ready = True

    async def _persist_project(self, project: Project) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO projects (id, data)
                VALUES (?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    data = excluded.data
                """,
                (project.id, json.dumps(project.model_dump())),
            )
            await db.commit()

    async def _delete_project(self, project_id: str) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("DELETE FROM projects WHERE id = ?", (project_id,))
            await db.commit()

    async def register(self, project: Project) -> None:
        await self._ensure_db()
        self._validate_project(project)
        async with self._lock:
            self._projects[project.id] = project
        await self._persist_project(project)

    async def get(self, project_id: str) -> Project:
        await self._ensure_db()
        async with self._lock:
            project = self._projects.get(project_id)
            if project is None:
                raise ProjectNotFoundError(f"Project not found: {project_id}")
            return project

    async def list(self) -> List[Project]:
        await self._ensure_db()
        async with self._lock:
            return list(self._projects.values())

    async def update(self, project_id: str, project: Project) -> None:
        await self._ensure_db()
        self._validate_project(project)
        async with self._lock:
            if project_id not in self._projects:
                raise ProjectNotFoundError(f"Project not found: {project_id}")
            self._projects[project_id] = project
        await self._persist_project(project)

    async def remove(self, project_id: str) -> None:
        await self._ensure_db()
        async with self._lock:
            if project_id not in self._projects:
                raise ProjectNotFoundError(f"Project not found: {project_id}")
            for other_id, other in self._projects.items():
                if other_id == project_id:
                    continue
                if project_id in other.bridge_project_ids:
                    updated = [
                        bridged_id for bridged_id in other.bridge_project_ids if bridged_id != project_id
                    ]
                    self._projects[other_id] = other.model_copy(update={"bridge_project_ids": updated})
            del self._projects[project_id]
            projects_to_persist = list(self._projects.values())

        for project in projects_to_persist:
            await self._persist_project(project)
        await self._delete_project(project_id)

    async def add_bridge(self, source_project_id: str, target_project_id: str) -> None:
        if source_project_id == target_project_id:
            raise ValueError("A project cannot be bridged to itself")

        await self._ensure_db()
        async with self._lock:
            source = self._projects.get(source_project_id)
            target = self._projects.get(target_project_id)
            if source is None:
                raise ProjectNotFoundError(f"Project not found: {source_project_id}")
            if target is None:
                raise ProjectNotFoundError(f"Project not found: {target_project_id}")
            if source.mode == "isolated" or target.mode == "isolated":
                raise ValueError("Both projects must be in 'bridged' mode to create a bridge")

            source_links = set(source.bridge_project_ids)
            target_links = set(target.bridge_project_ids)
            source_links.add(target_project_id)
            target_links.add(source_project_id)

            updated_source = source.model_copy(update={"bridge_project_ids": sorted(source_links)})
            updated_target = target.model_copy(update={"bridge_project_ids": sorted(target_links)})
            self._projects[source_project_id] = updated_source
            self._projects[target_project_id] = updated_target

        await self._persist_project(updated_source)
        await self._persist_project(updated_target)

    async def remove_bridge(self, source_project_id: str, target_project_id: str) -> None:
        if source_project_id == target_project_id:
            raise ValueError("A project cannot unbridge from itself")

        await self._ensure_db()
        async with self._lock:
            source = self._projects.get(source_project_id)
            target = self._projects.get(target_project_id)
            if source is None:
                raise ProjectNotFoundError(f"Project not found: {source_project_id}")
            if target is None:
                raise ProjectNotFoundError(f"Project not found: {target_project_id}")

            source_links = [pid for pid in source.bridge_project_ids if pid != target_project_id]
            target_links = [pid for pid in target.bridge_project_ids if pid != source_project_id]
            updated_source = source.model_copy(update={"bridge_project_ids": source_links})
            updated_target = target.model_copy(update={"bridge_project_ids": target_links})
            self._projects[source_project_id] = updated_source
            self._projects[target_project_id] = updated_target

        await self._persist_project(updated_source)
        await self._persist_project(updated_target)

    def _validate_project(self, project: Project) -> None:
        if project.id in project.bridge_project_ids:
            raise ValueError("A project cannot list itself as a bridge")
        if project.mode == "isolated" and project.bridge_project_ids:
            raise ValueError("Isolated projects cannot have bridge_project_ids")
