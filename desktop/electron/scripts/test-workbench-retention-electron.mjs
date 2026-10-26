import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'
import { _electron as electron } from 'playwright'

// Included by the existing desktop-browser Electron suite in CI. Everything is
// local and uses an isolated profile; no user browser or provider is contacted.
const profile = await mkdtemp(join(tmpdir(), 'opensquilla-retention-'))
const server = createServer((_request, response) => {
  response.setHeader('content-type', 'text/html; charset=utf-8')
  response.end('<!doctype html><title>Retention fixture</title><input id="draft"><script>window.retainedCounter=7</script>')
})
await new Promise(resolveReady => server.listen(0, '127.0.0.1', resolveReady))
const origin = `http://127.0.0.1:${server.address().port}`
let instance
try {
  instance = await electron.launch({
    args: [`--user-data-dir=${join(profile, 'chromium')}`, fileURLToPath(new URL('./fixtures/native-workbench-smoke', import.meta.url))],
    env: { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: 'true', NO_PROXY: '127.0.0.1,localhost', no_proxy: '127.0.0.1,localhost' },
  })
  const result = await instance.evaluate(async ({ BrowserWindow, webContents }, origin) => {
    const owner = new BrowserWindow({ show: false, width: 900, height: 700,
      webPreferences: { sandbox: true, contextIsolation: true, nodeIntegration: false } })
    await owner.loadURL('data:text/html,<title>Isolated retention owner</title>')
    const events = []
    const manager = new globalThis.__opensquillaNativeWorkbenchSurfaceManager({ getWindow: () => owner, emit: event => events.push(event) })
    const create = id => manager.createSurface({ version: 4, surfaceId: id, kind: 'url-preview',
      payload: { scopeId: id, url: origin } })
    try {
      const openings = Array.from({ length: 9 }, (_, index) => create(`retained-${index}`))
      const results = await Promise.all(openings)
      const first = manager.surfaces.get('retained-0')
      await first.contents.executeJavaScript('document.querySelector("#draft").value="Unsubmitted user work";window.retainedCounter=42')
      const identity = first.contents.id
      for (let round = 0; round < 6; round++) {
        manager.setSurfaceRect({ surfaceId: 'retained-0', x: 0, y: 0, width: 640, height: 480, visible: true })
        manager.setSurfaceRect({ surfaceId: 'retained-0', x: 0, y: 0, width: 640, height: 480, visible: false })
        manager.setSurfaceRect({ surfaceId: 'retained-1', x: 0, y: 0, width: 640, height: 480, visible: true })
      }
      const state = await first.contents.executeJavaScript('({draft:document.querySelector("#draft").value,counter:window.retainedCounter})')
      // A popup cannot bypass native admission; denial must not destroy its opener.
      const popupDenied = await first.contents.executeJavaScript(`window.open(${JSON.stringify(origin + '/popup')}) === null`)
      const beforeCleanup = manager.resourceBudget.snapshot()
      const cleanup = manager.cleanupDisposedRecord.bind(manager)
      let release
      const gate = new Promise(resolve => { release = resolve })
      manager.cleanupDisposedRecord = async record => { await gate; await cleanup(record) }
      const closing = manager.destroySurface('retained-7', true)
      const whileDisposing = await create('while-disposing')
      release()
      await closing
      manager.cleanupDisposedRecord = cleanup
      const afterClosing = await create('after-close')
      // Replacement must reserve the old slot before awaiting unload/cleanup;
      // another surface cannot take it after the old page has been destroyed.
      let releaseReplacement
      let enteredReplacement
      const replacementEntered = new Promise(resolve => { enteredReplacement = resolve })
      const replacementGate = new Promise(resolve => { releaseReplacement = resolve })
      manager.cleanupDisposedRecord = async record => { enteredReplacement(); await replacementGate; await cleanup(record) }
      const replacing = create('retained-6')
      await replacementEntered
      const duringReplacement = await create('steal-replacement-slot')
      releaseReplacement()
      const replacementResult = await replacing
      manager.cleanupDisposedRecord = cleanup
      const existingIds = [...manager.surfaces.values()].map(record => record.contents.id)
      const residentContents = webContents.getAllWebContents().filter(contents => existingIds.includes(contents.id)).length
      await manager.setBrowserAutomationState({ sessionKey: 'retained-0', taskId: 'run-1', active: true })
      await manager.setBrowserAutomationState({ sessionKey: 'retained-0', taskId: 'older-run', active: false })
      const staleStopIgnored = manager.browserAutomationStates.get('retained-0')?.taskId === 'run-1'
      await manager.setBrowserAutomationState({ sessionKey: 'retained-0', taskId: 'run-1', active: false })
      const inactiveOwners = manager.browserAutomationStates.size
      const identityPreserved = identity === first.contents.id
      // Interactive previews have no count cap. Every page belongs to a
      // different chat and retains its form/JS state while the visible slot
      // moves among them, even when all browser admission slots are occupied.
      const previewIds = Array.from({ length: 6 }, (_, index) => `preview-${index}`)
      const previewResults = await Promise.all(previewIds.map(id => manager.createSurface({
        version: 4, surfaceId: id, kind: 'artifact-preview',
        payload: { launchUrl: origin, expectedOrigin: origin, scopeId: `chat-${id}`, mode: 'full' },
      })))
      if (previewResults.some(result => !result.ok)) throw new Error(JSON.stringify(previewResults))
      const previewContents = previewIds.map(id => manager.surfaces.get(id).contents)
      const previewIdentities = previewContents.map(contents => contents.id)
      for (let index = 0; index < previewIds.length; index++) {
        await previewContents[index].executeJavaScript(
          `document.querySelector("#draft").value=${JSON.stringify(`Draft ${index}`)};window.retainedCounter=${index + 100}`)
      }
      for (let round = 0; round < 2; round++) {
        for (const id of previewIds) {
          manager.setSurfaceRect({ surfaceId: id, x: 0, y: 0, width: 640, height: 480, visible: true })
          manager.setSurfaceRect({ surfaceId: id, x: 0, y: 0, width: 640, height: 480, visible: false })
        }
      }
      const previewStates = await Promise.all(previewContents.map(contents =>
        contents.executeJavaScript('({draft:document.querySelector("#draft").value,counter:window.retainedCounter})')))
      const previewIdentitiesPreserved = previewIds.every((id, index) =>
        manager.surfaces.get(id).contents.id === previewIdentities[index])
      const previewReservations = manager.resourceBudget.snapshot().filter(entry => entry.kind === 'preview').length
      await Promise.all(previewIds.map(id => manager.destroySurface(id, true)))
      const previewsDestroyed = previewContents.every(contents => contents.isDestroyed())
      const previewReservationsAfterClose = manager.resourceBudget.snapshot().filter(entry => entry.kind === 'preview').length
      // A failed native close must keep its slot until Chromium actually exits;
      // replacing the same id cannot briefly create a second renderer in it.
      const previewRequest = { version: 4, surfaceId: 'slow-preview', kind: 'artifact-preview',
        payload: { launchUrl: origin, expectedOrigin: origin, scopeId: 'preview-owner', mode: 'full' } }
      const previewCreated = await manager.createSurface(previewRequest)
      if (!previewCreated.ok) throw new Error(previewCreated.message)
      const slowPreview = manager.surfaces.get('slow-preview')
      const closePreview = slowPreview.contents.close.bind(slowPreview.contents)
      slowPreview.contents.close = () => { throw new Error('Fixture: native close deferred') }
      const delayedReplacement = await manager.createSurface(previewRequest)
      const pendingPreviewSlots = manager.resourceBudget.snapshot().filter(entry => entry.kind === 'preview').length
      const oldPreviewStillAlive = !slowPreview.contents.isDestroyed()
      slowPreview.contents.close = closePreview
      await new Promise(resolve => { slowPreview.contents.once('destroyed', resolve); closePreview({ waitForBeforeUnload: false }) })
      const previewRetry = await manager.createSurface(previewRequest)
      await manager.destroyAll()
      return { results, state, identityPreserved, popupDenied,
        previewResults, previewStates, previewIdentitiesPreserved, previewReservations,
        previewsDestroyed, previewReservationsAfterClose,
        delayedReplacement, pendingPreviewSlots, oldPreviewStillAlive, previewRetry,
        beforeCleanup, whileDisposing, afterClosing, duringReplacement, replacementResult, residentContents, staleStopIgnored,
        inactiveOwners, finalBudget: manager.resourceBudget.snapshot(),
        popupLimited: events.some(event => event.type === 'blocked-action' && event.detail?.reason === 'resource-limit') }
    } finally {
      await manager.destroyAll()
      owner.destroy()
    }
  }, origin)
  assert.equal(result.results.filter(entry => entry.ok).length, 8)
  assert.equal(result.results.filter(entry => entry.code === 'RESOURCE_LIMIT').length, 1)
  assert.deepEqual(result.state, { draft: 'Unsubmitted user work', counter: 42 })
  assert.equal(result.identityPreserved, true)
  assert.equal(result.popupDenied, true)
  assert.equal(result.beforeCleanup.length, 8)
  assert.equal(result.whileDisposing.code, 'RESOURCE_LIMIT')
  assert.equal(result.afterClosing.ok, true)
  assert.equal(result.duringReplacement.code, 'RESOURCE_LIMIT')
  assert.equal(result.replacementResult.ok, true)
  assert.equal(result.residentContents, 8)
  assert.equal(result.staleStopIgnored, true)
  assert.equal(result.inactiveOwners, 0)
  assert.equal(result.previewResults.filter(entry => entry.ok).length, 6)
  assert.deepEqual(result.previewStates, Array.from({ length: 6 }, (_, index) => ({ draft: `Draft ${index}`, counter: index + 100 })))
  assert.equal(result.previewIdentitiesPreserved, true)
  assert.equal(result.previewReservations, 6)
  assert.equal(result.previewsDestroyed, true)
  assert.equal(result.previewReservationsAfterClose, 0)
  assert.equal(result.delayedReplacement.code, 'RESOURCE_RELEASING')
  assert.equal(result.pendingPreviewSlots, 1)
  assert.equal(result.oldPreviewStillAlive, true)
  assert.equal(result.previewRetry.ok, true)
  assert.deepEqual(result.finalBudget, [])
  console.log('Native retention: browser admission remains 8; 6 previews across chats preserve form/JS/target after hiding and showing; explicit close releases every preview and all reservations.')
} finally {
  await instance?.close()
  server.closeAllConnections()
  await new Promise(resolveClosed => server.close(resolveClosed))
  // Validate this unique test-owned profile before recursive cleanup.
  if (!resolve(profile).startsWith(resolve(tmpdir()) + sep) || !profile.includes('opensquilla-retention-')) throw new Error('Unexpected fixture cleanup path')
  await rm(profile, { recursive: true, force: true })
}
