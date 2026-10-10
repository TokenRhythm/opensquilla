import assert from 'node:assert/strict'
import { spawn, spawnSync } from 'node:child_process'
import { createServer } from 'node:http'
import { mkdtemp, rm, writeFile } from 'node:fs/promises'
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

function pdfFixture() {
  const stream = 'BT /F1 24 Tf 72 720 Td (Synthetic manager PDF) Tj ET'
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
    && process.env.OPENSQUILLA_PDF_MANAGER_UNDER_XVFB !== '1') {
    const display = spawnSync('xvfb-run', ['-a', process.execPath, fileURLToPath(import.meta.url)], {
      env: { ...process.env, OPENSQUILLA_PDF_MANAGER_UNDER_XVFB: '1' }, stdio: 'inherit',
    })
    if (display.error) throw display.error
    process.exit(display.status ?? 1)
  }
  const profile = await mkdtemp(join(tmpdir(), 'opensquilla-browser-pdf-manager-'))
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
  const { app, BrowserWindow } = await import('electron')
  const { NativeWorkbenchSurfaceManager } = await import('../dist/native-workbench-surface.js')
  app.commandLine.appendSwitch('disable-gpu')
  app.on('window-all-closed', () => {})
  void (async () => {
    await app.whenReady()
    const pdf = pdfFixture()
    const server = createServer((request, response) => {
      if (request.url === '/document.pdf') {
        response.writeHead(200, { 'content-type': 'application/pdf', 'content-length': pdf.length })
        response.end(pdf)
        return
      }
      if (request.url === '/redirect') {
        response.writeHead(302, { location: '/document.pdf' })
        response.end()
        return
      }
      response.writeHead(200, { 'content-type': 'text/html; charset=utf-8' })
      if (request.url === '/embedded.html') {
        response.end('<!doctype html><title>Embedded</title><iframe src="/document.pdf"></iframe>')
      } else if (request.url === '/link.html') {
        response.end('<!doctype html><title>Link</title><a id="pdf" href="/document.pdf">PDF</a>')
      } else if (request.url === '/popup.html') {
        response.end('<!doctype html><title>Popup</title><button id="popup" onclick="window.open(\'/document.pdf\',\'_blank\')">PDF popup</button>')
      } else {
        response.writeHead(404)
        response.end()
      }
    })
    let owner, manager
    const events = []
    try {
      await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
      const origin = `http://127.0.0.1:${server.address().port}`
      owner = new BrowserWindow({ show: true, width: 1000, height: 800,
        webPreferences: { sandbox: true, contextIsolation: true, nodeIntegration: false } })
      await owner.loadURL('data:text/html,<title>PDF manager fixture</title>')
      manager = new NativeWorkbenchSurfaceManager({ getWindow: () => owner, emit(event) { events.push(event) } })
      const record = id => manager.surfaces.get(id)
      const frames = frame => [frame, ...frame.frames.flatMap(frames)]
      const viewerLoaded = surface => Boolean(surface && frames(surface.view.webContents.mainFrame)
        .some(frame => frame.url === 'chrome-extension://mhjfbmdgcfjbbpaeojofohoefgiehjai/index.html'))
      const create = async (id, path) => {
        const result = await manager.createSurface({ version: 2, surfaceId: id,
          kind: 'url-preview', payload: { url: `${origin}${path}`, scopeId: 'pdf-test' } })
        assert.equal(result.ok, true, result.message)
        await until(() => viewerLoaded(record(id)), `${id} PDF viewer`)
        return record(id)
      }
      const direct = await create('pdf-direct', '/document.pdf')
      manager.setSurfaceRect({ surfaceId: 'pdf-direct', x: 0, y: 0,
        width: 900, height: 650, visible: true })
      manager.activateSurface('pdf-direct')
      await wait(1200)
      if (process.env.OPENSQUILLA_PDF_CAPTURE_PATH) {
        const screenshot = await direct.view.webContents.capturePage()
        await writeFile(process.env.OPENSQUILLA_PDF_CAPTURE_PATH, screenshot.toPNG())
        console.log(`PDF capture: ${process.env.OPENSQUILLA_PDF_CAPTURE_PATH}`)
      }
      const find = await manager.navigateSurface({ version: 2,
        surfaceId: 'pdf-direct', action: 'find', query: 'Synthetic' })
      assert.equal(find.ok, true, find.message)
      await until(() => events.some(event => event.surfaceId === 'pdf-direct'
        && event.type === 'find-state' && event.detail?.findFinal
        && event.detail?.findMatches >= 1), 'PDF text find result')
      const findEventCount = events.filter(event => event.surfaceId === 'pdf-direct'
        && event.type === 'find-state' && event.detail?.findFinal).length
      const next = await manager.navigateSurface({ version: 2,
        surfaceId: 'pdf-direct', action: 'find-next', forward: true })
      assert.equal(next.ok, true, next.message)
      await until(() => events.filter(event => event.surfaceId === 'pdf-direct'
        && event.type === 'find-state' && event.detail?.findFinal).length > findEventCount,
      'PDF find-next result')
      const stop = await manager.navigateSurface({ version: 2,
        surfaceId: 'pdf-direct', action: 'find-stop' })
      assert.equal(stop.ok, true, stop.message)
      assert.ok(events.some(event => event.surfaceId === 'pdf-direct'
        && event.type === 'find-state' && event.detail?.findQuery === ''
        && event.detail?.findMatches === 0))
      await create('pdf-embedded', '/embedded.html')
      await create('pdf-redirect', '/redirect')

      const link = await manager.createSurface({ version: 2, surfaceId: 'pdf-link',
        kind: 'url-preview', payload: { url: `${origin}/link.html`, scopeId: 'pdf-test' } })
      assert.equal(link.ok, true, link.message)
      await record('pdf-link').view.webContents.executeJavaScript("document.querySelector('#pdf').click()")
      await until(() => viewerLoaded(record('pdf-link')), 'renderer PDF link')

      const opened = await manager.executeBrowserMcp({ operation: 'open',
        sessionKey: 'pdf-test', url: `${origin}/document.pdf`, observationMode: 'dom' },
      AbortSignal.timeout(20_000))
      const openedRecord = [...manager.surfaces.values()].find(item => item.targetRef === opened.targetRef)
      assert.ok(openedRecord)
      await until(() => viewerLoaded(openedRecord), 'agent PDF open')

      const popup = await manager.createSurface({ version: 2, surfaceId: 'pdf-popup',
        kind: 'url-preview', payload: { url: `${origin}/popup.html`, scopeId: 'pdf-test' } })
      assert.equal(popup.ok, true, popup.message)
      const popupRecord = record('pdf-popup')
      await popupRecord.view.webContents.executeJavaScript("document.querySelector('#popup').click()")
      await until(() => [...manager.surfaces.values()].some(item =>
        item.openerTargetRef === popupRecord.targetRef && viewerLoaded(item)), 'PDF popup tab')

      const singleCloseStarted = Date.now()
      const closed = await manager.navigateSurface({ version: 2,
        surfaceId: 'pdf-direct', action: 'close' })
      assert.equal(closed.ok, true, closed.message)
      assert.ok(Date.now() - singleCloseStarted < 2000, 'A PDF tab should close promptly')
      await until(() => !manager.surfaces.has('pdf-direct'), 'closed PDF record cleanup')
      const allCloseStarted = Date.now()
      assert.equal(await manager.closeBrowserTabs(), true)
      assert.ok(Date.now() - allCloseStarted < 2000, 'The remaining PDF tabs should close promptly')
      console.log('Browser PDF manager Electron tests passed')
    } finally {
      if (manager) for (const surface of [...manager.surfaces.values()]) {
        await manager.destroySurface(surface.id, true)
      }
      owner?.destroy()
      await new Promise(resolve => server.close(resolve))
      app.quit()
    }
  })().catch(error => { console.error(error); app.exit(1) })
}
