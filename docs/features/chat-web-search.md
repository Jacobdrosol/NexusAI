# Chat Web Search

Direct chat gives the model two tools: `web_search` and `fetch_web_page`. The
model decides when to search, writes its own queries, reads the results, and can
open a page for more detail. No server-side heuristic or conversation-derived
query planner is involved.

The capability is opt-in per bot and is not enabled globally.

## Configuration

Enable it on a bot intended for interactive research:

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

The search capability is bot-scoped. It does not grant repository, filesystem,
worker, or deployment access, and it does not enable search for other bots.

## Search providers

Choose the transport with `NEXUSAI_SEARCH_PROVIDER`. Add the key for the
provider you select. Keys are read on the server only and are never sent to the
browser.

| Provider | Value | Key | Best for |
|---|---|---|---|
| Brave | `brave` | `NEXUSAI_BRAVE_SEARCH_API_KEY` | Recommended default. Independent index, official API, privacy-friendly, fast. Free tier available. |
| Tavily | `tavily` | `NEXUSAI_TAVILY_API_KEY` | AI research. Returns extracted page content, so the model needs fewer fetch steps. Free tier available. |
| Serper | `serper` | `NEXUSAI_SERPER_API_KEY` | Google SERP results for product comparisons. Third-party Google SERP API, free tier available. |
| Bing | `bing` | none | Zero-config fallback. Server-side Bing can return degraded pages to bots; use an API provider for production. |
| SearXNG | `searxng` | none | Self-hosted, no third-party search calls. Requires a SearXNG instance with working engines. |

### Why a keyed provider

Server-side scraping of Bing or Google is served a degraded page when the search
engine detects a bot, even though the same query works in a browser. The `bing`
and `searxng` options are useful for a zero-key install, but a keyed provider is
the reliable choice for actual work. Microsoft retired the official Bing Search
API, so there is no first-party Bing API to configure.

### Setup (Brave example)

1. Create a Brave Search API key in the Brave dashboard.
2. Add both values to `.env`:
   ```
   NEXUSAI_SEARCH_PROVIDER=brave
   NEXUSAI_BRAVE_SEARCH_API_KEY=your-key
   ```
3. Restart the control plane so it reloads the environment.

For Tavily use `NEXUSAI_SEARCH_PROVIDER=tavily` and
`NEXUSAI_TAVILY_API_KEY`. For Serper use `NEXUSAI_SEARCH_PROVIDER=serper` and
`NEXUSAI_SERPER_API_KEY`.

### Switching providers

Change `NEXUSAI_SEARCH_PROVIDER` to `brave`, `tavily`, or `serper`. If the
selected provider has no key, the app falls back to Bing RSS and then to
SearXNG, so an empty key does not break search.

## Runtime behavior

The tool loop runs for both normal and streaming chat. The model may call
`web_search` several times and `fetch_web_page` on results it wants to inspect.
Tool calls are capped, and when the research budget is spent the model is asked
for one final answer with citations and explicit gaps.

`fetch_web_page` only fetches public http/https URLs on ports 80 and 443. It
rejects private, loopback, link-local, and reserved addresses, so a model prompt
cannot turn it into an internal network request.
