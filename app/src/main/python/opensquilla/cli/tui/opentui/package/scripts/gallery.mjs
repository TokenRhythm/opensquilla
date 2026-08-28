// Render representative conversation frames through the REAL UI modules and
// print them as plain text — a fast visual feedback loop for aesthetic work
// without a terminal session or a model. Not a test: run it directly.
//
//   bun scripts/gallery.mjs            # default 100x34
//   bun scripts/gallery.mjs 80 30      # custom size
import { createTestRenderer } from "@opentui/core/testing";
import {
  ASCIIFontRenderable,
  BoxRenderable,
  MarkdownRenderable,
  ScrollBoxRenderable,
  SyntaxStyle,
  TextRenderable,
} from "@opentui/core";

import { createComposer } from "../src/composer.mjs";
import { createContextRail } from "../src/contextView.mjs";
import { createDispatcher } from "../src/ipc.mjs";
import { createTurnFlow, createTurnView } from "../src/turnView.mjs";
import { createWelcomeView } from "../src/welcomeView.mjs";
import { clampFooterHeight } from "../src/primitives.mjs";
import { registerThemeStyles } from "../src/syntaxTheme.mjs";
import { applyTheme, THEME } from "../src/theme.mjs";

const width = Number(process.argv[2] ?? 100);
const height = Number(process.argv[3] ?? 34);
const themeName = process.env.OPENSQUILLA_TUI_THEME ?? "opensquilla-dark";

applyTheme(themeName);

const FOOTER_HEIGHT = 6;
const footerRows = clampFooterHeight(FOOTER_HEIGHT, height);

const setup = await createTestRenderer({ width, height });
const { renderer, renderOnce, captureCharFrame, captureSpans } = setup;

const syntaxStyle = SyntaxStyle.create();
registerThemeStyles(syntaxStyle, THEME);

const conversationBox = new ScrollBoxRenderable(renderer, {
  id: "conversation",
  position: "absolute",
  left: 0,
  top: 0,
  right: 0,
  height: Math.max(1, height - footerRows),
  backgroundColor: THEME.appBg,
  stickyScroll: true,
  stickyStart: "bottom",
  scrollY: true,
  scrollX: false,
  viewportCulling: true,
});
renderer.root.add(conversationBox);

const inputBox = new BoxRenderable(renderer, {
  id: "input-region",
  position: "absolute",
  left: 0,
  right: 0,
  bottom: 0,
  height: footerRows,
  backgroundColor: THEME.footerBg,
});
renderer.root.add(inputBox);

// Match the production host's responsive shell: a full-height rail at >=132
// columns, plus the fixed identity header and narrow-screen footer strip fed by
// the same additive context.update frame.
const contextRail = createContextRail({
  renderer,
  BoxRenderable,
  TextRenderable,
  conversationBox,
  inputBox,
  footerHeight: FOOTER_HEIGHT,
});
renderer.root.add(contextRail.header);
renderer.root.add(contextRail.node);
contextRail.render();

const welcome = createWelcomeView({
  renderer,
  BoxRenderable,
  TextRenderable,
  ASCIIFontRenderable,
  conversationBox,
  contentWidth: () => contextRail.contentWidth(),
});

const overlayLayer = new BoxRenderable(renderer, {
  id: "overlay-layer",
  position: "absolute",
  left: 0,
  top: 0,
  right: 0,
  bottom: 0,
  zIndex: 1000,
  shouldFill: false,
  visible: false,
});
renderer.root.add(overlayLayer);

const sent = [];
const composer = createComposer({
  renderer,
  BoxRenderable,
  TextRenderable,
  conversationBox,
  inputBox,
  overlayLayer,
  footerHeight: FOOTER_HEIGHT,
  onContextUpdate: (snapshot) => contextRail.updateContext(snapshot),
  onRouterUpdate: (snapshot) => contextRail.updateRouter(snapshot),
  sendHostMessage: (m) => sent.push(m),
});
composer.install();

const turnDeps = {
  renderer,
  BoxRenderable,
  TextRenderable,
  MarkdownRenderable,
  syntaxStyle,
  conversationBox,
  contentWidth: () => contextRail.contentWidth(),
  agentLabel: () => contextRail.agentLabel(),
};
let seq = 0;
const flow = createTurnFlow((id) => createTurnView(turnDeps, id ?? seq++));

// Keep gallery context on the additive host protocol path rather than setting
// view internals directly. Additional frame kinds can join this deterministic
// visual fixture as the production shell evolves.
const dispatch = createDispatcher({
  contextUpdate: (message) => {
    composer.setContextState(message);
    flow.refreshContext();
  },
  routerUpdate: (message) => composer.setRouterState(message),
  unknown: () => {},
});

// ---- drive a representative session ----------------------------------------
const scenario = process.argv[4] ?? "full";

function promptEcho(text) {
  const turn = flow.ensure();
  turn.begin(`prompt-${seq++}`, "prompt", { text });
}

if (scenario === "full") {
  welcome.syncHistory({ messages: [{ role: "user", text: "existing history" }] });
  dispatch({
    type: "context.update",
    agent: { id: "main", name: "Mira", emoji: "🦐" },
    task: "TUI output fidelity",
    surface: "Web + TUI",
    gateway: "connected",
    model: "openai/gpt-5.4",
    permission: "workspace-write",
    workspace: "/workspace/opensquilla",
    queue: "0 queued",
    context: "12%",
  });
  dispatch({
    type: "router.update",
    model: "openai/gpt-5.4",
    route: "c2 91%",
    saving: "62%",
    context: "12%",
    io: "34.6k/548",
    source: "router",
    routingApplied: true,
  });

  // Turn 1 exercises the full retained process stream. The gallery expands it
  // explicitly (the Ctrl+O state) so a 160x40 capture can inspect arguments,
  // process updates, results, answer markdown, and usage in one frame.
  flow.setDetailsExpanded(true);
  promptEcho("Review the new TUI and verify that process output is complete.");
  const t1 = flow.ensure();
  t1.begin("r1", "reasoning", {});
  t1.append("r1", "I need to verify responsive geometry and output retention.\n");
  t1.append("r1", "First inspect the relevant tests, then run the focused suite.\n");
  t1.append("r1", "I will report the evidence without hiding process failures.");
  t1.end("r1");
  t1.begin("n1", "thinking", {});
  t1.append("n1", "Inspecting the responsive context layout.\n");
  t1.append("n1", "Checking full tool payload retention before summarizing.");
  t1.end("n1");
  t1.begin("tool1", "tool", {
    name: "exec_command",
    args_summary: "focused TUI tests",
    args_full: JSON.stringify({
      cmd: "bun test src/context-layout.bun.test.mjs src/tool-rendering.bun.test.mjs",
      cwd: "/workspace/opensquilla/src/opensquilla/cli/tui/opentui/package",
      timeout_ms: 120000,
    }, null, 2),
  });
  t1.update("tool1", {
    process: "collecting 2 test files\nrunning context and tool rendering contracts",
  });
  t1.append("tool1", "13 context layout assertions passed\n");
  t1.append("tool1", "12 tool rendering assertions passed\n");
  t1.append("tool1", "25 passed, 0 failed");
  t1.update("tool1", { status: "ok", duration: "0.42s" });
  t1.end("tool1");
  t1.begin("a1", "answer", {});
  t1.append(
    "a1",
    "## Verification complete\n\n" +
      "- Wide terminals keep a linear transcript beside the context rail.\n" +
      "- Thinking, reasoning, tool arguments, process, and results remain available.\n" +
      "- `Ctrl+O` switches between compact and complete detail without moving focus.",
  );
  t1.end("a1");
  t1.begin("u1", "usage", { text: "in 9.4k / out 548 · $0.08 · 3.3s" });
  t1.end("u1");
  flow.endTurn(false);

} else if (scenario === "reasoning") {
  promptEcho("prove the Collatz conjecture");
  const t = flow.ensure();
  t.begin("r1", "reasoning", {});
  t.append("r1", "The user asks for a proof of an open problem.\n");
  t.append("r1", "I should explain why no proof is known rather than invent one.\n");
  t.append("r1", "Let me survey what IS known: verified up to 2^68, Terras density results…\n");
  t.append("r1", "I will structure the answer around partial results and heuristics.\n");
} else if (scenario === "thinking-live") {
  dispatch({
    type: "context.update",
    agent: { id: "main", name: "Mira", emoji: "🦐" },
    surface: "Web + TUI",
    gateway: "connected",
    model: "openai/gpt-5.4",
    workspace: "/workspace/opensquilla",
  });
  promptEcho("Review the TUI first screen and improve its visual hierarchy.");
  const t = flow.ensure();
  t.begin("r-live", "reasoning", { waiting: true });
  t.append("r-live", "I’ll inspect the current hierarchy and terminal constraints.\n");
  t.append("r-live", "The first screen needs a clear brand anchor without crowding the composer.\n");
  t.append("r-live", "Next I’m checking the live reasoning rhythm at narrow and wide widths.");
} else if (scenario === "thinking-waiting") {
  dispatch({
    type: "context.update",
    agent: { id: "main", name: "main" },
    task: "Session",
    surface: "Web + TUI",
    gateway: "connected",
    model: "default",
    workspace: "/workspace/opensquilla",
  });
  promptEcho("hi");
  const t = flow.ensure();
  t.begin("r-waiting", "reasoning", { waiting: true });
} else if (scenario === "welcome") {
  dispatch({
    type: "context.update",
    agent: { id: "main", name: "main" },
    surface: "Web + TUI",
    gateway: "connected",
    model: "openai/gpt-5.4",
    workspace: "/workspace/opensquilla",
  });
}

// Markdown blocks parse asynchronously (tree-sitter); settle before capture.
for (let i = 0; i < 40; i += 1) {
  await renderOnce();
  await new Promise((resolve) => setTimeout(resolve, 5));
}
const frame = captureCharFrame();
const spansPath = process.env.OPENSQUILLA_TUI_GALLERY_SPANS;
if (spansPath) {
  await Bun.write(spansPath, JSON.stringify(captureSpans()));
}
console.log(`── ${themeName} · ${width}x${height} · ${scenario} ` + "─".repeat(Math.max(0, width - themeName.length - String(width).length - String(height).length - scenario.length - 12)));
console.log(frame);
console.log("─".repeat(width));
renderer.destroy?.();
process.exit(0);
