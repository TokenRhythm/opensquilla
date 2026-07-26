import { expect, test } from "bun:test";
import { BoxRenderable, TextRenderable } from "@opentui/core";
import { createTestRenderer } from "@opentui/core/testing";

import { createTurnView } from "./turnView.mjs";

test("a tool block anchor survives reasoning expansion without a whole-turn row guess", async () => {
  const { renderer, renderOnce } = await createTestRenderer({ width: 90, height: 30 });
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
    MarkdownRenderable: null,
    syntaxStyle: null,
    conversationBox,
    contentWidth: () => 90,
  }, "turn-anchor");

  turn.begin("prompt", "prompt", { text: "inspect the build" });
  turn.begin("reasoning", "reasoning", {
    text: Array.from({ length: 12 }, (_, index) => `reasoning row ${index + 1}`).join("\n"),
    elapsedSeconds: 2,
  });
  turn.end("reasoning");
  turn.begin("tool", "tool", { name: "read_file", args: "package.json" });
  turn.update("tool", { status: "ok", result: "done" });
  turn.end("tool");
  await renderOnce();

  const anchor = { block_id: "tool", row_within_block: 0 };
  const before = turn.rowForAnchor(anchor);
  expect(before).toBeNumber();
  expect(turn.anchorAtRow(before).block_id).toBe("tool");

  turn.setDetailsExpanded(true);
  await renderOnce();
  const after = turn.rowForAnchor(anchor);
  expect(after).toBeGreaterThan(before);
  expect(turn.anchorAtRow(after).block_id).toBe("tool");
  renderer.destroy?.();
});
