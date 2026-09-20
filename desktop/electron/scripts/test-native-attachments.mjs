import assert from 'node:assert/strict'
import { createHash, createHmac } from 'node:crypto'
import { mkdtemp, writeFile, rm, mkdir, symlink, chmod, realpath } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import { NativeAttachmentSelections, parseNativeAttachmentContext } from '../dist/native-attachments.js'

const context = { gatewayInstanceId: 'instance-test', sessionKey: 'session-test', sessionId: 'session-id-test', sessionEpoch: 0 }
const connection = { instanceId: context.gatewayInstanceId, profile: 'test-profile', url: 'http://127.0.0.1:54321', authToken: 'test-auth', nonce: 'test-nonce-private' }
async function fixture(run, options = {}) {
  const root = await realpath(await mkdtemp(join(tmpdir(), 'opensquilla-native-attachments-')))
  const path = join(root, 'selected.txt')
  await writeFile(path, 'selected content')
  let current = { ...connection }
  let now = Date.now()
  const requests = []
  const broker = new NativeAttachmentSelections({
    connection: () => current,
    now: () => now,
    fetch: async (url, init) => {
      requests.push({ url: String(url), init })
      return options.fetch ? options.fetch(url, init) : Response.json({ file_uuid: 'file-test' })
    },
  })
  try { await run({ root, path, broker, requests, switchGateway: () => { current = { ...connection, nonce: 'another-instance' } }, advance: ms => { now += ms } }) }
  finally { await rm(root, { recursive: true, force: true }) }
}

test('native picker and selected File enter the same single-use, private signed import', async () => fixture(async ({ broker, path, requests }) => {
  const [picked] = await broker.choose(12, context, async () => [path])
  assert.deepEqual(Object.keys(picked).sort(), ['mime', 'name', 'size', 'token'])
  const receipt = await broker.import(12, context, picked.token)
  assert.equal(receipt.file_uuid, 'file-test')
  const { init, url } = requests[0]
  assert.match(url, /\/api\/v1\/files\/native-import$/)
  const encoded = JSON.parse(init.body).selection
  const selected = JSON.parse(Buffer.from(encoded, 'base64url').toString())
  assert.equal(selected.path, path)
  assert.equal(selected.senderId, 12)
  assert.equal(selected.sessionKey, context.sessionKey)
  assert.equal(selected.sessionId, context.sessionId)
  assert.equal(selected.sessionEpoch, context.sessionEpoch)
  assert.equal(selected.instanceId, context.gatewayInstanceId)
  assert.match(selected.birthtimeNs, /^-?\d+$/)
  assert.match(selected.ctimeNs, /^-?\d+$/)
  assert.equal(selected.sha256, createHash('sha256').update('selected content').digest('hex'))
  assert.equal(init.headers['x-opensquilla-native-signature'], createHmac('sha256', connection.nonce).update('opensquilla-native-attachment-v1\n' + encoded).digest('hex'))
  await assert.rejects(broker.import(12, context, picked.token), /not selected/)
  const dropped = await broker.select(12, context, path)
  assert.deepEqual({ ...dropped, token: picked.token }, picked)
}))

test('paths, extra request fields and tokens from another window/session never import', async () => fixture(async ({ broker, path, requests }) => {
  assert.throws(() => parseNativeAttachmentContext({ ...context, path }), /Invalid/)
  await assert.rejects(broker.import(12, context, path), /not selected/)
  const chosen = await broker.select(12, context, path)
  await assert.rejects(broker.import(13, context, chosen.token), /not selected/)
  await assert.rejects(broker.import(12, { ...context, sessionKey: 'another-session' }, chosen.token), /not selected/)
  await assert.rejects(broker.import(12, { ...context, sessionEpoch: 1 }, chosen.token), /not selected/)
  await assert.rejects(broker.import(12, { ...context, sessionId: 'rotated-session' }, chosen.token), /not selected/)
  assert.equal(requests.length, 0)
  await broker.import(12, context, chosen.token)
}))

test('expired, cancelled and switched-instance selections fail closed', async () => fixture(async ({ broker, path, requests, advance, switchGateway }) => {
  const expired = await broker.select(12, context, path)
  advance(120_001)
  await assert.rejects(broker.import(12, context, expired.token), /expired/)
  const cancelled = await broker.select(12, context, path)
  broker.cancel(12)
  await assert.rejects(broker.import(12, context, cancelled.token), /not selected/)
  const switched = await broker.select(12, context, path)
  switchGateway()
  await assert.rejects(broker.import(12, context, switched.token), /expired/)
  assert.equal(requests.length, 0)
}))

test('session cancellation while native picker is open cannot mint a new capability', async () => fixture(async ({ broker, path }) => {
  let finish
  const choosing = broker.choose(12, context, () => new Promise(resolve => { finish = resolve }))
  broker.cancel(12)
  finish([path])
  await assert.rejects(choosing, /expired/)
}))

test('regular-file and actual-size checks reject directory, empty, oversized and changed files', async () => fixture(async ({ broker, path, root, requests }) => {
  await mkdir(join(root, 'directory'))
  await assert.rejects(broker.select(12, context, join(root, 'directory')), /regular file/)
  await writeFile(join(root, 'empty.txt'), '')
  await assert.rejects(broker.select(12, context, join(root, 'empty.txt')), /empty/)
  await writeFile(join(root, 'oversized.png'), Buffer.alloc(5 * 1024 * 1024 + 1))
  await assert.rejects(broker.select(12, context, join(root, 'oversized.png')), /size limit/)
  const chosen = await broker.select(12, context, path)
  await writeFile(path, 'modified content')
  await assert.rejects(broker.import(12, context, chosen.token), /changed/)
  assert.equal(requests.length, 0)
}))

test('symlink substitution is rejected, without opening the replacement target', async t => fixture(async ({ broker, path, root, requests }) => {
  const target = join(root, 'other.txt')
  await writeFile(target, 'not selected')
  const chosen = await broker.select(12, context, path)
  await rm(path)
  try { await symlink(target, path) } catch (error) {
    if (process.platform === 'win32' && error.code === 'EPERM') return t.skip('Windows account cannot create symlinks')
    throw error
  }
  await assert.rejects(broker.import(12, context, chosen.token), /regular file/)
  await assert.rejects(broker.select(12, context, path), /regular file/)
  assert.equal(requests.length, 0)
}))

test('OS file permission denial does not upload or downgrade to another read route', { skip: process.platform === 'win32' || process.getuid?.() === 0 }, async () => fixture(async ({ broker, path, requests }) => {
  const chosen = await broker.select(12, context, path)
  await chmod(path, 0)
  await assert.rejects(broker.import(12, context, chosen.token))
  assert.equal(requests.length, 0)
  await chmod(path, 0o600)
}))

for (const status of [401, 403, 404, 409, 413, 500]) {
  test(`HTTP ${status} never triggers native byte fallback`, async () => fixture(async ({ broker, path, requests }) => {
    const selected = await broker.select(12, context, path)
    await assert.rejects(broker.import(12, context, selected.token), /denied/)
    assert.equal(requests.length, 1)
  }, { fetch: async () => Response.json({ error: 'denied' }, { status }) }))
}

test('only explicit unsupported capability streams original bytes to existing upload endpoint', async () => {
  let calls = 0
  await fixture(async ({ broker, path, requests }) => {
    const selected = await broker.select(12, context, path)
    const result = await broker.import(12, context, selected.token)
    assert.equal(result.file_uuid, 'fallback-file')
    assert.equal(requests.length, 2)
    assert.match(requests[1].url, /\/api\/v1\/files\/upload$/)
    assert.equal(await requests[1].init.body.get('file').text(), 'selected content')
    assert.equal(requests[1].init.body.get('mime'), 'text/plain')
  }, { fetch: async () => ++calls === 1
    ? Response.json({ code: 'native_import_unsupported' }, { status: 501 })
    : Response.json({ file_uuid: 'fallback-file' }) })
})

test('binding is rechecked after an awaited network response', async () => {
  let reply
  await fixture(async ({ broker, path, requests, switchGateway }) => {
    const selected = await broker.select(12, context, path)
    const importing = broker.import(12, context, selected.token)
    while (!requests.length) await new Promise(resolve => setImmediate(resolve))
    switchGateway()
    reply(Response.json({ file_uuid: 'wrong-instance' }))
    await assert.rejects(importing, /expired/)
  }, { fetch: () => new Promise(resolve => { reply = resolve }) })
})

test('import rejects a receipt with mismatched actual integrity', async () => fixture(async ({ broker, path }) => {
  const selected = await broker.select(12, context, path)
  await assert.rejects(broker.import(12, context, selected.token), /integrity mismatch/)
}, { fetch: async () => Response.json({ file_uuid: 'bad-file', sha256: '0'.repeat(64) }) }))
