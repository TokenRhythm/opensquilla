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

OpenSquilla exposes separate consent controls for **Reliability diagnostics**
and **Product and growth analytics**. An unset choice, an explicit decline, an
incomplete consent receipt, or a stale notice version is treated as disabled.
The global control below is a hard veto over both telemetry scopes, passive
update checks, and automatic desktop update checks:

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

`OPENSQUILLA_TELEMETRY_DISABLED=true` remains a hard veto for both new
telemetry scopes. It does not grant consent and does not reactivate retired
legacy telemetry. `OPENSQUILLA_UPDATE_CHECK_DISABLED=true` applies only to
update checks.

Manual user-initiated actions may still contact network services after user
intent, including release downloads and configured providers, search, channels,
automation, or integrations. Update-availability checks, including
`opensquilla version --check` and the desktop manual check, do not bypass the
unified or legacy opt-out controls.

## Optional Telemetry

### Reliability diagnostics

When separately consented for the current notice, OpenSquilla may record the
result, bounded duration, enumerated error code, and other closed attributes
for app startup, Gateway startup, detected crashes, AI turns, tool calls, file
parsing, updates, and session performance. Reliability uses a random
`app_session_id`; it does not use an account identifier. Crash events contain
only a one-way error fingerprint, component, version, and bounded runtime facts.
Complete exception messages and stacks remain local.

### Product and growth analytics

When separately consented for the current notice, OpenSquilla may record
one-time funnel milestones for acquisition, onboarding completion, first app
readiness, registration, first turn start, and first successful response.
Growth uses random, purpose-specific `acquisition_id` and
`analytics_user_id` values. The analytics user ID is not a raw account ID or a
hash of one, is not shared with Reliability, and is deleted locally when Growth
consent is withdrawn.

Website, CDN, and account-service milestones must be emitted by those services
at their authoritative transaction boundary. They use independent server-side
signing credentials that are never shipped in browser JavaScript, installers,
or the desktop app. Ordinary installers without a consented, signed acquisition
token do not emit installation events, and the desktop does not infer an
external registration result.

### Collection and upload rules

Both scopes use a strict field whitelist and reject unknown fields. They write
to separate bounded local SQLite queues and upload batches to separate routes:
`/v1/reliability/events` and `/v1/growth/events`. Consent is checked before
local collection and again immediately before network upload. Offline retries
reuse `event_id` for deduplication. Growth events are not sampled.

Telemetry payloads never include prompts, responses, provider configuration,
agent configuration, tool arguments, task parameters, file names, file paths,
file contents, raw exception messages, complete stacks, usernames, hostnames,
API keys, raw account IDs, order data, IP addresses, MAC addresses, or device
fingerprints. Source IP addresses may be visible to network servers at the
transport layer, but are not telemetry fields and are never used to join
website and client identities.

CI, test, and `DO_NOT_TRACK` environments fail closed for both scopes. A remote
or local forced-off state pauses sending without manufacturing or changing a
saved consent decision. Withdrawing a scope's consent deletes that scope's
pending local telemetry; withdrawing Growth consent also deletes its local
analytics identity.

### Retired legacy telemetry

The automatic installation upload at `/v1/install`, the daily token aggregate
at `/v1/usage`, and the `X-OpenSquilla-Install-Id` provider header are retired.
Production code no longer starts those upload loops, records daily usage for
them, derives an installation identifier from MAC or local IP data, or attaches
that identifier to provider requests. Legacy modules and environment-variable
names remain only for source/configuration compatibility and cannot opt a user
into telemetry v2.

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
