---
name: multi-search-engine
description: "Query scholarly metadata and the web through Crossref, Brave, Tavily, and DuckDuckGo with a single CLI surface. Trigger when the user asks for research search, fact lookup, source discovery, or engine comparison. Results retain DOI, publication year, and authors when supplied by Crossref, then deduplicate by DOI, arXiv ID, or normalized URL. API-key engines gate themselves on the relevant environment variable; Crossref and DuckDuckGo need no key."
description_zh: "通过统一 CLI 使用 Crossref、Brave、Tavily 和 DuckDuckGo 查询学术元数据与网页。适用于研究搜索、事实查找、来源发现或引擎覆盖比较。结果会保留 Crossref 提供的 DOI、出版年份和作者，并按 DOI、arXiv ID 或规范化 URL 去重。需要密钥的引擎由对应环境变量启用；Crossref 和 DuckDuckGo 无需密钥。"
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
- Compare scholarly metadata coverage with general web results
- Find citable publication metadata without requiring an API key

## Limitations

- A single engine sufficient → call its API directly instead
- Need headless-browser DOM rendering → this skill is HTTP-only

## Quick start

```bash
python {baseDir}/scripts/search.py \
    --query "openclaw skill registry" \
    --engines crossref,duckduckgo,brave \
    --limit 10 \
    --json
```

Output:

```json
{
  "query": "...",
  "results": [
    {
      "engine": "crossref",
      "title": "...",
      "url": "https://...",
      "snippet": "...",
      "rank": 1,
      "doi": "10.1234/example",
      "year": 2024,
      "authors": ["Ada Example", "Edge Research Consortium"],
      "corporate_authors": ["Edge Research Consortium"]
    }
  ],
  "errors": [
    {"engine": "brave", "reason": "BRAVE_SEARCH_API_KEY/BRAVE_API_KEY not set; skipping"}
  ]
}
```

`doi`, `year`, `authors`, and `corporate_authors` are optional. They are
emitted only when an engine supplies verifiable values, so the existing
five-field web-result shape remains compatible for general engines. The
parallel corporate-author list lets BibTeX consumers protect institution
names from person-name parsing.

## Engines

| Engine | Needs key | Key env var | Strength |
|---|---|---|---|
| `crossref` | no | optional `CROSSREF_MAILTO` | Scholarly works with canonical DOI, year, and author metadata |
| `duckduckgo` | no | — | No-key, privacy-oriented broad web baseline |
| `brave` | yes | `BRAVE_SEARCH_API_KEY` or legacy `BRAVE_API_KEY` | High-quality results, generous free tier |
| `tavily` | yes | `TAVILY_API_KEY` | Designed for AI agents, returns clean JSON |

The script never errors out when an API-key engine's key is missing — it
records a per-engine `errors` entry and continues with the rest. Pass
`--strict` to fail fast when any requested engine is unavailable.

## Routing tips

The host should pick engines by language and availability:

- Academic queries → `crossref` first, then `brave` or `tavily` for broader context
- General web queries → `duckduckgo` plus `brave` or `tavily` for triangulation
- Time-sensitive (last 24h) → `brave` (recency filter) or `tavily`
- Long-tail academic → start with `crossref`; supplement with direct arXiv when needed

`engines.md` has the full per-engine guidance.

## Boundaries

- HTTP-only. JS-rendered pages will not be readable; use a headless-browser
  skill if needed.
- DuckDuckGo scraping is best-effort —
  HTML structure changes can break it. The script logs parse failures
  individually and keeps the run going.
- Timeout, HTTP 429, and transient HTTP 5xx responses receive at most two
  retries with bounded backoff. Repeated failures remain per-engine soft
  errors. Callers must still avoid high-rate loops.
- Captcha-protected results are not bypassed. If an engine returns a
  challenge page, the parser will return zero results for that engine and
  log a warning.

Crossref is a public metadata service, not a full-text search index. The
client sends `query.bibliographic`, respects the requested result limit, and
uses canonical `https://doi.org/<doi>` URLs. Set `CROSSREF_MAILTO` to identify
your application through Crossref's polite-pool convention. See the
[Crossref REST API etiquette](https://www.crossref.org/documentation/retrieve-metadata/rest-api/rest-api-metadata-retrieval/)
and [rate-limit documentation](https://www.crossref.org/documentation/retrieve-metadata/rest-api/rest-api-metadata-retrieval/#00817).
