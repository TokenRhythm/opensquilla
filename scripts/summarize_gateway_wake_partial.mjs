/** Preserve interrupted real-clock runs without treating missing trials as passes. */
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { execFileSync } from 'node:child_process';

if (!process.argv[2]) throw new Error('Usage: node scripts/summarize_gateway_wake_partial.mjs EVIDENCE_DIR [EMPTY_OUTPUT_DIR]');
const directory = path.resolve(process.argv[2]);
const outputDir = process.argv[3] ? path.resolve(process.argv[3])
  : fs.mkdtempSync(path.join(os.tmpdir(), 'opensquilla-gateway-summary-'));
if (fs.existsSync(outputDir) && (!fs.statSync(outputDir).isDirectory() || fs.readdirSync(outputDir).length)) {
  throw new Error(`Summary output directory must be empty: ${outputDir}`);
}
fs.mkdirSync(outputDir, { recursive: true });
const baselineSha = '6750223bf72b418a257a2276c18e6edec07ef14f';
const scenarios = ['single-wake-blackhole', 'repeat3-blackhole', 'repeat14-blackhole', 'repeat40-blackhole', 'buffer13-recovery'];
const variants = ['baseline675', 'candidate10', 'candidate15', 'candidate20', 'candidate30'];
const sha256 = (content) => crypto.createHash('sha256').update(content).digest('hex');
const reference = fs.readFileSync(path.join(directory, 'source-candidate20.ts'), 'utf8');
const sources = variants.map((name) => {
  const file = `source-${name}.ts`;
  const content = fs.readFileSync(path.join(directory, file), 'utf8');
  const expected = name === 'baseline675'
    ? execFileSync('git', ['show', `${baselineSha}:opensquilla-webui/src/lib/rpc.ts`], { encoding: 'utf8' })
    : reference.replace(/const WAKE_INCIDENT_BUDGET_MS = [\d_]+;/,
      `const WAKE_INCIDENT_BUDGET_MS = ${Number(name.slice('candidate'.length)) * 1000};`);
  assert.equal(content, expected, `${name}: source differs beyond budget constant`);
  return { name, file, sha256: sha256(content) };
});
const files = fs.readdirSync(directory).filter((name) => /^(baseline675|candidate\d+)-.+-\d+\.json$/.test(name));
const results = files.map((file) => ({ file, result: JSON.parse(fs.readFileSync(path.join(directory, file), 'utf8')) }));
const ids = new Set();
const fields = {
  faultToSuspectMs: (events) => events.find((e) => e.event === 'transport' && e.phase === 'probe_timeout'),
  faultToCloseMs: (events) => events.find((e) => e.event === 'socket_close_called'),
  faultToReconnectMs: (events) => events.find((e) => e.event === 'transport' && e.phase === 'connect_start'),
  faultToReplacementHelloMs: (events) => events.find((e) => e.event === 'hello' && e.count >= 2),
  faultToUsableMs: (events) => events.find((e) => e.event === 'usable_echo'),
};
for (const { file, result } of results) {
  const expectedFile = `${result.variant}-${result.scenario}-${result.repetition}.json`;
  assert.equal(file, expectedFile);
  assert(!ids.has(file), `duplicate trial ${file}`);
  ids.add(file);
  assert(result.repetition >= 1 && result.repetition <= 30);
  const faultAt = result.events.find((e) => e.event === 'fault_injected').at;
  const after = result.events.filter((e) => e.at >= faultAt);
  for (const [field, find] of Object.entries(fields)) {
    const event = find(after);
    assert.equal(result[field], event ? +(event.at - faultAt).toFixed(1) : null, `${file}/${field}`);
  }
}
function stats(values) {
  values = values.filter((value) => typeof value === 'number').sort((a, b) => a - b);
  return values.length ? { count: values.length, p50: values[Math.floor(values.length * 0.5)],
    p95: values[Math.ceil(values.length * 0.95) - 1], min: values[0], max: values.at(-1),
    range: [values[0], values.at(-1)] } : null;
}
const cells = {};
for (const variant of variants) for (const scenario of scenarios) {
  if (!['baseline675', 'candidate20'].includes(variant) && scenario.startsWith('repeat')) continue;
  const matches = results.filter(({ result }) => result.variant === variant && result.scenario === scenario);
  const values = matches.map(({ result }) => result);
  cells[`${variant}/${scenario}`] = {
    status: values.length === 30 ? 'complete_cell' : values.length ? 'partial_cell_excluded' : 'not_completed',
    planned: 30, completed: values.length,
    recovered: values.filter((value) => value.recovered).length,
    preserved: values.filter((value) => value.recovered && value.helloCount === 1).length,
    censored: values.filter((value) => !value.recovered).length,
    timing: Object.fromEntries(Object.keys(fields).map((field) => [field, stats(values.map((value) => value[field]))])),
    trials: matches.map(({ file }) => file),
  };
}
const manifest = {
  schema: 'opensquilla.gateway.wake-real-clock.interrupted.v1', baselineSha,
  generatedAt: new Date().toISOString(),
  runStatus: 'partial_files_recovered', exitCodeObserved: null,
  interruptionCause: 'Exit code and interruption cause cannot be inferred from trial files; missing trials are not passes.',
  plannedTrials: 480, rawTrials: results.length,
  trialsInCompleteCells: Object.values(cells).filter((cell) => cell.status === 'complete_cell').reduce((n, cell) => n + cell.completed, 0),
  sources, cells,
  verification: { sourceMatchesFrozenBaseline: true, variantsDifferOnlyInBudget: true, eventTimingsRecomputed: true },
  scope: 'Actual headless Chromium, native WebSocket and real clocks over loopback TCP relay with synthetic upstream; not physical remote, native sleep or production Gateway.',
};
const output = path.join(outputDir, 'interrupted-run-summary.json');
fs.writeFileSync(output, JSON.stringify(manifest, null, 2), { flag: 'wx' });
console.log(JSON.stringify({ output, rawTrials: manifest.rawTrials, trialsInCompleteCells: manifest.trialsInCompleteCells,
  cells: Object.fromEntries(Object.entries(cells).map(([key, cell]) => [key,
    { status: cell.status, completed: cell.completed, recovered: cell.recovered, preserved: cell.preserved, censored: cell.censored,
      usable: cell.timing.faultToUsableMs, close: cell.timing.faultToCloseMs }])) }, null, 2));
