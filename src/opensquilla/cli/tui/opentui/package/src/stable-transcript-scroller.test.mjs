import assert from "node:assert/strict";
import test from "node:test";

import {
  installConversationWheelHandler,
  invalidateConversationViewport,
  scheduleConversationLayoutCommit,
} from "./opentuiCompat.mjs";
import { createStableTranscriptScroller } from "./stableTranscriptScroller.mjs";

function harness() {
  const scrollBox = { scrollTop: 80, scrollHeight: 100, height: 20 };
  const scheduled = [];
  const states = [];
  let invalidations = 0;
  const scroller = createStableTranscriptScroller({
    scrollBox,
    renderer: {},
    scheduleFrame: (callback) => { scheduled.push(callback); return callback; },
    cancelFrame: () => {},
    invalidate: () => { invalidations += 1; },
    onStateChange: (state) => states.push(state),
  });
  return { scrollBox, scheduled, states, scroller, invalidations: () => invalidations };
}

test("wheel ownership uses OpenTUI's public scroll callback and acceleration API", () => {
  const calls = [];
  const scrollBox = {
    scrollAcceleration: null,
  };
  assert.equal(installConversationWheelHandler(scrollBox, (event) => {
    calls.push(["app", event.type]);
    return true;
  }), true);

  const event = {
    type: "scroll",
    stopPropagation: () => calls.push(["stop"]),
    preventDefault: () => calls.push(["prevent"]),
  };
  scrollBox.onMouseScroll(event);
  assert.deepEqual(calls, [["app", "scroll"], ["stop"], ["prevent"]]);
  assert.equal(scrollBox.scrollAcceleration.tick(), 0);
  scrollBox.scrollAcceleration.reset();
});

test("routine viewport invalidation never requests a full framebuffer repaint", () => {
  let viewportRenders = 0;
  let rendererRenders = 0;
  const renderer = {
    forceFullRepaintRequested: false,
    requestRender: () => { rendererRenders += 1; },
  };
  const scrollBox = {
    requestRender: () => { viewportRenders += 1; },
  };

  invalidateConversationViewport(renderer, scrollBox);

  assert.equal(renderer.forceFullRepaintRequested, false);
  assert.equal(viewportRenders, 1);
  assert.equal(rendererRenders, 0);
});

test("layout commits run after Yoga calculation and before the paint callback returns", () => {
  let frameCallback = null;
  let calculated = 0;
  const renderer = {
    root: { calculateLayout: () => { calculated += 1; } },
    setFrameCallback: (callback) => { frameCallback = callback; },
    removeFrameCallback: (callback) => {
      if (frameCallback === callback) frameCallback = null;
    },
    requestRender: () => {},
  };
  const bar = { scrollSize: 10, viewportSize: 10 };
  const scrollBox = {
    content: { getLayoutNode: () => ({ getComputedLayout: () => ({ height: 120 }) }) },
    viewport: { getLayoutNode: () => ({ getComputedLayout: () => ({ height: 30 }) }) },
    verticalScrollBar: bar,
  };
  const seen = [];
  scheduleConversationLayoutCommit(renderer, scrollBox, () => {
    seen.push([calculated, bar.scrollSize, bar.viewportSize]);
  });

  assert.equal(typeof frameCallback, "function");
  frameCallback();
  assert.deepEqual(seen, [[1, 120, 30]]);
  assert.equal(frameCallback, null);
});

test("wheel updates are coalesced and upward scrolling enters held mode", () => {
  const h = harness();
  h.scroller.handleWheel({ type: "scroll", scroll: { direction: "up", delta: 1 } });
  h.scroller.handleWheel({ type: "scroll", scroll: { direction: "up", delta: 2 } });
  assert.equal(h.scheduled.length, 1);
  h.scheduled.shift()();
  assert.equal(h.scrollBox.scrollTop, 71);
  assert.equal(h.scroller.followMode, "held");
  assert.equal(h.invalidations(), 1);
});

test("held viewport does not jump when streaming content grows", () => {
  const h = harness();
  h.scroller.handleWheel({ scroll: { direction: "up", delta: 1 } });
  h.scheduled.shift()();
  const top = h.scrollBox.scrollTop;
  h.scroller.mutate(() => { h.scrollBox.scrollHeight += 40; });
  h.scheduled.shift()(); // pre-paint layout/anchor commit
  assert.equal(h.scrollBox.scrollTop, top);
  assert.equal(h.scroller.snapshot().newOutput, true);
});

test("upward intent survives a transient no-range streaming layout", () => {
  const scrollBox = {
    scrollTop: 0,
    scrollHeight: 28,
    height: 28,
    stickyScroll: true,
  };
  const scheduled = [];
  const scroller = createStableTranscriptScroller({
    scrollBox,
    renderer: {},
    scheduleFrame: (callback) => { scheduled.push(callback); return callback; },
    cancelFrame: () => {},
    invalidate: () => {},
  });

  scroller.handleWheel({ type: "scroll", scroll: { direction: "up", delta: 2 } });
  scheduled.shift()();
  assert.equal(scroller.followMode, "held");
  assert.equal(scrollBox.stickyScroll, false);

  scroller.mutate(() => { scrollBox.scrollHeight = 34; });
  scheduled.shift()();
  assert.equal(scrollBox.scrollTop, 0);
  assert.equal(scroller.snapshot().newOutput, true);

  scroller.followLatest();
  assert.equal(scrollBox.scrollTop, 6);
  assert.equal(scrollBox.stickyScroll, true);
  assert.equal(scroller.followMode, "following");
});

test("returning to the bottom resumes following", () => {
  const h = harness();
  h.scroller.handleWheel({ scroll: { direction: "up", delta: 1 } });
  h.scheduled.shift()();
  h.scroller.followLatest();
  assert.equal(h.scrollBox.scrollTop, 80);
  assert.equal(h.scroller.followMode, "following");
  h.scroller.mutate(() => { h.scrollBox.scrollHeight += 10; });
  h.scheduled.shift()();
  assert.equal(h.scrollBox.scrollTop, 90);
});

test("scroll range follows OpenTUI viewport height instead of outer box height", () => {
  const scrollBox = {
    scrollTop: 0,
    scrollHeight: 100,
    height: 30,
    viewport: { height: 20 },
  };
  const scheduled = [];
  const scroller = createStableTranscriptScroller({
    scrollBox,
    renderer: {},
    scheduleFrame: (callback) => { scheduled.push(callback); return callback; },
    cancelFrame: () => {},
    invalidate: () => {},
  });

  scroller.followLatest();
  assert.equal(scrollBox.scrollTop, 80);
});

test("surface rebuild advances one epoch and commits one pre-paint restore", () => {
  const scrollBox = { scrollTop: 80, scrollHeight: 100, height: 20 };
  const scheduled = [];
  const order = [];
  const scroller = createStableTranscriptScroller({
    scrollBox,
    renderer: {},
    scheduleFrame: (callback) => { scheduled.push(callback); return callback; },
    cancelFrame: () => {},
    invalidate: () => { order.push("invalidate"); },
  });

  scroller.restoreSurface(
    () => {
      order.push("layout");
      scrollBox.scrollHeight = 140;
    },
    { afterLayout: () => order.push("cursor") },
  );

  assert.equal(scroller.snapshot().surfaceEpoch, 1);
  assert.equal(scroller.followMode, "restoring");
  assert.equal(scheduled.length, 1);
  scheduled.shift()();
  assert.equal(scrollBox.scrollTop, 120);
  assert.equal(scroller.followMode, "following");
  // The surface caller marks the already-scheduled frame as a full repaint.
  // No invalidation may happen from inside the frame callback, because
  // OpenTUI would interpret it as an immediate second physical frame.
  assert.deepEqual(order, ["layout", "cursor"]);
});

test("surface rebuild preserves a wheel gesture pending at the frame boundary", () => {
  const scrollBox = {
    scrollTop: 80,
    scrollHeight: 100,
    height: 20,
    viewport: { height: 20 },
  };
  const scheduled = [];
  const cancelled = new Set();
  const scroller = createStableTranscriptScroller({
    scrollBox,
    renderer: {},
    scheduleFrame: (callback) => {
      const token = { callback };
      scheduled.push(token);
      return token;
    },
    cancelFrame: (token) => { cancelled.add(token); },
    invalidate: () => {},
  });

  scroller.handleWheel({ scroll: { direction: "up", delta: 1 } });
  const wheelFrame = scheduled.shift();
  scroller.restoreSurface(() => { scrollBox.scrollHeight = 140; });
  assert.equal(cancelled.has(wheelFrame), true);
  scheduled.shift().callback();

  assert.equal(scrollBox.scrollTop, 77);
  assert.equal(scroller.followMode, "held");
});

test("a nested surface rebuild preserves wheel rows already merged into the transaction", () => {
  const scrollBox = {
    scrollTop: 80,
    scrollHeight: 100,
    height: 20,
    viewport: { height: 20 },
  };
  const scheduled = [];
  const cancelled = new Set();
  const scroller = createStableTranscriptScroller({
    scrollBox,
    renderer: {},
    scheduleFrame: (callback) => {
      const token = { callback };
      scheduled.push(token);
      return token;
    },
    cancelFrame: (token) => { cancelled.add(token); },
    invalidate: () => {},
  });

  scroller.handleWheel({ scroll: { direction: "up", delta: 1 } });
  const wheelFrame = scheduled.shift();
  scroller.restoreSurface(() => { scrollBox.scrollHeight = 120; });
  const firstSurfaceFrame = scheduled.shift();
  scroller.restoreSurface(() => { scrollBox.scrollHeight = 140; });
  const finalSurfaceFrame = scheduled.shift();

  assert.equal(cancelled.has(wheelFrame), true);
  assert.equal(cancelled.has(firstSurfaceFrame), true);
  finalSurfaceFrame.callback();
  assert.equal(scrollBox.scrollTop, 77);
  assert.equal(scroller.followMode, "held");
  assert.equal(scroller.snapshot().surfaceEpoch, 2);
});

test("surface rebuild leaves restoring mode even when relayout throws", () => {
  const h = harness();
  assert.throws(
    () => h.scroller.restoreSurface(() => { throw new Error("layout failed"); }),
    /layout failed/,
  );
  assert.equal(h.scroller.followMode, "restoring");
  h.scheduled.shift()();
  assert.equal(h.scroller.followMode, "following");
});

test("streaming mutations coalesce into one pre-paint anchor restore", () => {
  const h = harness();
  h.scroller.handleWheel({ scroll: { direction: "up", delta: 1 } });
  h.scheduled.shift()();
  const top = h.scrollBox.scrollTop;

  h.scroller.mutate(() => { h.scrollBox.scrollHeight += 10; });
  h.scroller.mutate(() => { h.scrollBox.scrollHeight += 10; });
  h.scroller.mutate(() => { h.scrollBox.scrollHeight += 10; });
  assert.equal(h.scheduled.length, 1);
  h.scheduled.shift()();

  assert.equal(h.scrollBox.scrollTop, top);
  assert.equal(h.scroller.snapshot().newOutput, true);
});
