# Connections

The `connections` module provides a unified resolver for looking up project and bot connections stored in the platform database. It is used by the orchestration and scheduler layers to inject credentials and connection config into PM workflow tasks.

---

## Purpose

- Map connection IDs to full connection records (name, kind, config, auth)
- Look up connections scoped to a project or enabled for a specific bot
- Provide smart search by ID, name, or unique match
- Fall back to legacy dashboard ORM if the primary SQL path fails

---

## Files

### `resolver.py` — `ConnectionResolver`

Synchronous resolver backed by raw SQLite queries. Used at runtime by the agent scheduler and assignment service.

**Key methods:**

| Method | Description |
|--------|-------------|
| `list_project_connections(project_id)` | All connections associated with a project |
| `get_project_connection(project_id, connection_id)` | Single connection with full config and auth JSON |
| `list_bot_connections(bot_id)` | Connections enabled for a specific bot |
| `find_bot_connection(bot_id, connection_id_or_name)` | Smart lookup: tries exact ID match, then name match, then unique match |

**Connection record fields:**

| Field | Type | Description |
|-------|------|-------------|
| `id` | TEXT | Connection UUID |
| `name` | TEXT | Human-readable name |
| `kind` | TEXT | Connection type: `database`, `http`, `github`, `custom`, etc. |
| `description` | TEXT | Optional description |
| `enabled` | BOOL | Whether the connection is active |
| `config_json` | DICT | Non-sensitive configuration (URLs, options) |
| `auth_json` | DICT | Sensitive credentials (tokens, keys) — handle carefully |
| `schema_text` | TEXT | Optional schema or API spec for the connection |

---

## How It Fits In

```
AssignmentService._validate_project_bindings()
  └── ConnectionResolver.list_project_connections()

AgentScheduleEngine.tick()
  └── ConnectionResolver.find_bot_connection()

Platform AI context resolution
  └── ConnectionResolver.get_project_connection()
```

---

## Fallback Behavior

If the primary SQLite query fails (e.g., missing table or column), the resolver silently falls back to the dashboard ORM (`dashboard.models`). This cross-module dependency is a known architectural issue:

- The fallback can return stale data if the DB was updated after the ORM cached it
- SQL errors are swallowed with no log warning
- The ORM import fails if the dashboard package is not on the Python path

---

## Known Issues

| # | Severity | Issue |
|---|----------|-------|
| 1 | 🔴 High | Silent fallback to dashboard ORM masks SQL errors — failures are invisible | 
| 2 | 🟠 Medium | No caching — every resolver call queries the DB; N+1 patterns in loops |
| 3 | 🟠 Medium | Dual-source ambiguity: primary SQL and legacy ORM can return different records |
| 4 | 🟡 Low | `auth_json` returned in full to callers — no field-level redaction |

---

## Refactor Notes

- The fallback ORM dependency should be removed. Connection data should live in a single canonical store accessible to both dashboard and control plane.
- Add an in-process cache (e.g., per-request dict or short TTL) to avoid repeated DB queries.
- `auth_json` should be redacted in list responses and only returned in explicit single-connection fetch calls.
- Log a warning (not silent) when falling back to the legacy ORM.
