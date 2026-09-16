import assert from 'node:assert/strict'
import { test } from 'node:test'
import { mkdtemp, mkdir, readFile, readdir, realpath, rm, symlink, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import * as fs from 'node:fs/promises'
import { saveArtifactFile, performSourceFileAction } from '../dist/resource-file-actions.js'

async function fixture(t) {
  const root = await realpath(await mkdtemp(join(tmpdir(), 'opensquilla-file-actions-')))
  t.after(() => rm(root, { recursive: true, force: true }))
  const page = join(root, 'editorial.html')
  await writeFile(page, '<h1>current page</h1>')
  return { root, page }
}
const payload = { data: Uint8Array.from([0, 255, 1, 13, 10]).buffer, name: '../page.html', mime: 'text/html' }

test('save uses chosen destination, preserves bytes, atomically overwrites and leaves no temporary file', async t => {
  const { root, page } = await fixture(t)
  assert.deepEqual(await saveArtifactFile(payload, async name => {
    assert.equal(name, 'page.html')
    return page
  }), { status: 'saved' })
  assert.deepEqual(await readFile(page), Buffer.from(payload.data))
  assert.deepEqual(await readdir(root), ['editorial.html'])
})
test('cancel never writes; failed replacement preserves target and cleans temporary file', async t => {
  const { root, page } = await fixture(t)
  const original = await readFile(page)
  assert.deepEqual(await saveArtifactFile(payload, async () => null), { status: 'cancelled' })
  assert.deepEqual(await readFile(page), original)
  const directory = join(root, 'existing')
  await mkdir(directory)
  await writeFile(join(directory, 'original.html'), original)
  await assert.rejects(saveArtifactFile(payload, async () => directory))
  assert.deepEqual(await readFile(join(directory, 'original.html')), original)
  assert.equal((await readdir(root)).some(name => name.endsWith('.part')), false)
  await assert.rejects(saveArtifactFile({ ...payload, data: 'fake' }, async () => page))
  assert.deepEqual(await readFile(page), original)
})

test('partial write failure preserves existing target bytes and removes only the temporary file', async t => {
  const { root, page } = await fixture(t)
  const original = await readFile(page)
  const io = { ...fs, open: async (...args) => {
    const file = await fs.open(...args)
    file.writeFile = async () => { await file.write(Buffer.from('partial')); throw new Error('disk write failed') }
    return file
  } }
  await assert.rejects(saveArtifactFile(payload, async () => page, io), /disk write failed/)
  assert.deepEqual(await readFile(page), original)
  assert.deepEqual(await readdir(root), ['editorial.html'])
})

const request = { gatewayInstanceId: 'owned-one', sessionKey: 'agent:main:webchat:fixture',
  documentId: 'doc_fixture', pagePath: 'editorial.html', action: 'open' }
async function sourceFixture(t) {
  const { root, page } = await fixture(t)
  let connection = { instanceId: 'owned-one', profile: 'profile-one', url: 'http://127.0.0.1:18792', authToken: 'fixture' }
  const metadata = { documentId: request.documentId, pagePath: request.pagePath,
    sourcePath: page, workspace: root, name: 'editorial.html', mime: 'text/html', size: 21 }
  const opened = [], revealed = []
  const deps = {
    connection: () => connection,
    fetch: async (url, options) => {
      assert.equal(url.pathname, '/api/v1/artifact-documents/doc_fixture/working-file')
      assert.equal(url.searchParams.get('pagePath'), 'editorial.html')
      assert.equal(options.headers['x-opensquilla-session-key'], request.sessionKey)
      assert.equal(options.headers.Authorization, 'Bearer fixture')
      assert.equal(options.redirect, 'error')
      return Response.json(metadata)
    },
    openPath: async path => { opened.push(path); return '' }, reveal: path => revealed.push(path),
  }
  return { root, page, metadata, deps, opened, revealed, switchConnection: next => { connection = next } }
}
test('native actions open and reveal real source, not downloaded copies', async t => {
  const f = await sourceFixture(t)
  await performSourceFileAction(request, f.deps)
  await performSourceFileAction({ ...request, action: 'reveal' }, f.deps)
  assert.deepEqual(f.opened, [f.page])
  assert.deepEqual(f.revealed, [f.page])
})
for (const patch of [{ path: '/fake.html' }, { url: 'file:///fake.html' }, { action: 'exec' },
  { pagePath: '../other.html' }, { pagePath: 'style.css' }, { gatewayInstanceId: 'stale' }]) {
  test(`rejects untrusted request ${JSON.stringify(patch)}`, async t => {
    const f = await sourceFixture(t)
    await assert.rejects(performSourceFileAction({ ...request, ...patch }, f.deps))
    assert.deepEqual(f.opened, [])
  })
}
for (const patch of [{ pagePath: 'index.html' }, { documentId: 'doc_other' }, { mime: 'application/pdf' },
  { sourcePath: '/missing.html' }]) {
  test(`rejects incorrect metadata ${JSON.stringify(patch)}`, async t => {
    const f = await sourceFixture(t)
    Object.assign(f.metadata, patch)
    await assert.rejects(performSourceFileAction(request, f.deps))
    assert.deepEqual(f.opened, [])
  })
}
test('rejects symlinks, outside workspace, remote and changed connections', async t => {
  const f = await sourceFixture(t)
  const link = join(f.root, 'link.html')
  await symlink(f.page, link)
  f.metadata.sourcePath = link
  await assert.rejects(performSourceFileAction(request, f.deps), /identity changed/)
  f.metadata.sourcePath = f.page
  f.metadata.workspace = await realpath(await mkdtemp(join(tmpdir(), 'opensquilla-other-root-')))
  t.after(() => rm(f.metadata.workspace, { recursive: true, force: true }))
  await assert.rejects(performSourceFileAction(request, f.deps), /identity changed/)
  f.switchConnection(null)
  await assert.rejects(performSourceFileAction(request, f.deps), /unavailable/)
  f.switchConnection({ instanceId: 'owned-one', profile: 'profile-one', url: 'https://remote.example', authToken: 'fixture' })
  await assert.rejects(performSourceFileAction(request, f.deps), /Invalid owned/)
  assert.deepEqual(f.opened, [])
})
test('connection switch during metadata read prevents delayed native action', async t => {
  const f = await sourceFixture(t)
  f.deps.fetch = async () => { f.switchConnection(null); return Response.json(f.metadata) }
  await assert.rejects(performSourceFileAction(request, f.deps), /Gateway changed/)
  assert.deepEqual(f.opened, [])
})

test('IPC retains separate trusted-window and owned-local authority boundaries', async () => {
  const main = await readFile(new URL('../src/main.ts', import.meta.url), 'utf8')
  const save = main.slice(main.indexOf("ipcMain.handle('desktop:artifact:save'"), main.indexOf("ipcMain.handle('desktop:source-file:action'"))
  const source = main.slice(main.indexOf("ipcMain.handle('desktop:source-file:action'"), main.indexOf("ipcMain.handle('desktop:workspace:choose-directory'"))
  assert.match(save, /trustedMainWindowControlIpc\(event\)/)
  assert.doesNotMatch(save, /trustedControlUiIpc\(event\)/)
  assert.match(save, /dialog\.showSaveDialog/)
  assert.match(source, /trustedControlUiIpc\(event\)/)
  assert.match(source, /gatewayState\.status !== 'ready'/)
  assert.match(source, /snapshot\.profileFingerprint/)
  assert.match(source, /snapshot\.instanceId/)
})
