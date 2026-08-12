"""Poller functions for each supported ticket source type.

Each poller receives the source config + credential from the vault and returns
a list of normalised item dicts:

    {
        "external_id": str,
        "title": str,
        "body": str,
        "url": str | None,
        "state": str | None,
        "labels": list[str],
        "author": str | None,
        "raw": dict,
    }
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# Maximum items returned by any single poll (hard cap)
_MAX_ITEMS_CAP = 100

# Default User-Agent. Some CDNs/APIs (Cloudflare, etc.) reject the stock
# "Python-urllib/x" agent with HTTP 403. A real browser-like agent avoids
# false blocks. Callers can override per-source via config "user_agent".
_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; NexusAI-TicketSource/1.0; +https://nexusai.example)"
)


def _fetch_json(
    url: str,
    *,
    headers: Dict[str, str],
    timeout: int = 30,
    user_agent: str = _DEFAULT_USER_AGENT,
) -> Any:
    """Fetch and parse JSON from a URL. Raises RuntimeError on failure."""
    req_headers = dict(headers)
    req_headers.setdefault("User-Agent", user_agent)
    req = urllib.request.Request(url, headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} from {url}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Connection failed to {url}: {exc}") from exc
    return json.loads(body.decode("utf-8"))


def _fetch_json_post(
    url: str,
    *,
    headers: Dict[str, str],
    data: bytes,
    timeout: int = 30,
    user_agent: str = _DEFAULT_USER_AGENT,
) -> Any:
    """POST and parse JSON from a URL."""
    req_headers = dict(headers)
    req_headers.setdefault("User-Agent", user_agent)
    req = urllib.request.Request(url, headers=req_headers, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} from {url}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Connection failed to {url}: {exc}") from exc
    return json.loads(body.decode("utf-8"))


# ---------------------------------------------------------------------------
#  github_issues
# ---------------------------------------------------------------------------

async def poll_github_issues(
    config: Dict[str, Any],
    *,
    credential: Optional[str] = None,
) -> List[Dict[str, Any]]:
    repo_full_name = str(config.get("repo_full_name") or "").strip()
    if not repo_full_name:
        raise ValueError("github_issues requires repo_full_name in config")
    if not credential:
        raise ValueError("github_issues requires a GitHub PAT credential")

    state = config.get("state", "open")
    max_items = min(int(config.get("max_items", 25)), _MAX_ITEMS_CAP)
    sort = config.get("sort", "updated")
    direction = config.get("direction", "desc")
    labels = config.get("labels") or []
    label_filter_mode = config.get("label_filter", "any")

    params: Dict[str, Any] = {
        "state": state,
        "sort": sort,
        "direction": direction,
        "per_page": min(max_items, 100),
    }
    if labels:
        params["labels"] = ",".join(labels)

    since_param = None
    if config.get("since_hours"):
        from datetime import timedelta
        dt = datetime.now(timezone.utc) - timedelta(hours=int(config["since_hours"]))
        since_param = dt.isoformat().replace("+00:00", "Z")

    base_url = f"https://api.github.com/repos/{repo_full_name}/issues"
    headers = {
        "Authorization": f"Bearer {credential}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    url_label_set = set(lbl.lower() for lbl in labels)
    all_items: List[Dict[str, Any]] = []
    page = 1

    while len(all_items) < max_items:
        p = dict(params)
        p["page"] = str(page)
        if since_param:
            p["since"] = since_param
        qs = urllib.parse.urlencode(p)
        raw = _fetch_json(f"{base_url}?{qs}", headers=headers)

        if not isinstance(raw, list) or not raw:
            break

        for issue in raw:
            if not isinstance(issue, dict) or "pull_request" in issue:
                continue
            if url_label_set:
                issue_labels = set(
                    lbl.get("name", "").lower()
                    for lbl in issue.get("labels", [])
                    if isinstance(lbl, dict)
                )
                if label_filter_mode == "all" and not url_label_set.issubset(issue_labels):
                    continue
                elif label_filter_mode == "none" and url_label_set.intersection(issue_labels):
                    continue
                elif label_filter_mode == "any" and not url_label_set.intersection(issue_labels):
                    continue
            all_items.append({
                "external_id": str(issue.get("number")),
                "title": str(issue.get("title") or ""),
                "body": str(issue.get("body") or ""),
                "url": issue.get("html_url"),
                "state": issue.get("state"),
                "labels": [
                    lbl.get("name")
                    for lbl in issue.get("labels", [])
                    if isinstance(lbl, dict)
                ],
                "author": (issue.get("user") or {}).get("login") if isinstance(issue.get("user"), dict) else None,
                "raw": issue,
            })
        if len(raw) < min(max_items, 100):
            break
        page += 1

    return all_items[:max_items]


# ---------------------------------------------------------------------------
#  generic_http
# ---------------------------------------------------------------------------

async def poll_generic_http(
    config: Dict[str, Any],
    *,
    credential: Optional[str] = None,
) -> List[Dict[str, Any]]:
    url = str(config.get("url") or "").strip()
    if not url:
        raise ValueError("generic_http requires a url in config")

    method = str(config.get("method", "GET")).upper()
    max_items = min(int(config.get("max_items", 25)), _MAX_ITEMS_CAP)
    headers: Dict[str, str] = {}
    for h in config.get("headers") or []:
        if isinstance(h, dict) and h.get("name") and h.get("value"):
            headers[str(h["name"])] = str(h["value"])
    if credential:
        auth_header = str(config.get("auth_header_name") or "Authorization")
        auth_scheme = str(config.get("auth_scheme") or "Bearer")
        headers[auth_header] = f"{auth_scheme} {credential}" if auth_scheme.lower() != "none" else credential

    results_path = str(config.get("results_path") or "").strip()
    field_map = config.get("field_map") or {}
    user_agent = str(config.get("user_agent") or _DEFAULT_USER_AGENT).strip()

    def _resolve_path(obj: Any, path: str) -> Any:
        for part in path.split("."):
            if isinstance(obj, dict):
                obj = obj.get(part)
            elif isinstance(obj, list) and part.isdigit():
                idx = int(part)
                obj = obj[idx] if 0 <= idx < len(obj) else None
            else:
                return None
        return obj

    if method == "POST":
        post_body = json.dumps(config.get("post_body") or {}).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
        raw_resp = _fetch_json_post(url, headers=headers, data=post_body, user_agent=user_agent)
    else:
        raw_resp = _fetch_json(url, headers=headers, user_agent=user_agent)

    # Board-style flattening: boards[] -> columns[] -> cards[].
    # Any board-like API (Trello, Jira, custom scrumboards) can be consumed by
    # naming the boards/column/card array fields.
    board_field = str(config.get("board_field") or "").strip()
    column_field = str(config.get("column_field") or "").strip()
    card_field = str(config.get("card_field") or "").strip()
    column_name_field = str(config.get("column_name_field") or "name").strip()
    board_title_field = str(config.get("board_title_field") or "title").strip()

    if board_field and column_field and card_field:
        # Resolve the boards array: first try results_path, then board_field,
        # then common keys on the response root.
        boards: Any = None
        if results_path:
            boards = _resolve_path(raw_resp, results_path)
        if boards is None:
            boards = _resolve_path(raw_resp, board_field)
        if not isinstance(boards, list):
            if isinstance(boards, dict):
                for key in ("boards", "data", "results", "items"):
                    if isinstance(boards.get(key), list):
                        boards = boards[key]
                        break
                else:
                    boards = []
            else:
                boards = []
        return _flatten_board_items(
            boards,
            column_field=column_field,
            card_field=card_field,
            column_name_field=column_name_field,
            board_title_field=board_title_field,
            field_map=field_map,
            max_items=max_items,
        )

    items_list: Any = raw_resp
    if results_path:
        items_list = _resolve_path(raw_resp, results_path)

    if not isinstance(items_list, list):
        if isinstance(items_list, dict):
            for key in ("items", "tickets", "issues", "data", "results"):
                if isinstance(items_list.get(key), list):
                    items_list = items_list[key]
                    break
            else:
                items_list = [items_list]
        else:
            items_list = []

    out: List[Dict[str, Any]] = []
    for raw_item in items_list:
        if not isinstance(raw_item, dict):
            continue
        fm = field_map
        ext_id = str(raw_item.get(fm.get("id", "id")) or raw_item.get("id") or "")
        if not ext_id:
            continue
        labels_val = raw_item.get(fm.get("labels", "labels"))
        if isinstance(labels_val, str):
            labels_list = [l.strip() for l in labels_val.split(",") if l.strip()]
        elif isinstance(labels_val, list):
            labels_list = [str(l) for l in labels_val]
        else:
            labels_list = []
        out.append({
            "external_id": ext_id,
            "title": str(raw_item.get(fm.get("title", "title")) or ""),
            "body": str(raw_item.get(fm.get("body", "body")) or raw_item.get(fm.get("description", "description")) or ""),
            "url": raw_item.get(fm.get("url", "url")),
            "state": raw_item.get(fm.get("state", "state")),
            "labels": labels_list,
            "author": raw_item.get(fm.get("author", "author")),
            "raw": raw_item,
        })
    return out[:max_items]


def _flatten_board_items(
    boards: List[Any],
    *,
    column_field: str,
    card_field: str,
    column_name_field: str,
    board_title_field: str,
    field_map: Dict[str, Any],
    max_items: int,
) -> List[Dict[str, Any]]:
    """Flatten a boards[] -> columns[] -> cards[] structure into ticket items.

    Each card becomes one ticket item. The column name and board title are
    attached to the card's raw payload so downstream consumers can group by
    board/column. Field mapping keys may reference nested card fields via
    dot-notation (e.g. "fields.summary").
    """
    out: List[Dict[str, Any]] = []
    fm = field_map or {}

    def _get(obj: Any, key: str) -> Any:
        for part in str(key).split("."):
            if isinstance(obj, dict):
                obj = obj.get(part)
            elif isinstance(obj, list) and part.isdigit():
                idx = int(part)
                obj = obj[idx] if 0 <= idx < len(obj) else None
            else:
                return None
        return obj

    for board in boards:
        if not isinstance(board, dict):
            continue
        board_title = str(_get(board, board_title_field) or "")
        columns = board.get(column_field)
        if not isinstance(columns, list):
            continue
        for column in columns:
            if not isinstance(column, dict):
                continue
            column_name = str(_get(column, column_name_field) or "")
            cards = column.get(card_field)
            if not isinstance(cards, list):
                continue
            for card in cards:
                if not isinstance(card, dict):
                    continue
                ext_id = str(_get(card, fm.get("id", "id")) or card.get("id") or "")
                if not ext_id:
                    continue
                labels_val = _get(card, fm.get("labels", "labels"))
                if isinstance(labels_val, str):
                    labels_list = [l.strip() for l in labels_val.split(",") if l.strip()]
                elif isinstance(labels_val, list):
                    labels_list = [str(l) for l in labels_val]
                else:
                    labels_list = []
                enriched = dict(card)
                enriched.setdefault("_board_title", board_title)
                enriched.setdefault("_column_name", column_name)
                out.append({
                    "external_id": ext_id,
                    "title": str(_get(card, fm.get("title", "title")) or ""),
                    "body": str(_get(card, fm.get("body", "body")) or _get(card, fm.get("description", "description")) or ""),
                    "url": _get(card, fm.get("url", "url")),
                    "state": _get(card, fm.get("state", "state")),
                    "labels": labels_list,
                    "author": _get(card, fm.get("author", "author")),
                    "raw": enriched,
                })
    return out[:max_items]


# ---------------------------------------------------------------------------
#  jira  (stub — full implementation deferred for third-party users)
# ---------------------------------------------------------------------------

async def poll_jira(
    config: Dict[str, Any],
    *,
    credential: Optional[str] = None,
) -> List[Dict[str, Any]]:
    base_url = str(config.get("base_url") or "").strip()
    if not base_url:
        raise ValueError("jira requires a base_url in config")
    if not credential:
        raise ValueError("jira requires an API token credential")

    jql = str(config.get("jql") or "").strip()
    max_items = min(int(config.get("max_items", 25)), _MAX_ITEMS_CAP)
    email = str(config.get("email") or "").strip()

    headers = {
        "Accept": "application/json",
    }
    if email:
        import base64
        token = base64.b64encode(f"{email}:{credential}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"
    else:
        headers["Authorization"] = f"Bearer {credential}"

    params = urllib.parse.urlencode({"jql": jql, "maxResults": max_items})
    url = f"{base_url.rstrip('/')}/rest/api/2/search?{params}"
    raw = _fetch_json(url, headers=headers)

    issues = (raw or {}).get("issues", [])
    out: List[Dict[str, Any]] = []
    for issue in issues:
        fields = issue.get("fields", {})
        out.append({
            "external_id": str(issue.get("key") or ""),
            "title": str(fields.get("summary") or ""),
            "body": str(fields.get("description") or ""),
            "url": f"{base_url.rstrip('/')}/browse/{issue.get('key')}",
            "state": fields.get("status", {}).get("name") if isinstance(fields.get("status"), dict) else None,
            "labels": [str(l) for l in fields.get("labels", [])],
            "author": (fields.get("reporter") or {}).get("name") if isinstance(fields.get("reporter"), dict) else None,
            "raw": issue,
        })
    return out[:max_items]


# ---------------------------------------------------------------------------
#  asana  (stub — full implementation deferred for third-party users)
# ---------------------------------------------------------------------------

async def poll_asana(
    config: Dict[str, Any],
    *,
    credential: Optional[str] = None,
) -> List[Dict[str, Any]]:
    project_id = str(config.get("project_id") or "").strip()
    if not project_id:
        raise ValueError("asana requires a project_id in config")
    if not credential:
        raise ValueError("asana requires a token credential")

    max_items = min(int(config.get("max_items", 25)), _MAX_ITEMS_CAP)
    completed = config.get("completed", False)

    headers = {"Authorization": f"Bearer {credential}"}
    params = urllib.parse.urlencode({"limit": max_items, "completed": str(completed).lower()})
    url = f"https://app.asana.com/api/1.0/projects/{project_id}/tasks?{params}"
    raw = _fetch_json(url, headers=headers)

    tasks = raw.get("data", []) if isinstance(raw, dict) else []
    out: List[Dict[str, Any]] = []
    for task in tasks:
        out.append({
            "external_id": str(task.get("gid") or task.get("id") or ""),
            "title": str(task.get("name") or ""),
            "body": "",
            "url": f"https://app.asana.com/0/{project_id}/{task.get('gid') or task.get('id')}",
            "state": "completed" if task.get("completed") else "open",
            "labels": [],
            "author": None,
            "raw": task,
        })
    return out[:max_items]


POLLERS = {
    "github_issues": poll_github_issues,
    "generic_http": poll_generic_http,
    "jira": poll_jira,
    "asana": poll_asana,
}


async def poll_source(
    source_type: str,
    config: Dict[str, Any],
    *,
    credential: Optional[str] = None,
) -> List[Dict[str, Any]]:
    poller = POLLERS.get(source_type)
    if poller is None:
        raise ValueError(f"unsupported ticket source type: {source_type}")
    return await poller(config, credential=credential)