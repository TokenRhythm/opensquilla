import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { mkdtemp, rm } from 'node:fs/promises'
import { createServer } from 'node:http'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { _electron as electron } from 'playwright'

const scriptPath = fileURLToPath(import.meta.url)
if (process.platform === 'linux' && !process.env.DISPLAY && !process.env.WAYLAND_DISPLAY
  && process.env.OPENSQUILLA_FULLSCREEN_UNDER_XVFB !== '1') {
  const result = spawnSync('xvfb-run', ['-a', process.execPath, scriptPath], {
    env: { ...process.env, OPENSQUILLA_FULLSCREEN_UNDER_XVFB: '1' }, stdio: 'inherit',
  })
  if (result.error) throw new Error(`A display or xvfb-run is required: ${result.error.message}`)
  process.exit(result.status ?? 1)
}

// This suite uses real Chromium fullscreen events and isolated, synthetic pages.
const profile = await mkdtemp(join(tmpdir(), 'opensquilla-fullscreen-'))
const server = createServer((_request, response) => {
  response.setHeader('content-type', 'text/html; charset=utf-8')
  response.end(`<!doctype html><title>Fullscreen sidebar fixture</title>
    <style>body{margin:0;background:#fff}main{height:6000px}input{position:fixed;top:10px}</style>
    <input id="draft"><main>Isolated scrolling page</main><script>window.retainedCounter=7</script>`)
})
await new Promise(resolveReady => server.listen(0, '127.0.0.1', resolveReady))
const origin = `http://127.0.0.1:${server.address().port}`
let instance
try {
  instance = await electron.launch({
    args: [`--user-data-dir=${join(profile, 'chromium')}`, fileURLToPath(new URL('./fixtures/native-workbench-smoke', import.meta.url))],
    env: { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: 'true', NO_PROXY: '127.0.0.1,localhost', no_proxy: '127.0.0.1,localhost' },
  })
  const result = await instance.evaluate(async ({ app, BrowserWindow }, origin) => {
    // Exercise Chromium's actual fullscreen top layer without depending on
    // macOS Spaces activation between Electron suites. The browser-foundations
    // suite covers native window and child webpage fullscreen transitions.
    const owner = new BrowserWindow({ show: true, width: 960, height: 700,
      webPreferences: { sandbox: true, contextIsolation: true, nodeIntegration: false,
        disableHtmlFullscreenWindowResize: true } })
    const htmlEvents = { entered: 0, left: 0 }
    owner.webContents.on('enter-html-full-screen', () => { htmlEvents.entered += 1 })
    owner.webContents.on('leave-html-full-screen', () => { htmlEvents.left += 1 })
    await owner.loadURL('data:text/html,' + encodeURIComponent(`<!doctype html><title>Video fullscreen owner</title>
      <style>body{margin:0;background:#111}video{width:480px;height:270px}</style><video id="video" controls muted></video>`))
    await owner.webContents.executeJavaScript(`(() => {
      const canvas=document.createElement('canvas');canvas.width=320;canvas.height=180;
      const context=canvas.getContext('2d');context.fillStyle='#246';context.fillRect(0,0,320,180);
      const video=document.querySelector('#video');video.srcObject=canvas.captureStream(10);
      window.fixtureFrameTimer=setInterval(()=>context.fillRect(0,0,320,180),100);void video.play();
    })()`)
    const manager = new globalThis.__opensquillaNativeWorkbenchSurfaceManager({ getWindow: () => owner, emit: () => {} })
    manager.observeWindow(owner)
    const surfaceId = 'fullscreen-sidebar'
    const rect = { surfaceId, x: 500, y: 80, width: 400, height: 450, visible: true }
    const require = (condition, message) => { if (!condition) throw new Error(message) }
    async function waitFor(check, label, timeoutMs = 30_000) {
      const deadline = Date.now() + timeoutMs
      while (Date.now() < deadline) {
        if (await check()) return
        await new Promise(resolveWait => setTimeout(resolveWait, 25))
      }
      throw new Error(`Timed out waiting for ${label}.`)
    }
    async function enterVideo() {
      const entered = htmlEvents.entered + 1
      const wasNativeFullscreen = owner.isFullScreen()
      app.focus({ steal: true })
      owner.focus()
      owner.webContents.focus()
      await owner.webContents.executeJavaScript('void document.querySelector("#video").requestFullscreen()', true)
      await waitFor(() => htmlEvents.entered === entered, 'Electron HTML fullscreen entry')
      await waitFor(() => owner.isVisible(), 'visible fullscreen owner')
      await waitFor(() => owner.webContents.executeJavaScript('document.fullscreenElement?.id === "video"'), 'the actual video fullscreen element')
      return wasNativeFullscreen
    }
    async function exitVideo(wasNativeFullscreen, escape = false) {
      const left = htmlEvents.left + 1
      if (escape) {
        owner.webContents.focus()
        owner.webContents.sendInputEvent({ type: 'keyDown', keyCode: 'Escape' })
        owner.webContents.sendInputEvent({ type: 'keyUp', keyCode: 'Escape' })
      } else {
        await owner.webContents.executeJavaScript('void document.exitFullscreen()')
      }
      await waitFor(() => htmlEvents.left === left, 'Electron HTML fullscreen exit')
      await waitFor(async () => !await owner.webContents.executeJavaScript('Boolean(document.fullscreenElement)'), 'video fullscreen exit')
      require(owner.isFullScreen() === wasNativeFullscreen, 'HTML fullscreen exit must preserve the native window state')
      await waitFor(() => owner.isVisible(), 'visible owner after fullscreen')
    }
    async function state(record) {
      return { id: record.contents.id, url: record.contents.getURL(), ...await record.contents.executeJavaScript(`({
        draft:document.querySelector('#draft').value,counter:window.retainedCounter,scrollY,viewportHeight:innerHeight
      })`) }
    }
    async function waitForViewport(record) {
      await waitFor(async () => {
        const height = await record.contents.executeJavaScript('innerHeight')
        return Math.abs(height * record.contents.getZoomFactor() - record.view.getBounds().height) <= 1
      }, 'renderer viewport matching native bounds')
    }
    try {
      // No sidebar has existed yet, so the manager must also observe a host
      // document that was already fullscreen before its first surface.
      const firstEntry = await enterVideo()
      const firstId = 'fullscreen-first-tab'
      const firstCreated = await manager.createSurface({ version: 4, surfaceId: firstId, kind: 'url-preview',
        payload: { scopeId: 'fullscreen-fixture', url: origin + '/first' } })
      require(firstCreated.ok, firstCreated.message || 'first sidebar creation failed')
      const first = manager.surfaces.get(firstId)
      manager.setSurfaceRect({ ...rect, surfaceId: firstId })
      require(!first.view.getVisible(), 'a first sidebar created after video fullscreen entry must stay hidden')
      await exitVideo(firstEntry)
      await waitFor(() => first.view.getVisible(), 'first sidebar after fullscreen exit')
      await manager.destroySurface(firstId, true)

      const created = await manager.createSurface({ version: 4, surfaceId, kind: 'url-preview',
        payload: { scopeId: 'fullscreen-fixture', url: origin + '/article' } })
      require(created.ok, created.message || 'sidebar creation failed')
      const record = manager.surfaces.get(surfaceId)
      manager.setSurfaceRect(rect)
      await waitFor(() => record.view.getVisible(), 'visible sidebar before fullscreen')
      await waitForViewport(record)
      await record.contents.executeJavaScript(`document.querySelector('#draft').value='Unsubmitted fixture draft';window.retainedCounter=42;scrollTo(0,1800)`)
      const before = await state(record)
      require(before.scrollY === 1800, 'fixture must start scrolled into the article')
      const wasNativeFullscreen = await enterVideo()
      require(!record.view.getVisible(), 'side view must be hidden while the owner video is fullscreen')
      const frozenBounds = record.view.getBounds()
      manager.setSurfaceRect({ ...rect, height: 900 })
      manager.activateSurface(surfaceId)
      manager.refreshBounds(owner)
      // Exercise the resize listener too, without asking macOS to resize a Space.
      owner.emit('resize')
      require(!record.view.getVisible(), 'bounds, activation and resize must not reveal the sidebar during video fullscreen')
      for (const event of ['show', 'restore']) {
        owner.emit(event)
        require(!record.view.getVisible(), `${event} must not reveal the sidebar during video fullscreen`)
      }
      owner.webContents.setZoomFactor(1.2)
      owner.webContents.emit('zoom-changed', {}, 'in')
      require(!record.view.getVisible(), 'owner zoom must not reveal the sidebar during video fullscreen')
      owner.webContents.setZoomFactor(1)
      owner.webContents.emit('zoom-changed', {}, 'out')
      require(JSON.stringify(record.view.getBounds()) === JSON.stringify(frozenBounds), 'hidden sidebar bounds must remain stable during fullscreen')
      manager.setSurfaceRect(rect)
      await exitVideo(wasNativeFullscreen)
      await waitFor(() => record.view.getVisible(), 'restored sidebar')
      await waitForViewport(record)
      const after = await state(record)

      await record.contents.executeJavaScript('scrollTo(0,document.documentElement.scrollHeight)')
      const bottomBefore = await state(record)
      require(bottomBefore.scrollY > before.scrollY, 'fixture must reach the article bottom')
      const bottomEntry = await enterVideo()
      manager.setSurfaceRect({ ...rect, height: 900 })
      require(!record.view.getVisible(), 'sidebar must stay hidden while its requested viewport grows')
      const bottomDuring = await state(record)
      require(bottomDuring.scrollY === bottomBefore.scrollY, 'fullscreen layout must not clamp the retained scroll position')
      await exitVideo(bottomEntry, true)
      await waitFor(() => record.view.getVisible(), 'sidebar after Escape')
      await waitForViewport(record)
      await record.contents.executeJavaScript('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
      manager.setSurfaceRect(rect)
      await waitFor(() => record.view.getVisible(), 'sidebar after Escape')
      await waitForViewport(record)
      const bottomAfter = await state(record)
      require(bottomAfter.scrollY === bottomBefore.scrollY, `exit layout must preserve bottom scroll: ${bottomBefore.scrollY} -> ${bottomAfter.scrollY}`)

      const hiddenEntry = await enterVideo()
      manager.setSurfaceRect({ ...rect, visible: false })
      await exitVideo(hiddenEntry)
      require(!record.view.getVisible(), 'a sidebar explicitly hidden during fullscreen must remain hidden on exit')
      manager.setSurfaceRect(rect)
      const secondId = 'fullscreen-second-tab'
      const secondCreated = await manager.createSurface({ version: 4, surfaceId: secondId, kind: 'url-preview',
        payload: { scopeId: 'fullscreen-fixture', url: origin + '/second' } })
      require(secondCreated.ok, secondCreated.message || 'second sidebar creation failed')
      const second = manager.surfaces.get(secondId)
      const switchedEntry = await enterVideo()
      manager.setSurfaceRect({ ...rect, surfaceId: secondId })
      require(!record.view.getVisible() && !second.view.getVisible(), 'switching tabs during video fullscreen must keep both hidden')
      await exitVideo(switchedEntry)
      require(!record.view.getVisible() && second.view.getVisible(), 'exit must restore the current tab instead of resurrecting the former tab')

      const closedEntry = await enterVideo()
      await manager.destroySurface(secondId, true)
      await exitVideo(closedEntry)
      require(!manager.surfaces.has(secondId) && second.contents.isDestroyed(), 'a sidebar closed during video fullscreen must stay destroyed')
      require(!record.view.getVisible(), 'closing the active sidebar must not resurrect a previously hidden tab')
      const replacementId = 'fullscreen-replacement-tab'
      const replacementCreated = await manager.createSurface({ version: 4, surfaceId: replacementId, kind: 'url-preview',
        payload: { scopeId: 'fullscreen-fixture', url: origin + '/replacement' } })
      require(replacementCreated.ok, replacementCreated.message || 'replacement sidebar creation failed')
      const replacement = manager.surfaces.get(replacementId)
      manager.setSurfaceRect({ ...rect, surfaceId: replacementId })
      require(replacement.view.getVisible(), 'opening a sidebar after a fullscreen close must work normally')

      await enterVideo()
      const newId = 'fullscreen-created-tab'
      const newCreated = await manager.createSurface({ version: 4, surfaceId: newId, kind: 'url-preview',
        payload: { scopeId: 'fullscreen-fixture', url: origin + '/created-during-fullscreen' } })
      require(newCreated.ok, newCreated.message || 'sidebar creation during fullscreen failed')
      const newRecord = manager.surfaces.get(newId)
      manager.setSurfaceRect({ ...rect, surfaceId: newId })
      manager.activateSurface(newId)
      require(!newRecord.view.getVisible() && !replacement.view.getVisible(), 'creating and activating a sidebar during video fullscreen must keep it hidden')
      await newRecord.contents.executeJavaScript(`document.querySelector('#draft').value='Retained through owner navigation';window.retainedCounter=91`)
      const newIdentity = newRecord.contents.id
      const navigationExit = htmlEvents.left + 1
      await owner.loadURL('data:text/html,<title>Owner navigated after fullscreen</title>')
      await waitFor(() => htmlEvents.left === navigationExit, 'navigation leaving HTML fullscreen')
      require(!await owner.webContents.executeJavaScript('Boolean(document.fullscreenElement)'), 'navigating the owner must leave HTML fullscreen')
      manager.setSurfaceRect({ ...rect, surfaceId: newId })
      manager.activateSurface(newId)
      await waitFor(() => newRecord.view.getVisible(), 'sidebar after owner document navigation')
      require(newRecord.contents.id === newIdentity && newRecord.contents.getURL() === origin + '/created-during-fullscreen', 'owner navigation must retain the sidebar page')
      const navigatedState = await state(newRecord)
      require(navigatedState.draft === 'Retained through owner navigation' && navigatedState.counter === 91, 'owner navigation must preserve sidebar form and JavaScript state')
      require(!record.view.getVisible() && !replacement.view.getVisible(), 'owner navigation must only restore the current sidebar')

      // Exercise the registered crash contract without crashing Chromium,
      // which can stall a hidden renderer under Linux + Xvfb.
      owner.webContents.emit('render-process-gone', {}, { reason: 'crashed', exitCode: 1 })
      await manager.destroyAll()
      require(newRecord.contents.isDestroyed(), 'owner crash must dispose its previous sidebar')
      await owner.loadURL('data:text/html,<title>Recovered owner document</title>')
      const recoveredId = 'fullscreen-recovered-tab'
      const recoveredCreated = await manager.createSurface({ version: 4, surfaceId: recoveredId, kind: 'url-preview',
        payload: { scopeId: 'fullscreen-fixture', url: origin + '/recovered' } })
      require(recoveredCreated.ok, recoveredCreated.message || 'sidebar creation after owner recovery failed')
      const recovered = manager.surfaces.get(recoveredId)
      manager.setSurfaceRect({ ...rect, surfaceId: recoveredId })
      require(recovered.view.getVisible(), 'a recovered owner must show a new sidebar without retaining its unresponsive state')
      require(manager.surfaces.size === 1 && recovered.contents.id !== newIdentity, 'owner recovery must keep only the newly created sidebar')
      return { before, after, bottomBefore, bottomAfter, ownerHtmlEntries: htmlEvents.entered }
    } finally {
      try {
        await manager.destroyAll()
        await owner.webContents.executeJavaScript('void document.exitFullscreen().catch(() => {})')
      } finally {
        owner.destroy()
      }
    }
  }, origin)
  assert.deepEqual(result.after, result.before, 'fullscreen exit must preserve WebContents, URL, form, JavaScript state and scroll')
  assert.deepEqual(result.bottomAfter, result.bottomBefore, 'Escape must restore the original viewport and bottom scroll position')
  assert.equal(result.ownerHtmlEntries, 7)
  console.log('Native Workbench fullscreen: video occlusion, viewport/scroll retention, Escape, tab creation/closure, navigation and visibility events passed')
} finally {
  await instance?.close().catch(() => {})
  server.closeAllConnections()
  await new Promise(resolveClosed => server.close(resolveClosed))
  await rm(profile, { recursive: true, force: true })
}
