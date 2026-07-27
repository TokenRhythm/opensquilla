import assert from "node:assert/strict";
import test from "node:test";

import {
  acceptCompletionText,
  filterCatalog,
  lineEndIndex,
  lineStartIndex,
  menuKeyAction,
  slashArgumentContext,
  shouldDropResponse,
  shouldTriggerMenu,
  spliceOut,
  tokenUnderCaret,
  wordEndIndex,
  wordStartIndex,
} from "./composer.mjs";
import { createDispatcher } from "./ipc.mjs";

test("tokenUnderCaret returns the token before the caret and its start index", () => {
  assert.deepEqual(tokenUnderCaret("/cmp", 4), { token: "/cmp", start: 0 });
  assert.deepEqual(tokenUnderCaret("ask @src/foo", 12), {
    token: "@src/foo",
    start: 4,
  });
  assert.deepEqual(tokenUnderCaret("one\n/resume", 11), {
    token: "/resume",
    start: 4,
  });
  assert.deepEqual(tokenUnderCaret("trail space ", 12), { token: "", start: 12 });
});

test("shouldTriggerMenu allows slash only at line start and files anywhere", () => {
  assert.deepEqual(shouldTriggerMenu("/cmp", 0, true), {
    active: true,
    kind: "slash",
    query: "cmp",
  });
  assert.deepEqual(shouldTriggerMenu("/cmp", 6, false), {
    active: false,
    kind: null,
    query: "",
  });
  assert.deepEqual(shouldTriggerMenu("@src/fo", 4, false), {
    active: true,
    kind: "file",
    query: "src/fo",
  });
  assert.deepEqual(shouldTriggerMenu("plain", 0, true), {
    active: false,
    kind: null,
    query: "",
  });
});

test("filterCatalog ranks subsequence matches predictably", () => {
  const catalog = [
    { label: "/compress", description: "compress", insert_text: "/compress " },
    { label: "/compact", description: "compact", insert_text: "/compact " },
    { label: "/resume", description: "resume", insert_text: "/resume " },
    { label: "/reset", description: "reset", insert_text: "/reset " },
  ];

  assert.equal(filterCatalog(catalog, "cmp")[0].label, "/compact");
  assert.deepEqual(
    filterCatalog(catalog, "re").map((item) => item.label).slice(0, 2),
    ["/reset", "/resume"],
  );
  assert.deepEqual(filterCatalog(catalog, "").map((item) => item.label), [
    "/compress",
    "/compact",
    "/resume",
    "/reset",
  ]);
});

test("filterCatalog prefers command-name prefixes over later path segments", () => {
  const catalog = [
    { label: "/audio-cog", description: "skill", insert_text: "/audio-cog " },
    { label: "/html-coder", description: "skill", insert_text: "/html-coder " },
    { label: "/compact", description: "command", insert_text: "/compact " },
    { label: "/cost", description: "command", insert_text: "/cost " },
  ];

  assert.deepEqual(
    filterCatalog(catalog, "co").map((item) => item.label).slice(0, 2),
    ["/cost", "/compact"],
  );
});

test("default palette preserves curated order and hides compatibility rows", () => {
  const catalog = [
    { label: "/model", visible_by_default: true },
    { label: "/strategy", visible_by_default: true },
    { label: "/models", visible_by_default: false, deprecated: true },
    { label: "/forget", visible_by_default: false },
  ];

  assert.deepEqual(
    filterCatalog(catalog, "").map((item) => item.label),
    ["/model", "/strategy"],
  );
  assert.deepEqual(
    filterCatalog(catalog, "models").map((item) => item.label),
    ["/models"],
  );
});

test("aliases remain searchable without rendering duplicate alias rows", () => {
  const catalog = [
    {
      label: "/reset",
      aliases: ["/clear"],
      description: "Clear context.",
      insert_text: "/reset ",
    },
  ];

  const matches = filterCatalog(catalog, "clear");
  assert.equal(matches.length, 1);
  assert.equal(matches[0].label, "/reset");
  assert.equal(matches[0].insert_text, "/reset ");
});

test("slashArgumentContext offers structured choices after command plus space", () => {
  const catalog = [
    {
      label: "/router",
      aliases: [],
      argument_choices: [
        { value: "on", description: "Enable Router." },
        { value: "off", description: "Use direct mode." },
        { value: "status", description: "Show current strategy." },
      ],
    },
  ];

  const empty = slashArgumentContext("/router ", 8, catalog);
  assert.equal(empty.tokenStart, 8);
  assert.deepEqual(empty.items.map((item) => item.label), ["on", "off", "status"]);

  const partial = slashArgumentContext("/router st", 10, catalog);
  assert.equal(partial.query, "st");
  assert.equal(filterCatalog(partial.items, partial.query)[0].label, "status");
  assert.equal(slashArgumentContext("/unknown ", 9, catalog), null);
});

test("acceptCompletionText replaces the active token with insert text", () => {
  assert.deepEqual(acceptCompletionText("/cmp", 0, 4, "/compact "), {
    text: "/compact ",
    cursor: 9,
  });
  assert.deepEqual(acceptCompletionText("查看 @文", 3, 5, "@文档.md "), {
    text: "查看 @文档.md ",
    cursor: 10,
  });
});

test("shouldDropResponse rejects stale completion responses", () => {
  assert.equal(shouldDropResponse(2, 3), true);
  assert.equal(shouldDropResponse(3, 3), false);
});

test("menuKeyAction handles menu navigation before submit and cancel keys", () => {
  const menu = {
    active: true,
    selected: 1,
    filtered: [{ label: "a" }, { label: "b" }],
  };

  assert.deepEqual(menuKeyAction(menu, "up"), {
    handled: true,
    action: "navigate",
    menu: { ...menu, selected: 0 },
  });
  assert.deepEqual(menuKeyAction(menu, "down"), {
    handled: true,
    action: "navigate",
    menu: { ...menu, selected: 1 },
  });
  assert.deepEqual(menuKeyAction(menu, "escape"), {
    handled: true,
    action: "close",
    menu: { ...menu, active: false },
  });
  assert.deepEqual(menuKeyAction(menu, "return").action, "accept");
  assert.deepEqual(menuKeyAction(menu, "tab").action, "accept");
  assert.equal(menuKeyAction(menu, "backspace").handled, false);
});

test("Enter runs a highlighted slash command in one keystroke; Tab only completes", () => {
  const menu = {
    active: true,
    kind: "slash",
    selected: 0,
    filtered: [{ label: "/theme", insert_text: "/theme " }],
  };
  // Enter accepts AND submits (runs it) — no second Enter needed.
  assert.equal(menuKeyAction(menu, "return").action, "accept_submit");
  // Tab just completes so you can still type arguments (e.g. `/theme dark`).
  assert.equal(menuKeyAction(menu, "tab").action, "accept");
});

test("Enter on a file completion inserts the path without submitting the message", () => {
  const menu = {
    active: true,
    kind: "file",
    selected: 0,
    filtered: [{ label: "src/a.ts", insert_text: "@src/a.ts " }],
  };
  // File completions are part of a message being composed: never auto-submit.
  assert.equal(menuKeyAction(menu, "return").action, "accept");
  assert.equal(menuKeyAction(menu, "tab").action, "accept");
});

test("menuKeyAction lets Enter submit when the menu has no matches", () => {
  const empty = { active: true, kind: "command", filtered: [], selected: 0 };
  // With nothing to accept, Enter must NOT be swallowed — it falls through so the
  // message is submitted instead of silently lost.
  assert.equal(menuKeyAction(empty, "return").handled, false);
  // Tab just closes the menu (no stray insert), without submitting.
  const tab = menuKeyAction(empty, "tab");
  assert.equal(tab.handled, true);
  assert.equal(tab.action, "close");
});

test("menuKeyAction passes Up/Down through when the menu shows no matches", () => {
  const empty = { active: true, kind: "file", filtered: [], selected: 0 };
  // There is nothing to navigate — swallowing the arrows would block vertical
  // caret movement and history recall behind an empty "no matches" shell.
  assert.equal(menuKeyAction(empty, "up").handled, false);
  assert.equal(menuKeyAction(empty, "down").handled, false);
});

test("Enter on a skill suggestion completes without submitting the prompt prefix", () => {
  const menu = {
    active: true,
    kind: "slash",
    selected: 0,
    filtered: [
      {
        label: "/html-coder",
        insert_text: "use the html-coder skill: ",
        category: "skill",
      },
    ],
  };
  // The insert text is a prefix that still needs a task typed after it, so
  // Enter must behave like Tab (accept), never accept_submit.
  assert.equal(menuKeyAction(menu, "return").action, "accept");
  assert.equal(menuKeyAction(menu, "tab").action, "accept");
});

test("Enter still runs command and setting entries in one keystroke", () => {
  const command = {
    active: true,
    kind: "slash",
    selected: 0,
    filtered: [{ label: "/compact", insert_text: "/compact ", category: "command" }],
  };
  const setting = {
    active: true,
    kind: "slash",
    selected: 0,
    filtered: [{ label: "/cost", insert_text: "/cost", category: "setting" }],
  };
  assert.equal(menuKeyAction(command, "return").action, "accept_submit");
  assert.equal(menuKeyAction(setting, "return").action, "accept_submit");
});

test("Enter completes a required-argument command without a bare picker", () => {
  const menu = {
    active: true,
    kind: "slash",
    selected: 0,
    filtered: [{
      label: "/delete",
      insert_text: "/delete ",
      category: "session",
      submit_behavior: "complete",
    }],
  };
  assert.equal(menuKeyAction(menu, "return").action, "accept");
});

test("ipc dispatcher routes completion responses", () => {
  let seen = null;
  const dispatch = createDispatcher({
    completionResponse: (message) => {
      seen = message;
    },
    unknown: () => {},
  });

  dispatch({ type: "completion.response", request_id: 7, kind: "file", items: [] });

  assert.deepEqual(seen, {
    type: "completion.response",
    request_id: 7,
    kind: "file",
    items: [],
  });
});

test("line-edit index helpers compute line and word boundaries", () => {
  // line start: just after the previous newline (or 0)
  assert.equal(lineStartIndex("abc", 2), 0);
  assert.equal(lineStartIndex("ab\ncd", 5), 3);
  // line end: the next newline (or end of text)
  assert.equal(lineEndIndex("abc", 1), 3);
  assert.equal(lineEndIndex("ab\ncd", 0), 2);
  // word start: skip trailing whitespace, then the word
  assert.equal(wordStartIndex("hello world", 11), 6);
  assert.equal(wordStartIndex("hello world  ", 13), 6);
  assert.equal(wordStartIndex("solo", 4), 0);
  // word end: skip leading whitespace, then the word
  assert.equal(wordEndIndex("hello world", 0), 5);
  assert.equal(wordEndIndex("  hello", 0), 7);
  assert.equal(wordEndIndex("hello world", 5), 11);
  // splice removes the [from,to) range and collapses the caret to the cut start
  assert.deepEqual(spliceOut("hello world", 6, 11), { text: "hello ", cursor: 6 });
  assert.deepEqual(spliceOut("hello world", 11, 6), { text: "hello ", cursor: 6 }); // order-agnostic
});
