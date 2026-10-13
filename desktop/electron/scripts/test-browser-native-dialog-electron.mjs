import assert from 'node:assert/strict'
import { spawn, spawnSync } from 'node:child_process'
import { createServer } from 'node:http'
import { mkdtemp, rm } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

if (!process.versions.electron) {
  if (process.platform === 'linux' && !process.env.DISPLAY && !process.env.WAYLAND_DISPLAY
    && process.env.OPENSQUILLA_NATIVE_DIALOG_UNDER_XVFB !== '1') {
    const display = spawnSync('xvfb-run', ['-a', process.execPath, fileURLToPath(import.meta.url)], {
      env: { ...process.env, OPENSQUILLA_NATIVE_DIALOG_UNDER_XVFB: '1' }, stdio: 'inherit',
    })
    if (display.error) throw display.error
    process.exit(display.status ?? 1)
  }
  const root = await mkdtemp(join(tmpdir(), 'opensquilla-native-dialog-'))
  const require = createRequire(import.meta.url)
  const child = spawn(require('electron'), [
    ...(process.platform === 'linux' && process.getuid?.() === 0 ? ['--no-sandbox'] : []),
    `--user-data-dir=${join(root, 'chromium')}`, fileURLToPath(import.meta.url),
  ], { env: { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: 'true' }, stdio: 'inherit' })
  const watchdog = setTimeout(() => child.kill('SIGKILL'), 30_000)
  let code
  try {
    code = await new Promise((resolve, reject) => {
      child.once('error', reject)
      child.once('exit', status => resolve(status ?? 1))
    })
  } finally {
    clearTimeout(watchdog)
    await rm(root, { recursive: true, force: true })
  }
  process.exit(code)
} else {
  const { app, BrowserWindow, dialog } = await import('electron')
  const { NativeWorkbenchSurfaceManager } = await import('../dist/native-workbench-surface.js')
  const { DesktopBrowserServer } = await import('../dist/desktop-browser.js')
  app.commandLine.appendSwitch('disable-gpu')
  app.on('window-all-closed', () => {})
  void (async () => {
    await app.whenReady()
    let server, manager, owner
    let exitCode = 0
    const pendingSheets = new Set()
    const originalMessageBox = dialog.showMessageBox
    // Observe the real native dialog lifetime; do not replace or auto-answer it.
    dialog.showMessageBox = function (...args) {
      const options = args.at(-1)
      const sheet = { message: options.message, aborted: false }
      pendingSheets.add(sheet)
      options.signal?.addEventListener('abort', () => { sheet.aborted = true }, { once: true })
      return originalMessageBox.apply(this, args).finally(() => pendingSheets.delete(sheet))
    }
    const web = createServer((request, response) => {
      response.writeHead(200, { 'content-type': 'text/html' })
      const label = request.url === '/other' ? 'Other page confirmation' : 'Synthetic native confirmation'
      response.end(`<!doctype html><title>Native dialog lifecycle</title>
        <button onclick="document.querySelector('output').textContent=confirm('${label}')?'accepted':'dismissed'">Confirm</button>
        <button onclick="if(confirm('First chained confirmation')){alert('Second chained alert');document.querySelector('output').textContent='chain complete'}">Chained dialogs</button>
        <output>pending</output>`)
    })
    try {
      assert.equal(app.commandLine.hasSwitch('remote-debugging-port'), false)
      assert.equal(app.commandLine.hasSwitch('remote-debugging-pipe'), false)
      assert.equal(app.commandLine.hasSwitch('inspect'), false)
      await new Promise(resolve => web.listen(0, '127.0.0.1', resolve))
      owner = new BrowserWindow({ show: true, width: 900, height: 700,
        webPreferences: { sandbox: true, contextIsolation: true, nodeIntegration: false } })
      await owner.loadURL('data:text/html,<title>Native dialog host</title>')
      manager = new NativeWorkbenchSurfaceManager({ getWindow: () => owner, emit() {} })
      server = new DesktopBrowserServer((request, signal) => manager.executeBrowser(request, signal),
        undefined, (request, signal) => manager.executeBrowserMcp(request, signal))
      const environment = await server.start()
      let serial = 0
      const call = async (name, args, expectError = false) => {
        const id = ++serial
        const response = await fetch(environment.OPENSQUILLA_DESKTOP_BROWSER_URL + '/mcp', {
          method: 'POST', signal: AbortSignal.timeout(10_000),
          headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${environment.OPENSQUILLA_DESKTOP_BROWSER_TOKEN}` },
          body: JSON.stringify({ jsonrpc: '2.0', id, method: 'tools/call', params: {
            name, arguments: args, _meta: { sessionKey: 'synthetic-native-dialog', operationId: `dialog-op-${id}`, observationMode: 'dom' },
          } }),
        })
        const envelope = await response.json()
        assert.equal(envelope.result?.isError, expectError, JSON.stringify(envelope))
        return envelope.result.structuredContent
      }
      const until = async (predicate, message) => {
        const deadline = Date.now() + 1000
        while (!predicate() && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 20))
        assert.ok(predicate(), `${message}: ${JSON.stringify([...pendingSheets])}`)
      }
      const nativePending = message => [...pendingSheets].some(sheet => sheet.message === message && !sheet.aborted)
      const nativeClosed = async message => until(() => ![...pendingSheets].some(sheet => sheet.message === message),
        'The renderer resumed, but its native dialog still blocks the host window')
      const openDialog = async (page, name, message) => {
        const observed = await call('browser_observe', { targetRef: page.targetRef })
        const blocked = await call('browser_batch', { targetRef: page.targetRef,
          actions: [{ action: 'click', ref: observed.observation.refs.find(ref => ref.name === name).ref }] })
        const pending = blocked.observation.browserState.dialogs.pending[0]
        assert.equal(pending.message, message)
        await until(() => nativePending(message), 'The fixture must exercise a real native dialog')
        return pending
      }
      const respond = (page, pending, accept) => call('browser_handle_dialog', {
        targetRef: page.targetRef, dialogId: pending.id, accept,
      })
      const origin = `http://127.0.0.1:${web.address().port}`
      const page = await call('browser_open', { url: origin })
      manager.setSurfaceRect({ version: 4, surfaceId: page.surfaceId,
        x: 0, y: 0, width: 900, height: 650, visible: true })
      for (const accept of [true, false]) {
        const pending = await openDialog(page, 'Confirm', 'Synthetic native confirmation')
        assert.equal(pending.type, 'confirm')
        const handled = await respond(page, pending, accept)
        assert.equal(handled.observation.browserState.dialogs.pending.length, 0)
        assert.match(handled.observation.text, accept ? /accepted/ : /dismissed/)
        await nativeClosed(pending.message)
      }

      const first = await openDialog(page, 'Chained dialogs', 'First chained confirmation')
      const halfway = await respond(page, first, true)
      const second = halfway.observation.browserState.dialogs.pending[0]
      assert.equal(second.type, 'alert')
      assert.equal(second.message, 'Second chained alert')
      assert.notEqual(second.id, first.id)
      await nativeClosed(first.message)
      await until(() => nativePending(second.message), 'Closing the first dialog must preserve the next dialog')
      const finished = await respond(page, second, true)
      assert.match(finished.observation.text, /chain complete/)
      await nativeClosed(second.message)

      const other = await call('browser_open', { url: origin + '/other' })
      const otherPending = await openDialog(other, 'Confirm', 'Other page confirmation')
      const ownPending = await openDialog(page, 'Confirm', 'Synthetic native confirmation')
      await respond(page, ownPending, true)
      await nativeClosed(ownPending.message)
      assert.ok(nativePending(otherPending.message), 'Closing one WebContents dialog must not close another page dialog')
      const otherState = await call('browser_observe', { targetRef: other.targetRef })
      assert.equal(otherState.observation.browserState.dialogs.pending[0].id, otherPending.id)
      await respond(other, otherPending, false)
      await nativeClosed(otherPending.message)

      const unavailable = await openDialog(page, 'Confirm', 'Synthetic native confirmation')
      const contents = [...manager.surfaces.values()].find(record => record.targetRef === page.targetRef).view.webContents
      const cancellationListeners = contents.rawListeners('-cancel-dialogs')
      contents.removeAllListeners('-cancel-dialogs')
      try {
        const rejected = await call('browser_handle_dialog', {
          targetRef: page.targetRef, dialogId: unavailable.id, accept: true,
        }, true)
        assert.equal(rejected.code, 'DIALOG_CONTROL_UNAVAILABLE')
        assert.equal(rejected.outcome, 'not_started')
        assert.ok(nativePending(unavailable.message))
        const stillPending = await call('browser_observe', { targetRef: page.targetRef })
        assert.equal(stillPending.observation.browserState.dialogs.pending[0].id, unavailable.id)
      } finally {
        for (const listener of cancellationListeners) contents.on('-cancel-dialogs', listener)
      }
      await respond(page, unavailable, false)
      await nativeClosed(unavailable.message)
      assert.equal(pendingSheets.size, 0)
      console.log('Browser native dialog lifecycle passed without an external debugger: accept, dismiss, chained dialogs, page isolation and unavailable native cleanup.')
    } catch (error) {
      exitCode = 1
      console.error(error)
    } finally {
      const shutdown = setTimeout(() => app.exit(exitCode), 3000)
      shutdown.unref()
      await server?.close().catch(() => {})
      await manager?.destroyAll().catch(() => {})
      if (owner && !owner.isDestroyed()) owner.destroy()
      web.closeAllConnections()
      await new Promise(resolve => web.close(resolve))
      dialog.showMessageBox = originalMessageBox
      app.exit(exitCode)
    }
  })().catch(error => { console.error(error); app.exit(1) })
}
