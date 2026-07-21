---
name: tavily-search
toolsets:
  - web_research
description: "Web search via Tavily API (alternative to Brave). Use when the user asks to search the web / look up sources / find links and Brave web_search is unavailable or undesired. Returns a small set of relevant results (title, url, snippet) and can optionally include short answer summaries."
---

# Tavily Search

Use the platform-native `tavily_search` tool for every search. It is the
controlled, read-only network entry point and receives credentials from the
runtime.

## Workflow

1. Call `tavily_search` with a focused `query`.
2. Keep `max_results` at 3–5 by default; use up to 10 only when broader
   coverage materially helps.
3. Refine the query and call `tavily_search` again when the first result set is
   ambiguous or incomplete.
4. Return the useful findings with their source URLs. Use `fetch_url` only
   when a returned page needs closer inspection.

## Runtime boundary

- Do not run `scripts/tavily_search.py` through `execute` for normal searches.
- Do not ask the user for `TAVILY_API_KEY`; the native tool owns credential
  handling.
- If `tavily_search` returns an error, report that error or use another visible
  controlled research tool. Do not bypass the platform boundary with Python,
  curl, or a hand-written HTTP client.

## Notes

- Prefer returning URLs + snippets; fetch full pages only when needed.
