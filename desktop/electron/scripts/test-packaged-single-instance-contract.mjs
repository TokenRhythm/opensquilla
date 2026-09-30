// Pure contracts only. Importing the native entry point must not launch an app.
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { mkdtemp, readdir, rmdir } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { activationEvidence, gatewayShutdownEvidence, parseArguments, startupEvidence } from './test-packaged-single-instance.mjs'

const script = fileURLToPath(new URL('./test-packaged-single-instance.mjs', import.meta.url))
const options = ['--executable', process.execPath, '--workdir', join(tmpdir(), 'synthetic-single-instance'),
  '--output', join(tmpdir(), 'synthetic-single-instance.json')]

test('native entry requires a bounded scenario, new root, explicit package and GPU policy', () => {
  assert.throws(() => parseArguments([]), /Missing required/)
  assert.throws(() => parseArguments([...options, '--scenario', 'activation']), /disable-gpu/)
  assert.throws(() => parseArguments([...options, '--scenario', 'arbitrary', '--disable-gpu']), /Unsupported/)
  assert.throws(() => parseArguments([...options, '--scenario', 'activation', '--disable-gpu', '--profile', 'real']), /Unknown/)
  assert.throws(() => parseArguments([...options, '--scenario', 'activation', '--scenario', 'relaunch']), /Duplicate/)
  for (const scenario of ['activation', 'relaunch']) {
    const value = parseArguments([...options, '--scenario', scenario, '--disable-gpu', '--startup-timing'])
    assert.equal(value.scenario, scenario)
    assert.equal(value.startupTiming, true)
    assert.equal(value.disableGpu, true)
  }
})

test('fixed evidence distinguishes accepted handoff, true lock retry, and cold launch', () => {
  const lines = [
    { event: 'desktop_exit_phase', to: 'draining', reason: 'PRIVATE' },
    { event: 'second_instance', args: 'PRIVATE' },
    { event: 'single_instance_activation_accepted', attempt: 1, nonce: 'PRIVATE' },
    { event: 'launch_forwarded_to_existing_instance' },
    { event: 'single_instance_lock_acquired', attempt: 3 },
    { event: 'single_instance_lock_acquired', attempt: 'PRIVATE' },
    { event: 'single_instance_lock_acquired', attempt: -1 },
    { event: 'gateway_spawned', runtimeCwd: 'PRIVATE', pid: 42 },
  ]
  const value = activationEvidence('not json\n' + lines.map(row => JSON.stringify(row)).join('\n'))
  assert.deepEqual(value, { accepted: 1, forwarded: 1, aborted: 0, secondInstanceEvents: 1,
    draining: true, lockAttempts: [3], gatewaySpawns: 1 })
  assert.equal(JSON.stringify(value).includes('PRIVATE'), false)
  const cold = activationEvidence('{"event":"single_instance_lock_acquired","attempt":1}')
  assert.equal(cold.lockAttempts.some(attempt => attempt > 1), false)
  assert.equal(activationEvidence('{"event":"launch_aborted_lock_held"}').aborted, 1)
})

test('startup evidence exposes fixed numeric timings with the disposal boundary', () => {
  const value = startupEvidence([
    { event: 'launch', at: '2026-09-29T00:00:00.000Z', privateData: 'PRIVATE' },
    { event: 'single_instance_lock_acquired', at: '2026-09-29T00:00:00.012Z', attempt: 1, elapsedMs: 18 },
  ].map(item => JSON.stringify(item)).join('\n'))
  assert.equal(value.launchToLockAcquiredMs, 12)
  assert.equal(value.lockElapsedMs, 18)
  assert.equal(value.lockAttempt, 1)
  assert.match(value.boundary, /precedes request disposal/)
  assert.equal(JSON.stringify(value).includes('PRIVATE'), false)
  assert.equal(startupEvidence('{"event":"launch","at":"PRIVATE"}').launchToLockAcquiredMs, null)
})

test('natural Electron exit cannot hide a hard-terminated or unproven Gateway shutdown', () => {
  const text = [
    { event: 'quit_gateway_exit', exited: true, hardTerminated: false },
    { event: 'quit_gateway_exit', exited: true, hardTerminated: true },
    { event: 'quit_gateway_exit', exited: true, error: 'PRIVATE' },
    { event: 'quit_gateway_exit', exited: false, hardTerminated: false },
    { event: 'unrelated', exited: true, hardTerminated: false },
  ].map(item => JSON.stringify(item)).join('\n')
  const value = gatewayShutdownEvidence(text)
  assert.deepEqual(value, { exits: 4, gracefulExits: 1, hardTerminations: 1, unprovenExits: 2 })
  assert.equal(JSON.stringify(value).includes('PRIVATE'), false)
  assert.equal(gatewayShutdownEvidence('').gracefulExits, 0)
})

test('existing evidence root is rejected without launching even a supplied Node executable', async () => {
  const root = await mkdtemp(join(tmpdir(), 'single-instance-contract-'))
  try {
    const result = spawnSync(process.execPath, [script, '--executable', process.execPath, '--workdir', root,
      '--output', join(root, 'report.json'), '--scenario', 'activation', '--disable-gpu'],
    { encoding: 'utf8', timeout: 5_000, windowsHide: true })
    assert.equal(result.status, 1)
    assert.match(result.stderr, /acceptance failed/)
    assert.deepEqual(await readdir(root), [])
  } finally {
    // Empty directory created by this test only; no recursive cleanup.
    await rmdir(root)
  }
})
