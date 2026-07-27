import { expect, test } from "bun:test";
import {
  BoxRenderable,
  ScrollBoxRenderable,
  TextRenderable,
} from "@opentui/core";
import { createTestRenderer } from "@opentui/core/testing";

import { createComposer } from "./composer.mjs";
import { createTurnView } from "./turnView.mjs";
import { THEME } from "./theme.mjs";

const FOOTER_HEIGHT = 6;
const HISTORY_TURNS = 100; // prompt + assistant = 200 hydrated messages
const TOOL_TURNS = 50;
const SAMPLE_FRAMES = 20;

function p95(values) {
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.max(0, Math.ceil(sorted.length * 0.95) - 1)] ?? 0;
}

async function benchmarkSurface(width, height) {
  const { renderer, renderOnce } = await createTestRenderer({ width, height });
  const conversationBox = new ScrollBoxRenderable(renderer, {
    id: "conversation",
    position: "absolute",
    left: 0,
    top: 0,
    right: 0,
    height: height - FOOTER_HEIGHT,
    stickyScroll: true,
    stickyStart: "bottom",
    scrollY: true,
    scrollX: false,
    viewportCulling: false,
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
  renderer.root.add(conversationBox);
  renderer.root.add(inputBox);
  renderer.root.add(overlayLayer);

  const deps = {
    renderer,
    BoxRenderable,
    TextRenderable,
    MarkdownRenderable: null,
    syntaxStyle: null,
    conversationBox,
    contentWidth: () => width,
  };
  for (let index = 0; index < HISTORY_TURNS; index += 1) {
    const turn = createTurnView(deps, `history-${index}`);
    turn.begin(`prompt-${index}`, "prompt", { text: `historical prompt ${index}` });
    if (index < TOOL_TURNS) {
      turn.begin(`tool-${index}`, "tool", { name: "read_file", args: `file-${index}.txt` });
      turn.update(`tool-${index}`, { status: "ok", result: `result ${index}` });
      turn.end(`tool-${index}`);
    }
    turn.begin(`assistant-${index}`, "intermediate", {});
    turn.append(`assistant-${index}`, `historical answer ${index}`);
    turn.end(`assistant-${index}`);
    turn.begin(`usage-${index}`, "usage", { text: "in 100 / out 40" });
    turn.finish(false);
  }

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
  composer.install();

  const active = createTurnView(deps, "active");
  active.begin("active-prompt", "prompt", { text: "continue streaming" });
  active.begin("active-stream", "intermediate", {});
  await renderOnce();
  // Warm the renderer/layout caches before measuring steady-state streaming.
  for (let index = 0; index < 3; index += 1) {
    active.append("active-stream", ` warm-${index}`);
    await renderOnce();
  }

  const frameTimes = [];
  for (let index = 0; index < SAMPLE_FRAMES; index += 1) {
    active.append("active-stream", ` stream-${index}`);
    const started = performance.now();
    await renderOnce();
    frameTimes.push(performance.now() - started);
  }

  const inputTimes = [];
  for (const character of "abcdefghijklmnopqrst") {
    const started = performance.now();
    renderer.keyInput.emit("keypress", { name: character, sequence: character });
    await renderOnce();
    inputTimes.push(performance.now() - started);
  }
  const result = { frameP95: p95(frameTimes), inputP95: p95(inputTimes) };
  renderer.destroy?.();
  return result;
}

test("200-message transcript with 50 folded tools meets the daily-use latency gate", async () => {
  for (const [width, height] of [[80, 24], [120, 30], [160, 40]]) {
    const result = await benchmarkSurface(width, height);
    expect(result.frameP95).toBeLessThanOrEqual(33);
    expect(result.inputP95).toBeLessThanOrEqual(50);
  }
}, 30_000);
