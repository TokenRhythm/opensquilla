# Desktop-Pet Reminder & Directed Dispatch

Date: 2026-11-13

## Goal

Bring the three voice/companion capabilities that previously lived outside the
gateway (an external Node bridge + VBS + scheduled-task watchdog) in-process as
first-class, opt-in gateway features:

1. **Reminder service (PR-2)** — subscribe to session events and drive the
   AgentRMC native loopback bridge (`/notify`, `/speak`) directly.
2. **Directed dispatch (PR-3)** — poll the native dispatch queue and inject
   pet-originated messages into the right session through the gateway's normal
   admission path.

The external bridge (`opensquilla-pet-bridge.mjs`) becomes unnecessary; the
gateway owns the lifecycle, so a gateway restart restarts the service and there
is no cross-process keep-alive to fail.

## Red lines

- Only OpenSquilla's own links are implemented. No other host (Qoder/ZCode/DSH/
  WorkBuddy) reminders or dispatch.
- The AgentRMC native side and the AgentBridge HTTP contract are **not modified**.
- Default off `{ enabled = false }`: users without the pet get zero behavior and
  zero port probes.

## Architecture

The `opensquilla.pet` package talks to the gateway only through narrow seams
(`pet/ports.py`): an event source, an RPC invoker, and a redactor. Importing the
package has no side effects.

- **Event funnel** — `SessionStreamRegistry.record()` (the single funnel for all
  live `session.event.*` frames, used by both the RPC and channel paths) gained
  an in-process observer hook (`add_local_observer`). The service registers a
  cheap, non-blocking callback that enqueues events; observer faults are
  isolated and never affect replay buffering or WebSocket delivery.
- **Mapping & dedup** (`pet/events.py`) — ported from the external bridge:
  `done`/`chat.done`/`task.succeeded` → `stop`, `approval_required` →
  `permission`, `plan_presentation` → `question`, `turn_committed` →
  `turn-start`. Identity-aware dedup (per approval/turn/task id, 15s window)
  with a session-level watermark for identity-less echoes; a new turn clears the
  session ledger.
- **Clarify/choice cards** — the interactive "需要输入" form is *not* a dedicated
  event: it rides a `session.event.tool_result` whose `result` is a paused
  user-input contract (`kind="user_input"`, `paused=true`, `request_id`,
  `clarify_schema`). `pending_clarify_request()` inspects the tool-result payload
  and maps a pending form to a `question` notification (speak + raise), deduped
  by `request_id`; answered/cancelled/expired outcomes are ignored. This covers
  the selection-card case the original external bridge did not handle.
- **Bridge client** (`pet/bridge_client.py`) — loopback-only httpx client
  (`trust_env=False`, no redirects). Notify/speak succeed on `204`.
- **Offline spool** (`pet/spool.py`) — on notify failure, append to the legacy
  `%APPDATA%/LIG/AgentRMCNative/pending-events.jsonl` (1MB cap, keep newest) so
  the native `importSpool()` replays it unchanged. A pause file mutes output.
- **Dispatch** (`pet/dispatch.py`) — poll `/dispatch/pending?exe=...`, guard with
  `sessions.resolve`, inject via `sessions.send` using a `client_message_id`
  derived from `dispatch_id` (idempotent re-poll/retry), then `/dispatch/ack`.
  The RPC invoker reuses the exact gateway admission path through an operator
  `RpcContext` (no bypass). Timeout/reconnect is reported as *uncertain*,
  distinct from a definite rejection.
- **Lifecycle** — mounted in `boot.py` as the `desktop_pet` deferred warmup
  (gated on `[pet].enabled`), stopped during gateway close before the task
  runtime drains.

Notification text is the configured template, redacted and truncated (~120
chars); raw session content is never echoed to the pet.

## Configuration

See `[pet]` in `opensquilla.toml.example`. Key fields: `enabled`, `bridge_url`,
`exe`, `raise_window`, `include_cron`, `dispatch_enabled`, `poll_interval_ms`,
`dedupe_window_ms`, `spool_dir`, `pause_file`, and the `msg_*` templates.

## Tool & CLI surface

- Tools: `pet_speak`, `pet_notify` (policy-governed; return `not_available` when
  the service is disabled or the pet is unreachable).
- CLI: `opensquilla pet status|test|say` — diagnostics that talk to the native
  bridge directly (work regardless of the in-process service).

## Testing

`tests/test_pet.py` covers the mapping table, identity dedup + turn reset, spool
cap, dispatch guard/idempotent cmid/ack, and the service notify/spool/pause/
redaction flow — all offline against an httpx fake bridge and in-memory event/
RPC fakes. The reminder path was additionally verified live against a running
AgentRMC native bridge (`record()` → observer → real `/notify` 204).
