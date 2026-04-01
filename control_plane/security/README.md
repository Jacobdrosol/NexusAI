# Security — `control_plane/security/`

Request-level security guards for the NexusAI control plane. Provides
**body-size enforcement** and **per-client rate limiting** implemented as
async helper functions that FastAPI route handlers call directly (not as
middleware).

---

## Files

| File | Purpose |
|---|---|
| `guards.py` | Body-size and rate-limit guards; env-var-driven configuration helpers |

---

## Architecture

Guards are **not** FastAPI middleware. They are `async` functions that route
handlers (or `Depends()` dependencies) call explicitly, allowing per-route
limits without a global filter. Both guards raise `fastapi.HTTPException` on
violation so FastAPI's exception handling returns the appropriate HTTP status
code automatically.

State for the rate limiter (`rate_limit_lock` and `rate_limit_store`) is
stored in `request.app.state`, making it shared across all requests within a
single process lifetime.

---

## Environment Variable Configuration

Every limit has a hardcoded default that callers pass in, but can be
overridden at runtime via environment variables — useful for tuning production
deployments without code changes.

### Body-size variable naming

```
CP_MAX_BODY_BYTES_<ROUTE_KEY>
```

Where `<ROUTE_KEY>` is the route name uppercased with hyphens replaced by
underscores. Example: route `webhook-receive` → `CP_MAX_BODY_BYTES_WEBHOOK_RECEIVE`.

### Rate-limit variable naming

```
CP_RATE_LIMIT_<ROUTE_KEY>_COUNT          # max requests in window
CP_RATE_LIMIT_<ROUTE_KEY>_WINDOW_SECONDS # sliding window size
```

Both variables must be valid integers; malformed values fall back to the
caller-supplied defaults.

---

## All Guard Functions

### Private Helpers

#### `_env_int(name: str, default: int) → int`

Reads an environment variable and converts it to `int`. Returns `default` on
any conversion error or if the variable is unset.

#### `_route_key(route_name: str) → str`

Converts a route name to the canonical environment variable fragment:
uppercased with `-` → `_`. Example: `"chat-send"` → `"CHAT_SEND"`.

#### `_resolve_max_body(route_name: str, default_bytes: int) → int`

Looks up `CP_MAX_BODY_BYTES_<ROUTE_KEY>` and returns the result, falling back
to `default_bytes`.

#### `_resolve_rate_limit(route_name: str, default_limit: int, default_window_seconds: int) → Tuple[int, int]`

Looks up `CP_RATE_LIMIT_<ROUTE_KEY>_COUNT` and `CP_RATE_LIMIT_<ROUTE_KEY>_WINDOW_SECONDS`.
Returns `(limit, window)` where both values are clamped to a minimum of `1`.

---

### Public Guards

#### `async enforce_body_size(request, route_name, default_max_bytes) → None`

```python
async def enforce_body_size(
    request: Request,
    route_name: str,
    default_max_bytes: int,
) -> None
```

Enforces a maximum request body size using a two-step check:

1. **Header check** — reads the `Content-Length` header. If present and
   exceeds the limit, raises `HTTPException(413)` immediately (no body read).
2. **Body check** — reads the entire raw body with `await request.body()`.
   If the byte count exceeds the limit, raises `HTTPException(413)`.

Setting `default_max_bytes <= 0` disables the check entirely for that call.

Raises: `HTTPException(status_code=413, detail="request body too large (max N bytes)")`

#### `async enforce_rate_limit(request, route_name, default_limit, default_window_seconds) → None`

```python
async def enforce_rate_limit(
    request: Request,
    route_name: str,
    default_limit: int,
    default_window_seconds: int,
) -> None
```

Per-client sliding-window rate limiter backed by an in-memory `deque`.

- The rate-limit store is a `dict[str, deque[float]]` stored on
  `request.app.state.rate_limit_store`. It is created on first use.
- A single `asyncio.Lock` (`request.app.state.rate_limit_lock`) serialises all
  store mutations.
- The key for each bucket is `"<route_name>:<client_ip>"`. Requests from
  unknown clients use the key `"<route_name>:unknown"`.
- Timestamps older than `now - window_seconds` are evicted from the front of
  the deque before checking the count.
- If `len(bucket) >= limit`, raises `HTTPException(429)`.
- Otherwise, the current timestamp is appended and the request proceeds.

Raises: `HTTPException(status_code=429, detail="rate limit exceeded")`

---

## FastAPI Integration Pattern

```python
from control_plane.security.guards import enforce_body_size, enforce_rate_limit

@router.post("/webhooks/github/{project_id}")
async def receive_github_webhook(project_id: str, request: Request):
    await enforce_body_size(request, "webhook-receive", default_max_bytes=512_000)
    await enforce_rate_limit(request, "webhook-receive", default_limit=300, default_window_seconds=60)
    ...
```

---

## Known Issues

- **In-process state only** — `rate_limit_store` lives in process memory. It
  is not shared across multiple control-plane processes or workers. Deploying
  more than one process removes rate-limit effectiveness.
- **No Redis / persistent backend** — restarting the process resets all rate
  buckets.
- **No authentication guard** — `guards.py` does not check API keys, bearer
  tokens, or session cookies. Authentication is handled elsewhere (see
  `dashboard/` session middleware or `X-Nexus-API-Key` checks in route
  handlers).
- **Body is read into memory** — `enforce_body_size` calls `await request.body()`
  which buffers the entire request in memory. For very large allowed sizes this
  may be undesirable.
- **`unknown` bucket collision** — clients that present no IP (e.g. Unix
  socket connections) all share the `"unknown"` bucket, making their rate
  limits collective rather than individual.
