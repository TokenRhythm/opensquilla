import { THEME } from "../theme.mjs";
import { TOOL_INDENT, clipToCells, stripTerminalControls, timelineAvailCells, wrapToCells } from "../primitives.mjs";
import { destroyRenderable } from "../renderableLifecycle.mjs";

// Intermediate assistant narration is useful context, but it should not turn a
// completed turn into an unbounded wall of process prose. Keep a readable
// preview after completion and retain every source byte behind an explicit,
// deterministic expansion API. While the block is live, all narration remains
// visible so the UI never appears to swallow an in-flight update.
const COMPLETED_PREVIEW_ROWS = 6;

export function createThinkingBlock(ctx) {
  const { renderer, TextRenderable, box, idPrefix } = ctx;
  const contentWidth = typeof ctx.contentWidth === "function"
    ? ctx.contentWidth
    : () => renderer.terminalWidth;
  let rawText = "";
  let done = false;
  let expanded = false;
  const rowNodes = [];
  let summaryNode = null;
  let hiddenLineCount = 0;

  function allRows() {
    // Strip only for display. rawText deliberately remains byte-for-byte equal
    // to the concatenated protocol deltas, including controls split across
    // delta boundaries, so expansion and diagnostics can never lose payload.
    const safe = stripTerminalControls(rawText).replace(/^\n+/, "");
    if (!safe) return [];
    const firstPrefix = `${TOOL_INDENT}✻ `;
    const avail = timelineAvailCells(firstPrefix, contentWidth());
    const rows = [];
    for (const line of safe.split("\n")) {
      for (const row of wrapToCells(line, avail)) rows.push(row);
    }
    return rows;
  }

  function insertAfter(node, previous) {
    const children = box.getChildren?.() ?? [];
    const index = previous ? children.indexOf(previous) : -1;
    box.add(node, index >= 0 ? index + 1 : undefined);
  }

  function reconcileRows(rows) {
    const firstPrefix = `${TOOL_INDENT}✻ `;
    const contPrefix = `${TOOL_INDENT}  `;
    const avail = timelineAvailCells(firstPrefix, contentWidth());
    while (rowNodes.length > rows.length) {
      const node = rowNodes.pop();
      destroyRenderable(box, node);
    }
    while (rowNodes.length < rows.length) {
      const index = rowNodes.length;
      const node = new TextRenderable(renderer, {
        id: `${idPrefix}-l${index}`,
        content: "",
        fg: done ? THEME.detailText : THEME.thinkingAccent,
      });
      insertAfter(node, rowNodes[index - 1] ?? null);
      rowNodes.push(node);
    }
    rows.forEach((row, index) => {
      rowNodes[index].content = `${index === 0 ? firstPrefix : contPrefix}${clipToCells(row, avail)}`;
      rowNodes[index].fg = done ? THEME.detailText : THEME.thinkingAccent;
    });
  }

  function render() {
    const rows = allRows();
    const collapse = done && !expanded && rows.length > COMPLETED_PREVIEW_ROWS;
    const visible = collapse ? rows.slice(0, COMPLETED_PREVIEW_ROWS) : rows;
    hiddenLineCount = collapse ? rows.length - visible.length : 0;
    reconcileRows(visible);

    if (hiddenLineCount > 0) {
      const suffix = hiddenLineCount === 1 ? "line" : "lines";
      const content = `${TOOL_INDENT}  ▸ ${hiddenLineCount} more ${suffix} · expand details`;
      if (!summaryNode) {
        summaryNode = new TextRenderable(renderer, {
          id: `${idPrefix}-summary`,
          content,
          fg: THEME.muted,
        });
        insertAfter(summaryNode, rowNodes.at(-1) ?? null);
      } else {
        summaryNode.content = content;
        summaryNode.fg = THEME.muted;
      }
    } else if (summaryNode) {
      destroyRenderable(box, summaryNode);
      summaryNode = null;
    }
    renderer.requestRender?.();
  }

  function toggleExpanded(force) {
    const next = typeof force === "boolean" ? force : !expanded;
    if (next === expanded) return expanded;
    expanded = next;
    render();
    return expanded;
  }

  return {
    get rawText() { return rawText; },
    get isExpanded() { return expanded; },
    get hiddenLineCount() { return hiddenLineCount; },
    begin(meta = {}) {
      const seed = meta?.text;
      if (seed !== undefined && seed !== null) rawText += String(seed);
      render();
    },
    append(delta) {
      rawText += String(delta ?? "");
      render();
    },
    update(patch = {}) {
      let changed = false;
      if (Object.prototype.hasOwnProperty.call(patch ?? {}, "text")) {
        // A terminal snapshot is authoritative, including an explicit empty
        // string used to withdraw a stale streamed preview.
        rawText = String(patch.text ?? "");
        changed = true;
      }
      if (typeof patch.expanded === "boolean" && patch.expanded !== expanded) {
        expanded = patch.expanded;
        changed = true;
      }
      // Text and disclosure state can arrive in one protocol patch. Reconcile
      // them in a single render transaction so row destruction/creation never
      // exposes an intermediate stale frame.
      if (changed) render();
    },
    end() {
      done = true;
      render();
    },
    toggleExpanded,
    // Re-wrap every row from the raw text at the current terminal width, so a
    // resize re-flows narration instead of leaving rows wrapped or clipped to
    // the old width.
    relayout() { render(); },
    recolor() {
      for (const node of rowNodes) node.fg = done ? THEME.detailText : THEME.thinkingAccent;
      if (summaryNode) summaryNode.fg = THEME.muted;
    },
  };
}
