---
name: visualize
visibility: public
invocation: direct
description: Create interactive diagrams, simulations, charts, and small tools directly inside conversation. Use when manipulating a visual helps explain, compare, or explore a question.
description_zh: 在对话正文中创建可交互图表、模拟器、原型与小工具，用于解释、比较和探索。
provenance:
  origin: opensquilla-original
  license: Apache-2.0
  maintained_by: OpenSquilla
metadata:
  opensquilla:
    requires_tools:
      - open_workspace_preview
---

# Interactive visuals in conversation

Choose a visual that helps answer the user's question: relationships for an
architecture, steps for a process, aligned views for a comparison, or linked
inputs and results for a simulation. Every control should visibly change the
diagram, selection, result, or explanation. Do not substitute a long statistics
report for a relationship diagram or add decorative controls with no effect.

## Fit the conversation

Default to a compact HTML fragment with its own styles and scripts, saved as a
UTF-8 `.html` file. Full HTML documents remain supported for existing canvases
or tasks that need them. The host supplies the isolated frame and its controls;
fragments do not need `html`, `head`, `body`, or a second page shell.

Let the surrounding answer introduce and explain the visual. Use a small local
title only when it helps. Avoid repeating the answer's title inside a large hero,
adding app navigation, or nesting many bordered cards. Prefer one clear visual
and nearby controls; reveal secondary detail on selection. A simulation may
need more space than a diagram: optimize for understanding, not a fixed layout.

Use fluid widths, wrapping controls, touch-friendly targets, readable labels and
keyboard access. Design for 360 px mobile width. For full documents include a
viewport meta tag. Keep the background transparent unless a meaningful region
needs contrast. Use host tokens `--color-background`, `--color-text-primary`,
`--color-text-secondary`, and `--color-border` with sensible CSS fallbacks; avoid
fixed light backgrounds or dark text. The host handles frame spacing and resize.

Optional original OpenSquilla examples show design choices, not a component
catalog or required templates. Read only the relevant example via `skill_view`:
- [Parameter simulation](references/parameter-simulation.txt): dependent inputs,
  units, state restoration, and a concrete result check.
- [Relationship exploration](references/relationship-exploration.txt): focused
  nodes and edges, selectable detail, and a small-screen alternative.
- [Before/after comparison](references/before-after-comparison.txt): synchronized
  views, a step control, and observable differences.

## Publish the exact result

Create the self-contained file in the current workspace, then call
`open_workspace_preview(path="relative/path.html", presentation="inline")`.
After a successful `inline-ready` result, place its exact `marker` in a standalone
inline-code paragraph between the relevant explanatory paragraphs. For example:

`visualize:doc_example:rev_example`

Use only the marker returned by the tool; do not invent IDs. The tool does not
finish the response. Continue explaining what the user can try and what it means.
An update must call the tool again to capture a new immutable version.
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

## State and conversation follow-up

The host provides `window.opensquillaVisualize` before your scripts execute:

```js
const visual = window.opensquillaVisualize;
await visual.ready;
const saved = visual.getState(); // a small JSON value, or null/undefined
visual.setState({ selected: "option-a", amount: 5 });
// Only behind an explicit user click or form submit:
visual.sendPrompt("Explain option A with amount 5.");
```

Restore all important inputs, selections, tabs and simulation progress from
saved state on startup, validate their ranges, and derive outputs from that
state. Save after changes and after reset; saving only the selected tab is not
enough when the user has adjusted parameters. Keep state under 16 KiB; it is
scoped to this browser session and exact canvas version, not a shared database.
It does not synchronize across devices or migrate automatically to a new version.
`sendPrompt` proposes text for a host confirmation button; it does not send the
chat automatically. Include the relevant selection/parameters in readable text.
Never request tool execution or credentials from inside the canvas.
