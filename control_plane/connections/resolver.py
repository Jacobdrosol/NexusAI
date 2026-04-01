from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional


_DEFAULT_DB_PATH = str(Path(__file__).parent.parent.parent / "data" / "nexusai.db")


def _sqlite_db_path() -> str:
    db_url = str(os.environ.get("DATABASE_URL", "") or "").strip()
    if db_url.startswith("sqlite:///"):
        return db_url[len("sqlite:///") :]
    return _DEFAULT_DB_PATH


def _parse_json(raw: Any, default: Any) -> Any:
    text = str(raw or "").strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except Exception:
        return default


def _dict_row(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    return {str(key): row[key] for key in row.keys()}


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except Exception:
        pass
    return getattr(row, key, default)


class ConnectionResolver:
    """Database-backed connection resolver without dashboard ORM imports."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = db_path or _sqlite_db_path()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _legacy_linked_connections(
        self,
        *,
        bot_ref: Optional[str] = None,
        project_ref: Optional[str] = None,
    ) -> List[Any]:
        try:
            from dashboard.db import get_db
            from dashboard.models import BotConnection, Connection, ProjectConnection
        except Exception:
            return []

        session = None
        try:
            session = get_db()
            if session is None:
                return []
            link_rows: List[Any] = []
            if bot_ref is not None:
                link_rows = list(
                    session.query(BotConnection).filter(BotConnection.bot_ref == str(bot_ref)).all() or []
                )
            elif project_ref is not None:
                link_rows = list(
                    session.query(ProjectConnection).filter(ProjectConnection.project_ref == str(project_ref)).all() or []
                )
            connection_ids: List[int] = []
            for row in link_rows:
                try:
                    connection_ids.append(int(_row_value(row, "connection_id", 0) or 0))
                except Exception:
                    continue
            connection_ids = [item for item in connection_ids if item > 0]
            if not connection_ids:
                return []
            query = session.query(Connection).filter(Connection.id.in_(connection_ids))
            try:
                query = query.order_by(Connection.name.asc())
            except Exception:
                pass
            return list(query.all() or [])
        except Exception:
            return []
        finally:
            try:
                if session is not None and hasattr(session, "close"):
                    session.close()
            except Exception:
                pass

    def list_project_connections(self, project_id: str) -> List[Dict[str, Any]]:
        project_ref = str(project_id or "").strip()
        if not project_ref:
            return []
        rows: List[Any] = []
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT c.id, c.name, c.kind, c.description, c.config_json, c.auth_json, c.schema_text, c.enabled
                    FROM project_connections pc
                    JOIN connections c ON c.id = pc.connection_id
                    WHERE pc.project_ref = ?
                    ORDER BY c.name ASC
                    """,
                    (project_ref,),
                ).fetchall()
        except Exception:
            rows = []
        if not rows:
            rows = self._legacy_linked_connections(project_ref=project_ref)
        result: List[Dict[str, Any]] = []
        for row in rows:
            item = _dict_row(row) if isinstance(row, sqlite3.Row) else None
            result.append(
                {
                    "id": int(_row_value(item or row, "id", 0) or 0),
                    "name": str(_row_value(item or row, "name", "") or ""),
                    "kind": str(_row_value(item or row, "kind", "") or ""),
                    "description": str(_row_value(item or row, "description", "") or ""),
                    "enabled": bool(_row_value(item or row, "enabled", True)),
                }
            )
        return result

    def get_project_connection(self, project_id: str, connection_id: int) -> Optional[Dict[str, Any]]:
        project_ref = str(project_id or "").strip()
        if not project_ref:
            return None
        try:
            connection_ref = int(connection_id)
        except Exception:
            return None
        row: Any = None
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT c.id, c.name, c.kind, c.description, c.config_json, c.auth_json, c.schema_text, c.enabled
                    FROM project_connections pc
                    JOIN connections c ON c.id = pc.connection_id
                    WHERE pc.project_ref = ? AND c.id = ?
                    LIMIT 1
                    """,
                    (project_ref, connection_ref),
                ).fetchone()
        except Exception:
            row = None
        if row is None:
            for legacy in self._legacy_linked_connections(project_ref=project_ref):
                if str(_row_value(legacy, "id", "")) == str(connection_ref):
                    row = legacy
                    break
        item = _dict_row(row) if isinstance(row, sqlite3.Row) else None
        if row is None and item is None:
            return None
        source = item or row
        return {
            "id": int(_row_value(source, "id", 0) or 0),
            "name": str(_row_value(source, "name", "") or ""),
            "kind": str(_row_value(source, "kind", "") or ""),
            "description": str(_row_value(source, "description", "") or ""),
            "config": _parse_json(_row_value(source, "config_json", "{}"), {}),
            "auth": _parse_json(_row_value(source, "auth_json", "{}"), {}),
            "schema_text": str(_row_value(source, "schema_text", "") or ""),
            "enabled": bool(_row_value(source, "enabled", True)),
        }

    def list_bot_connections(self, bot_id: str) -> List[Dict[str, Any]]:
        bot_ref = str(bot_id or "").strip()
        if not bot_ref:
            return []
        rows: List[Any] = []
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT c.id, c.name, c.kind, c.description, c.config_json, c.auth_json, c.schema_text, c.enabled
                    FROM bot_connections bc
                    JOIN connections c ON c.id = bc.connection_id
                    WHERE bc.bot_ref = ? AND c.enabled = 1
                    ORDER BY c.name ASC
                    """,
                    (bot_ref,),
                ).fetchall()
        except Exception:
            rows = []
        if not rows:
            rows = self._legacy_linked_connections(bot_ref=bot_ref)
        payloads: List[Dict[str, Any]] = []
        for row in rows:
            item = _dict_row(row) if isinstance(row, sqlite3.Row) else None
            source = item or row
            payloads.append(
                {
                    "id": int(_row_value(source, "id", 0) or 0),
                    "name": str(_row_value(source, "name", "") or ""),
                    "kind": str(_row_value(source, "kind", "") or ""),
                    "description": str(_row_value(source, "description", "") or ""),
                    "config": _parse_json(_row_value(source, "config_json", "{}"), {}),
                    "auth": _parse_json(_row_value(source, "auth_json", "{}"), {}),
                    "schema_text": str(_row_value(source, "schema_text", "") or ""),
                    "enabled": bool(_row_value(source, "enabled", True)),
                }
            )
        return payloads

    def find_bot_connection(
        self,
        bot_id: str,
        *,
        requested_name: Optional[str] = None,
        requested_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        rows = self.list_bot_connections(bot_id)
        requested_name_norm = str(requested_name or "").strip().lower()
        requested_id_norm = str(requested_id or "").strip()
        if requested_id_norm:
            for row in rows:
                if str(row.get("id") or "") == requested_id_norm:
                    return row
        if requested_name_norm:
            for row in rows:
                if str(row.get("name") or "").strip().lower() == requested_name_norm:
                    return row
        if len(rows) == 1:
            return rows[0]
        return None
