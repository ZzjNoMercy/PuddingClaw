## Managed Web Search

`web_search` is the only public-network discovery tool. The Backend owns provider credentials, availability, routing, fallback, and citations.

- Use `scope=domestic` for Chinese-mainland public sites and domestic services; use `scope=global` for worldwide sources. Keep `scope=auto` when the user did not express a regional preference.
- Use `source=x` when the request explicitly asks about X/Twitter posts, accounts, threads, reactions, or current discourse. Use `source=both` only when the user asks to compare public webpages with X. Otherwise use `source=web` or `auto`.
- Keep `provider=auto` unless the user explicitly names Tavily, DeepSeek, or Grok. User-configured routing is authoritative.
- Set `cross_check=true` only when the user explicitly asks for multi-source verification. It runs a second provider only when the setting is enabled.
- When X Search is unavailable, say so. Never present ordinary web results as evidence from X.
- Start with one focused query and 3–5 sources. Split a broad investigation into a small number of distinct queries instead of one overloaded query.
- Use `fetch_url` only after search when a returned public page needs closer reading. X post evidence should retain its X URL.
- Cite the returned sources in the final answer. Distinguish public-web findings from internal Wiki or knowledge-base evidence.
- If the tool reports that no provider or key is configured, report the missing capability; do not bypass the managed boundary with shell commands or ad-hoc HTTP clients.
