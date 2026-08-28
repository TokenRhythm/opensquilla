import { test, expect } from "bun:test";
import { createTestRenderer } from "@opentui/core/testing";
import { BoxRenderable, MarkdownRenderable, TextRenderable } from "@opentui/core";

import { createBlock } from "./blockRegistry.mjs";
import { createTurnView, isOutOfCardKind } from "./turnView.mjs";

function flatText(frame) {
  return frame.lines.map((line) => line.spans.map((span) => span.text).join("")).join("\n");
}

async function mountBlock({ width = 80, height = 24 } = {}) {
  const setup = await createTestRenderer({ width, height });
  const { renderer } = setup;
  const box = new BoxRenderable(renderer, {
    id: "ensemble-box",
    position: "absolute",
    left: 0,
    top: 0,
    right: 0,
    bottom: 0,
    flexDirection: "column",
  });
  renderer.root.add(box);
  const block = createBlock("ensemble", {
    renderer,
    BoxRenderable,
    TextRenderable,
    MarkdownRenderable,
    box,
    idPrefix: "ensemble",
    contentWidth: () => renderer.terminalWidth,
  });
  return { ...setup, block };
}

test("ensemble registry renders one compact progress row and merges live member updates", async () => {
  const { renderer, renderOnce, captureSpans, block } = await mountBlock();
  try {
    block.begin({
      completed: 0,
      total: 3,
      status: "running",
      members: [
        { id: "anchor", label: "anchor", status: "running" },
        { id: "research", label: "research", status: "queued" },
        { id: "critic", label: "critic", status: "queued" },
      ],
    });
    await renderOnce();
    let text = flatText(captureSpans());
    expect(text).toContain("Ensemble · 0/3 complete");
    expect(text).not.toContain("anchor");

    block.update({
      progress: { completed: 1, total: 3 },
      members: [{
        id: "anchor",
        label: "anchor",
        model: "qwen3.7-plus",
        provider: "openrouter",
        status: "completed",
        elapsed_ms: 1250,
        input_tokens: 120,
        output_tokens: 44,
        cost_usd: 0.0012,
      }],
      status: "running",
    });
    await renderOnce();
    text = flatText(captureSpans());
    expect(text).toContain("Ensemble · 1/3 complete");
    expect(text.split("\n").filter((line) => line.includes("Ensemble"))).toHaveLength(1);
    expect(text).not.toContain("qwen3.7-plus");
    expect(block.hiddenLineCount).toBeGreaterThanOrEqual(3);
  } finally {
    renderer.destroy?.();
  }
});

test("Ctrl+O turn details disclose member model, status, elapsed, tokens, cost, and error", async () => {
  const setup = await createTestRenderer({ width: 88, height: 34 });
  const { renderer, renderOnce, captureSpans } = setup;
  const conversationBox = new BoxRenderable(renderer, {
    id: "conversation",
    position: "absolute",
    left: 0,
    top: 0,
    right: 0,
    bottom: 0,
    flexDirection: "column",
  });
  renderer.root.add(conversationBox);
  const turn = createTurnView({
    renderer,
    BoxRenderable,
    TextRenderable,
    MarkdownRenderable,
    syntaxStyle: undefined,
    conversationBox,
    contentWidth: () => renderer.terminalWidth,
  }, "ensemble-turn");
  try {
    turn.begin("ensemble", "ensemble", {
      completed: 1,
      total: 2,
      status: "running",
      members: [
        {
          id: "anchor",
          label: "anchor",
          model: "qwen3.7-plus",
          provider: "openrouter",
          status: "completed",
          elapsed_ms: 1250,
          input_tokens: 120,
          output_tokens: 44,
          cost_usd: 0.0012,
        },
        {
          id: "critic",
          label: "critic",
          model: "glm-5.2",
          provider: "z-ai",
          status: "failed",
          elapsed_ms: 980,
          error: "\u001b[31mprovider timeout\u001b[0m",
        },
      ],
    });
    await renderOnce();
    const collapsed = flatText(captureSpans());
    expect(collapsed).toContain("╭ squilla");
    expect(collapsed).toContain("Ensemble · 1/2 complete");
    expect(collapsed).not.toContain("qwen3.7-plus");

    expect(turn.toggleDetails()).toBe(true);
    await renderOnce();
    const expanded = flatText(captureSpans());
    expect(expanded).toContain("qwen3.7-plus");
    expect(expanded).toContain("openrouter");
    expect(expanded).toContain("completed");
    expect(expanded).toContain("1.3s");
    expect(expanded).toContain("in 120 / out 44 tok");
    expect(expanded).toContain("$0.0012");
    expect(expanded).toContain("glm-5.2");
    expect(expanded).toContain("failed");
    expect(expanded).toContain("provider timeout");
    expect(expanded).not.toContain("\u001b[31m");
    expect(expanded).toContain("collapse details");
  } finally {
    renderer.destroy?.();
  }
});

test("completed ensemble keeps its summary and fallback receipt after block.end", async () => {
  const { renderer, renderOnce, captureSpans, block } = await mountBlock({ width: 100, height: 20 });
  try {
    block.begin({ completed: 2, total: 3, status: "running" });
    block.update({
      completed: 3,
      total: 3,
      status: "completed",
      summary: "Synthesized best response",
      request_count: 3,
      fallback_used: true,
      fallback_reason: "aggregator timed out",
    });
    block.end();
    await renderOnce();
    const receipt = flatText(captureSpans());
    expect(receipt).toContain("Ensemble · 3/3 complete");
    expect(receipt).toContain("Synthesized best response");
    expect(receipt).toContain("3 requests");
    expect(receipt).toContain("fallback");
    expect(receipt).not.toContain("aggregator timed out");

    block.toggleExpanded(true);
    await renderOnce();
    expect(flatText(captureSpans())).toContain("fallback · aggregator timed out");
  } finally {
    renderer.destroy?.();
  }
});

test("an aggregator detail row does not inflate the proposer progress denominator", async () => {
  const { renderer, renderOnce, captureSpans, block } = await mountBlock();
  try {
    block.begin({
      completed: 1,
      total: 1,
      status: "done",
      members: [
        { id: "proposer:0", label: "fast", status: "done" },
        { id: "aggregator:0", label: "judge", model: "judge-model", status: "done" },
      ],
    });
    block.end();
    await renderOnce();
    expect(flatText(captureSpans())).toContain("Ensemble · 1/1 complete");

    block.toggleExpanded(true);
    await renderOnce();
    expect(flatText(captureSpans())).toContain("judge-model");
  } finally {
    renderer.destroy?.();
  }
});

test("ensemble is a registered in-card detail block", () => {
  expect(isOutOfCardKind("ensemble")).toBe(false);
});
