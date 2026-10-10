import assert from 'node:assert/strict'
import { spawn, spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { createServer } from 'node:http'
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

function pdfFixture() {
  const stream = 'BT /F1 18 Tf 72 720 Td (Synthetic managed download) Tj ET'
  const objects = [
    '<< /Type /Catalog /Pages 2 0 R >>',
    '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
    '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    `<< /Length ${Buffer.byteLength(stream)} >>\nstream\n${stream}\nendstream`,
  ]
  let body = '%PDF-1.4\n'
  const offsets = []
  for (const [index, object] of objects.entries()) {
    offsets.push(Buffer.byteLength(body))
    body += `${index + 1} 0 obj\n${object}\nendobj\n`
  }
  const xref = Buffer.byteLength(body)
  body += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n`
  for (const offset of offsets) body += `${String(offset).padStart(10, '0')} 00000 n \n`
  body += `trailer\n<< /Root 1 0 R /Size ${objects.length + 1} >>\nstartxref\n${xref}\n%%EOF\n`
  return Buffer.from(body, 'ascii')
}

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
  const { DesktopBrowserMcp } = await import('../dist/desktop-browser-mcp.js')
  const { NativeWorkbenchSurfaceManager } = await import('../dist/native-workbench-surface.js')
  app.commandLine.appendSwitch('disable-gpu')
  app.on('window-all-closed', () => {})
  void (async () => {
    await app.whenReady()
    const trace = step => { if (process.env.OPENSQUILLA_CONTEXT_TRACE === '1') console.error(`[context-files] ${step}`) }
    let manager, owner, userDownloadDirectory
    let exitCode = 0
    const text = 'Synthetic download\n\nPreserved paragraph — 文字\n'
    const pdfBytes = pdfFixture()
    const pdfRequests = []
    const web = createServer((request, response) => {
      const authenticated = String(request.headers.cookie ?? '').split(';')
        .some(cookie => cookie.trim() === 'synthetic-pdf-auth=granted')
      if (request.url?.startsWith('/pdf/') || request.url === '/asset.pdf') {
        pdfRequests.push({ url: request.url, referer: request.headers.referer })
        if (!authenticated) {
          response.writeHead(403, { 'content-type': 'text/plain; charset=utf-8' })
          response.end('Authentication required')
          return
        }
      }
      if (['/pdf/referrer-paper', '/pdf/rewritten-paper'].includes(request.url)) {
        const referer = request.headers.referer
        if (!referer || new URL(referer).pathname !== '/') {
          response.writeHead(403, { 'content-type': 'text/plain' })
          response.end('The actual source page is required')
          return
        }
      }
      if (request.url?.startsWith('/pdf/')) {
        response.writeHead(200, { 'content-type': 'application/pdf', 'content-length': pdfBytes.length })
        response.end(pdfBytes)
        return
      }
      if (request.url === '/asset.pdf') {
        response.writeHead(200, { 'content-type': 'application/pdf',
          'content-disposition': 'attachment; filename="synthetic-paper.pdf"',
          'content-length': pdfBytes.length })
        response.end(pdfBytes)
        return
      }
      if (request.url === '/asset') {
        response.writeHead(200, { 'content-type': 'text/plain; charset=utf-8',
          'content-disposition': 'attachment; filename="synthetic-note.txt"' })
        response.end(text)
        return
      }
      response.writeHead(200, { 'content-type': 'text/html; charset=utf-8' })
      response.end(`<!doctype html><title>Context and files</title>
        <a href="/asset" download>Download sample</a>
        <a href="/asset">Native attachment navigation</a>
        <a href="/asset.pdf" download>Download PDF sample</a>
        <a href="/pdf/synthetic-paper" target="_blank">Download inline PDF</a>
        <a id="generated-pdf" download="synthetic-generated.pdf">Download generated PDF</a>
        <a href="/pdf/referrer-paper" target="_blank">Download referrer PDF</a>
        <a id="listener-confirm" href="/asset.pdf">Listener confirm PDF</a>
        <a id="delegated-confirm" href="/asset.pdf">Delegated confirm PDF</a>
        <a id="rewrite-pdf" href="/original.pdf" target="_blank">Rewrite PDF link</a>
        <a id="delegate-rewrite" href="/original.pdf">Rewrite PDF navigation</a>
        <a href="/asset" download onclick="return confirm('Confirm synthetic download?')">Confirm download</a>
        <label>Upload sample<input type="file" aria-label="Upload sample"></label>
        <output id="upload-result">No uploaded file</output><script>
        window.channel = new BroadcastChannel('synthetic-channel');
        window.channel.onmessage = event => window.received = event.data;
        window.uploadCompletion = null;
        document.querySelector('#generated-pdf').href = URL.createObjectURL(new Blob([
          Uint8Array.from(atob('${pdfBytes.toString('base64')}'), character => character.charCodeAt(0))
        ], { type: 'application/pdf' }));
        window.listenerConfirmCount = 0;
        window.delegatedConfirmCount = 0;
        window.rewritePdfCount = 0;
        window.delegateRewriteCount = 0;
        document.querySelector('#listener-confirm').addEventListener('click', event => {
          event.preventDefault(); window.listenerConfirmCount++;
          confirm('Listener PDF confirmation');
        });
        document.querySelector('#rewrite-pdf').addEventListener('click', event => {
          window.rewritePdfCount++; event.currentTarget.href = '/pdf/rewritten-paper';
        });
        document.addEventListener('click', event => {
          const link = event.target.closest('a');
          if (link?.id === 'delegated-confirm') {
            event.preventDefault(); window.delegatedConfirmCount++;
            confirm('Delegated PDF confirmation');
          }
          if (link?.id === 'delegate-rewrite') {
            event.preventDefault(); window.delegateRewriteCount++;
            location.assign('/pdf/rewritten-paper');
          }
        });
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
      const blank = manager.allocateSurface({ version: 4, surfaceId: 'aboutblank-download-boundary',
        kind: 'url-preview', payload: { url: 'about:blank', scopeId: 'synthetic-task' } }, owner)
      await blank.contents.loadURL('about:blank')
      blank.browserPreviousDocument = { url: 'about:blank', ready: false, stopped: false }
      manager.restoreBrowserDocumentAfterAbortedNavigation(blank)
      assert.equal(blank.documentUrl, 'about:blank', 'An uncommitted blank download popup retains a valid document URL')
      await manager.destroySurface(blank.id, true)
      const first = await call({ operation: 'open', url: origin })
      trace('first page opened')
      await evaluate(first, "localStorage.setItem('synthetic-session-value', 'retained'); document.cookie = 'synthetic-pdf-auth=granted; SameSite=Lax'")
      const shared = await call({ operation: 'open', url: origin, contextTargetRef: first.targetRef })
      trace('shared page opened')
      const isolated = await call({ operation: 'open', url: origin })
      trace('isolated page opened')
      assert.equal(record(first).previewSession, record(shared).previewSession)
      assert.notEqual(record(first).previewSession, record(isolated).previewSession)
      assert.equal(await evaluate(shared, "localStorage.getItem('synthetic-session-value')"), 'retained')
      assert.equal(await evaluate(isolated, "localStorage.getItem('synthetic-session-value')"), null)
      assert.match(await evaluate(shared, 'document.cookie'), /synthetic-pdf-auth=granted/)
      assert.doesNotMatch(await evaluate(isolated, 'document.cookie'), /synthetic-pdf-auth=granted/)
      trace('storage isolation checked')
      await evaluate(first, "window.channel.postMessage('same-context-message')")
      const messageDeadline = Date.now() + 1000
      while (!await evaluate(shared, 'window.received') && Date.now() < messageDeadline) {
        await new Promise(resolve => setTimeout(resolve, 20))
      }
      assert.equal(await evaluate(shared, 'window.received'), 'same-context-message')
      assert.equal(await evaluate(isolated, 'window.received'), undefined)
      trace('broadcast isolation checked')
      const count = manager.surfaces.size
      await assert.rejects(call({ sessionKey: 'another-task', operation: 'open', url: origin,
        contextTargetRef: first.targetRef }), error => error.code === 'TARGET_NOT_FOUND')
      trace('cross-task context denied')
      assert.equal(manager.surfaces.size, count)
      trace('closing first page')
      await call({ operation: 'tab', tabAction: 'close', targetRef: first.targetRef })
      trace('first page closed')
      assert.equal(await evaluate(shared, "localStorage.getItem('synthetic-session-value')"), 'retained')
      await assert.rejects(call({ operation: 'open', url: origin, contextTargetRef: first.targetRef }),
        error => error.code === 'TARGET_NOT_FOUND')

      let saveOptions
      record(shared).previewSession.emit('will-download', { preventDefault() { assert.fail('User download should remain available') } }, {
        hasUserGesture: () => true, getURL: () => origin + '/asset',
        setSaveDialogOptions: options => { saveOptions = options },
      }, record(shared).view.webContents)
      assert.equal(saveOptions.title, 'Save preview download', 'Ordinary downloads retain the native confirmation path')

      userDownloadDirectory = await mkdtemp(join(tmpdir(), 'opensquilla-native-user-download-'))
      const userDownloadPath = join(userDownloadDirectory, 'synthetic-note.txt')
      const userSaved = new Promise((resolve, reject) => {
        record(shared).previewSession.once('will-download', (_event, item) => {
          item.setSavePath(userDownloadPath)
          item.once('done', (_event, state) => state === 'completed' ? resolve() : reject(new Error(state)))
        })
      })
      let nativeObserved = await call({ operation: 'observe', targetRef: shared.targetRef })
      const nativeRef = nativeObserved.observation.refs.find(value => value.name === 'Native attachment navigation').ref
      await call({ operation: 'act', targetRef: shared.targetRef, action: 'click', ref: nativeRef })
      await userSaved
      assert.equal(await readFile(userDownloadPath, 'utf8'), text)
      assert.equal(record(shared).contents.getURL(), origin + '/')
      assert.equal(record(shared).browserDocumentReady, true, 'A user attachment download retains the live page')
      nativeObserved = await call({ operation: 'observe', targetRef: shared.targetRef })
      assert.ok(nativeObserved.observation.refs.some(value => value.name === 'Download sample'))
      trace('ordinary attachment navigation retains document readiness')

      let observed = await call({ operation: 'observe', targetRef: shared.targetRef })
      let ref = observed.observation.refs.find(value => value.name === 'Download sample').ref
      const downloaded = await call({ operation: 'act', targetRef: shared.targetRef, action: 'download', ref })
      trace('text downloaded')
      assert.equal(downloaded.download.name, 'synthetic-note.txt')
      const inspected = await call({ operation: 'snapshot', targetRef: shared.targetRef,
        downloadId: downloaded.download.downloadId })
      assert.equal(inspected.download.text, text)
      await assert.rejects(call({ operation: 'snapshot', targetRef: isolated.targetRef,
        downloadId: downloaded.download.downloadId }), error => error.code === 'DOWNLOAD_NOT_FOUND')

      observed = await call({ operation: 'observe', targetRef: shared.targetRef })
      ref = observed.observation.refs.find(value => value.name === 'Download PDF sample').ref
      const pdfDownload = await call({ operation: 'act', targetRef: shared.targetRef, action: 'download', ref })
      trace('PDF downloaded')
      assert.equal(pdfDownload.download.name, 'synthetic-paper.pdf')
      const mcp = new DesktopBrowserMcp((request, signal) => manager.executeBrowserMcp(request, signal))
      const pdfResponse = await mcp.handle({ jsonrpc: '2.0', id: 1, method: 'tools/call', params: {
        name: 'browser_inspect', arguments: {
          targetRef: shared.targetRef, downloadId: pdfDownload.download.downloadId,
        }, _meta: { sessionKey: 'synthetic-task', operationId: 'synthetic-pdf-inspect', exportPdf: true },
      } }, AbortSignal.timeout(15_000))
      trace('PDF private export returned')
      assert.equal(pdfResponse.result.isError, false, JSON.stringify(pdfResponse.result.structuredContent))
      const privateExport = pdfResponse.result._meta['opensquilla/pdfExport']
      assert.equal(privateExport.downloadId, pdfDownload.download.downloadId)
      assert.equal(privateExport.mimeType, 'application/pdf')
      assert.equal(privateExport.byteLength, pdfBytes.length)
      assert.equal(privateExport.sha256, createHash('sha256').update(pdfBytes).digest('hex'))
      assert.equal(Buffer.from(privateExport.dataBase64, 'base64').compare(pdfBytes), 0)
      assert.equal(JSON.stringify(pdfResponse.result.content).includes(privateExport.dataBase64), false)
      assert.equal(JSON.stringify(pdfResponse.result.structuredContent).includes(privateExport.dataBase64), false)
      assert.equal('pdfExport' in pdfResponse.result.structuredContent, false)

      const otherPagePdf = await mcp.handle({ jsonrpc: '2.0', id: 2, method: 'tools/call', params: {
        name: 'browser_inspect', arguments: {
          targetRef: isolated.targetRef, downloadId: pdfDownload.download.downloadId,
        }, _meta: { sessionKey: 'synthetic-task', operationId: 'synthetic-other-page-pdf', exportPdf: true },
      } }, AbortSignal.timeout(15_000))
      assert.equal(otherPagePdf.result.isError, true)
      assert.equal(otherPagePdf.result.structuredContent.code, 'DOWNLOAD_NOT_FOUND')
      assert.equal(otherPagePdf.result._meta, undefined)

      const textResponse = await mcp.handle({ jsonrpc: '2.0', id: 3, method: 'tools/call', params: {
        name: 'browser_inspect', arguments: {
          targetRef: shared.targetRef, downloadId: downloaded.download.downloadId,
        }, _meta: { sessionKey: 'synthetic-task', operationId: 'synthetic-text-inspect', exportPdf: true },
      } }, AbortSignal.timeout(15_000))
      assert.equal(textResponse.result.isError, false)
      assert.equal(textResponse.result._meta, undefined)
      assert.equal(textResponse.result.structuredContent.download.text, text)

      observed = await call({ operation: 'observe', targetRef: shared.targetRef })
      ref = observed.observation.refs.find(value => value.name === 'Download inline PDF').ref
      const inlinePdf = await call({ operation: 'act', targetRef: shared.targetRef, action: 'download', ref })
      trace('inline PDF downloaded')
      assert.equal(inlinePdf.download.state, 'completed', 'Inline PDF must be captured without a viewer navigation')
      assert.equal(inlinePdf.download.mimeType, 'application/pdf')
      const inlineRead = await mcp.handle({ jsonrpc: '2.0', id: 4, method: 'tools/call', params: {
        name: 'browser_inspect', arguments: {
          targetRef: shared.targetRef, downloadId: inlinePdf.download.downloadId,
        }, _meta: { sessionKey: 'synthetic-task', operationId: 'synthetic-inline-pdf', exportPdf: true },
      } }, AbortSignal.timeout(15_000))
      assert.equal(inlineRead.result.isError, false)
      assert.equal(inlineRead.result._meta['opensquilla/pdfExport'].sha256,
        createHash('sha256').update(pdfBytes).digest('hex'))

      observed = await call({ operation: 'observe', targetRef: shared.targetRef })
      ref = observed.observation.refs.find(value => value.name === 'Download generated PDF').ref
      const generated = await call({ operation: 'act', targetRef: shared.targetRef, action: 'download', ref })
      assert.equal(generated.download.state, 'completed')
      assert.equal(generated.download.name, 'synthetic-generated.pdf')
      assert.equal(generated.download.mimeType, 'application/pdf')
      const generatedRead = await mcp.handle({ jsonrpc: '2.0', id: 5, method: 'tools/call', params: {
        name: 'browser_inspect', arguments: {
          targetRef: shared.targetRef, downloadId: generated.download.downloadId,
        }, _meta: { sessionKey: 'synthetic-task', operationId: 'synthetic-generated-pdf', exportPdf: true },
      } }, AbortSignal.timeout(15_000))
      assert.equal(generatedRead.result.isError, false)
      const generatedExport = generatedRead.result._meta['opensquilla/pdfExport']
      assert.equal(generatedExport.downloadId, generated.download.downloadId)
      assert.equal(generatedExport.sha256, createHash('sha256').update(pdfBytes).digest('hex'))
      assert.equal(Buffer.from(generatedExport.dataBase64, 'base64').compare(pdfBytes), 0)
      assert.equal(JSON.stringify(generatedRead.result.structuredContent).includes(generatedExport.dataBase64), false)
      if (process.env.OPENSQUILLA_BLOB_PDF_EXPORT_PATH) {
        await writeFile(process.env.OPENSQUILLA_BLOB_PDF_EXPORT_PATH, JSON.stringify(generatedExport))
      }
      trace('generated Blob PDF download and private export checked')

      for (const [name, counter, message] of [
        ['Listener confirm PDF', 'listenerConfirmCount', 'Listener PDF confirmation'],
        ['Delegated confirm PDF', 'delegatedConfirmCount', 'Delegated PDF confirmation'],
      ]) {
        const requestCount = pdfRequests.length
        observed = await call({ operation: 'observe', targetRef: shared.targetRef })
        ref = observed.observation.refs.find(value => value.name === name).ref
        const blockedPdf = await call({ operation: 'act', targetRef: shared.targetRef, action: 'download', ref })
        assert.equal(blockedPdf.execution.state, 'blocked')
        assert.equal(blockedPdf.download.state, 'not_captured')
        const pending = record(shared).playwright.pendingDialog
        assert.equal(pending.message, message)
        assert.equal(pdfRequests.length, requestCount, 'A cancelled click must not issue the bare PDF request')
        await call({ operation: 'dialog', targetRef: shared.targetRef, dialogId: pending.id, accept: false })
        assert.equal(await evaluate(shared, `window.${counter}`), 1)
      }

      for (const [name, counter] of [
        ['Download referrer PDF', undefined],
        ['Rewrite PDF link', 'rewritePdfCount'],
        ['Rewrite PDF navigation', 'delegateRewriteCount'],
      ]) {
        const tabCount = manager.surfaces.size
        observed = await call({ operation: 'observe', targetRef: shared.targetRef })
        ref = observed.observation.refs.find(value => value.name === name).ref
        const naturalPdf = await call({ operation: 'act', targetRef: shared.targetRef, action: 'download', ref })
        assert.equal(naturalPdf.download.state, 'completed', name)
        assert.equal(naturalPdf.download.mimeType, 'application/pdf', name)
        if (counter) assert.equal(await evaluate(shared, `window.${counter}`), 1)
        assert.equal(manager.surfaces.size, tabCount, 'An uncommitted download-only popup must be released')
        const artifact = await call({ operation: 'snapshot', targetRef: shared.targetRef,
          downloadId: naturalPdf.download.downloadId, exportPdf: true })
        assert.equal(artifact.pdfExport.sha256, createHash('sha256').update(pdfBytes).digest('hex'))
        assert.equal(pdfRequests.at(-1).referer, origin + '/', 'Use the actual click referrer policy')
      }
      trace('PDF listener, delegated confirmation, rewritten URL and referrer checked')

      observed = await call({ operation: 'observe', targetRef: shared.targetRef })
      ref = observed.observation.refs.find(value => value.name === 'Confirm download').ref
      const started = Date.now()
      const blocked = await call({ operation: 'act', targetRef: shared.targetRef, action: 'download', ref })
      trace('confirm download blocked')
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
      trace('file uploaded')
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
      if (userDownloadDirectory) await rm(userDownloadDirectory, { recursive: true, force: true })
      app.exit(exitCode)
    }
  })()
}
