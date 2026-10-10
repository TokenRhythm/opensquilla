import assert from 'node:assert/strict'
import { spawn, spawnSync } from 'node:child_process'
import { createServer } from 'node:http'
import { mkdtemp, rm } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

const wait = ms => new Promise(resolve => setTimeout(resolve, ms))
async function until(predicate, label) {
  const deadline = Date.now() + 8000
  while (Date.now() < deadline) {
    if (await predicate()) return
    await wait(30)
  }
  throw new Error(`Timed out waiting for ${label}`)
}

if (!process.versions.electron) {
  if (process.platform === 'linux' && !process.env.DISPLAY && !process.env.WAYLAND_DISPLAY
    && process.env.OPENSQUILLA_PROMPT_MANAGER_UNDER_XVFB !== '1') {
    const display = spawnSync('xvfb-run', ['-a', process.execPath, fileURLToPath(import.meta.url)], {
      env: { ...process.env, OPENSQUILLA_PROMPT_MANAGER_UNDER_XVFB: '1' }, stdio: 'inherit',
    })
    if (display.error) throw display.error
    process.exit(display.status ?? 1)
  }
  const profile = await mkdtemp(join(tmpdir(), 'opensquilla-browser-prompt-manager-'))
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
    const web = createServer((request, response) => {
      response.setHeader('content-type', 'text/html; charset=utf-8')
      if (request.url === '/onload') {
        response.end(`<!doctype html><title>Onload prompt</title><output id="result"></output>
          <script>const value = window.prompt('Opening question?', 'opening value');
          document.querySelector('#result').textContent = value === null ? '[cancelled]' : value;</script>`)
        return
      }
      if (request.url === '/popup') {
        response.end(`<!doctype html><title>Popup source</title>
          <button id="open" onclick="window.open('/button','_blank')">Open tab</button>`)
        return
      }
      response.end(`<!doctype html><title>Prompt manager fixture</title>
        <button id="ask">Ask a question</button><output id="result"></output>
        <script>document.querySelector('#ask').onclick = () => {
          const value = window.prompt('Manager question?', 'starting value');
          document.querySelector('#result').textContent = value === null ? '[cancelled]' : value;
        }</script>`)
    })
    let owner, manager
    try {
      await new Promise(resolve => web.listen(0, '127.0.0.1', resolve))
      const origin = `http://127.0.0.1:${web.address().port}`
      owner = new BrowserWindow({ show: true, width: 1000, height: 800,
        webPreferences: { sandbox: true, contextIsolation: true, nodeIntegration: false } })
      await owner.loadURL('data:text/html,<title>Prompt manager host</title>')
      manager = new NativeWorkbenchSurfaceManager({ getWindow: () => owner, emit() {} })
      const call = request => manager.executeBrowserMcp({ sessionKey: 'prompt-task',
        observationMode: 'dom', ...request }, AbortSignal.timeout(20_000))
      const opened = await call({ operation: 'open', url: origin })
      const record = [...manager.surfaces.values()].find(item => item.targetRef === opened.targetRef)
      assert.ok(record)
      const observed = await call({ operation: 'observe', targetRef: opened.targetRef })
      const button = observed.observation.refs.find(ref => ref.name === 'Ask a question')
      assert.ok(button)

      const click = call({ operation: 'act', targetRef: opened.targetRef,
        action: 'click', ref: button.ref })
      await until(() => record.prompt?.state(), 'agent-visible prompt')
      const pending = record.prompt.state()
      assert.equal(pending.message, 'Manager question?')
      assert.equal(pending.defaultValue, 'starting value')
      assert.equal(pending.origin, origin)
      assert.equal(typeof pending.openedAt, 'number')
      const during = await call({ operation: 'observe', targetRef: opened.targetRef })
      assert.equal(during.observation.consistency, 'blocked')
      assert.equal(during.observation.browserState.dialogs.pending[0].id, pending.id)
      const handled = await call({ operation: 'dialog', targetRef: opened.targetRef,
        dialogId: pending.id, accept: true, promptText: 'agent response' })
      assert.equal(handled.performed, true)
      await click
      assert.equal(await record.view.webContents.executeJavaScript(
        "document.querySelector('#result').textContent"), 'agent response')
      assert.equal(record.prompt.state(), null)

      const manualClick = record.view.webContents.executeJavaScript(
        "document.querySelector('#ask').click()")
      await until(() => record.prompt?.state(), 'manual prompt')
      await until(() => BrowserWindow.getAllWindows().some(window =>
        window !== owner && window.getTitle() === 'Page prompt' && window.isVisible()),
      'manual prompt modal')
      const modal = BrowserWindow.getAllWindows().find(window =>
        window !== owner && window.getTitle() === 'Page prompt')
      await modal.webContents.executeJavaScript(`document.getElementById('value').value = 'human response';
        document.querySelector('#answer button[type=submit]').click()`)
      await manualClick
      assert.equal(await record.view.webContents.executeJavaScript(
        "document.querySelector('#result').textContent"), 'human response')

      const opening = call({ operation: 'open', url: `${origin}/onload` })
      await until(() => [...manager.surfaces.values()].some(item =>
        item.documentUrl.endsWith('/onload') && item.prompt?.state()), 'onload prompt')
      const openingRecord = [...manager.surfaces.values()].find(item =>
        item.documentUrl.endsWith('/onload'))
      const openingDialog = openingRecord.prompt.state()
      await call({ operation: 'dialog', targetRef: openingRecord.targetRef,
        dialogId: openingDialog.id, accept: true, promptText: 'opening response' })
      const openedOnload = await opening
      assert.equal(openedOnload.targetRef, openingRecord.targetRef)
      assert.equal(await openingRecord.view.webContents.executeJavaScript(
        "document.querySelector('#result').textContent"), 'opening response')

      const popupSource = await call({ operation: 'open', url: `${origin}/popup` })
      const popupSourceRecord = [...manager.surfaces.values()].find(item =>
        item.targetRef === popupSource.targetRef)
      await popupSourceRecord.view.webContents.executeJavaScript(
        "document.querySelector('#open').click()")
      await until(() => [...manager.surfaces.values()].some(item =>
        item.openerTargetRef === popupSource.targetRef && item.view.webContents.getURL().endsWith('/button')),
      'popup browser tab')
      const popupRecord = [...manager.surfaces.values()].find(item =>
        item.openerTargetRef === popupSource.targetRef)
      const popupClick = popupRecord.view.webContents.executeJavaScript(
        "document.querySelector('#ask').click()")
      await until(() => popupRecord.prompt?.state(), 'popup prompt')
      await call({ operation: 'dialog', targetRef: popupRecord.targetRef,
        dialogId: popupRecord.prompt.state().id, accept: true, promptText: 'popup response' })
      await popupClick
      assert.equal(await popupRecord.view.webContents.executeJavaScript(
        "document.querySelector('#result').textContent"), 'popup response')
      console.log('Browser prompt manager and MCP tests passed')
    } finally {
      if (manager) for (const surface of [...manager.surfaces.values()]) {
        await manager.destroySurface(surface.id)
      }
      owner?.destroy()
      await new Promise(resolve => web.close(resolve))
      app.quit()
    }
  })().catch(error => { console.error(error); app.exit(1) })
}
