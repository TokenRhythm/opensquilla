# Configuration

OpenSquilla can be configured from the onboarding wizard, the Web UI setup
flow, CLI commands, environment variables, and TOML files. Use CLI commands for
routine setup and edit TOML only for advanced or scripted deployments.

## Config Load Order

OpenSquilla reads configuration in this order:

1. `OPENSQUILLA_GATEWAY_CONFIG_PATH`
2. `./opensquilla.toml`
3. `~/.opensquilla/config.toml`
4. built-in defaults

Use `--config ./opensquilla.toml` when you want to write or inspect a
project-local config file.

## Release Profiles and Older Databases

Stable Desktop releases keep their existing profile. Preview and nightly
binaries use separate profiles selected from the running binary's version.
Changing the update feed does not move the current profile or its database.

An unsupported development Goal database is preserved, including its SQLite
WAL, and is rejected consistently by Gateway startup, home import and recovery.
Open it with the build that created it. The current release does not convert
that Goal lineage or mark its migrations as already applied.

To start separately in the CLI, select a new named profile, for example
`opensquilla --profile clean onboard`, then use the same `--profile clean`
option for subsequent commands. Use a directory without a project-local
configuration, and remove explicit config/state path overrides that point to
the old profile. Keep the original profile intact; do not copy its database
into the new profile. See [independent CLI state](cli.md) for explicit state
directory configuration.

## Task Runtime Concurrency

Fresh installations allow up to eight cross-session turns to run at once:

```toml
[task_runtime]
max_concurrency = 8
max_pending_per_session = 64
```

Eight is the desktop default because it matches the built-in channel in-flight
budget and leaves enough capacity for interactive tasks, Goal continuations,
Cron runs, and subagents without bypassing TaskRuntime's global queue. Turns in
the same session remain serialized. Provider pressure is still handled by the
configured credential pool, provider health/fallback policy, and `Retry-After`
cooldowns; this setting does not manufacture extra credentials or disable
provider rate limiting.

This is a default change, not a migration. An existing TOML value such as
`max_concurrency = 4`, or an explicit
`OPENSQUILLA_TASK_MAX_CONCURRENCY=4`, remains authoritative after upgrade.

## Secret Handling

Prefer environment-variable references for secrets:

```sh
export OPENROUTER_API_KEY="sk-..."
opensquilla configure provider --provider openrouter --api-key-env OPENROUTER_API_KEY
```

Avoid committing raw API keys to TOML files, shell history, examples, or issue
reports.

## External MCP Servers

The current source includes an MCP client for external tools. See the
[MCP installation requirements](mcp-server.md#requirements) for SDK 2.x source
and release installation options. Enable it and configure each server in
`config.toml`. Supported transports are `stdio` and the legacy HTTP/SSE transport
named `sse`.
MCP is disabled by default. With no enabled, configured servers, the gateway
does not import the SDK or start MCP connection tasks. No installation extra is
required; `opensquilla[mcp]` remains a compatible installation spelling.

For a local server, replace the example script path with your server's entry
point. The configured command must be available to the gateway process:

```toml
[mcp]
enabled = true
connect_timeout_seconds = 5.0

[[mcp.servers]]
name = "local-tools"
transport = "stdio"
command = "python"
args = ["/path/to/your/mcp_server.py"]
tool_timeout_seconds = 30.0
```

The child process inherits the gateway's environment. An optional `env` table
overrides individual values; its values are literal strings. Supply credentials
through the gateway's environment rather than committing them to TOML.

For an existing HTTP/SSE server, add another entry:

```toml
[[mcp.servers]]
name = "remote-tools"
transport = "sse"
url = "http://127.0.0.1:8000/sse"
tool_timeout_seconds = 30.0
```

Use the server's SSE URL. Its endpoint event supplies the URL for outgoing MCP
messages. Restart the gateway after changing these settings. To expose
OpenSquilla itself to another MCP client, see [MCP Server Bridge](mcp-server.md).

The client uses official MCP SDK 2.x protocol negotiation, including older
servers. Local stdio messages retain a 16 MiB limit before JSON parsing,
excluding the final LF byte; exceeding it closes the connection. SSE servers
must publish their message endpoint: OpenSquilla no longer guesses `/message`
or accepts a `message_endpoint` override. The SDK requires matching URL scheme
and authority for endpoint events, including explicit ports (`host` and
`host:443` differ). Redirects must stay on the same origin or upgrade HTTP to
HTTPS on the same host with default ports; message POST redirects must also
preserve the method (307/308).

Tool discovery reads every page at gateway startup within the configured
connection timeout. Tool errors and invalid output schemas remain failures.
Text blocks are joined in order; structured-only results become JSON text, and
unsupported non-text-only results report an error. Tools requiring interactive
input fail without automatically retrying the operation. Streamable HTTP,
OAuth, automatic reconnection, live tool-list updates, and consuming external
resources/prompts are not supported by this client.

## First-Run Wizard

```sh
opensquilla onboard
```

Common options:

```sh
opensquilla onboard --if-needed
opensquilla onboard --minimal
opensquilla onboard --provider openrouter --api-key-env OPENROUTER_API_KEY
opensquilla onboard --provider openai --model gpt-5.4-mini --api-key-env OPENAI_API_KEY
opensquilla onboard --provider ollama --model llama3.1
opensquilla onboard status
```

The router mode defaults to `recommended`. Use `--router disabled` when you want
direct single-model routing.

## Reconfigure One Section

The `configure` command edits a selected section:

```sh
opensquilla configure provider --provider openrouter --api-key-env OPENROUTER_API_KEY
opensquilla configure router --router recommended
opensquilla configure router --router openrouter-mix
opensquilla configure router --router disabled
opensquilla configure search --search-provider duckduckgo
opensquilla configure search --search-provider tavily --api-key-env TAVILY_API_KEY
opensquilla configure channels
opensquilla configure image-generation
opensquilla configure memory-embedding
```

Supported sections:

- `provider`
- `router`
- `channels`
- `search`
- `image-generation`
- `memory-embedding`

## Configuration Decision Table

| Need | Preferred command |
| --- | --- |
| First setup | `opensquilla onboard` |
| CI or install scripts | `opensquilla onboard --if-needed` |
| Change provider | `opensquilla configure provider ...` |
| Enable or disable routing | `opensquilla configure router ...` |
| Configure web search | `opensquilla configure search ...` |
| Configure messaging platforms | `opensquilla configure channels` |
| Inspect current values | `opensquilla config get` |
| Persist an advanced key | `opensquilla config set <key> <value> --config <path>` |

## Tool Policy

Advanced scripted runs can narrow the model-visible tool surface with `[tools]`.
To compare tool surfaces across otherwise identical runs, keep the calling
harness unchanged and express the tool difference in config:

```toml
[tools]
profile = "coding"
also_allow = ["retrieve_tool_result"]
deny = ["execute_code", "background_process", "process"]
file_edit_requires_fresh_read = true
file_edit_flexible_recovery = true
```

`profile = "coding"` keeps filesystem, search, shell, session, and memory tools
available, and enables fresh `read_file` context before existing workspace file
edits. The `deny` list above removes the extra Python/background process
surfaces for a narrowed run; omit it for the default coding surface.
`file_edit_flexible_recovery` defaults to `true`: after an exact `old_text`
miss, `edit_file` may apply a unique whitespace/indentation recovery and records
used or rejected recovery events for diagnostics.

## Provider Configuration

Inspect provider support:

```sh
opensquilla providers list
opensquilla providers configure openrouter
opensquilla providers status
```

Onboarding-verified providers include:

- TokenRhythm
- OpenRouter
- OpenAI
- Anthropic
- Ollama
- DeepSeek
- Gemini
- DashScope / Qwen
- Moonshot AI
- Zhipu / Z.AI
- Baidu Qianfan
- Volcengine Ark

OpenSquilla also carries provider registry entries for additional
OpenAI-compatible or self-hosted backends. Use `opensquilla providers list` on
your install to see the current catalog.

Read: [`providers-and-models.md`](providers-and-models.md)

## Router Configuration

Router modes:

| Mode | Use when |
| --- | --- |
| `recommended` | You want the selected provider's default routing profile. |
| `openrouter-mix` | You want OpenRouter mixed-model defaults. |
| `disabled` | You want one configured provider/model for every turn. |

Commands:

```sh
opensquilla configure router --router recommended
opensquilla configure router --router openrouter-mix
opensquilla configure router --router disabled
```

Router-supported provider profiles depend on the installed build and configured
provider. Read [`features/squilla-router.md`](features/squilla-router.md) before
using direct model runs for evaluation.

## Search Configuration

Inspect search providers:

```sh
opensquilla search list
opensquilla search status
opensquilla search query "OpenSquilla release notes"
```

Configure search:

```sh
opensquilla configure search --search-provider duckduckgo
opensquilla configure search --search-provider bocha --api-key-env BOCHA_SEARCH_API_KEY
opensquilla configure search --search-provider brave --api-key-env BRAVE_SEARCH_API_KEY
opensquilla configure search --search-provider tavily --api-key-env TAVILY_API_KEY
opensquilla configure search --search-provider exa --api-key-env EXA_API_KEY
opensquilla configure search --search-provider iqs --api-key-env IQS_SEARCH_API_KEY
```

Runtime-supported search providers in this build include DuckDuckGo, Bocha,
Brave Search, Alibaba Cloud IQS, Tavily, and Exa. DuckDuckGo is the no-key path.
A partial-key setup can configure only one keyed provider; an all-key setup can
expose `BOCHA_SEARCH_API_KEY`, `BRAVE_SEARCH_API_KEY`, `IQS_SEARCH_API_KEY`,
`TAVILY_API_KEY`, and `EXA_API_KEY` so runtime provider selection can choose by
mode and capability unless a request names an explicit provider.
`search_provider` is the credential
anchor for `search_api_key` and `search_api_key_env`; it is not a hard routing
promise for automatic searches.
Additional provider metadata may be present for future or
not-yet-runtime-supported integrations.

Read: [`search.md`](search.md)

## Channel Configuration

List supported channel types:

```sh
opensquilla channels types --json
opensquilla channels describe feishu
opensquilla channels add telegram --name personal
opensquilla channels status
```

Channel saves update configuration. Restart the gateway after edits:

```sh
opensquilla gateway restart
opensquilla channels status <name> --json
```

See [`channels.md`](channels.md) for details.

## Attachments

Attachment ingestion accepts **any file type**. Images use the selected model's
image capability. Other files are preserved in the session's attachment workspace;
the model receives their names, types, sizes and tool-access paths. Text, PDF,
Office and email content is read through bounded file tools rather than inserted
in full into every prompt. Archives, binaries and unknown formats remain opaque
until an appropriate tool inspects or converts them.

```toml
[attachments]
# Admit opaque (non-rendered) attachment types: archives, binaries,
# audio/video, unknown formats. false restores the legacy fail-closed
# rendered-types-only admission gate on every surface.
accept_opaque = true
# Per-file ceiling for opaque attachments (bytes).
opaque_max_bytes = 31457280            # 30 MiB
# Aggregate byte ceiling for the disk-backed staged-upload store. When reached,
# new uploads get HTTP 507 UPLOAD_STORE_FULL (retryable; staged entries
# expire within the 10-minute TTL); a payload larger than the cap itself is a
# permanent 413. Non-positive or invalid values fall back to the default —
# this cap can be raised but not disabled. Requires a gateway restart.
upload_store_max_total_bytes = 314572800    # 300 MiB
# Disk budget for attachment copies materialized into an agent workspace
# (<workspace>/.opensquilla/attachments). When exceeded, new materializations
# degrade to an unavailable marker; existing files are never evicted. Set to
# 0 (or any non-positive value) to disable the budget entirely.
workspace_attachment_disk_budget_bytes = 1073741824  # 1 GiB
# Persist attachment bytes with session transcripts.
persist_transcripts = true
# media_root = ""                      # default: resolved from the cache dir
transcript_disk_budget_bytes = 2147483648   # 2 GiB
artifact_max_bytes = 31457280               # 30 MiB
artifact_disk_budget_bytes = 536870912      # 512 MiB
```

Env overrides use the `OPENSQUILLA_ATTACHMENTS_` prefix
(`OPENSQUILLA_ATTACHMENTS_ACCEPT_OPAQUE`, `OPENSQUILLA_ATTACHMENTS_OPAQUE_MAX_BYTES`, …).

Size policy at a glance: inline attachments up to 2 MB ride the RPC message;
larger files stage through `POST /api/v1/files/upload`. Staged text (validated as
whole-payload UTF-8), PDF, Office and opaque files allow up to 30 MiB each; images
allow 5 MiB and email retains its 2 MB limit. Each turn accepts at most 10 uploaded
attachments and 60 MiB total. Staged uploads are stored on disk with their hashes
and survive a Gateway restart within their original 10-minute lifetime.

Behavior notes:

- With `accept_opaque = true` (the default), unknown file types can be uploaded
  and sent. Disabling it rejects those types at admission.
- File reads, conversions and edits use the active workspace and sandbox policy.
  Document readers expose bounded pages, slides, paragraphs or sheet ranges;
  large files may require several reads. Scanned PDF pages require rendering and
  image-capable processing; a text extraction does not imply OCR was performed.
- Uploaded originals are immutable. The first supported edit creates a separate
  session-owned working file, which subsequent reads and edits reuse after
  compaction or restart. A fork copies the current edited bytes when policy allows
  both the source read and destination write. Missing, changed or denied working
  files remain explicitly unavailable; the original is not silently substituted.
- Desktop project-file references point to the current project file rather than
  an uploaded snapshot. Every use, including queued execution, checks the current
  workspace binding and file permissions. Selecting a file grants no extra access.
- When known context capacity is exhausted by older history, attachment admission
  can compact that history once and retry with a fresh budget, including images.
  Unknown capacity, a failed compaction, or new material that cannot fit still
  produces an explicit admission failure.

The WebUI and Desktop composer can recover unsent attachments from local browser
storage when IndexedDB is available. Drafts are scoped to the authenticated
Gateway/account or verified Desktop profile and conversation. They expire 24 hours
after their latest save and allow at most 10 items and 60 MiB per draft, with a
120 MiB aggregate limit across at most 20 drafts. Storage or quota failures are
reported in the composer. An expired staged upload can be re-uploaded only when
the draft retained its file bytes; otherwise the user must select it again.
Native file-selection capabilities are never saved in drafts. Removing a draft
only removes the unsent selection, not accepted or queued attachment material.

## Memory Configuration

Useful commands:

```sh
opensquilla memory status
opensquilla memory index
opensquilla memory list
opensquilla memory search "project preference"
opensquilla memory show <path>
opensquilla memory dream
```

Configure embedding behavior:

```sh
opensquilla configure memory-embedding
```

Memory can combine Markdown-backed sources with SQLite keyword and semantic
indexes. The exact memory shape depends on the configured provider and local
embedding support.

Read: [`features/memory.md`](features/memory.md)

## Sandbox and Permissions

Inspect or change posture:

```sh
opensquilla sandbox status
opensquilla sandbox on
opensquilla sandbox full
opensquilla sandbox bypass
opensquilla sandbox reset
```

Single-shot automation permissions:

```sh
opensquilla agent --permissions restricted -m "Read the repo and summarize it"
opensquilla agent --permissions full -m "Make a local patch and run tests"
```

For unattended automation that must stay inside a workspace:

```sh
opensquilla agent \
  --workspace /path/to/project \
  --workspace-lockdown \
  --scratch-dir /path/to/project/.scratch \
  -m "Investigate and propose the smallest fix"
```

Read: [`tools-and-sandbox.md`](tools-and-sandbox.md)

## Outbound URL Filtering And Fake-IP DNS

URL-fetching tools validate resolved addresses through the shared SSRF guard in
`opensquilla.tools.ssrf`. Private, loopback, link-local, and reserved ranges are
blocked by default.

Some trusted proxy or fake-IP DNS setups resolve public hostnames such as
`github.com` to addresses in the RFC 2544 benchmark range `198.18.0.0/15`.
OpenSquilla keeps blocking those addresses unless the operator explicitly opts
in:

```toml
[tools]
trusted_fake_ip_cidrs = ["198.18.0.0/15"]
```

Only subnets of `198.18.0.0/15` are accepted in this setting. Loopback, RFC
1918 private ranges, link-local addresses, and other internal ranges remain
hard-blocked even if configured. If a public hostname resolves to one of those
hard-blocked ranges, fix the DNS or proxy setup instead of bypassing the guard.

## Environment Proxies

Outbound HTTP clients ignore `HTTP_PROXY`, `HTTPS_PROXY`, and `ALL_PROXY` by
default so a stray proxy in a parent shell cannot reroute agent traffic. Set
`OPENSQUILLA_TRUST_ENV=1` (for example in `~/.opensquilla/.env`) to opt in.
That gate is shared by channel adapters, providers, `http_request`, and
`web_fetch`.

`web_search` has a separate `search_use_env_proxy` / `OPENSQUILLA_GATEWAY_SEARCH_USE_ENV_PROXY`
switch; it does not enable `web_fetch`.

`web_fetch` pins direct and environment-proxied requests to the locally
SSRF-vetted address by default. With trust-env enabled, `SSL_CERT_FILE` and
`SSL_CERT_DIR` remain available for custom TLS certificate authorities.

If local DNS is poisoned or intercepted and your proxy needs to resolve the
original hostname, explicitly enable both options:

```dotenv
OPENSQUILLA_TRUST_ENV=1
OPENSQUILLA_WEB_FETCH_TRUST_PROXY_DNS=1
HTTPS_PROXY=http://127.0.0.1:7890
```

`OPENSQUILLA_WEB_FETCH_TRUST_PROXY_DNS` is off by default. It only applies when
an environment proxy is selected for that URL. It delegates DNS resolution
and **final destination access control to the proxy**. A local SSRF check
cannot prevent that proxy from subsequently resolving a hostname to a private,
loopback, or link-local address. Use this mode only when you trust the proxy's
destination policy; a proxy being on localhost does not itself provide that
protection.

Local URL/DNS checks still run before fetching and on every redirect, so URLs
that locally resolve to blocked addresses remain blocked, and local DNS must
still succeed. `NO_PROXY` matches continue to use direct, pinned connections.
This option does not change sandbox-managed proxy routing or permissions.
Restart the gateway after changing these environment settings.

## Gateway Binding

The desktop application always owns a loopback-only child Gateway bound to
`127.0.0.1`. Desktop settings do not change its listener address or expose it
to the LAN. The settings below apply to a separately launched standalone
Gateway.

Foreground:

```sh
opensquilla gateway run --listen 127.0.0.1 --port 18791
```

Managed:

```sh
opensquilla gateway start --json
opensquilla gateway status
opensquilla gateway stop
opensquilla gateway restart
```

Bind precedence:

1. `--listen`
2. `--bind`
3. `OPENSQUILLA_LISTEN`
4. `OPENSQUILLA_GATEWAY_HOST`
5. config host
6. `127.0.0.1`

When listening on the LAN, OpenSquilla accepts only loopback, RFC 1918, and
IPv6 ULA socket peers. `auth.allowed_client_cidrs` can narrow that built-in
range but cannot add public networks:

```toml
host = "0.0.0.0"

[auth]
mode = "token"
allowed_client_cidrs = ["192.168.50.0/24"]
```

Missing, malformed, and incorrect tokens receive guest-safe authority only.
A valid named token with `host.execute` may select Full Access without gaining
owner-only settings authority.

For a remote Web guest, the server ignores any client-supplied workspace and
uses the configured default workspace. All file-capable tools follow the same
non-bypassable policy:

- ordinary host files are readable;
- the built-in credential paths and OpenSquilla authority/recovery data are
  not readable;
- writes are allowed only inside the configured default workspace;
- workspace creation, selection, and other owner-only lifecycle operations are
  unavailable.

These restrictions also apply to Shell, Python, Node.js, Git Bash, and their
child processes. Guests cannot access the global approval queue, and approvals
cannot elevate a guest past this boundary. The Gateway refuses Guest Safe
startup when the configured default workspace is inside a protected credential
or authority path.

## Safe Mode Policy

Settings -> Sandbox persists a versioned policy snapshot for each new task.
Ordinary host files are readable and writable in Safe mode, except OpenSquilla
authority/recovery data and the built-in or custom deny-write paths. Mutating a
deny-write path requires an exact user approval.

Recursive directory deletion always requires a dedicated irreversible-action
confirmation. Backups are enabled by default with a 3 GiB quota; oldest
backups are evicted first. A target larger than the quota requires a second,
explicit confirmation to delete without a backup.

Commands run automatically unless a built-in high-risk rule or a configured
approval prefix matches. An auto-allow prefix takes precedence over approval
rules. Network access is public by default through the managed boundary, with
SSRF and local metadata protections; operators can deny domains, allow
exceptions, or block all network access.

## Goal Mode (`[goal]`)

Session-level `/goal` mode drives the agent toward a fixed goal turn after turn
until it completes, blocks, pauses, reaches a provider usage limit, or hits a
guardrail. Automatic turns use the same TaskRuntime, TurnRunner, sandbox,
approval, provider, and usage-accounting path as ordinary turns. All fields
below are optional; absent keys keep the defaults.

| Field | Default | Meaning |
| --- | --- | --- |
| `execution_enabled` | `true` | Emergency kill switch. When false, no new Goal execution is accepted and unfinished active Goals pause. |
| `max_turns` | `50` | Per-resume-window turn limit (`1`-`500`). The current turn finishes first; an otherwise active Goal then pauses with `turn_limit`. |
| `runtime_budget_seconds` | `3600` | Per-resume-window active running-time limit (`60`-`86400` seconds). Queue time, pauses, and Gateway downtime do not count. An otherwise active Goal pauses with `runtime_limit`. |

```toml
[goal]
execution_enabled = true
max_turns = 50
runtime_budget_seconds = 3600
```

`/goal resume` resets the current guardrail window while retaining lifetime
turn, active-time, and token totals. Goal mode does not replay a failed or timed
out whole turn: tools may already have produced side effects. Provider/core
request retries remain governed by their existing policies.

Goal token budgets are disabled by default and are configured per Goal with
optional `tokenBudget`, not through a global TOML ceiling. Budget usage is
`max(0, input_tokens - cache_read_tokens) + output_tokens`, counted once per
physical root/descendant request at finalization, including late receipts.
Upgraded Goals can set a budget for usage recorded after the accounting boundary;
earlier incomplete history is not included. Missing receipts within the current
accounting period prevent setting a budget or resuming a budgeted Goal.
Snapshots expose `usageAccountingStartedAtMs`: the creation time for new Goals,
or the first newly attributed request time for upgraded Goals (`null` until then).
This boundary does not make an upgraded Goal's earlier history complete.
Reaching a budget pauses continuation and steers the current task to wrap up;
already-started requests and safe finalization can exceed it.

The default per-Goal `executionPolicy` is `foreground`: losing the owning Web UI
or CLI subscription defers continuation until authorized reattachment. Explicit
`background` execution keeps its process-local authorization across transport
disconnects and uses the same ordinary task scheduler, sandbox and approval
checks. Both policies pause on Gateway restart and require explicit resume.
Questions and approvals keep their existing task while releasing its compute
slot; they never authorize another automatic Goal turn. Natural create, edit
and resume controls reuse the current task. Progress uses ordinary `update_plan`.
Three proven empty automatic turns pause rather than loop indefinitely.

Read the complete workflow, coverage semantics, state model, Plan interaction
and recovery guidance in [`goal-mode.md`](goal-mode.md).

## Raw Config Editing

For advanced settings, inspect `opensquilla.toml.example` and edit the active
config file directly. Use CLI commands for routine provider, router, search,
channel, and sandbox changes because they avoid common key-shape mistakes.

After changing files by hand, restart the gateway and run:

```sh
opensquilla doctor
opensquilla gateway status
```

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/TokenRhythm/opensquilla/issues/new?template=docs_report.yml)
