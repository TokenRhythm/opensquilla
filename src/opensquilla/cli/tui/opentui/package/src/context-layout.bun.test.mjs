import { test, expect } from "bun:test";
import { createTestRenderer } from "@opentui/core/testing";
import { BoxRenderable, TextRenderable } from "@opentui/core";

import { createComposer } from "./composer.mjs";
import {
  compactContextItems,
  CONTEXT_HEADER_HEIGHT,
  contextAgentLabel,
  contextHeaderItems,
  contextRailRows,
  contextRailWidth,
  createContextRail,
  emptyContextState,
  isWideContextLayout,
  normalizeContextUpdate,
} from "./contextView.mjs";
import { createDispatcher } from "./ipc.mjs";
import { createRendererViewportState } from "./screenMode.mjs";
import { THEME } from "./theme.mjs";
import { textWidth } from "./primitives.mjs";

const FOOTER_HEIGHT = 6;

function frameText(frame) {
  return frame.lines.map((line) => line.spans.map((span) => span.text).join("")).join("\n");
}

function rowText(frame, row) {
  return frame.lines[row]?.spans.map((span) => span.text).join("") ?? "";
}

test("context.update is sanitized, partial, and explicitly clearable", () => {
  const first = normalizeContextUpdate({
    agent: { name: "Mi\x1b[31mra", emoji: "🦐", id: "main" },
    task: "TUI\npolish",
    model: "openai/gpt-5.4",
    workspace: "/workspace/opensquilla",
    permission: "workspace_write",
  });
  expect(first.agent).toBe("Mira");
  expect(first.agentEmoji).toBe("🦐");
  expect(first.agentId).toBe("main");
  expect(first.task).toBe("TUI polish");

  const partial = normalizeContextUpdate({ queue: "1 running / 0 queued" }, first);
  expect(partial.model).toBe("openai/gpt-5.4");
  expect(partial.queue).toBe("1 running / 0 queued");

  const cleared = normalizeContextUpdate({ task: null }, partial);
  expect(cleared.task).toBe("");
  expect(cleared.agent).toBe("Mira");
});

test("132 columns is the monotonic rail boundary and its width stays restrained", () => {
  expect(isWideContextLayout(80)).toBe(false);
  expect(isWideContextLayout(131)).toBe(false);
  expect(isWideContextLayout(132)).toBe(true);
  expect(contextRailWidth(131)).toBe(0);
  expect(contextRailWidth(132)).toBe(30);
  expect(contextRailWidth(160)).toBe(36);
  expect(contextRailWidth(240)).toBe(36);
});

test("the compact strip fits identity and safety fields by display cells at 80 columns", () => {
  const context = normalizeContextUpdate({
    agent: { name: "米拉 Mira", emoji: "🦐" },
    gateway: "connected",
    model: "openai/gpt-5.4",
    permission: "workspace_write",
    queue: "1 queued",
    context: "34%",
  });
  const items = compactContextItems(context, {
    route: "balanced",
    style: "normal",
    routingApplied: true,
    rolloutPhase: "full",
  }, 80);
  const content = items.map((item) => item.content).join(" · ");
  expect(content).toContain("米拉 Mira");
  expect(content).toContain("write");
  expect(content).toContain("router balanced");
  expect(content).toContain("GW ✓");
  expect(content).toContain("gpt-5.4");
  expect(textWidth(content)).toBeLessThanOrEqual(72);
});

test("a normal Router decision remains visible below the context-rail breakpoint", () => {
  const context = normalizeContextUpdate({
    agent: "main",
    gateway: "connected",
    model: "deepseek-v4-flash",
    permission: "normal",
    queue: "idle",
    context: "3%",
  });
  const content = compactContextItems(context, {
    route: "c0 60%",
    source: "router",
    style: "normal",
    routingApplied: true,
    rolloutPhase: "full",
  }, 120).map((item) => item.content).join(" · ");

  expect(content).toContain("router c0 60%");
  expect(textWidth(content)).toBeLessThanOrEqual(112);
});

test("an abnormal route outranks low-priority queue/context fields", () => {
  const context = normalizeContextUpdate({
    agent: "Mira",
    gateway: "connected",
    permission: "workspace-write",
    model: "gpt-5.4",
    queue: "many queued requests with a long label",
    context: "nearly full",
  });
  const content = compactContextItems(context, {
    route: "fallback-model",
    source: "fallback",
    style: "warning",
    routingApplied: true,
    rolloutPhase: "full",
  }, 80).map((item) => item.content).join(" · ");
  expect(content).toContain("router fallback-");
  expect(textWidth(content)).toBeLessThanOrEqual(72);
});

test("transport bootstrap placeholders are not presented as Router decisions", () => {
  const context = normalizeContextUpdate({
    agent: "main",
    gateway: "connected",
    model: "default",
  });
  const content = compactContextItems(context, {
    route: "gateway",
    style: "dim",
    routingApplied: false,
    rolloutPhase: "observe",
  }, 80).map((item) => item.content).join(" · ");

  expect(content).not.toContain("router gateway");
});

async function makeRailHarness(width, height = 30) {
  const setup = await createTestRenderer({ width, height });
  const { renderer } = setup;
  const viewportState = createRendererViewportState(renderer);
  renderer.on("resize", (nextWidth, nextHeight) => {
    viewportState.refresh("resize", { width: nextWidth, height: nextHeight });
  });
  const conversationBox = new BoxRenderable(renderer, {
    id: "conversation",
    position: "absolute",
    left: 0,
    top: 0,
    right: 0,
    height: 24,
    backgroundColor: THEME.appBg,
  });
  const inputBox = new BoxRenderable(renderer, {
    id: "input-region",
    position: "absolute",
    left: 0,
    right: 0,
    bottom: 0,
    height: FOOTER_HEIGHT,
    backgroundColor: THEME.footerBg,
  });
  renderer.root.add(conversationBox);
  renderer.root.add(inputBox);
  const rail = createContextRail({
    renderer,
    BoxRenderable,
    TextRenderable,
    conversationBox,
    inputBox,
    footerHeight: FOOTER_HEIGHT,
    viewport: () => viewportState.current(),
  });
  renderer.root.add(rail.header);
  renderer.root.add(rail.node);
  rail.updateContext({
    agent: { name: "Mira", emoji: "🦐" },
    task: "TUI polish",
    surface: "tui",
    gateway: "connected",
    model: "openai/gpt-5.4",
    permission: "workspace-write",
    workspace: "/workspace/opensquilla",
    queue: "idle",
    context: "34%",
  });
  rail.updateRouter({ route: "balanced", saving: "18%", io: "1.2k/240" });
  return { ...setup, conversationBox, inputBox, rail };
}

test("the context rail hides at 80x24 while the identity header remains one line", async () => {
  const { renderer, renderOnce, captureSpans, conversationBox, inputBox, rail } = await makeRailHarness(80, 24);
  await renderOnce();
  expect(rail.node.visible).toBe(false);
  expect(conversationBox.right).toBe(0);
  expect(inputBox.right).toBe(0);
  expect(rail.header.visible).toBe(true);
  expect(rail.header.right).toBe(0);
  expect(conversationBox.top).toBe(CONTEXT_HEADER_HEIGHT);
  expect(conversationBox.height).toBe(24 - FOOTER_HEIGHT - CONTEXT_HEADER_HEIGHT);
  const frame = captureSpans();
  expect(rowText(frame, 0)).toContain("OpenSquilla");
  expect(rowText(frame, 0)).toContain("TUI polish");
  expect(rowText(frame, 0)).toContain("Mira");
  expect(rowText(frame, 0)).toContain("shared · tui");
  expect(rowText(frame, 0)).toContain("GW ✓");
  expect(rowText(frame, 2)).not.toContain("OpenSquilla");
  renderer.destroy?.();
});

test("the rail spans the terminal and reserves the same width from transcript and footer", async () => {
  for (const [width, expectedRailWidth] of [[132, 30], [160, 36]]) {
    const { renderer, renderOnce, captureSpans, conversationBox, inputBox, rail } = await makeRailHarness(width);
    await renderOnce();
    const text = frameText(captureSpans());
    expect(rail.node.visible).toBe(true);
    expect(rail.node.width).toBe(expectedRailWidth);
    expect(rail.node.height).toBe(30);
    expect(conversationBox.right).toBe(expectedRailWidth);
    expect(inputBox.right).toBe(expectedRailWidth);
    expect(rail.header.right).toBe(expectedRailWidth);
    expect(conversationBox.top).toBe(CONTEXT_HEADER_HEIGHT);
    expect(conversationBox.height).toBe(30 - FOOTER_HEIGHT - CONTEXT_HEADER_HEIGHT);
    expect(text).toContain("context");
    expect(text).toContain("Mira");
    expect(text).toContain("TUI polish");
    expect(text).toContain("gpt-5.4");
    expect(text).toContain("permission  write");
    renderer.destroy?.();
  }
});

test("one rail controller owns wide to narrow to wide geometry without stale insets", async () => {
  const { renderer, renderOnce, captureSpans, conversationBox, inputBox, rail } = await makeRailHarness(160, 34);
  try {
    await renderOnce();
    expect(rail.node.visible).toBe(true);
    expect(rail.node.width).toBe(36);
    expect(conversationBox.right).toBe(36);
    expect(inputBox.right).toBe(36);

    renderer.resize(72, 24);
    rail.onResize();
    await renderOnce();
    let text = frameText(captureSpans());
    expect(rail.node.visible).toBe(false);
    expect(conversationBox.right).toBe(0);
    expect(inputBox.right).toBe(0);
    expect(rail.header.right).toBe(0);
    expect(text).not.toContain("AGENT");
    expect(text).not.toContain("RUNTIME");

    renderer.resize(132, 34);
    rail.onResize();
    await renderOnce();
    text = frameText(captureSpans());
    expect(rail.node.visible).toBe(true);
    expect(rail.node.width).toBe(30);
    expect(conversationBox.right).toBe(30);
    expect(inputBox.right).toBe(30);
    expect(rail.header.right).toBe(30);
    expect(text.match(/AGENT/gu)?.length).toBe(1);
    expect(text.match(/RUNTIME/gu)?.length).toBe(1);
  } finally {
    renderer.destroy?.();
  }
});

test("rail content geometry is coherent inside the resize callback before Yoga renders", async () => {
  const { renderer, renderOnce, conversationBox, inputBox, rail } = await makeRailHarness(132, 30);
  try {
    await renderOnce();
    expect(rail.rightInset()).toBe(30);
    expect(rail.contentWidth()).toBe(102);

    // OpenTUI emits resize after marking Yoga dirty but before the next frame
    // computes child widths. Every sibling relayout runs in this same callback,
    // so the rail controller must expose the new logical inset immediately
    // instead of leaking node.width from the previous rendered frame.
    renderer.resize(160, 40);
    rail.onResize();
    expect(conversationBox.right).toBe(36);
    expect(inputBox.right).toBe(36);
    expect(rail.rightInset()).toBe(36);
    expect(rail.contentWidth()).toBe(124);

    renderer.resize(72, 24);
    rail.onResize();
    expect(rail.rightInset()).toBe(0);
    expect(rail.contentWidth()).toBe(72);
  } finally {
    renderer.destroy?.();
  }
});

test("footer clamp expansion uses the new viewport before Yoga exposes the new height", async () => {
  const { renderer, renderOnce, conversationBox, inputBox, rail } = await makeRailHarness(160, 5);
  try {
    // Mirror the production surface transaction in a pane shorter than the
    // normal six-row footer, then render so OpenTUI caches the clamped height.
    inputBox.height = 5;
    rail.onResize();
    await renderOnce();

    // On expansion the setter updates Yoga style, but inputBox.height still
    // exposes the prior computed value until the next frame. Rail geometry must
    // come from the shared 30-row viewport, not that stale five-row getter.
    renderer.resize(160, 30);
    inputBox.height = FOOTER_HEIGHT;
    rail.onResize();
    expect(conversationBox.top).toBe(CONTEXT_HEADER_HEIGHT);
    // The public height getter intentionally remains the last computed Yoga
    // value until render. Inspect the pending public layout node style, then
    // prove the next OpenTUI frame exposes the same committed geometry.
    expect(conversationBox.getLayoutNode().getHeight().value).toBe(
      30 - FOOTER_HEIGHT - CONTEXT_HEADER_HEIGHT,
    );
    await renderOnce();
    expect(conversationBox.height).toBe(30 - FOOTER_HEIGHT - CONTEXT_HEADER_HEIGHT);
  } finally {
    renderer.destroy?.();
  }
});

test("without context.update the header and wide rail keep legacy zero-inset geometry", async () => {
  const setup = await createTestRenderer({ width: 160, height: 24 });
  const { renderer } = setup;
  const conversationBox = new BoxRenderable(renderer, {
    id: "conversation", position: "absolute", left: 0, top: 0, right: 0, height: 18,
  });
  const inputBox = new BoxRenderable(renderer, {
    id: "input-region", position: "absolute", left: 0, right: 0, bottom: 0, height: FOOTER_HEIGHT,
  });
  renderer.root.add(conversationBox);
  renderer.root.add(inputBox);
  const rail = createContextRail({ renderer, BoxRenderable, TextRenderable, conversationBox, inputBox, footerHeight: FOOTER_HEIGHT });
  renderer.root.add(rail.header);
  renderer.root.add(rail.node);
  rail.render();
  expect(rail.header.visible).toBe(false);
  expect(rail.node.visible).toBe(false);
  expect(conversationBox.top).toBe(0);
  expect(conversationBox.height).toBe(18);
  expect(inputBox.right).toBe(0);
  expect(rail.contentWidth()).toBe(160);
  renderer.destroy?.();
});

test("header fitting keeps brand and critical identity/connection context at 80 columns", () => {
  const context = normalizeContextUpdate({
    agent: { name: "Mira the extraordinarily long workspace agent", emoji: "🦐" },
    task: "A long task title that should be clipped before it can wrap",
    surface: "tui",
    gateway: "connected",
  });
  const items = contextHeaderItems(context, 80, 0);
  const line = items.map((item) => item.content).join(" · ");
  expect(items[0].key).toBe("brand");
  expect(items.some((item) => item.key === "agent")).toBe(true);
  expect(items.some((item) => item.key === "gateway")).toBe(true);
  expect(textWidth(line)).toBeLessThanOrEqual(78);
  expect(contextAgentLabel(context)).toStartWith("🦐 Mira");
});

async function makeComposerHarness(width) {
  const setup = await createTestRenderer({ width, height: 24 });
  const { renderer } = setup;
  const conversationBox = new BoxRenderable(renderer, {
    id: "conversation", position: "absolute", left: 0, top: 0, right: 0, height: 18,
  });
  const inputBox = new BoxRenderable(renderer, {
    id: "input-region", position: "absolute", left: 0, right: 0, bottom: 0,
    height: FOOTER_HEIGHT, backgroundColor: THEME.footerBg,
  });
  const overlayLayer = new BoxRenderable(renderer, {
    id: "overlay-layer", position: "absolute", left: 0, top: 0, right: 0, bottom: 0,
    zIndex: 1000, shouldFill: false, visible: false,
  });
  renderer.root.add(conversationBox);
  renderer.root.add(inputBox);
  renderer.root.add(overlayLayer);
  const composer = createComposer({
    renderer,
    BoxRenderable,
    TextRenderable,
    conversationBox,
    inputBox,
    overlayLayer,
    footerHeight: FOOTER_HEIGHT,
    sendHostMessage: () => {},
  });
  try { composer.install(); } catch { composer.rerender(); }
  return { ...setup, composer, inputBox };
}

test("80x24 renders canonical context in one strip without changing footer geometry", async () => {
  const { renderer, renderOnce, captureSpans, composer } = await makeComposerHarness(80);
  composer.setRouterState({ model: "openai/gpt-5.4", route: "balanced", saving: "18%", context: "34%", style: "normal" });
  composer.setContextState({ agent: { name: "Mira", emoji: "🦐" }, gateway: "connected", permission: "workspace-write", model: "openai/gpt-5.4" });
  await renderOnce();
  const frame = captureSpans();
  const strip = rowText(frame, 24 - FOOTER_HEIGHT);
  expect(strip).toContain("Mira");
  expect(strip).toContain("write");
  expect(strip).toContain("GW ✓");
  expect(strip).toContain("gpt-5.4");
  expect(strip).not.toContain("╭");
  const composerTop = rowText(frame, 24 - FOOTER_HEIGHT + 1);
  expect(composerTop).toContain("╭");
  expect(composerTop).toContain("╮");
  renderer.destroy?.();
});

test("the composer border stays free of duplicate turn activity", async () => {
  for (const width of [60, 80, 100]) {
    const { renderer, inputBox } = await makeComposerHarness(width);
    const composerBox = inputBox.getChildren().find((child) => child.id === "composer-box");
    expect(composerBox).toBeTruthy();
    const bottomTitle = composerBox.options?.bottomTitle ?? composerBox.bottomTitle ?? "";
    expect(String(bottomTitle).trim()).toBe("");
    renderer.destroy?.();
  }
});

test("IPC dispatch recognizes the additive context.update frame", () => {
  const seen = [];
  const dispatch = createDispatcher({
    contextUpdate: (message) => seen.push(message),
    unknown: (message) => seen.push({ unknown: message.type }),
  });
  dispatch({ type: "context.update", agent: "Mira" });
  expect(seen).toEqual([{ type: "context.update", agent: "Mira" }]);
});

test("empty host context leaves the legacy router fallback available", () => {
  expect(compactContextItems(emptyContextState(), { model: "gpt-5.4" }, 80)).toEqual([]);
  const rows = contextRailRows(emptyContextState(), {
    model: "pending", route: "pending", saving: "-", context: "pending",
  });
  expect(rows.map((row) => row.value ?? row.content ?? "").join(" ")).not.toContain("pending");
});
