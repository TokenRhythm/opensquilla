import assert from "node:assert/strict";
import test from "node:test";

import { historyBoundaryText, replayHistory, replaceHistoryConversation } from "./historyRenderer.mjs";

function harness() {
  const views = [];
  let active = null;
  const makeView = (id) => {
    const events = [];
    const view = {
      id,
      ended: false,
      events,
      begin: (blockId, kind, meta) => events.push(["begin", blockId, kind, meta]),
      append: (blockId, delta) => events.push(["append", blockId, delta]),
      update: (blockId, patch) => events.push(["update", blockId, patch]),
      end: (blockId) => events.push(["end", blockId]),
      finish: (cancelled) => events.push(["finish", cancelled]),
    };
    views.push(view);
    return view;
  };
  const flow = {
    ensure(id) {
      if (!active || active.ended) active = makeView(id);
      return active;
    },
    turnForPrompt(id) { return this.ensure(id); },
    endTurn(cancelled) {
      if (!active) return;
      active.finish(cancelled);
      active.ended = true;
    },
  };
  return { flow, views };
}

test("history boundary labels complete, windowed, and compacted snapshots", () => {
  assert.equal(historyBoundaryText({ history_scope: "complete", loaded_count: 4 }), "history · complete · 4 messages");
  assert.equal(historyBoundaryText({ history_scope: "latest_window", loaded_count: 20 }), "history · latest 20 messages · older messages available");
  assert.equal(historyBoundaryText({ history_scope: "compacted", loaded_count: 8, compaction_summaries: [{ id: "s1" }] }), "history · compacted · 8 recent messages · 1 earlier summary");
  assert.equal(historyBoundaryText({ history_scope: "complete", loaded_count: 0 }), "");
});

test("history replacement clears old conversation children before replay", () => {
  const children = [{ id: "old-turn" }, { id: "old-notice" }];
  const conversationBox = {
    getChildren: () => [...children],
    remove(node) {
      const index = children.findIndex((child) => child === node);
      if (index >= 0) children.splice(index, 1);
    },
    add(child) { children.push(child); },
  };
  const { flow, views } = harness();

  const replaced = replaceHistoryConversation({
    message: {
      history_scope: "complete",
      loaded_count: 1,
      messages: [{ id: "m1", role: "assistant", text: "fresh" }],
    },
    conversationBox,
    flowFactory: () => flow,
    addBoundary: (content) => conversationBox.add({ id: "new-boundary", content }),
    nextId: (id) => id,
  });

  assert.equal(replaced, flow);
  assert.deepEqual(children, [{ id: "new-boundary", content: "history · complete · 1 messages" }]);
  assert.equal(views.length, 1);
  assert.ok(views[0].events.some((event) => event[0] === "append" && event[2] === "fresh"));
});

test("canonical history reuses live turn blocks and deduplicates durable ids", () => {
  const { flow, views } = harness();
  replayHistory({
    flow,
    nextId: (id) => `history-${id}`,
    messages: [
      { id: "m1", role: "user", text: "hello", attachments: [{ name: "brief.pdf" }] },
      {
        id: "m2",
        role: "assistant",
        text: "done",
        reasoning: "checked",
        tool_calls: [{ id: "t1", name: "read_file", input: { path: "brief.pdf" }, result: "ok" }],
        artifacts: [{ name: "report.md" }],
        usage: { input_tokens: 3, output_tokens: 5, reasoning_tokens: 2, model: "openai/test" },
      },
      { id: "m2", role: "assistant", text: "duplicate" },
    ],
  });

  assert.equal(views.length, 1);
  const events = views[0].events;
  assert.ok(events.some((event) => event[0] === "begin" && event[2] === "prompt" && event[3].text.includes("brief.pdf")));
  assert.ok(events.some((event) => event[0] === "begin" && event[2] === "reasoning"));
  assert.ok(events.some((event) => event[0] === "begin" && event[2] === "tool" && event[3].name === "read_file"));
  assert.ok(events.some((event) => event[0] === "begin" && event[2] === "answer"));
  assert.ok(events.some((event) => event[0] === "begin" && event[2] === "history-detail"));
  assert.ok(events.some((event) =>
    event[0] === "begin"
    && event[2] === "usage"
    && event[3].text === "in 3 / out 5 / think 2 · openai/test"
  ));
  assert.equal(events.filter((event) => event[0] === "finish").length, 1);
  assert.equal(events.some((event) => event.includes("duplicate")), false);
});

test("causal turn identity groups prompt, reasoning, tools, and answer after hydrate", () => {
  const { flow, views } = harness();
  const turn_context = { turn_id: "turn-1", client_message_id: "client-1", intent: "send" };
  replayHistory({
    flow,
    nextId: (id) => `history-${id}`,
    messages: [
      { id: "user-1", role: "user", text: "inspect", turn_context },
      { id: "assistant-1", role: "assistant", reasoning: "checking", text: "done", turn_context },
    ],
  });

  assert.equal(views.length, 1);
  assert.equal(views[0].id, "history-turn-1");
  assert.ok(views[0].events.some((event) => event[1] === "history-user-1-prompt"));
  assert.ok(views[0].events.some((event) => event[1] === "history-assistant-1-reasoning"));
  assert.ok(views[0].events.some((event) => event[1] === "history-assistant-1-answer"));
  assert.equal(views[0].events.filter((event) => event[0] === "finish").length, 1);
});

test("history usage accepts camelCase reasoning and omits absent or zero values", () => {
  const { flow, views } = harness();
  replayHistory({
    flow,
    nextId: (id) => `history-${id}`,
    messages: [
      {
        id: "camel",
        role: "assistant",
        text: "camel usage",
        usage: { inputTokens: 10, outputTokens: 4, reasoningTokens: 3, model: "camel/model" },
      },
      {
        id: "zero",
        role: "assistant",
        text: "zero usage",
        usage: { input_tokens: 8, output_tokens: 2, reasoning_tokens: 0, model: "zero/model" },
      },
      {
        id: "missing",
        role: "assistant",
        text: "legacy usage",
        usage: { input_tokens: 7, output_tokens: 1, model: "legacy/model" },
      },
    ],
  });

  const usageTexts = views.flatMap((view) => view.events)
    .filter((event) => event[0] === "begin" && event[2] === "usage")
    .map((event) => event[3].text);
  assert.deepEqual(usageTexts, [
    "in 10 / out 4 / think 3 · camel/model",
    "in 8 / out 2 · zero/model",
    "in 7 / out 1 · legacy/model",
  ]);
});

test("hydrated ensemble turns retain the public receipt without candidate bodies", () => {
  const { flow, views } = harness();
  replayHistory({
    flow,
    nextId: (id) => `history-${id}`,
    messages: [{
      id: "ensemble-turn",
      role: "assistant",
      text: "combined answer",
      usage: {
        input_tokens: 30,
        output_tokens: 12,
        model: "ensemble/judge",
        ensemble_trace: {
          total_candidates: 1,
          successful_proposers: 1,
          fallback_used: false,
          candidates: [{
            index: 0,
            label: "fast",
            provider: "openrouter",
            model: "candidate-a",
            ok: true,
            elapsed_ms: 900,
            content: "PRIVATE CANDIDATE CONTENT",
            text: "PRIVATE FULL CANDIDATE",
          }],
        },
        model_usage_breakdown: [{
          role: "proposer",
          label: "fast",
          provider: "openrouter",
          model: "candidate-a",
          input_tokens: 10,
          output_tokens: 4,
          request_count: 1,
        }, {
          role: "aggregator",
          label: "judge",
          provider: "openrouter",
          model: "judge-model",
          input_tokens: 20,
          output_tokens: 8,
          request_count: 1,
        }],
      },
    }],
  });

  const ensemble = views[0].events.find(
    (event) => event[0] === "begin" && event[2] === "ensemble",
  );
  assert.ok(ensemble);
  assert.equal(ensemble[3].completed, 1);
  assert.equal(ensemble[3].total, 1);
  assert.equal(ensemble[3].request_count, 2);
  assert.equal(ensemble[3].members.length, 2);
  assert.equal(JSON.stringify(ensemble).includes("PRIVATE"), false);
  assert.ok(views[0].events.some(
    (event) => event[0] === "begin" && event[2] === "answer",
  ));
});
