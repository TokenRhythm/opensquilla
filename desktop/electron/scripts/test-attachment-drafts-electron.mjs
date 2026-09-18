import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { createServer } from 'node:http'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { _electron as electron } from 'playwright'
import ts from '@typescript/typescript6'

if (process.platform === 'linux' && !process.env.DISPLAY && !process.env.WAYLAND_DISPLAY
  && process.env.OPENSQUILLA_ATTACHMENT_DRAFTS_XVFB !== '1') {
  const result = spawnSync('xvfb-run', ['-a', process.execPath, fileURLToPath(import.meta.url)], {
    env: { ...process.env, OPENSQUILLA_ATTACHMENT_DRAFTS_XVFB: '1' }, stdio: 'inherit',
  })
  if (result.error) throw result.error
  process.exit(result.status ?? 1)
}
const modules = new Map()
for (const [route, file] of Object.entries({
  '/drafts.js': 'utils/chat/attachmentDrafts.ts',
  '/wal.js': 'utils/chat/pendingInputWal.ts',
  '/attachments.js': 'utils/chat/attachments.ts',
  '/page-context.js': 'types/pageContext.ts',
  '/selected-skills.js': 'types/selectedSkills.ts',
  '/message-identity.js': 'utils/chat/messageIdentity.ts',
})) {
  const source = await readFile(new URL(`../../../opensquilla-webui/src/${file}`, import.meta.url), 'utf8')
  const compiled = ts.transpileModule(source, { compilerOptions: {
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022,
  } }).outputText.replaceAll("'@/types/pageContext'", "'/page-context.js'")
    .replaceAll("'@/types/selectedSkills'", "'/selected-skills.js'")
    .replaceAll("'./attachments'", "'/attachments.js'")
    .replaceAll("'./messageIdentity'", "'/message-identity.js'")
  modules.set(route, compiled)
}
const root = await mkdtemp(join(tmpdir(), 'opensquilla-attachment-drafts-electron-'))
const server = createServer((request, response) => {
  response.setHeader('Content-Type', modules.has(request.url) ? 'text/javascript' : 'text/html')
  response.end(modules.get(request.url) || '<title>Attachment draft storage test</title>')
})
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
let app
try {
  app = await electron.launch({
    args: [`--user-data-dir=${join(root, 'chromium')}`, fileURLToPath(new URL('fixtures/native-attachments', import.meta.url))],
    env: { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: 'true' },
  })
  const page = await app.firstWindow()
  await page.goto(`http://127.0.0.1:${server.address().port}`)
  await page.evaluate(async () => {
    const { IndexedDbAttachmentDraftStore } = await import('/drafts.js')
    const store = new IndexedDbAttachmentDraftStore(indexedDB)
    const { createPendingInputWal } = await import('/wal.js')
    const { snapshotAttachment, serializeChatFiles } = await import('/attachments.js')
    const workspaceFile = new Proxy({ workspaceId: 'project-A', relativePath: 'src/code.ts',
      name: 'code.ts', mime: 'text/plain', size: 3 }, {})
    const attachment = new Proxy({ kind: 'workspace', local_id: 1, name: 'code.ts',
      mime: 'text/plain', workspaceFile }, {})
    const wal = createPendingInputWal(indexedDB)
    const selectedSkills = [{ name: 'tables', instanceId: 'skill:tables', digest: 'a'.repeat(64) }]
    await wal.put({ schemaVersion: 1, pendingInputId: 'fixture-pending', sessionKey: 'session-A',
      clientRequestId: 'fixture-request', clientMessageId: 'fixture-message',
      text: 'edit project notes', attachments: [attachment], selectedSkills, intent: null, state: 'local_only',
      createdAt: 1, updatedAt: 1 })
    await wal.putHandoff({ schemaVersion: 1, ownerRequestId: 'fixture-request', requestSessionKey: 'session-A',
      clientRequestId: 'fixture-request', clientMessageId: 'fixture-message',
      params: { message: 'edit project notes', sessionKey: 'session-A', clientRequestId: 'fixture-request',
        clientMessageId: 'fixture-message', selectedSkills, ...serializeChatFiles([attachment]) },
      composerText: 'edit project notes', recoveryAttachments: [snapshotAttachment(attachment)],
      state: 'submitting', createdAt: 1, updatedAt: 1 })
    wal.close()
    await store.save({ identity: 'gateway-user-A', sessionKey: 'session-A' }, [{ kind: 'inline', local_id: 1,
      name: 'notes.txt', mime: 'text/plain', size: 14, file: new File(['draft contents'], 'notes.txt') }])
  })
  await page.reload()
  const result = await page.evaluate(async () => {
    const { IndexedDbAttachmentDraftStore, ATTACHMENT_DRAFT_TTL_MS, ATTACHMENT_DRAFT_MAX_BYTES } = await import('/drafts.js')
    let now = Date.now()
    const store = new IndexedDbAttachmentDraftStore(indexedDB, () => now)
    const scope = { identity: 'gateway-user-A', sessionKey: 'session-A' }
    const initialSnapshot = await store.loadSnapshot(scope)
    const loaded = initialSnapshot.attachments
    const legacyRevisionRestored = Boolean(initialSnapshot.revision)
      && (await store.loadSnapshot(scope)).revision === initialSnapshot.revision
    const { createPendingInputWal } = await import('/wal.js')
    const wal = createPendingInputWal(indexedDB)
    const queued = await wal.list('session-A')
    const handoffs = await wal.listHandoffs()
    const walRestored = queued[0]?.attachments[0]?.workspaceFile?.relativePath === 'src/code.ts'
      && handoffs[0]?.params.workspaceFiles?.[0]?.relativePath === 'src/code.ts'
      && handoffs[0]?.recoveryAttachments[0]?.kind === 'workspace'
      && queued[0]?.selectedSkills[0]?.instanceId === 'skill:tables'
      && handoffs[0]?.params.selectedSkills[0]?.instanceId === 'skill:tables'
    wal.close()
    const blobText = await loaded[0].file.text()
    const isolated = (await store.load({ ...scope, identity: 'gateway-user-B' })).length === 0
    const workspaceFile = { workspaceId: 'project-A', relativePath: 'src/code.ts', name: 'code.ts', mime: 'text/plain', size: 3 }
    await store.save(scope, [{ kind: 'workspace', local_id: 2, name: 'code.ts', mime: 'text/plain', size: 3,
      workspaceFile, nativeSelectionToken: 'must-never-persist' }])
    const workspace = await store.load(scope)
    const savedRaw = await new Promise((resolve, reject) => {
      const opening = indexedDB.open('opensquilla-attachment-drafts', 1)
      opening.onsuccess = () => {
        const db = opening.result
        const request = db.transaction('drafts').objectStore('drafts').getAll()
        request.onsuccess = () => { resolve(JSON.stringify(request.result)); db.close() }
        request.onerror = () => reject(request.error)
      }
      opening.onerror = () => reject(opening.error)
    })
    now += ATTACHMENT_DRAFT_TTL_MS + 1
    const expired = (await store.load(scope)).length === 0
    const staged = [{ kind: 'staged', local_id: 1, name: 'large-a.bin', mime: 'application/octet-stream', size: ATTACHMENT_DRAFT_MAX_BYTES / 2, file_uuid: 'file-a' },
      { kind: 'staged', local_id: 2, name: 'large-b.bin', mime: 'application/octet-stream', size: ATTACHMENT_DRAFT_MAX_BYTES / 2, file_uuid: 'file-b' }]
    await store.save(scope, staged)
    await store.save({ ...scope, sessionKey: 'session-B' }, staged)
    let aggregateRejected = false
    try { await store.save({ ...scope, sessionKey: 'session-C' }, staged) } catch { aggregateRejected = true }
    const existingPreserved = (await store.load(scope)).length === 2
    let perDraftRejected = false
    try { await store.save(scope, [{ ...staged[0], size: ATTACHMENT_DRAFT_MAX_BYTES + 1 }]) } catch { perDraftRejected = true }
    await new Promise((resolve, reject) => {
      const request = indexedDB.open('accepted-queue-sentinel', 1)
      request.onupgradeneeded = () => request.result.createObjectStore('accepted')
      request.onsuccess = () => {
        const db = request.result
        const transaction = db.transaction('accepted', 'readwrite')
        transaction.objectStore('accepted').put('server-owned-material', 'accepted-id')
        transaction.oncomplete = () => { db.close(); resolve() }
        transaction.onerror = () => reject(transaction.error)
      }
    })
    await store.save(scope, [])
    const removed = (await store.load(scope)).length === 0
    const acceptedRetained = await new Promise((resolve, reject) => {
      const opening = indexedDB.open('accepted-queue-sentinel', 1)
      opening.onsuccess = () => {
        const db = opening.result
        const request = db.transaction('accepted').objectStore('accepted').get('accepted-id')
        request.onsuccess = () => { resolve(request.result === 'server-owned-material'); db.close() }
        request.onerror = () => reject(request.error)
      }
    })
    return { blobText, isolated, workspace, walRestored, legacyRevisionRestored, capabilityAbsent: !savedRaw.includes('must-never-persist'),
      expired, aggregateRejected, existingPreserved, perDraftRejected, removed, acceptedRetained }
  })
  assert.equal(result.blobText, 'draft contents')
  assert.equal(result.workspace[0].kind, 'workspace')
  assert.equal(result.workspace[0].workspaceFile.relativePath, 'src/code.ts')
  for (const key of ['walRestored', 'legacyRevisionRestored', 'isolated', 'capabilityAbsent', 'expired', 'aggregateRejected', 'existingPreserved', 'perDraftRejected', 'removed', 'acceptedRetained']) assert.equal(result[key], true, key)
  const nextWindow = app.waitForEvent('window')
  await app.evaluate(async ({ BrowserWindow }, url) => {
    const window = new BrowserWindow({ show: false, webPreferences: { sandbox: true, contextIsolation: true, nodeIntegration: false } })
    await window.loadURL(url)
  }, `http://127.0.0.1:${server.address().port}`)
  const otherPage = await nextWindow
  const scope = { identity: 'gateway-user-A', sessionKey: 'late-acceptance' }
  const files = ['first.txt', 'second.txt'].map((name, index) => ({
    kind: 'workspace', local_id: index + 1, name, mime: 'text/plain', size: 3,
    workspaceFile: { workspaceId: 'project-A', relativePath: name, name, mime: 'text/plain', size: 3 },
  }))
  await page.evaluate(async ({ scope, files }) => {
    const { IndexedDbAttachmentDraftStore } = await import('/drafts.js')
    await new IndexedDbAttachmentDraftStore(indexedDB).save(scope, files, 'accepted-version')
  }, { scope, files })
  // A different renderer writes an identical-looking new draft. Content or
  // local attachment IDs alone must never authorize the old ACK to erase it.
  await otherPage.evaluate(async ({ scope, files }) => {
    const { IndexedDbAttachmentDraftStore } = await import('/drafts.js')
    await new IndexedDbAttachmentDraftStore(indexedDB).save(scope, files, 'new-tab-version')
  }, { scope, files })
  const consumption = await page.evaluate(async scope => {
    const { IndexedDbAttachmentDraftStore } = await import('/drafts.js')
    const store = new IndexedDbAttachmentDraftStore(indexedDB)
    const restored = await store.loadSnapshot(scope)
    const rejectedOldVersion = !await store.consume(scope, 'accepted-version', [0, 1])
    const newerRetained = (await store.load(scope)).length === 2
    const consumedCurrentVersion = await store.consume(scope, restored.revision, [0])
    const rejectedDuplicate = !await store.consume(scope, restored.revision, [0])
    const remainingSnapshot = await store.loadSnapshot(scope)
    const remainder = remainingSnapshot.attachments
    const consumedRemainder = await store.consume(scope, remainingSnapshot.revision, [0])
      && (await store.load(scope)).length === 0
    return { newerRetained, rejectedOldVersion, consumedCurrentVersion, rejectedDuplicate, consumedRemainder,
      revision: restored.revision, remainder: remainder.map(item => item.name) }
  }, scope)
  assert.equal(consumption.revision, 'new-tab-version')
  for (const key of ['newerRetained', 'rejectedOldVersion', 'consumedCurrentVersion', 'rejectedDuplicate', 'consumedRemainder']) assert.equal(consumption[key], true, key)
  assert.deepEqual(consumption.remainder, ['second.txt'])
  console.log('Electron IndexedDB attachment drafts: reload/Blob, isolation, workspace and handoff WAL, expiry, budgets and queue ownership passed')
  console.log('Two-renderer IndexedDB acceptance: newer identical draft survives, partial consumption and duplicate ACK passed')
} finally {
  await app?.close()
  await new Promise(resolve => server.close(resolve))
  await rm(root, { recursive: true, force: true })
}
