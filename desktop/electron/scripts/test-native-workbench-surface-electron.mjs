import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { _electron as electron } from 'playwright'

const scriptPath = fileURLToPath(import.meta.url)
const scriptDir = dirname(scriptPath)
const fixtureRoot = join(scriptDir, 'fixtures', 'native-workbench-smoke')

// GitHub's Linux Electron jobs already use xvfb-run. Make the package script
// equally reliable when invoked directly on a headless Linux worker.
if (
  process.platform === 'linux'
  && !process.env.DISPLAY
  && !process.env.WAYLAND_DISPLAY
  && process.env.OPENSQUILLA_WORKBENCH_UNDER_XVFB !== '1'
) {
  const result = spawnSync(
    'xvfb-run',
    ['-a', process.execPath, scriptPath],
    {
      env: {
        ...process.env,
        OPENSQUILLA_WORKBENCH_UNDER_XVFB: '1',
      },
      stdio: 'inherit',
    },
  )
  if (result.error) {
    throw new Error(`A display or xvfb-run is required for the Electron smoke test: ${result.error.message}`)
  }
  process.exit(result.status ?? 1)
}

const isolationRoot = await mkdtemp(join(tmpdir(), 'opensquilla-workbench-smoke-'))
let electronApp

try {
  electronApp = await electron.launch({
    args: [
      `--user-data-dir=${join(isolationRoot, 'chromium')}`,
      fixtureRoot,
    ],
    env: {
      ...process.env,
      ELECTRON_DISABLE_SECURITY_WARNINGS: 'true',
      NO_PROXY: '127.0.0.1,localhost',
      no_proxy: '127.0.0.1,localhost',
    },
  })

  const result = await electronApp.evaluate(
    async ({ BrowserWindow, webContents }) => {
      const NativeWorkbenchSurfaceManager =
        globalThis.__opensquillaNativeWorkbenchSurfaceManager
      if (!NativeWorkbenchSurfaceManager) {
        throw new Error('The native Workbench manager fixture was not installed.')
      }

      const events = []
      const owner = new BrowserWindow({
        show: true,
        width: 900,
        height: 700,
        webPreferences: {
          contextIsolation: true,
          nodeIntegration: false,
          partition: `opensquilla-workbench-owner:${Date.now()}`,
          sandbox: true,
        },
      })
      await owner.loadURL('data:text/html,<title>Trusted Control UI fixture</title>')

      const manager = new NativeWorkbenchSurfaceManager({
        getWindow: () => owner,
        emit: event => events.push(event),
      })
      const encoder = new TextEncoder()
      const remoteOrigin = 'https://assets.example.test'

      async function waitFor(check, label, timeoutMs = 8_000) {
        const deadline = Date.now() + timeoutMs
        while (Date.now() < deadline) {
          const value = check()
          if (value) return value
          await new Promise(resolveWait => setTimeout(resolveWait, 25))
        }
        throw new Error(`Timed out waiting for ${label}.`)
      }

      function previewContents() {
        return webContents.getAllWebContents().find(contents =>
          contents.getURL().startsWith('opensquilla-artifact://'))
      }

      function emitRendererGone(contents) {
        // This smoke test owns the manager's Electron event contract, not
        // Chromium's renderer termination implementation. In particular,
        // forcefullyCrashRenderer() can silently stall for a hidden
        // WebContentsView under Linux + Xvfb. Emit the documented lifecycle
        // event on the real WebContents so every platform deterministically
        // exercises the registered listener and fail-closed recovery path.
        const handled = contents.emit(
          'render-process-gone',
          {},
          { reason: 'crashed', exitCode: 1 },
        )
        if (!handled) throw new Error('No renderer crash listener was registered.')
      }

      function emitUnresponsive(contents) {
        const handled = contents.emit('unresponsive')
        if (!handled) throw new Error('No renderer unresponsive listener was registered.')
      }

      function surfaceView(surfaceId) {
        const view = manager.surfaces.get(surfaceId)?.view
        if (!view) throw new Error(`Surface view ${surfaceId} was not found.`)
        return view
      }

      function installSyntheticHttpsProtocol(contents) {
        let requestCount = 0
        contents.session.protocol.handle('https', request => {
          requestCount += 1
          const url = new URL(request.url)
          if (url.pathname.endsWith('.css')) {
            return new Response(':root { --remote-stylesheet-probe: loaded; }', {
              headers: {
                'cache-control': 'no-store',
                'content-type': 'text/css; charset=utf-8',
              },
            })
          }
          if (url.pathname.endsWith('.js')) {
            return new Response(
              'window.__remoteProbe = (window.__remoteProbe || 0) + 1',
              {
                headers: {
                  'cache-control': 'no-store',
                  'content-type': 'application/javascript; charset=utf-8',
                },
              },
            )
          }
          if (url.pathname === '/download.txt') {
            return new Response('synthetic download', {
              headers: {
                'content-disposition': 'attachment; filename="synthetic.txt"',
                'content-type': 'text/plain; charset=utf-8',
              },
            })
          }
          return new Response(null, { status: 204 })
        })
        return () => requestCount
      }

      async function appendRemoteScript(contents, path) {
        return await contents.executeJavaScript(`new Promise(resolve => {
          const script = document.createElement('script')
          script.src = '${remoteOrigin}' + ${JSON.stringify(path)}
          script.onload = () => resolve('loaded')
          script.onerror = () => resolve('blocked')
          document.head.append(script)
          setTimeout(() => resolve('timeout'), 3000)
        })`)
      }

      async function appendRemoteStylesheet(contents, path) {
        return await contents.executeJavaScript(`new Promise(resolve => {
          const stylesheet = document.createElement('link')
          stylesheet.rel = 'stylesheet'
          stylesheet.href = '${remoteOrigin}' + ${JSON.stringify(path)}
          stylesheet.onload = () => resolve('loaded')
          stylesheet.onerror = () => resolve('blocked')
          document.head.append(stylesheet)
          setTimeout(() => resolve('timeout'), 3000)
        })`)
      }

      async function requestRemoteData(contents, path) {
        return await contents.executeJavaScript(`fetch(
          '${remoteOrigin}' + ${JSON.stringify(path)}
        ).then(() => 'loaded', () => 'blocked')`)
      }

      const offline = await manager.createSurface({
        version: 1,
        surfaceId: 'artifact:offline',
        kind: 'artifact-html',
        payload: {
          data: encoder.encode('<!doctype html><img src="./missing.png"><title>Offline preview</title>'),
          name: 'offline.html',
          mime: 'text/html',
          scopeId: 'synthetic:offline',
          allowRemoteResources: false,
        },
      })
      if (!offline.ok) throw new Error(offline.message || 'Offline surface failed to load.')
      const offlineContents = await waitFor(previewContents, 'offline preview WebContents')
      const offlineRequestCount = installSyntheticHttpsProtocol(offlineContents)
      const offlineScriptResult = await appendRemoteScript(offlineContents, '/offline.js')
      const offlineProbe = await offlineContents.executeJavaScript(
        'Number(window.__remoteProbe || 0)',
      )
      // Triggering a JavaScript dialog races Playwright's own CDP dialog
      // auto-dismissal against Chromium's disableDialogs handling. Assert the
      // effective WebContents preference instead; the browser process enforces
      // this preference before artifact code can show a native dialog.
      const dialogsDisabled =
        offlineContents.getLastWebPreferences().disableDialogs === true
      await waitFor(
        () => events.some(event =>
          event.surfaceId === 'artifact:offline'
          && event.type === 'missing-resource'
          && event.detail?.path === '/missing.png'),
        'relative-resource event',
      )
      await offlineContents.executeJavaScript(`Promise.allSettled(
        Array.from({ length: 400 }, (_, index) =>
          fetch('/missing-' + index + '.json?request=' + index)
        )
      )`)
      const missingResourceEventCount = events.filter(event =>
        event.surfaceId === 'artifact:offline'
        && event.type === 'missing-resource').length

      let popupCreated = false
      offlineContents.once('did-create-window', () => { popupCreated = true })
      const popupWasBlocked = await offlineContents.executeJavaScript(
        `window.open('${remoteOrigin}/popup') === null`,
      )
      await new Promise(resolveWait => setTimeout(resolveWait, 100))

      const documentUrl = offlineContents.getURL()
      await offlineContents.executeJavaScript(
        `location.href = '${remoteOrigin}/navigate'`,
      ).catch(() => undefined)
      await new Promise(resolveWait => setTimeout(resolveWait, 150))
      const navigationWasBlocked = offlineContents.getURL() === documentUrl

      const notificationPermission = await offlineContents.executeJavaScript(
        'Notification.requestPermission()',
      )

      let downloadSeen = false
      let downloadPrevented = false
      offlineContents.session.prependOnceListener('will-download', (event) => {
        downloadSeen = true
        const originalPreventDefault = event.preventDefault.bind(event)
        event.preventDefault = () => {
          downloadPrevented = true
          originalPreventDefault()
        }
      })
      offlineContents.downloadURL(`${remoteOrigin}/download.txt`)
      await waitFor(() => downloadSeen, 'blocked download')

      await manager.destroySurface('artifact:offline')
      await waitFor(() => offlineContents.isDestroyed(), 'surface destruction')

      const allowed = await manager.createSurface({
        version: 1,
        surfaceId: 'artifact:allowed',
        kind: 'artifact-html',
        payload: {
          data: encoder.encode('<!doctype html><title>Remote-resource preview</title>'),
          name: 'allowed.html',
          mime: 'text/html',
          scopeId: 'synthetic:allowed',
          allowRemoteResources: true,
        },
      })
      if (!allowed.ok) throw new Error(allowed.message || 'Allowed surface failed to load.')
      const allowedContents = await waitFor(previewContents, 'allowed preview WebContents')
      const allowedRequestCount = installSyntheticHttpsProtocol(allowedContents)
      const allowedStylesheetResult = await appendRemoteStylesheet(
        allowedContents,
        '/allowed.css',
      )
      const allowedStylesheetProbe = await allowedContents.executeJavaScript(
        "getComputedStyle(document.documentElement).getPropertyValue('--remote-stylesheet-probe').trim()",
      )
      const allowedScriptResult = await appendRemoteScript(allowedContents, '/allowed.js')
      const allowedProbe = await allowedContents.executeJavaScript(
        'Number(window.__remoteProbe || 0)',
      )
      const allowedDataResult = await requestRemoteData(allowedContents, '/data.json')
      await manager.destroySurface('artifact:allowed')
      await waitFor(() => allowedContents.isDestroyed(), 'allowed surface destruction')

      const zoomSurface = await manager.createSurface({
        version: 1,
        surfaceId: 'artifact:zoom',
        kind: 'artifact-html',
        payload: {
          data: encoder.encode('<!doctype html><title>Zoom preview</title>'),
          name: 'zoom.html',
          mime: 'text/html',
          scopeId: 'synthetic:zoom',
          allowRemoteResources: false,
        },
      })
      if (!zoomSurface.ok) throw new Error(zoomSurface.message || 'Zoom surface failed to load.')
      const zoomContents = await waitFor(previewContents, 'zoom preview WebContents')
      const zoomRect = manager.setSurfaceRect({
        surfaceId: 'artifact:zoom',
        x: 120,
        y: 90,
        width: 360,
        height: 240,
        visible: true,
      })
      if (!zoomRect.ok) throw new Error(zoomRect.message || 'Zoom surface bounds were rejected.')
      const zoomActivation = manager.activateSurface('artifact:zoom')
      if (!zoomActivation.ok) throw new Error(zoomActivation.message || 'Zoom surface failed to activate.')
      const zoomView = owner.contentView.children.find(view => view.webContents === zoomContents)
      if (!zoomView) throw new Error('Zoom surface view was not attached to its owner.')
      const zoomBoundsBefore = zoomView.getBounds()
      const zoomModifier = process.platform === 'darwin' ? 'meta' : 'control'
      zoomContents.sendInputEvent({ type: 'keyDown', keyCode: '=', modifiers: [zoomModifier] })
      await waitFor(
        () => Math.abs(owner.webContents.getZoomFactor() - 1.2) < 1e-6,
        'child zoom shortcut to update the owner factor',
      )
      const zoomChildFactor = zoomContents.getZoomFactor()
      const zoomBoundsAt120 = zoomView.getBounds()
      zoomContents.sendInputEvent({ type: 'keyDown', keyCode: '0', modifiers: [zoomModifier] })
      await waitFor(
        () => Math.abs(owner.webContents.getZoomFactor() - 1) < 1e-6,
        'child zoom reset to restore the owner factor',
      )
      const zoomBoundsAfterReset = zoomView.getBounds()
      await manager.destroySurface('artifact:zoom')
      await waitFor(() => zoomContents.isDestroyed(), 'zoom surface destruction')

      const lifecycleSurface = await manager.createSurface({
        version: 1,
        surfaceId: 'artifact:lifecycle',
        kind: 'artifact-html',
        payload: {
          data: encoder.encode('<!doctype html><title>Lifecycle preview</title>'),
          name: 'lifecycle.html',
          mime: 'text/html',
          scopeId: 'synthetic:lifecycle',
          allowRemoteResources: false,
        },
      })
      if (!lifecycleSurface.ok) {
        throw new Error(lifecycleSurface.message || 'Lifecycle surface failed to load.')
      }
      const lifecycleContents = await waitFor(previewContents, 'lifecycle WebContents')
      const lifecycleView = surfaceView('artifact:lifecycle')
      const lifecycleRect = {
        surfaceId: 'artifact:lifecycle',
        x: 400,
        y: 80,
        width: 400,
        height: 500,
        visible: true,
      }
      const lifecyclePositioned = manager.setSurfaceRect(lifecycleRect)
      await waitFor(() => lifecycleView.getVisible(), 'visible lifecycle surface')
      const realIsVisible = owner.isVisible.bind(owner)
      owner.isVisible = () => false
      owner.emit('hide')
      await waitFor(() => !lifecycleView.getVisible(), 'hidden owner surface')
      const hiddenRectAccepted = manager.setSurfaceRect(lifecycleRect)
      const hiddenActivation = manager.activateSurface('artifact:lifecycle')
      const hiddenSurfaceStayedHidden = !lifecycleView.getVisible()
      owner.isVisible = () => true
      owner.emit('show')
      await waitFor(() => lifecycleView.getVisible(), 'surface restored after owner show')
      // Window-manager support varies in headless CI, especially macOS and
      // Xvfb. Drive Electron's documented lifecycle event while overriding
      // only the real BrowserWindow state query used by the manager.
      const realIsMinimized = owner.isMinimized.bind(owner)
      owner.isMinimized = () => true
      owner.emit('minimize')
      await waitFor(() => !lifecycleView.getVisible(), 'minimized owner surface')
      const minimizedActivation = manager.activateSurface('artifact:lifecycle')
      const minimizedSurfaceStayedHidden = !lifecycleView.getVisible()
      owner.isMinimized = realIsMinimized
      owner.emit('restore')
      await waitFor(() => lifecycleView.getVisible(), 'surface restored with owner window')
      const lifecycleSurfaceRestored = lifecycleView.getVisible()
      owner.isVisible = realIsVisible
      await manager.destroySurface('artifact:lifecycle')
      await waitFor(() => lifecycleContents.isDestroyed(), 'lifecycle surface destruction')

      const loadErrorSurface = await manager.createSurface({
        version: 1,
        surfaceId: 'artifact:load-error',
        kind: 'artifact-html',
        payload: {
          data: encoder.encode('<!doctype html><title>Load error</title>'),
          name: 'load-error.html',
          mime: 'text/html',
          scopeId: 'synthetic:load-error',
          allowRemoteResources: false,
        },
      })
      if (!loadErrorSurface.ok) {
        throw new Error(loadErrorSurface.message || 'Load-error surface failed to load.')
      }
      const loadErrorContents = await waitFor(previewContents, 'load-error WebContents')
      await loadErrorContents.session.protocol.unhandle('opensquilla-artifact')
      await loadErrorContents.session.protocol.handle('opensquilla-artifact', () => {
        throw new Error('synthetic main document failure')
      })
      loadErrorContents.reloadIgnoringCache()
      await waitFor(
        () => events.some(event =>
          event.surfaceId === 'artifact:load-error'
          && event.type === 'error'),
        'main-frame load error event',
      )
      const loadErrorActivation = manager.activateSurface('artifact:load-error')
      await manager.destroySurface('artifact:load-error')

      const raceBase = await manager.createSurface({
        version: 1,
        surfaceId: 'artifact:replace-close-race',
        kind: 'artifact-html',
        payload: {
          data: encoder.encode('<!doctype html><title>Race base</title>'),
          name: 'race-base.html',
          mime: 'text/html',
          scopeId: 'synthetic:replace-close-race',
          allowRemoteResources: false,
        },
      })
      if (!raceBase.ok) throw new Error(raceBase.message || 'Race base surface failed to load.')
      const raceBaseContents = await waitFor(previewContents, 'race base preview WebContents')
      const replacementPromise = manager.createSurface({
        version: 1,
        surfaceId: 'artifact:replace-close-race',
        kind: 'artifact-html',
        payload: {
          data: encoder.encode('<!doctype html><title>Race replacement</title>'),
          name: 'race-replacement.html',
          mime: 'text/html',
          scopeId: 'synthetic:replace-close-race',
          allowRemoteResources: false,
        },
      })
      const closeDuringReplacement = manager.destroySurface('artifact:replace-close-race')
      await Promise.all([replacementPromise, closeDuringReplacement])
      await waitFor(
        () => !previewContents(),
        'replacement followed by close to destroy every preview WebContents',
      )
      const replacementCloseActivation = manager.activateSurface(
        'artifact:replace-close-race',
      )
      const raceBaseDestroyed = raceBaseContents.isDestroyed()

      const previewCrashSurface = await manager.createSurface({
        version: 1,
        surfaceId: 'artifact:preview-crash',
        kind: 'artifact-html',
        payload: {
          data: encoder.encode('<!doctype html><title>Preview crash</title>'),
          name: 'preview-crash.html',
          mime: 'text/html',
          scopeId: 'synthetic:preview-crash',
          allowRemoteResources: false,
        },
      })
      if (!previewCrashSurface.ok) {
        throw new Error(previewCrashSurface.message || 'Preview-crash surface failed to load.')
      }
      const previewCrashContents = await waitFor(previewContents, 'preview-crash WebContents')
      emitRendererGone(previewCrashContents)
      await waitFor(
        () => events.some(event =>
          event.surfaceId === 'artifact:preview-crash'
          && event.type === 'crashed'),
        'preview renderer crash event',
      )
      const previewCrashActivation = manager.activateSurface('artifact:preview-crash')
      const previewCrashRect = manager.setSurfaceRect({
        surfaceId: 'artifact:preview-crash',
        x: 400,
        y: 80,
        width: 400,
        height: 500,
        visible: true,
      })
      await manager.destroySurface('artifact:preview-crash')

      const unresponsiveSurface = await manager.createSurface({
        version: 1,
        surfaceId: 'artifact:unresponsive',
        kind: 'artifact-html',
        payload: {
          data: encoder.encode('<!doctype html><title>Unresponsive preview</title>'),
          name: 'unresponsive.html',
          mime: 'text/html',
          scopeId: 'synthetic:unresponsive',
          allowRemoteResources: false,
        },
      })
      if (!unresponsiveSurface.ok) {
        throw new Error(unresponsiveSurface.message || 'Unresponsive surface failed to load.')
      }
      const unresponsiveContents = await waitFor(
        previewContents,
        'unresponsive preview WebContents',
      )
      const unresponsiveView = surfaceView('artifact:unresponsive')
      const unresponsiveRectRequest = {
        surfaceId: 'artifact:unresponsive',
        x: 400,
        y: 80,
        width: 400,
        height: 500,
        visible: true,
      }
      manager.setSurfaceRect(unresponsiveRectRequest)
      await waitFor(() => unresponsiveView.getVisible(), 'visible unresponsive surface')
      const unresponsiveSurfaceWasVisible = unresponsiveView.getVisible()
      emitUnresponsive(unresponsiveContents)
      await waitFor(
        () => events.some(event =>
          event.surfaceId === 'artifact:unresponsive'
          && event.type === 'crashed'
          && event.detail?.reason === 'unresponsive'),
        'unresponsive renderer crash event',
      )
      await waitFor(
        () => !unresponsiveView.getVisible(),
        'unresponsive surface to become physically hidden',
      )
      emitUnresponsive(unresponsiveContents)
      emitRendererGone(unresponsiveContents)
      unresponsiveContents.emit(
        'did-fail-load',
        {},
        -2,
        'synthetic failure after unresponsive',
        unresponsiveContents.getURL(),
        true,
      )
      const unresponsiveTerminalEventCount = events.filter(event =>
        event.surfaceId === 'artifact:unresponsive'
        && (event.type === 'error' || event.type === 'crashed')).length
      const unresponsiveActivation = manager.activateSurface('artifact:unresponsive')
      const unresponsiveRect = manager.setSurfaceRect(unresponsiveRectRequest)
      owner.emit('hide')
      owner.emit('show')
      owner.setSize(901, 701)
      owner.webContents.emit('zoom-changed', {}, 'in')
      await new Promise(resolveWait => setTimeout(resolveWait, 100))
      const failedSurfaceStayedHidden = !unresponsiveView.getVisible()
      await manager.destroySurface('artifact:unresponsive')

      const ownerUnresponsiveSurfaceOne = await manager.createSurface({
        version: 1,
        surfaceId: 'artifact:owner-unresponsive-one',
        kind: 'artifact-html',
        payload: {
          data: encoder.encode('<!doctype html><title>Owner unresponsive one</title>'),
          name: 'owner-unresponsive-one.html',
          mime: 'text/html',
          scopeId: 'synthetic:owner-unresponsive-one',
          allowRemoteResources: false,
        },
      })
      const ownerUnresponsiveSurfaceTwo = await manager.createSurface({
        version: 1,
        surfaceId: 'artifact:owner-unresponsive-two',
        kind: 'artifact-html',
        payload: {
          data: encoder.encode('<!doctype html><title>Owner unresponsive two</title>'),
          name: 'owner-unresponsive-two.html',
          mime: 'text/html',
          scopeId: 'synthetic:owner-unresponsive-two',
          allowRemoteResources: false,
        },
      })
      if (!ownerUnresponsiveSurfaceOne.ok || !ownerUnresponsiveSurfaceTwo.ok) {
        throw new Error('Owner-unresponsive surfaces failed to load.')
      }
      const ownerUnresponsiveViewOne = surfaceView('artifact:owner-unresponsive-one')
      const ownerUnresponsiveViewTwo = surfaceView('artifact:owner-unresponsive-two')
      manager.setSurfaceRect({
        surfaceId: 'artifact:owner-unresponsive-one',
        x: 400,
        y: 80,
        width: 400,
        height: 500,
        visible: true,
      })
      await waitFor(
        () => ownerUnresponsiveViewOne.getVisible(),
        'visible owner-unresponsive surface',
      )
      emitUnresponsive(owner.webContents)
      await waitFor(
        () => events.filter(event =>
          event.type === 'crashed'
          && event.detail?.reason === 'owner-unresponsive').length === 2,
        'owner-unresponsive events for all surfaces',
      )
      const ownerUnresponsiveViewsHidden = !ownerUnresponsiveViewOne.getVisible()
        && !ownerUnresponsiveViewTwo.getVisible()
      const ownerUnresponsiveActivation = manager.activateSurface(
        'artifact:owner-unresponsive-one',
      )
      emitUnresponsive(owner.webContents)
      const ownerUnresponsiveTerminalEventCount = events.filter(event =>
        event.type === 'crashed'
        && event.detail?.reason === 'owner-unresponsive').length
      owner.webContents.emit('responsive')
      await manager.destroyAll()

      const ownerCrashSurface = await manager.createSurface({
        version: 1,
        surfaceId: 'artifact:owner-crash',
        kind: 'artifact-html',
        payload: {
          data: encoder.encode('<!doctype html><title>Owner crash cleanup</title>'),
          name: 'owner-crash.html',
          mime: 'text/html',
          scopeId: 'synthetic:owner-crash',
          allowRemoteResources: false,
        },
      })
      if (!ownerCrashSurface.ok) {
        throw new Error(ownerCrashSurface.message || 'Owner-crash surface failed to load.')
      }
      const crashContents = await waitFor(previewContents, 'owner-crash preview WebContents')
      emitRendererGone(owner.webContents)
      await waitFor(() => crashContents.isDestroyed(), 'owner renderer crash cleanup')
      const crashActivation = manager.activateSurface('artifact:owner-crash')

      await manager.destroyAll()
      if (!owner.isDestroyed()) owner.destroy()

      return {
        allowedProbe,
        allowedRequestCount: allowedRequestCount(),
        allowedDataResult,
        allowedScriptResult,
        allowedStylesheetProbe,
        allowedStylesheetResult,
        crashActivation,
        dialogsDisabled,
        downloadPrevented,
        downloadSeen,
        failedSurfaceStayedHidden,
        hiddenActivation,
        hiddenRectAccepted,
        hiddenSurfaceStayedHidden,
        lifecyclePositioned,
        lifecycleSurfaceRestored,
        loadErrorActivation,
        minimizedActivation,
        minimizedSurfaceStayedHidden,
        navigationWasBlocked,
        missingResourceEventCount,
        notificationPermission,
        offlineProbe,
        offlineRequestCount: offlineRequestCount(),
        offlineScriptResult,
        ownerUnresponsiveActivation,
        ownerUnresponsiveTerminalEventCount,
        ownerUnresponsiveViewsHidden,
        popupCreated,
        popupWasBlocked,
        previewCrashActivation,
        previewCrashRect,
        raceBaseDestroyed,
        replacementCloseActivation,
        zoomBoundsAfterReset,
        zoomBoundsAt120,
        zoomBoundsBefore,
        zoomChildFactor,
        unresponsiveActivation,
        unresponsiveRect,
        unresponsiveSurfaceWasVisible,
        unresponsiveTerminalEventCount,
      }
    },
  )

  assert.equal(result.offlineScriptResult, 'blocked', 'offline HTTPS script must fail')
  assert.equal(result.offlineProbe, 0, 'offline surfaces must not execute HTTPS scripts')
  assert.equal(result.offlineRequestCount, 0, 'offline HTTPS must stop before protocol dispatch')
  assert.equal(
    result.allowedStylesheetResult,
    'loaded',
    'explicit permission must load passive HTTPS resources',
  )
  assert.equal(
    result.allowedStylesheetProbe,
    'loaded',
    'the allowed passive resource must affect the preview',
  )
  assert.equal(
    result.allowedScriptResult,
    'blocked',
    'online-resource permission must not load remote scripts',
  )
  assert.equal(
    result.allowedProbe,
    0,
    'online-resource permission must not execute remote scripts',
  )
  assert.equal(
    result.allowedDataResult,
    'blocked',
    'online-resource permission must not enable remote data requests',
  )
  assert.equal(
    result.allowedRequestCount,
    1,
    'only the passive HTTPS resource may reach the protocol handler',
  )
  assert.equal(
    result.dialogsDisabled,
    true,
    'artifact scripts must not open blocking native dialogs',
  )
  assert.equal(result.popupWasBlocked, true, 'window.open must return null')
  assert.equal(result.popupCreated, false, 'blocked popup must not create a child WebContents')
  assert.equal(result.navigationWasBlocked, true, 'renderer top navigation must retain the artifact URL')
  assert.equal(
    result.missingResourceEventCount,
    1,
    'missing subresources must collapse to one bounded renderer event',
  )
  assert.equal(result.notificationPermission, 'denied', 'preview permissions must be denied')
  assert.equal(result.downloadSeen, true, 'download policy must observe attempted downloads')
  assert.equal(result.downloadPrevented, true, 'download policy must prevent the download')
  assert.equal(result.lifecyclePositioned.ok, true, 'a healthy visible surface must be positioned')
  assert.equal(result.hiddenRectAccepted.ok, true, 'hidden windows must retain visible surface intent')
  assert.equal(result.hiddenActivation.ok, true, 'hidden windows must retain surface activation')
  assert.equal(
    result.hiddenSurfaceStayedHidden,
    true,
    'a surface must remain physically hidden while its owner is hidden',
  )
  assert.equal(
    result.minimizedActivation.ok,
    true,
    'minimized windows must retain surface activation',
  )
  assert.equal(
    result.minimizedSurfaceStayedHidden,
    true,
    'a surface must remain physically hidden while its owner is minimized',
  )
  assert.equal(
    result.lifecycleSurfaceRestored,
    true,
    'a healthy requested surface must return after its owner is restored',
  )
  assert.equal(
    result.loadErrorActivation.ok,
    false,
    'a failed native document must stay hidden behind the DOM recovery state',
  )
  assert.equal(
    result.raceBaseDestroyed,
    true,
    'replacing a surface must destroy the previous WebContents',
  )
  assert.equal(
    result.replacementCloseActivation.ok,
    false,
    'a close queued during replacement must not leave a zombie surface',
  )
  assert.equal(
    result.previewCrashActivation.ok,
    false,
    'a crashed preview renderer must never be reactivated above the DOM error UI',
  )
  assert.equal(
    result.previewCrashRect.ok,
    false,
    'a crashed preview renderer must reject visible bounds until it is recreated',
  )
  assert.equal(result.zoomChildFactor, 1, 'child input must leave the preview zoom unchanged')
  assert.deepEqual(
    result.zoomBoundsAt120,
    { x: 144, y: 108, width: 432, height: 288 },
    'child zoom input must reapply active bounds at the owner zoom factor',
  )
  assert.deepEqual(
    result.zoomBoundsAfterReset,
    result.zoomBoundsBefore,
    'child zoom reset must restore the owner-scaled native bounds',
  )
  assert.equal(
    result.unresponsiveSurfaceWasVisible,
    true,
    'the unresponsive contract must exercise a physically visible native surface',
  )
  assert.equal(
    result.unresponsiveTerminalEventCount,
    1,
    'repeated terminal events must emit one final surface state',
  )
  assert.equal(
    result.unresponsiveActivation.ok,
    false,
    'an unresponsive preview renderer must never be reactivated',
  )
  assert.equal(
    result.unresponsiveRect.ok,
    false,
    'an unresponsive preview renderer must reject delayed visible bounds',
  )
  assert.equal(
    result.failedSurfaceStayedHidden,
    true,
    'show, resize and zoom must not resurrect a failed native surface',
  )
  assert.equal(
    result.ownerUnresponsiveViewsHidden,
    true,
    'an unresponsive owner must physically hide all of its child surfaces',
  )
  assert.equal(
    result.ownerUnresponsiveActivation.ok,
    false,
    'owner-unresponsive surfaces must reject reactivation',
  )
  assert.equal(
    result.ownerUnresponsiveTerminalEventCount,
    2,
    'each surface must emit one owner-unresponsive terminal event',
  )
  assert.equal(result.crashActivation.ok, false, 'owner crash must remove owned surfaces')

  console.log('native Workbench real Electron smoke checks passed')
} finally {
  if (electronApp) await electronApp.close().catch(() => {})
  await rm(isolationRoot, { recursive: true, force: true })
}
