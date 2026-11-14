# [Issue Draft] RFC: first-class desktop companion integration (voice reminders, directed session dispatch, multi-provider ASR)

<!-- Target repo: opensquilla/opensquilla · label suggestion: RFC / discussion.
     Self-contained: all evidence inline, no local-only references required. -->

## Summary

Ship three voice capabilities that today exist only as an **external bridge process** maintained in the companion desktop-pet project (AgentRMC), as opt-in first-class OpenSquilla features:

1. **ASR provider abstraction** for the existing `/api/audio/transcribe` path (today hard-bound to ElevenLabs) — add OpenAI-compatible cloud protocols now, an optional local SenseVoice engine later.
2. **Desktop companion notifier** — a gateway-internal optional service that turns turn-complete / approval-required / plan-presentation events into a spoken + bubble notification on the user's desktop pet, with offline spooling and do-not-disturb.
3. **Directed session dispatch** — the reverse channel: a message composed/uttered in the pet is polled from the companion, guarded against session existence, injected idempotently via the existing `sessions.send` path, and acked back.

All three are **off by default** (`[pet] enabled = false`, provider config unchanged for existing users), loopback-only, and testable offline against a fake companion server.

## Why now: the external bridge's failure modes are process-boundary artifacts

Current production integration is `opensquilla-pet-bridge.mjs` in the AgentRMC repository: a zero-dependency Node script that connects back to the gateway over `ws://127.0.0.1:18791/ws`, subscribes to session events, and POSTs to the companion at `127.0.0.1:45915` (`/notify`, `/speak`, `/dispatch/pending`, `/dispatch/ack`).

It works, but the whole distributed-lifecycle layer exists **only because the logic lives outside the gateway**:

- Processes derived from tool calls die silently within 15–30s → had to be moved to a startup VBS script plus a Task Scheduler watchdog polling a heartbeat file (real incident 2026-11-02: "voice reminders stopped entirely"; the watchdog script itself had a syntax bug and had **never** successfully restarted anything).
- Loopback WebSocket connections go half-open after sleep/network changes → had to invent a `sessions.list` probe with 3-strike forced reconnects and handshake deadlines.
- Reminder spooling, per-session dedup (15s windows), event-identity keys (`approval_id`/`turn_id`/`stream_seq`), paginated `sessions.list`, idempotent client-message ids for `sessions.send` — all correct-but-fragile re-implementations of in-process guarantees, over a documented-but-not-contract-tested HTTP seam.

In-process, these failure classes disappear: gateway restarts restart the service, event subscription is direct, session resolution is the same code path RPC uses.

## Proposal

### A. ASR providers (`[audio.stt]`)

Extract a small `SttProvider` interface behind the existing gateway route; keep ElevenLabs behavior byte-compatible. Add:

- `openai_transcriptions` — multipart `POST {base}/audio/transcriptions` (OpenAI Whisper, SiliconFlow, Groq, …)
- `openai_chat_input_audio` — `{base}/chat/completions` with the audio as an `input_audio` content part (e.g. MiMo ASR; shares one platform key with chat/TTS)
- (later PR) `local_sensevoice` — offline zh/en/ja/ko/yue via `sherpa-onnx`, isolated in an optional `[voice]` extra; weights via user-supplied directory with checksum validation, possibly aligned with the `runtime_packs` app-owned catalog pattern. Credentials continue through `endpoint_identity` / `metered_http` accounting.

### B. Companion notifier (`[pet]`, optional gateway service)

Registered exactly like `memory`/`channels` are today — one entry in `boot.py`'s `optional_owners` (`OptionalServiceOwner` gives generation fencing, config-revision, `disabled/degraded/ready` descriptors):

- subscribe to session events in-process (`session_streams` / EventBridge), map `done|task.succeeded → stop`, `approval_required|plan_presentation → permission`, `turn_committed → silent archive signal`
- POST `/notify` with `session_id` / `project` / `event_id` / `speech` (short spoken phrase kept separate from the detailed bubble text — long tool paths are audio noise)
- 15s dedup per (session, event identity); optional reply preview
- offline → append to the companion's spool file (same path/format as the existing bridge so migration is seamless); DND via sentinel file; `/state` probe surfaced in `opensquilla doctor` and gateway health
- explicit entry points: built-in `pet_speak`/`pet_notify` tools (policy-gated) so "say X now" / cron `agent_turn` reminders work without PowerShell scripts

### C. Directed dispatch (same service, one poller)

`GET /dispatch/pending?exe=OpenSquilla.exe` (default 1.5s, pauses with backoff when companion is down) → guard: target session must resolve (never blind-deliver) → inject via the same durable-admission `sessions.send` path with `client_message_id = agentrmc-<dispatch_id>` (retry-safe) → `POST /dispatch/ack {dispatch_id, ok, error}`. Per-session serial, cross-session concurrent. Only consumes `OpenSquilla.exe` items — other agents' flows are explicitly out of scope.

### Scope boundaries

- **Zero changes** to the companion app and to its loopback HTTP contract (`/state`, `/speak`, `/notify`, `/dispatch/*`, 64KB body cap, 204-on-success).
- No changes to v4 gateway contract event names. No other host integrations, no TTS on the OpenSquilla side, no wake-word / always-on mic listening.
- Companion is Windows-only → service auto-disables on other platforms (doctor says why). ASR is cross-platform.
- Default tests stay offline/deterministic: contract tests run against an in-process fake companion on an ephemeral loopback port.
- Migration guide included: enabling `[pet]` ⇒ uninstall the old startup VBS / watchdog (the two must not run simultaneously).

## Proposed PR breakdown

| PR | Contents | Depends on |
|---|---|---|
| 1 | `audio.stt` provider abstraction + two OpenAI-compatible providers + route registry + unit tests | — |
| 2 | `[pet]` companion notifier service (events/dedup/spool/health/tools/CLI) + fake-companion contract tests | — |
| 3 | Directed dispatch (poller, guard, idempotent inject, ack) | 2 |
| 4 | Optional `local_sensevoice` extra + TUI push-to-talk + docs/features pages + config example | 1 |

PR 1 and 2 are independent. Each carries its own docs fragment and changelog line.

## Questions for maintainers

1. Is a desktop-companion optional service acceptable **in core** (mirroring the `OptionalServiceOwner` pattern), or would you prefer it gated behind some future plugin surface? The modules are designed self-contained (single registration point, narrow injected ports, one config section) so they could move behind a plugin registry unchanged when one lands.
2. Config naming: `[pet]` vs. something more generic like `[companion]`? Bridge URL is a plain loopback HTTP endpoint, so the protocol itself is platform-agnostic even though today's companion is Windows.
3. Model distribution for the local ASR: user-supplied directory + checksum (like AgentRMC's model import), or would a `runtime_packs`-style app-owned catalog be the sanctioned path?
4. TUI push-to-talk: any concerns about a recording hotkey interacting with the real-terminal harness (`docs/tui-real-terminal-harness.md`)?

## Prior art / evidence

- External bridge (event mapping, dedup windows, event-identity keys, spool, dispatch guards, idempotent cmid): `opensquilla-pet-bridge.mjs` + README in `Agentrmc/plugins/opensquilla-bridge/` — ~350 lines of production-hardened lifecycle code that in-process delivery can largely delete.
- Companion-side contract: `native/src/core/AgentBridge.h` (endpoint semantics), `SessionDispatch.h` (dispatch/ack state machine; OpenSquilla is already a first-class recognized host there).
- OpenSquilla anchors: `gateway/service_owner.py`, `gateway/boot.py` (`optional_owners`), `gateway/event_bridge.py`, `gateway/audio_transcription.py` (single-provider factory to replace), `provider/audio.py`, `runtime_packs/catalog.py`, `docs/features/tui-frontend.md` (Plugin Slots precedent).
