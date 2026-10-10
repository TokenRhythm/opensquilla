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
    && process.env.OPENSQUILLA_PROMPT_UNDER_XVFB !== '1') {
    const display = spawnSync('xvfb-run', ['-a', process.execPath, fileURLToPath(import.meta.url)], {
      env: { ...process.env, OPENSQUILLA_PROMPT_UNDER_XVFB: '1' }, stdio: 'inherit',
    })
    if (display.error) throw display.error
    process.exit(display.status ?? 1)
  }
  const profile = await mkdtemp(join(tmpdir(), 'opensquilla-browser-prompt-'))
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
  const { app, BrowserWindow, WebContentsView, session } = await import('electron')
  const { BROWSER_PROMPT_PRELOAD, BrowserPromptController } = await import('../dist/browser-prompt.js')
  app.commandLine.appendSwitch('disable-gpu')
  app.on('window-all-closed', () => {})
  void (async () => {
    await app.whenReady()
    const web = createServer((_request, response) => {
      response.setHeader('content-type', 'text/html; charset=utf-8')
      response.end(`<!doctype html><title>Prompt fixture</title>
        <button id="ask">Ask</button><output id="result"></output>
        <script>document.querySelector('#ask').onclick = () => {
          const value = window.prompt('Synthetic question?', 'seed');
          document.querySelector('#result').textContent = value === null ? '[cancelled]' : value;
        }</script>`)
    })
    let owner, view, controller
    try {
      await new Promise(resolve => web.listen(0, '127.0.0.1', resolve))
      const origin = `http://127.0.0.1:${web.address().port}`
      owner = new BrowserWindow({ show: true, width: 800, height: 600,
        webPreferences: { sandbox: true, contextIsolation: true, nodeIntegration: false } })
      await owner.loadURL('data:text/html,<title>Prompt host</title>')
      view = new WebContentsView({ webPreferences: { preload: BROWSER_PROMPT_PRELOAD,
        session: session.fromPartition('browser-prompt-fixture'), sandbox: true,
        contextIsolation: true, nodeIntegration: false, webSecurity: true } })
      owner.contentView.addChildView(view)
      view.setBounds({ x: 0, y: 0, width: 700, height: 500 })
      controller = new BrowserPromptController({ owner, contents: view.webContents,
        isAllowed: () => true })
      await view.webContents.loadURL(origin)

      const click = () => view.webContents.executeJavaScript("document.querySelector('#ask').click()")
      const answer = () => view.webContents.executeJavaScript("document.querySelector('#result').textContent")

      const userClick = click()
      await until(() => controller.state(), 'first prompt state')
      assert.deepEqual({ message: controller.state().message,
        defaultValue: controller.state().defaultValue, origin: controller.state().origin },
      { message: 'Synthetic question?', defaultValue: 'seed', origin })
      await until(() => BrowserWindow.getAllWindows().some(window =>
        window !== owner && window.getTitle() === 'Page prompt' && window.isVisible()),
      'visible input modal')
      const modal = BrowserWindow.getAllWindows().find(window =>
        window !== owner && window.getTitle() === 'Page prompt')
      await modal.webContents.executeJavaScript(`document.getElementById('value').value = 'typed value';
        document.querySelector('#answer button[type=submit]').click()`)
      await userClick
      assert.equal(await answer(), 'typed value')
      assert.equal(controller.state(), null)

      const agentClick = click()
      await until(() => controller.state(), 'agent prompt state')
      assert.equal(controller.respond(controller.state().id, 'agent answer'), true)
      await agentClick
      assert.equal(await answer(), 'agent answer')

      const closeClick = click()
      await until(() => controller.state(), 'lifecycle prompt state')
      controller.dispose()
      await closeClick
      assert.equal(await answer(), '[cancelled]')
      console.log('Browser prompt Electron tests passed')
    } finally {
      controller?.dispose()
      if (view && owner && !owner.isDestroyed()) owner.contentView.removeChildView(view)
      if (view && !view.webContents.isDestroyed()) view.webContents.close()
      owner?.destroy()
      await new Promise(resolve => web.close(resolve))
      app.quit()
    }
  })().catch(error => { console.error(error); app.exit(1) })
}
