import assert from 'node:assert/strict'
import { spawn, spawnSync } from 'node:child_process'
import { createServer } from 'node:http'
import { mkdtemp, rm } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

const delay = ms => new Promise(resolve => setTimeout(resolve, ms))
async function until(predicate, description) {
  const deadline = Date.now() + 5000
  while (Date.now() < deadline) {
    if (await predicate()) return
    await delay(20)
  }
  throw new Error(`Timed out waiting for ${description}`)
}

if (!process.versions.electron) {
  if (process.platform === 'linux' && !process.env.DISPLAY && !process.env.WAYLAND_DISPLAY
    && process.env.OPENSQUILLA_SESSION_ISOLATION_XVFB !== '1') {
    const run = spawnSync('xvfb-run', ['-a', process.execPath, fileURLToPath(import.meta.url)], {
      env: { ...process.env, OPENSQUILLA_SESSION_ISOLATION_XVFB: '1' }, stdio: 'inherit',
    })
    if (run.error) throw run.error
    process.exit(run.status ?? 1)
  }
  const directory = await mkdtemp(join(tmpdir(), 'opensquilla-session-isolation-'))
  const require = createRequire(import.meta.url)
  const processHandle = spawn(require('electron'), [
    ...(process.platform === 'linux' && process.getuid?.() === 0 ? ['--no-sandbox'] : []),
    `--user-data-dir=${directory}`, fileURLToPath(import.meta.url),
  ], { stdio: 'inherit', env: { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: 'true' } })
  const watchdog = setTimeout(() => processHandle.kill('SIGKILL'), 60_000)
  try {
    process.exitCode = await new Promise((resolve, reject) => {
      processHandle.once('error', reject)
      processHandle.once('exit', status => resolve(status ?? 1))
    })
  } finally {
    clearTimeout(watchdog)
    await rm(directory, { recursive: true, force: true })
  }
} else {
  const { app, BrowserWindow } = await import('electron')
  const { NativeWorkbenchSurfaceManager } = await import('../dist/native-workbench-surface.js')
  app.commandLine.appendSwitch('disable-gpu')
  app.on('window-all-closed', () => {})
  void (async () => {
    await app.whenReady()
    let owner, manager, code = 0
    const events = []
    const server = createServer((request, response) => {
      if (request.url === '/note') {
        response.writeHead(200, { 'content-type': 'text/plain',
          'content-disposition': 'attachment; filename="synthetic.txt"' })
        response.end('Synthetic session-owned download')
        return
      }
      response.writeHead(200, { 'content-type': 'text/html; charset=utf-8' })
      response.end(`<!doctype html><title>Session ${request.url}</title>
        <h1>${request.url}</h1><textarea aria-label="Session draft"></textarea>
        <a href="/note" download>Download session note</a>
        <button onclick="window.open('/popup-a','shared-name')">Open related page</button>`)
    })
    try {
      await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
      const origin = `http://127.0.0.1:${server.address().port}`
      owner = new BrowserWindow({ show: false, width: 900, height: 700,
        webPreferences: { sandbox: true, contextIsolation: true, nodeIntegration: false } })
      await owner.loadURL('data:text/html,<title>Synthetic session host</title>')
      manager = new NativeWorkbenchSurfaceManager({ getWindow: () => owner,
        emit: event => events.push(event) })
      const call = (sessionKey, request) => manager.executeBrowserMcp({ sessionKey,
        observationMode: 'dom', ...request }, AbortSignal.timeout(10_000))
      const record = target => [...manager.surfaces.values()].find(value => value.targetRef === target.targetRef)
      const evaluate = (target, code) => record(target).contents.executeJavaScript(code)
      const a = await call('synthetic-a', { operation: 'open', url: `${origin}/a` })
      await evaluate(a, "document.cookie='sid=only-a; SameSite=Lax; Path=/'; localStorage.setItem('owner','a'); document.querySelector('textarea').value='retained draft a'")
      const aChild = await call('synthetic-a', { operation: 'open', url: `${origin}/related-a`, contextTargetRef: a.targetRef })
      const b = await call('synthetic-b', { operation: 'open', url: `${origin}/b` })
      assert.equal(record(a).previewSession, record(aChild).previewSession)
      assert.notEqual(record(a).previewSession, record(b).previewSession)
      assert.match(await evaluate(aChild, 'document.cookie'), /sid=only-a/)
      assert.equal(await evaluate(aChild, "localStorage.getItem('owner')"), 'a')
      assert.equal(await evaluate(b, 'document.cookie'), '')
      assert.equal(await evaluate(b, "localStorage.getItem('owner')"), null)
      for (const operation of ['observe', 'snapshot', 'reload']) {
        await assert.rejects(call('synthetic-b', { operation, targetRef: a.targetRef }),
          error => error.code === 'TARGET_NOT_FOUND')
      }
      await assert.rejects(call('synthetic-b', { operation: 'open', url: `${origin}/foreign`, contextTargetRef: a.targetRef }),
        error => error.code === 'TARGET_NOT_FOUND')
      owner.showInactive()
      const display = target => manager.setSurfaceRect({ surfaceId: record(target).id,
        x: 0, y: 0, width: 700, height: 500, visible: true })
      assert.equal(display(a).ok, true)
      assert.equal(record(a).view.getVisible(), true)
      assert.equal(display(b).ok, true)
      assert.equal(record(a).view.getVisible(), false)
      assert.equal(record(b).view.getVisible(), true)
      const background = await call('synthetic-a', { operation: 'open', url: `${origin}/background-a` })
      await call('synthetic-a', { operation: 'open', targetRef: aChild.targetRef, url: `${origin}/navigated-a` })
      await evaluate(a, "void window.open('/popup-a','shared-name')")
      await until(() => events.some(event => event.type === 'browser-opened' && event.detail?.url === `${origin}/popup-a`), 'background popup announcement')
      assert.equal(manager.activeSurfaceId, record(b).id)
      assert.equal(record(b).view.getVisible(), true)
      for (const item of manager.surfaces.values()) {
        if (item.scopeId === 'synthetic-a') assert.equal(item.view.getVisible(), false,
          'Background navigation, opening and popup completion must not reveal another task')
      }
      assert.equal((await call('synthetic-b', { operation: 'list' })).targets.length, 1)
      assert.ok((await call('synthetic-a', { operation: 'list' })).targets.length >= 4)
      assert.equal(display(a).ok, true)
      assert.equal(record(b).view.getVisible(), false)
      assert.equal(await evaluate(a, "document.querySelector('textarea').value"), 'retained draft a')
      assert.match(await evaluate(a, 'document.cookie'), /sid=only-a/)
      assert.equal(record(background).view.getVisible(), false)
      const refs = (await call('synthetic-a', { operation: 'observe', targetRef: a.targetRef })).observation.refs
      const download = await call('synthetic-a', { operation: 'act', targetRef: a.targetRef,
        action: 'download', ref: refs.find(value => value.name === 'Download session note').ref })
      await assert.rejects(call('synthetic-b', { operation: 'snapshot', targetRef: a.targetRef,
        downloadId: download.download.downloadId }), error => error.code === 'TARGET_NOT_FOUND')
      await assert.rejects(call('synthetic-b', { operation: 'snapshot', targetRef: b.targetRef,
        downloadId: download.download.downloadId }), error => error.code === 'DOWNLOAD_NOT_FOUND')
      console.log('Browser session isolation passed: visibility, background events, retained drafts, storage, target access and downloads')
    } catch (error) {
      console.error(error)
      code = 1
    } finally {
      await manager?.destroyAll()
      owner?.destroy()
      server.closeAllConnections()
      await new Promise(resolve => server.close(resolve))
      app.exit(code)
    }
  })()
}
