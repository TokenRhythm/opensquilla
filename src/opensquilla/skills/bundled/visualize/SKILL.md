---
name: visualize
visibility: public
invocation: direct
description: Create interactive diagrams, simulations, charts, and small tools directly inside conversation. Use when manipulating a visual helps explain, compare, or explore a question.
description_zh: 在对话正文中创建可交互图表、模拟器、原型与小工具，用于解释、比较和探索。
provenance:
  origin: opensquilla-adapted-generative-frame
  license: Apache-2.0 AND MIT
  upstream_url: https://github.com/assistant-ui/assistant-ui/tree/main/packages/generative-frame
  maintained_by: OpenSquilla
metadata:
  opensquilla:
    requires_tools:
      - show_widget
---

# Interactive visuals in conversation

Choose a visual that helps answer the user's question: relationships for an
architecture, steps for a process, aligned views for a comparison, or linked
inputs and results for a simulation. Every control should visibly change the
diagram, selection, result, or explanation. Do not substitute a long statistics
report for a relationship diagram or add decorative controls with no effect.

## Read the relevant guidance

Read [Shared visual guidance](references/guidance-base.txt) through `skill_view`
before creating a visual. Then read the relevant modules, combining them when
useful; they describe general capabilities, not a fixed catalog of examples:

- [Diagrams](references/guidance-diagram.txt): architectures, flows, sequences,
  relationships and hierarchies.
- [Charts](references/guidance-chart.txt): comparisons, trends, distributions,
  proportions and correlations.
- [Data views](references/guidance-data_viz.txt): dashboards, tables, heatmaps,
  custom data graphics and maps from available embedded data.
- [Interactive explanations](references/guidance-interactive.txt): simulations,
  calculators and controls that visibly change the result.
- [Interface mockups](references/guidance-mockup.txt): product interfaces and
  prototypes with local demonstration interactions.
- [Information collection](references/guidance-elicitation.txt): several related
  choices or constraints needed for an explicitly requested subsequent task.
- [Illustrations](references/guidance-art.txt): explanatory drawings and art.

For example, a selectable architecture uses diagrams plus interactive guidance;
a filterable dataset uses charts or data views plus interactive guidance. Choose
the visual from the user's question, not from the examples below. A plain answer,
Markdown table or static diagram may already be sufficient.

These generated references preserve the MIT-licensed generative-frame 0.0.5
guidance with explicit OpenSquilla host adaptations. Source hashes, adaptation
records and license text are bundled in `upstream-guidance.json` and
`LICENSE.generative-frame.txt`. The publication and host contracts in this file
and the three optional examples below are OpenSquilla content.

## Fit the conversation

Default to a compact HTML fragment with its own styles and scripts and call
`show_widget(title="short_title", widget_code="...")` to place it directly in
the conversation. Full HTML documents and workspace files remain supported for
existing canvases or tasks that need them. The host supplies the isolated frame
and its controls; fragments do not need `html`, `head`, `body`, or a second page
shell. Use `open_workspace_preview` only when the user asks for an editable
source file, a downloadable webpage, a multi-file bundle, or a workbench page.

Let the surrounding answer introduce and explain the visual. Use a small local
title only when it helps. Avoid repeating the answer's title inside a large hero,
adding app navigation, or nesting many bordered cards. Prefer one clear visual
and nearby controls; reveal secondary detail on selection. A simulation may
need more space than a diagram: optimize for understanding, not a fixed layout.

Use fluid widths, wrapping controls, touch-friendly targets, readable labels and
keyboard access. Design for 360 px mobile width. For full documents include a
viewport meta tag. Keep the background transparent unless a meaningful region
needs contrast. Use the full host token table and native-control styles in the
shared guidance; do not invent a separate palette or oversized primary buttons.
The host handles frame spacing, expansion, reload and export controls.

Optional original OpenSquilla examples show design choices, not a component
catalog or required templates. Read only the relevant example via `skill_view`:
- [Parameter simulation](references/parameter-simulation.txt): dependent inputs,
  units, state restoration, and a concrete result check.
- [Relationship exploration](references/relationship-exploration.txt): focused
  nodes and edges, selectable detail, and a small-screen alternative.
- [Before/after comparison](references/before-after-comparison.txt): synchronized
  views, a step control, and observable differences.

## Publish the exact result

For a pure in-chat visual, call `show_widget` with the exact HTML fragment or
standalone SVG. It returns an immutable Visualize Artifact receipt. Place its
exact `marker` in a standalone inline-code paragraph between the relevant
explanatory paragraphs. For example:

`visualize:doc_example:rev_example`

Use only the marker returned by the tool; do not invent IDs. The tool does not
finish the response. Continue explaining what the user can try and what it means.
An update should call `show_widget` again with the complete updated widget; the
first adapter stores a new immutable receipt for each call. Do not assume that
reusing a title edits an existing Artifact yet.

When a file or bundle is required, create the self-contained file in the current
workspace, then call
`open_workspace_preview(path="relative/path.html", presentation="inline")`.
Use the exact marker from that result as above. This path remains the supported
one for source delivery and multi-file resources.
When creating several examples, give each a separate file and include each final
marker beside its explanation. Do not overwrite all examples into one file.

`inline-ready` means the bytes were registered, not that a browser rendered them.
`renderStatus: not-observed` is not a successful visual check. Before publishing,
check key calculations with named input/output cases and check all control
handlers. When browser tools are available, inspect the published version in
the actual host at desktop and narrow widths, operate its controls, and check
state restoration and follow-up behavior. Distinguish what you tested from what
you could not observe. Never infer successful interaction from registration.

If publication returns a correctable HTML input error, fix that specific problem
and publish again; do not blindly repeat the same bytes. If a user supplies a
canvas error, use its revision and diagnosis to repair the source, then publish
a new version. A retry does not verify the new version's behavior by itself.

The canvas is an isolated browser document running on desktop, remote WebUI and
mobile browsers. Bundle all CSS, JavaScript, SVG, and data in the HTML. Do not use
network fetches, CDNs, local file paths, external frames, cookies or localStorage.
Use SVG/Canvas/native DOM for charts and diagrams. Keep the file under 1 MiB.

## Restore local interaction state

The host provides `window.opensquillaVisualize` before your scripts execute:

```js
const visual = window.opensquillaVisualize;
await visual.ready;
const saved = visual.getState(); // a small JSON value, or null/undefined
// Validate saved values into your local state before the first render.
```

Restore all important inputs, selections, tabs and simulation progress from
saved state on startup, validate their ranges, and derive outputs from that
state. Await readiness and capture `getState()` before the first render or any
`setState()` call. A render function that also saves must not run with defaults
before restoration: that would overwrite the saved state before you read it.
Keep render/resize functions free of state writes. Call
`visual.setState(currentState)` from validated change/reset handlers after
restoration. Keep initialization separate from saving user changes. Verify a
non-default selection survives both canvas reload and a session round trip.
For paused simulations, preserve the visible time, angle or progress as well as
the pause flag: restoring the controls while resetting the picture is not a
successful restore. Save these on discrete user changes or pause, not every
animation frame. Treat zero as a valid value and accept older snapshots with
missing fields.
Save after changes and after reset; saving only the selected tab is not
enough when the user has adjusted parameters. Keep state under 16 KiB; it is
scoped to this browser session and exact canvas version, not a shared database.
It does not synchronize across devices or migrate automatically to a new version.

Calculations, filtering, selection, highlighting, playback and ordinary prototype
actions stay local. Do not add a generic "continue discussion" or submit-to-chat
button to complete a visual. New visuals must not call legacy `sendPrompt`.
The local visual should answer the question without an extra model turn.

## Optional conditions for a subsequent model task

Use this only when the requested workflow benefits from model work with the
user's chosen conditions. Ordinary charts and simulations do not need it. The
host provides a consistent action to use prepared conditions in the conversation;
do not recreate that action inside the generated HTML.

```js
// After valid task conditions change, including restored conditions:
await visual.setContext({
  summary: "A selected route with a 20-minute transfer limit",
  data: { selectedRoute: "route-a", maxTransferMinutes: 20 },
  suggestedPrompt: "Compare this route with alternatives under these conditions."
});
// Clear a stale or invalid selection:
await visual.setContext(null);
```

`summary` is required, nonempty text up to 400 characters. Optional `data` is
plain JSON with finite numbers, nesting depth at most eight from its root, and
at most 2048 JSON nodes. Optional
`suggestedPrompt` is task-specific editable text up to 2000 characters. The whole
context must fit in 8 KiB of JSON; no other fields, functions, cycles or secrets.
Describe actual selected values, units and the requested next task, not generic
"continue" text. Do not copy the whole document into context.

`setContext` acknowledges preparation only: it does not call the model, send a
message, change the chat draft, or make state automatically visible to the model.
The user chooses the host action to add a removable conditions block and an
editable draft, then sends the message from the chat composer. Preserve this
distinction in any feedback. Keep fields editable; update or clear context when
conditions change or become invalid. Recompute useful context after restoring
local state rather than assuming an old context snapshot is still active.
Never request tool execution or credentials from inside the canvas.
