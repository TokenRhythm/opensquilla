import assert from 'node:assert/strict'
import { spawn, spawnSync } from 'node:child_process'
import { createServer } from 'node:http'
import { mkdtemp, rm } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms))
async function until(predicate, label) {
  const deadline = Date.now() + 8000
  while (Date.now() < deadline) {
    if (await predicate()) return
    await sleep(20)
  }
  throw new Error(`Timed out waiting for ${label}`)
}

if (!process.versions.electron) {
  if (process.platform === 'linux' && !process.env.DISPLAY && !process.env.WAYLAND_DISPLAY
    && process.env.OPENSQUILLA_HOST_NAVIGATION_UNDER_XVFB !== '1') {
    const display = spawnSync('xvfb-run', ['-a', process.execPath, fileURLToPath(import.meta.url)], {
      env: { ...process.env, OPENSQUILLA_HOST_NAVIGATION_UNDER_XVFB: '1' }, stdio: 'inherit',
    })
    if (display.error) throw display.error
    process.exit(display.status ?? 1)
  }
  const profile = await mkdtemp(join(tmpdir(), 'opensquilla-browser-host-navigation-'))
  const require = createRequire(import.meta.url)
  const child = spawn(require('electron'), [
    ...(process.platform === 'linux' && process.getuid?.() === 0 ? ['--no-sandbox'] : []),
    `--user-data-dir=${join(profile, 'chromium')}`, fileURLToPath(import.meta.url),
  ], { env: { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: 'true' }, stdio: 'inherit' })
  const watchdog = setTimeout(() => child.kill('SIGKILL'), 45_000)
  let code
  try {
    code = await new Promise((resolve, reject) => {
      child.once('error', reject)
      child.once('exit', status => resolve(status ?? 1))
    })
  } finally {
    clearTimeout(watchdog)
    await rm(profile, { recursive: true, force: true })
  }
  process.exit(code)
} else {
  const { app, BrowserWindow, dialog } = await import('electron')
  const { NativeWorkbenchSurfaceManager } = await import('../dist/native-workbench-surface.js')
  const { createBrowserReloadGuard } = await import('../dist/desktop-browser-reload-guard.js')
  app.commandLine.appendSwitch('disable-gpu')
  app.on('window-all-closed', () => {})
  void (async () => {
    await app.whenReady()
    let owner, manager
    let exitCode = 0
    let hostRequests = 0
    const web = createServer((request, response) => {
      response.writeHead(200, { 'content-type': 'text/html; charset=utf-8' })
      if (request.url === '/host') {
        hostRequests++
        response.end(`<!doctype html><title>Control host</title><script>window.hostLoad=${hostRequests}</script>`)
      } else if (request.url === '/page') {
        response.end(`<!doctype html><title>Unsaved page</title>
          <button id="guard">Arm unsaved state</button><script>
          document.querySelector('#guard').onclick = () => {
            window.pageState = 'retained';
            window.onbeforeunload = () => true;
          };
          </script>`)
      } else response.end('<!doctype html><title>Other</title>')
    })
    const originalDialog = dialog.showMessageBoxSync
    try {
      await new Promise(resolve => web.listen(0, '127.0.0.1', resolve))
      const origin = `http://127.0.0.1:${web.address().port}`
      const hostUrl = `${origin}/host`
      owner = new BrowserWindow({ show: false, width: 900, height: 700,
        webPreferences: { sandbox: true, contextIsolation: true, nodeIntegration: false } })
      await owner.loadURL(hostUrl)
      manager = new NativeWorkbenchSurfaceManager({ getWindow: () => owner, emit() {} })
      const guardReload = createBrowserReloadGuard(() => manager.closeBrowserTabs())
      const events = []
      let decision
      let cleanup
      owner.webContents.on('did-start-navigation', (_event, url, sameDocument, mainFrame) => {
        if (mainFrame && !sameDocument && url === hostUrl) events.push('did-start')
      })
      owner.webContents.on('will-navigate', (event, url) => {
        if (url !== hostUrl || !manager.hasBrowserTabs()) return
        events.push('will-navigate')
        event.preventDefault()
        decision = guardReload(() => { void owner.webContents.loadURL(url).catch(() => undefined) })
      })
      owner.webContents.on('did-navigate', (_event, url) => {
        if (url !== hostUrl) return
        events.push('did-navigate')
        cleanup = manager.destroyAll()
      })

      const call = request => manager.executeBrowserMcp({ sessionKey: 'synthetic-task',
        observationMode: 'dom', ...request }, AbortSignal.timeout(20_000))
      const page = await call({ operation: 'open', url: `${origin}/page` })
      const record = [...manager.surfaces.values()].find(value => value.targetRef === page.targetRef)
      assert.ok(record)
      const observed = await call({ operation: 'observe', targetRef: page.targetRef })
      const ref = observed.observation.refs.find(value => value.name === 'Arm unsaved state')?.ref
      assert.ok(ref)
      await call({ operation: 'act', targetRef: page.targetRef, action: 'click', ref })
      assert.equal(await record.contents.executeJavaScript('navigator.userActivation.hasBeenActive'), true)
      assert.equal(await record.contents.executeJavaScript('window.pageState'), 'retained')
      await owner.webContents.executeJavaScript("window.controlState='retained'")

      let confirmations = 0
      dialog.showMessageBoxSync = () => { confirmations++; return 0 }
      void owner.webContents.executeJavaScript('location.reload()').catch(() => undefined)
      await until(() => Boolean(decision), 'guarded host reload')
      assert.equal(await decision, false)
      assert.ok(events.indexOf('did-start') !== -1 && events.indexOf('did-start') < events.indexOf('will-navigate'),
        `A cancellable navigation must not release a child at did-start: ${JSON.stringify(events)}`)
      assert.equal(events.includes('did-navigate'), false)
      assert.equal(hostRequests, 1)
      assert.equal(cleanup, undefined)
      assert.equal(manager.surfaces.get(record.id), record)
      assert.equal(record.contents.isDestroyed(), false)
      assert.equal(await record.contents.executeJavaScript('window.pageState'), 'retained')
      assert.equal(await owner.webContents.executeJavaScript('window.controlState'), 'retained')

      decision = undefined
      events.length = 0
      dialog.showMessageBoxSync = () => { confirmations++; return 1 }
      void owner.webContents.executeJavaScript('location.reload()').catch(() => undefined)
      await until(() => Boolean(decision), 'accepted host reload')
      assert.equal(await decision, true)
      await until(() => events.includes('did-navigate'), 'committed host document')
      await cleanup
      await until(async () => (await owner.webContents.executeJavaScript('window.hostLoad').catch(() => undefined)) === 2,
        'new host document script')
      assert.equal(hostRequests, 2)
      assert.equal(await owner.webContents.executeJavaScript('window.hostLoad'), 2)
      assert.equal(await owner.webContents.executeJavaScript('window.controlState'), undefined)
      assert.equal(record.contents.isDestroyed(), true)
      assert.equal(manager.surfaces.size, 0)
      assert.ok(confirmations >= 2)
      console.log('Browser host navigation passed: cancelled renderer reload retains dirty tabs and JS state; accepted reload commits and cleans up.')
    } catch (error) {
      exitCode = 1
      console.error(error)
    } finally {
      dialog.showMessageBoxSync = originalDialog
      const shutdown = setTimeout(() => app.exit(exitCode), 3000)
      shutdown.unref()
      await manager?.destroyAll().catch(() => undefined)
      if (owner && !owner.isDestroyed()) owner.destroy()
      web.closeAllConnections()
      await new Promise(resolve => web.close(resolve))
      app.exit(exitCode)
    }
  })()
}
