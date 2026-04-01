# Security

The security module provides per-route body size limits and in-memory per-IP rate limiting for the control plane API.

---

## Guards (`guards.py`)

### `enforce_body_size(request, route_name, default_max_bytes)`

Checks the incoming request body against a size limit:
1. Reads `Content-Length` header — if present and exceeds limit, raises HTTP 413 immediately.
2. Reads the full body — raises HTTP 413 if actual body exceeds limit.

Override per route via environment variable:
```
CP_MAX_BODY_BYTES_<ROUTE_NAME> = <bytes>
```
Where `<ROUTE_NAME>` is the uppercase route name with hyphens replaced by underscores. E.g., `CP_MAX_BODY_BYTES_VAULT_INGEST=4000000`.

### `enforce_rate_limit(request, route_name, default_limit, default_window_seconds)`

Sliding window rate limiter per client IP:
- Uses `request.client.host` as the key.
- Stores a `deque` of timestamps per `<route_name>:<client_ip>` key in `request.app.state.rate_limit_store`.
- Raises HTTP 429 if the bucket already has `>= limit` entries within the window.

Override per route:
```
CP_RATE_LIMIT_<ROUTE_NAME>_COUNT = <n>
CP_RATE_LIMIT_<ROUTE_NAME>_WINDOW_SECONDS = <seconds>
```

### Example Defaults (from vault API)

| Route | Default limit | Default window |
|-------|--------------|----------------|
| `vault_ingest` | 30 req | 60 s |
| `chat_message` | (set by chat API) | (set by chat API) |

---

## Current Guard Usage by Route

| Endpoint | Body limit | Rate limit |
|----------|------------|------------|
| `POST /v1/vault/items` | 2 MB | 30/min |
| `POST /v1/vault/items/upsert` | 2 MB | 30/min |
| `POST /v1/chat/conversations/{id}/messages` | Configurable | Configurable |
| `POST /v1/bots` | Configurable | Configurable |
| `POST /v1/projects/{id}/...` | Configurable | Configurable |

---

## Auth Middleware

Defined in `control_plane/main.py`:

- If `CONTROL_PLANE_API_TOKEN` is not set → no auth, all requests pass.
- If set → all requests must include `X-Nexus-API-Key: <token>` or `Authorization: Bearer <token>`.
- Exempt paths: `/health`, `/docs`, `/redoc`, `/openapi.json`, `POST /v1/bots/<id>/trigger`.

---

## Known Issues

- **Rate limit store is per-process**: `request.app.state.rate_limit_store` is in-memory and not shared across multiple uvicorn workers or Gunicorn processes. Under multi-worker deploys, the effective rate limit is `limit × worker_count`.
- **`POST /v1/bots/{id}/trigger` is auth-exempt**: Any caller can trigger a bot run without authentication. This may be intentional for webhook integration but should be documented and configurable.
- **No IP allowlisting**: No mechanism to trust specific internal IPs and bypass rate limits.
- **No circuit breaker**: If the database is slow, requests are not shed — they queue in the event loop.
