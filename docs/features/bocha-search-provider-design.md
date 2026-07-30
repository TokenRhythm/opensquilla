# Bocha Search Provider Design

Date: 2026-06-25

## Goal

Add Bocha as a first-class OpenSquilla search provider using the existing search
runtime, provider catalog, onboarding, settings, diagnostics, and test surfaces.
The user experience should be: configure a Bocha key, then normal `web_search`
and `web_discover` flows can use it automatically when it is the best available
provider.

This design intentionally avoids a visible China-region strategy or profile. Bocha
is a normal provider with capabilities, credentials, ordering, and diagnostics.

## Non-Goals

- Do not add a visible `cn`, `domestic`, or region routing profile.
- Do not add Zhipu, a separate reader abstraction, or multi-provider result fusion.
- Do not change `search_provider` into a hard routing promise. It remains the
  credential anchor for `search_api_key` and `search_api_key_env`.
- Do not add user-facing provider priority controls in the MVP.
- Do not broaden fallback to empty-result or low-quality-result retries by default.

## Existing Runtime Boundaries

OpenSquilla already has the right extension points:

- Provider specs and provider factories live in `opensquilla.search.registry`.
- Runtime availability and provider ordering live in `SearchRuntimeConfig` and
  `ResolvedSearchRuntime`.
- `web_search` uses the canonical search pipeline.
- `web_discover` has a lighter provider path and must be updated separately.
- Onboarding and settings consume the provider catalog payload.
- `search_fallback_policy` currently supports `off` and `network`.

The implementation should use those surfaces instead of introducing a second
search router.

## Provider Behavior

Add `BochaSearchProvider` under `src/opensquilla/search/providers/bocha.py`.

Expected API shape:

- Endpoint: `https://api.bochaai.com/v1/web-search`
- Authentication: `Authorization: Bearer <api key>`
- Default environment variable: `BOCHA_SEARCH_API_KEY`
- Request options:
  - query text
  - max result count
  - freshness mapping when `SearchOptions.recency` is set
  - summary enabled when supported

The provider should normalize Bocha results into `SearchResult` fields:

- title from `name`
- url from `url`
- snippet from `snippet`
- provider content from `summary` when present
- published timestamp from `datePublished`
- source/site metadata from `siteName`, `displayUrl`, or equivalent fields

Bocha summaries should count as useful provider content so the canonical pipeline
does not fetch pages unnecessarily when Bocha already returned enough source
text.

## Provider Spec

Register Bocha with a `SearchProviderSpec`:

```python
SearchProviderSpec(
    provider_id="bocha",
    requires_api_key=True,
    env_key="BOCHA_SEARCH_API_KEY",
    capabilities=frozenset({"web", "freshness", "content"}),
)
```

Do not claim `domain_filter` unless the Bocha API supports an equivalent feature
and tests cover it.

## Runtime Ordering

Bocha should participate in the existing automatic ordering. Suggested MVP
ordering:

```python
_GENERAL_TIE_BREAKER = ("bocha", "tavily", "brave", "exa", "duckduckgo")
_TECHNICAL_TIE_BREAKER = ("exa", "bocha", "brave", "tavily", "duckduckgo")
_FRESHNESS_TIE_BREAKER = ("bocha", "tavily", "brave", "exa")
```

Rationale:

- General and freshness searches should prefer Bocha when it is configured,
  because it is the targeted improvement for reliable China-accessible search.
- Technical searches should keep Exa first because its existing role is stronger
  for semantic and content-oriented research.
- No-key and partial-key users are protected by the existing availability filter:
  unavailable keyed providers are skipped before ordering is returned.

## Fallback Policy

Keep fallback semantics narrow:

- `off`: send at most one provider network request and surface its error;
  automatic routing may skip a missing-key candidate before any request.
- `network`: after a provider-classified transient failure, try at most one
  additional compatible provider. Automatic routing prefers the next ranked
  provider with configured credentials and uses DuckDuckGo when no keyed
  fallback is available.

Do not fallback on:

- empty results
- low-quality results
- authentication or missing-key errors
- ordinary 4xx responses
- blocked/challenge responses
- parse errors

This preserves cost, latency, and predictability for users who configured
multiple paid providers.

## Configuration And Setup

Supported key resolution should remain layered:

1. active provider inline configured key
2. active provider configured env var
3. provider spec default env var

Bocha should support all existing search setup paths:

- CLI configuration
- onboarding setup engine
- gateway setup payload
- desktop settings provider catalog
- web UI setup provider catalog

The MVP should update fallback/static provider lists where the UI has offline
defaults, but the canonical source of truth remains the backend provider catalog.

## Diagnostics

Search runtime status should show Bocha like other providers:

- available/unavailable
- credential source
- credential configured boolean
- skipped reason
- capabilities

Diagnostics must not expose the raw API key.

A live provider probe can be added later, but it is not required for the MVP if
existing status remains buildability/configuration based.

## Testing

Required tests:

- Provider response normalization from a representative Bocha payload.
- Missing-key behavior and credential source resolution for Bocha.
- Runtime ordering for:
  - no keyed providers
  - only Bocha configured
  - Bocha plus existing keyed providers
  - technical mode
  - freshness/news mode
- `web_search` accepts `provider="bocha"` and `provider="auto"` can select Bocha.
- `web_discover` accepts and can build Bocha when configured.
- Onboarding catalog includes Bocha with `BOCHA_SEARCH_API_KEY`.
- Documentation and frontend catalog contract tests include Bocha.

Optional live tests:

- A manually gated Bocha smoke test using `BOCHA_SEARCH_API_KEY`.
- The live test must be opt-in and must not run in normal CI.

## Documentation

Update:

- `docs/search.md`
- `docs/configuration.md`
- relevant onboarding or setup docs if provider lists are repeated there

Document Bocha as a normal runtime-supported provider. Avoid describing it as a
region strategy.

## Acceptance Criteria

- A user with only `BOCHA_SEARCH_API_KEY` configured can run normal automatic web
  search successfully.
- Existing users with only Brave, Tavily, Exa, or DuckDuckGo keep the same
  behavior except for Bocha appearing in provider catalogs when available.
- All current fallback semantics remain unchanged.
- Bocha appears in CLI/onboarding/settings provider lists.
- Tests cover provider mapping, ordering, configuration, and documentation
  contracts.
