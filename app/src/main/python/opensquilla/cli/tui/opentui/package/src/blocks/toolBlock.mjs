import { STATUS, STATUS_PULSE_FRAMES, THEME } from "../theme.mjs";
import { DURATION_SEP, TOOL_INDENT, clipToCells, stripTerminalControls, textWidth, timelineAvailCells, wrapToCells } from "../primitives.mjs";
import { destroyRenderable } from "../renderableLifecycle.mjs";

// Tool output is a stream, not a one-shot preview. Every append delta is
// concatenated into rawText even after block.end; the compact transcript only
// changes how much is mounted, never what is retained. The public expansion
// API is deliberately UI-agnostic so a turn-level shortcut can reveal every
// argument/process/result/error line without making individual rows focusable.
const RESULT_PREVIEW_ROWS = 2;
const LIVE_PREVIEW_ROWS = 3;

function payloadText(value) {
  if (value === undefined || value === null) return "";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function normalizedDisplayText(raw) {
  return stripTerminalControls(raw).replace(/\r\n?/g, "\n");
}

function displayRows(raw, cells) {
  const safe = normalizedDisplayText(raw);
  if (!safe) return [];
  const logical = safe.split("\n");
  // A final newline terminates the prior row; it does not need a second empty
  // detail row. Interior blank lines remain represented by a connector row.
  while (logical.length > 1 && logical.at(-1) === "") logical.pop();
  const rows = [];
  for (const line of logical) {
    for (const row of wrapToCells(line, cells)) rows.push(row);
  }
  return rows;
}

function noun(count, singular, plural = `${singular}s`) {
  return count === 1 ? singular : plural;
}

export function unifiedDiffSummary(raw) {
  const lines = normalizedDisplayText(raw).split("\n");
  const isDiff = lines.some((line) => line.startsWith("diff --git ") || line.startsWith("@@ "));
  if (!isDiff) return null;
  const files = new Set();
  let added = 0;
  let removed = 0;
  for (const line of lines) {
    const match = /^diff --git a\/(.+?) b\/(.+)$/u.exec(line);
    if (match) files.add(match[2]);
    else if (line.startsWith("+++ b/")) files.add(line.slice(6));
    if (line.startsWith("+") && !line.startsWith("+++")) added += 1;
    if (line.startsWith("-") && !line.startsWith("---")) removed += 1;
  }
  return { files: files.size, added, removed };
}

function resultLineColor(line, fallback) {
  if (line.startsWith("+") && !line.startsWith("+++")) return STATUS.ok;
  if (line.startsWith("-") && !line.startsWith("---")) return STATUS.detailError;
  if (line.startsWith("@@ ")) return THEME.warning;
  if (line.startsWith("diff --git ") || line.startsWith("--- ") || line.startsWith("+++ ")) {
    return THEME.info ?? THEME.routeText;
  }
  return fallback;
}

export function createToolBlock(ctx) {
  const { renderer, TextRenderable, box, idPrefix } = ctx;
  const contentWidth = typeof ctx.contentWidth === "function"
    ? ctx.contentWidth
    : () => renderer.terminalWidth;
  let node = null;
  const detailNodes = [];
  let rawName = "";
  let rawArgs = "";
  let argsSummary = "";
  let processRaw = "";
  let resultRaw = "";
  let errorRaw = "";
  let durationTail = "";
  let runState = "running"; // running | ok | error | complete
  let currentGlyph = STATUS_PULSE_FRAMES.tool[0];
  let expanded = false;
  let hiddenLineCount = 0;

  function safeName() {
    return normalizedDisplayText(rawName).replace(/\s+/g, " ").trim();
  }

  function safeArgsSummary() {
    const source = argsSummary || rawArgs;
    return normalizedDisplayText(source).replace(/\s+/g, " ").trim();
  }

  function stateGlyph() {
    if (runState === "error") return "✗";
    if (runState === "ok") return "✓";
    if (runState === "complete") return "•";
    return currentGlyph;
  }

  function stateColor() {
    if (runState === "error") return STATUS.error;
    if (runState === "ok") return STATUS.ok;
    if (runState === "complete") return STATUS.detail;
    return STATUS.running;
  }

  function invocationContent() {
    const glyph = stateGlyph();
    const prefix = `${TOOL_INDENT}${glyph} `;
    const summary = safeArgsSummary();
    const label = `${safeName()}${summary ? ` ${summary}` : ""}`.trim();
    // Keep the completion duration visible even when the name/args are long.
    const budget = Math.max(
      8,
      (contentWidth() ?? 80) - textWidth(prefix) - textWidth(durationTail) - 6,
    );
    return `${prefix}${clipToCells(label, budget)}${durationTail}`;
  }

  function renderInvocation() {
    if (!node) return;
    node.content = invocationContent();
    node.fg = stateColor();
    node._done = runState !== "running";
  }

  function detailBudget() {
    return timelineAvailCells(`${TOOL_INDENT}├ `, contentWidth());
  }

  function argsNeedDetails(rows) {
    if (!rows.length) return false;
    const full = normalizedDisplayText(rawArgs).replace(/\s+/g, " ").trim();
    const summary = safeArgsSummary();
    const inlineBudget = Math.max(8, (contentWidth() ?? 80) - 14);
    return normalizedDisplayText(rawArgs).includes("\n") || full !== summary || textWidth(full) > inlineBudget;
  }

  function previewSection(
    entries,
    label,
    rows,
    { tail = false, color = STATUS.detail, lineColor = null } = {},
  ) {
    if (!rows.length) return 0;
    const limit = runState === "running" ? LIVE_PREVIEW_ROWS : RESULT_PREVIEW_ROWS;
    const visible = tail ? rows.slice(-limit) : rows.slice(0, limit);
    const hidden = Math.max(0, rows.length - visible.length);
    if (tail && hidden) {
      entries.push({ text: `… ${hidden} earlier ${noun(hidden, "line")}`, fg: THEME.muted });
    }
    for (const line of visible) {
      entries.push({ text: line, fg: lineColor ? lineColor(line, color) : color });
    }
    if (!tail && hidden) {
      entries.push({
        text: `▸ ${hidden} more ${label} ${noun(hidden, "line")} · expand details`,
        fg: THEME.muted,
      });
    } else if (tail && hidden && runState !== "running") {
      entries.push({ text: "▸ expand details", fg: THEME.muted });
    }
    return hidden;
  }

  function expandedEntries(argsRows, processRows, resultRows, errorRows) {
    const entries = [];
    if (argsNeedDetails(argsRows)) {
      entries.push({ text: `args · ${argsRows.length} ${noun(argsRows.length, "line")}`, fg: THEME.muted });
      for (const line of argsRows) entries.push({ text: `  ${line}`, fg: STATUS.detail });
    }
    if (processRows.length) {
      entries.push({ text: `process · ${processRows.length} ${noun(processRows.length, "line")}`, fg: THEME.muted });
      for (const line of processRows) entries.push({ text: `  ${line}`, fg: STATUS.detail });
    }
    if (resultRows.length) {
      entries.push({ text: `output · ${resultRows.length} ${noun(resultRows.length, "line")}`, fg: THEME.muted });
      for (const line of resultRows) {
        const fallback = runState === "error" ? STATUS.detailError : STATUS.detail;
        entries.push({ text: `  ${line}`, fg: resultLineColor(line, fallback) });
      }
    }
    if (errorRows.length) {
      entries.push({ text: `error · ${errorRows.length} ${noun(errorRows.length, "line")}`, fg: STATUS.error });
      for (const line of errorRows) entries.push({ text: `  ${line}`, fg: STATUS.detailError });
    }
    if (entries.length) entries.push({ text: "▾ collapse details", fg: THEME.muted });
    return entries;
  }

  function detailSpecs() {
    const budget = detailBudget();
    const argsRows = displayRows(rawArgs, budget - 2);
    const processRows = displayRows(processRaw, budget - 2);
    const resultRows = displayRows(resultRaw, budget);
    const safeResult = normalizedDisplayText(resultRaw);
    const safeError = normalizedDisplayText(errorRaw);
    const diff = unifiedDiffSummary(safeResult);
    // Some providers put the same exception in both result and error. Retain
    // both raw fields, but avoid presenting an identical error body twice.
    const errorRows = safeError && !safeResult.includes(safeError)
      ? displayRows(errorRaw, budget - 2)
      : [];

    let entries;
    if (expanded) {
      hiddenLineCount = 0;
      entries = expandedEntries(argsRows, processRows, resultRows, errorRows);
    } else {
      entries = [];
      hiddenLineCount = 0;
      if (argsNeedDetails(argsRows)) {
        hiddenLineCount += argsRows.length;
        entries.push({
          text: `args · ${argsRows.length} ${noun(argsRows.length, "line")} hidden · expand details`,
          fg: THEME.muted,
        });
      }
      if (processRows.length) {
        const hidden = previewSection(entries, "process", processRows, {
          tail: runState === "running",
          color: STATUS.detail,
        });
        hiddenLineCount += hidden;
      }
      if (resultRows.length) {
        const hidden = previewSection(entries, "output", resultRows, {
          tail: runState === "running" || runState === "error",
          color: runState === "error" ? STATUS.detailError : STATUS.detail,
          lineColor: resultLineColor,
        });
        hiddenLineCount += hidden;
      }
      if (errorRows.length) {
        const hidden = previewSection(entries, "error", errorRows, {
          tail: true,
          color: STATUS.detailError,
        });
        hiddenLineCount += hidden;
      }
    }

    if (diff) {
      entries.unshift({
        text: `diff · ${diff.files} ${noun(diff.files, "file")} · +${diff.added} −${diff.removed}`,
        fg: THEME.metricPositive ?? STATUS.ok,
      });
    }

    return entries.map((entry, index) => ({
      content: `${TOOL_INDENT}${index === entries.length - 1 ? "└ " : "├ "}${entry.text}`,
      fg: entry.fg,
    }));
  }

  function insertAfter(nodeToInsert, previous) {
    const children = box.getChildren?.() ?? [];
    const index = previous ? children.indexOf(previous) : -1;
    box.add(nodeToInsert, index >= 0 ? index + 1 : undefined);
  }

  function renderDetails() {
    const specs = detailSpecs();
    while (detailNodes.length > specs.length) {
      const detail = detailNodes.pop();
      destroyRenderable(box, detail);
    }
    while (detailNodes.length < specs.length) {
      const index = detailNodes.length;
      const detail = new TextRenderable(renderer, {
        id: `${idPrefix}-detail-${index}`,
        content: "",
        fg: STATUS.detail,
      });
      insertAfter(detail, detailNodes[index - 1] ?? node);
      detailNodes.push(detail);
    }
    specs.forEach((spec, index) => {
      detailNodes[index].content = spec.content;
      detailNodes[index].fg = spec.fg;
    });
  }

  function render() {
    renderInvocation();
    renderDetails();
    renderer.requestRender?.();
  }

  function setGlyph(glyph) {
    if (runState !== "running") return;
    currentGlyph = glyph;
    renderInvocation();
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
    get node() { return node; },
    get isRunning() { return node !== null && runState === "running"; },
    get rawText() { return resultRaw; },
    get rawArgs() { return rawArgs; },
    get rawProcess() { return processRaw; },
    get rawError() { return errorRaw; },
    get isExpanded() { return expanded; },
    get hiddenLineCount() { return hiddenLineCount; },
    setGlyph,
    begin(meta = {}) {
      rawName = payloadText(meta?.name);
      rawArgs = payloadText(meta?.args_full ?? meta?.raw_args ?? meta?.args ?? meta?.input);
      argsSummary = payloadText(meta?.args_summary ?? meta?.summary);
      processRaw = payloadText(meta?.process);
      resultRaw = payloadText(meta?.result);
      errorRaw = payloadText(meta?.error);
      node = new TextRenderable(renderer, {
        id: `${idPrefix}-node`,
        content: invocationContent(),
        fg: STATUS.running,
      });
      box.add(node);
      render();
    },
    append(delta) {
      // Do not guard on a first-result flag or done state: append is a stream
      // contract, and every later delta must remain available to expansion.
      resultRaw += String(delta ?? "");
      render();
    },
    update(patch = {}) {
      if (patch.duration !== undefined && patch.duration !== null) {
        durationTail = `${DURATION_SEP}${stripTerminalControls(String(patch.duration))}`;
      }
      if (patch.args_full !== undefined || patch.raw_args !== undefined) {
        rawArgs = payloadText(patch.args_full ?? patch.raw_args);
      }
      if (patch.args_summary !== undefined || patch.summary !== undefined) {
        argsSummary = payloadText(patch.args_summary ?? patch.summary);
      }
      if (patch.process !== undefined) processRaw = payloadText(patch.process);
      if (patch.result !== undefined) resultRaw = payloadText(patch.result);
      if (patch.error !== undefined) errorRaw = payloadText(patch.error);
      if (patch.status === "ok" || patch.status === "error") runState = patch.status;
      if (typeof patch.expanded === "boolean") expanded = patch.expanded;
      render();
    },
    end() {
      // A block.end without a terminal status is no longer visually active.
      // Keep the state neutral rather than leaving an orange pulsing row behind.
      if (runState === "running") runState = "complete";
      render();
    },
    toggleExpanded,
    relayout() { render(); },
    recolor() { render(); },
  };
}
