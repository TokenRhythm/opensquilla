import { createBlock } from "./blockRegistry.mjs";
import { STATUS_PULSE_FRAMES, THEME } from "./theme.mjs";
import { TOOL_INDENT, clipToCells, stripTerminalControls } from "./primitives.mjs";
import { destroyRenderable } from "./renderableLifecycle.mjs";
import { rendererViewportSnapshot } from "./screenMode.mjs";

// Block kinds that render OUTSIDE the assistant's single per-turn card: the
// prompt is the user's own compact row and the usage summary folds into the
// card footer. Everything else — answer markdown, intermediate narration, tool
// calls, the reasoning marker, errors, and any kind this host does not know yet
// (a newer Python may add block kinds) — shares ONE continuous left-border
// gutter so a multi-step turn reads as one assistant block (opencode/codex
// style) instead of a stack of repeated cards. Unknown kinds default INTO the
// card so a protocol addition can never seal it mid-turn; only the known
// trailing kind (usage) closes it.
const OUT_OF_CARD_KINDS = new Set(["prompt", "usage"]);
const DETAIL_KINDS = new Set(["thinking", "reasoning", "tool", "ensemble"]);

export function isOutOfCardKind(kind) {
  return OUT_OF_CARD_KINDS.has(kind);
}

function yogaRect(renderable) {
  const value = renderable?.getLayoutNode?.()?.getComputedLayout?.();
  if (!value || typeof value !== "object") return null;
  const top = Number(value.top);
  const height = Number(value.height);
  if (!Number.isFinite(top) || !Number.isFinite(height)) return null;
  return { top, height: Math.max(0, height) };
}

function offsetWithin(renderable, ancestor) {
  let current = renderable;
  let top = 0;
  while (current && current !== ancestor) {
    const rect = yogaRect(current);
    if (!rect) return null;
    top += rect.top;
    current = current.parent;
  }
  return current === ancestor ? top : null;
}

function walkRenderables(root, visit) {
  for (const child of root?.getChildren?.() ?? []) {
    visit(child);
    walkRenderables(child, visit);
  }
}

export function createTurnView(deps, id) {
  const { renderer, BoxRenderable, TextRenderable, MarkdownRenderable, syntaxStyle, conversationBox } = deps;
  const contentWidth = typeof deps.contentWidth === "function"
    ? deps.contentWidth
    : () => renderer.terminalWidth;
  const agentLabel = typeof deps.agentLabel === "function"
    ? deps.agentLabel
    : () => "squilla";
  const viewport = typeof deps.viewport === "function"
    ? deps.viewport
    : () => rendererViewportSnapshot(renderer);
  // marginTop gives each turn a blank line of vertical rhythm so turns read as
  // distinct groups (proximity) and the conversation has room to breathe.
  const box = new BoxRenderable(renderer, { id: `turn-${id}`, flexDirection: "column", marginTop: 1, paddingLeft: 1, paddingRight: 1 });
  conversationBox.add(box);
  const blocks = new Map();      // blockId -> { kind, r, idPrefix }
  const runningTools = new Set(); // toolBlock renderers animating
  const runningReasoning = new Set(); // reasoning markers animating

  // One card per assistant turn: a short "╭ squilla" label, a single
  // left-border gutter that runs unbroken through narration and tool calls, and
  // a "╰ …" footer that carries the usage summary. The chrome is deliberately
  // width-INDEPENDENT — a full-width header rule wraps a stray dash onto the
  // next row the moment the scrollbar steals a viewport column, so no rule may
  // depend on the terminal width. The card opens lazily on the first in-card
  // block so a turn that only echoes a prompt never draws an empty card.
  let cardBody = null;
  let cardTop = null; // the "╭ squilla" header label
  let cardBot = null; // the "╰ …" footer (usage summary / cancelled marker)
  let cancelNode = null; // "⚠ cancelled" fallback for card-less views (queued prompts)
  let usageText = null; // trailing usage summary, folded into the footer
  const gapRows = []; // prose<->procedure spacer rows (detailText)
  let cardOpen = false;
  let cardClosed = false;
  let cardCancelled = false;
  let turnFinished = false;
  let detailsExpanded = false;
  let lastInCardKind = null; // for prose<->procedure spacing inside the card
  let gapSeq = 0;
  let lastRelayoutWidth = contentWidth(); // block content is clipped at this width
  let lastRelayoutHeight = viewport().height; // live reasoning density follows height

  function openCard() {
    if (cardOpen) return;
    cardOpen = true;
    cardTop = new TextRenderable(renderer, { id: `turn-${id}-cardtop`, content: cardHeaderContent(), fg: frameColor() });
    box.add(cardTop);
    cardBody = new BoxRenderable(renderer, { id: `turn-${id}-cardbody`, width: "100%", flexDirection: "column", border: ["left"], borderColor: frameColor(), paddingLeft: 1, flexShrink: 0 });
    box.add(cardBody);
  }

  // The connected rail is the active-turn affordance. Once a turn finishes,
  // settle its frame to the muted transcript color so only the currently
  // running card carries the bright answer accent. Errors/cancellation keep
  // their semantic warning color in the footer.
  function frameColor() {
    return turnFinished ? THEME.muted : THEME.answerFrame;
  }

  function cardHeaderContent() {
    const safe = stripTerminalControls(String(agentLabel() ?? ""))
      .replace(/\s+/g, " ")
      .trim() || "squilla";
    // Reserve the corner/prefix and the turn box's horizontal breathing room.
    // clipToCells is display-width aware, so CJK/emoji identity cannot bleed
    // under the wide context rail.
    const budget = Math.max(1, (Number(contentWidth()) || 1) - 4);
    return `╭ ${clipToCells(safe, budget)}`;
  }

  function applyFrameColor() {
    if (cardTop) cardTop.fg = frameColor();
    if (cardBody) cardBody.borderColor = frameColor();
  }

  function footerContent() {
    if (cardCancelled) {
      return usageText ? `╰ ⚠ cancelled · ${usageText}` : "╰ ⚠ cancelled";
    }
    return usageText ? `╰ ${usageText}` : "╰";
  }

  function footerColor() {
    if (cardCancelled) return THEME.warning;
    return usageText || turnFinished ? THEME.muted : THEME.answerFrame;
  }

  function standaloneUsageRow() {
    if (!usageText) return;
    const row = new TextRenderable(renderer, { id: `turn-${id}-usage`, content: `${TOOL_INDENT}${usageText}`, fg: THEME.muted });
    box.add(row);
  }

  function closeCard() {
    if (!cardOpen) {
      // No card to close (e.g. a turn that only echoed a prompt): a usage
      // summary still deserves its receipt row.
      standaloneUsageRow();
      usageText = null;
      return;
    }
    if (cardClosed) {
      // Already closed: a late usage/cancel still refreshes the footer text.
      if (cardBot) {
        // TextRenderable keeps already-shaped spans when fg changes after its
        // first paint. Recreate this one trailing row so a late cancellation
        // cannot retain the completed footer's muted color.
        destroyRenderable(box, cardBot);
        cardBot = new TextRenderable(renderer, {
          id: `turn-${id}-cardbot`,
          content: footerContent(),
          fg: footerColor(),
        });
        box.add(cardBot);
        renderer.requestRender?.();
      }
      return;
    }
    // A body that kept no children would close into an empty agent shell (for
    // example, a future block kind that emitted no payload). Drop the chrome
    // instead — keeping any usage receipt as a plain row — and let a later
    // in-card block simply re-open a fresh card.
    const kept = cardBody?.getChildrenCount?.() ?? cardBody?.getChildren?.().length ?? 0;
    if (kept === 0) {
      destroyRenderable(box, cardTop);
      destroyRenderable(box, cardBody);
      cardTop = cardBody = null;
      cardOpen = false;
      lastInCardKind = null;
      standaloneUsageRow();
      usageText = null;
      renderer.requestRender?.();
      return;
    }
    cardClosed = true;
    cardBot = new TextRenderable(renderer, { id: `turn-${id}-cardbot`, content: footerContent(), fg: footerColor() });
    box.add(cardBot);
    renderer.requestRender?.();
  }

  function ctxFor(blockId, kind) {
    // In-card blocks draw into the shared bordered body so the gutter stays
    // continuous; everything else draws straight into the turn box.
    const target = !isOutOfCardKind(kind) && cardBody ? cardBody : box;
    return {
      renderer,
      BoxRenderable,
      TextRenderable,
      MarkdownRenderable,
      syntaxStyle,
      contentWidth,
      viewport,
      box: target,
      idPrefix: `turn-${id}-${blockId}`,
    };
  }

  function setDetailsExpanded(value) {
    detailsExpanded = Boolean(value);
    for (const entry of blocks.values()) {
      if (DETAIL_KINDS.has(entry.kind)) entry.r.toggleExpanded?.(detailsExpanded);
    }
    renderer.requestRender?.();
    return detailsExpanded;
  }

  function toggleDetails() {
    return setDetailsExpanded(!detailsExpanded);
  }

  function refreshContext() {
    if (cardTop) cardTop.content = cardHeaderContent();
    renderer.requestRender?.();
  }

  // Resolve semantic transcript anchors from the renderables a block really
  // owns. Reading Yoga's computed layout (after the scroller's pre-paint
  // layout commit) makes this stable across Markdown rewrap, Ctrl+O detail
  // expansion, and same-turn streaming growth without adding layout wrappers
  // that could perturb the product visuals.
  function blockSpans() {
    const spans = [];
    for (const [blockId, entry] of blocks) {
      const prefix = `${entry.idPrefix}-`;
      let start = Number.POSITIVE_INFINITY;
      let end = Number.NEGATIVE_INFINITY;
      walkRenderables(box, (node) => {
        if (!String(node?.id ?? "").startsWith(prefix)) return;
        const localTop = offsetWithin(node, box);
        const rect = yogaRect(node);
        if (localTop === null || !rect || rect.height <= 0) return;
        start = Math.min(start, localTop);
        end = Math.max(end, localTop + rect.height);
      });
      if (!Number.isFinite(start) || !Number.isFinite(end)) continue;
      spans.push({ block_id: String(blockId), start, end: Math.max(start + 1, end) });
    }
    return spans.sort((left, right) => left.start - right.start);
  }

  function anchorAtLocalRow(row) {
    const local = Math.max(0, Number(row) || 0);
    const spans = blockSpans();
    if (!spans.length) return null;
    let selected = spans[0];
    for (const span of spans) {
      if (span.start > local) break;
      selected = span;
    }
    return {
      block_id: selected.block_id,
      row_within_block: Math.max(0, local - selected.start),
    };
  }

  function localRowForAnchor(anchor) {
    const blockId = String(anchor?.block_id ?? "");
    const span = blockSpans().find((candidate) => candidate.block_id === blockId);
    if (!span) return null;
    return span.start + Math.max(0, Number(anchor?.row_within_block) || 0);
  }

  return {
    box,
    turnId: String(id),
    ended: false,
    begin(blockId, kind, meta) {
      if (kind === "usage") {
        // The usage summary is the card's own trailing line, not a block: fold
        // it into the "╰ …" footer so the card closes into its receipt instead
        // of a floating row. append/update/end for this id stay safe no-ops.
        usageText = stripTerminalControls(String(meta?.text ?? "")).trim() || null;
        closeCard();
        return;
      }
      if (!isOutOfCardKind(kind)) {
        openCard();
        // Separate the markdown answer (prose) from procedure rows (tools and
        // narration) with one blank gutter row, but pack consecutive procedure
        // rows tight — mirrors opencode's part spacing without an even gap
        // between every step. The card border keeps the gutter continuous.
        if (lastInCardKind !== null && (kind === "answer") !== (lastInCardKind === "answer")) {
          const gap = new TextRenderable(renderer, { id: `turn-${id}-gap-${gapSeq++}`, content: TOOL_INDENT, fg: THEME.detailText });
          cardBody.add(gap);
          gapRows.push(gap);
        }
        lastInCardKind = kind;
      }
      const context = ctxFor(blockId, kind);
      const r = createBlock(kind, context);
      blocks.set(blockId, { kind, r, idPrefix: context.idPrefix });
      r.begin(meta ?? {});
      if (DETAIL_KINDS.has(kind) && detailsExpanded) r.toggleExpanded?.(true);
      if (kind === "tool") runningTools.add(r);
      if (kind === "reasoning") runningReasoning.add(r);
    },
    append(blockId, delta) { blocks.get(blockId)?.r.append(delta); },
    update(blockId, patch) {
      const entry = blocks.get(blockId);
      if (!entry) return;
      entry.r.update(patch);
      if (entry.kind === "tool" && (patch?.status === "ok" || patch?.status === "error")) runningTools.delete(entry.r);
    },
    end(blockId) {
      const entry = blocks.get(blockId);
      if (!entry) return;
      entry.r.end();
      if (entry.kind === "tool") runningTools.delete(entry.r);
      if (entry.kind === "reasoning") runningReasoning.delete(entry.r);
    },
    // Close the single per-turn card once the turn is over (the runtime calls
    // this on turn.end). Idempotent and a no-op when no card ever opened. A
    // cancelled turn (Esc mid-stream) closes into a "╰ ⚠ cancelled" footer so
    // the transcript records that this answer was cut short; a card-less view
    // (a discarded queued prompt) gets a standalone marker row instead.
    finish(cancelled) {
      if (cancelled) cardCancelled = true;
      // A terminal turn state is authoritative even if a provider omitted a
      // trailing block.end on an error path. Settle every renderer so no tool
      // pulse or bright reasoning/narration state survives in history; block
      // end methods are intentionally idempotent for the normal already-ended
      // path.
      for (const entry of blocks.values()) entry.r.end?.();
      runningTools.clear();
      runningReasoning.clear();
      turnFinished = true;
      closeCard();
      applyFrameColor();
      if (cancelled && !cardOpen && !cancelNode) {
        cancelNode = new TextRenderable(renderer, { id: `turn-${id}-cancelled`, content: `${TOOL_INDENT}⚠ cancelled`, fg: THEME.warning });
        box.add(cancelNode);
        renderer.requestRender?.();
      }
    },
    // Re-clip width-clipped block content to the current terminal width on
    // resize. A height-only change is cheaper: only active reasoning depends
    // on terminal height (its live peek is 3–8 rows), so completed history and
    // unrelated blocks skip text-buffer work.
    relayout() {
      const width = contentWidth();
      const height = viewport().height;
      const widthChanged = width !== lastRelayoutWidth;
      const heightChanged = height !== lastRelayoutHeight;
      if (!widthChanged && !heightChanged) return;
      lastRelayoutWidth = width;
      lastRelayoutHeight = height;
      for (const entry of blocks.values()) {
        if (widthChanged || runningReasoning.has(entry.r)) entry.r.relayout?.();
      }
      renderer.requestRender?.();
    },
    // Live /theme switch: re-point this turn's card chrome at the (in-place
    // updated) THEME, then let each block recolor its own nodes. Existing
    // renderables captured their fg at creation, so without this a dark→light
    // switch leaves prior transcript unreadable on the new background.
    recolor() {
      applyFrameColor();
      if (cardBot) cardBot.fg = footerColor();
      if (cancelNode) cancelNode.fg = THEME.warning;
      for (const gap of gapRows) gap.fg = THEME.detailText;
      for (const entry of blocks.values()) entry.r.recolor?.();
    },
    refreshPulse(frame) {
      const toolGlyph = STATUS_PULSE_FRAMES.tool[frame % STATUS_PULSE_FRAMES.tool.length];
      const thinkingGlyph = STATUS_PULSE_FRAMES.thinking[frame % STATUS_PULSE_FRAMES.thinking.length];
      for (const r of runningTools) r.setGlyph(toolGlyph);
      for (const r of runningReasoning) r.setGlyph(thinkingGlyph);
    },
    dispose() {
      destroyRenderable(conversationBox, box);
    },
    // One transcript-wide command can expand/collapse retained process detail
    // without moving focus away from the composer. Per-block APIs remain
    // available for future mouse/focus affordances and deterministic tests.
    setDetailsExpanded,
    toggleDetails,
    refreshContext,
    layoutTop() {
      return Math.max(0, Number(yogaRect(box)?.top) || 0);
    },
    measuredRows() {
      return Math.max(1, Number(yogaRect(box)?.height ?? box?.height) || 1);
    },
    anchorAtRow: anchorAtLocalRow,
    rowForAnchor: localRowForAnchor,
    get detailsExpanded() { return detailsExpanded; },
    blockState(blockId) {
      const entry = blocks.get(blockId);
      if (!entry) return null;
      return {
        kind: entry.kind,
        isExpanded: Boolean(entry.r.isExpanded),
        rawText: entry.r.rawText ?? "",
        hiddenLineCount: Number(entry.r.hiddenLineCount ?? 0),
      };
    },
  };
}

// Decides which turn view receives each protocol event. Kept apart from the
// renderer wiring so queued-prompt routing and late-block tolerance are plain
// logic: newView(id) creates a view (createTurnView bound to real deps).
export function createTurnFlow(newView) {
  const turns = []; // every view ever created, for resize reflow + theme recolor
  const turnsById = new Map();
  // Scroll anchors can be captured against an optimistic client id just before
  // prompt.state binds the durable Gateway turn id. Routing must forget the
  // optimistic id, but anchor restoration still needs that alias for the life
  // of this retained view.
  const anchorAliasesById = new Map();
  const promptsByClientMessageId = new Map();
  let active = null;
  let detailsExpanded = false;

  function create(id) {
    const view = newView(id);
    view.setDetailsExpanded?.(detailsExpanded);
    turns.push(view);
    if (id !== undefined && id !== null && String(id)) {
      turnsById.set(String(id), view);
      anchorAliasesById.set(String(id), view);
    }
    return view;
  }

  function bindIdentity(view, id, clientMessageId) {
    if (!view) return null;
    const durableId = id === undefined || id === null ? "" : String(id);
    const clientId = clientMessageId === undefined || clientMessageId === null
      ? "" : String(clientMessageId);
    if (durableId) {
      if (view.turnId) {
        anchorAliasesById.set(String(view.turnId), view);
        turnsById.delete(String(view.turnId));
      }
      view.turnId = durableId;
      turnsById.set(durableId, view);
      anchorAliasesById.set(durableId, view);
    }
    if (clientId) promptsByClientMessageId.set(clientId, view);
    return view;
  }

  function ensure(id, clientMessageId) {
    const durableId = id === undefined || id === null ? "" : String(id);
    const clientId = clientMessageId === undefined || clientMessageId === null
      ? "" : String(clientMessageId);
    if (!active || active.ended) {
      active = (clientId && promptsByClientMessageId.get(clientId))
        || (durableId && turnsById.get(durableId))
        || create(durableId || clientId || undefined);
      bindIdentity(active, durableId, clientId);
    }
    return active;
  }

  function setDetailsExpanded(value) {
    detailsExpanded = Boolean(value);
    for (const view of turns) view.setDetailsExpanded?.(detailsExpanded);
    return detailsExpanded;
  }

  function toggleDetails() {
    return setDetailsExpanded(!detailsExpanded);
  }

  function refreshContext() {
    for (const view of turns) view.refreshContext?.();
  }

  function release(view) {
    if (!view) return false;
    const turnIndex = turns.indexOf(view);
    if (turnIndex >= 0) turns.splice(turnIndex, 1);
    for (const [id, candidate] of turnsById) {
      if (candidate === view) turnsById.delete(id);
    }
    for (const [id, candidate] of anchorAliasesById) {
      if (candidate === view) anchorAliasesById.delete(id);
    }
    for (const [id, candidate] of promptsByClientMessageId) {
      if (candidate === view) promptsByClientMessageId.delete(id);
    }
    if (active === view) active = null;
    view.dispose?.();
    return turnIndex >= 0;
  }

  function releaseEndedViews() {
    let released = 0;
    for (const view of [...turns]) {
      if (!view.ended) continue;
      if (release(view)) released += 1;
    }
    return released;
  }

  function measuredRows(view) {
    if (typeof view?.measuredRows === "function") return view.measuredRows();
    return Math.max(
      1,
      Number(view?.box?.height ?? view?.box?.contentHeight ?? 1) || 1,
    );
  }

  function positionedTurns() {
    let fallbackTop = 0;
    return turns.map((view) => {
      const explicit = typeof view?.layoutTop === "function"
        ? Number(view.layoutTop())
        : Number.NaN;
      const top = Number.isFinite(explicit) ? Math.max(0, explicit) : fallbackTop;
      const height = measuredRows(view);
      fallbackTop = Math.max(fallbackTop, top + height);
      return { view, top, height };
    });
  }

  function anchorAtRow(row) {
    const absolute = Math.max(0, Number(row) || 0);
    let selected = null;
    for (const positioned of positionedTurns()) {
      if (positioned.top > absolute) break;
      selected = positioned;
      if (absolute < positioned.top + positioned.height) break;
    }
    if (selected) {
      const local = Math.max(0, absolute - selected.top);
      const semantic = selected.view.anchorAtRow?.(local);
      if (semantic?.block_id) {
        return {
          turn_id: String(selected.view.turnId ?? "turn"),
          block_id: String(semantic.block_id),
          row_within_block: Math.max(0, Number(semantic.row_within_block) || 0),
          _absolute_row: absolute,
        };
      }
    }
    return {
      turn_id: "viewport",
      block_id: "row",
      row_within_block: absolute,
      _absolute_row: absolute,
    };
  }

  function rowForAnchor(anchor) {
    const turnId = String(anchor?.turn_id ?? "");
    const anchoredView = anchorAliasesById.get(turnId) ?? null;
    for (const positioned of positionedTurns()) {
      if (positioned.view === anchoredView || String(positioned.view.turnId ?? "") === turnId) {
        const local = positioned.view.rowForAnchor?.(anchor);
        if (Number.isFinite(Number(local))) {
          return positioned.top + Math.max(0, Number(local));
        }
        break;
      }
    }
    return Math.max(
      0,
      Number(anchor?._absolute_row ?? anchor?.row_within_block) || 0,
    );
  }

  return {
    turns,
    turnsById,
    promptsByClientMessageId,
    active: () => active,
    ensure,
    setDetailsExpanded,
    toggleDetails,
    refreshContext,
    release,
    releaseEndedViews,
    anchorAtRow,
    rowForAnchor,
    get detailsExpanded() { return detailsExpanded; },
    // block.begin after turn.end is a late straggler (e.g. a trailing usage
    // line) that belongs to the turn that just ended. Routing it there keeps
    // it from spawning a fresh un-ended turn that would absorb the next
    // prompt.echo into the same box.
    turnForBlock(id) {
      return active && active.ended ? active : ensure(id);
    },
    // prompt.echo while a turn is still streaming means the submission was
    // QUEUED behind it: give the echo its own view — reusing the live turn
    // would seal its card mid-stream and glue its usage line to the new
    // prompt. ensure() then adopts queued views in order as their turns begin.
    turnForPrompt(id, clientMessageId) {
      const clientId = clientMessageId === undefined || clientMessageId === null
        ? "" : String(clientMessageId);
      if (clientId && promptsByClientMessageId.has(clientId)) {
        return promptsByClientMessageId.get(clientId);
      }
      if (active && !active.ended) {
        const view = create(id || clientId || undefined);
        bindIdentity(view, id, clientId);
        return view;
      }
      const view = ensure(id, clientId);
      bindIdentity(view, id, clientId);
      return view;
    },
    bindPrompt(id, clientMessageId) {
      const durableId = id === undefined || id === null ? "" : String(id);
      const clientId = clientMessageId === undefined || clientMessageId === null
        ? "" : String(clientMessageId);
      const view = (clientId && promptsByClientMessageId.get(clientId))
        || (durableId && turnsById.get(durableId))
        || null;
      return bindIdentity(view, durableId, clientId);
    },
    endTurn(cancelled = false) {
      // A cancelled turn.end only comes from the cancel path (Esc / empty
      // Ctrl+C), which already discarded every queued submission server-side.
      // Invalidate their views too, or ensure() would adopt a stale discarded
      // prompt's box for the NEXT real submission — fusing the new prompt and
      // its whole answer under a dead prompt card. Marking each flushed view
      // cancelled makes the discarded prompt visibly unanswered.
      if (cancelled) {
        const pendingViews = new Set(promptsByClientMessageId.values());
        for (const view of pendingViews) {
          if (view === active) continue;
          view.finish?.(true);
          view.ended = true;
        }
        promptsByClientMessageId.clear();
      }
      if (!active) return;
      active.finish?.(cancelled);
      active.ended = true;
    },
  };
}
