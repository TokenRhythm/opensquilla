import { THEME } from "./theme.mjs";
import { TOOL_INDENT, stripTerminalControls } from "./primitives.mjs";
import { createPromptBlock } from "./blocks/promptBlock.mjs";
import { createThinkingBlock } from "./blocks/thinkingBlock.mjs";
import { createReasoningBlock } from "./blocks/reasoningBlock.mjs";
import { createToolBlock } from "./blocks/toolBlock.mjs";
import { createEnsembleBlock } from "./blocks/ensembleBlock.mjs";
import { createAnswerBlock } from "./blocks/answerBlock.mjs";
import { createUsageBlock } from "./blocks/usageBlock.mjs";
import { createErrorBlock } from "./blocks/errorBlock.mjs";

const FACTORIES = {
  prompt: createPromptBlock,
  // Intermediate narration the model speaks between tool calls. It streams in
  // full, then long completed narration keeps a retained expandable preview.
  thinking: createThinkingBlock,
  // Extended-thinking process: a live tail while running and a bounded preview
  // after completion; expansion always reconstructs the full retained payload.
  reasoning: createReasoningBlock,
  tool: createToolBlock,
  ensemble: createEnsembleBlock,
  answer: createAnswerBlock,
  usage: createUsageBlock,
  error: createErrorBlock,
};

// The block kind set is a Python→JS protocol surface: a newer renderer emitting
// a kind this host does not know must degrade to visible dim plain text, not
// throw mid-dispatch (which would drop the block's content entirely and leave
// the turn card half-mutated).
function createFallbackBlock(ctx) {
  const { renderer, TextRenderable, box, idPrefix } = ctx;
  let node = null;
  let text = "";
  const show = () => {
    const content = `${TOOL_INDENT}${stripTerminalControls(text)}`;
    if (node) {
      node.content = content;
    } else {
      node = new TextRenderable(renderer, { id: `${idPrefix}-fallback`, content, fg: THEME.detailText });
      box.add(node);
    }
    renderer.requestRender?.();
  };
  return {
    get rawText() { return text; },
    get isExpanded() { return false; },
    get hiddenLineCount() { return 0; },
    begin(meta) {
      const seed = String(meta?.text ?? "");
      if (seed) { text = seed; show(); }
    },
    append(delta) { text += String(delta); show(); },
    update() {},
    end() {},
    toggleExpanded() { return false; },
    recolor() { if (node) node.fg = THEME.detailText; },
  };
}

export function createBlock(kind, ctx) {
  const factory = Object.prototype.hasOwnProperty.call(FACTORIES, kind)
    ? FACTORIES[kind]
    : createFallbackBlock;
  return factory(ctx);
}
