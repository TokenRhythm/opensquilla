import assert from "node:assert/strict";
import test from "node:test";

import { NOTICE_LEVELS, isStructuredLine, noticeContent, parseNotice } from "./ansiNotice.mjs";
import { TOOL_INDENT } from "./primitives.mjs";

const ESC = "\u001b";
const BEL = "\u0007";

test("parseNotice strips ANSI and classifies the dominant severity level", () => {
  const cases = [
    // [raw, expected level, expected clean text]
    [`${ESC}[38;2;245;102;0mcompacting context...${ESC}[0m`, "accent", "compacting context..."],
    [`${ESC}[33mcompact: flush service is unavailable${ESC}[0m`, "warn", "compact: flush service is unavailable"],
    [`${ESC}[31mUnknown command. Use /help.${ESC}[0m`, "error", "Unknown command. Use /help."],
    [`${ESC}[32mStarted new session${ESC}[0m`, "success", "Started new session"],
    [`${ESC}[38;2;14;122;82mcompacted ok${ESC}[0m`, "success", "compacted ok"],
    [
      `${ESC}[38;2;245;102;0mcompact skipped${ESC}[0m ${ESC}[2malready within budget${ESC}[0m`,
      "accent",
      "compact skipped already within budget",
    ],
    ["plain status", "detail", "plain status"],
    [`${ESC}[2mdim only${ESC}[0m`, "detail", "dim only"],
  ];
  for (const [raw, level, text] of cases) {
    const result = parseNotice(raw);
    assert.equal(result.level, level, `level for ${JSON.stringify(raw)}`);
    assert.equal(result.text, text, `text for ${JSON.stringify(raw)}`);
    assert.ok(!/\u001b/.test(result.text), "no escape bytes survive (no compact:->act: clipping)");
  }
});

test("the most severe colored segment wins over a leading dim word", () => {
  const raw = `${ESC}[2mnote:${ESC}[0m ${ESC}[31mflush failed${ESC}[0m`;
  assert.equal(parseNotice(raw).level, "error");
});

test("every notice level has a glyph and a THEME color token", () => {
  for (const [name, spec] of Object.entries(NOTICE_LEVELS)) {
    assert.ok(spec.glyph, `${name} glyph`);
    assert.ok(spec.token, `${name} token`);
  }
});

test("isStructuredLine flags box-drawing borders so they skip the glyph", () => {
  assert.equal(isStructuredLine("│ model    deepseek │"), true);
  assert.equal(isStructuredLine("╭─ router ─╮"), true);
  assert.equal(isStructuredLine("compact skipped"), false);
});

test("truncated, private, and control-byte sequences never survive parsing", () => {
  // notice_capture flushes buffered PARTIAL lines, so a line can end
  // mid-sequence; stderr captures can carry bells/backspaces from libraries.
  const cases = [
    // [raw, expected clean text]
    [`color${ESC}[3`, "color[3"], // truncated CSI: the ESC byte is swept
    [`ding${BEL}done`, "dingdone"], // BEL must not ring inside the alt screen
    [`${ESC}[>4;2mmodifyOtherKeys${ESC}[0m`, "modifyOtherKeys"], // private-parameter CSI
    ["back\bspace", "backspace"],
    [`lone escape ${ESC} byte`, "lone escape  byte"],
  ];
  for (const [raw, text] of cases) {
    const result = parseNotice(raw);
    assert.equal(result.text, text, `text for ${JSON.stringify(raw)}`);
    assert.ok(
      !/[\u0000-\u0008\u000b-\u001f\u007f]/.test(result.text),
      `no control bytes survive for ${JSON.stringify(raw)}`,
    );
  }
});

test("background and underline extended colors never classify the line", () => {
  // 48/58 arguments must be consumed, not re-read as standalone codes: a gray
  // background like 48;2;31;31;31 would otherwise classify as basic red.
  assert.equal(parseNotice(`${ESC}[48;2;31;31;31mstatus line${ESC}[0m`).level, "detail");
  assert.equal(parseNotice(`${ESC}[48;5;31mstatus line${ESC}[0m`).level, "detail");
  assert.equal(parseNotice(`${ESC}[58;2;33;33;33munderlined${ESC}[0m`).level, "detail");
  // A real foreground after a gray background still wins.
  assert.equal(parseNotice(`${ESC}[48;2;31;31;31;32mok on gray${ESC}[0m`).level, "success");
});

test("256-color foregrounds classify through the xterm palette", () => {
  assert.equal(parseNotice(`${ESC}[38;5;196mfailed${ESC}[0m`).level, "error"); // cube red
  assert.equal(parseNotice(`${ESC}[38;5;244mnote${ESC}[0m`).level, "detail"); // grayscale ramp
  assert.equal(parseNotice(`${ESC}[38;5;2mok${ESC}[0m`).level, "success"); // base table
  // Combined params: bold + 256-color red still classifies as error.
  assert.equal(parseNotice(`${ESC}[1;38;5;196mfailed${ESC}[0m`).level, "error");
  // The index skip keeps trailing params aligned: 38;5;31 is a cyan (info) and
  // its 31 must not be re-read as basic red.
  assert.equal(parseNotice(`${ESC}[38;5;31mchannel${ESC}[0m`).level, "info");
});

test("noticeContent builds the rendered line and theme token main.mjs consumes", () => {
  const err = noticeContent(`${ESC}[31mUnknown command. Use /help.${ESC}[0m`);
  assert.equal(err.content, `${TOOL_INDENT}✗ Unknown command. Use /help.`);
  assert.equal(err.token, "error");
  // Table/panel borders render as-is so their box-drawing stays aligned.
  const border = noticeContent("│ model   deepseek-v4 │");
  assert.equal(border.content, `${TOOL_INDENT}│ model   deepseek-v4 │`);
  assert.equal(border.token, "detailText");
  // Blank spacer lines drop entirely.
  assert.equal(noticeContent("   "), null);
  assert.equal(noticeContent(`${ESC}[2m   ${ESC}[0m`), null);
  assert.equal(noticeContent(undefined), null);
});
