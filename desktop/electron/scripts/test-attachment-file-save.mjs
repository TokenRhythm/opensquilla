import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { test } from 'node:test'
import { mkdtemp, readFile, readdir, realpath, rm, writeFile } from 'node:fs/promises'
import * as fs from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { saveAttachmentFile } from '../dist/resource-file-actions.js'

async function fixture(t) {
  const root = await realpath(await mkdtemp(join(tmpdir(), 'opensquilla-attachment-save-')))
  t.after(() => rm(root, { recursive: true, force: true }))
  const destination = join(root, 'saved.txt')
  await writeFile(destination, 'existing destination')
  const bytes = Buffer.from('\ufeffliteral\r\ntext 🦐\r\n'.repeat(20_000))
  const sha256 = createHash('sha256').update(bytes).digest('hex')
  const payload = { gatewayInstanceId: 'owned-one', sessionKey: 'agent:main:webchat:fixture',
    sha256, name: '../saved.txt' }
  let connection = { instanceId: 'owned-one', profile: 'profile-one',
    url: 'http://127.0.0.1:18792', authToken: 'fixture-secret' }
  let fetches = 0
  const deps = {
    connection: () => connection,
    choosePath: async name => { assert.equal(name, 'saved.txt'); return destination },
    fetch: async (url, options) => {
      fetches += 1
      assert.equal(url.href, `http://127.0.0.1:18792/api/v1/attachments/${sha256}`)
      assert.equal(url.search, '')
      assert.equal(options.headers.Authorization, 'Bearer fixture-secret')
      assert.equal(options.headers['x-opensquilla-session-key'], payload.sessionKey)
      assert.equal(options.redirect, 'error')
      const response = new Response(bytes)
      response.arrayBuffer = () => { throw new Error('must not buffer response') }
      response.blob = () => { throw new Error('must not buffer response') }
      return response
    },
  }
  return { root, destination, bytes, payload, deps, fetches: () => fetches,
    switchConnection: value => { connection = value } }
}

test('streams full literal bytes in bounded writes and verifies SHA before atomic replacement', async t => {
  const f = await fixture(t)
  const writes = []
  const io = { ...fs, open: async (path, flags, mode) => {
    assert.equal(flags, 'wx')
    assert.equal(mode, 0o600)
    const file = await fs.open(path, flags, mode)
    const write = file.writeFile.bind(file)
    file.writeFile = async chunk => { writes.push(chunk.byteLength); await write(chunk) }
    return file
  } }
  assert.deepEqual(await saveAttachmentFile(f.payload, f.deps, io), { status: 'saved' })
  assert.deepEqual(await readFile(f.destination), f.bytes)
  assert.ok(writes.length > 1)
  assert.ok(writes.every(size => size <= 64 * 1024))
  assert.deepEqual(await readdir(f.root), ['saved.txt'])
})

test('cancelled native destination never fetches or writes', async t => {
  const f = await fixture(t)
  assert.deepEqual(await saveAttachmentFile(f.payload, { ...f.deps, choosePath: async () => null }),
    { status: 'cancelled' })
  assert.equal(f.fetches(), 0)
  assert.equal(await readFile(f.destination, 'utf8'), 'existing destination')
})

for (const patch of [{ url: 'https://external.invalid' }, { path: 'C:/fake' }, { data: 'fake' },
  { sha256: 'a'.repeat(63) }, { sha256: '../escape' }, { sha256: 'A'.repeat(64) },
  { sessionKey: ' session' }, { sessionKey: 'bad\nheader' }, { gatewayInstanceId: 'stale' }]) {
  test(`rejects invalid attachment save payload ${JSON.stringify(patch)}`, async t => {
    const f = await fixture(t)
    await assert.rejects(saveAttachmentFile({ ...f.payload, ...patch }, f.deps))
    assert.equal(f.fetches(), 0)
    assert.deepEqual(await readdir(f.root), ['saved.txt'])
  })
}

for (const url of ['https://remote.invalid', 'http://127.0.0.1:18792/path',
  'http://user:password@127.0.0.1:18792', 'http://127.0.0.1:18792/?token=secret']) {
  test(`rejects non-owned Gateway URL ${url.replace(/password|secret/g, 'redacted')}`, async t => {
    const f = await fixture(t)
    f.switchConnection({ ...f.deps.connection(), url })
    await assert.rejects(saveAttachmentFile(f.payload, f.deps), /Invalid owned Gateway/)
    assert.equal(f.fetches(), 0)
  })
}

test('connection changes while destination dialog is open prevent fetch', async t => {
  const f = await fixture(t)
  await assert.rejects(saveAttachmentFile(f.payload, { ...f.deps,
    choosePath: async () => { f.switchConnection(null); return f.destination },
  }), /Gateway changed/)
  assert.equal(f.fetches(), 0)
})

for (const failure of ['integrity', 'read', 'write', 'abort', 'connection']) {
  test(`${failure} failure preserves destination and removes partial temporary file`, async t => {
    const f = await fixture(t)
    const controller = new AbortController()
    const deps = { ...f.deps, signal: controller.signal }
    if (failure === 'integrity') deps.fetch = async () => new Response('corrupted bytes')
    if (failure === 'read') deps.fetch = async () => {
      let emitted = false
      return new Response(new ReadableStream({ pull(stream) {
        if (emitted) stream.error(new Error('read failure'))
        else { emitted = true; stream.enqueue(f.bytes.subarray(0, 1024)) }
      } }))
    }
    const io = { ...fs, open: async (...args) => {
      const file = await fs.open(...args)
      const write = file.writeFile.bind(file)
      file.writeFile = async chunk => {
        await write(chunk)
        if (failure === 'write') throw new Error('disk failure')
        if (failure === 'abort') controller.abort()
        if (failure === 'connection') f.switchConnection(null)
      }
      return file
    } }
    await assert.rejects(saveAttachmentFile(f.payload, deps, io))
    assert.equal(await readFile(f.destination, 'utf8'), 'existing destination')
    assert.deepEqual(await readdir(f.root), ['saved.txt'])
  })
}

test('download HTTP failure writes no file and reports no secret-bearing URL', async t => {
  const f = await fixture(t)
  await assert.rejects(saveAttachmentFile(f.payload, { ...f.deps,
    fetch: async () => new Response('unauthorized', { status: 401 }),
  }), /Attachment unavailable \(401\)/)
  assert.deepEqual(await readdir(f.root), ['saved.txt'])
})

test('failed final rename preserves destination and cleans only its temporary file', async t => {
  const f = await fixture(t)
  await assert.rejects(saveAttachmentFile(f.payload, f.deps, { ...fs,
    rename: async () => { throw new Error('replace failed') },
  }), /replace failed/)
  assert.equal(await readFile(f.destination, 'utf8'), 'existing destination')
  assert.deepEqual(await readdir(f.root), ['saved.txt'])
})

test('failed exclusive create never deletes a temporary file it did not create', async t => {
  const f = await fixture(t)
  let removals = 0
  await assert.rejects(saveAttachmentFile(f.payload, f.deps, { ...fs,
    open: async () => { throw new Error('exclusive create failed') },
    unlink: async () => { removals += 1 },
  }), /exclusive create failed/)
  assert.equal(removals, 0)
  assert.equal(await readFile(f.destination, 'utf8'), 'existing destination')
})

test('attachment IPC requires trusted main window and current owned ready Gateway', async () => {
  const main = await readFile(new URL('../src/main.ts', import.meta.url), 'utf8')
  const handler = main.slice(main.indexOf("ipcMain.handle('desktop:attachment:save'"),
    main.indexOf("ipcMain.handle('desktop:source-file:action'"))
  assert.match(handler, /trustedMainWindowControlIpc\(event\)/)
  assert.match(handler, /!gatewayState\.owned \|\| gatewayState\.status !== 'ready'/)
  assert.match(handler, /dialog\.showSaveDialog/)
  assert.match(handler, /snapshot\.profileFingerprint/)
  assert.match(handler, /once\('destroyed', abort\)/)
  assert.match(handler, /removeListener\('destroyed', abort\)/)
  const preload = await readFile(new URL('../src/preload.cts', import.meta.url), 'utf8')
  assert.match(preload, /saveAttachment:.*ipcRenderer\.invoke\('desktop:attachment:save'/)
})
