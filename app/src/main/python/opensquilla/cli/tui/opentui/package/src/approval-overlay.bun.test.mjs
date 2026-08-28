// Tool-approval overlay tests:
//   - approvalKeyAction maps keys to approve/deny/navigate/choose, is modal
//     (swallows every other key while open), and passes Ctrl+C through so the
//     interrupt path is never trapped behind a pending approval;
//   - the composer mounts the overlay on approval.request, sends one
//     approval.response frame per decision, and clears the overlay afterwards.
//
// Run with: bun test src/approval-overlay.bun.test.mjs
import { test, expect } from "bun:test";

import { approvalKeyAction, approvalRowPlan, createComposer } from "./composer.mjs";
import { THEME, applyTheme } from "./theme.mjs";

// Minimal fake renderable mirroring the add/remove/getChildren contract the
// composer relies on (same shape as overlay-mount.test.mjs).
class FakeNode {
  constructor(_renderer, options = {}) {
    this.options = options;
    this.id = options.id;
    this.zIndex = options.zIndex ?? 0;
    this.children = [];
  }
  add(node) {
    this.children.push(node);
    return this.children.length;
  }
  remove(node) {
    this.children = this.children.filter((child) => child !== node);
  }
  getChildren() {
    return this.children;
  }
}

function makeHarness({ terminalWidth = 100, terminalHeight = 24 } = {}) {
  applyTheme("opensquilla-dark");
  const keypressHandlers = [];
  const pasteHandlers = [];
  const sent = [];
  let renderRequests = 0;
  const renderer = {
    terminalWidth,
    terminalHeight,
    keyInput: {
      on(event, handler) {
        if (event === "keypress") keypressHandlers.push(handler);
        if (event === "paste") pasteHandlers.push(handler);
      },
    },
    setCursorPosition() {},
    requestRender() { renderRequests += 1; },
  };
  const conversationBox = new FakeNode(renderer, { id: "conversation" });
  conversationBox.scrollBy = () => {};
  const inputBox = new FakeNode(renderer, { id: "input-region" });
  const overlayLayer = new FakeNode(renderer, { id: "overlay-layer", zIndex: 1000 });
  const composer = createComposer({
    renderer,
    BoxRenderable: FakeNode,
    TextRenderable: FakeNode,
    conversationBox,
    inputBox,
    overlayLayer,
    footerHeight: 6,
    sendHostMessage: (m) => sent.push(m),
  });
  composer.install();
  const press = (key) => keypressHandlers.forEach((handler) => handler(key));
  const paste = (text) =>
    pasteHandlers.forEach((handler) => handler({ bytes: new TextEncoder().encode(text) }));
  const type = (text) => {
    for (const ch of text) press({ name: ch === " " ? "space" : ch, sequence: ch });
  };
  return {
    composer,
    press,
    paste,
    type,
    overlayLayer,
    sent,
    renderRequests: () => renderRequests,
  };
}

function findDeep(node, id) {
  if (node.id === id) return node;
  for (const child of node.getChildren?.() ?? []) {
    const hit = findDeep(child, id);
    if (hit) return hit;
  }
  return null;
}

const request = (overrides = {}) => ({
  id: "appr-1",
  tool: "shell",
  summary: "touch demo.txt",
  choices: [],
  ...overrides,
});

const CHOICES = ["allow_once", "allow_same_type", "deny"];

test("approvalKeyAction maps decision keys, is modal, and passes Ctrl+C through", () => {
  const overlay = { active: true, choices: CHOICES, selected: 1 };
  expect(approvalKeyAction(overlay, { name: "y" })).toMatchObject({ action: "approve" });
  expect(approvalKeyAction(overlay, { name: "n" })).toMatchObject({ action: "deny" });
  expect(approvalKeyAction(overlay, { name: "escape" })).toMatchObject({ action: "deny" });
  expect(approvalKeyAction(overlay, { name: "up" })).toMatchObject({
    action: "navigate", selected: 0,
  });
  expect(approvalKeyAction(overlay, { name: "down" })).toMatchObject({
    action: "navigate", selected: 2,
  });
  // clamps at the ends
  expect(approvalKeyAction({ ...overlay, selected: 0 }, { name: "up" })).toMatchObject({
    selected: 0,
  });
  expect(approvalKeyAction({ ...overlay, selected: 2 }, { name: "down" })).toMatchObject({
    selected: 2,
  });
  expect(approvalKeyAction(overlay, { name: "return" })).toMatchObject({
    action: "choose", selected: 1,
  });
  // without choices, Enter approves and Up/Down are swallowed as plain keys
  const bare = { active: true, choices: [], selected: 0 };
  expect(approvalKeyAction(bare, { name: "return" })).toMatchObject({ action: "approve" });
  expect(approvalKeyAction(bare, { name: "up" })).toMatchObject({ handled: true, action: "none" });
  // modal: every other key is swallowed so it never leaks into the input
  expect(approvalKeyAction(overlay, { name: "x", sequence: "x" })).toMatchObject({
    handled: true, action: "none",
  });
  // Ctrl+C must fall through to the interrupt path
  expect(approvalKeyAction(overlay, { name: "c", ctrl: true })).toMatchObject({
    handled: false, action: "pass",
  });
  // inactive overlay passes keys through
  expect(approvalKeyAction(null, { name: "y" })).toMatchObject({ handled: false });
});

test("modified chords are swallowed, never treated as decisions", () => {
  // Ctrl+Y is the composer's yank chord: reaching for it just as the overlay
  // pops must NOT approve a permission-gated tool the user never read.
  const bare = { active: true, choices: [], selected: 0 };
  const withChoices = { active: true, choices: CHOICES, selected: 1 };
  const swallowed = { handled: true, action: "none" };
  expect(approvalKeyAction(bare, { name: "y", ctrl: true })).toMatchObject(swallowed);
  expect(approvalKeyAction(bare, { name: "y", meta: true })).toMatchObject(swallowed);
  expect(approvalKeyAction(bare, { name: "y", option: true })).toMatchObject(swallowed);
  expect(approvalKeyAction(bare, { name: "n", ctrl: true })).toMatchObject(swallowed);
  expect(approvalKeyAction(bare, { name: "n", alt: true })).toMatchObject(swallowed);
  expect(approvalKeyAction(bare, { name: "return", ctrl: true })).toMatchObject(swallowed);
  expect(approvalKeyAction(withChoices, { name: "return", alt: true })).toMatchObject(swallowed);
  expect(approvalKeyAction(withChoices, { name: "escape", ctrl: true })).toMatchObject(swallowed);
});

test("Ctrl+Y while the overlay is open neither approves nor yanks into the draft", () => {
  const { composer, press, type, overlayLayer, sent } = makeHarness();
  // Kill some text so the yank chord has something it WOULD re-insert.
  type("draft");
  press({ name: "u", ctrl: true }); // kill: input empty, kill buffer "draft"
  composer.openApprovalOverlay(request());

  press({ name: "y", ctrl: true }); // muscle-memory yank mid-approval

  expect(sent.some((m) => m.type === "approval.response")).toBe(false);
  expect(findDeep(overlayLayer, "approval-overlay")).not.toBeNull();

  press({ name: "n", sequence: "n" }); // deny, close the overlay
  // Enter on an empty draft is a no-op (no submit frame), so prove the chord
  // inserted nothing with a sentinel: the submission must be ONLY the sentinel.
  type("X");
  press({ name: "return" });
  expect(sent.find((m) => m.type === "input.submit")?.text).toBe("X");
});

test("overlay mounts on approval.request with tool, summary, and hint", () => {
  const { composer, overlayLayer } = makeHarness();
  composer.openApprovalOverlay(request());

  const overlay = findDeep(overlayLayer, "approval-overlay");
  expect(overlay).not.toBeNull();
  expect(overlayLayer.visible).toBe(true);
  expect(findDeep(overlay, "approval-overlay-tool").options.content).toContain("shell");
  expect(findDeep(overlay, "approval-overlay-summary").options.content)
    .toContain("touch demo.txt");
  expect(findDeep(overlay, "approval-overlay-hint").options.content).toContain("approve");
});

test("a request without an id never mounts an unanswerable overlay", () => {
  const { composer, overlayLayer } = makeHarness();
  composer.openApprovalOverlay({ tool: "shell", summary: "no id" });
  expect(findDeep(overlayLayer, "approval-overlay")).toBeNull();
});

test("y approves and clears the overlay", () => {
  const { composer, press, overlayLayer, sent } = makeHarness();
  composer.openApprovalOverlay(request());

  press({ name: "y", sequence: "y" });

  expect(sent).toContainEqual({
    type: "approval.response", id: "appr-1", approved: true, choice: null,
  });
  expect(findDeep(overlayLayer, "approval-overlay")).toBeNull();
});

test("n and Escape deny", () => {
  const denyByN = makeHarness();
  denyByN.composer.openApprovalOverlay(request());
  denyByN.press({ name: "n", sequence: "n" });
  expect(denyByN.sent).toContainEqual({
    type: "approval.response", id: "appr-1", approved: false, choice: null,
  });

  const denyByEscape = makeHarness();
  denyByEscape.composer.openApprovalOverlay(request());
  denyByEscape.press({ name: "escape" });
  expect(denyByEscape.sent).toContainEqual({
    type: "approval.response", id: "appr-1", approved: false, choice: null,
  });
  expect(findDeep(denyByEscape.overlayLayer, "approval-overlay")).toBeNull();
});

test("Up/Down navigate choices and Enter confirms the highlighted one", () => {
  const { composer, press, overlayLayer, sent } = makeHarness();
  composer.openApprovalOverlay(request({ choices: CHOICES }));

  press({ name: "down" });
  const overlay = findDeep(overlayLayer, "approval-overlay");
  expect(findDeep(overlay, "approval-overlay-choice-1").options.content).toContain("› ");
  expect(findDeep(overlay, "approval-overlay-choice-1").options.content)
    .toContain("allow same type");

  press({ name: "return" });
  expect(sent).toContainEqual({
    type: "approval.response", id: "appr-1", approved: true, choice: "allow_same_type",
  });
  expect(findDeep(overlayLayer, "approval-overlay")).toBeNull();
});

test("the overlay swallows typing and paste so keys never leak into the draft", () => {
  const { composer, press, paste, type, sent } = makeHarness();
  composer.openApprovalOverlay(request());

  type("abc");
  paste("sneaky");
  press({ name: "n", sequence: "n" }); // deny, close the overlay
  // Enter on an empty draft is a no-op (no submit frame): a sentinel typed
  // after the deny proves nothing leaked — the submission is ONLY the sentinel.
  type("X");
  press({ name: "return" });

  expect(sent.find((m) => m.type === "input.submit")?.text).toBe("X");
});

test("Ctrl+C keeps its cancel path while the overlay is open", () => {
  const { composer, press, overlayLayer, sent } = makeHarness();
  composer.openApprovalOverlay(request());

  press({ name: "c", ctrl: true });

  expect(sent).toContainEqual({ type: "input.cancel" });
  // Cancelling the turn is not a decision: the overlay stays until one is made
  // (or the Python side times the request out into a deny).
  expect(findDeep(overlayLayer, "approval-overlay")).not.toBeNull();
});

test("the overlay survives ordinary footer re-renders instead of flashing away", () => {
  const { composer, overlayLayer } = makeHarness();
  composer.openApprovalOverlay(request({ choices: CHOICES }));

  composer.rerender();

  const overlays = overlayLayer
    .getChildren()
    .filter((child) => child.id === "approval-overlay");
  expect(overlays.length).toBe(1);
});

test("overlay chrome and rows use active THEME tokens", () => {
  const { composer, overlayLayer } = makeHarness();
  composer.openApprovalOverlay(request({ choices: CHOICES }));

  const overlay = findDeep(overlayLayer, "approval-overlay");
  expect(overlay.options.borderColor).toBe(THEME.warning);
  expect(overlay.options.backgroundColor).toBe(THEME.overlayBg);
  expect(findDeep(overlay, "approval-overlay-tool").options.fg).toBe(THEME.text);
  expect(findDeep(overlay, "approval-overlay-summary").options.fg).toBe(THEME.muted);
  expect(findDeep(overlay, "approval-overlay-choice-0").options.fg).toBe(THEME.brandAccentSoft);
  expect(findDeep(overlay, "approval-overlay-choice-1").options.fg).toBe(THEME.muted);
  expect(findDeep(overlay, "approval-overlay-hint").options.fg).toBe(THEME.detailText);
});

test("an approval request closes an open theme picker instead of stacking overlays", () => {
  const { composer, press, overlayLayer, sent } = makeHarness();
  composer.openThemePicker();
  composer.openApprovalOverlay(request());

  expect(findDeep(overlayLayer, "theme-picker")).toBeNull();
  expect(findDeep(overlayLayer, "approval-overlay")).not.toBeNull();

  // Approval keys drive the approval overlay, not the (closed) picker.
  press({ name: "y", sequence: "y" });
  expect(sent).toContainEqual({
    type: "approval.response", id: "appr-1", approved: true, choice: null,
  });
});

test("approval.dismiss closes the overlay with one footer redraw", () => {
  const { composer, press, overlayLayer, sent, renderRequests } = makeHarness();
  composer.openApprovalOverlay(request());
  const beforeDismiss = renderRequests();

  // A dismiss for some other (older) request must not touch the live overlay.
  composer.dismissApprovalOverlay("appr-0");
  expect(findDeep(overlayLayer, "approval-overlay")).not.toBeNull();
  expect(renderRequests()).toBe(beforeDismiss);

  // The matching dismiss closes the overlay without emitting a decision:
  // Python already resolved the request, so a response would only be dropped.
  composer.dismissApprovalOverlay("appr-1");
  expect(findDeep(overlayLayer, "approval-overlay")).toBeNull();
  expect(renderRequests()).toBe(beforeDismiss + 1);
  expect(sent.filter((m) => m.type === "approval.response")).toEqual([]);

  // Keys reach the composer again once the modal is gone: a bare "y" is
  // ordinary typed input, never a decision for the dismissed request.
  press({ name: "y", sequence: "y" });
  expect(sent.filter((m) => m.type === "approval.response")).toEqual([]);
});

test("approval.dismiss with no overlay open is a safe no-op", () => {
  const { composer, overlayLayer } = makeHarness();
  composer.dismissApprovalOverlay("appr-1");
  expect(findDeep(overlayLayer, "approval-overlay")).toBeNull();
});

test("overlay renders the rationale message the console prompt prints", () => {
  const { composer, overlayLayer } = makeHarness();
  composer.openApprovalOverlay(request({ message: "Sandbox denied a write outside the workspace." }));

  const overlay = findDeep(overlayLayer, "approval-overlay");
  expect(findDeep(overlay, "approval-overlay-message").options.content)
    .toContain("Sandbox denied a write");
});

test("a long summary wraps into rows instead of clipping to one line", () => {
  const { composer, overlayLayer } = makeHarness();
  const command = "uv run pytest tests/unit/cli/tui --maxfail 1 --durations 10 -k approval_overlay_and_more_selectors";
  composer.openApprovalOverlay(request({ summary: command }));

  const overlay = findDeep(overlayLayer, "approval-overlay");
  const first = findDeep(overlay, "approval-overlay-summary").options.content;
  const second = findDeep(overlay, "approval-overlay-summary-1")?.options.content ?? "";
  // The full command must survive across the wrapped rows — the user is
  // authorizing exactly this text.
  expect(`${first} ${second}`.replace(/\s+/gu, " ")).toContain("approval_overlay_and_more_selectors");
  expect(first.endsWith("…")).toBe(false);
});

test("a pathological summary is capped with an explicit tail marker", () => {
  const { composer, overlayLayer } = makeHarness();
  const huge = Array.from({ length: 40 }, (_, i) => `arg-number-${i}-padding-padding`).join(" ");
  composer.openApprovalOverlay(request({ summary: huge }));

  const overlay = findDeep(overlayLayer, "approval-overlay");
  // Capped at six rows: row index 5 exists, row index 6 must not.
  const last = findDeep(overlay, "approval-overlay-summary-5");
  expect(last).not.toBeNull();
  expect(findDeep(overlay, "approval-overlay-summary-6")).toBeNull();
  expect(last.options.content).toContain("…");
  // Choices and the key hint stay visible below the capped summary.
  expect(findDeep(overlay, "approval-overlay-hint")).not.toBeNull();
});

test("a short terminal keeps the choices AND at least one summary row", () => {
  // At height 14 (footer 6) the row budget is 7: borders + tool + 3 choices
  // leave exactly one flexible row. That row must go to the summary — the
  // user is authorizing that text — while the key hint (redundant with the
  // y/n/esc keys themselves) is what gets dropped.
  const { composer, overlayLayer } = makeHarness({ terminalHeight: 14 });
  const huge = Array.from({ length: 40 }, (_, i) => `arg-number-${i}-padding-padding`).join(" ");
  composer.openApprovalOverlay(request({
    summary: huge,
    message: "This tool call was gated by the standard policy tier.",
    choices: CHOICES,
  }));

  const overlay = findDeep(overlayLayer, "approval-overlay");
  CHOICES.forEach((_, index) => {
    expect(findDeep(overlay, `approval-overlay-choice-${index}`)).not.toBeNull();
  });
  const summary = findDeep(overlay, "approval-overlay-summary");
  expect(summary).not.toBeNull();
  expect(summary.options.content).toContain("arg-number-0");
  expect(summary.options.content).toContain("…"); // truncation is explicit
  expect(findDeep(overlay, "approval-overlay-summary-1")).toBeNull(); // capped to the one row
  expect(findDeep(overlay, "approval-overlay-hint")).toBeNull(); // hint is what gives way
});

test("one more row restores the hint before growing the summary", () => {
  const { composer, overlayLayer } = makeHarness({ terminalHeight: 15 });
  const huge = Array.from({ length: 40 }, (_, i) => `arg-number-${i}-padding-padding`).join(" ");
  composer.openApprovalOverlay(request({ summary: huge, choices: CHOICES }));

  const overlay = findDeep(overlayLayer, "approval-overlay");
  expect(findDeep(overlay, "approval-overlay-summary")).not.toBeNull();
  expect(findDeep(overlay, "approval-overlay-hint")).not.toBeNull();
  expect(findDeep(overlay, "approval-overlay-summary-1")).toBeNull();
});

test("approvalRowPlan never zeroes the summary while rows remain", () => {
  // Sweep realistic geometries: for every budget that can hold the immovable
  // rows plus one more, the plan must include at least one summary row.
  for (let maxRows = 4; maxRows <= 30; maxRows += 1) {
    for (const choices of [0, 2, 3]) {
      const plan = approvalRowPlan(maxRows, choices, 99, 99);
      const immovable = 2 + 1 + choices;
      if (maxRows > immovable) {
        expect(plan.summaryRows).toBeGreaterThanOrEqual(1);
      }
      // The plan must always fit the budget it was given.
      const used = immovable + plan.summaryRows + plan.messageRows + (plan.hint ? 1 : 0);
      expect(used).toBeLessThanOrEqual(Math.max(maxRows, immovable));
    }
  }
});

test("a short summary rebates its unused reservation to the message", () => {
  // maxRows 13 with 3 choices leaves a budget of 7. A one-row summary must
  // not reserve the worst-case six rows and starve the rationale message.
  const plan = approvalRowPlan(13, 3, 1, 3);
  expect(plan.summaryRows).toBe(1);
  expect(plan.hint).toBe(true);
  expect(plan.messageRows).toBe(3);
});

test("the rationale message renders alongside a short summary on a 20-row terminal", () => {
  const { composer, overlayLayer } = makeHarness({ terminalHeight: 20 });
  composer.openApprovalOverlay(request({
    summary: "touch demo.txt",
    message: "This tool call was gated by the standard policy tier.",
    choices: CHOICES,
  }));

  const overlay = findDeep(overlayLayer, "approval-overlay");
  expect(findDeep(overlay, "approval-overlay-summary")).not.toBeNull();
  expect(findDeep(overlay, "approval-overlay-message").options.content)
    .toContain("standard policy tier");
  expect(findDeep(overlay, "approval-overlay-hint")).not.toBeNull();
});

test("choice ids are stripped of control bytes for display only", () => {
  const { composer, press, overlayLayer, sent } = makeHarness();
  const hostile = "\u001b]0;pwn\u0007allow_once";
  composer.openApprovalOverlay(request({ choices: [hostile, "deny"] }));

  const overlay = findDeep(overlayLayer, "approval-overlay");
  const label = findDeep(overlay, "approval-overlay-choice-0").options.content;
  expect(label).not.toContain("\u001b");
  expect(label).not.toContain("\u0007");
  expect(label).toContain("allow once");

  // The RAW id still round-trips in the response — display stripping must
  // never desynchronize the decision protocol.
  press({ name: "return" });
  const response = sent.find((m) => m.type === "approval.response");
  expect(response.choice).toBe(hostile);
});

test("a terminal too small for any summary row signposts the hidden text", () => {
  // Height 13 (footer 6) leaves maxRows 6 = exactly the immovable rows for a
  // 3-choice envelope: zero summary rows fit, so the title must say so.
  const { composer, overlayLayer } = makeHarness({ terminalHeight: 13 });
  composer.openApprovalOverlay(request({
    summary: "rm -rf /tmp/demo",
    choices: CHOICES,
  }));

  const overlay = findDeep(overlayLayer, "approval-overlay");
  expect(findDeep(overlay, "approval-overlay-summary")).toBeNull();
  expect(overlay.options.title).toContain("summary hidden");
  CHOICES.forEach((_, index) => {
    expect(findDeep(overlay, `approval-overlay-choice-${index}`)).not.toBeNull();
  });
});
