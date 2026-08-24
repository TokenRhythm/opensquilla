# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

### Changed

- Fresh and managed TokenRhythm configurations now use DeepSeek V4 Flash 0731
  for C0, DeepSeek V4 Pro 0813 as the direct and C1 default, Kimi K2.7 Code for
  C2, and GLM 5.2 B5 fusion for C3. Existing custom inline tiers are not
  migrated, and the mixed-family preset continues to leave thinking levels
  unset.
### Fixed

- Desktop startup now shows monotonic, milestone-based progress without
  estimating remaining time or changing recovery and Gateway startup behavior.
- Stop now acknowledges immediately for provider and ordinary tool work while
  safely settling in-flight filesystem mutations before publishing a terminal
  turn. Late provider or tool results are discarded, timed-out writes retain
  their timeout outcome, and committed file changes are recorded before the
  next turn can run.

## [0.5.3] - 2026-08-13

### Added

- Durable Goals can continue across turns with explicit progress, pause,
  resume, edit, clear, and Plan-mode deferral controls. Queued follow-ups,
  attachments, project handoffs, and session forks now survive reconnects and
  preserve their intended ordering during long-running work.
- MetaSkills, Cron workspace management, inline `/meta` requests, and richer
  Skills lifecycle diagnostics are available through the shared runtime and
  management surfaces.
- Community Skills from ClawHub and GitHub now support immutable source
  resolution and transactional management through one shared service. Gateway
  RPC and CLI add read-only Doctor diagnostics, agent installs return lifecycle
  diagnostics, and the Web UI displays lifecycle and selection state. The
  compatibility claim is intentionally limited to single-root,
  instruction-first Skills; unsupported execution dialects remain inert and
  explicit. Source package, managed storage, and runtime names are tracked
  separately, dangerous scanner confirmations are bound to the reviewed
  artifact, and GitHub batches are serial, capped at 10 references, and pause
  remaining work when the source reports a rate limit.
- Official TokenRhythm HTTPS requests can include an optional pseudonymous
  installation identifier. It is limited to official service endpoints and
  respects the unified privacy and telemetry opt-outs.
- TokenRhythm model discovery now combines the official published catalog with
  the current credential's declared model entitlements, exposes versioned
  `metadata.published` / `metadata.declared` fields, and reports stale catalog
  state without blocking normal model listing or turns.
- The Sandbox settings module now covers Safe/Full defaults, versioned file,
  command and network policies, bounded recursive-delete backups, pinned
  bundled runtime versions, LAN listening/CIDR controls, and named tokens.
- Safe availability is now decided by cached live process, filesystem worker,
  deny-write, and authority-deny-read canaries instead of setup state alone.

### Fixed

- Long-running chat streams recover more reliably from renderer reconnects and
  provider activity gaps, while bounded history rendering keeps large sessions
  responsive. Assistant answers, tool markers, pending input, session counts,
  and created-chat cards now keep their correct UI boundaries and state.
- Desktop startup, profile recovery, update checks, Windows packaging, provider
  reasoning replay, prompt-cache continuity, compaction, schedules, and usage
  receipts are more reliable across supported platforms.
- TokenRhythm maximum-output limits now prefer the authenticated top-level
  declaration, preserve the published value separately from the runtime-safe
  value, and clamp fallback requests to each physical model's known limit.
- TokenRhythm catalog refreshes now use bounded lazy caching, last-good
  snapshots, authority isolation, credential-safe persistence, and explicit
  refresh/credential lifecycle invalidation without a background timer.

### Changed

- The landing-page game suggestion now submits only its visible localized
  label. The previously hidden, detailed game request is no longer bundled in
  the client or sent when the suggestion is selected.
- The responsive chat header, floating composer, sidebar peer sections, and
  artifact presentation have been refined without changing existing gateway
  configuration or persisted chat data.
- `onboarding.models.discover`, profile model discovery, and `models.list` add
  optional catalog metadata and maximum-output fields. Existing clients may
  ignore them; external decoders using `additionalProperties: false` must allow
  the new additive fields before upgrading.

### Removed

- Removed the in-repository `opensquilla swebench` CLI, optional dependency,
  Python namespace, and bundled skill. Benchmark recipes and evidence now live
  in the external experiment ledger and handoff runner; `opensquilla agent` and
  Coding Mode are unchanged. Existing local benchmark artifacts are left
  untouched and are no longer managed by OpenSquilla.

### Security

- Community Skill ingestion now fails closed on unsafe archive paths and links,
  ambiguous package roots, source drift, malformed manifests, YAML alias/depth
  expansion, bounded artifact limits, and interrupted publication recovery.
- LAN WebSocket peers are limited to loopback, RFC 1918, or IPv6 ULA ranges
  and may be narrowed further with `auth.allowed_client_cidrs`; public peers
  are rejected before authentication.
- Safe tasks pin one policy version for the entire turn. High-risk commands
  require exact user approval, and recursive deletion is brokered through an
  irreversible-action prompt plus a default 3 GiB oldest-first backup vault.

## [0.5.2] - 2026-07-30

### Added

- Versioned same-turn steering now works consistently across the Web UI, CLI,
  Gateway, and shared runtime for supported single-model and router turns.
  Accepted input is idempotent and survives reconnects and eligible provider
  fallback without rerouting the logical turn; older clients and gateways keep
  their legacy or visible queue compatibility paths.

### Fixed

- Desktop and Gateway startup no longer wait on unnecessary recovery scans,
  legacy-recovery maintenance, repeated ownership readiness windows, or
  historical usage backfill.
- Retained session history loads independently from interactive controls, so
  navigation, controls, existing messages, and message sending remain usable
  during hydration and recovery.
- Recovery consolidation preserves historical usage for deduplicated sessions,
  and usage backfill uses smaller transactions with bounded SQLite contention
  handling.
- Windows Desktop validates the Electron profile marker for sandbox setup, and
  the macOS project picker supports folder creation, system-localized text, and
  the intended initial directory.
- Desktop HTML artifacts render when their native preview surface appears, and
  preview mode switches no longer require an extra confirmation or overwrite
  the saved default.
- Custom compatible providers expose their Base URL fields again, the Usage
  currency toggle applies consistently, and provider/usage presentation avoids
  clipped or ambiguous actions.
- Model Ensemble activity timing remains stable across generic channel
  heartbeats, and the Desktop startup loader rotates in the expected direction.

## [0.5.1] - 2026-07-29

### Added

- Durable Plan mode, project workspaces, native macOS project-folder creation,
  desktop deep links, richer artifact workbench previews, and general pasted
  file attachments.
- Qwen Token Plan and custom Anthropic-compatible providers.
- Recoverable Desktop profile import and bounded primary-profile
  consolidation.
- New agent tool `audio_config`: configures the audio (TTS) provider through
  the same validated path as onboarding — atomic UTF-8 persistence with
  backup, live hot-apply, and `restartRequired: false`. Agent-driven setup is
  restricted to the registered ElevenLabs endpoint and credential environment
  variable; advanced endpoint settings remain operator-managed. API keys are
  stored but omitted from tool results and validation errors.

### Fixed

- Local-owner `bypass` again resolves to Full host execution consistently
  across Web, CLI, TaskRuntime, and Cron. Full file, Shell, and Git operations
  no longer initialize a sandbox worker or apply sandbox path checks.
- Cron creation is atomically idempotent for identical tool requests, and
  owner jobs preserve their host execution target.
- Frozen macOS filesystem workers start through the packaged gateway's
  dedicated worker entry point, while frozen Windows gateways resolve sandbox
  setup paths correctly.
- First-install provider and model routing state, long-session recovery,
  reasoning timers, subagent usage accounting, and session deletion lifecycle
  handling are more reliable.
- Settings overlays cover the viewport, Skills navigation remains stable, and
  project/chat activity, onboarding, assistant activity, and provider settings
  are clearer.
- The `gateway` tool no longer advertises capabilities it cannot deliver:
  `restart` and `config_set` now report their actual availability, audio
  settings point to `audio_config`, and `config_get` uses the canonical
  public-config redactor instead of echoing credentials.
- Shell commands that would terminate the gateway's own process
  (`Stop-Process -Id <gateway pid>`, `taskkill /PID`, `kill`, and
  name-targeted variants) are structurally refused in every host mode,
  before any configurable policy layer; other processes stay manageable.
- Config persistence explicitly routes non-UTF-8 (e.g. GBK-corrupted)
  config files into the backup-then-rewrite recovery path, with regression
  coverage for corrupt-file recovery, CJK round-trips, and mid-write
  failures leaving the original bytes untouched.

### Changed

- The packaged Desktop gateway carries its CA trust bundle, channel
  administration approvals are aligned, and coding/delivery guidance now
  reflects actual runtime capability.
- Desktop now consolidates data from legacy recovery profiles into the single
  primary profile before startup. Existing primary settings remain
  authoritative; when the primary profile has no settings, the newest legacy
  recovery settings are adopted automatically. Desktop no longer creates or
  asks users to confirm isolated recovery profiles.
- Closing the Desktop main window now preserves the live Control UI on macOS
  and keeps Windows reachable from a system tray icon. Explicit **Quit
  OpenSquilla** still drains and stops the local Gateway, and the close behavior
  can be changed in Runtime settings.

## [0.5.0] - 2026-07-23

OpenSquilla 0.5.0 is the first stable release of the 0.5 line, collecting the
0.5.0rc1–rc4 preview work below plus the changes since Preview 4.

### Added

- Source checkouts can opt into the full-screen OpenTUI chat with shared
  Gateway sessions, turn/tool/reasoning presentation, Router/Ensemble controls,
  and guarded terminal restoration. Companion hosts remain development-only:
  this change does not add release assets or alter the release installer.

### Changed

- Telegram edited messages no longer start a new agent turn. Previously,
  editing an earlier message re-triggered the bot; edited updates are now
  dropped deliberately so an edit is not double-billed as a new request. In
  private chats the bot replies once per chat explaining that edits are
  ignored and the corrected text should be sent as a new message.
- `opensquilla chat` uses automatic renderer selection without treating stale
  internal backend environment state as a user request. Release installs keep
  the Python-native chat when no compatible host is installed.
- Generated Vue control-console files are no longer tracked in Git. Release
  wheels, Desktop installers, and container images still include the CI-built
  console and require no Node.js at install time. Source installs and wheelhouse
  builds now require Node.js 22.12+ with npm and build the console from the
  locked frontend dependencies before packaging.
- Optional profile migration is now available only from **Settings → Advanced
  → Data maintenance**. Onboarding, the sidebar, Doctor, and native upgrade
  prompts stay silent; gateway startup logs a single advisory line when a
  fresh profile boots beside importable legacy data so headless operators can
  find their previous home. Desktop can still preview and apply a guarded
  whole-profile replacement, while Web administrators receive a path-free,
  read-only preview.

### Fixed

- OpenSquilla profile transfers now leave machine-local `code-task` run
  worktrees in the source and preserve lexically contained relative workspace
  links on POSIX without dereferencing them. Existing-profile backup and
  restore keeps canonical and historical workspace links as no-follow leaves;
  source imports still fail closed on absolute or escaping workspace links and
  Windows reparse points.

## [0.5.0rc4] - 2026-07-13

### Added

- Desktop profile recovery now validates the active workspace before any
  identity, memory, or chat state can be initialized. Ambiguous profiles keep
  their original data and can open a persistent recovery profile.
- Fresh Windows Desktop installations can explicitly copy a retired Portable
  profile during setup. The source remains unchanged, and other CLI or Desktop
  profiles stay behind the deliberate Settings transfer flow.
- Release downloads are mirrored to Alibaba Cloud OSS, including a stable
  browser download page for users who cannot reliably reach GitHub assets.

### Changed

- Normal RC2/RC3 upgrades no longer present a generic data-import decision.
  Complete profile transfers use stable snapshots, full backups, and
  all-or-nothing replacement instead of file-level merging.
- Desktop uninstall preserves profile data by default and distinguishes setup
  reset, current-profile deletion, and deletion of all user data.
- Long-running Desktop sessions can discover later 0.5 previews or the final
  0.5.0 release while retaining the installed release channel.
- Official TokenRhythm and OpenRouter API requests now include OpenSquilla's
  public application-attribution headers; custom compatible hosts remain
  unchanged.

### Fixed

- Provider API-key changes remain effective after restart instead of being
  replaced by an older stored value.
- Model Ensemble progress, stall handling, usage accounting, message limits,
  and budget handoff are more reliable for slow and multi-model turns.
- WeCom heartbeat handling, transport and child-process cleanup, SQLite
  extension loading, and checkpoint recovery fail more predictably without
  discarding valid state.
- The Control UI more reliably preserves chat scroll position and exposes full
  tool results and trustworthy model choices.

## [0.5.0rc3] - 2026-07-10

### Added

- Legacy home migration: `opensquilla migrate opensquilla` imports an
  existing OpenSquilla home — a CLI `~/.opensquilla`, a retired Windows
  portable data directory (enumerated with a chooser across
  `%LOCALAPPDATA%\OpenSquilla\portable\*`), or an explicit `--source` path
  (Docker volumes, relocated or restored homes) — into the current install.
  Dry-run by default with a pinned machine-readable report; apply stages a whole-home
  copy with WAL-safe database handling and commits transactionally, so an
  interrupted import never leaves a partial target. Imported configs drop
  stale absolute path pins, inline provider keys relocate to `.env`, and
  imported scheduler jobs arrive paused. Entry points: the desktop
  onboarding offers detected legacy data as a first step (with provider
  prefill), Settings → Runtime gains an "Import legacy data" rescue action,
  the Web UI setup flow shows a read-only legacy-data advisory
  (`onboarding.status` gains the additive `legacyData` block), gateway boot
  warns once when a fresh home coexists with legacy data, and doctor emits
  a `migration.legacy_home_detected` finding with copyable fix commands.
- Golden legacy-config fixtures: every released line's default config dump
  (0.1 through 0.5, portable and desktop shapes) is pinned as a fixture and
  must load on current code, so old-config upgrades are tested mechanically.
- The Web UI gains an opt-in background-music player. Enabling it (Settings →
  Appearance, or the command palette action) reveals a topbar control that
  loops tracks from a user-supplied library: `public/music/playlist.local.json`
  lists bundled filenames or HTTPS URLs, and a session-only "Choose local
  file…" picker plays ad-hoc audio. Track choice, volume, and play state
  persist per browser; the feature is off by default and no audio files ship
  with the repository (see `opensquilla-webui/public/music/README.md`).
- Attachment resource ceilings: the in-memory staged-upload store now has an
  aggregate RAM cap (`attachments.upload_store_max_total_bytes`, default
  300 MiB) — when reached, new uploads are rejected with the additive HTTP
  507 `UPLOAD_STORE_FULL` (retryable; staged entries expire within the TTL)
  instead of evicting staged uploads — and attachment copies materialized
  into agent workspaces are bounded by a disk budget
  (`attachments.workspace_attachment_disk_budget_bytes`, default 1 GiB) that
  degrades new materializations to an unavailable marker without evicting
  existing files.
- Provider connection tests now report latency: `onboarding.provider.probe`
  returns an additive `latencyMs` field (0 on pre-network failures), the CLI
  `models probe --json` rows carry `latency_ms`, and the Web UI setup panel
  renders a verdict line — connected state with round-trip latency, live
  model count, and sample model ids; failure pills include the latency so a
  fast 401 rejection reads differently from a slow timeout.
- `[models.<provider_id>."<model_id>"].context_window` now governs context
  budgeting as documented: the catalog resolver consults the per-model
  override first (new `override` source), the turn budget ladder, compaction
  window, usage pressure (`windowSource: "model_override"`), and router
  capability facts all honor it, and the per-model value beats the global
  `llm.context_window_tokens`. The Web UI setup panel gains a context-window
  override field with an auto-detected/override/effective readout and a
  small-window warning for local runtimes.
- Doctor now lints API-key shape: a key that looks like a URL or an
  environment-variable name yields a `provider.active.api_key_shape` warning
  (shape only — key material never leaves the gateway) with recovery steps.
  `providers.status` rows carry the additive `apiKeyShape` field.
- The console Overview can copy the full doctor report as sanitized JSON
  (home paths normalized) and hand it to a new agent chat ("Diagnose with
  agent", hidden when the provider itself blocks readiness); findings for
  provider/channel/router surfaces deep-link into the matching settings
  section, and `/settings/provider#provider-<id>` preselects that provider.
- Passive provider latency stats: the agent loop records time-to-first-token
  and call duration per provider call into an in-memory rolling window;
  `providers.status` rows expose an additive `latency` snapshot
  (`p50TtftMs`/`p95TtftMs`/`samples`, gated below minimum sample counts) and
  the Overview shows a latency readout for the active provider.
- The chat view warns when a streaming turn produces no content events for
  90 seconds (server heartbeats alone no longer look like progress): a
  dismissible notice offers keep-waiting or interrupt, and stays quiet while
  a tool is executing or an approval is pending.

- Added Tencent TokenHub providers for the Hunyuan hy3 family:
  `tencent_tokenhub` (OpenAI-compatible mainland endpoint,
  `TENCENT_TOKENHUB_API_KEY`), `tencent_tokenhub_anthropic` (the same
  deployment's Anthropic Messages protocol with `x-api-key` auth), and
  `tencent_tokenhub_intl` (the international deployment with its own
  `TENCENT_TOKENHUB_INTL_API_KEY`). hy3/hy3-preview thinking maps onto the
  documented `reasoning_effort` `low`/`high` values plus the thinking enable
  object, and assistant `reasoning_content` is replayed across turns per the
  hy3 interleaved-thinking contract. The Token Plan subscription is covered
  too: `tencent_token_plan` (Chat Completions at
  `api.lkeap.cloud.tencent.com/plan/v3`) and `tencent_token_plan_anthropic`
  (Anthropic Messages at `/plan/anthropic`, bearer auth), both reading the
  dedicated `TENCENT_TOKEN_PLAN_API_KEY` (`sk-tp-…`) plan credential.
- Attachments now accept **any file type** on every surface (Web UI, desktop,
  CLI `/file`, channels, RPC). Rendered families (images, PDF, text, Office,
  email) keep their extraction and anti-forgery behavior; everything else is
  admitted as an *opaque* attachment staged into the agent workspace — the
  bytes are never parsed, decompressed, or inlined into a provider prompt, and
  the model receives an escaped metadata envelope plus the workspace path.
  New config: `attachments.accept_opaque` (default `true`; `false` restores
  the legacy fail-closed admission gate) and `attachments.opaque_max_bytes`
  (default 30 MiB).
- Text attachments above the 2 MB inline threshold now stage through the
  upload endpoint up to 30 MiB when the whole payload is proven UTF-8, so
  large LaTeX sources and logs no longer dead-end at "file too large".
- Channel file downloads (Telegram, Discord, Feishu, Matrix) now use staged
  per-category ceilings, so archives, voice notes, and videos up to 30 MiB
  ingest instead of being silently skipped at a flat 5 MiB unknown-type cap.

- Added Alibaba Cloud IQS (`iqs`) as a runtime-supported web search provider:
  unified-search endpoint with freshness, site include/exclude filters, inline
  main-text content, and rerank scores, configured via `IQS_SEARCH_API_KEY`.
- Added a runtime development branch sync: request-proof budgeting with
  deterministic compaction, DashScope provider profile with prompt-cache
  markers and thinking-mode plumbing, an optional LLM trace recorder,
  tool-result store compression, sandbox-descriptor integration for
  filesystem tools, and a family of default-off, env-lever-controlled
  runtime recovery modules.
- Prebuilt multi-arch (`linux/amd64` + `linux/arm64`) container images are
  published to GHCR (`ghcr.io/opensquilla/opensquilla`) on release tags,
  with a manual dispatch mode that validates the build without publishing.
- `docs/docker.md`: a container deployment guide for home servers and NAS
  (Debian 12 walkthrough, prebuilt images, LAN exposure with token auth,
  bind-mount ownership, upgrades and rollback).

### Changed

- The per-turn `[Current user request reminder]` message appended after
  tool-result turns is now off by default. Re-presenting the objective as a
  fresh user message every turn re-triggered model reasoning on nearly every
  request (observed 96–99% of requests emitting reasoning tokens, vs 14–31%
  without it) and broke the provider prompt-cache suffix each turn, which in
  internal evaluation cut agent throughput roughly 2.5× on long tool-loop
  tasks without improving outcomes. Set
  `OPENSQUILLA_TURN_OBJECTIVE_REMINDER=on` to restore the previous behavior
  byte-identically, or `trim:<chars>` for a shortened variant.
- An explicitly configured `llm.base_url` now wins over the provider's
  derived environment variable (`OPENAI_BASE_URL`, `OPENROUTER_BASE_URL`, …),
  mirroring the existing api_key rule. Previously the env var silently
  overrode a custom endpoint saved in the Web UI or via `config.set` on
  every boot/reload, reverting it to the env value (#484). Endpoints that
  were never explicitly chosen — a config without `base_url`, or one holding
  the provider's default URL — still follow the env var, so fleet-wide
  `*_BASE_URL` overrides keep working, and `OPENSQUILLA_LLM_BASE_URL`
  (the settings layer) still fills an unset `base_url` and then counts as
  explicit. As a side effect, a minimal config such as
  `provider = "openai"` without `base_url` now resolves to that provider's
  own default endpoint instead of leaking the OpenRouter URL from the model
  field default.
- **Security:** the gateway no longer emits CORS headers by default —
  `cors.allowed_origins` now defaults to `[]` instead of `"*"`. The Web UI
  (served same-origin from the gateway), the CLI, curl, and the desktop app
  are unaffected. Deployments that serve a separate frontend from another
  origin must list that origin explicitly in `cors.allowed_origins` to
  restore the previous behavior; configuring `"*"` together with
  `cors.allow_credentials` now logs a boot-time warning.
- **Security:** state-changing gateway HTTP routes (chat send, system
  shutdown, approvals settings/resolve, elevated mode, channel logout, file
  upload, audio transcription, artifact native open, diagnostics bundle) now
  reject browser requests whose `Origin` is not the gateway itself with
  `403 FORBIDDEN_ORIGIN`, extending the diagnostics-bundle same-origin guard
  to the whole HTTP surface. Requests without an `Origin` header (curl, the
  desktop client) and origins listed in `cors.allowed_origins` still pass.
- `compose.yaml` now documents prebuilt-image selection, safe LAN exposure,
  and Web UI token auth, and the troubleshooting guide covers common Docker
  deployment failures.
- The Web UI composer's file picker no longer sets an `accept=` filter, so
  native file dialogs (notably on Windows) show all files instead of hiding
  types like `.tex` (#472). The attach-button tooltip in all six locales now
  describes the any-type policy.
- Under the default `attachments.accept_opaque = true`, the upload endpoint
  no longer returns HTTP 415 `UNSUPPORTED_MEDIA_TYPE` for type reasons and
  `sessions.send` no longer raises `unsupported_mime` for unrendered types;
  the codes and message formats are unchanged for strict deployments that
  disable the flag. Clients that string-matched the "must be one of [...]"
  detail should rely on the typed codes instead.
- Transcripts written by this version may contain opaque attachment
  envelopes; older builds replay such history with the attachment silently
  omitted (current builds emit an omission marker).
- Common non-canonical MIME spellings (`image/jpg`,
  `application/x-zip-compressed`, `application/x-gzip`) now normalize to
  their canonical types in every mode, so an `image/jpg` upload is accepted
  as JPEG even under `accept_opaque = false` where it previously drew a 415.

- Provider retry handling: responses that stop at the length limit without
  visible text or tool calls now enter the reasoning-only retry path instead
  of the length-capped continuation path, and a thinking-related provider
  stream error now disables thinking for the next call only (one-shot)
  instead of the rest of the turn.
- Context compaction: `read_file` and `git_diff` results are preserved
  verbatim (exempt from semantic projection and aggregate compaction), tool
  results already shown in full are no longer retroactively compacted under
  context pressure, and compaction placeholders gained `preview_complete`
  plus retrieval hints.
- Tool dispatch: a preflight validation pipeline now rejects malformed tool
  calls before execution and reports invalid tool arguments as retryable;
  `write_file` refuses destructive overwrites that would shrink an existing
  large workspace file by more than half; `grep_search` output gained a
  header, offset paging, VCS-directory exclusion, and binary-file skipping;
  `edit_file` accepts single-edit shorthand and recovers from near-miss
  matches by default; `apply_patch` accepts `@@` hunks with optional counts.
- The `coding` tool profile now enforces fresh workspace reads before edits.
- `match_workspace_write_deny` deny patterns now apply only to
  workspace-contained paths (previously they could match paths outside the
  workspace).
- Request-proof compaction marks compacted tool arguments with inline
  `[provider_request_tool_input_compacted: ...]` markers instead of a JSON
  envelope.

### Fixed

- The desktop gateway now receives the OpenSquilla home root (not the state
  subdirectory) in `OPENSQUILLA_STATE_DIR`; a one-time relocation moves the
  previously nested skills, workspace, session archive, router data, and
  `.env` up to their intended locations without touching the databases.
- Legacy configs no longer hard-fail strict validation: stale
  `memory.dream.model_override` keys are stripped, channel entries with
  unregistered types are parked with a warning instead of rejecting the
  file, a `squilla_router.tier_profile` that no longer matches
  `llm.provider` is cleared, and an unwritable config location degrades the
  post-migration rewrite to a warning instead of failing boot.
- The desktop shell resolves the UI language from the OS preference list
  correctly: English tags now match in place instead of falling through (a
  Hong Kong list like `en-HK, zh-Hans-HK, …, fr-HK` previously landed on
  French), and an explicit `Hans` script subtag wins over a
  Traditional-default region, so `zh-Hans-HK`/`zh-Hans-TW` readers get
  Simplified Chinese instead of the English fallback. The resolver is
  extracted to `desktop/electron/src/desktop-locale.ts` with a regression
  suite (`npm run test:desktop-locale`).
- Dream (memory consolidation) now resolves its provider credentials through
  the shared explicit-config-first resolver: previously `OPENROUTER_API_KEY` /
  `OPENROUTER_BASE_URL` in the gateway environment unconditionally overrode
  the configured key and endpoint — even when the configured provider was not
  OpenRouter — silently redirecting Dream turns away from the operator's
  endpoint.
- Fixed managed-network sandbox domain grants missing the Bocha, Tavily, and
  Exa search API hosts: `web_search` runs with those providers active were
  blocked under the managed-network sandbox. All runtime search providers now
  have system domain grants and default search-allowlist entries, enforced by
  a contract test.
- Byte-level text sniffing no longer misclassifies clean UTF-8 payloads as
  binary when a multibyte character straddles the sniff peek window (affected
  CJK plain-text uploads with unrendered MIME claims).

- Fixed secret redaction missing assignment values that start with a quote
  (for example `password: "..."`); quoted values are now masked in memory
  persistence and trace capture paths.

## [0.5.0rc2] - 2026-07-06

### Added

- Added clearer provider/router setup surfaces, config provenance RPC coverage,
  preset registry behavior, custom provider substrate, and cross-provider router
  settings for the 0.5 preview line.

### Changed

- Kept fresh installs on the direct single-model `squilla_router` path while
  making front-end-enabled ensemble mode default to `static_openrouter_b5`.
- Refined desktop onboarding, session empty states, ensemble progress display,
  and packaged Web UI assets for the Preview 2 release surface.

### Fixed

- Fixed main CI contract drift around default router mode, migration/provider
  persistence, static-B5 doctor checks, and onboarding status wire fields.
- Fixed code-task scaffold prompts so build-mode scaffolding stays
  non-interactive.
- Fixed expired staged Web UI uploads by refreshing file UUIDs before send when
  the original file remains available.
- Fixed local HTML artifact opening, desktop reopen behavior, cancelled-turn
  rollback, sandbox denial resume recovery, composer draft persistence, session
  cleanup, attachment isolation, and several provider/router recovery paths.

## [0.5.0rc1] - 2026-07-04

### Added

- Added dynamic Model Ensemble routing, OpenAI/Codex-oriented provider support,
  direct single-model routing defaults, and progressive reveal behavior so
  preview users can test the new routing line before the next stable release.
- Added managed execution host-routing paths and sandbox/approval alignment for
  safer terminal, desktop, and host-execution workflows.
- Added Control UI and desktop affordances for router/provider settings,
  drag-and-drop attachments, history materialization, and image preview
  navigation.
- Added OpenTUI preview improvements for terminal and gateway workflows.

### Changed

- Preview release assets now publish Electron desktop installers, updater
  metadata, a versioned Python wheel, and `SHA256SUMS`; new 0.5 preview releases
  no longer publish Windows portable zips or portable latest aliases.
- Sandbox run modes, approval boundaries, and managed host execution now share
  clearer authorization and diagnostics across Windows, Linux, and desktop
  sessions.
- Desktop update, privacy, code-signing, and release documentation now describe
  the preview asset set and portable retirement path.

### Fixed

- Improved Windows subprocess encoding, process cleanup, gateway lifecycle
  diagnostics, router timeout handling, and packaged desktop runtime checks.
- Fixed desktop/Web UI recovery cases around settings restore, refreshed
  sessions, image preview movement, attachment handling, and provider/router
  visibility.

## [0.4.1] - 2026-06-30

### Added

- `opensquilla uninstall` removes OpenSquilla across install methods (uv-tool,
  pip, pipx, and source; portable removes its venv; Docker and desktop print
  guided removal steps). It keeps your data by default — pass `--purge-state`,
  `--purge-config`, or `--purge-all` to delete it (a total wipe requires a typed
  confirmation phrase on every surface), and `--dry-run` / `--json` preview the
  exact remove/keep/manual plan without touching anything. Deletion is contained
  to the OpenSquilla home; a relocated or shared root is refused. The desktop
  Settings → Runtime panel gains a matching "Danger zone".
- The Control UI and desktop client now ship first-class localization for
  English, Simplified Chinese, Japanese, French, German, and Spanish, including
  first-paint desktop boot text, settings surfaces, and persisted language
  selection.

### Changed

- Stopping, restarting, or uninstalling the gateway now drains in-flight agent
  turns and background completions before exit instead of cutting them off
  mid-write. The force-kill deadline exceeds the drain budget (tunable via
  `OPENSQUILLA_GATEWAY_GRACEFUL_TIMEOUT`); on Windows, an owner-only
  `POST /api/system/shutdown` endpoint provides the same graceful stop where
  POSIX signals are unavailable.
- OpenSquilla now treats `main` as the active development and release
  integration branch, with release guidance and pull-request targeting language
  aligned around `main`.
- Desktop packaging now fails fast when SquillaRouter assets are missing or
  still Git LFS pointer files, and the packaged gateway smoke tests exercise
  coding mode, `code-task`, and the router runtime before release assets are
  uploaded.

### Fixed

- Install telemetry now skips CI and test environments before creating local
  telemetry state or uploading install events, so GitHub Actions and pytest
  runs do not count as user installs.
- The Windows Electron app, installer, and uninstaller now use the OpenSquilla
  icon.
- Web UI fixes keep streaming turns, stale task events, session deletion,
  settings restore, topbar connection state, status line breaks, and desktop
  HTML artifact opening stable across refreshed sessions.
- Provider and router fixes improve Volcengine and BytePlus profiles, macOS
  router runtime diagnostics, and the target labels shown for model requests.

## [0.4.0] - 2026-06-27

### Added

- The refreshed Control UI is now the default browser console, with a
  conversation-first sidebar, Settings modal, Sessions ledger, artifact
  previews, share export, deliverables drawer, turn trace, mobile tabs, and
  clearer Skills, Usage, Cron, Logs, and Approvals surfaces.
- Signed desktop release assets are now available for the Vue/Electron desktop
  shell: a notarized macOS Apple Silicon DMG/ZIP and a Windows x64 NSIS
  installer.
- Coding mode and the `code-task` workflow provide a guarded path for code
  changes: code work runs through an isolated run directory, uses trusted-host
  confirmation, and verifies before persisting changes back to the source.
- `opensquilla swebench` adds an optional SWE-bench evaluation surface for
  users who install `opensquilla[swebench]` and have Docker available.
- OpenTUI preview backend documentation now covers explicit opt-in usage,
  dependency setup, replay benchmarks, and real-terminal harness evidence.
- Web search now supports DuckDuckGo, Bocha, Brave, Tavily, and Exa through the
  runtime provider catalog, with source-backed `web_search` and lightweight
  `web_discover` roles documented for agent workflows.
- `openai_responses` exposes OpenAI's native Responses API shape alongside
  `openai`, sharing `OPENAI_API_KEY` and the default OpenAI base URL while
  making Responses-specific behavior selectable by provider id.

### Changed

- MetaSkills are now **manual-only by default**. They no longer auto-trigger from
  message keywords or appear in the runtime prompt; run them explicitly with the
  new `/meta` command (`/meta` lists available MetaSkills, `/meta <name>` runs
  one). To restore the previous automatic behavior, set
  `meta_skill.auto_trigger = true`. Full list+run is available in web chat and
  the CLI gateway TUI; channel and standalone CLI surfaces support `/meta`
  listing only.
- Terminal chat documentation now distinguishes the stable Python-native default
  backend from the opt-in OpenTUI preview backend.
- Search configuration now treats `search_provider` as the credential anchor for
  a configured key rather than a hard routing promise for automatic searches.
- The old Windows portable zip remains published as a legacy compatibility
  build, while new Windows desktop users should prefer the Electron installer.

### Fixed

- Provider stream parsing accepts no-space SSE data lines, and Gemini thought
  signatures are preserved across tool turns so provider continuity metadata can
  be replayed safely.
- The SSRF guard now gives operators actionable fake-IP DNS guidance while
  keeping private, loopback, link-local, and internal ranges blocked by default.
- Runtime, gateway, and Web UI fixes improve session recovery, attachment and
  artifact handling, approval event delivery, share-image export, router
  visibility, and cross-platform test stability.

### Acknowledgements

- Thanks to the 0.4.0 contributors recorded in
  [`CONTRIBUTORS.md`](CONTRIBUTORS.md), including PR work from @ab2ence,
  @myz-ah, @nice-code-la, @openvictory, @weiconghe, @changquanyou, @nkgotcode,
  @C1-BA-B1-F3, @BlueOcean223, @szdtzpj, @lose4578, and cwan0785
  (GitHub @Anonymous-4427).

## [0.3.1] - 2026-06-03

### Added

- Slack Socket Mode support now covers app mentions, self-targeting replies,
  channel metadata, and threaded response routing across onboarding and channel
  runtime paths.
- Short-drama and video helper workflows remain available in the bundled
  MetaSkill catalog, with stronger Windows-safe script handling and clearer
  review pauses for generated media flows.
- CI impact-surface gates classify docs, runtime, dependency, release, and test
  changes so pull requests can run the right checks without forcing the full
  matrix for every documentation-only edit.

### Changed

- WebChat and the Skills view now make MetaSkill readiness, active runs, and
  install visibility easier to inspect while workflows are being reviewed.
- Release install documentation and installer defaults now point to the 0.3.1
  wheel and Windows portable asset names.

### Fixed

- User chat bubbles preserve multiline text and read like authored messages
  instead of collapsing or visually blending with generated output.
- Slack onboarding and runtime paths now reject incomplete Socket Mode setup,
  preserve existing secrets, enforce webhook signing secrets where needed, and
  keep threaded reply channel context.
- Voice/audio workflows and clarification pauses are represented on the main
  release line, so release users get the same usable handoff and resume
  behavior already validated on integration branches.
- Provider request hardening keeps malformed tool-call history from reaching
  providers as invalid request state.

### Acknowledgements

- Thanks @openvictory for #123, #133, and #137, which helped bring visible
  running-state feedback plus short-drama and media helper workflows into the
  0.3.1 release line.
- Thanks @freeaccount-create for #142, which helped bring Slack Socket Mode and
  self-targeting replies into the channel workflow.
- Thanks @ruhook for #124, and thanks @qq712696307 for the authored commit in
  that pull request, which preserved user message newlines in WebChat.
- Thanks @Cola-Alex for #143, which increased tokenjuice summarize and
  failure-context windows for fallback tool-result projection.
- Thanks @nice-code-la for #165 and #166, which helped make voice workflows
  usable end to end and clarification pauses resume cleanly.

## [0.3.0] - 2026-05-31

### Added

- MetaSkills are now first-class workflow capabilities: bundled stable
  MetaSkills, composition parsing, step scheduling, pause/resume user-input
  flows, proposal gates, runtime history, and authoring documentation let
  repeatable multi-step work become reusable agent routines.
- `opensquilla doctor` and the WebUI Health view now provide actionable
  readiness diagnostics across provider, gateway, memory, logs, search, image
  generation, router, channels, sandbox, and embedding surfaces.
- Tokenjuice-backed tool-result projection now compacts large logs, diffs,
  JSON, test output, package-manager output, and other known tool shapes before
  they crowd out provider context.
- A task-oriented documentation set now covers quickstart, configuration,
  WebUI, CLI, tools and sandboxing, sessions, providers, usage and cost,
  memory, compaction, MetaSkills, tool compression, scheduling, channels, MCP,
  troubleshooting, and contribution guidance.

### Changed

- Tool-output context management now separates durable runtime results from
  provider-visible compact previews, records projection telemetry, and uses
  provider request proof/compaction before oversized payloads reach an LLM.
- WebChat, CLI chat, and terminal TUI internals now share more runtime-backed
  turn, stream, slash-command, artifact, attachment, and recovery behavior.
- Long-session memory and compaction flows now preserve raw archive evidence,
  checkpoint receipts, repair queues, and WebUI-safe compaction status instead
  of treating semantic memory quality and context safety as the same signal.
- Channel install extras now expose only real optional packages; Feishu,
  Telegram, DingTalk, WeCom, and QQ are included in the base install instead
  of being accepted as no-op extras.

### Fixed

- WebChat reliability fixes cover router replay, session restore gaps,
  duplicate compaction status, attachment and pasted-text rendering, artifact
  downloads, composer layout, model-router animation timing, and visible
  recovery during long turns.
- Provider and runtime hardening reduces malformed tool-call fallout, preserves
  configured model-switch intent, handles provider tool-choice requirements,
  and keeps oversized current-turn tool payloads from surfacing as bare
  internal failures.
- Cross-platform CI and Windows portability fixes stabilize CLI help rendering,
  sqlite fallback behavior, UTF-8 subprocess handling, Windows-only test
  fixtures, onboarding commands, and release-surface checks.

## [0.2.1] - 2026-05-21

### Changed

- WebUI diagnostics, transcript replay, and artifact presentation now retain
  more turn-usage evidence while keeping generated-file markers out of normal
  chat output.
- Long-running agent turns now expose softer recovery paths for exhausted tool
  budgets, repeated tool failures, large file-write attempts, and artifact
  delivery handoffs when the final model response degrades.
- Release metadata, installer defaults, and documented wheel URLs now point to
  the 0.2.1 release line.

### Fixed

- Windows portable startup now includes a stronger Visual C++ runtime bootstrap
  path for the bundled ONNX router.
- Memory semantic recall now normalizes stored and query embeddings before
  sqlite-vec search, and high-confidence lexical matches are preserved even
  when vector scoring is weak.
- Generated artifact placeholder text is removed from WebChat history and
  channel-facing output after files have already been delivered.
- Tool dispatch and result budgeting reduce bare internal budget failures by
  returning model-visible recovery context when a turn can still continue.

### Acknowledgements

- Thanks @nice-code-la for the portable Windows VC++ runtime bootstrap work in
  #52.

## [0.2.0] - 2026-05-20

### Added

- `opensquilla migrate` imports existing OpenClaw/Hermes homes into OpenSquilla
  with dry-run previews, explicit `--apply`, source auto-detection, migration
  reports, memory/persona conflict handling, skill compatibility reporting, and
  MCP/channel config mapping.
- `opensquilla chat` is now an early usable interactive CLI chat surface with a
  persistent terminal UI, streaming output, queued input, slash-mode discovery,
  prompt/status chrome, tool-call feedback, inline approval handling, and
  deterministic live prompt output.
- Cron automation now spans CLI, WebUI, RPC, channel, and webhook surfaces:
  structured schedule creation, timezone-aware cron/every/at schedules,
  exact/jitter controls, manual runs, channel delivery, webhook delivery, and
  failure destinations.
- Feishu and Discord channel support now includes capability manifests, safer
  DM/group policy metadata, native file/artifact paths, attachment ingestion,
  Feishu websocket/webhook handling, Discord thread/group handling, and clearer
  channel health/status reporting.
- OpenSquilla can run as an inbound MCP server bridge for session workflows:
  clients can list sessions, resolve/read conversation history, send messages,
  and wait for session events through the gateway.
- Generated artifact delivery is more complete across WebUI and channels,
  including traceable generated-file delivery, recovered fallback delivery,
  channel-safe artifact text, and Unicode-safe PDF report rendering.
- Memory surfaces now separate curated memory from raw transcript search, recall
  prior-session evidence, keep manual Dream runs on configured memory
  workspaces, and let compaction continue when memory flush degrades.
- Release/install support now includes versioned release URLs,
  latest-download aliases, reproducible wheel/portable release guidance,
  source install scripts, Windows portable hardening, ONNX/router recovery
  messaging, and Docker/compose alignment on the gateway port.

### Changed

- Release installation docs now use 0.2.0 release asset URLs and
  `/releases/latest/download/` aliases for the current wheel and Windows
  portable zip.
- Cron tool calls now require structured schedule input (`{kind, ...}`) instead
  of backend natural-language schedule parsing. CLI cron flags still accept
  standard user-facing forms such as `--cron`, `--every`, `--at`, and
  `--expression` through RPC compatibility paths.
- Tool dispatch now runs through a policy pipeline with side-effect-aware
  concurrency: safe/read-only tools can batch, while mutating or
  side-effecting tools stay serialized, keyed, or capped.
- Long-running agent turns now use staged `TurnRunner` execution, provider
  request-budget compaction, prompt-cache anchor preservation, bounded
  tool-result storage, approval-aware retry handling, and recovery paths for
  malformed or non-executable tool calls.
- Channel adapters now declare normalized capability and error-taxonomy
  metadata so unsupported, degraded, retryable, and fatal channel behavior is
  surfaced more consistently.
- WebUI chat, sessions, usage, setup, cron, and search surfaces now share more
  runtime-backed state for recency ordering, per-turn token metrics, provider
  badges, setup form behavior, and session cancellation/readback.
- The default gateway/release documentation now centers on port `18791`, and
  release download paths use the current 0.2.0 assets.

### Fixed

- Failed or aborted turns are kept out of later provider context, reducing
  cascading failures after a bad turn.
- Approval-gated tool retries wait for operator decisions instead of exposing
  pending approval state as ordinary model-visible tool output.
- Provider-context tool markers are protected from becoming executable tool
  state.
- High-volume tool and chat turns recover more reliably from request-tail bloat,
  current-turn tool overflow, and provider payload budget pressure.
- Channel replies avoid leaking provider compaction markers, and channel cron
  delivery now reports failures explicitly.
- WebUI reliability fixes cover recency ordering, table boundaries, mobile and
  composer layout, duplicate visible toasts, setup form resilience, search
  provider badges, and session cancellation counters.
- Generated files that were omitted from normal delivery can be recovered
  through the artifact fallback path.
- Feishu-delivered PDF reports now render Unicode text instead of black or
  placeholder glyph blocks.

## [0.1.0rc1] - 2026-05-12

### Added

- OOTB startup path: `compose.yaml` + `start.sh` + `start.ps1` + Quickstart section in README.
- Legacy `memory.*` config fields: 16 deprecated keys silently dropped with a single aggregated `DeprecationWarning`.
- Agent CLI no-key error: three-section actionable panel (Symptom / Cause / Next steps).
- Tool concurrency: same-turn safe `tool_calls` dispatched concurrently via `asyncio.gather` (22 safe tools enrolled in `_SAFE_TOOL_NAMES`; mutex tools remain serial).
- PID file lock to prevent two gateway instances from sharing the same state directory.
- Core observability counters: `opensquilla_queue_depth`, `in_flight_turns_total`, `turn_cancellations_total`, `queue_full_errors_total`.
- CI matrix on `ubuntu-latest` and `windows-latest` × Python 3.11/3.12, including a metric-name drift check and a tracemalloc leak smoke step.
- Per-channel-adapter in-flight reply cap (`_ChannelInFlightSet`) so a single channel cannot exhaust the global concurrency budget.
- Cross-session fair queueing: sessions sharing an `agent_id` round-robin available slots by completion count.
- Session epoch counter so events from a pre-reset turn are discarded by the frontend after `session.reset`.
- Atomic write helper for transcript attachments (`_atomic_write_bytes`): tmp + fsync + `os.replace`.
- Concurrency env overrides — `OPENSQUILLA_TASK_MAX_CONCURRENCY` and `OPENSQUILLA_CHANNEL_INFLIGHT_CAP` — with invalid-value fallback and warning logs.

### Changed

- Internal SquillaRouter package moved from `opensquilla.contrib.squilla_router`
  to `opensquilla.squilla_router`; bundled model assets now live under
  `src/opensquilla/squilla_router/models/`.
- `TurnRunner` and `TaskRuntime` share a single per-session `asyncio.Lock` (injected via `session_lock_provider`), removing the two-layer lock dictionary and the reverse-acquire risk it created.

### Fixed

- Channel adapter ghost-turn bug: a `TaskQueueFullError` no longer leaves a dangling user message in the transcript.
- `TaskRuntime` terminal-state dictionary leak across `_tasks`, `_session_locks`, and `_pending_by_session`.
