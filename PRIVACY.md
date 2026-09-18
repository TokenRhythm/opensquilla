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

OpenSquilla uses one **Network reporting** control for V1 installation and daily
usage statistics, V2 Reliability diagnostics, and V2 Product and growth analytics,
following the existing opt-out policy.
Reporting is enabled by default; there are no separate statistics choices or
consent popups during onboarding. A notice-version update does not require a
new choice or create a consent timestamp. An explicit decline saved under either
of the former per-scope controls is migrated to the unified control being off.

The control below disables all statistics uploads, passive update checks, and
automatic desktop update checks:

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

`OPENSQUILLA_TELEMETRY_DISABLED=true` remains a hard veto for V1 and V2
statistics. `OPENSQUILLA_UPDATE_CHECK_DISABLED=true` disables update checks and,
for compatibility with V1, installation and daily usage uploads; it does not
disable V2 statistics.

Manual user-initiated actions may still contact network services after user
intent, including release downloads and configured providers, search, channels,
automation, or integrations. Update-availability checks, including
`opensquilla version --check` and the desktop manual check, do not bypass the
unified or legacy opt-out controls.

## Optional Usage Statistics

### Reliability diagnostics

While unified reporting is enabled, OpenSquilla may record the
result, bounded duration, enumerated error code, and other closed attributes
for app startup, Gateway startup, detected crashes, AI turns, tool calls, file
parsing, updates, and session performance. Reliability uses a random
`app_session_id` for operation/session metrics and the device token described
below for device counts; it does not use an account identifier. Crash events contain
only a one-way error fingerprint, component, version, and bounded runtime facts.
Complete exception messages and stacks remain local.

### Product and growth analytics

While unified reporting is enabled, OpenSquilla may record client launches,
actual MetaSkill and Coding Mode executions, and
one-time funnel milestones for acquisition, onboarding completion, first app
readiness, registration, first turn start, and first successful response.
Product activity is recorded at most once per device, local profile, surface, and
UTC day. The server calculates daily active devices and rolling 30-day monthly
active devices by deduplicating the device token across Desktop, Web, TUI, CLI,
and all profiles on the same OS device. It includes only the surface and common
event fields, not activity content; merely running a background Gateway does not
count as product activity.
Client first-use milestones require fresh-install eligibility; enabling
reporting on an existing installation does not backfill those milestones.
Growth uses random, purpose-specific `acquisition_id` and
`analytics_user_id` values. The analytics user ID is not a raw account ID or a
hash of one and is not shared with Reliability. It is retained for legacy
cohort/queue compatibility, not used as a substitute for a device count.
Repeatable usage counts do not
require the installation to qualify as a newly activated user.

Application events in both scopes may include `device_id`: a one-way SHA-256
digest with an OpenSquilla-specific domain, OS type, and OS machine identifier
(macOS IOPlatformUUID, Windows MachineGuid, or Linux machine-id). The raw OS
identifier never leaves the device. No account, MAC address, IP address, or
profile path is used to derive this token. It is resolved only when reporting
is allowed and is stable across profiles and application upgrades. Reinstalling
an OS or cloning a VM can change or duplicate the OS identity; a device here
means an OS installation, not a guaranteed physical person or chassis.
If the OS identifier cannot be read, the field is omitted and the event is
excluded from device counts; random/profile IDs are never substituted.
Legacy events without a device token are not included in device counts.
Feature run counts and reliability success rates still count actual operations.
Gateway-observed activity and runtime features identify the execution host;
remote Web/TUI connections to one Gateway count as that one device, not as
separate browser machines. Native Desktop events identify the Desktop host.
Website acquisition journeys retain their own journey counts because a website
cannot access the application's OS device identifier.

Website, CDN, and account-service milestones must be emitted by those services
at their authoritative transaction boundary. They use independent server-side
signing credentials that are never shipped in browser JavaScript, installers,
or the desktop app. Ordinary installers without a consented, signed acquisition
token do not emit installation events, and the desktop does not infer an
external registration result.

### Collection and upload rules

Both scopes use a strict field whitelist and reject unknown fields. They write
to separate bounded local SQLite queues and upload batches to separate routes:
`/v1/reliability/events` and `/v1/growth/events`. The reporting policy is checked before
local collection and again immediately before network upload. Offline retries
reuse `event_id` for deduplication. Growth events are not sampled.

V2 statistics payloads never include prompts, responses, provider configuration,
agent configuration, tool arguments, task parameters, file names, file paths,
file contents, raw exception messages, complete stacks, usernames, hostnames,
API keys, raw account IDs, order data, IP addresses, MAC addresses, or raw OS
machine identifiers. The only device token is the purpose-specific digest
described above. Source IP addresses may be visible to network servers at the
transport layer, but are not event fields and are never used to join
website and client identities.

CI, test, and `DO_NOT_TRACK` environments fail closed for both streams. Disabling
the unified control stops collection and pauses sending. It does not erase
existing bounded queues, analytics identities, or first-use milestone state;
pending events can resume after reporting is enabled again. Remote and
environment-variable vetoes do not change the saved setting. Local data can be
removed through the deletion options below.

### V1 installation and daily usage statistics

V1 statistics run alongside V2. After the Gateway listener and runtime are ready,
a background worker sends `install` on first use and `version_seen` once per
new version to `/v1/install`. These events contain a pseudonymous installation
identifier, application version, installation method, OS/version, architecture,
Python major/minor version, and first-seen/send timestamps.

Completed top-level interactive turns contribute local UTC daily counters for
conversation turns, input tokens, output tokens, cached tokens, and cache-write
tokens. A background task uploads pending completed days to `/v1/usage` at startup
and retries hourly. The current UTC day is excluded until it ends. Existing
installation state is retained. Daily event IDs use a random identity saved in
each aggregate database, so separate profiles on one machine do not collide.
The identity is kept across restarts, retries, and database moves; it is not
itself uploaded. Retained pending days can resume when reporting is re-enabled.

On upgrade, completed days already marked uploaded remain untouched. Pending
legacy days use the new database-specific keys. Older versions did not record
upload attempts, so a legacy day already accepted by the server whose
acknowledgment was lost may be counted again during this one-time transition.

V1 preserves its installation identity: a local SHA-256 digest derived from
available MAC addresses, then local IP addresses if needed, with a persisted
random fallback. Raw MAC/IP values are not uploaded. This identifier is separate
from V2 identities and is not attached to provider requests; the
`X-OpenSquilla-Install-Id` provider header remains retired. V1 payloads contain
no prompts, responses, file contents, tool arguments, credentials, or account IDs.

The unified opt-out, either previously saved scope decline, the legacy statistics
opt-out, the product-analytics environment veto, and CI/test/`DO_NOT_TRACK`
suppression apply before V1 collection and again before upload. Pausing V1 keeps
existing installation state and pending daily counters. Endpoint overrides remain
available through `OPENSQUILLA_TELEMETRY_ENDPOINT` and
`OPENSQUILLA_USAGE_TELEMETRY_ENDPOINT`.

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
checksums are published in `SHA256SUMS` when release assets are generated. On
Windows, OpenSquilla fetches the canonical `SHA256SUMS` from the matching GitHub
Release, streams the selected installer from the selected source into an
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
