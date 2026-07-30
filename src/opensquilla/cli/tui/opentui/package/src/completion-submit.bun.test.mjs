// End-to-end completion-confirm behavior, driven through real keypresses:
//   - Enter on a highlighted slash command RUNS it in one keystroke (it used to
//     just insert the text and wait for a second Enter — "Tab adds a blank, the
//     command only appears after the next Enter").
//   - Tab completes the command (so you can still type arguments) without
//     submitting.
//
// Run with: bun test src/completion-submit.bun.test.mjs
import { test, expect } from "bun:test";
import { createTestRenderer } from "@opentui/core/testing";
import { BoxRenderable, TextRenderable } from "@opentui/core";

import { createComposer } from "./composer.mjs";
import { applyTheme } from "./theme.mjs";

async function setupComposer() {
  applyTheme("opensquilla-dark");
  const sent = [];
  const { renderer } = await createTestRenderer({ width: 60, height: 12 });
  const conversationBox = new BoxRenderable(renderer, {
    id: "conversation", position: "absolute", left: 0, top: 0, right: 0, height: 6,
  });
  renderer.root.add(conversationBox);
  const inputBox = new BoxRenderable(renderer, {
    id: "input-region", position: "absolute", left: 0, right: 0, bottom: 0, height: 6,
  });
  renderer.root.add(inputBox);
  const overlayLayer = new BoxRenderable(renderer, {
    id: "overlay-layer", position: "absolute", left: 0, top: 0, right: 0, bottom: 0,
    zIndex: 1000, shouldFill: false, visible: false,
  });
  renderer.root.add(overlayLayer);
  const composer = createComposer({
    renderer, BoxRenderable, TextRenderable, conversationBox, inputBox, overlayLayer,
    footerHeight: 6, sendHostMessage: (m) => sent.push(m),
  });
  try {
    composer.install();
  } catch {
    composer.rerender();
  }
  composer.setCompletionContext({
    catalog: [
      { label: "/theme", insert_text: "/theme ", description: "List or switch the OpenTUI color theme.", category: "command" },
      { label: "/skill:html-coder", insert_text: "use the html-coder skill: ", description: "HTML skill.", category: "skill", submit_behavior: "complete" },
      {
        label: "/router",
        insert_text: "/router ",
        description: "Control Router.",
        category: "model",
        argument_choices: [
          { value: "on", description: "Enable Router." },
          { value: "off", description: "Use direct mode." },
          { value: "status", description: "Show current strategy." },
        ],
      },
      { label: "/resume", insert_text: "/resume ", description: "Resume a session.", category: "session", submit_behavior: "submit" },
    ],
    files: ["src/app.mjs"],
  });
  return { renderer, composer, sent };
}

const press = (renderer, name, sequence = name) =>
  renderer.keyInput.emit("keypress", { name, sequence });
const type = (renderer, text) => {
  for (const ch of text) press(renderer, ch, ch);
};

test("Enter on the /theme suggestion runs it in one keystroke", async () => {
  const { renderer, sent } = await setupComposer();
  type(renderer, "/theme"); // the slash menu opens with /theme highlighted
  press(renderer, "return");

  const submits = sent.filter((m) => m.type === "input.submit");
  expect(submits.length).toBe(1); // exactly one Enter ran the command
  expect(submits[0].text).toBe("/theme "); // completed command was submitted
  renderer.destroy?.();
});

test("Tab on the /theme suggestion completes without submitting", async () => {
  const { renderer, sent } = await setupComposer();
  type(renderer, "/theme");
  press(renderer, "tab");

  // Tab completes (for typing arguments) — it must NOT submit on its own.
  expect(sent.some((m) => m.type === "input.submit")).toBe(false);
  renderer.destroy?.();
});

test("command argument choices complete and submit in one Enter", async () => {
  const { renderer, sent } = await setupComposer();
  type(renderer, "/router s");
  press(renderer, "return");

  expect(sent.find((m) => m.type === "input.submit")?.text).toBe("/router status ");
  renderer.destroy?.();
});

test("bare picker command Enter opens it in one keystroke", async () => {
  const { renderer, sent } = await setupComposer();
  type(renderer, "/resume");
  press(renderer, "return");

  expect(sent.find((m) => m.type === "input.submit")?.text).toBe("/resume ");
  renderer.destroy?.();
});

test("busy Enter requests steer while busy Tab explicitly queues", async () => {
  const first = await setupComposer();
  first.composer.setTurnActive({ active: true });
  type(first.renderer, "adjust the current approach");
  press(first.renderer, "return");
  expect(first.sent.find((m) => m.type === "input.submit")).toEqual({
    type: "input.submit",
    text: "adjust the current approach",
    intent: "steer",
  });
  first.renderer.destroy?.();

  const second = await setupComposer();
  second.composer.setTurnActive({ active: true });
  type(second.renderer, "run this afterward");
  press(second.renderer, "tab");
  expect(second.sent.find((m) => m.type === "input.submit")).toEqual({
    type: "input.submit",
    text: "run this afterward",
    intent: "queue",
  });
  second.renderer.destroy?.();
});

test("session picker filters canonical rows and resumes the selection", async () => {
  const { renderer, composer, sent } = await setupComposer();
  composer.openSessionPicker({
    current_key: "agent:main:first",
    sessions: [
      { key: "agent:main:first", title: "First task", model: "m1", status: "idle" },
      { key: "agent:main:second", title: "Second task", model: "m2", status: "done" },
    ],
  });
  type(renderer, "second");
  press(renderer, "return");
  expect(sent.find((m) => m.type === "input.submit")).toEqual({
    type: "input.submit",
    text: "/resume agent:main:second",
  });
  renderer.destroy?.();
});

test("Enter on a skill suggestion completes the prefix without submitting it", async () => {
  const { renderer, sent } = await setupComposer();
  type(renderer, "/html");
  press(renderer, "return");

  // The insert text is a prompt PREFIX that still needs a task after it, so the
  // first Enter must only complete; the follow-up Enter submits the full line.
  expect(sent.some((m) => m.type === "input.submit")).toBe(false);
  type(renderer, "build a page");
  press(renderer, "return");
  const submit = sent.find((m) => m.type === "input.submit");
  expect(submit?.text).toBe("use the html-coder skill: build a page");
  renderer.destroy?.();
});

test("Alt+Enter with a slash menu open inserts a newline, never accept-submits", async () => {
  const { renderer, sent } = await setupComposer();
  type(renderer, "/theme"); // menu open with /theme highlighted
  renderer.keyInput.emit("keypress", { name: "return", meta: true });

  expect(sent.some((m) => m.type === "input.submit")).toBe(false);
  type(renderer, "x");
  press(renderer, "return");
  expect(sent.find((m) => m.type === "input.submit")?.text).toBe("/theme\nx");
  renderer.destroy?.();
});

test("fast typing coalesces file-completion requests and drops stale responses", async () => {
  const { renderer, composer, sent } = await setupComposer();
  type(renderer, "@src"); // 4 keystrokes, each re-arming the 120ms debounce
  await new Promise((resolve) => setTimeout(resolve, 200));

  const requests = sent.filter((m) => m.type === "completion.request");
  expect(requests.length).toBe(1); // coalesced into one request...
  expect(requests[0].request_id).toBe(4); // ...carrying the LATEST sequence

  // A stale response (an earlier request_id) must not clobber the menu.
  composer.applyCompletionResponse({
    kind: "file",
    request_id: 2,
    items: [{ label: "STALE", insert_text: "@STALE ", category: "file" }],
  });
  press(renderer, "tab"); // accept the (cached) match, not the stale item
  press(renderer, "return");
  const submit = sent.find((m) => m.type === "input.submit");
  expect(submit?.text).toBe("@src/app.mjs ");
  renderer.destroy?.();
});
