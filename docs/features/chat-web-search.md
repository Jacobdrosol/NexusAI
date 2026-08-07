# Chat Web Search

Direct chat can add current web evidence through a self-hosted SearXNG service. It is not enabled globally and does not use a paid search API.

## Configuration

Enable it only on a bot intended for interactive research by setting this bot routing value:

```json
{
  "chat_tool_access": {
    "enabled": true,
    "web_search": true,
    "filesystem": false,
    "repo_search": false
  }
}
```

The search capability is bot-scoped. It does not grant repository, filesystem, worker, or deployment access, and it does not enable search for other bots.

## Runtime behavior

The control plane calls `NEXUSAI_SEARXNG_URL` (default `http://searxng:8080`) only for prompts that indicate a current-data or lookup need, such as prices, current information, schedules, serial or part lookups, or an explicit web search request. Normal and private chat prompts are not sent to SearXNG.

The resulting title, URL, and snippet are added as bounded system context. The model is instructed to cite exact URLs for time-sensitive claims and to state when the search evidence cannot establish an exact answer. A SearXNG failure simply omits web context; it does not expose the user's prompt in an error message.
