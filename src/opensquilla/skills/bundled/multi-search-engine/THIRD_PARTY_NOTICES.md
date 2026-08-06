# Third-party notices for `multi-search-engine` skill

Source: ClawHub `multi-search-engine`
(<https://clawhub.ai/multi-search-engine>, MIT-0).

## Runtime dependencies

Reuses `httpx` and `beautifulsoup4`, both in OpenSquilla default
dependencies. No additional packages needed.

## Engine API terms

Crossref's public REST API provides scholarly metadata without an API key;
use is subject to Crossref's REST API etiquette and rate limits. The optional
`CROSSREF_MAILTO` parameter identifies the client for polite-pool use. See:

- <https://www.crossref.org/documentation/retrieve-metadata/rest-api/>
- <https://www.crossref.org/documentation/retrieve-metadata/rest-api/rest-api-metadata-retrieval/>

Brave and Tavily each have their own terms of service. Users must comply with
those terms when using their respective API keys. DuckDuckGo HTML access is
subject to its robots.txt and terms. The client uses bounded retries for
timeout, HTTP 429, and transient HTTP 5xx failures, but callers remain
responsible for keeping request volume reasonable.

Crossref support reuses the existing `httpx` runtime dependency and adds no
package or vendored code.

## License

The ClawHub source is MIT-0. The OpenSquilla project license is Apache-2.0.
