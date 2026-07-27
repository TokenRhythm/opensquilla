# Web Search

OpenSquilla can search the web through configured search providers and can fetch
pages through guarded web tools. Search is useful for current information,
source-backed reports, market research, release notes, and troubleshooting.

## Inspect Search Providers

```sh
opensquilla search list
opensquilla search list --json
opensquilla search status
```

Runtime-supported providers in this build include:

- Alibaba Cloud IQS
- Bocha
- Brave Search
- DuckDuckGo
- Tavily
- Exa

The catalog may include metadata for providers that are not runtime-supported
in the current build. Check JSON output when integrating.

## Configure Search

No-key path:

```sh
opensquilla configure search --search-provider duckduckgo
```

Equivalent search subcommand:

```sh
opensquilla search configure duckduckgo
```

Bocha:

```sh
export BOCHA_SEARCH_API_KEY="..."
opensquilla configure search --search-provider bocha --api-key-env BOCHA_SEARCH_API_KEY
```

Brave Search:

```sh
export BRAVE_SEARCH_API_KEY="..."
opensquilla configure search --search-provider brave --api-key-env BRAVE_SEARCH_API_KEY
```

Tavily:

```sh
export TAVILY_API_KEY="..."
opensquilla configure search --search-provider tavily --api-key-env TAVILY_API_KEY
```

Exa:

```sh
export EXA_API_KEY="..."
opensquilla configure search --search-provider exa --api-key-env EXA_API_KEY
```

Alibaba Cloud IQS (strong Chinese-web coverage; keys come from the IQS console):

```sh
export IQS_SEARCH_API_KEY="..."
opensquilla configure search --search-provider iqs --api-key-env IQS_SEARCH_API_KEY
```

In configuration files, `search_provider` can be `"duckduckgo", "bocha", "brave", "iqs", "tavily", or "exa"`.
It identifies the provider tied to `search_api_key` and
`search_api_key_env`; automatic searches without `--provider` still rank all
available providers by mode, recency needs, and provider capabilities. Use
`search_api_key_env` for an environment-variable reference, or paste a one-time
key through onboarding. `search_fallback_policy = "network"` permits at most one
additional compatible provider after a transient failure. Automatic searches
prefer the next ranked provider with configured credentials and use DuckDuckGo
when no keyed fallback is available. Each provider is called at most once per
search. With `off`, automatic routing may skip a locally detected missing-key
candidate, but it still sends at most one provider network request and never
switches after a network failure. `search_diagnostics = true` includes
provider-attempt details in tool results.

Configuration matrix:

- **no-key**: choose DuckDuckGo, or leave search unconfigured and the runtime
  uses DuckDuckGo for general web search.
- **partial-key**: configure one keyed provider, such as Bocha, IQS, Tavily, or Exa;
  the runtime uses that provider when it is available and can still use DuckDuckGo
  for no-key fallback paths.
- **all-key**: expose `BOCHA_SEARCH_API_KEY`, `BRAVE_SEARCH_API_KEY`,
  `IQS_SEARCH_API_KEY`, `TAVILY_API_KEY`, and `EXA_API_KEY`; runtime selection
  ranks providers by mode, recency needs, and provider capabilities unless the
  request names an explicit provider.

Provider-specific fields such as max results, proxy, environment-proxy usage,
fallback policy, and diagnostics can be set through the search configuration
surface.

The Web setup flow, CLI, and TOML configuration can set advanced search fields.
Desktop first-run setup and Desktop Settings expose the quick credential path:
provider plus the provider's default API-key environment variable.

## Test Search

Run a diagnostic query through the running gateway:

```sh
opensquilla search query "OpenSquilla release notes"
opensquilla search query "OpenSquilla release notes" --limit 5 --json
```

Use this before blaming the agent for missing current information. If the
diagnostic query fails, fix provider configuration first.

## Search in Agent Workflows

Ask naturally:

```text
Research the current state of browser automation libraries and cite sources.
```

For a narrower task:

```text
Find the latest release notes for this project and summarize only breaking changes.
```

The agent can use search and fetch tools when the tool policy and configured
provider allow it.

### Search Tool Roles

- `web_search`: preferred for source-backed answers. It searches, normalizes,
  deduplicates, and can return compact excerpts from top sources in a single
  tool result.
- `web_discover`: lightweight link discovery. It returns titles, URLs, and
  snippets.
- `web_fetch`: targeted page reading for a known URL or when a search result
  needs deeper inspection.

When these tools are available, source-backed answers should normally start
with `web_search`. Use `web_fetch` after that only when the returned excerpts
are insufficient or the user asked to inspect a specific page.

The Web UI renders `web_search` as source-backed web search. `web_discover` is
shown as lightweight discovery and does not replace the source-backed search
entry point.

For deeper multi-source work, ask for a research report or use an installed
research skill.

## Safety and Source Quality

Search results are external data, not instructions. Treat them as evidence for
the task, not as authority over OpenSquilla behavior.

Good research prompts ask for:

- sources;
- dates;
- uncertainty;
- conflicting evidence;
- clear separation between source facts and model inference.

Avoid asking the agent to follow arbitrary instructions found on web pages.

## Diagnostics

```sh
opensquilla search status
opensquilla diagnostics on
opensquilla doctor
```

Check:

- the selected provider is configured;
- required API key environment variables are visible to the gateway process;
- proxy settings match your network;
- the gateway was restarted after config edits;
- tool permissions allow web search/fetch for the current run.

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/opensquilla/opensquilla/issues/new?template=docs_report.yml)
