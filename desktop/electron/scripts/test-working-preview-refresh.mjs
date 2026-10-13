import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { DesktopBrowserError } from '../dist/desktop-browser.js'

// Exercise the shipped manager's scheduler without a Chromium process or wall-clock sleeps.
const runtime = await readFile(new URL('../dist/native-workbench-surface.js', import.meta.url), 'utf8')
const start = runtime.indexOf('    async watchWorkingPreview(')
const end = runtime.indexOf('    browserRecord(', start)
assert.ok(start > 0 && end > start)
const queueStart = runtime.indexOf('    queueSurfaceOperation(')
const queueEnd = runtime.indexOf('    async configureLegacySession(', queueStart)
assert.ok(queueStart > 0 && queueEnd > queueStart)

function fixture({ immutable = false } = {}) {
  const timers = new Map()
  let now = 0
  let revision = '1'
  let heads = 0
  let reloads = 0
  let fetchOverride
  const setTimer = (run, delay) => {
    const timer = { unref() {} }
    timers.set(timer, { at: now + delay, run })
    return timer
  }
  const manager = new Function('DesktopBrowserError', 'setTimeout', 'clearTimeout',
    `return new (class {${runtime.slice(start, end)}${runtime.slice(queueStart, queueEnd)}})`,
  )(DesktopBrowserError, setTimer, timer => timers.delete(timer))
  const record = {
    id: 'preview', targetRef: 'page-one', kind: 'artifact-preview', documentUrl: 'https://fixture.invalid/working',
    disposed: false, crashed: false, revisionWatching: false, revisionVisible: false,
    revisionTimer: null, revisionRequest: null, revisionRequestBackground: false,
    revisionCheckQueued: false,
    revisionKind: 'unknown', revisionLast: null, revisionEpoch: 0, revisionInteracted: false,
    annotationPickerActive: false, annotationCandidate: null, annotationFallbackActive: false,
    annotationFocusTimer: null, annotationDocumentGeneration: 1, pendingPermissions: new Map(),
    pendingAuthentication: null, browserDocumentReady: true, browserNavigationStopped: false,
    view: { webContents: {
      isDestroyed: () => false, isLoading: () => false,
      reload() { reloads++; record.annotationDocumentGeneration++; record.browserDocumentReady = true },
    } },
    previewSession: { async fetch(_url, options) {
      heads++
      if (fetchOverride) return await fetchOverride(options)
      return response()
    } },
  }
  const response = () => new Response(null, { headers: {
    etag: revision, ...(!immutable ? { 'x-opensquilla-working-preview': '1' } : {}),
  } })
  manager.surfaces = new Map([[record.id, record]])
  manager.surfaceQueues = new Map()
  const flush = async () => { for (let i = 0; i < 30; i++) await Promise.resolve() }
  return {
    manager, record, timers, response, flush,
    get heads() { return heads }, get reloads() { return reloads },
    set revision(value) { revision = value }, set fetch(value) { fetchOverride = value },
    async tick(ms) {
      now += ms
      for (const [timer, item] of [...timers]) {
        if (item.at <= now && timers.delete(timer)) item.run()
      }
      await flush()
    },
  }
}

{
  const f = fixture()
  await f.manager.watchWorkingPreview(f.record)
  assert.equal(f.heads, 1)
  assert.equal(f.timers.size, 0, 'hidden retained previews must have no revision timer')
  f.manager.updateWorkingPreviewVisibility(f.record, true)
  await f.tick(0)
  assert.equal(f.heads, 2)
  f.manager.updateWorkingPreviewVisibility(f.record, false)
  await f.tick(600_000)
  assert.equal(f.heads, 2, 'ten idle minutes must not create a hidden HEAD request')
  assert.equal(f.timers.size, 0)
  f.revision = '2'
  await f.manager.queueSurfaceOperation('operation:page-one', () =>
    f.manager.checkWorkingPreview(f.record, false, () => {}))
  assert.equal(f.reloads, 1, 'an Agent access refreshes the retained hidden document')
  assert.equal(f.record.revisionVisible, false)
  assert.equal(f.timers.size, 0)
}

{
  const f = fixture({ immutable: true })
  await f.manager.watchWorkingPreview(f.record)
  f.manager.updateWorkingPreviewVisibility(f.record, true)
  await f.tick(600_000)
  await f.manager.checkWorkingPreview(f.record, false, () => {})
  assert.equal(f.heads, 1, 'immutable previews classify once, including Agent accesses')
  assert.equal(f.timers.size, 0)
}

for (const immutable of [false, true]) {
  const f = fixture({ immutable })
  // loadURL resolves at did-finish-load, before isLoading necessarily clears.
  f.record.view.webContents.isLoading = () => true
  await f.manager.watchWorkingPreview(f.record)
  assert.equal(f.heads, 1, 'a ready hidden document must receive its initial classification')
  assert.equal(f.record.revisionKind, immutable ? 'immutable' : 'working')
  assert.equal(f.reloads, 0)
  assert.equal(f.timers.size, 0)
  f.revision = '2'
  f.manager.updateWorkingPreviewVisibility(f.record, true)
  await f.tick(1000)
  await f.manager.checkWorkingPreview(f.record, false, () => {})
  assert.equal(f.heads, 1, 'a known preview must still defer checks while loading')
  assert.equal(f.reloads, 0, 'initial classification must not reload a loading document')
  f.manager.updateWorkingPreviewVisibility(f.record, false)
}

{
  const f = fixture()
  f.record.browserDocumentReady = false
  f.record.view.webContents.isLoading = () => true
  await f.manager.watchWorkingPreview(f.record)
  assert.equal(f.heads, 0, 'a document that is not ready must not classify while loading')
  assert.equal(f.record.revisionKind, 'unknown')
  assert.equal(f.reloads, 0)
  assert.equal(f.timers.size, 0)
}

for (const interrupt of ['navigate', 'dispose', 'stop']) {
  const f = fixture({ immutable: true })
  f.record.view.webContents.isLoading = () => true
  let release
  f.fetch = () => new Promise(resolve => { release = resolve })
  const watching = f.manager.watchWorkingPreview(f.record)
  await f.flush()
  if (interrupt === 'navigate') f.record.annotationDocumentGeneration++
  else if (interrupt === 'dispose') f.record.disposed = true
  else f.record.browserNavigationStopped = true
  release(f.response())
  await watching
  assert.equal(f.heads, 1)
  assert.equal(f.record.revisionKind, 'unknown', `${interrupt}: ignore a stale initial classification`)
  assert.equal(f.reloads, 0)
  assert.equal(f.timers.size, 0)
}

{
  const f = fixture()
  let release
  const activeOperation = f.manager.queueSurfaceOperation('operation:page-one', () =>
    new Promise(resolve => { release = resolve }))
  await f.flush()
  const classification = f.manager.watchWorkingPreview(f.record)
  f.record.browserNavigationStopped = true
  release()
  await Promise.all([activeOperation, classification])
  assert.equal(f.heads, 0, 'stopping a page must retire its queued initial probe')
  assert.equal(f.timers.size, 0)
}

for (const protection of ['revisionInteracted', 'annotationPickerActive', 'annotationCandidate',
  'annotationFallbackActive', 'annotationFocusTimer', 'pendingPermissions', 'pendingAuthentication']) {
  const f = fixture()
  await f.manager.watchWorkingPreview(f.record)
  f.revision = '2'
  f.record[protection] = protection === 'pendingPermissions' ? new Map([['request', {}]]) : true
  f.manager.updateWorkingPreviewVisibility(f.record, true)
  await f.tick(0)
  assert.equal(f.heads, 1, `${protection}: background work should defer`)
  await assert.rejects(f.manager.checkWorkingPreview(f.record, false, () => {}),
    error => error.code === 'REFRESH_DEFERRED')
  assert.equal(f.reloads, 0, `${protection}: neither visibility nor Agent reads may discard state`)
  assert.equal(f.record.revisionLast, '1', 'a deferred version is not the loaded version')
  f.manager.updateWorkingPreviewVisibility(f.record, false)
}

for (const interrupt of ['hide', 'dispose', 'replace', 'navigate', 'stop', 'edit', 'annotation']) {
  const f = fixture()
  await f.manager.watchWorkingPreview(f.record)
  let release
  f.fetch = () => new Promise(resolve => { release = resolve }) // Simulate a transport ignoring abort.
  f.manager.updateWorkingPreviewVisibility(f.record, true)
  await f.tick(0)
  assert.ok(f.record.revisionRequest)
  if (interrupt === 'hide') f.manager.updateWorkingPreviewVisibility(f.record, false)
  if (interrupt === 'dispose') { f.record.disposed = true; f.record.revisionWatching = false }
  if (interrupt === 'replace') f.manager.surfaces.set(f.record.id, {})
  if (interrupt === 'navigate') f.record.annotationDocumentGeneration++
  if (interrupt === 'stop') f.record.browserNavigationStopped = true
  if (interrupt === 'edit') f.record.revisionInteracted = true
  if (interrupt === 'annotation') f.record.annotationCandidate = {}
  f.revision = '2'
  release(f.response())
  await f.flush()
  assert.equal(f.reloads, 0, `${interrupt}: late responses cannot reload the page`)
  assert.equal(f.record.revisionRequest, null)
  if (['hide', 'dispose', 'replace'].includes(interrupt)) assert.equal(f.timers.size, 0)
  f.manager.updateWorkingPreviewVisibility(f.record, false)
  assert.equal(f.timers.size, 0)
}

{
  const f = fixture()
  await f.manager.watchWorkingPreview(f.record)
  let release
  const activeOperation = f.manager.queueSurfaceOperation('operation:page-one', () =>
    new Promise(resolve => { release = resolve }))
  await f.flush()
  f.manager.updateWorkingPreviewVisibility(f.record, true)
  await f.tick(0)
  assert.equal(f.heads, 1, 'a refresh must wait for the complete browser operation, not just one CDP command')
  for (let i = 0; i < 30; i++) {
    f.manager.updateWorkingPreviewVisibility(f.record, false)
    f.manager.updateWorkingPreviewVisibility(f.record, true)
    await f.tick(0)
  }
  f.revision = '2'
  release()
  await activeOperation
  await f.flush()
  assert.equal(f.reloads, 1)
  assert.equal(f.heads, 2, 'visibility churn coalesces into one queued check')
  f.manager.updateWorkingPreviewVisibility(f.record, false)
}

{
  const f = fixture()
  await f.manager.watchWorkingPreview(f.record)
  let release
  f.fetch = () => new Promise(resolve => { release = resolve })
  const controller = new AbortController()
  const check = f.manager.checkWorkingPreview(f.record, false, () => {
    if (controller.signal.aborted) throw new DesktopBrowserError('TIMEOUT', 'Cancelled', 504)
  }, controller.signal)
  controller.abort()
  assert.equal(f.record.revisionRequest.signal.aborted, true)
  f.revision = '2'
  release(f.response())
  await assert.rejects(check, error => error.code === 'TIMEOUT')
  assert.equal(f.reloads, 0, 'an expired Agent request cannot cause a late reload')
  assert.equal(f.timers.size, 0)
}

console.log('Working preview visibility, freshness, operation serialization and retained-state regressions passed.')
