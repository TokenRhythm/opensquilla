// Pure harness checks: importing the scenario module cannot launch the product.
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { mkdtemp, readdir, rmdir } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { test } from 'node:test'
import { inside, isolatedEnvironment, observeFrame, parseArguments, safeFailureReason, syntheticConfig } from './test-packaged-gateway-reliability.mjs'

const script = fileURLToPath(new URL('./test-packaged-gateway-reliability.mjs', import.meta.url))
const options = ['--executable', process.execPath, '--workdir', join(tmpdir(), 'synthetic-reliability'), '--output', join(tmpdir(), 'synthetic-report.json')]

test('reject missing/unknown/duplicate arguments before any launch; GPU flag is explicit', () => {
  assert.throws(() => parseArguments([]), /Missing --executable/)
  assert.throws(() => parseArguments([...options, '--scenario', 'unsupported']), /Supported scenarios/)
  assert.throws(() => parseArguments([...options, '--scenario', 'restart', '--scenario', 'restart']), /Duplicate/)
  assert.equal(parseArguments([...options, '--scenario', 'configuration']).disableGpu, false)
  assert.equal(parseArguments([...options, '--scenario', 'late-ready', '--disable-gpu']).disableGpu, true)
})

test('drop sensitive and override names before reading their values; isolate every writable environment root', () => {
  const source = { PATH: 'synthetic-path' }
  for (const name of ['OPENAI_API_KEY', 'OPENSQUILLA_STATE_DIR', 'UV_CACHE_DIR', 'NODE_OPTIONS', 'HTTP_PROXY']) {
    Object.defineProperty(source, name, { enumerable: true, get() { throw new Error('must not read') } })
  }
  const root = join(tmpdir(), 'synthetic-env')
  const env = isolatedEnvironment(source, root)
  assert.equal(env.PATH, 'synthetic-path')
  assert.equal(env.OPENAI_API_KEY, undefined)
  assert.equal(env.OPENSQUILLA_STATE_DIR, undefined)
  for (const key of ['HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'TEMP', 'TMP', 'OPENSQUILLA_USER_STATE_DIR']) assert.ok(inside(root, env[key]))
  assert.equal(inside(root, `${root}-sibling`), false)
  assert.equal(inside(root, dirname(root)), false)
})

test('synthetic config accepts only credential-free explicit loopback endpoints', () => {
  const profile = join(tmpdir(), 'synthetic', 'profile')
  const config = syntheticConfig(profile, 'http://127.0.0.1:12345', 'http://127.0.0.1:12346/sse')
  assert.ok(config.includes('connect_timeout_seconds = 135'))
  assert.ok(config.includes(JSON.stringify(join(profile, 'workspace'))))
  assert.throws(() => syntheticConfig(profile, 'https://example.com:443'))
  assert.throws(() => syntheticConfig(profile, 'http://secret@127.0.0.1:12345'))
  assert.throws(() => syntheticConfig(profile, 'http://127.0.0.1:12345?token=secret'))
})

test('passive frame evidence never serializes credentials, bodies, URL, unknown policy, or raw epoch', () => {
  const secret = 'SYNTHETIC_SECRET_MUST_NOT_ESCAPE'
  const state = join(tmpdir(), 'synthetic-state')
  const summary = { methods: {}, requestedCaps: [] }
  observeFrame(summary, 'sent', JSON.stringify({ type: 'req', method: 'connect', params: {
    token: secret, caps: ['transport.flow.v1', 'transport.recovery.v1', secret] } }), state)
  observeFrame(summary, 'received', JSON.stringify({ type: 'res', payload: { type: 'hello-ok', protocol: 3,
    auth: secret, snapshot: { state_dir: state, secret }, features: { methods: ['sessions.messages.resume', 'sessions.messages.snapshot.release'] },
    policy: { transport_flow: { delivery_epoch: secret, window_frames: 128, window_bytes: 4194304 }, secret } } }), state)
  observeFrame(summary, 'sent', JSON.stringify({ type: 'req', method: 'chat.send', params: { message: secret, sessionKey: secret } }), state)
  assert.equal(summary.hello.flow, true)
  assert.equal(summary.hello.recoveryMethods, true)
  assert.equal(summary.hello.stateDirMatches, true)
  assert.equal(summary.methods['chat.send'], 1)
  assert.equal(JSON.stringify(summary).includes(secret), false)
  assert.equal(JSON.stringify(summary).includes(state), false)
})

test('configuration success requires a response to the actual observed configure request', () => {
  const summary = { methods: {}, requestedCaps: [] }
  observeFrame(summary, 'received', '{"type":"res","id":"other","ok":true}', tmpdir())
  assert.equal(summary.configurationSucceeded, undefined)
  observeFrame(summary, 'sent', '{"type":"req","id":"save","method":"onboarding.provider.configure","params":{"secret":"not persisted"}}', tmpdir())
  observeFrame(summary, 'received', '{"type":"res","id":"save","ok":false}', tmpdir())
  observeFrame(summary, 'received', '{"type":"res","id":"save","ok":true}', tmpdir())
  assert.equal(summary.configurationFailed, 1)
  assert.equal(summary.configurationSucceeded, undefined)
  assert.equal(JSON.stringify(summary).includes('not persisted'), false)
})

test('failure reasons accept only fixed script text, never arbitrary Playwright error strings', () => {
  assert.equal(safeFailureReason(new Error('Timed out: persisted sidebar row')), 'Timed out: persisted sidebar row')
  assert.equal(safeFailureReason(new Error('Playwright failed at https://example.com?token=SECRET')), 'details-redacted')
  assert.equal(safeFailureReason(new Error('Timed out: persisted sidebar row SECRET')), 'details-redacted')
  assert.equal(safeFailureReason(new Error('No repeated chat.send\nsecret actual/expected values')), 'No repeated chat.send')
})

test('history proof ignores earlier, unrelated, failed and incomplete reads', () => {
  const summary = { methods: {}, requestedCaps: [] }
  const probe = { key: 'synthetic-target', armed: false }
  const request = (id, key) => observeFrame(summary, 'sent', JSON.stringify({ type: 'req', id,
    method: 'sessions.messages.snapshot.read', params: { key } }), tmpdir(), probe)
  const response = (id, ok, segment_index = 0, segment_count = 1) => observeFrame(summary, 'received', JSON.stringify({ type: 'res', id, ok,
    payload: { segment_index, segment_count } }), tmpdir(), probe)
  request('earlier', probe.key)
  probe.armed = true
  response('earlier', true)
  request('other-session', 'unrelated'); response('other-session', true)
  request('failed', probe.key); response('failed', false)
  request('first-segment', probe.key); response('first-segment', true, 0, 2)
  assert.equal(summary.targetReadCompleted, undefined)
  request('last-segment', probe.key); response('last-segment', true, 1, 2)
  assert.equal(summary.targetReadCompleted, 1)
  assert.equal(JSON.stringify(summary).includes(probe.key), false)
})

test('no-argument CLI fails without creating evidence or importing a packaged runtime', () => {
  const result = spawnSync(process.execPath, [script], { encoding: 'utf8', timeout: 5_000 })
  assert.equal(result.status, 1)
  assert.match(result.stderr, /Missing --executable/)
})

test('existing workdir is rejected before any packaged launch or file overwrite', { skip: process.platform !== 'win32' }, async () => {
  const root = await mkdtemp(join(tmpdir(), 'osq-reliability-contract-'))
  try {
    const result = spawnSync(process.execPath, [script, '--executable', process.execPath,
      '--workdir', root, '--output', join(root, 'report.json'), '--scenario', 'restart'], { encoding: 'utf8', timeout: 5_000 })
    assert.equal(result.status, 1)
    assert.deepEqual(await readdir(root), [])
  } finally {
    // This test created the unique, empty root; never remove a supplied path.
    await rmdir(root)
  }
})
