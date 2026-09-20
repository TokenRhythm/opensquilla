/** Independently check raw real-clock evidence and recompute its statistics. */
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

if (!process.argv[2]) throw new Error('Usage: node scripts/verify_gateway_wake_real_clock.mjs EVIDENCE_DIR [EMPTY_OUTPUT_DIR]');
const evidenceDir = path.resolve(process.argv[2]);
const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const outputDir = process.argv[3] ? path.resolve(process.argv[3])
  : fs.mkdtempSync(path.join(os.tmpdir(), 'opensquilla-gateway-verify-'));
if (fs.existsSync(outputDir) && (!fs.statSync(outputDir).isDirectory() || fs.readdirSync(outputDir).length)) {
  throw new Error(`Verification output directory must be empty: ${outputDir}`);
}
fs.mkdirSync(outputDir, { recursive: true });
const report = JSON.parse(fs.readFileSync(path.join(evidenceDir, 'results.json'), 'utf8'));
const hash = (file) => crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
assert.equal(report.schema, 'opensquilla.gateway.wake-real-clock.v1');
const baselineRef = process.env.OSQ_WAKE_BASELINE_SHA?.trim() || report.baselineSha;
const resolvedBaselineSha = execFileSync('git', [
  'rev-parse', '--verify', '--end-of-options', `${baselineRef}^{commit}`,
], { cwd: repo, encoding: 'utf8' }).trim();
assert.match(resolvedBaselineSha, /^[0-9a-f]{40}$/);
assert.equal(report.baselineSha, resolvedBaselineSha);
assert.equal(report.failures.length, 0, 'fixture failures must remain visible');
assert(report.harness.loops >= 30, 'formal acceptance requires at least30 trials per cell');
const ids = new Set();
for (const variant of report.variants) assert.equal(hash(path.join(evidenceDir, variant.source)), variant.sha256);
for (const result of report.results) {
  const id = `${result.variant}-${result.scenario}-${result.repetition}`;
  assert(!ids.has(id), `duplicate trial ${id}`);
  ids.add(id);
  const raw = JSON.parse(fs.readFileSync(path.join(evidenceDir, `${id}.json`), 'utf8'));
  // Teardown can append client_closed after the per-trial file is written.
  // Compare the common prefix and allow only those cleanup records afterward.
  const sharedLength = Math.min(raw.relay.length, result.relay.length);
  assert.deepEqual(raw.relay.slice(0, sharedLength), result.relay.slice(0, sharedLength));
  for (const event of [...raw.relay.slice(sharedLength), ...result.relay.slice(sharedLength)]) {
    assert.equal(event.event, 'client_closed', `${id}: unexpected appended relay event`);
  }
  const rawComparable = { ...raw, relay: result.relay };
  assert.deepEqual(rawComparable, result);
  const faultAt = result.events.find((event) => event.event === 'fault_injected').at;
  const after = result.events.filter((event) => event.at >= faultAt);
  const expected = {
    faultToSuspectMs: after.find((event) => event.event === 'transport' && event.phase === 'probe_timeout'),
    faultToCloseMs: after.find((event) => event.event === 'socket_close_called'),
    faultToReconnectMs: after.find((event) => event.event === 'transport' && event.phase === 'connect_start'),
    faultToReplacementHelloMs: after.find((event) => event.event === 'hello' && event.count >= 2),
    faultToUsableMs: after.find((event) => event.event === 'usable_echo'),
  };
  for (const [field, event] of Object.entries(expected)) {
    assert.equal(result[field], event ? +(event.at - faultAt).toFixed(1) : null, `${id}/${field}`);
  }
  if (result.variant === 'candidate20' && result.scenario.endsWith('blackhole')) {
    assert.equal(result.recovered, true, `${id}: replacement did not recover`);
    assert.equal(result.retirementReason, 'wake_incident_timeout');
    assert.equal(result.incidentDeadlineMs, 20000);
    assert(result.faultToCloseMs >= 19950 && result.faultToCloseMs < 23000,
      `${id}: deadline retirement did not happen near20s`);
  }
  if (result.scenario === 'buffer13-recovery' && result.variant !== 'candidate10') {
    assert.equal(result.outcome, 'original_connection_usable', `${id}:13s controlled buffer was cut`);
  }
}
const recomputed = {};
for (const [key, group] of Object.entries(report.summary)) {
  const [variant, scenario] = key.split('/');
  const results = report.results.filter((result) => result.variant === variant && result.scenario === scenario);
  assert.equal(results.length, report.harness.loops, `${key}: incomplete trial count`);
  assert.equal(group.count, results.length);
  assert.equal(group.recovered, results.filter((result) => result.recovered).length);
  assert.equal(group.originalConnectionPreserved, results.filter((result) => result.recovered && result.helloCount === 1).length);
  assert.equal(group.censoredAtObservationLimit, results.filter((result) => !result.recovered).length);
  for (const [field, summary] of Object.entries(group.timing)) {
    const values = results.map((result) => result[field]).filter((value) => typeof value === 'number').sort((a, b) => a - b);
    const actual = values.length ? {
      count: values.length, p50: values[Math.floor(values.length * 0.5)],
      p95: values[Math.min(values.length - 1, Math.ceil(values.length * 0.95) - 1)],
      max: values.at(-1), min: values[0], range: [values[0], values.at(-1)],
    } : null;
    assert.deepEqual(summary, actual, `${key}/${field}: incorrect statistics`);
  }
  recomputed[key] = { trials: results.length, recovered: group.recovered,
    preserved: group.originalConnectionPreserved, censored: group.censoredAtObservationLimit,
    usableP50Ms: group.timing.faultToUsableMs?.p50 ?? null,
    usableP95Ms: group.timing.faultToUsableMs?.p95 ?? null,
    usableMaxMs: group.timing.faultToUsableMs?.max ?? null,
    closeP50Ms: group.timing.faultToCloseMs?.p50 ?? null };
}
const verification = { verified: true, trialCount: ids.size, summary: recomputed };
const output = path.join(outputDir, 'verification.json');
fs.writeFileSync(output, JSON.stringify(verification, null, 2), { flag: 'wx' });
console.log(JSON.stringify({ output, ...verification }, null, 2));
