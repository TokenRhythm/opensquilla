import { STATUS, THEME } from "../theme.mjs";
import {
  TOOL_INDENT,
  clipToCells,
  stripTerminalControls,
  timelineAvailCells,
  wrapToCells,
} from "../primitives.mjs";
import { destroyRenderable } from "../renderableLifecycle.mjs";

const SUCCESS_STATES = new Set(["complete", "completed", "done", "ok", "success", "succeeded"]);
const ERROR_STATES = new Set(["error", "failed", "failure"]);
const CANCELLED_STATES = new Set(["cancelled", "canceled"]);
const QUEUED_STATES = new Set(["queued", "pending", "waiting"]);

function inline(value) {
  if (value === undefined || value === null) return "";
  const text = typeof value === "string" ? value : JSON.stringify(value);
  return stripTerminalControls(String(text)).replace(/\s+/g, " ").trim();
}

function count(value) {
  if (value === undefined || value === null || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 ? Math.floor(number) : null;
}

function metric(value) {
  if (value === undefined || value === null || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 ? number : null;
}

function status(value, fallback = "queued") {
  return inline(value).toLowerCase() || fallback;
}

function isTerminal(memberStatus) {
  return SUCCESS_STATES.has(memberStatus)
    || ERROR_STATES.has(memberStatus)
    || CANCELLED_STATES.has(memberStatus);
}

function memberGlyph(memberStatus) {
  if (SUCCESS_STATES.has(memberStatus)) return "✓";
  if (ERROR_STATES.has(memberStatus)) return "✗";
  if (CANCELLED_STATES.has(memberStatus)) return "⚠";
  if (QUEUED_STATES.has(memberStatus)) return "○";
  return "◌";
}

function memberColor(memberStatus) {
  if (SUCCESS_STATES.has(memberStatus)) return STATUS.ok;
  if (ERROR_STATES.has(memberStatus)) return STATUS.error;
  if (CANCELLED_STATES.has(memberStatus)) return THEME.warning;
  if (QUEUED_STATES.has(memberStatus)) return STATUS.queued;
  return STATUS.running;
}

function formatElapsed(value) {
  const ms = metric(value);
  if (ms === null) return "";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const seconds = ms / 1000;
  return `${Number.isInteger(seconds) ? seconds.toFixed(0) : seconds.toFixed(1)}s`;
}

function formatCost(value) {
  const cost = metric(value);
  if (cost === null) return "";
  const digits = cost >= 1 ? 2 : cost >= 0.01 ? 3 : 4;
  return `$${cost.toFixed(digits).replace(/\.?0+$/, "")}`;
}

function normalizeMember(raw, id, index) {
  const memberStatus = status(raw?.status);
  return {
    id,
    label: inline(raw?.label) || inline(raw?.id) || `member ${index + 1}`,
    model: inline(raw?.model),
    provider: inline(raw?.provider),
    status: memberStatus,
    elapsed_ms: metric(raw?.elapsed_ms),
    input_tokens: count(raw?.input_tokens),
    output_tokens: count(raw?.output_tokens),
    cost_usd: metric(raw?.cost_usd),
    error: inline(raw?.error),
  };
}

// Ensemble progress is intentionally compact in the transcript: one live row
// updates in place, while Ctrl+O uses the same turn-level details contract as
// reasoning and tools to disclose every member's public execution metadata.
// Candidate answer text and private reasoning are not part of this block.
export function createEnsembleBlock(ctx) {
  const { renderer, TextRenderable, box, idPrefix } = ctx;
  const contentWidth = typeof ctx.contentWidth === "function"
    ? ctx.contentWidth
    : () => renderer.terminalWidth;
  let header = null;
  const detailNodes = [];
  const members = new Map();
  const memberOrder = [];
  let completed = null;
  let total = null;
  let runStatus = "running";
  let summary = "";
  let requestCount = null;
  let fallbackUsed = false;
  let fallbackReason = "";
  let expanded = false;
  let ended = false;
  let hiddenLineCount = 0;

  function mergeMembers(incoming) {
    if (!Array.isArray(incoming)) return;
    incoming.forEach((raw, index) => {
      if (!raw || typeof raw !== "object") return;
      const rawId = inline(raw.id) || inline(raw.label) || `member-${index + 1}`;
      const previous = members.get(rawId) ?? {};
      const merged = normalizeMember({ ...previous, ...raw }, rawId, index);
      if (!members.has(rawId)) memberOrder.push(rawId);
      members.set(rawId, merged);
    });
  }

  function applyPatch(patch = {}) {
    const progress = patch?.progress && typeof patch.progress === "object" && !Array.isArray(patch.progress)
      ? patch.progress
      : {};
    const nextCompleted = patch.completed !== undefined ? patch.completed : progress.completed;
    const nextTotal = patch.total !== undefined ? patch.total : progress.total;
    if (nextCompleted !== undefined) completed = count(nextCompleted);
    if (nextTotal !== undefined) total = count(nextTotal);
    mergeMembers(progress.members);
    mergeMembers(patch.members);
    const nextStatus = patch.status !== undefined ? patch.status : progress.status;
    if (nextStatus !== undefined) runStatus = status(nextStatus, runStatus);
    if (patch.summary !== undefined) summary = inline(patch.summary);
    if (patch.request_count !== undefined) requestCount = count(patch.request_count);
    if (patch.fallback_used !== undefined) fallbackUsed = Boolean(patch.fallback_used);
    if (patch.fallback_reason !== undefined) fallbackReason = inline(patch.fallback_reason);
  }

  function completedCount() {
    if (completed !== null) return completed;
    let value = 0;
    for (const member of members.values()) if (isTerminal(member.status)) value += 1;
    return value;
  }

  function totalCount() {
    const done = completedCount();
    // `total` counts proposer candidates. A final receipt may additionally
    // disclose the aggregator as a member row; that must not turn a truthful
    // 3/3 candidate result into 3/4 merely because details gained a judge row.
    if (total !== null) return Math.max(total, done);
    return Math.max(memberOrder.length, done);
  }

  function runDone() {
    return ended || SUCCESS_STATES.has(runStatus) || ERROR_STATES.has(runStatus) || CANCELLED_STATES.has(runStatus);
  }

  function headerGlyph() {
    if (ERROR_STATES.has(runStatus)) return "✗";
    if (CANCELLED_STATES.has(runStatus) || fallbackUsed) return "⚠";
    if (runDone()) return "✓";
    if (QUEUED_STATES.has(runStatus)) return "○";
    return "◌";
  }

  function headerColor() {
    if (ERROR_STATES.has(runStatus)) return STATUS.error;
    if (CANCELLED_STATES.has(runStatus) || fallbackUsed) return THEME.warning;
    if (runDone()) return STATUS.ok;
    if (QUEUED_STATES.has(runStatus)) return STATUS.queued;
    return STATUS.running;
  }

  function headerContent() {
    const base = `${TOOL_INDENT}${headerGlyph()} Ensemble · ${completedCount()}/${totalCount()} complete`;
    const tails = [];
    if (runDone() && summary) tails.push(summary);
    if (runDone() && requestCount !== null) tails.push(`${requestCount} requests`);
    if (fallbackUsed) tails.push("fallback");
    const full = tails.length ? `${base} · ${tails.join(" · ")}` : base;
    return clipToCells(full, Math.max(12, (Number(contentWidth()) || 80) - 2));
  }

  function memberDescription(member) {
    const fields = [member.label];
    if (member.model) fields.push(member.model);
    if (member.provider) fields.push(member.provider);
    fields.push(member.status);
    const elapsed = formatElapsed(member.elapsed_ms);
    if (elapsed) fields.push(elapsed);
    if (member.input_tokens !== null || member.output_tokens !== null) {
      fields.push(`in ${member.input_tokens ?? 0} / out ${member.output_tokens ?? 0} tok`);
    }
    const cost = formatCost(member.cost_usd);
    if (cost) fields.push(cost);
    return `${memberGlyph(member.status)} ${fields.join(" · ")}`;
  }

  function detailEntries() {
    const prefix = `${TOOL_INDENT}├ `;
    const budget = timelineAvailCells(prefix, contentWidth());
    const entries = [];
    for (const id of memberOrder) {
      const member = members.get(id);
      if (!member) continue;
      const rows = wrapToCells(memberDescription(member), budget);
      rows.forEach((row, index) => entries.push({
        text: index === 0 ? row : `  ${row}`,
        fg: memberColor(member.status),
      }));
      if (member.error) {
        const errorRows = wrapToCells(`error · ${member.error}`, budget - 2);
        errorRows.forEach((row) => entries.push({ text: `  ${row}`, fg: STATUS.detailError }));
      }
    }
    if (fallbackReason) {
      for (const row of wrapToCells(`fallback · ${fallbackReason}`, budget)) {
        entries.push({ text: row, fg: THEME.warning });
      }
    }
    return entries;
  }

  function detailSpecs() {
    const entries = detailEntries();
    hiddenLineCount = expanded ? 0 : entries.length;
    if (!expanded) return [];
    const specs = entries.map((entry, index) => ({
      content: `${TOOL_INDENT}${index === entries.length - 1 ? "└ " : "├ "}${entry.text}`,
      fg: entry.fg,
    }));
    if (specs.length) {
      specs.push({ content: `${TOOL_INDENT}  ▾ collapse details`, fg: THEME.muted });
    }
    return specs;
  }

  function insertAfter(node, previous) {
    const children = box.getChildren?.() ?? [];
    const index = previous ? children.indexOf(previous) : -1;
    box.add(node, index >= 0 ? index + 1 : undefined);
  }

  function render() {
    if (!header) {
      header = new TextRenderable(renderer, {
        id: `${idPrefix}-header`,
        content: headerContent(),
        fg: headerColor(),
      });
      box.add(header);
    }
    header.content = headerContent();
    header.fg = headerColor();
    const specs = detailSpecs();
    while (detailNodes.length > specs.length) {
      destroyRenderable(box, detailNodes.pop());
    }
    while (detailNodes.length < specs.length) {
      const index = detailNodes.length;
      const node = new TextRenderable(renderer, {
        id: `${idPrefix}-detail-${index}`,
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
    get node() { return header; },
    get rawText() { return summary; },
    get isRunning() { return !runDone(); },
    get isExpanded() { return expanded; },
    get hiddenLineCount() { return hiddenLineCount; },
    begin(meta = {}) {
      applyPatch(meta);
      render();
    },
    append(delta) {
      summary = inline(`${summary}${String(delta ?? "")}`);
      render();
    },
    update(patch = {}) {
      applyPatch(patch);
      if (typeof patch.expanded === "boolean") expanded = patch.expanded;
      render();
    },
    end() {
      ended = true;
      if (!runDone() || runStatus === "running") runStatus = "complete";
      render();
    },
    toggleExpanded,
    relayout() { render(); },
    recolor() { render(); },
  };
}
