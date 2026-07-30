import { THEME } from "../theme.mjs";
import { TOOL_INDENT, clipToCells, stripTerminalControls, timelineAvailCells, wrapToCells } from "../primitives.mjs";
import { destroyRenderable } from "../renderableLifecycle.mjs";
import { rendererViewportSnapshot } from "../screenMode.mjs";

// Extended reasoning stays bounded by default, but bounded no longer means
// invisible or destructive: every delta is retained, completed reasoning keeps
// a substantial fixed preview, and toggleExpanded() deterministically
// reveals/collapses the complete sanitized payload. Keeping the completed cap
// independent from terminal height avoids reflowing old turns on every resize.
const MIN_LIVE_PEEK_ROWS = 3;
const MAX_LIVE_PEEK_ROWS = 8;
const COMPLETED_PREVIEW_ROWS = 8;

export function livePeekRows(terminalHeight) {
  const height = Math.max(1, Number(terminalHeight) || 24);
  return Math.max(MIN_LIVE_PEEK_ROWS, Math.min(MAX_LIVE_PEEK_ROWS, Math.floor(height / 5)));
}

export function createReasoningBlock(ctx) {
  const { renderer, TextRenderable, box, idPrefix } = ctx;
  const contentWidth = typeof ctx.contentWidth === "function"
    ? ctx.contentWidth
    : () => renderer.terminalWidth;
  const viewport = typeof ctx.viewport === "function"
    ? ctx.viewport
    : () => rendererViewportSnapshot(renderer);
  const headerId = `${idPrefix}-mark`;
  let header = null;
  const detailNodes = [];
  let rawText = "";
  let startedAt = null;
  let elapsedAtEnd = null;
  let elapsedHintAtEnd = null;
  let done = false;
  let expanded = false;
  let glyph = "✻";
  let hiddenLineCount = 0;
  let waiting = false;

  const elapsedSeconds = () => {
    if (elapsedAtEnd !== null) return elapsedAtEnd;
    return startedAt === null ? 0 : Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
  };

  function headerContent() {
    if (done) {
      const verb = rawText.trim() ? "Thought" : "Worked";
      return `${TOOL_INDENT}✻ ${verb} for ${elapsedSeconds()}s`;
    }
    const elapsed = elapsedSeconds();
    const timer = elapsed >= 2 ? ` · ${elapsed}s` : "";
    return `${TOOL_INDENT}${glyph} Thinking${timer}`;
  }

  function ensureHeader() {
    if (header) return;
    if (startedAt === null) startedAt = Date.now();
    header = new TextRenderable(renderer, {
      id: headerId,
      content: headerContent(),
      // Keep the semantic purple marker after completion. The reasoning body
      // settles to bounded muted detail, so retained history stays quiet while
      // `Thought` cannot be mistaken for an answer or usage receipt.
      fg: THEME.thinkingAccent,
    });
    box.add(header);
  }

  function allRows() {
    const safe = stripTerminalControls(rawText).replace(/^\n+/, "");
    if (!safe) return [];
    const prefix = `${TOOL_INDENT}  `;
    const avail = timelineAvailCells(prefix, contentWidth());
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

  function detailSpecs() {
    const rows = allRows();
    const prefix = `${TOOL_INDENT}  `;
    const avail = timelineAvailCells(prefix, contentWidth());

    if (expanded) {
      hiddenLineCount = 0;
      const specs = rows.map((line) => ({
        content: `${prefix}${clipToCells(line, avail)}`,
        fg: THEME.detailText,
      }));
      if (rows.length) {
        specs.push({ content: `${prefix}▾ collapse details`, fg: THEME.muted });
      }
      return specs;
    }

    if (done) {
      if (!rows.length) return [];
      const visible = rows.slice(-COMPLETED_PREVIEW_ROWS);
      hiddenLineCount = Math.max(0, rows.length - visible.length);
      const specs = [];
      if (hiddenLineCount) {
        specs.push({
          content: `${prefix}… ${hiddenLineCount} earlier · Ctrl+O details`,
          fg: THEME.muted,
        });
      }
      specs.push(...visible.map((line) => ({
        content: `${prefix}${clipToCells(line, avail)}`,
        fg: THEME.detailText,
      })));
      return specs;
    }

    if (!rows.length && waiting) {
      hiddenLineCount = 0;
      return [{
        content: `${prefix}Waiting for model output…`,
        fg: THEME.detailText,
      }];
    }

    const visible = rows.slice(-livePeekRows(viewport().height));
    hiddenLineCount = Math.max(0, rows.length - visible.length);
    const specs = [];
    if (hiddenLineCount) {
      const noun = hiddenLineCount === 1 ? "line" : "lines";
      specs.push({
        content: `${prefix}… ${hiddenLineCount} earlier ${noun}`,
        fg: THEME.muted,
      });
    }
    for (const [index, line] of visible.entries()) {
      // The newest streamed line carries full text contrast; older context is
      // intentionally quieter. This makes forward progress legible without
      // turning the whole reasoning trace into a competing answer surface.
      const newest = index === visible.length - 1;
      specs.push({
        content: `${prefix}${clipToCells(line, avail)}`,
        fg: newest ? THEME.text : THEME.detailText,
      });
    }
    return specs;
  }

  function renderDetails() {
    // A sub-second silent wait is useful while it is happening but becomes
    // visual noise once another block arrives. Remove that transient row
    // completely; longer silent waits retain the honest "Worked for Ns"
    // receipt, and any real provider reasoning always retains "Thought".
    if (done && !rawText.trim() && elapsedSeconds() === 0) {
      while (detailNodes.length) {
        const node = detailNodes.pop();
        destroyRenderable(box, node);
      }
      if (header) {
        destroyRenderable(box, header);
        header = null;
      }
      hiddenLineCount = 0;
      renderer.requestRender?.();
      return;
    }
    ensureHeader();
    const specs = detailSpecs();
    while (detailNodes.length > specs.length) {
      const node = detailNodes.pop();
      destroyRenderable(box, node);
    }
    while (detailNodes.length < specs.length) {
      const index = detailNodes.length;
      const node = new TextRenderable(renderer, {
        id: `${idPrefix}-t${index}`,
        content: "",
        fg: THEME.detailText,
      });
      insertAfter(node, detailNodes[index - 1] ?? header);
      detailNodes.push(node);
    }
    specs.forEach((spec, index) => {
      detailNodes[index].content = spec.content;
      detailNodes[index].fg = spec.fg;
    });
    if (header) {
      header.content = headerContent();
      header.fg = THEME.thinkingAccent;
    }
    renderer.requestRender?.();
  }

  function setGlyph(next) {
    if (!header || done) return;
    glyph = next;
    header.content = headerContent();
    renderer.requestRender?.();
  }

  function toggleExpanded(force) {
    const next = typeof force === "boolean" ? force : !expanded;
    if (next === expanded) return expanded;
    expanded = next;
    renderDetails();
    return expanded;
  }

  return {
    get rawText() { return rawText; },
    get isExpanded() { return expanded; },
    get hiddenLineCount() { return hiddenLineCount; },
    begin(meta = {}) {
      if (startedAt === null) startedAt = Date.now();
      const elapsedHint = Number(meta?.elapsedSeconds);
      if (Number.isFinite(elapsedHint) && elapsedHint >= 0) {
        elapsedHintAtEnd = Math.floor(elapsedHint);
      }
      waiting = Boolean(meta?.waiting);
      const seed = meta?.text;
      if (seed !== undefined && seed !== null) {
        rawText += String(seed);
        if (String(seed)) waiting = false;
      }
      ensureHeader();
      renderDetails();
    },
    append(delta) {
      // Even a protocol-straggler arriving after block.end belongs to this
      // reasoning block. Retain it and refresh the preview count/expanded
      // body instead of silently dropping the late tail.
      const next = String(delta ?? "");
      rawText += next;
      if (next) waiting = false;
      renderDetails();
    },
    update(patch = {}) {
      if (typeof patch.expanded === "boolean") toggleExpanded(patch.expanded);
    },
    setGlyph,
    end() {
      if (!done) {
        elapsedAtEnd = elapsedHintAtEnd ?? elapsedSeconds();
        done = true;
      }
      renderDetails();
    },
    toggleExpanded,
    relayout() { renderDetails(); },
    recolor() {
      if (header) header.fg = THEME.thinkingAccent;
      // Recompute rather than assigning one token: expanded rows and the
      // collapsed disclosure row intentionally have different hierarchy.
      const specs = detailSpecs();
      specs.forEach((spec, index) => {
        if (detailNodes[index]) detailNodes[index].fg = spec.fg;
      });
    },
  };
}
