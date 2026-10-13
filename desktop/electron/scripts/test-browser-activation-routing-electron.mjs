import assert from 'node:assert/strict'
import { spawn, spawnSync } from 'node:child_process'
import { createServer } from 'node:http'
import { randomUUID } from 'node:crypto'
import { mkdtemp, rm } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

const wait = ms => new Promise(resolve => setTimeout(resolve, ms))
async function until(predicate, label, timeout = 8000) {
  const deadline = Date.now() + timeout
  while (Date.now() < deadline) {
    if (await predicate()) return
    await wait(40)
  }
  throw new Error(`Timed out waiting for ${label}`)
}

function pdfFixture(marker) {
  const stream = `BT /F1 24 Tf 72 720 Td (${marker}) Tj ET`
  const objects = [
    '<< /Type /Catalog /Pages 2 0 R >>',
    '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
    '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    `<< /Length ${Buffer.byteLength(stream)} >>\nstream\n${stream}\nendstream`,
  ]
  let body = '%PDF-1.4\n'
  const offsets = [0]
  for (const [index, object] of objects.entries()) {
    offsets.push(Buffer.byteLength(body))
    body += `${index + 1} 0 obj\n${object}\nendobj\n`
  }
  const xref = Buffer.byteLength(body)
  body += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n`
  for (const offset of offsets.slice(1)) body += `${String(offset).padStart(10, '0')} 00000 n \n`
  body += `trailer\n<< /Root 1 0 R /Size ${objects.length + 1} >>\nstartxref\n${xref}\n%%EOF\n`
  return Buffer.from(body, 'ascii')
}

if (!process.versions.electron) {
  if (process.platform === 'linux' && !process.env.DISPLAY && !process.env.WAYLAND_DISPLAY
    && process.env.OPENSQUILLA_ACTIVATION_UNDER_XVFB !== '1') {
    const display = spawnSync('xvfb-run', ['-a', process.execPath, fileURLToPath(import.meta.url)], {
      env: { ...process.env, OPENSQUILLA_ACTIVATION_UNDER_XVFB: '1' }, stdio: 'inherit',
    })
    if (display.error) throw display.error
    process.exit(display.status ?? 1)
  }
  const profile = await mkdtemp(join(tmpdir(), 'opensquilla-browser-activation-'))
  const require = createRequire(import.meta.url)
  const child = spawn(require('electron'), [
    ...(process.platform === 'linux' && process.getuid?.() === 0 ? ['--no-sandbox'] : []),
    `--user-data-dir=${join(profile, 'chromium')}`, fileURLToPath(import.meta.url),
  ], { env: { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: 'true' }, stdio: 'inherit' })
  const watchdog = setTimeout(() => child.kill('SIGKILL'), 90_000)
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
  const { app, BrowserWindow, dialog, webContents } = await import('electron')
  const { NativeWorkbenchSurfaceManager } = await import('../dist/native-workbench-surface.js')
  app.commandLine.appendSwitch('disable-gpu')
  app.on('window-all-closed', () => {})
  void (async () => {
    await app.whenReady()
    const wanted = pdfFixture('Requested export')
    const preview = pdfFixture('Unrelated preview')
    let assetVersion = 1
    const assetHeaders = []
    let mainRequests = 0
    let postRequests = 0
    let heldRequests = 0
    const server = createServer((request, response) => {
      if (request.url === '/cached.js') {
        assetHeaders.push(request.headers)
        response.writeHead(200, { 'content-type': 'application/javascript', 'cache-control': 'public,max-age=3600' })
        response.end(`window.assetVersion=${assetVersion}`)
        return
      }
      if (request.url === '/form-export') {
        assert.equal(request.method, 'POST')
        postRequests += 1
        let body = ''
        request.on('data', chunk => { body += chunk })
        request.on('end', () => {
          assert.equal(body, 'export=synthetic')
          response.writeHead(302, { location: '/wanted.pdf' }); response.end()
        })
        return
      }
      if (request.url === '/wanted.pdf' || request.url === '/preview.pdf') {
        const pdf = request.url === '/wanted.pdf' ? wanted : preview
        response.writeHead(200, { 'content-type': 'application/pdf', 'content-length': pdf.length })
        response.end(pdf)
        return
      }
      if (request.url === '/landing') {
        response.writeHead(200, { 'content-type': 'text/html' })
        response.end('<!doctype html><script>location.href="/wanted.pdf"</script>')
        return
      }
      if (request.url === '/held') { heldRequests += 1; return }
      response.writeHead(200, { 'content-type': 'text/html', 'cache-control': 'no-store' })
      if (request.url === '/') {
        mainRequests += 1
        response.end(`<!doctype html><title>Activation routing</title><script src="/cached.js"></script>
          <button onclick="window.open('/child','existing')">Open named tab</button>
          <a href="/wanted.pdf" target="existing">Export to named tab</a>
          <a href="/landing" target="existing">Export through landing page</a>
          <button onclick="window.open('/wanted.pdf','existing')">Export with script to named tab</button>
          <a href="/wanted.pdf" target="wrong-name" onclick="this.target='existing'">Export with changed target</a>
          <a href="/wanted.pdf" target="existing" onclick="event.preventDefault()">Cancel named export</a>
          <form method="post" action="/form-export" target="existing"><input type="hidden" name="export" value="synthetic"><button>Export form to named tab</button></form>
          <button onclick="const f=document.createElement('iframe');f.src='/preview.pdf';document.body.append(f);setTimeout(()=>{const a=document.createElement('a');a.href='/wanted.pdf';a.download='wanted.pdf';document.body.append(a);a.click()},300)">Export after preview</button>
          <a href="/wanted.pdf" target="wrong-name" id="late-link">Export with late window listener</a>
          <form id="script-form" method="post" action="/form-export" target="existing"><input type="hidden" name="export" value="synthetic"></form>
          <button onclick="document.querySelector('#script-form').submit()">Export with script form submit</button>
          <script>window.addEventListener('click', event => {
            if (event.target.id === 'late-link') event.target.target = 'existing';
          })</script>
          <input id="entry"><button onclick="window.onbeforeunload=()=>true">Protect page</button>`)
      } else response.end('<!doctype html><title>Existing child</title><p>Existing child</p>')
    })
    let owner, manager
    let exitCode = 0
    const originalDialog = dialog.showMessageBoxSync
    try {
      await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
      const origin = `http://127.0.0.1:${server.address().port}`
      owner = new BrowserWindow({ show: true, width: 1000, height: 800,
        webPreferences: { sandbox: true, contextIsolation: true, nodeIntegration: false } })
      await owner.loadURL('data:text/html,<title>Activation host</title>')
      manager = new NativeWorkbenchSurfaceManager({ getWindow: () => owner, emit() {} })
      const call = request => manager.executeBrowserMcp({ sessionKey: 'activation-task',
        observationMode: 'dom', ...request }, AbortSignal.timeout(8000))
      const record = target => [...manager.surfaces.values()].find(value => value.targetRef === target.targetRef)
      const parent = await call({ operation: 'open', url: origin + '/' })
      const parentRecord = record(parent)
      const contents = parentRecord.contents
      const click = async (name, action = 'click') => {
        console.log('Activation case:', name)
        const refs = (await call({ operation: 'observe', targetRef: parent.targetRef })).observation.refs
        const ref = refs.find(value => value.name === name)?.ref
        assert.ok(ref, name)
        return await call({ operation: 'act', targetRef: parent.targetRef, action, ref })
      }
      const assertWanted = async result => {
        assert.equal(result.download.state, 'completed')
        const inspected = await call({ operation: 'snapshot', targetRef: parent.targetRef,
          downloadId: result.download.downloadId, exportPdf: true })
        assert.equal(Buffer.from(inspected.pdfExport.dataBase64, 'base64').compare(wanted), 0,
          'Capture must contain the requested export, not the inline preview')
      }
      await click('Open named tab')
      await until(() => [...manager.surfaces.values()].some(value =>
        value.openerTargetRef === parent.targetRef && value.initialDocumentCommitted), 'named related tab')
      const child = [...manager.surfaces.values()].find(value => value.openerTargetRef === parent.targetRef)
      assert.equal(child.contents.mainFrame.name, 'existing')
      await assertWanted(await click('Export to named tab', 'download'))
      assert.equal(child.contents.getURL(), origin + '/child', 'Downloaded named navigation retains the old document')
      assert.equal(child.browserDownloadParent, undefined, 'The attempt must revoke the related tab grant')
      await assertWanted(await click('Export form to named tab', 'download'))
      assert.equal(postRequests, 1, 'The form must use its actual POST and redirect')
      await assertWanted(await click('Export with script to named tab', 'download'))
      assert.equal(child.browserDownloadParent, undefined)
      await assertWanted(await click('Export with changed target', 'download'))
      assert.equal(child.browserDownloadParent, undefined)
      await assertWanted(await click('Export with late window listener', 'download'))
      assert.equal(child.browserDownloadParent, undefined)
      await assertWanted(await click('Export with script form submit', 'download'))
      assert.equal(postRequests, 2)
      assert.equal(child.browserDownloadParent, undefined)
      await assertWanted(await click('Export through landing page', 'download'))
      assert.equal(child.browserDownloadParent, undefined)
      const cancelledRefs = (await call({ operation: 'observe', targetRef: parent.targetRef })).observation.refs
      await assert.rejects(manager.executeBrowserMcp({ sessionKey: 'activation-task', operation: 'act',
        targetRef: parent.targetRef, action: 'download', ref: cancelledRefs.find(value => value.name === 'Cancel named export').ref },
        AbortSignal.timeout(300)), error => error.code === 'TIMEOUT')
      assert.equal(child.browserDownloadParent, undefined, 'Cancelling must revoke any named target capture')
      assert.equal(parentRecord.browserDownloadAttempt, undefined)
      const foreign = await manager.executeBrowserMcp({ sessionKey: 'different-task', operation: 'open', url: origin + '/child' }, AbortSignal.timeout(8000))
      const foreignRecord = record(foreign)
      await foreignRecord.contents.executeJavaScript("window.name='existing'")
      await assertWanted(await click('Export to named tab', 'download'))
      assert.equal(foreignRecord.browserDownloadParent, undefined, 'A same-name foreign task cannot receive the grant')
      assert.equal(foreignRecord.contents.getURL(), origin + '/child')
      await manager.destroySurface(foreignRecord.id, true)
      await contents.executeJavaScript("window.open('/','second-parent');void 0")
      await until(() => [...manager.surfaces.values()].some(value =>
        value.contents.mainFrame.name === 'second-parent' && value.browserDocumentReady), 'second related parent')
      const secondParent = [...manager.surfaces.values()].find(value => value.contents.mainFrame.name === 'second-parent')
      const downloadOwner = value => ({ sessionKey: value.scopeId, targetRef: value.targetRef, webContentsId: value.webContentsId })
      const firstOwner = downloadOwner(parentRecord)
      const secondOwner = downloadOwner(secondParent)
      const firstCapture = await manager.browserDownloads.arm(firstOwner, AbortSignal.timeout(8000))
      const secondCapture = await manager.browserDownloads.arm(secondOwner, AbortSignal.timeout(8000))
      parentRecord.browserDownloadAttempt = randomUUID()
      secondParent.browserDownloadAttempt = randomUUID()
      try {
        await contents.executeJavaScript("window.open('/held','existing');void 0")
        await until(() => heldRequests === 1 && child.browserDownloadParent?.targetRef === parent.targetRef,
          'first request owns the reused destination')
        await secondParent.contents.executeJavaScript("window.open('/wanted.pdf','existing');void 0")
        const secondResult = await secondCapture.completed
        assert.equal(child.browserDownloadParent?.targetRef, secondParent.targetRef,
          'The latest canonical initiator must replace the earlier parent grant')
        assert.equal(manager.browserDownloads.isArmed(firstOwner), true,
          'The earlier parent must not capture the later parent request')
        const exported = await manager.browserDownloads.exportPdf(secondOwner, secondResult.downloadId)
        assert.equal(Buffer.from(exported.dataBase64, 'base64').compare(wanted), 0)
      } finally {
        firstCapture.cancel(); secondCapture.cancel()
        parentRecord.browserDownloadAttempt = undefined
        secondParent.browserDownloadAttempt = undefined
        child.browserDownloadParent = undefined
        await manager.destroySurface(secondParent.id, true)
      }
      await assertWanted(await click('Export after preview', 'download'))
      const frames = frame => [frame, ...frame.frames.flatMap(frames)]
      await until(() => frames(contents.mainFrame).some(frame =>
        frame.url === 'chrome-extension://mhjfbmdgcfjbbpaeojofohoefgiehjai/index.html'), 'inline preview viewer')
      assert.equal(parentRecord.browserDownloadAttempt, undefined)
      assert.equal(manager.surfaces.size, 2, 'Existing related tab is retained')

      assert.equal(manager.setSurfaceRect({ surfaceId: parentRecord.id, x: 0, y: 0,
        width: 900, height: 650, visible: true }).ok, true)
      manager.activateSurface(parentRecord.id)
      app.focus({ steal: true })
      owner.focus()
      contents.focus()
      await until(() => webContents.getFocusedWebContents() === contents, 'browser focus').catch(error => {console.log('focus diagnostic', webContents.getFocusedWebContents()?.id, webContents.getFocusedWebContents()?.getURL(), webContents.getFocusedWebContents()?.hostWebContents?.id, contents.id, owner.webContents.id, contents.isFocused(), owner.isFocused()); throw error})
      let hostLoads = 0
      owner.webContents.on('did-finish-load', () => { hostLoads += 1 })
      assert.equal(await contents.executeJavaScript('window.assetVersion'), 1)
      assetVersion = 2
      let previousRequests = mainRequests
      assert.equal(manager.reloadFocusedBrowser(owner, false), true)
      await until(() => mainRequests > previousRequests && parentRecord.browserDocumentReady, 'focused normal reload')
      assert.equal(await contents.executeJavaScript('window.assetVersion'), 2)
      const normalHeaders = assetHeaders.at(-1)
      assert.notEqual(normalHeaders['cache-control'], 'no-cache', 'Normal reload keeps cache revalidation semantics')
      previousRequests = mainRequests
      assert.equal(manager.reloadFocusedBrowser(owner, true), true)
      await until(() => mainRequests > previousRequests && parentRecord.browserDocumentReady, 'focused hard reload')
      assert.equal(await contents.executeJavaScript('window.assetVersion'), 2)
      assert.equal(assetHeaders.at(-1)['cache-control'], 'no-cache', 'Hard reload must bypass the HTTP cache')
      assetVersion = 3
      previousRequests = mainRequests
      contents.sendInputEvent({ type: 'keyDown', keyCode: 'R', modifiers: ['shift', process.platform === 'darwin' ? 'meta' : 'control'] })
      await until(() => mainRequests > previousRequests && parentRecord.browserDocumentReady, 'hard reload shortcut')
      assert.equal(await contents.executeJavaScript('window.assetVersion'), 3)
      if (process.platform !== 'darwin') {
        assetVersion = 4
        previousRequests = mainRequests
        contents.sendInputEvent({ type: 'keyDown', keyCode: 'F5', modifiers: ['control'] })
        await until(() => mainRequests > previousRequests && parentRecord.browserDocumentReady, 'Ctrl F5 hard reload')
        assert.equal(await contents.executeJavaScript('window.assetVersion'), 4)
      }
      await click('Protect page')
      await contents.executeJavaScript("document.querySelector('#entry').value='unsaved synthetic'")
      let decisions = 0
      dialog.showMessageBoxSync = () => { decisions += 1; return 0 }
      previousRequests = mainRequests
      assert.equal(manager.reloadFocusedBrowser(owner, true), true)
      await until(() => decisions === 1 && !parentRecord.browserNavigationPromise, 'Stay on focused hard reload')
      assert.equal(mainRequests, previousRequests, 'Stay must prevent the request')
      assert.equal(await contents.executeJavaScript("document.querySelector('#entry').value"), 'unsaved synthetic')
      assert.equal(hostLoads, 0, 'Focused browser reload must not reload the workbench')
      assert.equal(manager.surfaces.size, 2, 'Focused reload must not close other tabs')
      owner.webContents.focus()
      await until(() => webContents.getFocusedWebContents() === owner.webContents, 'workbench focus')
      assert.equal(manager.reloadFocusedBrowser(owner, false), false, 'Host reload falls back to its close guard')
      console.log('Browser activation, named target download and focused reload checks passed')
    } catch (error) {
      console.error(error); exitCode = 1
    } finally {
      dialog.showMessageBoxSync = originalDialog
      if (manager) await manager.destroyAll()
      if (owner && !owner.isDestroyed()) owner.destroy()
      server.closeAllConnections()
      await new Promise(resolve => server.close(resolve))
      app.exit(exitCode)
    }
  })()
}
