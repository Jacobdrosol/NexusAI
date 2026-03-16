"""SettingsManager: SQLite-backed runtime settings for NexusAI.

This module provides a thread-safe singleton :class:`SettingsManager` that
stores all application settings in a SQLite database so they can be edited
at runtime from the dashboard without restarting any service.

It is importable by ``dashboard``, ``control_plane``, and ``worker_agent``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------
_CREATE_SETTINGS = """
CREATE TABLE IF NOT EXISTS nexus_settings (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    value_type  TEXT DEFAULT 'string',
    category    TEXT DEFAULT 'general',
    label       TEXT,
    description TEXT,
    updated_at  DATETIME,
    updated_by  TEXT
)
"""

_CREATE_AUDIT = """
CREATE TABLE IF NOT EXISTS nexus_settings_audit (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    key        TEXT,
    old_value  TEXT,
    new_value  TEXT,
    changed_by TEXT,
    changed_at DATETIME
)
"""

# ---------------------------------------------------------------------------
# Default settings catalogue
# (key, default_value, value_type, category, label, description)
# ---------------------------------------------------------------------------
_DEFAULTS: List[tuple] = [
    # General
    ("site_name", "NexusAI", "string", "general", "Site Name",
     "Display name shown in the dashboard header."),
    ("site_tagline", "", "string", "general", "Site Tagline",
     "Short subtitle displayed on the dashboard."),
    ("control_plane_host", "localhost", "string", "general", "Control Plane Host",
     "Hostname or IP of the control-plane service."),
    ("control_plane_port", "8000", "int", "general", "Control Plane Port",
     "TCP port the control-plane listens on."),
    # Auth
    ("session_secret_key", "", "secret", "auth", "Session Secret Key",
     "Secret used to sign sessions. Keep this private."),
    ("session_timeout_minutes", "60", "int", "auth", "Session Timeout (minutes)",
     "Idle session expiry time in minutes."),
    ("allow_user_registration", "false", "bool", "auth", "Allow User Registration",
     "Whether new users can self-register."),
    # LLM / Workers
    ("default_llm_host", "http://localhost:11434", "string", "llm",
     "Default LLM Host", "Base URL of the default LLM backend (e.g. Ollama)."),
    ("default_llm_model", "llama3.2:latest", "string", "llm",
     "Default LLM Model", "Model name used when no explicit model is specified."),
    ("default_embedding_model", "nomic-embed-text", "string", "llm",
     "Default Embedding Model", "Model name used for text embeddings."),
    ("worker_heartbeat_interval", "30", "int", "llm",
     "Worker Heartbeat Interval (s)", "Seconds between worker heartbeat pings."),
    ("cloud_backend_timeout_seconds", "900", "int", "llm",
     "Cloud Backend Timeout (s)",
     "Timeout in seconds for cloud model API calls used by orchestration and bot runs."),
    # Logging
    ("log_level", "INFO", "string", "logging", "Log Level",
     "Logging verbosity: DEBUG, INFO, WARNING, or ERROR."),
    ("log_to_file", "true", "bool", "logging", "Log to File",
     "Whether to write log output to a file."),
    ("log_file_path", "data/nexusai.log", "string", "logging", "Log File Path",
     "Filesystem path for the log file."),
    # Advanced
    ("max_task_retries", "3", "int", "advanced", "Max Task Retries",
     "Maximum number of retry attempts for a failed task."),
    ("task_retry_delay", "5.0", "float", "advanced", "Task Retry Delay (s)",
     "Seconds to wait between task retry attempts."),
    ("task_retry_max_tokens_increment", "512", "int", "advanced", "Task Retry Max Tokens Increment",
     "Additional max_tokens applied for each retry attempt when a backend already defines max_tokens."),
    ("task_retry_num_width_increment", "256", "int", "advanced", "Task Retry Num Width Increment",
     "Additional num_width applied for each retry attempt. If num_width is unset, the same increment is applied to num_ctx."),
    ("bot_trigger_max_depth", "20", "int", "advanced", "Bot Trigger Max Depth",
     "Maximum number of chained bot-trigger hops allowed in one workflow or pipeline run."),
    ("external_trigger_default_auth_header", "X-Nexus-Trigger-Token", "string", "advanced",
     "External Trigger Auth Header",
     "Default header name checked for per-bot external trigger token authentication."),
    ("external_trigger_default_source", "external_trigger", "string", "advanced",
     "External Trigger Source Label",
     "Default task metadata source value for tasks created by external trigger intake."),
    ("external_trigger_max_body_bytes", "1000000", "int", "advanced",
     "External Trigger Max Body (bytes)",
     "Maximum allowed request body size for external trigger calls."),
    ("external_trigger_rate_limit_count", "120", "int", "advanced",
     "External Trigger Rate Limit Count",
     "Maximum external trigger requests allowed per client within the configured rate window."),
    ("external_trigger_rate_limit_window_seconds", "60", "int", "advanced",
     "External Trigger Rate Limit Window (s)",
     "Sliding window size for external trigger request rate limiting."),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mask(value: str, value_type: str) -> str:
    """Return ``[REDACTED]`` for secrets; otherwise return *value* unchanged."""
    if value_type == "secret":
        return "[REDACTED]"
    return value


def _coerce(raw: Optional[str], value_type: str) -> Any:
    """Cast a stored raw string to the Python type described by *value_type*."""
    if raw is None:
        return None
    if value_type == "int":
        try:
            return int(raw)
        except (ValueError, TypeError):
            logger.warning("Failed to coerce value %r to int", raw)
            return raw
    if value_type == "float":
        try:
            return float(raw)
        except (ValueError, TypeError):
            logger.warning("Failed to coerce value %r to float", raw)
            return raw
    if value_type == "bool":
        return raw.strip().lower() in ("1", "true", "yes")
    if value_type == "json":
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.warning("Failed to coerce value %r to JSON", raw)
            return raw
    return raw  # string / secret


def _to_raw(value: Any) -> str:
    """Serialise an arbitrary Python value to a string suitable for storage."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


# ---------------------------------------------------------------------------
# SettingsManager
# ---------------------------------------------------------------------------

class SettingsManager:
    """Thread-safe, SQLite-backed singleton for NexusAI runtime settings.

    Typical usage::

        mgr = SettingsManager.instance()
        site = mgr.get("site_name", "NexusAI")
        mgr.set("log_level", "DEBUG", changed_by="admin")

    The manager keeps an in-process cache with a short TTL (default 5 s) so
    repeated reads are cheap while still picking up changes from other
    processes within a few seconds.
    """

    _instance: Optional["SettingsManager"] = None
    _class_lock: threading.Lock = threading.Lock()

    # ------------------------------------------------------------------
    # Singleton factory
    # ------------------------------------------------------------------

    @classmethod
    def instance(cls, db_path: str = "data/nexusai.db") -> "SettingsManager":
        """Return (creating if necessary) the shared :class:`SettingsManager`."""
        with cls._class_lock:
            if cls._instance is None:
                cls._instance = cls(db_path)
        return cls._instance

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(self, db_path: str = "data/nexusai.db") -> None:
        self._db_path = db_path
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._cache_ts: float = 0.0
        self._cache_ttl: float = 5.0
        self._write_lock = threading.Lock()
        self._init_db()

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        """Create tables and seed default settings (INSERT OR IGNORE)."""
        with self._connect() as conn:
            conn.execute(_CREATE_SETTINGS)
            conn.execute(_CREATE_AUDIT)
            now = datetime.now(timezone.utc).isoformat()
            for key, value, vtype, category, label, desc in _DEFAULTS:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO nexus_settings
                        (key, value, value_type, category, label,
                         description, updated_at, updated_by)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (key, value, vtype, category, label, desc, now, "system"),
                )
            conn.commit()

    # ------------------------------------------------------------------
    # Cache
    # ------------------------------------------------------------------

    def _load_all_rows(self) -> Dict[str, Dict[str, Any]]:
        """Return cached setting rows, refreshing if the TTL has expired."""
        now = time.monotonic()
        if now - self._cache_ts < self._cache_ttl and self._cache:
            return self._cache
        with self._connect() as conn:
            cur = conn.execute("SELECT * FROM nexus_settings")
            rows = {row["key"]: dict(row) for row in cur.fetchall()}
        self._cache = rows
        self._cache_ts = now
        return rows

    def _invalidate_cache(self) -> None:
        self._cache_ts = 0.0

    # ------------------------------------------------------------------
    # Public read API
    # ------------------------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        """Return the typed value for *key*, or *default* if not found."""
        rows = self._load_all_rows()
        row = rows.get(key)
        if row is None:
            return default
        return _coerce(row["value"], row["value_type"])

    def get_all(self, mask_secrets: bool = False) -> Dict[str, Dict[str, Any]]:
        """Return all settings as a dict keyed by setting key.

        Each value is a dict with keys: ``value``, ``value_type``,
        ``category``, ``label``, ``description``, ``updated_at``,
        ``updated_by``.

        Args:
            mask_secrets: When ``True``, replace secret values with
                ``[REDACTED]``.
        """
        rows = self._load_all_rows()
        result: Dict[str, Dict[str, Any]] = {}
        for key, row in rows.items():
            val = row["value"]
            if mask_secrets and row["value_type"] == "secret":
                val = "[REDACTED]"
            result[key] = {
                "value": val,
                "value_type": row["value_type"],
                "category": row["category"],
                "label": row["label"],
                "description": row["description"],
                "updated_at": row["updated_at"],
                "updated_by": row["updated_by"],
            }
        return result

    # ------------------------------------------------------------------
    # Public write API
    # ------------------------------------------------------------------

    def set(self, key: str, value: Any, changed_by: str = "system") -> None:
        """Persist a new value for *key* and append an audit-log entry.

        Args:
            key: The setting key to update.
            value: New value (will be coerced to a string for storage).
            changed_by: Identity of the caller (shown in the audit log).
        """
        raw = _to_raw(value)
        now = datetime.now(timezone.utc).isoformat()
        with self._write_lock:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT value, value_type FROM nexus_settings WHERE key = ?",
                    (key,),
                )
                row = cur.fetchone()
                old_raw: str = (row["value"] or "") if row else ""
                vtype: str = row["value_type"] if row else "string"
                old_masked = _mask(old_raw, vtype)
                new_masked = _mask(raw, vtype)
                conn.execute(
                    """
                    INSERT INTO nexus_settings (key, value, updated_at, updated_by)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value      = excluded.value,
                        updated_at = excluded.updated_at,
                        updated_by = excluded.updated_by
                    """,
                    (key, raw, now, changed_by),
                )
                conn.execute(
                    """
                    INSERT INTO nexus_settings_audit
                        (key, old_value, new_value, changed_by, changed_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (key, old_masked, new_masked, changed_by, now),
                )
                conn.commit()
        self._invalidate_cache()

    def import_from_dict(
        self,
        d: Dict[str, Any],
        changed_by: str = "system",
    ) -> None:
        """Bulk-import settings from a plain ``{key: value}`` mapping.

        Args:
            d: Mapping of setting key → new value.
            changed_by: Identity written to the audit log for each change.
        """
        for key, value in d.items():
            self.set(key, value, changed_by=changed_by)

    # ------------------------------------------------------------------
    # Export helpers
    # ------------------------------------------------------------------

    def export_yaml(self) -> str:
        """Return all settings serialised as a YAML string (secrets masked)."""
        data = {k: v["value"] for k, v in self.get_all(mask_secrets=True).items()}
        return yaml.dump(data, default_flow_style=False, allow_unicode=True)

    def export_json(self) -> str:
        """Return all settings serialised as a JSON string (secrets masked)."""
        data = {k: v["value"] for k, v in self.get_all(mask_secrets=True).items()}
        return json.dumps(data, indent=2)

    # ------------------------------------------------------------------
    # Audit log
    # ------------------------------------------------------------------

    def get_audit_log(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Return the *limit* most-recent audit-log entries (newest first).

        Args:
            limit: Maximum number of rows to return.
        """
        with self._connect() as conn:
            cur = conn.execute(
                """
                SELECT id, key, old_value, new_value, changed_by, changed_at
                FROM nexus_settings_audit
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            )
            return [dict(row) for row in cur.fetchall()]
