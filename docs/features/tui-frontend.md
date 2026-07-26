# TUI Frontend

OpenSquilla terminal chat exposes one public UI policy over two renderers:

| Backend or target | Status | How to use | Requirements |
| --- | --- | --- | --- |
| `auto` | Default policy | `opensquilla chat` or `--ui auto` | Installed host when available; startup-only plain fallback |
| `tui` | Strict full-screen TUI | `opensquilla chat --ui tui` | Source override today; future same-version companion |
| `plain` | Minimal rescue surface | `opensquilla chat --ui plain` | Python package only |
| `live-opentui` | Manual harness target | Real-terminal harness only | tmux, OpenTUI deps, and live provider config |

`live-opentui` is not an `OPENSQUILLA_TUI_BACKEND` value. It is a guarded test
target that launches the OpenTUI path through the real CLI.

The TUI contracts are renderer-independent and built around two separate planes:

- **Streaming plane:** batches token deltas before writing to the terminal, so
  long answers do not redraw the whole interface for every token.
- **Structured UI plane:** sends normalized TUI domain events to plugins. Plugin
  snapshots can be rendered by capable TUI backends and by future renderers.

The core wheel remains platform-neutral, and current releases do not publish a
TUI companion. Source checkouts run the host through the explicit development
override below. The repository also contains a builder that can produce a
self-contained `opensquilla-tui-host` artifact for validation, but the formal release workflow
and installer do not publish or install it. Release installs therefore use the
`plain` fallback.

## Plugin Slots

Plugins consume renderer-independent events and publish small snapshots through
named slots. Current slots include:

| Slot | Purpose |
| --- | --- |
| `router_hud` | Active-turn model-routing decision. |
| `status` | Compact status or queue notices. |
| `tool_activity` | Tool cards and tool summary history. |
| `usage` | Token, cache, and cost summary. |
| `inspector` | Optional detail panel state for selected items. |

The first plugin is `RouterHudPlugin`. It listens for
`router_decision` events and updates the bottom toolbar without changing router
selection behavior.

## Router HUD

When routing metadata is available, capable TUI backends can render a Router
HUD. In the current implementation, the OpenTUI footer is the primary terminal
display for this HUD. The HUD is display-only: it consumes turn metadata and
does not change model selection.

The HUD can show:

- selected tier and model;
- baseline model;
- route source;
- confidence;
- estimated savings;
- fallback state;
- thinking mode;
- prompt policy;
- whether routing was applied;
- rollout phase.

`routing_applied=true` with a full rollout is shown as an active route.
`routing_applied=false` or an observe rollout is shown as observe-only. Fallback
routes use warning styling. A real normal decision remains visible in the
compact footer (for example, `router c0 60%`); transport bootstrap placeholders
such as `gateway` are not presented as decisions. The decision state is cleared
at the start of every turn so a bypassed turn cannot inherit an earlier route.

## Responsive Transcript and Context

OpenTUI keeps one linear, scrollable transcript. Once `context.update` arrives,
a fixed one-line identity header presents the product, task, canonical Agent,
shared surface, and Gateway state. The same Agent label is used on retained and
new turn cards, including after a session context refresh.

At 132 terminal columns or wider, a 30–36-column context rail occupies the full
terminal height. Both the transcript and composer are inset by the rail width;
the rail does not introduce an independently scrolling message pane. At narrower
widths it collapses into a single priority-fitted footer strip. Layout and
clipping use terminal display cells, so CJK and emoji labels do not corrupt
borders or wrap the fixed header.

The additive `context.update` frame can carry Agent identity, task, surface,
Gateway state, model, permission, workspace, queue, and context information.
Older parents that do not send the frame retain the previous geometry and
router-only footer behavior.

An empty canonical history mounts a transcript-native welcome view with the
OpenSquilla wordmark, positioning, resolved runtime context, and first-action
shortcuts. The display typography selects a six-row `block`, two-row `tiny`, or
plain-text mode from the transcript's actual width and terminal height. History
replacement is authoritative: resumed content removes the welcome view, while
an empty `/new` or `/reset` snapshot remounts it.

OpenTUI's public renderer `resize` and `focus` events own normal viewport
recovery. Raw WriteStream resize and `SIGWINCH` are coalesced fallbacks only
when the renderer misses its resize event. A resize, remount, theme change, or
history replacement rebuilds transcript, rail, welcome, footer, and caret in
one pre-paint transaction and exposes one full frame. Healthy focus/resize
paths do not rewrite alternate-screen or mouse modes; the stronger mode
reassertion is reserved for explicit recovery and the first wheel after a
known blur. This avoids repeated screen swaps and keeps the caret inside the
composer.

There is no automatic periodic repaint in Codex, VS Code, or another terminal.
For diagnosis only, maintainers can opt in with a positive
`OPENSQUILLA_TUI_REPAINT_WATCHDOG_MS` value (clamped to at least 250ms). The
real-terminal gate must pass with the default event-driven value of zero.

## Complete Process Detail

`turn.begin` also opens a stable reasoning activity block immediately. It first
renders `Waiting for model output…`; real provider reasoning deltas append to
that same block and render incrementally. The live peek grows from three to at
most eight visual rows based on terminal height, emphasizing the newest line.
No synthetic reasoning is generated: a sub-second empty block disappears, a
longer empty block may settle as `Worked for Ns`, and a block containing provider
reasoning settles as `Thought for Ns`.

Thinking, reasoning, and tool renderers accumulate every delta delivered by the
host protocol. Tool detail includes full arguments, process updates, results,
and errors. Completed reasoning keeps up to eight of its latest visual rows, so
short traces remain fully visible and long traces retain their most recent
context without changing height on terminal resize. Other completed detail
stays compact. Folded content shows the number of hidden visual lines; this is
a presentation choice, not data discarding. Late deltas received after
`block.end` are retained as part of the same block.

When an ensemble actually executes, provider lifecycle events create one
in-place `Ensemble · n/m complete` block. `Ctrl+O` discloses public member model,
provider, status, elapsed time, tokens, cost, and error metadata. The completed
receipt and fallback reason survive history hydration. Candidate answer bodies
and private reasoning are never copied into this block. Configuration alone is
not treated as evidence that an Ensemble executed. The footer separately shows
the Gateway-owned `direct | router | ensemble` strategy. `/strategy` is the
primary picker and `/router` plus `/ensemble` remain compatibility controls;
all three update that canonical state through `models.routing.set`. During an
active Turn the footer labels it as the next-Turn strategy while the Turn keeps
rendering its captured Router decision or Ensemble lifecycle.

The composer remains interactive during streaming. Local UI commands execute
immediately. In Gateway mode, busy **Enter** requests native turn steering and
busy **Tab** explicitly queues a follow-up; a late/unavailable steer visibly
falls back to the bounded queue. Standalone turns keep their in-process
tool-boundary injection contract. History hydration and unresolved attachments
may still disable or block submission explicitly.

`Ctrl+O` expands or collapses all retained process detail without taking focus
from the composer. Expansion uses sanitized terminal text and the transcript's
current content width, including the wide-rail inset. This frontend guarantee
starts at the host-protocol boundary: upstream tool-result compression or
provider truncation remains governed by the separate
[`tool-compression.md`](tool-compression.md) contract.

## UI Selection

The public selector is `--ui auto|tui|plain`, and omitted `--ui` means `auto`.
`auto` may fall back only before alternate-screen startup. Explicit `tui` fails
clearly when the host is absent or incompatible. A host crash after startup
restores the terminal and exits; it does not switch renderers during a turn.

`OPENSQUILLA_TUI_BACKEND` is an internal handoff between the public selector and
the runtime adapters. Bare `opensquilla chat` ignores any pre-existing value so
a stale profile or workspace dotenv cannot disable the plain rescue path. New
user and source-development instructions must use `--ui`.

```sh
bun install --frozen-lockfile --cwd=src/opensquilla/cli/tui/opentui/package
OPENSQUILLA_TUI_DEV_SOURCE_HOST=1 uv run opensquilla chat --ui tui
```

The source backend is loaded only under an explicit developer override. The
resolver can validate a same-version installed companion for future rollout,
but current core-only release installs do not provide one.

Do not add parallel terminal/frontend implementations without fresh product
direction and replay plus real-terminal evidence.

## OpenTUI compatibility contract

The source host and development companion pin one exact `@opentui/core` version
(currently `0.4.3`) and one exact Bun toolchain. Product layout follows OpenTUI's documented
[renderer and resize contract](https://opentui.com/docs/core-concepts/renderer/),
[absolute renderable, mouse, and z-index APIs](https://opentui.com/docs/core-concepts/renderables/),
and public [ScrollBox contract](https://opentui.com/docs/components/scrollbox/).
It uses `onMouseScroll`, `scrollAcceleration`, and the documented cursor and
[lifecycle cleanup](https://opentui.com/docs/core-concepts/lifecycle/) APIs.
Application code must not override protected renderable methods.

OpenTUI 0.4.x documentation does not expose a pre-paint layout callback or a
public full-frame invalidation method. The typed `setFrameCallback` /
`calculateLayout` bridge is isolated in `opentuiCompat.mjs`, and the one private
full-repaint flag is isolated in `viewportRecovery.mjs`; no product component
may reach into either seam. Both adapters are re-audited on every OpenTUI
upgrade and removed as soon as documented upstream replacements exist.

Follow new OpenTUI releases deliberately rather than floating the dependency:

1. update the exact package and native-artifact lock together;
2. run the complete Node and Bun renderer suites;
3. run the styled framebuffer visual matrix at 80×24, 120×30, and 160×40;
4. pass real-terminal streaming, wheel, resize, focus/remount, alternate-screen,
   cursor, and teardown gates on macOS and Linux;
5. wire release assets only in a separate rollout after the packaged companion
   repeats the same gates on native runners.

An OpenTUI major version is eligible promptly, but it is never adopted solely
because it exists: public API review and the visual/terminal gates are the
compatibility decision.

Component tests use OpenTUI's official
[`@opentui/core/testing` renderer](https://opentui.com/docs/core-concepts/testing/)
for exact character/span/cursor assertions. The PTY/tmux harness remains the
separate integration oracle for real alternate-screen bytes and terminal mode
restoration.

## Replay Benchmarks

The replay harness measures the OpenTUI rendering path without a live provider:

```sh
uv run python scripts/bench_tui_replay.py --renderer opentui --fixture long-stream --summary-json .artifacts/tui/opentui-long-stream.json
uv run python scripts/bench_tui_replay.py --renderer opentui --fixture dense-history --summary-json .artifacts/tui/opentui-dense-history.json
```

Summary fields include `renderer`, `fixture`, `available`, `skip_reason`,
`event_count`, `text_chars`, `tool_count`, `router_decision_count`, `wall_ms`,
`flush_count`, `max_buffer_chars`, `coalescing_ratio`, `transcript_items`,
`visible_items`, `expanded_tools`, `projection_wall_ms`,
`rendered_text_matches`, `plugin_error_count`, and `errors`.

Use the OpenTUI results as renderer regression evidence. They validate the
development surface; they do not imply that a companion has been published.

For terminal-level launch and rendering evidence, use the
[real-terminal TUI harness](../tui-real-terminal-harness.md).

The product ownership and legacy-freeze rules are defined in
[`tui-product-contract.md`](tui-product-contract.md).
