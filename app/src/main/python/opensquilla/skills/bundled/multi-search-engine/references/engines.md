# Engine selection guide

Per-engine notes — when each is good, where it fails, and what an
appropriate query looks like.

## No-key engines (always available)

### Crossref

Uses the official Crossref REST API `GET /v1/works` endpoint with
`query.bibliographic`. It returns scholarly metadata rather than arbitrary web
pages. Results with a DOI use the canonical `https://doi.org/<doi>` URL and
retain publication year and author names when Crossref provides them.

No API key is required. `CROSSREF_MAILTO` is optional and, when set, is sent as
the `mailto` parameter under Crossref's polite-pool convention. Crossref asks
clients to identify themselves, cache responsibly, respect rate-limit headers,
and avoid excessive concurrent traffic. The script has a finite retry policy
for timeout, 429, and transient 5xx responses; it does not promise service
availability. Official guidance:

- <https://www.crossref.org/documentation/retrieve-metadata/rest-api/>
- <https://www.crossref.org/documentation/retrieve-metadata/rest-api/rest-api-metadata-retrieval/>

Use when: a downstream workflow needs verifiable DOI, author, and publication
year metadata, especially for a bibliography.

### DuckDuckGo

Implementation uses the HTML-form endpoint at `html.duckduckgo.com`.
Strengths: privacy-friendly, no rate limit at moderate volumes, returns a
mix of public-web sources without strong personalization. Weaknesses: less
recency-tuned than Brave; result ranking shifts week-to-week.

Use when: general web search where you want a "neutral" baseline.

## API-key engines

### Brave Search API

`BRAVE_SEARCH_API_KEY` from <https://brave.com/search/api/>. Legacy
`BRAVE_API_KEY` is also accepted for migrated OpenClaw setups. 2k queries/month
free tier. Returns clean JSON with title, URL, description, and recency
hints.

Use when: building a deep-research pipeline that runs at scale; need
recency filtering.

### Tavily

`TAVILY_API_KEY` from <https://tavily.com>. Designed for AI agent
consumption — returns short summaries alongside results. Free tier
available.

Use when: the agent needs ready-to-use snippets rather than full source
HTML.

## Routing decision tree

```
Is the query scholarly or bibliography-oriented?
  yes → crossref (+ brave/tavily for broader context)
   no → continue
Is the topic time-sensitive (last 24h)?
  yes → brave or tavily
   no → continue
Is BRAVE_SEARCH_API_KEY or BRAVE_API_KEY set?
  yes → brave + duckduckgo
   no → duckduckgo
```

## Per-engine result limits

Default `--limit 10` is safe across engines. Higher limits:

- DuckDuckGo: HTML returns up to ~30; beyond that, scrape the next page
- Crossref: `rows` is set to the requested limit; keep requests moderate
- Brave: API tops out at 20 per request
- Tavily: 5 results on the free tier, 20 on paid

## Anti-patterns

- **Asking N engines in a tight loop without jitter**: rate limits will
  cascade. Sleep 200-500ms between requests, more for scraping engines.
- **Trusting a single engine's top result as ground truth**: ranking is
  noisy. Cross-check with a second engine.
- **Running every engine on every query**: redundant. Pick 2-3 by topic.

## Maintenance notes

The DuckDuckGo HTML parser can break when the upstream site reshuffles its
CSS. Treat the parser as
expected-to-fail-eventually code: the script logs parse failures rather
than crashing, and the calling agent should be able to fall back to
another engine on the spot. Routine maintenance is to test each parser
against a known query monthly.
