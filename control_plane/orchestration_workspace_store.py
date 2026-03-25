import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class OrchestrationWorkspaceStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._entries: Dict[Tuple[str, str], Dict[str, Any]] = {}

    def _key(self, project_id: str, orchestration_id: str) -> Tuple[str, str]:
        return (str(project_id or "").strip(), str(orchestration_id or "").strip())

    def _public_entry(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        entry = dict(raw)
        temp_root = str(entry.get("temp_root") or "").strip()
        path_exists = bool(temp_root) and Path(temp_root).exists()
        entry["path_exists"] = path_exists
        lifecycle_state = str(entry.get("lifecycle_state") or "unavailable").strip() or "unavailable"
        if lifecycle_state == "retained" and not path_exists:
            lifecycle_state = "lost"
        entry["lifecycle_state"] = lifecycle_state
        if lifecycle_state == "lost" and not entry.get("availability_reason"):
            entry["availability_reason"] = "temp_workspace_missing"
        return entry

    async def register(
        self,
        *,
        project_id: str,
        orchestration_id: str,
        source_root: str,
        temp_root: str,
        mode: str,
    ) -> Dict[str, Any]:
        now = _utc_now()
        entry = {
            "project_id": str(project_id or "").strip(),
            "orchestration_id": str(orchestration_id or "").strip(),
            "source_root": str(source_root or "").strip(),
            "temp_root": str(temp_root or "").strip(),
            "mode": str(mode or "").strip(),
            "workspace_source": "orchestration_temp",
            "lifecycle_state": "retained",
            "availability_reason": "available",
            "created_at": now,
            "updated_at": now,
            "retained_until_apply": True,
            "deleted_at": None,
            "cleanup_reason": None,
            "applied_at": None,
        }
        async with self._lock:
            self._entries[self._key(project_id, orchestration_id)] = entry
        return self._public_entry(entry)

    async def get(self, *, project_id: str, orchestration_id: str) -> Optional[Dict[str, Any]]:
        async with self._lock:
            raw = self._entries.get(self._key(project_id, orchestration_id))
        if raw is None:
            return None
        return self._public_entry(raw)

    async def list_for_project(self, project_id: str) -> List[Dict[str, Any]]:
        project_key = str(project_id or "").strip()
        async with self._lock:
            rows = [dict(value) for value in self._entries.values() if str(value.get("project_id") or "").strip() == project_key]
        rows = [self._public_entry(row) for row in rows]
        rows.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return rows

    async def mark_deleted(
        self,
        *,
        project_id: str,
        orchestration_id: str,
        reason: str,
    ) -> Optional[Dict[str, Any]]:
        key = self._key(project_id, orchestration_id)
        async with self._lock:
            raw = self._entries.get(key)
            if raw is None:
                return None
            raw = dict(raw)
            raw["lifecycle_state"] = "deleted"
            raw["availability_reason"] = str(reason or "deleted").strip() or "deleted"
            raw["cleanup_reason"] = str(reason or "deleted").strip() or "deleted"
            raw["deleted_at"] = _utc_now()
            raw["updated_at"] = raw["deleted_at"]
            self._entries[key] = raw
        return self._public_entry(raw)

    async def mark_applied(self, *, project_id: str, orchestration_id: str) -> Optional[Dict[str, Any]]:
        key = self._key(project_id, orchestration_id)
        async with self._lock:
            raw = self._entries.get(key)
            if raw is None:
                return None
            raw = dict(raw)
            raw["lifecycle_state"] = "applied"
            raw["availability_reason"] = "applied_to_project_repo"
            raw["applied_at"] = _utc_now()
            raw["updated_at"] = raw["applied_at"]
            self._entries[key] = raw
        return self._public_entry(raw)
