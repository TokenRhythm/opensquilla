# OpenSquilla Privacy Policy

OpenSquilla is a local-first desktop and CLI application. This policy describes
what project-distributed OpenSquilla software stores locally, what it may send
over the network, and how users can opt out or delete local data.

This policy covers OpenSquilla release artifacts published by the OpenSquilla
project. Third-party AI providers, search providers, operating systems, app
stores, package registries, and GitHub are governed by their own policies.

## Local Data

OpenSquilla stores user configuration, sessions, logs, memory, scheduler state,
cache, and provider settings on the user's machine. The default CLI/gateway
state lives under `~/.opensquilla`. The Electron desktop app also uses the
platform Electron `userData` directory for desktop-specific configuration,
encrypted credentials when Electron `safeStorage` is available, and gateway
logs.

OpenSquilla does not require an OpenSquilla account. Provider API keys are
configured by the user and are kept locally as environment variables, local
configuration references, `.env` files, or desktop encrypted storage depending
on the installation path and setup choices.

## Provider Requests

OpenSquilla sends prompts, messages, tool results, selected files, or generated
context to third-party AI providers only when the user configures a provider and
starts a workflow that uses that provider. The exact data sent depends on the
active provider, model, command, channel, skill, and user-selected context.

Users should review their configured provider's terms and privacy policy before
using external models. OpenSquilla cannot control how an external provider
stores, logs, filters, trains on, or processes requests after the provider API
receives them.

## Search, Channels, And Integrations

Features such as web search, channel connectors, GitHub workflows, browser
automation, or other integrations may contact external services when the user
configures and invokes them. OpenSquilla does not send those requests unless the
corresponding feature is enabled by configuration or user action.

## Network Observability Controls

OpenSquilla groups background network observability and the optional
pseudonymous installation identifier attached to official TokenRhythm API
requests under one switch. Enable it to disable automatic install telemetry,
daily aggregate usage telemetry, passive update checks, automatic desktop
update checks, and that TokenRhythm request identifier. Changes to the
TokenRhythm identifier policy apply to the next request without requiring a
restart:

```sh
OPENSQUILLA_PRIVACY_DISABLE_NETWORK_OBSERVABILITY=true
```

The same control can be set in configuration:

```toml
[privacy]
disable_network_observability = true
```

Legacy environment variables remain honored for compatibility:

```sh
OPENSQUILLA_TELEMETRY_DISABLED=true
OPENSQUILLA_UPDATE_CHECK_DISABLED=true
```

`OPENSQUILLA_TELEMETRY_DISABLED=true` also suppresses the optional TokenRhythm
installation identifier. Setting only
`OPENSQUILLA_UPDATE_CHECK_DISABLED=true` does not suppress it.

Manual user-initiated actions may still contact network services after user
intent, including release downloads and configured providers, search, channels,
automation, or integrations. Update-availability checks, including
`opensquilla version --check` and the desktop manual check, do not bypass the
unified or legacy opt-out controls.

## Installation Telemetry

OpenSquilla uses pseudonymous installation telemetry to estimate install
counts, version adoption, and runtime compatibility. Telemetry is sent on first
gateway startup and once per OpenSquilla version. Uploads use a short timeout
and never block startup.

Telemetry payloads include:

- schema version
- locally generated stable `install_id` digest
- OpenSquilla version
- event type, such as `install` or `version_seen`
- install method, such as `pip`, `source`, `docker`, `desktop`, or `unknown`
- operating system, OS version, CPU architecture, and Python major/minor version
- first-seen and sent timestamps
- CI/test-environment marker

The `install_id` is a local one-way SHA-256 digest derived from usable MAC
addresses, then local IP addresses when no MAC is available, with a random
persisted fallback. Raw MAC addresses and raw IP addresses are not uploaded.

Telemetry does not include usernames, hostnames, local paths, API keys,
provider configuration, chat content, session content, memory content, agent
content, file names, or file contents. Source IP addresses may be visible to
HTTP servers at the transport layer, but are not part of the telemetry payload.

Use the unified network observability switch above to opt out before startup.
The legacy telemetry opt-out `OPENSQUILLA_TELEMETRY_DISABLED=true` remains
honored for compatibility.

CI and test environments automatically suppress installation telemetry before
an installation identifier is generated or uploaded.

Advanced deployments can direct installation and usage telemetry to independent
routes on their own service:

```sh
OPENSQUILLA_TELEMETRY_ENDPOINT=https://example.com/v1/install
OPENSQUILLA_USAGE_TELEMETRY_ENDPOINT=https://example.com/v1/usage
```

## TokenRhythm Installation Identifier

By default, OpenSquilla may add this optional header to requests sent directly
to the official TokenRhythm HTTPS API:

```http
X-OpenSquilla-Install-Id: <current install_id>
```

This is a pseudonymous installation-level identifier. It is stable across
sessions and reuses the same locally persisted `install_id` described above,
including its MAC-address, local-IP, and random persisted fallback order. Raw
MAC addresses and raw IP addresses are never placed in the header or sent as
part of the request. Identifier resolution happens in the background; if it is
not ready or fails validation, OpenSquilla omits the header and continues the
request normally.

The header is allowed only for direct API targets on
`https://tokenrhythm.studio` and `https://api.tokenrhythm.studio`, using the
default HTTPS port or an explicit port `443`. OpenSquilla does not attach it to
HTTP URLs, URLs with user information, nonstandard ports, lookalike domains,
custom proxies, OpenRouter, other providers, browser registration pages,
returned image or CDN downloads, or redirected nonofficial targets. It is not
placed in request bodies or query strings, and provider traces record only
whether it was present, not its value. The raw value is also excluded from
logs, errors, and serialized configuration.

The unified network-observability switch and the legacy telemetry opt-out both
suppress generation and transmission of this header. CI and test environments
suppress it automatically. The legacy update-check opt-out alone does not.

TokenRhythm services must treat this header as optional and untrusted. It must
not be used for authentication, authorization, billing, rate limiting, or
anti-abuse decisions.

## Daily Aggregate Usage Telemetry

OpenSquilla uses the same telemetry service with a dedicated `/v1/usage` route
and the unified network observability switch for content-free daily usage
aggregates. It records only completed top-level interactive turns. While the
gateway is running, it attempts to upload pending cumulative UTC-day snapshots
at startup and once per hour, including the current day. Heartbeats, scheduled
jobs, subagents, and incomplete turns are excluded.

Daily payloads include the existing `install_id`, OpenSquilla version, UTC day,
send timestamp, a retry-stable event ID, completed conversation count, and
aggregate input, output, cached, and cache-write token counts. They do not
include prompts, responses, provider or model names, channels, session
identifiers, costs, tools, file names, or file contents. Failed uploads remain
pending locally and are retried later.

## Logs And Diagnostics

OpenSquilla writes local logs for gateway, desktop, workflow, and troubleshooting
purposes. Logs may include command names, runtime errors, provider identifiers,
timestamps, local status, and diagnostic context. Users should review logs
before sharing them publicly because logs may reflect local configuration or
workflow details.

## Updates And Downloads

OpenSquilla release metadata and downloads are hosted on GitHub Releases and an
Alibaba Cloud OSS mirror. Desktop channel discovery currently reads a small OSS
manifest; the selected versioned update feed or asset may then come from GitHub
or OSS. These requests may expose standard request metadata, such as IP address
and user agent, to those hosts and network intermediaries. Desktop updater
requests override electron-updater's per-install staging header with one fixed,
non-user-specific value; OpenSquilla
does not use that header for device identification or staged rollout. Release
checksums are published in `SHA256SUMS` when release assets are generated. For
unsigned Windows builds, OpenSquilla fetches the canonical `SHA256SUMS` from the
matching GitHub Release, streams the installer from the selected source into an
application-owned directory, and reveals it only after SHA-256 verification.
The app does not automatically execute that installer.

The unified network observability switch disables passive update checks and
automatic desktop update checks at startup and during long-running app sessions.
Explicit update-availability checks remain disabled while this switch (or a
legacy update opt-out) is active. Opening a release page or downloading an asset
is a separate user-initiated action and may still contact GitHub or the OSS
mirror.

## Deletion

Use `opensquilla uninstall` to remove OpenSquilla. By default it removes the
program and keeps user data. To delete local state and configuration, opt in:

```sh
opensquilla uninstall --purge-state
opensquilla uninstall --purge-config
opensquilla uninstall --purge-all
```

The command previews and limits deletion to OpenSquilla-owned paths. Desktop
and Docker installs may require platform-specific removal steps shown by the
uninstall command; desktop data cleanup does not remove the OS app bundle.

## Security And Privacy Reports

Report security or privacy issues through the process documented in
[`SECURITY.md`](SECURITY.md). Please do not include secrets, API keys, private
conversation content, or unrelated personal data in public issues.
