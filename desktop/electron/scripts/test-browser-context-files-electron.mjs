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
    && process.env.OPENSQUILLA_CONTEXT_FILES_UNDER_XVFB !== '1') {
    const display = spawnSync('xvfb-run', ['-a', process.execPath, fileURLToPath(import.meta.url)], {
      env: { ...process.env, OPENSQUILLA_CONTEXT_FILES_UNDER_XVFB: '1' }, stdio: 'inherit',
    })
    if (display.error) throw display.error
    process.exit(display.status ?? 1)
  }
  const root = await mkdtemp(join(tmpdir(), 'opensquilla-context-files-'))
  const require = createRequire(import.meta.url)
  const child = spawn(require('electron'), [
    ...(process.platform === 'linux' && process.getuid?.() === 0 ? ['--no-sandbox'] : []),
    `--user-data-dir=${join(root, 'chromium')}`, fileURLToPath(import.meta.url),
  ], { env: { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: 'true' }, stdio: 'inherit' })
  const watchdog = setTimeout(() => child.kill('SIGKILL'), 60_000)
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
  const { app, BrowserWindow } = await import('electron')
  const { NativeWorkbenchSurfaceManager } = await import('../dist/native-workbench-surface.js')
  app.commandLine.appendSwitch('disable-gpu')
  app.on('window-all-closed', () => {})
  void (async () => {
    await app.whenReady()
    let manager, owner
    let exitCode = 0
    const text = 'Synthetic download\n\nPreserved paragraph — 文字\n'
    const web = createServer((request, response) => {
      if (request.url === '/asset') {
        response.writeHead(200, { 'content-type': 'text/plain; charset=utf-8',
          'content-disposition': 'attachment; filename="synthetic-note.txt"' })
        response.end(text)
        return
      }
      response.writeHead(200, { 'content-type': 'text/html; charset=utf-8' })
      response.end(`<!doctype html><title>Context and files</title>
        <a href="/asset" download>Download sample</a>
        <a href="/asset" download onclick="return confirm('Confirm synthetic download?')">Confirm download</a>
        <label>Upload sample<input type="file" aria-label="Upload sample"></label>
        <output id="upload-result">No uploaded file</output><script>
        window.channel = new BroadcastChannel('synthetic-channel');
        window.channel.onmessage = event => window.received = event.data;
        window.uploadCompletion = null;
        document.querySelector('input').onchange = event => {
          const file = event.target.files[0];
          window.uploadCompletion = (async () => {
            const rendered = file ? file.name + ': ' + await file.text() : 'No uploaded file';
            document.querySelector('output').textContent = rendered;
            return rendered;
          })();
        };
        </script>`)
    })
    try {
      await new Promise(resolve => web.listen(0, '127.0.0.1', resolve))
      owner = new BrowserWindow({ show: false, width: 900, height: 700,
        webPreferences: { sandbox: true, contextIsolation: true, nodeIntegration: false } })
      await owner.loadURL('data:text/html,<title>Browser context test host</title>')
      manager = new NativeWorkbenchSurfaceManager({ getWindow: () => owner, emit() {} })
      const origin = `http://127.0.0.1:${web.address().port}`
      const call = request => manager.executeBrowserMcp({ sessionKey: 'synthetic-task',
        observationMode: 'dom', ...request }, AbortSignal.timeout(15_000))
      const record = target => [...manager.surfaces.values()].find(value => value.targetRef === target.targetRef)
      const evaluate = (target, expression) => record(target).view.webContents.executeJavaScript(expression)
      const first = await call({ operation: 'open', url: origin })
      await evaluate(first, "localStorage.setItem('synthetic-session-value', 'retained')")
      const shared = await call({ operation: 'open', url: origin, contextTargetRef: first.targetRef })
      const isolated = await call({ operation: 'open', url: origin })
      assert.equal(record(first).previewSession, record(shared).previewSession)
      assert.notEqual(record(first).previewSession, record(isolated).previewSession)
      assert.equal(await evaluate(shared, "localStorage.getItem('synthetic-session-value')"), 'retained')
      assert.equal(await evaluate(isolated, "localStorage.getItem('synthetic-session-value')"), null)
      await evaluate(first, "window.channel.postMessage('same-context-message')")
      const messageDeadline = Date.now() + 1000
      while (!await evaluate(shared, 'window.received') && Date.now() < messageDeadline) {
        await new Promise(resolve => setTimeout(resolve, 20))
      }
      assert.equal(await evaluate(shared, 'window.received'), 'same-context-message')
      assert.equal(await evaluate(isolated, 'window.received'), undefined)
      const count = manager.surfaces.size
      await assert.rejects(call({ sessionKey: 'another-task', operation: 'open', url: origin,
        contextTargetRef: first.targetRef }), error => error.code === 'TARGET_NOT_FOUND')
      assert.equal(manager.surfaces.size, count)
      await call({ operation: 'tab', tabAction: 'close', targetRef: first.targetRef })
      assert.equal(await evaluate(shared, "localStorage.getItem('synthetic-session-value')"), 'retained')
      await assert.rejects(call({ operation: 'open', url: origin, contextTargetRef: first.targetRef }),
        error => error.code === 'TARGET_NOT_FOUND')

      let saveOptions
      record(shared).previewSession.emit('will-download', { preventDefault() { assert.fail('User download should remain available') } }, {
        hasUserGesture: () => true, getURL: () => origin + '/asset',
        setSaveDialogOptions: options => { saveOptions = options },
      }, record(shared).view.webContents)
      assert.equal(saveOptions.title, 'Save preview download', 'Ordinary downloads retain the native confirmation path')

      let observed = await call({ operation: 'observe', targetRef: shared.targetRef })
      let ref = observed.observation.refs.find(value => value.name === 'Download sample').ref
      const downloaded = await call({ operation: 'act', targetRef: shared.targetRef, action: 'download', ref })
      assert.equal(downloaded.download.name, 'synthetic-note.txt')
      const inspected = await call({ operation: 'snapshot', targetRef: shared.targetRef,
        downloadId: downloaded.download.downloadId })
      assert.equal(inspected.download.text, text)
      await assert.rejects(call({ operation: 'snapshot', targetRef: isolated.targetRef,
        downloadId: downloaded.download.downloadId }), error => error.code === 'DOWNLOAD_NOT_FOUND')

      observed = await call({ operation: 'observe', targetRef: shared.targetRef })
      ref = observed.observation.refs.find(value => value.name === 'Confirm download').ref
      const started = Date.now()
      const blocked = await call({ operation: 'act', targetRef: shared.targetRef, action: 'download', ref })
      assert.ok(Date.now() - started < 5000, 'Report the dialog promptly, without waiting for the 15-second request deadline')
      assert.equal(blocked.execution.state, 'blocked')
      assert.equal(blocked.download.state, 'not_captured')
      assert.equal(blocked.download.managedCapture, false)
      assert.equal(blocked.download.downloadId, undefined)
      const pendingDialog = blocked.observation.browserState.dialogs.pending[0]
      assert.equal(pendingDialog.message, 'Confirm synthetic download?')
      saveOptions = undefined
      record(shared).previewSession.emit('will-download', { preventDefault() { assert.fail('Unrelated download must not be cancelled') } }, {
        hasUserGesture: () => true, getURL: () => origin + '/asset',
        setSaveDialogOptions: options => { saveOptions = options },
      }, record(shared).view.webContents)
      assert.equal(saveOptions.title, 'Save preview download', 'No stale capture consumes a later download')
      await call({ operation: 'dialog', targetRef: shared.targetRef, dialogId: pendingDialog.id, accept: false })

      observed = await call({ operation: 'observe', targetRef: shared.targetRef })
      ref = observed.observation.refs.find(value => value.name === 'Upload sample').ref
      const opened = await call({ operation: 'act', targetRef: shared.targetRef, action: 'click', ref })
      const pending = opened.observation.browserState.fileChoosers.pending[0]
      assert.ok(pending.chooserId)
      const tabs = await call({ operation: 'list' })
      assert.equal(tabs.targets.find(value => value.targetRef === shared.targetRef).hostBlockers[0].kind, 'file_chooser')
      const file = { fileId: 'synthetic-file-id', name: 'synthetic-upload.txt', mimeType: 'text/plain',
        dataBase64: Buffer.from('Uploaded synthetic contents').toString('base64') }
      const uploaded = await call({ operation: 'act', targetRef: shared.targetRef, action: 'upload', chooserId: pending.chooserId,
        fileId: file.fileId, uploadFile: file })
      assert.equal(uploaded.performed, true)
      assert.equal(uploaded.uploaded.name, file.name)
      assert.equal(await evaluate(shared, 'document.querySelector("input").files[0].name'), file.name)
      assert.equal(await evaluate(shared, 'Boolean(window.uploadCompletion)'), true, 'Upload dispatched the fixture change handler')
      // Selecting the file completes before the page's asynchronous File.text()
      // rendering. Await that fixture-owned work instead of assuming one frame.
      assert.equal(await evaluate(shared, 'window.uploadCompletion'), file.name + ': Uploaded synthetic contents')
      assert.match(await evaluate(shared, "document.querySelector('output').textContent"), /Uploaded synthetic contents/)
      observed = await call({ operation: 'observe', targetRef: shared.targetRef })
      ref = observed.observation.refs.find(value => value.name === 'Upload sample').ref
      const reopened = await call({ operation: 'act', targetRef: shared.targetRef, action: 'click', ref })
      const chooserId = reopened.observation.browserState.fileChoosers.pending[0].chooserId
      await call({ operation: 'act', targetRef: shared.targetRef, action: 'cancelUpload', chooserId })
      assert.equal(record(shared).playwright.pendingFileChooser, undefined)
      assert.equal(await evaluate(shared, 'document.querySelector("input").files[0].name'), file.name)
      console.log('Browser context and files passed: shared storage and BroadcastChannel, isolation, lifecycle, managed download and intercepted upload.')
    } catch (error) {
      exitCode = 1
      console.error(error)
    } finally {
      const shutdown = setTimeout(() => app.exit(exitCode), 3000)
      shutdown.unref()
      await manager?.destroyAll().catch(() => {})
      if (owner && !owner.isDestroyed()) owner.destroy()
      web.closeAllConnections()
      await new Promise(resolve => web.close(resolve))
      app.exit(exitCode)
    }
  })()
}
