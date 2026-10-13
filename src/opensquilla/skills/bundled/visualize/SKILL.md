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

Create a self-contained UTF-8 HTML file in the current workspace, then call
`open_workspace_preview(path="relative/path.html", presentation="inline")`.
After a successful `inline-ready` result, place its exact `marker` in a standalone
inline-code paragraph between the relevant explanatory paragraphs. For example:

`visualize:doc_example:rev_example`

Use only the marker returned by the tool; do not invent IDs. The tool does not
finish the response. Continue explaining what the user can try and what it means.
An update must call the tool again to capture a new immutable version.

The canvas is an isolated browser document running on desktop, remote WebUI and
mobile browsers. Bundle all CSS, JavaScript, SVG, and data in the HTML. Do not use
network fetches, CDNs, local file paths, external frames, cookies or localStorage.
Use SVG/Canvas/native DOM for charts and diagrams. Keep the file under 1 MiB.

Use fluid widths and a viewport meta tag, touch-friendly controls, readable
labels and keyboard access. Design for 360 px mobile width. Respect the host theme
using `--color-background`, `--color-text-primary`, and `--color-text-secondary`.

The host provides `window.opensquillaVisualize` before your scripts execute:

```js
const visual = window.opensquillaVisualize;
await visual.ready;
const saved = visual.getState(); // a small JSON value, or null/undefined
visual.setState({ selected: "option-a", amount: 5 });
// Only behind an explicit user click or form submit:
visual.sendPrompt("Explain option A with amount 5.");
```

Restore controls from saved state on startup. Keep state under 16 KiB; it is
scoped to this browser session and exact canvas version, not a shared database.
`sendPrompt` proposes text for a host confirmation button; it does not send the
chat automatically. Include the relevant selection/parameters in readable text.
Never request tool execution or credentials from inside the canvas.
