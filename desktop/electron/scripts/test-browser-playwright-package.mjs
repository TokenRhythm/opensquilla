import assert from 'node:assert/strict'
import { spawn, spawnSync } from 'node:child_process'
import { copyFile, mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createPackage, listPackage } from '@electron/asar'
import { Platform } from 'app-builder-lib'
import { getNodeModuleFileMatcher } from 'app-builder-lib/out/fileMatcher.js'
import { computeNodeModuleFileSets } from 'app-builder-lib/out/util/appFileCopier.js'
import { TmpDir } from 'temp-file'
import electron from 'electron'

const scriptPath = fileURLToPath(import.meta.url)
if (process.platform === 'linux' && !process.env.DISPLAY && !process.env.WAYLAND_DISPLAY
  && process.env.OPENSQUILLA_PACKAGE_UNDER_XVFB !== '1') {
  const child = spawnSync('xvfb-run', ['-a', process.execPath, scriptPath], {
    env: { ...process.env, OPENSQUILLA_PACKAGE_UNDER_XVFB: '1' }, stdio: 'inherit',
  })
  if (child.error) throw child.error
  process.exit(child.status ?? 1)
}

const projectDir = fileURLToPath(new URL('../', import.meta.url))
const metadata = JSON.parse(await readFile(join(projectDir, 'package.json'), 'utf8'))
const root = await mkdtemp(join(tmpdir(), 'opensquilla-browser-package-'))
const stage = join(root, 'app')
const temporary = new TmpDir()
const fixtureHtml = `<!doctype html><meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'none'; style-src 'none'; img-src 'none'">
  <h1>Packaged browser</h1><button id="increment">Increment</button><button disabled>Waiting button</button>`
let processHandle
try {
  const info = { debugLogger: { isEnabled: false }, tempDirManager: temporary,
    appDir: projectDir, projectDir, getWorkspaceRoot: async () => projectDir,
    getPackageManager: async () => 'npm', nodePackageName: metadata.name,
    appInfo: { type: 'module' }, config: metadata.build }
  const platform = process.platform === 'win32' ? Platform.WINDOWS : process.platform === 'darwin' ? Platform.MAC : Platform.LINUX
  const specific = metadata.build[platform.buildConfigurationKey] ?? {}
  const matcher = getNodeModuleFileMatcher(projectDir, stage, value => value, specific, info)
  // Use the installed builder's real production collector and file matcher,
  // then package only their selected files without an installer or signing.
  const fileSets = await computeNodeModuleFileSets({ info, config: metadata.build, platform }, matcher)
  const playwrightSet = fileSets.find(set => set.destination.endsWith('playwright-core'))
  assert.ok(playwrightSet, 'playwright-core must be collected as a production dependency')
  assert.ok(playwrightSet.files.some(file => file.endsWith('coreBundle.js')))
  assert.ok(!fileSets.some(set => set.destination.endsWith('/playwright')), 'test-only Playwright must remain excluded')
  for (const fileSet of fileSets) {
    for (const file of fileSet.files) {
      if (!fileSet.metadata.get(file)?.isFile()) continue
      const destination = join(fileSet.destination, relative(fileSet.src, file))
      await mkdir(dirname(destination), { recursive: true })
      await copyFile(file, destination)
    }
  }
  await mkdir(join(stage, 'dist'), { recursive: true })
  for (const name of ['browser-pointer.js', 'browser-pointer-controller.js', 'browser-mouse-trajectory.js', 'browser-playwright.js', 'desktop-browser.js', 'desktop-browser-mcp.js']) {
    await copyFile(join(projectDir, 'dist', name), join(stage, 'dist', name))
  }
  await writeFile(join(stage, 'package.json'), JSON.stringify({ name: 'browser-package-fixture',
    version: '1.0.0', type: 'module', main: 'main.mjs' }))
  await writeFile(join(stage, 'main.mjs'), `
import assert from 'node:assert/strict'
import { app, BrowserWindow, WebContentsView } from 'electron'
import { BrowserPlaywrightDriver } from './dist/browser-playwright.js'
app.commandLine.appendSwitch('disable-gpu')
app.on('window-all-closed', () => {})
void app.whenReady().then(async () => {
let driver
try {
  assert.equal(app.commandLine.hasSwitch('remote-debugging-port'), false)
  const owner = new BrowserWindow({ show: false, webPreferences: { sandbox: true } })
  const view = new WebContentsView({ webPreferences: { sandbox: true, contextIsolation: true,
    nodeIntegration: false, partition: 'synthetic-package-page' } })
  owner.contentView.addChildView(view)
  view.setBounds({ x: 0, y: 0, width: 800, height: 600 })
  await view.webContents.loadURL('data:text/html,' + encodeURIComponent(${JSON.stringify(fixtureHtml)}))
  await view.webContents.executeJavaScript('document.getElementById("increment").addEventListener("click", () => { window.clicks = (window.clicks || 0) + 1 })')
  view.webContents.debugger.attach('1.3')
  let pointerVisible = true
  driver = new BrowserPlaywrightDriver(view.webContents, () => pointerVisible)
  await driver.pointer.setTask('synthetic-package-turn')
  await driver.pointer.touch()
  const signal = new AbortController().signal
  let snapshot = await driver.snapshot(1, () => {}, signal)
  assert.equal(view.webContents.getBackgroundThrottling(), true)
  assert.match(snapshot.text, /Packaged browser/)
  await driver.act({ action: 'click', ref: snapshot.refs.find(node => node.name === 'Increment').ref }, 1, () => {}, signal)
  assert.equal(await view.webContents.executeJavaScript('window.clicks'), 1)
  const pointer = await view.webContents.executeJavaScript(
    '(() => { const host = document.getElementById("__opensquilla-browser-pointer"); const cursor = host?.shadowRoot?.querySelector("svg"); const box = cursor?.getBoundingClientRect(); const button = document.getElementById("increment"); const target = button.getBoundingClientRect(); return { visible: Boolean(host && host.dataset.opensquillaBrowserPointer === "true"), pointerEvents: host ? getComputedStyle(host).pointerEvents : null, display: host ? getComputedStyle(host).display : null, position: host ? getComputedStyle(host).position : null, cursorWidth: box?.width || 0, cursorHeight: box?.height || 0, cursorVisible: cursor ? getComputedStyle(cursor).visibility : null, hitTarget: document.elementFromPoint(target.x + target.width / 2, target.y + target.height / 2) === button } })()',
  )
  assert.equal(pointer.visible, true)
  assert.equal(pointer.pointerEvents, 'none')
  assert.equal(pointer.display, 'block', 'strict style-src must not hide the cursor')
  assert.equal(pointer.position, 'fixed')
  assert.equal(pointer.cursorVisible, 'visible')
  assert.ok(pointer.cursorWidth > 0 && pointer.cursorHeight > 0)
  assert.equal(pointer.hitTarget, true, 'the pointer must not intercept the button hit target')
  const cleanSnapshot = await driver.snapshot(1, () => {}, signal)
  assert.equal(cleanSnapshot.text, snapshot.text, 'the pointer must not become accessible page content')
  assert.deepEqual(cleanSnapshot.refs.map(({ ref, ...node }) => node), snapshot.refs.map(({ ref, ...node }) => node),
    'the pointer must not add actionable element refs')
  snapshot = cleanSnapshot
  assert.equal(owner.isVisible(), false)
  assert.equal(view.webContents.getBackgroundThrottling(), true)
  const sendCommand = view.webContents.debugger.sendCommand.bind(view.webContents.debugger)
  let cursorAtCapture
  view.webContents.debugger.sendCommand = async (method, params, sessionId) => {
    if (method === 'Page.captureScreenshot') {
      const check = await sendCommand('Runtime.evaluate', {
        expression: 'Boolean(document.getElementById("__opensquilla-browser-pointer"))', returnByValue: true,
      }, sessionId)
      cursorAtCapture = check.result.value
    }
    return await sendCommand(method, params, sessionId)
  }
  const image = await driver.screenshot(() => {}, signal)
  view.webContents.debugger.sendCommand = sendCommand
  assert.ok(image.width > 0 && image.height > 0)
  assert.equal(cursorAtCapture, false, 'the pointer must be removed before Chromium captures pixels')
  assert.equal(await view.webContents.executeJavaScript('Boolean(document.getElementById("__opensquilla-browser-pointer"))'), true,
    'the active pointer must return after a screenshot')
  pointerVisible = false
  await driver.act({ action: 'click', ref: snapshot.refs.find(node => node.name === 'Increment').ref }, 1, () => {}, signal)
  assert.equal(await view.webContents.executeJavaScript('window.clicks'), 2)
  assert.equal(await view.webContents.executeJavaScript('Boolean(document.getElementById("__opensquilla-browser-pointer"))'), false,
    'a hidden surface callback must prevent pointer creation')
  pointerVisible = true
  const cancellation = new AbortController()
  const pending = driver.act({ action: 'click', ref: snapshot.refs.find(node => node.name === 'Waiting button').ref }, 1, () => {}, cancellation.signal)
  const cancelled = assert.rejects(pending, { code: 'TIMEOUT' })
  await new Promise(resolve => setTimeout(resolve, 50))
  assert.equal(view.webContents.getBackgroundThrottling(), false)
  cancellation.abort()
  await cancelled
  assert.equal(view.webContents.getBackgroundThrottling(), true)
  assert.equal(owner.isVisible(), false)
  view.webContents.setBackgroundThrottling(false)
  const preserved = await driver.snapshot(1, () => {}, signal)
  await driver.act({ action: 'click', ref: preserved.refs.find(node => node.name === 'Increment').ref }, 1, () => {}, signal)
  assert.equal(view.webContents.getBackgroundThrottling(), false)
  assert.equal(await view.webContents.executeJavaScript('Boolean(document.getElementById("__opensquilla-browser-pointer"))'), true)
  await new Promise(resolve => setTimeout(resolve, 1_700))
  assert.equal(await view.webContents.executeJavaScript('Boolean(document.getElementById("__opensquilla-browser-pointer"))'), true,
    'the active task cursor must survive idle time under strict CSP')
  await driver.pointer.setTask(null)
  await new Promise(resolve => setTimeout(resolve, 260))
  assert.equal(await view.webContents.executeJavaScript('Boolean(document.getElementById("__opensquilla-browser-pointer"))'), false,
    'a completed task must fade out and remove its cursor')
  view.webContents.setBackgroundThrottling(true)
  const disposePending = driver.act({ action: 'click', ref: preserved.refs.find(node => node.name === 'Waiting button').ref }, 1, () => {}, signal)
  const disposed = assert.rejects(disposePending)
  await new Promise(resolve => setTimeout(resolve, 50))
  assert.equal(view.webContents.getBackgroundThrottling(), false)
  await driver.dispose()
  await disposed
  assert.equal(view.webContents.getBackgroundThrottling(), true)
  assert.equal(owner.isVisible(), false)
  assert.equal(view.webContents.debugger.isAttached(), true)
  console.log('BROWSER_PACKAGE_PASS')
  app.exit(0)
} catch (error) {
  console.error(error)
  await driver?.dispose().catch(() => {})
  app.exit(1)
}
})
`)
  const archive = join(root, 'app.asar')
  await createPackage(stage, archive)
  assert.ok(listPackage(archive).some(path => path.endsWith('/node_modules/playwright-core/lib/coreBundle.js')))
  const output = await new Promise((resolve, reject) => {
    const env = { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: 'true', NO_PROXY: '*', no_proxy: '*' }
    delete env.ELECTRON_RUN_AS_NODE
    processHandle = spawn(process.env.OPENSQUILLA_ELECTRON_EXECUTABLE || electron,
      [...(process.platform === 'linux' ? ['--no-sandbox'] : []), `--user-data-dir=${join(root, 'profile')}`, archive],
      { env, stdio: ['ignore', 'pipe', 'pipe'] })
    let stdout = ''
    let stderr = ''
    processHandle.stdout.on('data', chunk => { stdout += chunk })
    processHandle.stderr.on('data', chunk => { stderr += chunk })
    const timer = setTimeout(() => processHandle.kill('SIGKILL'), 30_000)
    processHandle.once('error', error => { clearTimeout(timer); reject(error) })
    processHandle.once('exit', (code, signal) => {
      clearTimeout(timer)
      if (code === 0) resolve(stdout)
      else reject(new Error(`Packaged browser fixture failed (${code ?? signal}): ${stdout}\n${stderr}`))
    })
  })
  assert.match(output, /BROWSER_PACKAGE_PASS/)
  console.log(`Production packaging passed: builder selected ${playwrightSet.files.length} Playwright files; isolated Electron loaded ASAR and operated its existing page without a debugging port.`)
} finally {
  if (processHandle && processHandle.exitCode === null) processHandle.kill('SIGKILL')
  await temporary.cleanup()
  await rm(root, { recursive: true, force: true })
}
