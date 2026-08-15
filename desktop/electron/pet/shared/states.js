'use strict';

// Single source of truth for the pet state vocabulary.
//
// Required by the main process (backend/core.js), loaded as a <script> by the
// renderer (renderer/pet.html → window.OctoStates), and imported by the
// state-machine test. Keeping ONE copy ends the historical drift where five
// separate lists disagreed — e.g. the test's hand-copy silently missed
// 'loafing', blinding the class-leak assertion to that state.
//
// UMD shim: module.exports for Node require(), window.OctoStates for the browser
// <script> and the vm-sandboxed test.

(function (root, factory) {
  const api = factory();
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (root) root.OctoStates = api;
})(typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : this), function () {
  // Backend aggregation priority — highest wins for the global mood across
  // multiple sessions.
  const STATE_PRIORITY = {
    error: 8,
    notification: 7,
    sweeping: 6,
    attention: 5,
    carrying: 4,
    juggling: 4,
    working: 3,
    thinking: 2,
    idle: 1,
    roam: 1,
    sleeping: 0,
  };

  // Oneshot states decay back to idle after their TTL if no further event lands
  // (notification is excluded — it means "waiting for you" and must persist).
  const ONESHOT_STATES = ['attention', 'error', 'sweeping', 'notification', 'carrying'];
  const ONESHOT_TTL_MS = { attention: 15000, carrying: 15000, sweeping: 20000, error: 45000 };

  // Falling-asleep sequence — vocabulary reserved; no producer yet.
  const SLEEP_SEQUENCE = ['yawning', 'dozing', 'collapsing', 'sleeping', 'waking'];

  // Busy = counts toward the stuck-sweep + transcript polling in core.
  const BUSY_STATES = ['working', 'thinking', 'juggling', 'carrying', 'sweeping'];

  // Every state the /state route accepts (backend vocabulary).
  const VALID_STATES = Array.from(new Set([...Object.keys(STATE_PRIORITY), ...SLEEP_SEQUENCE]));

  // Renderer-only synthesized states + emotion tints (no backend priority entry).
  const RENDER_EXTRA = [
    'loafing', 'happy', 'waiting', 'needsinput', 'greet', 'talking',
    'loved', 'sad', 'sorry', 'excited', 'puzzled',
    'anxious', 'angry', 'crying', 'eating', 'sick', 'drinking', 'chou',
  ];

  // Every class word the renderer may put on a skin element. classList.remove
  // MUST cover this whole set or a stale state class leaks. The class-leak test
  // iterates the SAME list, so cleanup and assertion can't drift apart.
  const RENDER_STATE_WORDS = Array.from(new Set([...VALID_STATES, ...RENDER_EXTRA]));

  function getPriority(state) { return STATE_PRIORITY[state] || 0; }

  return {
    STATE_PRIORITY,
    ONESHOT_STATES,
    ONESHOT_TTL_MS,
    SLEEP_SEQUENCE,
    BUSY_STATES,
    VALID_STATES,
    RENDER_EXTRA,
    RENDER_STATE_WORDS,
    getPriority,
  };
});
