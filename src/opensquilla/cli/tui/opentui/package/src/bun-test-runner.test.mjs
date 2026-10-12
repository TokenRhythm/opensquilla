import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { constants } from "node:os";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { runInNewContext } from "node:vm";

const runnerUrl = new URL("../scripts/run-bun-tests.mjs", import.meta.url);
// Execute the runner's actual control flow with its process/filesystem ports
// replaced. No Bun process or terminal renderer is needed for exit propagation.
const runnerSource = readFileSync(runnerUrl, "utf8")
  .replace(/^import .*;\r?\n/gm, "")
  .replaceAll("import.meta.url", "runnerUrl");

function runWith(firstResult, entries = ["b.bun.test.mjs", "a.bun.test.mjs"]) {
  const calls = [];
  const exited = Symbol("runner exited");
  let exitCode = null;
  let error = null;
  try {
    runInNewContext(runnerSource, {
      URL, fileURLToPath, constants, runnerUrl: runnerUrl.href,
      readdirSync: () => entries.map(name => ({ name, isFile: () => true })),
      spawnSync: (...args) => {
        calls.push(args);
        return calls.length === 1 ? firstResult : { status: 0, signal: null };
      },
      console: { log() {} },
      process: {
        platform: "linux", env: { TEST_RUNNER_SENTINEL: "preserved" },
        exit(code) { exitCode = code; throw exited; },
      },
    }, { filename: fileURLToPath(runnerUrl), timeout: 1000 });
  } catch (caught) {
    if (caught !== exited) error = caught;
  }
  return { calls, exitCode, error };
}

test("the Bun runner executes every selected file once and preserves process isolation", () => {
  const result = runWith({ status: 0, signal: null }, [
    "b.bun.test.mjs", "ignored.test.mjs", "a.bun.test.mjs",
  ]);
  assert.equal(result.error, null);
  assert.equal(result.exitCode, null);
  assert.deepEqual(result.calls.map(call => call[1][2]), [
    "src/a.bun.test.mjs", "src/b.bun.test.mjs",
  ]);
  for (const [command, args, options] of result.calls) {
    assert.equal(command, "bun");
    assert.deepEqual(Array.from(args).slice(0, 2), ["test", "--max-concurrency=1"]);
    assert.equal(options.cwd, fileURLToPath(new URL("../", runnerUrl)));
    assert.equal(options.env.TEST_RUNNER_SENTINEL, "preserved");
    assert.equal(options.env.OPENSQUILLA_TUI_COLOR, "truecolor");
    assert.equal(options.stdio, "inherit");
  }
});

for (const [name, childResult, expectedExit] of [
  ["nonzero status", { status: 7, signal: null }, 7],
  ["explicit SIGILL status", { status: 132, signal: null }, 132],
  ["SIGILL termination", { status: null, signal: "SIGILL" }, 132],
  ["SIGTERM termination", { status: null, signal: "SIGTERM" }, 143],
  ["unknown signal", { status: null, signal: "UNKNOWN_SIGNAL" }, 1],
  ["missing status and signal", { status: null, signal: null }, 1],
]) {
  test(`the Bun runner stops and propagates ${name}`, () => {
    const result = runWith(childResult);
    assert.equal(result.error, null);
    assert.equal(result.exitCode, expectedExit);
    assert.equal(result.calls.length, 1, "must not run another file after failure");
  });
}

test("the Bun runner fails closed on spawn errors", () => {
  const error = new Error("synthetic spawn failure");
  const result = runWith({ error, status: null, signal: null });
  assert.equal(result.error, error);
  assert.equal(result.exitCode, null);
  assert.equal(result.calls.length, 1);
});

test("the Bun runner fails closed when no Bun test files are discovered", () => {
  const result = runWith({ status: 0, signal: null }, ["ordinary.test.mjs"]);
  assert.match(result.error?.message ?? "", /No Bun test files found/);
  assert.equal(result.exitCode, null);
  assert.equal(result.calls.length, 0);
});
