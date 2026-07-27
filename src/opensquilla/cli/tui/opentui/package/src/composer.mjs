import { THEME, THEME_NAMES, applyTheme, activeThemeName } from "./theme.mjs";
import { cellWidth, clampFooterHeight, clipToCells, stripTerminalControls, textWidth, wrapToCells } from "./primitives.mjs";
import {
  compactContextItems,
  emptyContextState,
  normalizeContextUpdate,
} from "./contextView.mjs";
import { destroyChildren, destroyRenderable } from "./renderableLifecycle.mjs";
import { rendererViewportSnapshot } from "./screenMode.mjs";

const COMPLETION_MENU_LEFT = 1;
const COMPLETION_MENU_RIGHT = 34;
const COMPLETION_MENU_CHROME_CELLS = 4; // left/right border plus left/right padding
const MIN_COMPLETION_ROW_CELLS = 16;
const COMPOSER_LEFT = 1;
const COMPOSER_CONTENT_LEFT = COMPOSER_LEFT + 2; // border plus left padding
// The composer box sits one row below the top of the footer (the router status
// strip occupies that first row), so its first content row is 2 below footerTop:
// the strip row (0) + the composer box's own top border (1).
const COMPOSER_CONTENT_TOP_OFFSET = 2;

// Pure key handling for the theme picker overlay (modeled on menuKeyAction):
// up/down preview the highlighted theme live, enter keeps it, escape reverts.
export function themePickerKeyAction(picker, keyName) {
  if (!picker?.active) return { handled: false, action: "pass", selected: 0 };
  const max = Math.max(0, (picker.names?.length ?? 0) - 1);
  const sel = clamp(Number(picker.selected) || 0, 0, max);
  if (keyName === "up") return { handled: true, action: "preview", selected: clamp(sel - 1, 0, max) };
  if (keyName === "down") return { handled: true, action: "preview", selected: clamp(sel + 1, 0, max) };
  if (keyName === "return" || keyName === "tab") return { handled: true, action: "confirm", selected: sel };
  if (keyName === "escape") return { handled: true, action: "cancel", selected: sel };
  // Modal: swallow every other key so it never leaks into the input while open.
  return { handled: true, action: "none", selected: sel };
}

// Router and Ensemble are views over one Gateway-owned three-state strategy.
// The picker does not preview writes: only Enter commits shared state.
export function routingPickerKeyAction(picker, keyName) {
  if (!picker?.active) return { handled: false, action: "pass", selected: 0 };
  const max = Math.max(0, (picker.options?.length ?? 0) - 1);
  const sel = clamp(Number(picker.selected) || 0, 0, max);
  if (keyName === "up") return { handled: true, action: "navigate", selected: clamp(sel - 1, 0, max) };
  if (keyName === "down") return { handled: true, action: "navigate", selected: clamp(sel + 1, 0, max) };
  if (keyName === "return" || keyName === "tab") return { handled: true, action: "confirm", selected: sel };
  if (keyName === "escape") return { handled: true, action: "cancel", selected: sel };
  return { handled: true, action: "none", selected: sel };
}

// A long command or rationale wraps instead of clipping — the user is being
// asked to authorize exactly this text, so hiding it is a trust problem. But
// a modal must stay a modal: each section is capped so a multi-kilobyte
// summary cannot grow without bound.
export const APPROVAL_SUMMARY_MAX_ROWS = 6;
export const APPROVAL_MESSAGE_MAX_ROWS = 4;

// Row allocation for the approval overlay, in priority order: the tool line
// and every choice are immovable; then ONE summary row — the user is
// authorizing exactly that text, so it must never be fully invisible while
// any flexible row remains; then the key hint (y/n/esc still work without
// it); then the rest of the summary; then the rationale message. Needs are
// the ACTUAL wrapped row counts, so a one-line summary never reserves rows
// the message could have used.
export function approvalRowPlan(maxRows, choicesCount, summaryNeed, messageNeed) {
  let budget = Math.max(0, maxRows - 2 - 1 - choicesCount);
  const summaryTarget = Math.min(Math.max(0, summaryNeed), APPROVAL_SUMMARY_MAX_ROWS);
  const messageTarget = Math.min(Math.max(0, messageNeed), APPROVAL_MESSAGE_MAX_ROWS);
  const summaryMin = summaryTarget > 0 && budget > 0 ? 1 : 0;
  budget -= summaryMin;
  const hint = budget > 0;
  if (hint) budget -= 1;
  const summaryExtra = Math.max(0, Math.min(summaryTarget - summaryMin, budget));
  budget -= summaryExtra;
  const messageRows = Math.max(0, Math.min(messageTarget, budget));
  return { summaryRows: summaryMin + summaryExtra, messageRows, hint };
}

// Pure key handling for the tool-approval overlay. Modal like the theme picker
// — every key is swallowed while it is open — with one deliberate exception:
// Ctrl+C passes through so the interrupt path (clear input / cancel the turn)
// is never trapped behind a pending approval. Keys: y approves, n/Escape
// denies; with choices, Up/Down move the highlight and Enter confirms it.
// Only BARE keys decide: modified chords (Ctrl+Y is the composer's yank,
// Alt+N …) are swallowed like any other non-decision key, so a chord queued
// just as the overlay pops can never approve a gated tool unread.
export function approvalKeyAction(overlay, key) {
  if (!overlay?.active) return { handled: false, action: "pass", selected: 0 };
  const max = Math.max(0, (overlay.choices?.length ?? 0) - 1);
  const sel = clamp(Number(overlay.selected) || 0, 0, max);
  const hasChoices = (overlay.choices?.length ?? 0) > 0;
  if (key?.ctrl && key?.name === "c") return { handled: false, action: "pass", selected: sel };
  if (key?.ctrl || key?.meta || key?.alt || key?.option) {
    return { handled: true, action: "none", selected: sel };
  }
  const name = key?.name;
  if (name === "y") return { handled: true, action: "approve", selected: sel };
  if (name === "n" || name === "escape") return { handled: true, action: "deny", selected: sel };
  if (hasChoices && name === "up") return { handled: true, action: "navigate", selected: clamp(sel - 1, 0, max) };
  if (hasChoices && name === "down") return { handled: true, action: "navigate", selected: clamp(sel + 1, 0, max) };
  if (name === "return") {
    if (hasChoices) return { handled: true, action: "choose", selected: sel };
    return { handled: true, action: "approve", selected: sel };
  }
  return { handled: true, action: "none", selected: sel };
}

// Last path segment of a model id ("vendor/big-model" -> "big-model").
export function shortModel(m) {
  return m ? m.split("/").pop() : m;
}

// Router model row value. On downgrade, keep the resolved target model visible;
// the source/baseline model is already represented by the down marker.
export function formatRouterModelValue(model, baselineModel) {
  const modelShort = shortModel(model);
  const baselineShort = shortModel(baselineModel);
  if (baselineShort && modelShort && baselineShort !== modelShort) {
    return `↓ ${modelShort}`;
  }
  return modelShort || model;
}

// Router strip value cell: whitespace collapsed and clipped to a fixed 18
// cells so one long model id cannot push the other fields off the strip row.
export function routerStripValue(value) {
  return clipToCells(String(value ?? "").replace(/\s+/gu, " ").trim() || "-", 18);
}

const ATTACHMENT_STATUS = new Set(["reading", "uploading", "ready", "failed"]);
const ATTACHMENT_ICON = { reading: "◌", uploading: "⇡", ready: "✓", failed: "✗" };

function normalizedAttachment(message, previous = null) {
  const status = ATTACHMENT_STATUS.has(String(message?.status ?? ""))
    ? String(message.status)
    : previous?.status ?? "failed";
  return {
    id: String(message?.id ?? previous?.id ?? ""),
    kind: stripTerminalControls(String(message?.kind ?? previous?.kind ?? "file")),
    label: stripTerminalControls(String(message?.label ?? previous?.label ?? "attachment")),
    status,
    message: stripTerminalControls(String(message?.message ?? "")),
  };
}

export function attachmentChipText(attachment) {
  const item = normalizedAttachment(attachment);
  const detail = item.status === "failed" && item.message ? ` · ${item.message}` : "";
  return `[${ATTACHMENT_ICON[item.status]} ${item.kind} ${item.label}${detail}]`;
}

export function attachmentSubmitBlocked(attachments) {
  return Array.from(attachments ?? []).some((item) =>
    item?.status === "reading" || item?.status === "uploading");
}

function clamp(value, min, max) {
  if (max < min) return min;
  return Math.max(min, Math.min(max, value));
}

export function tokenUnderCaret(text, cursorPos) {
  const chars = Array.from(String(text ?? ""));
  const pos = clamp(Number(cursorPos) || 0, 0, chars.length);
  let start = pos;
  while (start > 0 && !/\s/u.test(chars[start - 1])) start -= 1;
  return { token: chars.slice(start, pos).join(""), start };
}

function lineStartForToken(text, start) {
  const chars = Array.from(String(text ?? ""));
  const pos = clamp(Number(start) || 0, 0, chars.length);
  return pos === 0 || chars[pos - 1] === "\n";
}

export function shouldTriggerMenu(token, start, lineStart) {
  const value = String(token ?? "");
  if (value.startsWith("/") && lineStart) {
    return { active: true, kind: "slash", query: value.slice(1) };
  }
  if (value.startsWith("@")) {
    return { active: true, kind: "file", query: value.slice(1) };
  }
  return { active: false, kind: null, query: "" };
}

// Resolve the first argument of a known slash command into structured choice
// rows.  The Python catalog owns the command grammar; the host only projects
// it into the same completion menu used for command names.  Keeping this pure
// also makes the exact "command + space" transition independently testable.
export function slashArgumentContext(text, cursorPos, catalog) {
  const chars = Array.from(String(text ?? ""));
  const pos = clamp(Number(cursorPos) || 0, 0, chars.length);
  let lineStart = pos;
  while (lineStart > 0 && chars[lineStart - 1] !== "\n") lineStart -= 1;
  const line = chars.slice(lineStart, pos).join("");
  const match = line.match(/^(\/\S+)\s+(\S*)$/u);
  if (!match) return null;

  const head = match[1].toLocaleLowerCase();
  const query = match[2];
  const command = (Array.isArray(catalog) ? catalog : []).find((item) => {
    const words = [item?.label, ...(Array.isArray(item?.aliases) ? item.aliases : [])];
    return words.some((word) => String(word ?? "").toLocaleLowerCase() === head);
  });
  const choices = command?.argument_choices ?? command?.argumentChoices;
  if (!Array.isArray(choices) || choices.length === 0) return null;

  return {
    query,
    tokenStart: pos - Array.from(query).length,
    items: choices.map((choice) => ({
      label: String(choice?.value ?? ""),
      description: String(choice?.description ?? ""),
      insert_text: `${String(choice?.value ?? "")} `,
      category: "argument",
      visible_by_default: true,
      deprecated: false,
      submit_behavior: "submit",
      parent_command: String(command?.label ?? ""),
    })).filter((item) => item.label),
  };
}

function subsequencePositions(query, text) {
  const positions = [];
  let from = 0;
  for (const char of Array.from(query)) {
    const index = text.indexOf(char, from);
    if (index < 0) return null;
    positions.push(index);
    from = index + 1;
  }
  return positions;
}

function pathSegments(text) {
  return String(text ?? "")
    .replaceAll("\\", "/")
    .split(/[\/._\-\s]+/u)
    .filter(Boolean);
}

function isSegmentStart(text, position) {
  return position === 0 || "/\\._- ".includes(text[position - 1]);
}

function fuzzyScore(query, candidate) {
  const q = String(query ?? "").toLocaleLowerCase();
  const text = String(candidate ?? "").toLocaleLowerCase();
  if (!q) return 0;
  const positions = subsequencePositions(q, text);
  if (!positions) return null;

  let score = q.length * 100;
  if (text.startsWith(q)) score += 80;
  const segments = pathSegments(text);
  const commandSegment = text.startsWith("/") ? segments[0] : null;
  if (commandSegment?.startsWith(q)) score += 90;
  const prefixSegment = segments.find((segment) => segment.startsWith(q));
  if (prefixSegment) {
    score += 60;
    score += Math.max(0, 24 - prefixSegment.length * 2);
  }

  let runLength = 1;
  let longestRun = 1;
  for (let i = 1; i < positions.length; i += 1) {
    if (positions[i] === positions[i - 1] + 1) {
      runLength += 1;
      longestRun = Math.max(longestRun, runLength);
    } else {
      runLength = 1;
    }
  }
  score += longestRun * longestRun * 8;

  for (const position of positions) {
    if (isSegmentStart(text, position)) score += 18;
  }
  score += Math.max(0, 30 - positions[0] * 0.75);
  score += Math.max(0, 18 - String(candidate ?? "").length * 0.35);
  return score;
}

export function filterCatalog(catalog, query) {
  const items = Array.isArray(catalog) ? catalog : [];
  const q = String(query ?? "");
  if (!q) {
    return items.filter((item) =>
      item?.visible_by_default !== false && item?.deprecated !== true);
  }
  return items
    .map((item, index) => {
      const words = [
        String(item?.label ?? ""),
        ...(Array.isArray(item?.aliases) ? item.aliases.map(String) : []),
      ];
      const scores = words
        .map((word) => fuzzyScore(q, word))
        .filter((score) => score !== null);
      return {
        item,
        index,
        score: scores.length > 0 ? Math.max(...scores) : null,
      };
    })
    .filter((entry) => entry.score !== null)
    .sort((a, b) => (b.score - a.score) || (a.index - b.index))
    .map((entry) => entry.item);
}

export function acceptCompletionText(text, tokenStart, cursorPos, insertText) {
  const chars = Array.from(String(text ?? ""));
  const start = clamp(Number(tokenStart) || 0, 0, chars.length);
  const cursor = clamp(Number(cursorPos) || 0, start, chars.length);
  const insertChars = Array.from(String(insertText ?? ""));
  const nextText = [
    ...chars.slice(0, start),
    ...insertChars,
    ...chars.slice(cursor),
  ].join("");
  return { text: nextText, cursor: start + insertChars.length };
}

export function shouldDropResponse(responseReqId, currentSeq) {
  return Number(responseReqId) !== Number(currentSeq);
}

// Index of the start of the line the caret is on (just after the previous "\n",
// or 0). Powers Ctrl+A and the start of the Ctrl+U cut.
export function lineStartIndex(text, pos) {
  const chars = Array.from(String(text ?? ""));
  let i = clamp(Number(pos) || 0, 0, chars.length);
  while (i > 0 && chars[i - 1] !== "\n") i -= 1;
  return i;
}

// Index of the end of the line the caret is on (the next "\n", or end of text).
// Powers Ctrl+E and the end of the Ctrl+K cut.
export function lineEndIndex(text, pos) {
  const chars = Array.from(String(text ?? ""));
  let i = clamp(Number(pos) || 0, 0, chars.length);
  while (i < chars.length && chars[i] !== "\n") i += 1;
  return i;
}

// Start of the whitespace-delimited word before the caret (skip trailing
// whitespace, then the word). Powers Ctrl+W / Alt+Backspace and Ctrl+Left.
export function wordStartIndex(text, pos) {
  const chars = Array.from(String(text ?? ""));
  let i = clamp(Number(pos) || 0, 0, chars.length);
  while (i > 0 && /\s/u.test(chars[i - 1])) i -= 1;
  while (i > 0 && !/\s/u.test(chars[i - 1])) i -= 1;
  return i;
}

// End of the whitespace-delimited word after the caret (skip leading whitespace,
// then the word). Powers Ctrl+Right / Alt+F.
export function wordEndIndex(text, pos) {
  const chars = Array.from(String(text ?? ""));
  let i = clamp(Number(pos) || 0, 0, chars.length);
  while (i < chars.length && /\s/u.test(chars[i])) i += 1;
  while (i < chars.length && !/\s/u.test(chars[i])) i += 1;
  return i;
}

// Remove the [from, to) code-point range; returns the new text and the caret
// position (collapsed to the cut start).
export function spliceOut(text, from, to) {
  const chars = Array.from(String(text ?? ""));
  const a = clamp(Math.min(from, to), 0, chars.length);
  const b = clamp(Math.max(from, to), 0, chars.length);
  return { text: [...chars.slice(0, a), ...chars.slice(b)].join(""), cursor: a };
}

// Grapheme-cluster boundaries around a code-point position, so backspace,
// delete, and left/right arrows treat multi-code-point clusters (emoji
// families, flags, composed accents) as one unit instead of mutating them
// component by component. Positions stay code-point indices into
// Array.from(text); only the STEP size is cluster-aware.
const graphemeSegmenter = new Intl.Segmenter(undefined, { granularity: "grapheme" });

export function graphemeBoundaryBefore(text, pos) {
  const chars = Array.from(String(text ?? ""));
  const p = clamp(Number(pos) || 0, 0, chars.length);
  if (p === 0) return 0;
  let last = null;
  for (const seg of graphemeSegmenter.segment(chars.slice(0, p).join(""))) last = seg.segment;
  return last ? p - Array.from(last).length : p - 1;
}

export function graphemeBoundaryAfter(text, pos) {
  const chars = Array.from(String(text ?? ""));
  const p = clamp(Number(pos) || 0, 0, chars.length);
  if (p >= chars.length) return chars.length;
  for (const seg of graphemeSegmenter.segment(chars.slice(p).join(""))) {
    return p + Array.from(seg.segment).length;
  }
  return p + 1;
}

// Word-wrap one logical line (an array of {ch, caret} entries) to `width`
// display cells: break after the last space that fits, hard-break a word
// longer than a whole row. Every entry survives the wrap (spaces carry over
// to the next row instead of being dropped) so each code point keeps exactly
// one screen cell for the caret math to land on. cellWidth takes the next
// code point too, so a VS16 emoji-presentation pair counts its 2 cells.
function wrapLineEntries(entries, width) {
  const rows = [];
  let current = [];
  let currentWidth = 0;
  let breakAt = -1; // index in `current` just AFTER the last space (wrap point)
  for (let e = 0; e < entries.length; e += 1) {
    const entry = entries[e];
    const w = cellWidth(entry.ch, entries[e + 1]?.ch);
    while (current.length > 0 && currentWidth + w > width) {
      if (breakAt > 0 && breakAt < current.length) {
        rows.push(current.slice(0, breakAt));
        current = current.slice(breakAt);
      } else {
        rows.push(current);
        current = [];
      }
      currentWidth = 0;
      breakAt = -1;
      for (let i = 0; i < current.length; i += 1) {
        currentWidth += cellWidth(current[i].ch, (current[i + 1] ?? entry).ch);
        if (current[i].ch === " ") breakAt = i + 1;
      }
    }
    current.push(entry);
    currentWidth += w;
    if (entry.ch === " ") breakAt = current.length;
  }
  rows.push(current);
  return rows;
}

// Lay out the composer content: word-wrap the input (with the caret's own
// blank cell spliced in, exactly as it renders) to `contentWidth` cells,
// locate the caret's wrapped row/column, and window the rows so the caret is
// always inside the `contentRows`-row content area. This single model feeds
// both the rendered lines and the hardware-cursor position, so the visible
// caret (and the IME anchor) can never detach from the real caret.
export function composerLayout(text, caretPos, contentWidth, contentRows) {
  const width = Math.max(1, Math.floor(Number(contentWidth) || 1));
  const rowsBudget = Math.max(1, Math.floor(Number(contentRows) || 1));
  const chars = Array.from(String(text ?? ""));
  const pos = clamp(Number(caretPos) || 0, 0, chars.length);
  const entries = [
    ...chars.slice(0, pos).map((ch) => ({ ch, caret: false })),
    { ch: " ", caret: true },
    ...chars.slice(pos).map((ch) => ({ ch, caret: false })),
  ];
  const rows = [];
  let logical = [];
  for (const entry of entries) {
    if (entry.ch === "\n") {
      rows.push(...wrapLineEntries(logical, width));
      logical = [];
    } else {
      logical.push(entry);
    }
  }
  rows.push(...wrapLineEntries(logical, width));

  let caretRow = 0;
  let caretCol = 0;
  for (let row = 0; row < rows.length; row += 1) {
    const rowEntries = rows[row];
    let col = 0;
    let found = false;
    for (let i = 0; i < rowEntries.length; i += 1) {
      if (rowEntries[i].caret) {
        found = true;
        break;
      }
      col += cellWidth(rowEntries[i].ch, rowEntries[i + 1]?.ch);
    }
    if (found) {
      caretRow = row;
      caretCol = col;
      break;
    }
  }

  const totalRows = rows.length;
  const scrollRowOffset = clamp(caretRow - rowsBudget + 1, 0, Math.max(0, totalRows - rowsBudget));
  const visibleLines = rows
    .slice(scrollRowOffset, scrollRowOffset + rowsBudget)
    .map((row) => row.map((entry) => entry.ch).join(""));
  return { visibleLines, caretRow, caretCol, scrollRowOffset, totalRows };
}

export function menuKeyAction(menu, keyName) {
  if (!menu?.active) return { handled: false, action: "pass", menu };
  const selected = Number(menu.selected) || 0;
  const maxSelected = Math.max(0, (menu.filtered?.length ?? 0) - 1);
  const empty = (menu.filtered?.length ?? 0) === 0;
  if (keyName === "up" || keyName === "down") {
    // Nothing to navigate while the menu shows "no matches": let Up/Down fall
    // through so caret movement and history recall keep working (mirroring the
    // empty-list Enter fall-through below).
    if (empty) return { handled: false, action: "pass", menu };
    const step = keyName === "up" ? -1 : 1;
    return {
      handled: true,
      action: "navigate",
      menu: { ...menu, selected: clamp(selected + step, 0, maxSelected) },
    };
  }
  if (keyName === "escape") {
    return { handled: true, action: "close", menu: { ...menu, active: false } };
  }
  if (keyName === "return" || keyName === "tab") {
    // Nothing to accept (zero matches): Enter must still SUBMIT the message
    // (fall through, don't swallow it); Tab just closes the menu with no insert.
    if (empty) {
      if (keyName === "return") return { handled: false, action: "pass", menu: { ...menu, active: false } };
      return { handled: true, action: "close", menu: { ...menu, active: false } };
    }
    // Enter on a slash command RUNS it (accept + submit) in one keystroke instead
    // of just inserting it and waiting for a second Enter. Tab completes it so you
    // can still type arguments (e.g. `/theme dark`). File completions only insert
    // the path — Enter there keeps composing the message, never submits it.
    if (keyName === "return" && menu.kind === "slash") {
      const item = menu.filtered[clamp(selected, 0, maxSelected)];
      // Structured metadata decides whether Enter is a one-step command submit
      // or only a completion.  Required-argument commands and skill prompt
      // prefixes use "complete" so Enter cannot send an incomplete operation.
      if (
        String(item?.submit_behavior ?? "") === "complete"
        || String(item?.category ?? "") === "skill"
      ) {
        return { handled: true, action: "accept", menu };
      }
      return { handled: true, action: "accept_submit", menu };
    }
    return { handled: true, action: "accept", menu };
  }
  return { handled: false, action: "pass", menu };
}

function fileCompletionItems(paths) {
  return (Array.isArray(paths) ? paths : []).map((path) => ({
    label: String(path),
    // The label already IS the path; a description would render it twice on an
    // already width-clipped menu row.
    description: "",
    insert_text: `@${path} `,
    category: "file",
  }));
}

// Factory for the composer / input-region. All state that main.mjs previously
// held as module-level globals lives here as closure state; the rendering deps
// (renderer, renderable classes, boxes, footer height, host writer) are injected
// via `deps`.
export function createComposer(deps) {
  const {
    renderer,
    BoxRenderable,
    TextRenderable,
    conversationBox,
    inputBox,
    overlayLayer,
    footerHeight,
    viewport = () => rendererViewportSnapshot(renderer),
    sendHostMessage,
    onContextUpdate,
    onRouterUpdate,
    onFullRedraw,
    onJumpToLatest,
    onTranscriptScroll,
    isTranscriptHeld,
  } = deps;

  // `footerHeight` is the DESIRED footer height (e.g. 6). On a very short
  // terminal main.mjs clamps the actual inputBox height with clampFooterHeight,
  // so the composer must lay out (and place the caret / overlays) against the
  // same clamped value or it overflows a 3–5 row pane. Recomputed each use so it
  // tracks live resizes.
  const viewportHeight = () => viewport().height;
  const effFooterHeight = () => clampFooterHeight(footerHeight, viewportHeight());

  // inputBox.right is the single source of truth for space reserved by the
  // optional context rail. Composer wrapping, overlays, and the hardware caret
  // all use the same effective surface so none can paint beneath the rail.
  function terminalWidth() {
    return viewport().width;
  }

  function footerRightInset() {
    return clamp(Number(inputBox?.right) || 0, 0, Math.max(0, terminalWidth() - 1));
  }

  function footerSurfaceWidth() {
    return Math.max(1, terminalWidth() - footerRightInset());
  }

  let inputText = "";
  // Caret position as a code-point index into Array.from(inputText), range
  // [0, len]. Backspace/Delete and Left/Right step by grapheme cluster (see
  // graphemeBoundaryBefore/After) so multi-code-point emoji edit atomically.
  let cursorPos = 0;
  // Goal column for vertical (Up/Down) motion, measured in DISPLAY CELLS so the
  // caret tracks the same visual column across lines that mix narrow and wide
  // (CJK) glyphs. Preserved across consecutive vertical moves — passing through a
  // short line keeps the original column — and reset to null by any other caret
  // motion (see the keypress handler). null means "recompute from the caret".
  let desiredVisualCol = null;
  // Input history (newest last). historyIndex === history.length means "current
  // draft" (not browsing history); 0..length-1 selects a recalled entry.
  const inputHistory = [];
  // Initialize to the sentinel (== length) meaning "on the current draft, not
  // browsing history". With empty history this is 0, but the semantics are correct.
  let historyIndex = inputHistory.length;
  let draftBeforeHistory = "";
  // Set when an edit detached from history browsing: the input then holds an
  // edited RECALL, not the draft, so re-entering browse must not overwrite a
  // saved draft with it (the draft would be unrecoverable).
  let historyEditedRecall = false;
  // Busy-turn input stays editable. Enter is an explicit steer request while
  // Tab is an explicit FIFO follow-up; Python/Gateway remain authoritative and
  // may downgrade a late steer to queue with a visible notice.
  let turnActive = false;
  // Last text removed by a kill command (Ctrl+U/K/W, Alt+Backspace, Alt+D);
  // Ctrl+Y yanks it back so a mistyped kill is recoverable.
  let killBuffer = "";
  // Trigger token dismissed with Escape: the menu stays closed while the caret
  // remains on that same token, instead of reopening on the next keystroke.
  let menuDismissed = null;
  // Guard against install() binding the keypress/paste listeners more than once.
  let installed = false;

  const composer = {
    placeholder: "send a message",
    text: "",
    disabled: false,
  };

  const routerState = {
    model: "pending",
    route: "pending",
    saving: "pending",
    context: "pending",
    style: "dim",
    baselineModel: "",
    source: "",
    routingApplied: true,
    rolloutPhase: "full",
    io: "", // last turn's in/out token pair ("34.6k/548"); empty before any turn
  };

  const modelRoutingState = {
    current: "direct",
    next: null,
    busy: false,
  };

  // Canonical task/agent context arrives through the additive context.update
  // frame. It intentionally starts empty: older Python parents keep the proven
  // router-only strip, while a newer parent progressively enriches the same UI.
  let contextState = emptyContextState();

  function notifyContextObserver() {
    try { onContextUpdate?.({ ...contextState }); } catch { /* presentation observers are non-fatal */ }
  }

  function notifyRouterObserver() {
    try { onRouterUpdate?.({ ...routerState }); } catch { /* presentation observers are non-fatal */ }
  }

  const completionContext = {
    catalog: [],
    files: [],
    filtersSensitivePaths: true,
  };
  const attachments = new Map();
  const menu = {
    active: false,
    kind: null,
    query: "",
    tokenStart: 0,
    filtered: [],
    selected: 0,
    requestSeq: 0,
  };
  let fileDebounce = null;

  function startCursorBlink() {
    // Intentionally a no-op. The caret is the real hardware terminal cursor now
    // (caretGlyph renders a blank cell; syncTerminalCursorToCaret shows it via
    // setCursorPosition), and the terminal blinks it natively. The old 530ms
    // self-render re-asserted setCursorPosition on every tick — during macOS IME
    // composition the app gets no keystrokes, so that timer was the ONLY thing
    // re-rendering, and its repeated cursor re-assertion disrupted the terminal's
    // marked-text handling and corrupted the router panel's first row while
    // typing. Removing the timer fixes that; nothing else needs the 530ms redraw.
  }

  function routerModelValue() {
    return formatRouterModelValue(routerState.model, routerState.baselineModel);
  }

  function modelStrategyLabel() {
    const mode = modelRoutingState.next ?? modelRoutingState.current;
    return `${modelRoutingState.next ? "next " : ""}${mode}${modelRoutingState.busy ? " …" : ""}`;
  }

  function modelStrategyColor() {
    if (modelRoutingState.next || modelRoutingState.busy) return THEME.warning;
    if (modelRoutingState.current === "ensemble") return THEME.metricPositive;
    if (modelRoutingState.current === "router") return THEME.routeText;
    return THEME.text;
  }

  // Router route row: tag the route when it is not a normal applied decision so
  // forced/observe/fallback states are distinguishable. A normal route adds no
  // marker to keep the panel quiet.
  function routerRouteValue() {
    const route = routerState.route;
    const source = routerState.source;
    if (routerState.rolloutPhase === "observe" || routerState.routingApplied === false) {
      return `${route} observe`;
    }
    if (source === "forced" || source === "observe" || source === "fallback") {
      return `${route} ·${source}`;
    }
    return route;
  }

  // The caret is now the REAL hardware terminal cursor (positioned + shown via
  // syncTerminalCursorToCaret with visible:true), which is what macOS IME anchors
  // its candidate popover to. So the composer no longer paints its own caret —
  // doing both would render two carets. Keep a blank cell here so the input line
  // layout is unchanged and the hardware cursor sits on an empty cell.
  function caretGlyph() {
    return " ";
  }

  function resetMenu() {
    menu.active = false;
    menu.kind = null;
    menu.query = "";
    menu.tokenStart = 0;
    menu.filtered = [];
    menu.selected = 0;
    if (fileDebounce) {
      clearTimeout(fileDebounce);
      fileDebounce = null;
    }
    clearOverlay();
  }

  function scheduleFileCompletionRequest(query) {
    if (fileDebounce) clearTimeout(fileDebounce);
    const requestId = menu.requestSeq + 1;
    menu.requestSeq = requestId;
    fileDebounce = setTimeout(() => {
      fileDebounce = null;
      if (!menu.active || menu.kind !== "file" || menu.requestSeq !== requestId) return;
      sendHostMessage({
        type: "completion.request",
        kind: "file",
        query,
        request_id: requestId,
      });
    }, 120);
    fileDebounce.unref?.();
  }

  // Keep the highlight on the same candidate when the list is replaced (an
  // async completion.response landing, or a re-filter while typing); fall back
  // to the top instead of whatever item now happens to sit at the old index.
  function reanchorMenuSelection(previous) {
    if (!previous) {
      menu.selected = 0;
      return;
    }
    const index = menu.filtered.findIndex(
      (item) => String(item?.label ?? "") === String(previous.label ?? ""),
    );
    menu.selected = index >= 0 ? index : 0;
  }

  // Rows available above the footer for the menu box (its borders included).
  function completionMenuMaxRows() {
    return Math.max(0, viewportHeight() - effFooterHeight());
  }

  function updateMenuFromInput() {
    const { token, start } = tokenUnderCaret(inputText, cursorPos);
    const argumentContext = slashArgumentContext(
      inputText,
      cursorPos,
      completionContext.catalog,
    );
    const trigger = argumentContext
      ? { active: true, kind: "slash", query: argumentContext.query }
      : shouldTriggerMenu(token, start, lineStartForToken(inputText, start));
    const tokenStart = argumentContext?.tokenStart ?? start;
    if (!trigger.active) {
      menuDismissed = null; // leaving the token ends an Escape dismissal
      resetMenu();
      return;
    }
    // Escape dismissed the menu for this exact token: keep it closed until the
    // token (or trigger kind) changes, instead of reopening on the next key.
    if (
      menuDismissed
      && menuDismissed.kind === trigger.kind
      && menuDismissed.tokenStart === tokenStart
    ) {
      resetMenu();
      return;
    }
    menuDismissed = null;
    // A menu that cannot show at least one candidate row above the footer would
    // be an invisible modal eating arrow keys — fall back to plain typing.
    if (completionMenuMaxRows() < 3) {
      resetMenu();
      return;
    }

    const previous = menu.active && menu.kind === trigger.kind
      ? menu.filtered[menu.selected]
      : null;
    menu.active = true;
    menu.kind = trigger.kind;
    menu.query = trigger.query;
    menu.tokenStart = tokenStart;
    if (menu.kind === "slash") {
      if (fileDebounce) {
        clearTimeout(fileDebounce);
        fileDebounce = null;
      }
      menu.filtered = filterCatalog(
        argumentContext?.items ?? completionContext.catalog,
        menu.query,
      );
    } else {
      menu.filtered = filterCatalog(fileCompletionItems(completionContext.files), menu.query);
      scheduleFileCompletionRequest(menu.query);
    }
    reanchorMenuSelection(previous);
  }

  function completionMenuRows() {
    if (menu.filtered.length === 0) {
      return [{ content: "no matches", fg: THEME.muted }];
    }
    const visible = Math.max(
      1,
      Math.min(6, menu.filtered.length, completionMenuMaxRows() - 2),
    );
    const selected = clamp(menu.selected, 0, menu.filtered.length - 1);
    let start = Math.max(0, selected - Math.floor(visible / 2));
    start = Math.min(start, Math.max(0, menu.filtered.length - visible));
    return menu.filtered.slice(start, start + visible).map((item, offset) => {
      const index = start + offset;
      const marker = index === selected ? "› " : "  ";
      const label = String(item.label ?? "");
      const rawDescription = String(item.description ?? "");
      // A description that merely repeats the label (file rows) would render
      // the same text twice on an already width-clipped row.
      const description = rawDescription === label ? "" : rawDescription;
      const content = `${marker}${label}${description ? `  ${description}` : ""}`;
      return {
        content: clipToCells(content, completionMenuRowCells()),
        fg: index === selected ? THEME.brandAccentSoft : THEME.text,
      };
    });
  }

  // On narrow terminals shrink the menu's right inset instead of clipping rows
  // wider than the box: the clip width must always equal the box inner width,
  // or over-wide rows wrap inside the fixed-height box and corrupt its layout.
  function completionMenuRightInset() {
    const surfaceWidth = footerSurfaceWidth();
    const localRight = clamp(
      surfaceWidth - COMPLETION_MENU_LEFT - COMPLETION_MENU_CHROME_CELLS - MIN_COMPLETION_ROW_CELLS,
      0,
      COMPLETION_MENU_RIGHT,
    );
    return footerRightInset() + localRight;
  }

  function completionMenuRowCells() {
    const localRight = completionMenuRightInset() - footerRightInset();
    return Math.max(
      1,
      footerSurfaceWidth() - COMPLETION_MENU_LEFT - localRight - COMPLETION_MENU_CHROME_CELLS,
    );
  }

  // Remove any previously mounted completion menu from the overlay layer so a
  // shrinking menu never leaves a stale node behind and re-renders don't stack.
  function clearOverlay() {
    destroyRenderable(overlayLayer, "completion-menu");
    destroyRenderable(overlayLayer, "theme-picker");
    destroyRenderable(overlayLayer, "approval-overlay");
    destroyRenderable(overlayLayer, "session-picker");
    destroyRenderable(overlayLayer, "model-routing-picker");
    destroyRenderable(overlayLayer, "model-picker");
    // Hide the layer again so it stops intercepting wheel events — otherwise a
    // permanently-visible full-screen overlay blocks conversation scrolling.
    if (overlayLayer) overlayLayer.visible = false;
  }

  function renderCompletionMenu() {
    // Always clear first: a closed menu must vanish, and an open one is rebuilt
    // fresh so its height tracks the current candidate count exactly.
    clearOverlay();
    if (!menu.active) return;
    // A resize can shrink the terminal under an open menu; with no room to show
    // even one candidate row it must deactivate, not linger as an invisible
    // modal that swallows arrow keys and Enter.
    if (completionMenuMaxRows() < 3) {
      resetMenu();
      return;
    }
    const rows = completionMenuRows();
    // Mounted on the full-screen overlay layer (a root sibling), so `bottom` is
    // screen-relative: footerHeight rows up puts the menu directly above the
    // footer. It can never bleed into the scrollback buffer the way an
    // inputBox-child overflowing upward did, and the overlay's high zIndex keeps
    // it painted above the conversation.
    const menuNode = new BoxRenderable(renderer, {
      id: "completion-menu",
      position: "absolute",
      left: COMPLETION_MENU_LEFT,
      right: completionMenuRightInset(),
      bottom: effFooterHeight(),
      height: Math.min(8, rows.length + 2, completionMenuMaxRows()),
      borderStyle: "rounded",
      borderColor: THEME.composerBorder,
      // Opaque fill so the conversation behind the menu cannot show through and
      // collide with the menu rows (a transparent box leaks the backdrop).
      backgroundColor: THEME.overlayBg,
      title: menu.kind === "file" ? " files " : " commands ",
      titleAlignment: "left",
      flexDirection: "column",
      paddingLeft: 1,
      paddingRight: 1,
    });
    rows.forEach((row, index) => {
      menuNode.add(new TextRenderable(renderer, {
        id: `completion-menu-row-${index}`,
        content: row.content,
        fg: row.fg,
      }));
    });
    overlayLayer.add(menuNode);
    // Reveal the layer only now that it carries a menu, so it intercepts mouse
    // events solely while the menu is open (clearOverlay hides it again).
    overlayLayer.visible = true;
  }

  // ---- Theme picker overlay -------------------------------------------------
  // A modal theme list mounted on the overlay layer (like the completion menu).
  // Arrow keys preview each theme live; Enter keeps it; Esc reverts to the theme
  // that was active when the picker opened. Rendering here (not console output)
  // is why it looks like a panel instead of stray scrollback text.
  const THEME_PICKER_WIDTH = 34;
  let themePicker = null;
  const SESSION_PICKER_WIDTH = 72;
  let sessionPicker = null;
  const MODEL_ROUTING_PICKER_WIDTH = 46;
  let modelRoutingPicker = null;
  const MODEL_PICKER_WIDTH = 72;
  let modelPicker = null;

  function overlayPanelGeometry(preferredWidth) {
    const width = Math.max(
      1,
      Math.min(preferredWidth, footerSurfaceWidth() - COMPLETION_MENU_LEFT - 1),
    );
    return { width, inner: Math.max(1, width - COMPLETION_MENU_CHROME_CELLS) };
  }

  function applyHostTheme(name) {
    applyTheme(name);
    renderer.setBackgroundColor?.(THEME.appBg);
    if (conversationBox) conversationBox.backgroundColor = THEME.appBg;
    if (inputBox) inputBox.backgroundColor = THEME.footerBg;
    // Repaint the footer and remount any active overlay once. The shared
    // rerender path also reasserts the hardware caret for IME anchoring.
    rerenderInputRegion();
  }

  function renderThemePicker() {
    if (!themePicker?.active) return;
    destroyRenderable(overlayLayer, "theme-picker");
    const names = themePicker.names;
    const geometry = overlayPanelGeometry(THEME_PICKER_WIDTH);
    const maxRows = Math.max(1, viewportHeight() - effFooterHeight() - 1);
    const node = new BoxRenderable(renderer, {
      id: "theme-picker",
      position: "absolute",
      left: COMPLETION_MENU_LEFT,
      width: geometry.width,
      bottom: effFooterHeight(),
      height: Math.min(names.length + 3, maxRows),
      borderStyle: "rounded",
      borderColor: THEME.brandAccent,
      backgroundColor: THEME.overlayBg,
      title: " theme ",
      titleAlignment: "left",
      flexDirection: "column",
      paddingLeft: 1,
      paddingRight: 1,
    });
    names.forEach((name, index) => {
      const active = index === themePicker.selected;
      node.add(new TextRenderable(renderer, {
        id: `theme-picker-row-${index}`,
        content: clipToCells(`${active ? "› " : "  "}${name}`, geometry.inner),
        fg: active ? THEME.brandAccentSoft : THEME.muted,
      }));
    });
    node.add(new TextRenderable(renderer, {
      id: "theme-picker-hint",
      content: clipToCells("↑↓ preview · enter keep · esc", geometry.inner),
      fg: THEME.detailText,
    }));
    overlayLayer.add(node);
    overlayLayer.visible = true;
  }

  function openThemePicker() {
    if (sessionPicker?.active) closeSessionPicker();
    if (modelRoutingPicker?.active) closeModelRoutingPicker();
    if (modelPicker?.active) closeModelPicker();
    resetMenu(); // close the completion menu if it was open
    const names = THEME_NAMES;
    let selected = names.indexOf(activeThemeName());
    if (selected < 0) selected = 0;
    themePicker = { active: true, names, selected, original: activeThemeName() };
    renderThemePicker();
    renderer.requestRender?.();
  }

  function closeThemePicker() {
    themePicker = null;
    clearOverlay();
  }

  function handleThemePickerKey(keyName) {
    const result = themePickerKeyAction(themePicker, keyName);
    if (!result.handled) return false;
    if (result.action === "preview") {
      themePicker.selected = result.selected;
      applyHostTheme(themePicker.names[result.selected]);
    } else if (result.action === "confirm") {
      const confirmed = themePicker.names[themePicker.selected];
      closeThemePicker();
      // Report the kept theme so the Python side can persist it — the /theme
      // command path knows the name it sent, but a picker choice is made
      // entirely in the host.
      sendHostMessage({ type: "theme.selected", name: confirmed });
      renderer.requestRender?.();
    } else if (result.action === "cancel") {
      const original = themePicker.original;
      closeThemePicker();
      applyHostTheme(original); // revert the live preview
    }
    return true;
  }

  // ---- Session picker ------------------------------------------------------
  // Gateway owns the canonical rows; the host only filters and returns a
  // normal /resume command, keeping session mutation in the existing Python
  // command path instead of inventing a second owner.
  function filteredSessions() {
    if (!sessionPicker?.active) return [];
    const query = String(sessionPicker.query ?? "").trim().toLowerCase();
    if (!query) return sessionPicker.sessions;
    return sessionPicker.sessions.filter((item) =>
      `${item.title} ${item.key} ${item.model} ${item.status}`.toLowerCase().includes(query));
  }

  function renderSessionPicker() {
    if (!sessionPicker?.active) return;
    destroyRenderable(overlayLayer, "session-picker");
    const geometry = overlayPanelGeometry(SESSION_PICKER_WIDTH);
    const available = Math.max(4, viewportHeight() - effFooterHeight() - 1);
    const rows = filteredSessions();
    sessionPicker.selected = clamp(sessionPicker.selected, 0, Math.max(0, rows.length - 1));
    const visible = Math.max(1, Math.min(rows.length || 1, available - 4, 8));
    let start = Math.max(0, sessionPicker.selected - Math.floor(visible / 2));
    start = Math.min(start, Math.max(0, rows.length - visible));
    const node = new BoxRenderable(renderer, {
      id: "session-picker",
      position: "absolute",
      left: COMPLETION_MENU_LEFT,
      width: geometry.width,
      bottom: effFooterHeight(),
      height: Math.min(available, visible + 4),
      borderStyle: "rounded",
      borderColor: THEME.brandAccent,
      backgroundColor: THEME.overlayBg,
      title: " sessions ",
      titleAlignment: "left",
      flexDirection: "column",
      paddingLeft: 1,
      paddingRight: 1,
    });
    node.add(new TextRenderable(renderer, {
      id: "session-picker-query",
      content: clipToCells(`search: ${sessionPicker.query || "_"}`, geometry.inner),
      fg: THEME.text,
      wrapMode: "none",
    }));
    if (rows.length === 0) {
      node.add(new TextRenderable(renderer, {
        id: "session-picker-empty",
        content: "  no matching sessions",
        fg: THEME.muted,
      }));
    } else {
      rows.slice(start, start + visible).forEach((item, offset) => {
        const index = start + offset;
        const current = item.key === sessionPicker.currentKey ? "●" : " ";
        const label = item.title || item.key;
        const meta = [item.model, item.status, `${item.messageCount} msgs`]
          .filter(Boolean).join(" · ");
        node.add(new TextRenderable(renderer, {
          id: `session-picker-row-${index}`,
          content: clipToCells(
            `${index === sessionPicker.selected ? "›" : " "} ${current} ${label}${meta ? `  ${meta}` : ""}`,
            geometry.inner,
          ),
          fg: index === sessionPicker.selected ? THEME.brandAccentSoft : THEME.muted,
          wrapMode: "none",
        }));
      });
    }
    node.add(new TextRenderable(renderer, {
      id: "session-picker-hint",
      content: clipToCells("type to filter · ↑↓ choose · enter resume · esc", geometry.inner),
      fg: THEME.detailText,
      wrapMode: "none",
    }));
    overlayLayer.add(node);
    overlayLayer.visible = true;
  }

  function closeSessionPicker() {
    sessionPicker = null;
    clearOverlay();
  }

  function openSessionPicker(message) {
    if (themePicker?.active) handleThemePickerKey("escape");
    if (modelRoutingPicker?.active) closeModelRoutingPicker();
    if (modelPicker?.active) closeModelPicker();
    resetMenu();
    const sessions = (Array.isArray(message?.sessions) ? message.sessions : [])
      .map((item) => ({
        key: String(item?.key ?? ""),
        title: String(item?.title ?? ""),
        model: String(item?.model ?? ""),
        status: String(item?.status ?? ""),
        messageCount: Number(item?.message_count ?? 0) || 0,
      }))
      .filter((item) => item.key);
    sessionPicker = {
      active: true,
      currentKey: String(message?.current_key ?? ""),
      sessions,
      query: "",
      selected: Math.max(0, sessions.findIndex(
        (item) => item.key === String(message?.current_key ?? ""),
      )),
    };
    renderSessionPicker();
    renderer.requestRender?.();
  }

  function handleSessionPickerKey(key) {
    if (!sessionPicker?.active) return false;
    if (key.name === "escape" || (key.ctrl && key.name === "c")) {
      closeSessionPicker();
      rerenderInputRegion();
      return true;
    }
    const rows = filteredSessions();
    if (key.name === "up" || key.name === "down") {
      const delta = key.name === "up" ? -1 : 1;
      sessionPicker.selected = clamp(
        sessionPicker.selected + delta,
        0,
        Math.max(0, rows.length - 1),
      );
      renderSessionPicker();
      renderer.requestRender?.();
      return true;
    }
    if (key.name === "return") {
      const selected = rows[sessionPicker.selected];
      if (!selected) return true;
      closeSessionPicker();
      sendHostMessage({ type: "input.submit", text: `/resume ${selected.key}` });
      rerenderInputRegion();
      return true;
    }
    if (key.name === "backspace") {
      sessionPicker.query = Array.from(sessionPicker.query).slice(0, -1).join("");
      sessionPicker.selected = 0;
      renderSessionPicker();
      renderer.requestRender?.();
      return true;
    }
    const printable = !key.ctrl && !key.meta && !key.alt && !key.option
      && typeof key.sequence === "string"
      && !/[\u0000-\u001f\u007f]/u.test(key.sequence);
    if (printable) {
      sessionPicker.query += key.sequence;
      sessionPicker.selected = 0;
      renderSessionPicker();
      renderer.requestRender?.();
    }
    return true;
  }

  // ---- Model-routing picker -----------------------------------------------
  const MODEL_ROUTING_LABELS = Object.freeze({
    direct: ["Direct", "Use the selected/default model"],
    router: ["Squilla Router", "Route by cost and capability"],
    ensemble: ["Model Ensemble", "Run candidates and synthesize"],
  });

  function renderModelRoutingPicker() {
    if (!modelRoutingPicker?.active) return;
    destroyRenderable(overlayLayer, "model-routing-picker");
    const geometry = overlayPanelGeometry(MODEL_ROUTING_PICKER_WIDTH);
    const available = Math.max(4, viewportHeight() - effFooterHeight() - 1);
    const node = new BoxRenderable(renderer, {
      id: "model-routing-picker",
      position: "absolute",
      left: COMPLETION_MENU_LEFT,
      width: geometry.width,
      bottom: effFooterHeight(),
      height: Math.min(available, modelRoutingPicker.options.length + 3),
      borderStyle: "rounded",
      borderColor: THEME.brandAccent,
      backgroundColor: THEME.overlayBg,
      title: " model strategy ",
      titleAlignment: "left",
      flexDirection: "column",
      paddingLeft: 1,
      paddingRight: 1,
    });
    modelRoutingPicker.options.forEach((mode, index) => {
      const [label, detail] = MODEL_ROUTING_LABELS[mode] ?? [mode, ""];
      const selected = index === modelRoutingPicker.selected;
      const current = mode === modelRoutingPicker.current ? "●" : " ";
      node.add(new TextRenderable(renderer, {
        id: `model-routing-picker-row-${mode}`,
        content: clipToCells(
          `${selected ? "›" : " "} ${current} ${label}${detail ? `  ${detail}` : ""}`,
          geometry.inner,
        ),
        fg: selected ? THEME.brandAccentSoft : THEME.muted,
        wrapMode: "none",
      }));
    });
    node.add(new TextRenderable(renderer, {
      id: "model-routing-picker-hint",
      content: clipToCells("↑↓ choose · enter apply next turn · esc", geometry.inner),
      fg: THEME.detailText,
      wrapMode: "none",
    }));
    overlayLayer.add(node);
    overlayLayer.visible = true;
  }

  function closeModelRoutingPicker() {
    modelRoutingPicker = null;
    clearOverlay();
  }

  function openModelRoutingPicker(message) {
    if (themePicker?.active) handleThemePickerKey("escape");
    if (sessionPicker?.active) closeSessionPicker();
    if (modelPicker?.active) closeModelPicker();
    if (modelRoutingPicker?.active) closeModelRoutingPicker();
    resetMenu();
    const supplied = Array.isArray(message?.options) ? message.options : [];
    const options = supplied
      .map((item) => String(item ?? "").toLowerCase())
      .filter((item) => Object.hasOwn(MODEL_ROUTING_LABELS, item));
    const canonical = options.length ? [...new Set(options)] : ["direct", "router", "ensemble"];
    const current = String(message?.current ?? modelRoutingState.current ?? "direct");
    modelRoutingPicker = {
      active: true,
      options: canonical,
      current,
      selected: Math.max(0, canonical.indexOf(current)),
    };
    renderModelRoutingPicker();
    renderer.requestRender?.();
  }

  function handleModelRoutingPickerKey(keyName) {
    const result = routingPickerKeyAction(modelRoutingPicker, keyName);
    if (!result.handled) return false;
    if (result.action === "navigate") {
      modelRoutingPicker.selected = result.selected;
      renderModelRoutingPicker();
      renderer.requestRender?.();
    } else if (result.action === "cancel") {
      closeModelRoutingPicker();
      rerenderInputRegion();
    } else if (result.action === "confirm") {
      const mode = modelRoutingPicker.options[result.selected] ?? "direct";
      closeModelRoutingPicker();
      const command = mode === "ensemble"
        ? "/ensemble on"
        : mode === "router" ? "/router on" : "/router off";
      sendHostMessage({ type: "input.submit", text: command, intent: "control" });
      rerenderInputRegion();
    }
    return true;
  }

  // ---- Session model picker -----------------------------------------------
  // The Gateway sends its canonical model catalog.  "Auto" is a synthetic
  // first row that clears the durable session pin; every other row submits the
  // existing /model fast path so Python remains the only mutation owner.
  function filteredModels() {
    if (!modelPicker?.active) return [];
    const query = String(modelPicker.query ?? "").trim().toLowerCase();
    if (!query) return modelPicker.options;
    return modelPicker.options.filter((item) =>
      `${item.label} ${item.id} ${item.provider}`.toLowerCase().includes(query));
  }

  function renderModelPicker() {
    if (!modelPicker?.active) return;
    destroyRenderable(overlayLayer, "model-picker");
    const geometry = overlayPanelGeometry(MODEL_PICKER_WIDTH);
    const available = Math.max(4, viewportHeight() - effFooterHeight() - 1);
    const rows = filteredModels();
    modelPicker.selected = clamp(modelPicker.selected, 0, Math.max(0, rows.length - 1));
    const visible = Math.max(1, Math.min(rows.length || 1, available - 4, 10));
    let start = Math.max(0, modelPicker.selected - Math.floor(visible / 2));
    start = Math.min(start, Math.max(0, rows.length - visible));
    const node = new BoxRenderable(renderer, {
      id: "model-picker",
      position: "absolute",
      left: COMPLETION_MENU_LEFT,
      width: geometry.width,
      bottom: effFooterHeight(),
      height: Math.min(available, visible + 4),
      borderStyle: "rounded",
      borderColor: THEME.brandAccent,
      backgroundColor: THEME.overlayBg,
      title: " session model ",
      titleAlignment: "left",
      flexDirection: "column",
      paddingLeft: 1,
      paddingRight: 1,
    });
    node.add(new TextRenderable(renderer, {
      id: "model-picker-query",
      content: clipToCells(`search: ${modelPicker.query || "_"}`, geometry.inner),
      fg: THEME.text,
      wrapMode: "none",
    }));
    if (rows.length === 0) {
      node.add(new TextRenderable(renderer, {
        id: "model-picker-empty",
        content: "  no matching models",
        fg: THEME.muted,
      }));
    } else {
      rows.slice(start, start + visible).forEach((item, offset) => {
        const index = start + offset;
        const current = item.id === modelPicker.current ? "●" : " ";
        const meta = [item.provider, item.contextWindow]
          .filter(Boolean).join(" · ");
        node.add(new TextRenderable(renderer, {
          id: `model-picker-row-${index}`,
          content: clipToCells(
            `${index === modelPicker.selected ? "›" : " "} ${current} ${item.label}${meta ? `  ${meta}` : ""}`,
            geometry.inner,
          ),
          fg: index === modelPicker.selected ? THEME.brandAccentSoft : THEME.muted,
          wrapMode: "none",
        }));
      });
    }
    node.add(new TextRenderable(renderer, {
      id: "model-picker-hint",
      content: clipToCells("type to filter · ↑↓ choose · enter pin · esc", geometry.inner),
      fg: THEME.detailText,
      wrapMode: "none",
    }));
    overlayLayer.add(node);
    overlayLayer.visible = true;
  }

  function closeModelPicker() {
    modelPicker = null;
    clearOverlay();
  }

  function openModelPicker(message) {
    if (themePicker?.active) handleThemePickerKey("escape");
    if (sessionPicker?.active) closeSessionPicker();
    if (modelRoutingPicker?.active) closeModelRoutingPicker();
    if (modelPicker?.active) closeModelPicker();
    resetMenu();
    const seen = new Set();
    const supplied = (Array.isArray(message?.options) ? message.options : [])
      .map((item) => ({
        id: String(item?.id ?? "").trim(),
        label: String(item?.id ?? "").trim(),
        provider: String(item?.provider ?? "").trim(),
        contextWindow: item?.context_window ?? item?.contextWindow ?? "",
      }))
      .filter((item) => item.id && !seen.has(item.id) && seen.add(item.id));
    const options = [
      {
        id: "auto",
        label: "Auto",
        provider: "Router/default decides",
        contextWindow: "",
      },
      ...supplied,
    ];
    const current = String(message?.current ?? "").trim() || "auto";
    modelPicker = {
      active: true,
      options,
      current,
      query: "",
      selected: Math.max(0, options.findIndex((item) => item.id === current)),
    };
    renderModelPicker();
    renderer.requestRender?.();
  }

  function handleModelPickerKey(key) {
    if (!modelPicker?.active) return false;
    if (key.name === "escape" || (key.ctrl && key.name === "c")) {
      closeModelPicker();
      rerenderInputRegion();
      return true;
    }
    const rows = filteredModels();
    if (key.name === "up" || key.name === "down") {
      const delta = key.name === "up" ? -1 : 1;
      modelPicker.selected = clamp(
        modelPicker.selected + delta,
        0,
        Math.max(0, rows.length - 1),
      );
      renderModelPicker();
      renderer.requestRender?.();
      return true;
    }
    if (key.name === "return") {
      const selected = rows[modelPicker.selected];
      if (!selected) return true;
      closeModelPicker();
      sendHostMessage({
        type: "input.submit",
        text: `/model ${selected.id}`,
        intent: "control",
      });
      rerenderInputRegion();
      return true;
    }
    if (key.name === "backspace") {
      modelPicker.query = Array.from(modelPicker.query).slice(0, -1).join("");
      modelPicker.selected = 0;
      renderModelPicker();
      renderer.requestRender?.();
      return true;
    }
    const printable = !key.ctrl && !key.meta && !key.alt && !key.option
      && typeof key.sequence === "string"
      && !/[\u0000-\u001f\u007f]/u.test(key.sequence);
    if (printable) {
      modelPicker.query += key.sequence;
      modelPicker.selected = 0;
      renderModelPicker();
      renderer.requestRender?.();
    }
    return true;
  }

  // ---- Tool-approval overlay ------------------------------------------------
  // A modal confirm panel mounted on the overlay layer (like the theme picker),
  // opened by the Python side's approval.request frame while a turn is waiting
  // on a gated tool. y/Enter approves, n/Esc denies; when the request carries
  // choices, Up/Down move the highlight and Enter confirms the selected one.
  // The decision is sent back as one approval.response frame; the Python side
  // treats silence (timeout, teardown) as a deny, so the overlay never has to
  // guarantee delivery.
  const APPROVAL_OVERLAY_WIDTH = 72;
  let approvalOverlay = null;

  function approvalChoiceLabel(choice) {
    // Ids are tool/model-derived and skip the Python display sanitizer (the
    // raw id must round-trip in approval.response), so strip control bytes
    // at the one place the id becomes visible text.
    return stripTerminalControls(String(choice ?? "")).replaceAll("_", " ");
  }

  function approvalWrappedRows(text, cells) {
    return String(text ?? "")
      .split("\n")
      .flatMap((line) => wrapToCells(line, cells));
  }

  function approvalTruncatedRows(rows, cells, maxRows) {
    if (rows.length <= maxRows) return rows;
    const kept = rows.slice(0, maxRows);
    kept[maxRows - 1] = clipToCells(`${kept[maxRows - 1]} … +${rows.length - maxRows} rows`, cells);
    return kept;
  }

  function renderApprovalOverlay() {
    if (!approvalOverlay?.active) return;
    destroyRenderable(overlayLayer, "approval-overlay");
    const choices = approvalOverlay.choices;
    const geometry = overlayPanelGeometry(APPROVAL_OVERLAY_WIDTH);
    const maxRows = Math.max(1, viewportHeight() - effFooterHeight() - 1);
    const wrappedSummary = approvalOverlay.summary
      ? approvalWrappedRows(approvalOverlay.summary, geometry.inner)
      : [];
    const wrappedMessage = approvalOverlay.message
      ? approvalWrappedRows(approvalOverlay.message, geometry.inner)
      : [];
    const plan = approvalRowPlan(
      maxRows,
      choices.length,
      wrappedSummary.length,
      wrappedMessage.length,
    );
    const summaryRows = plan.summaryRows > 0
      ? approvalTruncatedRows(wrappedSummary, geometry.inner, plan.summaryRows)
      : [];
    const messageRows = plan.messageRows > 0
      ? approvalTruncatedRows(wrappedMessage, geometry.inner, plan.messageRows)
      : [];
    // On a terminal so small that not even one summary row fits, approving
    // unseen text must at least be signposted.
    const summaryHidden = wrappedSummary.length > 0 && summaryRows.length === 0;
    const bodyRows =
      1 + choices.length + summaryRows.length + messageRows.length + (plan.hint ? 1 : 0);
    const node = new BoxRenderable(renderer, {
      id: "approval-overlay",
      position: "absolute",
      left: COMPLETION_MENU_LEFT,
      width: geometry.width,
      bottom: effFooterHeight(),
      height: Math.min(bodyRows + 2, maxRows),
      borderStyle: "rounded",
      borderColor: THEME.warning,
      backgroundColor: THEME.overlayBg,
      title: summaryHidden ? " approval · summary hidden " : " approval ",
      titleAlignment: "left",
      flexDirection: "column",
      paddingLeft: 1,
      paddingRight: 1,
    });
    node.add(new TextRenderable(renderer, {
      id: "approval-overlay-tool",
      content: clipToCells(approvalOverlay.tool, geometry.inner),
      fg: THEME.text,
    }));
    summaryRows.forEach((row, index) => {
      node.add(new TextRenderable(renderer, {
        id: index === 0 ? "approval-overlay-summary" : `approval-overlay-summary-${index}`,
        content: row,
        fg: THEME.muted,
      }));
    });
    // The Python side's rationale line (why this needs approval) — the same
    // text the plain-console prompt prints; the overlay must never show less.
    messageRows.forEach((row, index) => {
      node.add(new TextRenderable(renderer, {
        id: index === 0 ? "approval-overlay-message" : `approval-overlay-message-${index}`,
        content: row,
        fg: THEME.detailText,
      }));
    });
    choices.forEach((choice, index) => {
      const active = index === approvalOverlay.selected;
      node.add(new TextRenderable(renderer, {
        id: `approval-overlay-choice-${index}`,
        content: clipToCells(
          `${active ? "› " : "  "}${approvalChoiceLabel(choice)}`,
          geometry.inner,
        ),
        fg: active ? THEME.brandAccentSoft : THEME.muted,
      }));
    });
    if (plan.hint) {
      node.add(new TextRenderable(renderer, {
        id: "approval-overlay-hint",
        content: clipToCells(
          choices.length > 0
            ? "↑↓ choose · enter confirm · y/n · esc deny"
            : "y/enter approve · n/esc deny",
          geometry.inner,
        ),
        fg: THEME.detailText,
      }));
    }
    overlayLayer.add(node);
    overlayLayer.visible = true;
  }

  function openApprovalOverlay(message) {
    const id = String(message?.id ?? "");
    if (!id) return;
    // The approval takes the overlay layer: close the theme picker (reverting
    // its live preview, exactly like Escape) and any completion menu first.
    if (themePicker?.active) handleThemePickerKey("escape");
    if (sessionPicker?.active) closeSessionPicker();
    if (modelRoutingPicker?.active) closeModelRoutingPicker();
    if (modelPicker?.active) closeModelPicker();
    resetMenu();
    approvalOverlay = {
      active: true,
      id,
      tool: String(message?.tool ?? "tool"),
      summary: String(message?.summary ?? ""),
      message: String(message?.message ?? ""),
      choices: Array.isArray(message?.choices) ? message.choices.map(String) : [],
      selected: 0,
    };
    renderApprovalOverlay();
    renderer.requestRender?.();
  }

  function closeApprovalOverlay() {
    approvalOverlay = null;
    clearOverlay();
  }

  // Python dismisses a request it stopped waiting on (timeout / turn cancel).
  // Close only the matching active overlay, and send no approval.response —
  // the Python side already resolved the request, so a late decision frame
  // would just be logged as unmatched and dropped.
  function dismissApprovalOverlay(id) {
    if (!approvalOverlay?.active) return;
    if (String(id ?? "") !== approvalOverlay.id) return;
    closeApprovalOverlay();
    rerenderInputRegion();
  }

  function sendApprovalDecision(approved, choice) {
    const id = approvalOverlay?.id;
    closeApprovalOverlay();
    rerenderInputRegion();
    sendHostMessage({ type: "approval.response", id, approved, choice: choice ?? null });
  }

  function handleApprovalKey(key) {
    const result = approvalKeyAction(approvalOverlay, key);
    if (!result.handled) return false;
    if (result.action === "navigate") {
      approvalOverlay.selected = result.selected;
      renderApprovalOverlay();
      renderer.requestRender?.();
    } else if (result.action === "approve") {
      sendApprovalDecision(true, null);
    } else if (result.action === "deny") {
      sendApprovalDecision(false, null);
    } else if (result.action === "choose") {
      sendApprovalDecision(true, approvalOverlay.choices[result.selected] ?? null);
    }
    return true;
  }

  // Composer content geometry, shared by the rendered lines and the caret
  // math so the two can never disagree. The box spans the full width minus the
  // COMPOSER_LEFT margins; border + padding eat 2 more cells per side.
  function composerContentWidth() {
    return Math.max(1, footerSurfaceWidth() - COMPOSER_LEFT * 2 - 4);
  }

  // Content rows inside the composer box: box height (effFooterHeight - 1,
  // the router strip takes the top footer row) minus its two border rows.
  function composerContentRows() {
    return Math.max(1, effFooterHeight() - 3 - attachmentRowCount());
  }

  function attachmentRowCount() {
    return attachments.size > 0 && effFooterHeight() >= 5 ? 1 : 0;
  }

  function attachmentLine() {
    return clipToCells(
      Array.from(attachments.values()).map(attachmentChipText).join(" "),
      composerContentWidth(),
    );
  }

  function attachmentColor() {
    const values = Array.from(attachments.values());
    if (values.some((item) => item.status === "failed")) return THEME.error;
    if (attachmentSubmitBlocked(values)) return THEME.warning;
    return THEME.muted;
  }

  // Lines to render inside the composer box: the wrapped, caret-windowed
  // layout from composerLayout (see its docs), plus the muted placeholder for
  // an empty input.
  function composerViewport() {
    if (Array.from(inputText).length === 0) {
      // Empty: caret sits before the muted placeholder.
      const placeholder = turnActive
        ? "steer current turn · Tab queues"
        : composer.placeholder;
      return {
        lines: [{ text: `${caretGlyph()}${placeholder}`, muted: true }],
        caretRow: 0,
        caretCol: 0,
        scrollRowOffset: 0,
      };
    }
    const layout = composerLayout(
      inputText,
      cursorPos,
      composerContentWidth(),
      composerContentRows(),
    );
    return {
      lines: layout.visibleLines.map((text) => ({ text, muted: false })),
      caretRow: layout.caretRow,
      caretCol: layout.caretCol,
      scrollRowOffset: layout.scrollRowOffset,
    };
  }

  function rerenderInputRegion() {
    if (!inputBox) return;
    destroyChildren(inputBox);
    const viewport = composerViewport();
    const width = terminalWidth();
    const composerNode = new BoxRenderable(renderer, {
      id: "composer-box",
      position: "absolute",
      left: COMPOSER_LEFT,
      right: COMPOSER_LEFT, // full width: nothing shares the caret's rows
      bottom: 0,
      height: Math.max(1, effFooterHeight() - 1), // the router strip takes the top row
      borderStyle: "rounded",
      borderColor: composer.disabled ? THEME.composerDisabledBorder : THEME.composerBorder,
      paddingLeft: 1,
      paddingRight: 1,
      flexDirection: "column",
      justifyContent: "flex-start",
    });
    if (attachmentRowCount()) {
      composerNode.add(new TextRenderable(renderer, {
        id: "attachment-chip-row",
        content: attachmentLine(),
        fg: attachmentColor(),
        wrapMode: "none",
      }));
    }
    viewport.lines.forEach((line, index) => {
      composerNode.add(new TextRenderable(renderer, {
        id: `composer-text-${index}`,
        content: line.text,
        fg: line.muted ? THEME.muted : THEME.text,
        // The lines are already wrapped to the content width by composerLayout;
        // letting the engine re-wrap them would desync the caret math.
        wrapMode: "none",
      }));
    });
    inputBox.add(composerNode);

    // Router status as a compact single-line strip ABOVE the (now full-width)
    // composer. Keeping it OFF the caret's rows is the fix for the macOS IME
    // corruption: with no box sharing the rows where the terminal composites the
    // marked-text / candidate overlay, there is no adjacent cell band for the
    // terminal's wide-char accounting to desync. (opencode, on the same
    // @opentui/core engine + alt-screen, is immune for exactly this reason — its
    // status sits below the input and its sidebar is a disjoint column.)
    const routerStrip = new BoxRenderable(renderer, {
      id: "router-strip",
      position: "absolute",
      top: 0,
      left: COMPOSER_LEFT,
      right: COMPOSER_LEFT,
      height: 1,
      paddingLeft: 2, // align the strip text with the composer's input text
      flexDirection: "row",
      backgroundColor: THEME.footerBg,
    });
    const chip = (suffix, content, fg) =>
      routerStrip.add(new TextRenderable(renderer, { id: `router-${suffix}`, content, fg, wrapMode: "none" }));
    // A quiet "router" label, dim field labels, and each VALUE in its semantic
    // color, with fields separated by a dim middot — matching the usage footnote
    // (value-forward hierarchy: data pops, labels recede).
    let sepN = 0;
    const field = (key, label, value, valueFg) => {
      chip(`sep${sepN++}`, " · ", THEME.detailText);
      chip(`${key}-label`, `${label} `, THEME.detailText);
      chip(`${key}-value`, routerStripValue(value), valueFg);
    };
    // The renderer sends a strip-wide semantic style ("dim" while the router is
    // pending, "warning"/"error" for fallback or broken routes); it overrides
    // the per-field value colors so that state is visible instead of dropped.
    const styleFg =
      routerState.style === "warning" ? THEME.warning
      : routerState.style === "error" ? THEME.error
      : routerState.style === "dim" ? THEME.detailText
      : null;
    const strategyLabel = modelStrategyLabel();
    chip("strategy", strategyLabel, modelStrategyColor());
    const compact = footerRightInset() > 0
      ? []
      : compactContextItems(
        contextState,
        routerState,
        Math.max(40, width - textWidth(strategyLabel) - 3),
      );
    if (compact.length > 0) {
      // Below the rail breakpoint, task identity and runtime safety collapse into one
      // display-cell-fitted strip. The helper already applied priority and
      // clipping, so no trailing high-priority field disappears off-screen.
      compact.forEach((item, index) => {
        chip(`context-sep-${index}`, " · ", THEME.detailText);
        chip(`${item.key}-value`, item.content, THEME[item.token] ?? THEME.text);
      });
    } else {
      // Wide layouts have the expanded context rail; older parents (which never
      // send context.update) also retain this exact router-only fallback.
      field("model", "model", routerModelValue(), styleFg ?? THEME.text);
      field("route", "route", routerRouteValue(), styleFg ?? THEME.routeText);
      field("saving", "save", routerState.saving, styleFg ?? THEME.metricPositive);
      field("context", "ctx", routerState.context, styleFg ?? THEME.warning);
      // Token traffic is its own field so "ctx" can stay a pure pressure value —
      // "ctx 34.6k/548" read like a fraction of a 548-token window.
      if (routerState.io) field("io", "io", routerState.io, styleFg ?? THEME.detailText);
    }
    inputBox.add(routerStrip);
    renderCompletionMenu();
    // The theme picker shares the overlay layer, so a footer re-render (router
    // update or keystroke) clears it via
    // renderCompletionMenu's clearOverlay. Re-mount it whenever it is open so it
    // never "flashes" away and gets stuck modally swallowing keys while invisible.
    if (themePicker?.active) renderThemePicker();
    // Same for the approval overlay: it stays mounted across footer re-renders
    // while the turn waits on the user's decision.
    if (approvalOverlay?.active) renderApprovalOverlay();
    if (sessionPicker?.active) renderSessionPicker();
    if (modelRoutingPicker?.active) renderModelRoutingPicker();
    if (modelPicker?.active) renderModelPicker();
    syncTerminalCursorToCaret(viewport);
    renderer.requestRender?.();
  }

  function submitInput(intent = null) {
    const text = inputText;
    // A staged attachment is not sendable until Python marks it ready. This
    // also blocks a second slash submit while the first file is being read or
    // uploaded. Failed chips remain visible but do not trap unrelated input.
    if (composer.disabled || attachmentSubmitBlocked(attachments.values())) {
      rerenderInputRegion();
      return;
    }
    // Enter on a blank composer is a no-op, like every shell/REPL: submitting
    // whitespace would echo an empty prompt card and queue a phantom message
    // behind a running turn.
    if (!text.trim()) return;
    if (inputHistory[inputHistory.length - 1] !== text) {
      inputHistory.push(text);
    }
    historyIndex = inputHistory.length;
    historyEditedRecall = false;
    draftBeforeHistory = "";
    inputText = "";
    cursorPos = 0;
    composer.text = "";
    menuDismissed = null; // resetting the input ends the dismissal scope
    resetMenu();
    const effectiveIntent = intent ?? (turnActive ? "steer" : "auto");
    const payload = { type: "input.submit", text };
    if (effectiveIntent !== "auto") payload.intent = effectiveIntent;
    sendHostMessage(payload);
    rerenderInputRegion();
  }

  function setInput(text) {
    inputText = text;
    composer.text = text;
    cursorPos = Array.from(text).length;
    desiredVisualCol = null; // history recall / programmatic set ends a vertical run
    menuDismissed = null; // replacing the input wholesale ends an Escape dismissal
  }

  // Every edit detaches from history browsing back to the draft slot. When the
  // edit happened INSIDE a recalled entry, the input now holds an edited recall
  // rather than the draft — remember that so the next Up does not overwrite the
  // saved draft with it.
  function detachHistoryToDraft() {
    if (historyIndex !== inputHistory.length) historyEditedRecall = true;
    historyIndex = inputHistory.length;
  }

  // Up/Down arrows walk the input history. The slot past the end (index ===
  // length) holds the in-progress draft so Down returns to what was typed.
  function recallHistory(direction) {
    if (inputHistory.length === 0) return;
    const next = historyIndex + direction;
    if (next < 0 || next > inputHistory.length) return;
    if (historyIndex === inputHistory.length) {
      // Leaving the draft slot: keep what is on screen recoverable.
      if (historyEditedRecall && draftBeforeHistory) {
        // The input holds an edited RECALL while a real draft is stashed.
        // Overwriting the draft would lose it for good, and dropping the edit
        // would destroy the text on screen — so keep the edit the way a shell
        // does: append it to history as the newest entry, so Down passes back
        // through it on the way to the draft.
        if (inputText.trim() && inputHistory[inputHistory.length - 1] !== inputText) {
          inputHistory.push(inputText);
        }
      } else {
        draftBeforeHistory = inputText;
      }
    }
    historyIndex = next;
    historyEditedRecall = false;
    setInput(next === inputHistory.length ? draftBeforeHistory : inputHistory[next]);
    updateMenuFromInput();
    rerenderInputRegion();
  }

  function caretVisualLineCol() {
    const chars = Array.from(inputText);
    const pos = Math.max(0, Math.min(cursorPos, chars.length));
    let line = 0;
    let lineText = "";
    for (let i = 0; i < pos; i += 1) {
      if (chars[i] === "\n") {
        line += 1;
        lineText = "";
      } else {
        lineText += chars[i];
      }
    }
    return { line, col: textWidth(lineText) };
  }

  function syncTerminalCursorToCaret(viewport) {
    const setCursorPosition = renderer?.setCursorPosition;
    if (typeof setCursorPosition !== "function") return;
    const terminalHeight = viewportHeight();
    const fh = effFooterHeight();
    const footerTop = Math.max(0, terminalHeight - fh);
    // The caret row/col come from the SAME wrapped-and-windowed layout the
    // composer just rendered, so the hardware cursor lands on the exact cell
    // the caret occupies even on soft-wrapped or scrolled drafts.
    const screenRow = viewport.caretRow - viewport.scrollRowOffset;
    const maxX = Math.max(COMPOSER_CONTENT_LEFT, footerSurfaceWidth() - COMPOSER_CONTENT_LEFT - 1);
    const maxY = Math.max(footerTop, footerTop + fh - 2);
    const x = clamp(COMPOSER_CONTENT_LEFT + viewport.caretCol, COMPOSER_CONTENT_LEFT, maxX);
    const contentTop = COMPOSER_CONTENT_TOP_OFFSET + attachmentRowCount();
    const y = clamp(
      footerTop + contentTop + screenRow,
      footerTop + contentTop,
      maxY,
    );
    // visible:true is REQUIRED for IME anchoring. OpenTUI's native renderer only
    // emits a CUP move (and shows the hardware cursor) when visible is true; with
    // false it keeps the hardware cursor hidden at home, so macOS terminals
    // (Terminal.app/iTerm2) anchor the Pinyin candidate popover to that hidden
    // home position — the candidate window drifts to a corner instead of the
    // caret. Showing the real cursor here lets the IME attach candidates at the
    // caret cell. The composer no longer paints its own "▏" (see caretGlyph) so
    // there is exactly one caret.
    //
    // +1 on both axes: x/y above are 0-based screen cells, but OpenTUI's native
    // cursor path is 1-based — its own TextEditor.renderCursor passes
    // screenX/Y + visual + 1. Without the +1 the reported cell is one row too
    // high and one column too far left, so the IME (and the visible caret) land
    // off the true caret cell. Match OpenTUI's convention exactly.
    setCursorPosition.call(renderer, x + 1, y + 1, true);
  }

  // Code-point index of the caret on `targetLine` whose DISPLAY-CELL column is
  // closest to `targetVisualCol`. Snaps to a code-point boundary: when the goal
  // falls mid-glyph (e.g. inside a width-2 CJK char), land on whichever side is
  // nearer, and never past the line's end.
  function lineVisualColToPos(chars, targetLine, targetVisualCol) {
    let line = 0;
    let col = 0; // display cells consumed on the current line so far
    let lineStart = 0;
    for (let i = 0; i <= chars.length; i += 1) {
      const atEnd = i === chars.length || chars[i] === "\n";
      if (line === targetLine && (atEnd || col >= targetVisualCol)) {
        // Boundary before chars[i] sits at column `col`. If we stepped over the
        // goal mid-glyph, the previous boundary may be the nearer one.
        if (!atEnd && i > lineStart && col > targetVisualCol) {
          const prevCol = col - cellWidth(chars[i - 1], chars[i]);
          if (targetVisualCol - prevCol < col - targetVisualCol) return i - 1;
        }
        return i;
      }
      if (atEnd) {
        if (line === targetLine) return i; // goal past the end of this line
        line += 1;
        col = 0;
        lineStart = i + 1;
      } else {
        col += cellWidth(chars[i], chars[i + 1]);
      }
    }
    return chars.length;
  }

  // Move caret up/down a line. Returns true if it moved within the text; false if
  // already at the very first/last line (caller may then switch history). Tracks a
  // desired VISUAL column (display cells) so the caret keeps its on-screen column
  // across lines with wide (CJK) glyphs, and preserves it across consecutive moves.
  function moveCaretVertical(direction) {
    const { line, col } = caretVisualLineCol();
    const chars = Array.from(inputText);
    const lineCount = inputText.split("\n").length;
    const target = line + direction;
    if (target < 0 || target >= lineCount) return false;
    if (desiredVisualCol === null) desiredVisualCol = col;
    cursorPos = lineVisualColToPos(chars, target, desiredVisualCol);
    return true;
  }

  // Step one grapheme cluster left/right, so the caret never lands inside a
  // multi-code-point emoji or composed character.
  function moveCaretHorizontal(direction) {
    cursorPos = direction < 0
      ? graphemeBoundaryBefore(inputText, cursorPos)
      : graphemeBoundaryAfter(inputText, cursorPos);
  }

  function insertAtCursor(insertText) {
    desiredVisualCol = null; // editing (incl. paste) ends a vertical-motion run
    const chars = Array.from(inputText);
    const pos = Math.max(0, Math.min(cursorPos, chars.length));
    const insertChars = Array.from(insertText);
    inputText = [...chars.slice(0, pos), ...insertChars, ...chars.slice(pos)].join("");
    composer.text = inputText;
    cursorPos = pos + insertChars.length;
  }

  // Backspace: remove the whole grapheme cluster BEFORE the caret (one emoji
  // family or composed char deletes atomically, never one code point of it).
  function deleteBeforeCursor() {
    const chars = Array.from(inputText);
    const pos = Math.max(0, Math.min(cursorPos, chars.length));
    if (pos === 0) return;
    const from = graphemeBoundaryBefore(inputText, pos);
    inputText = [...chars.slice(0, from), ...chars.slice(pos)].join("");
    composer.text = inputText;
    cursorPos = from;
  }

  // Forward delete: remove the grapheme cluster AT the caret (the Delete key).
  // The caret stays put, like every other terminal input.
  function deleteAtCursor() {
    const chars = Array.from(inputText);
    const pos = Math.max(0, Math.min(cursorPos, chars.length));
    if (pos >= chars.length) return;
    const to = graphemeBoundaryAfter(inputText, pos);
    inputText = [...chars.slice(0, pos), ...chars.slice(to)].join("");
    composer.text = inputText;
    cursorPos = pos;
  }

  function acceptCompletion() {
    const item = menu.filtered[clamp(menu.selected, 0, menu.filtered.length - 1)];
    if (!item) {
      resetMenu();
      rerenderInputRegion();
      return;
    }
    const insertText = String(item.insert_text ?? item.label ?? "");
    const accepted = acceptCompletionText(inputText, menu.tokenStart, cursorPos, insertText);
    inputText = accepted.text;
    composer.text = inputText;
    cursorPos = accepted.cursor;
    resetMenu();
    rerenderInputRegion();
  }

  function applyMenuKeyResult(result, keyName) {
    if (!result.handled) return false;
    if (result.action === "accept") {
      acceptCompletion();
      return true;
    }
    if (result.action === "accept_submit") {
      // Insert the highlighted command, then submit it — one Enter runs it.
      acceptCompletion();
      submitInput();
      return true;
    }
    if (result.action === "close" && keyName === "escape") {
      // Latch the dismissed token so updateMenuFromInput does not reopen the
      // menu on the very next keystroke for the same token. Only an explicit
      // Escape latches: Tab merely closing an empty "no matches" list must not
      // keep the menu shut once the token edits back into matches.
      menuDismissed = { kind: menu.kind, tokenStart: menu.tokenStart };
    }
    Object.assign(menu, result.menu);
    rerenderInputRegion();
    return true;
  }

  // Kitty-protocol terminals report keypad keys under distinct "kp*" names
  // (numpad Enter arrives as kpenter, never return); alias them onto the base
  // names so the branches below handle them like their main-keyboard twins.
  const KEYPAD_KEY_ALIASES = {
    kpenter: "return",
    kpup: "up",
    kpdown: "down",
    kpleft: "left",
    kpright: "right",
    kphome: "home",
    kpend: "end",
    kpdelete: "delete",
    kppageup: "pageup",
    kppagedown: "pagedown",
  };

  function installKeyboardHandlers() {
    renderer.keyInput.on("keypress", (rawKey) => {
      const keypadAlias = KEYPAD_KEY_ALIASES[rawKey?.name];
      const key = keypadAlias ? { ...rawKey, name: keypadAlias, sequence: "" } : rawKey;
      // Any key other than Up/Down ends a vertical-motion run, so the next Up/Down
      // recomputes the goal column from the caret's current visual position.
      if (key.name !== "up" && key.name !== "down") desiredVisualCol = null;
      // The approval overlay is modal and outranks the theme picker; only
      // Ctrl+C falls through (approvalKeyAction passes it) so the interrupt
      // path below keeps working while a decision is pending.
      if (approvalOverlay?.active) {
        if (handleApprovalKey(key)) return;
      }
      // The theme picker is modal: it consumes every key while open.
      if (themePicker?.active) {
        handleThemePickerKey(key.name);
        return;
      }
      if (sessionPicker?.active) {
        handleSessionPickerKey(key);
        return;
      }
      if (modelRoutingPicker?.active) {
        handleModelRoutingPickerKey(key.name);
        return;
      }
      if (modelPicker?.active) {
        handleModelPickerKey(key);
        return;
      }
      // Modified Enter is the newline chord — it must reach the newline branch
      // below even while a menu is open, or Alt+Enter would accept (and, for a
      // slash menu, accept AND submit) the highlighted completion instead.
      const newlineChord =
        key.name === "return" && Boolean(key.shift || key.option || key.meta || key.alt);
      if (menu.active && !newlineChord) {
        const menuResult = menuKeyAction(menu, key.name);
        if (applyMenuKeyResult(menuResult, key.name)) return;
      }
      // Completion owns Tab whenever its menu is open. Outside a menu, Tab on
      // a busy turn is the unambiguous "run next" chord; idle Tab stays inert.
      if (key.name === "tab" && turnActive) {
        submitInput("queue");
        return;
      }
      if (key.ctrl && key.name === "c") {
        // With text: clear the input. Empty: interrupt the in-flight turn.
        // Ctrl-D owns EOF/exit.
        if (inputText.length > 0) {
          setInput("");
          historyIndex = inputHistory.length;
          historyEditedRecall = false;
          resetMenu();
          rerenderInputRegion();
        } else {
          sendHostMessage({ type: "input.cancel" });
          rerenderInputRegion();
        }
        return;
      }
      if (key.ctrl && key.name === "d") {
        sendHostMessage({ type: "input.eof" });
        return;
      }
      if (key.ctrl && key.name === "l") {
        onFullRedraw?.();
        return;
      }
      if ((key.ctrl && key.name === "end") || (key.ctrl && key.name === "g")) {
        onJumpToLatest?.();
        return;
      }
      if (
        key.name === "end"
        && !key.ctrl && !key.meta && !key.alt && !key.option
        && isTranscriptHeld?.()
      ) {
        onJumpToLatest?.();
        return;
      }
      // Standard readline-style line editing. Caret motion re-derives the menu
      // (updateMenuFromInput) exactly like the edit branches: an open menu must
      // track the token under the NEW caret position, or a later accept would
      // splice a stale token range.
      if ((key.ctrl && key.name === "a") || key.name === "home") {
        cursorPos = lineStartIndex(inputText, cursorPos); // start of line
        updateMenuFromInput();
        rerenderInputRegion();
        return;
      }
      if ((key.ctrl && key.name === "e") || key.name === "end") {
        cursorPos = lineEndIndex(inputText, cursorPos); // end of line
        updateMenuFromInput();
        rerenderInputRegion();
        return;
      }
      if (
        (key.ctrl && (key.name === "u" || key.name === "k" || key.name === "w")) ||
        ((key.meta || key.alt || key.option) && key.name === "backspace")
      ) {
        // Ctrl+U cut to line start · Ctrl+K cut to line end · Ctrl+W /
        // Alt+Backspace delete the previous word.
        let from = cursorPos;
        let to = cursorPos;
        if (key.name === "k") to = lineEndIndex(inputText, cursorPos);
        else if (key.name === "u") from = lineStartIndex(inputText, cursorPos);
        else from = wordStartIndex(inputText, cursorPos);
        const removed = Array.from(inputText)
          .slice(Math.min(from, to), Math.max(from, to))
          .join("");
        if (removed) killBuffer = removed; // Ctrl+Y yanks the last kill back
        const edited = spliceOut(inputText, from, to);
        inputText = edited.text;
        composer.text = inputText;
        cursorPos = edited.cursor;
        detachHistoryToDraft();
        updateMenuFromInput();
        rerenderInputRegion();
        return;
      }
      // Yank: re-insert the last kill at the caret (readline Ctrl+Y), so a
      // mistyped Ctrl+U/K/W is recoverable instead of destroying the draft.
      if (key.ctrl && key.name === "y") {
        if (killBuffer) {
          insertAtCursor(killBuffer);
          detachHistoryToDraft();
          updateMenuFromInput();
          rerenderInputRegion();
        }
        return;
      }
      // Word-wise cursor movement (must precede the plain left/right branches,
      // which ignore modifiers). Ctrl+Left/Alt+Left/Alt+B back a word;
      // Ctrl+Right/Alt+Right/Alt+F forward a word.
      const wordBack =
        (key.ctrl && key.name === "left") ||
        ((key.meta || key.alt || key.option) && (key.name === "b" || key.name === "left"));
      const wordForward =
        (key.ctrl && key.name === "right") ||
        ((key.meta || key.alt || key.option) && (key.name === "f" || key.name === "right"));
      if (wordBack || wordForward) {
        cursorPos = wordBack
          ? wordStartIndex(inputText, cursorPos)
          : wordEndIndex(inputText, cursorPos);
        updateMenuFromInput();
        rerenderInputRegion();
        return;
      }
      if (key.name === "escape") {
        // Interrupt the in-flight turn (reuses the cancel path on the Python side).
        sendHostMessage({ type: "input.cancel" });
        return;
      }
      if (key.name === "return") {
        // Shift+Enter (kitty-protocol terminals) and Alt/Option+Enter insert a
        // newline; legacy terminals cannot report Shift+Enter and are unaffected.
        if (key.shift || key.option || key.meta || key.alt) {
          insertAtCursor("\n");
          detachHistoryToDraft();
          updateMenuFromInput();
          rerenderInputRegion();
          return;
        }
        submitInput();
        return;
      }
      if (key.name === "left") {
        moveCaretHorizontal(-1);
        updateMenuFromInput();
        rerenderInputRegion();
        return;
      }
      if (key.name === "right") {
        moveCaretHorizontal(1);
        updateMenuFromInput();
        rerenderInputRegion();
        return;
      }
      if (key.name === "up") {
        // Move the caret up a line; only switch history when already on the very
        // first character (cursorPos === 0).
        if (cursorPos === 0 || !moveCaretVertical(-1)) recallHistory(-1);
        else {
          updateMenuFromInput();
          rerenderInputRegion();
        }
        return;
      }
      if (key.name === "down") {
        // Move the caret down a line; only switch history when already at the very
        // end of the input.
        if (cursorPos === Array.from(inputText).length || !moveCaretVertical(1)) recallHistory(1);
        else {
          updateMenuFromInput();
          rerenderInputRegion();
        }
        return;
      }
      if (key.name === "pageup") {
        if (onTranscriptScroll) onTranscriptScroll(-10);
        else conversationBox?.scrollBy({ x: 0, y: -10 });
        renderer.requestRender?.();
        return;
      }
      if (key.name === "pagedown") {
        if (onTranscriptScroll) onTranscriptScroll(10);
        else conversationBox?.scrollBy({ x: 0, y: 10 });
        renderer.requestRender?.();
        return;
      }
      if (key.name === "backspace") {
        deleteBeforeCursor();
        detachHistoryToDraft();
        updateMenuFromInput();
        rerenderInputRegion();
        return;
      }
      // Forward word delete (Alt+D / Ctrl+Delete) — must precede the plain Delete
      // branch, which ignores modifiers.
      if (
        ((key.meta || key.alt || key.option) && key.name === "d") ||
        (key.ctrl && key.name === "delete")
      ) {
        const removed = Array.from(inputText)
          .slice(cursorPos, wordEndIndex(inputText, cursorPos))
          .join("");
        if (removed) killBuffer = removed;
        const edited = spliceOut(inputText, cursorPos, wordEndIndex(inputText, cursorPos));
        inputText = edited.text;
        composer.text = inputText;
        cursorPos = edited.cursor;
        detachHistoryToDraft();
        updateMenuFromInput();
        rerenderInputRegion();
        return;
      }
      if (key.name === "delete") {
        deleteAtCursor(); // forward-delete the character at the caret
        detachHistoryToDraft();
        updateMenuFromInput();
        rerenderInputRegion();
        return;
      }
      const printable = key.sequence ?? key.name ?? "";
      // Only single keystrokes reach here (paste has its own handler below).
      // Reject control bytes: Tab (\t) and the ESC sequences from unhandled
      // special keys (F-keys etc.) would otherwise be inserted verbatim and end
      // up submitted in the message. Real typed text is printable.
      const isControlKey = /[\u0000-\u001f\u007f]/u.test(printable);
      if (printable.length > 0 && !isControlKey && !key.ctrl && !key.meta && key.name !== "space") {
        insertAtCursor(printable);
        detachHistoryToDraft();
        updateMenuFromInput();
        rerenderInputRegion();
      } else if (key.name === "space" && !key.ctrl && !key.meta) {
        // Modified space chords (Ctrl+Space arrives as a NUL byte named
        // "space", Alt+Space as ESC+space with meta set) are not typed text.
        insertAtCursor(" ");
        detachHistoryToDraft();
        updateMenuFromInput();
        rerenderInputRegion();
      }
    });

    const decoder = new TextDecoder();
    renderer.keyInput.on("paste", (event) => {
      // The theme picker is modal for keypresses; paste must not slip past it
      // and mutate the draft underneath the overlay. The approval overlay is
      // modal the same way.
      if (
        themePicker?.active
        || approvalOverlay?.active
        || modelRoutingPicker?.active
        || modelPicker?.active
      ) return;
      // Sanitize pasted text: real terminals (and tmux paste-buffer) transmit
      // pasted newlines as bare CR, so normalize CRLF/CR to LF FIRST — the
      // control-byte strip below removes CR and would otherwise concatenate
      // the pasted lines. Then strip ANSI/escape sequences and C0/DEL control
      // bytes (e.g. pasting colored terminal output or a log) so they cannot
      // corrupt the input or get submitted to the model. Newlines and tabs are
      // preserved, so multi-line and indented pastes are unaffected.
      const pasted = stripTerminalControls(
        decoder.decode(event.bytes).replace(/\r\n?/g, "\n"),
      );
      if (!pasted) return;
      insertAtCursor(pasted);
      detachHistoryToDraft();
      if (pasted.includes("\n")) resetMenu();
      else updateMenuFromInput();
      rerenderInputRegion();
    });
  }

  // Bundle keyboard-install + blink-start + initial render. (In the old main.mjs
  // buildLayout() did the initial render, and main() called the install + blink.)
  function install() {
    if (installed) return;
    installed = true;
    installKeyboardHandlers();
    startCursorBlink();
    notifyContextObserver();
    notifyRouterObserver();
    rerenderInputRegion();
  }

  function setComposerState(message) {
    Object.assign(composer, {
      placeholder: String(message.placeholder ?? composer.placeholder),
      text: String(message.text ?? composer.text),
      disabled: Boolean(message.disabled ?? composer.disabled),
    });
    // Only a frame that actually carries text may reposition the caret. The
    // renderer sends text-less disabled toggles at every turn begin/end; those
    // must not yank the caret to the end of an in-progress draft (or re-derive
    // the completion menu) while the user types ahead mid-turn.
    if (message.text !== undefined) {
      // Route text through setInput so inputText/composer.text/cursorPos stay in
      // sync (caret lands at the end of any prefilled text instead of drifting).
      setInput(composer.text);
      updateMenuFromInput();
    }
    rerenderInputRegion();
  }

  function setTurnActive(message) {
    turnActive = Boolean(message?.active);
    if (!turnActive && modelRoutingState.next) {
      modelRoutingState.current = modelRoutingState.next;
      modelRoutingState.next = null;
    }
    rerenderInputRegion();
  }

  function setModelRoutingState(message) {
    const mode = String(message?.mode ?? "").toLowerCase();
    if (!["direct", "router", "ensemble"].includes(mode)) return;
    modelRoutingState.busy = Boolean(message?.busy ?? false);
    if (turnActive && mode !== modelRoutingState.current) {
      modelRoutingState.next = mode;
    } else {
      modelRoutingState.current = mode;
      modelRoutingState.next = null;
    }
    rerenderInputRegion();
  }

  function addAttachmentState(message) {
    const attachment = normalizedAttachment(message);
    if (!attachment.id) return;
    attachments.set(attachment.id, attachment);
    rerenderInputRegion();
  }

  function updateAttachmentState(message) {
    const id = String(message?.id ?? "");
    const current = attachments.get(id);
    if (!current) return;
    attachments.set(id, normalizedAttachment(message, current));
    rerenderInputRegion();
  }

  function removeAttachmentState(id) {
    if (!attachments.delete(String(id ?? ""))) return;
    rerenderInputRegion();
  }

  function clearAttachmentStates(status = null) {
    const target = status === null || status === undefined ? null : String(status);
    let changed = false;
    for (const [id, attachment] of attachments) {
      if (target === null || attachment.status === target) {
        attachments.delete(id);
        changed = true;
      }
    }
    if (changed) rerenderInputRegion();
  }

  function setRouterState(message) {
    Object.assign(routerState, {
      model: String(message.model ?? routerState.model),
      route: String(message.route ?? routerState.route),
      saving: String(message.saving ?? routerState.saving),
      context: String(message.context ?? routerState.context),
      style: String(message.style ?? routerState.style),
      baselineModel: String(message.baseline_model ?? routerState.baselineModel),
      source: String(message.source ?? routerState.source),
      routingApplied: message.routing_applied ?? routerState.routingApplied,
      rolloutPhase: String(message.rollout_phase ?? routerState.rolloutPhase),
      io: String(message.io ?? routerState.io),
    });
    notifyRouterObserver();
    rerenderInputRegion();
  }

  function setContextState(message) {
    contextState = normalizeContextUpdate(message, contextState);
    notifyContextObserver();
    rerenderInputRegion();
  }

  function setCompletionContext(message) {
    completionContext.catalog = Array.isArray(message.catalog) ? message.catalog : [];
    completionContext.files = Array.isArray(message.files) ? message.files : [];
    completionContext.filtersSensitivePaths = Boolean(
      message.filters_sensitive_paths ?? completionContext.filtersSensitivePaths,
    );
    if (menu.active) {
      updateMenuFromInput();
      rerenderInputRegion();
    }
  }

  function applyCompletionResponse(message) {
    if (
      !menu.active
      || menu.kind !== String(message.kind ?? "")
      || shouldDropResponse(message.request_id, menu.requestSeq)
    ) {
      return;
    }
    // The user may have arrow-navigated the locally filtered list while this
    // response was in flight: follow the highlighted ITEM into the new list
    // instead of keeping a raw index that now points at an unrelated entry.
    const previous = menu.filtered[menu.selected];
    menu.filtered = Array.isArray(message.items) ? message.items : [];
    reanchorMenuSelection(previous);
    rerenderInputRegion();
  }

  // Composer-relevant resize work: re-render the footer. (conversationBox height
  // resize stays in main.mjs.)
  function onResize() {
    rerenderInputRegion();
  }

  // Root overlays and transcript-only stream frames can repaint after the
  // footer without rebuilding it. Reassert the hardware caret from the same
  // wrapped composer viewport so the terminal cursor cannot be stranded on
  // the last transcript/overlay cell that happened to change.
  function syncCursor() {
    syncTerminalCursorToCaret(composerViewport());
  }

  return {
    install,
    rerender: rerenderInputRegion,
    setComposerState,
    setTurnActive,
    addAttachmentState,
    updateAttachmentState,
    removeAttachmentState,
    clearAttachmentStates,
    setContextState,
    setRouterState,
    setModelRoutingState,
    setCompletionContext,
    applyCompletionResponse,
    onResize,
    syncCursor,
    openThemePicker,
    openSessionPicker,
    openModelRoutingPicker,
    openModelPicker,
    openApprovalOverlay,
    dismissApprovalOverlay,
    applyHostTheme,
  };
}
