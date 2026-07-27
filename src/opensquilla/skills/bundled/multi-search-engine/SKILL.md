---
name: multi-search-engine
description: "Query the web through multiple search engines (Brave, Tavily, SerpAPI, DuckDuckGo, Bing, Baidu, Sogou, 360) with a single CLI surface. Trigger when the user asks for a research search, fact lookup, source discovery, or wants to compare engines for coverage. The skill aggregates per-engine result lists and normalizes them into a uniform JSON shape for downstream skills (deep-research is the primary consumer). API-key engines gate themselves on the relevant environment variable; engines requiring no key always run."
description_zh: "通过多个搜索引擎（Brave、Tavily、SerpAPI、DuckDuckGo、Bing、Baidu、Sogou、360）以统一的CLI接口查询网络。当用户需要研究搜索、事实查找、来源发现或比较各引擎覆盖度时触发。技能会聚合各引擎结果并归一化为统一JSON结构供下游技能使用（deep-research为主要消费者）。需密钥的引擎依赖相应环境变量启用，无需密钥的引擎始终运行。"
homepage: ""
provenance:
  origin: clawhub-mit0
  license: MIT-0
  upstream_url: https://clawhub.ai/multi-search-engine
  maintained_by: OpenSquilla
metadata:
  {
    "platform":
      {
        "emoji": "🔍",
        "requires": { "anyBins": ["python", "python3"] },
      },
  }
entrypoint:
  command: python {baseDir}/scripts/search.py
  args:
    - --query
    - "{{ with.query | default(inputs.user_message) }}"
    - --engines
    - "{{ with.engines | default(['brave', 'duckduckgo']) | join(',') }}"
    - --limit
    - "{{ with.max_results | default(25) }}"
    - --json
  parse: json
  timeout: 60
---

# multi-search-engine

A unified CLI for querying several web search engines in parallel and
returning a normalized result list. Built on `httpx` and `beautifulsoup4`
(both already in OpenSquilla default dependencies, so no extra install
beyond `pip install opensquilla`).

## Use cases

- Building a `deep-research` round with diverse engine coverage
- Fact-check a claim against >1 engine
- Compare what Bing returns vs DuckDuckGo for the same query
- Search Chinese-language sources via Baidu/Sogou/360 alongside global engines

## Limitations

- A single engine sufficient → call its API directly instead
- Need headless-browser DOM rendering → this skill is HTTP-only

## Quick start

```bash
python {baseDir}/scripts/search.py \
    --query "openclaw skill registry" \
    --engines duckduckgo,brave \
    --limit 10 \
    --json
```

Output:

```json
{
  "query": "...",
  "results": [
    {
      "engine": "duckduckgo",
      "title": "...",
      "url": "https://...",
      "snippet": "...",
      "rank": 1
    }
  ],
  "errors": [
    {"engine": "brave", "reason": "BRAVE_SEARCH_API_KEY/BRAVE_API_KEY not set; skipping"}
  ]
}
```

## Engines

| Engine | Needs key | Key env var | Strength |
|---|---|---|---|
| `duckduckgo` | no | — | Privacy-friendly, no rate limit by default |
| `bing` | no | — | HTML scrape; respect rate limits |
| `baidu` | no | — | Chinese-language web |
| `sogou` | no | — | Chinese-language web |
| `360` | no | — | Chinese-language web |
| `brave` | yes | `BRAVE_SEARCH_API_KEY` or legacy `BRAVE_API_KEY` | High-quality results, generous free tier |
| `tavily` | yes | `TAVILY_API_KEY` | Designed for AI agents, returns clean JSON |
| `serpapi` | yes | `SERPAPI_API_KEY` | Aggregator across many engines |

The script never errors out when an API-key engine's key is missing — it
records a per-engine `errors` entry and continues with the rest. Pass
`--strict` to fail fast when any requested engine is unavailable.

## Routing tips

The host should pick engines by language and availability:

- English queries → `duckduckgo`, `brave`, `bing` (one or two for triangulation)
- Chinese queries → `baidu` plus optionally `sogou` for cross-check
- Time-sensitive (last 24h) → `brave` (recency filter) or `tavily`
- Long-tail academic → fall back to direct arXiv / Google Scholar; this
  skill targets general web search

`engines.md` has the full per-engine guidance.

## Boundaries

- HTTP-only. JS-rendered pages will not be readable; use a headless-browser
  skill if needed.
- Scraping engines (DuckDuckGo, Bing, Baidu, Sogou, 360) are best-effort —
  HTML structure changes break them. The script logs parse failures
  individually and keeps the run going.
- Rate limiting is not handled inside the script. Calling the same engine
  10x/sec from a loop will get blocked. Add jitter and back-off in the
  caller.
- Captcha-protected results are not bypassed. If an engine returns a
  challenge page, the parser will return zero results for that engine and
  log a warning.
