# TUI product contract

`opensquilla chat` is OpenSquilla's interactive terminal client. The Web UI is
the control plane for configuration, monitoring, and multi-session management;
the TUI is a first-class client for working in one current session. Both clients
project the same Gateway-owned session rather than copying or owning it.

## Ownership

- The Gateway owns sessions, turns, history, queues, tool execution, usage, and
  approval decisions, plus the persistent `direct | router | ensemble` model
  strategy.
- TUI and Web UI share canonical messages and task state. Draft text, cursor,
  scroll position, theme, and local attachment staging remain client-local.
- A turn records its origin surface and reply target. A session's previous
  channel must never determine where a new TUI turn is delivered.
- `--standalone` is an explicit isolated runtime. A Gateway failure never
  silently changes a normal chat into standalone mode.

## UI selection

`opensquilla chat --ui auto|tui|plain` selects presentation only:

- Omitting `--ui` is equivalent to `auto`.
- `auto` uses an installed, compatible OpenTUI host when one exists and may
  fall back to plain only before the alternate screen starts. Current releases
  do not publish that host.
- `tui` requires OpenTUI and fails with a diagnostic when it is unavailable.
- `plain` is a minimal terminal rescue surface over the same runtime contracts,
  not a separately evolving chat product.

Once a full-screen session starts, a renderer crash restores the terminal and
exits. It does not hot-switch renderers mid-turn.

## Transcript and detail fidelity

The TUI presents one linear session transcript. Web UI remains the control
plane; the responsive identity header and wide context rail are projections of
Gateway-owned context, not a second local identity or session model. At 132
columns or wider the rail spans the terminal height; below that width the same
state collapses into a compact footer strip.

An empty session may render a responsive product wordmark and start guidance in
the transcript. It must not appear inside resumed canonical history. On submit,
the transcript must show a live activity row before the provider's first event;
provider reasoning streams into that same row. If a provider exposes no
reasoning, the client may describe the observable wait but must not synthesize
or imply private thought content.

Every thinking, reasoning, tool-argument, tool-process, tool-result, and
tool-error delta delivered to the TUI is retained. Completed process detail may
be folded by default to preserve transcript readability, but folding must expose
a hidden-line count and must be reversible with `Ctrl+O`; it must never be used
as destructive truncation. This promise applies at the TUI protocol boundary
and does not override an explicit upstream provider or Gateway compression
contract.

A real Router decision must remain visible in both wide and compact context
layouts and must be reset at the next turn boundary. An executed ensemble must
have a turn-scoped live progress block and a durable completion receipt. The
receipt may contain public member execution metadata, but never candidate
answer bodies or private reasoning. Clients must not infer ensemble execution
from an enabled configuration flag.

`/router` and `/ensemble` are Gateway control commands, not chat messages.
Their bare forms open one shared three-state picker; `on`, `off`, and `status`
offer direct keyboard control. They never echo as prompts, steer, queue, or
cancel an active Turn. A successful write is broadcast across TUI and WebUI and
affects only the next accepted Turn; the current Turn retains its admission-time
strategy snapshot. Standalone mode is explicitly read-only for this contract.

Streaming a turn does not lock the composer. Local UI commands remain
immediate; accepted follow-ups are queued or steered according to the shared
runtime contract. Explicit loading, attachment, approval, and terminal-safety
states may still restrict input.

## Development status and legacy policy

The alternate-screen OpenTUI is the intended terminal product, but remains a
source-checkout development surface. Formal releases do not publish or install
its companion host yet, so installed users continue on `plain`. Legacy-only
entrypoints and implementation are frozen pending their dedicated cleanup;
they receive no new product features or parity work. `plain` is the rescue
presentation over the shared Gateway runtime, not a second product contract.

## Deferred platform distribution

The repository contains a self-contained companion package and macOS/Linux
builders so distribution can be validated before rollout. They are not wired
to the formal release workflow or installer. A future rollout must keep core
and host on the same version and pass native platform gates before adding any
release assets. A missing or incompatible host under `auto` triggers the
startup-only `plain` fallback; under strict `--ui tui` it is an error.

Native Windows remains separate work for its host artifact,
ConPTY terminal lifecycle, process-tree cleanup, signing, installer, and native
terminal evidence. It reuses these additive product and Gateway contracts and
must not require macOS/Linux-specific behavior changes.
