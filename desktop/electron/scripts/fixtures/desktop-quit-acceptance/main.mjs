import { app, BrowserWindow, dialog, ipcMain, Menu, nativeTheme, screen } from 'electron'
import { spawn } from 'node:child_process'
import { createHash } from 'node:crypto'
import { EventEmitter, once } from 'node:events'
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs'
import { createServer } from 'node:http'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createContext, runInContext } from 'node:vm'
import ts from '@typescript/typescript6'
import { DesktopWriterAdmission } from '../../../dist/desktop-writer-admission.js'
import { DesktopQuitBudget, DesktopQuitConfirmation } from '../../../dist/desktop-quit.js'
import { buildDesktopQuitDialogHtml, parseDesktopQuitDialogResponse } from '../../../dist/desktop-quit-dialog.js'
import { mainWindowCloseAction } from '../../../dist/desktop-window-lifecycle.js'
import { normalizeGatewayLocale } from '../../../dist/desktop-locale.js'
import { NativeWorkbenchSurfaceManager } from '../../../dist/native-workbench-surface.js'

const fixtureDir = dirname(fileURLToPath(import.meta.url))
const distDir = join(fixtureDir, '../../../dist')
const profileArg = process.argv.find(value => value.startsWith('--acceptance-profile='))
if (!profileArg) throw new Error('Pass --acceptance-profile=<isolated absolute directory>.')
const profile = profileArg.slice('--acceptance-profile='.length)
mkdirSync(profile, { recursive: true })
mkdirSync(join(profile, 'chromium'), { recursive: true })
app.setPath('userData', join(profile, 'chromium'))
app.commandLine.appendSwitch('disable-gpu')
app.on('window-all-closed', () => {})
const compiledMain = readFileSync(join(distDir, 'main.js'), 'utf8')
const productionMainSha256 = createHash('sha256').update(compiledMain).digest('hex')
const source = ts.createSourceFile('main.js', compiledMain, ts.ScriptTarget.Latest, true, ts.ScriptKind.JS)
const functions = new Map()
let messages
let beforeQuit
function collect(node) {
  if (ts.isFunctionDeclaration(node) && node.name) functions.set(node.name.text, node.getText(source))
  if (ts.isVariableDeclaration(node) && node.name.getText(source) === 'DESKTOP_MESSAGES') messages = node.initializer.getText(source)
  if (ts.isCallExpression(node) && node.expression.getText(source) === 'app.on'
    && ts.isStringLiteral(node.arguments[0]) && node.arguments[0].text === 'before-quit') beforeQuit = node.arguments[1].getText(source)
  ts.forEachChild(node, collect)
}
collect(source)
const required = ['desktopT', 'showDesktopQuitConfirmation', 'promptForMainWindowClose', 'handleMainWindowClose', 'holdQuitForTaskConfirmation', 'showDesktopQuitFailure', 'shutdownFromSignal']
for (const name of required) if (!functions.has(name)) throw new Error(`Production handler missing: ${name}`)
if (!messages || !beforeQuit) throw new Error('Production messages/before-quit handler missing.')

let owner
let controller
let manager
let env
let task
let ticks = 0
let finishWriter = null
let pageNumber = 0
let resetting = false
let resetGeneration = 0
let stopped = false
let dialogCount = 0
const settings = { behavior: 'ask', locale: 'en', theme: 'light', activity: 'active', cleanupFailure: false,
  manualNativeErrors: process.argv.includes('--manual-native-errors') }
const logs = []
const nativeWindowEvents = []
const dialogResponseReceipts = []
let dialogResponseSequence = 0
const ledger = { hide: 0, quitRequests: 0, drain: 0, acceptedExit: 0, errorBoxes: [], savedPreferences: [], dialogWindows: [] }
const persistEvents = () => writeFileSync(join(profile, 'events.json'), JSON.stringify({ ledger, logs, nativeWindowEvents, dialogResponseReceipts }, null, 2))
// Observe real preload IPC before the production listener can close its sender.
// Recording only the production parser's accepted decisions excludes malformed
// messages; no fixture command fabricates a response or clicks a confirmation.
ipcMain.on('desktop:quit-dialog-response', (event, value) => {
  const response = parseDesktopQuitDialogResponse(value)
  if (!response) return
  dialogResponseReceipts.push({ sequence: ++dialogResponseSequence, webContentsId: event.sender.id, response })
  if (dialogResponseReceipts.length > 80) dialogResponseReceipts.shift()
  persistEvents()
})
const log = (event, detail = {}) => {
  logs.push({ event, ...detail })
  if (logs.length > 100) logs.shift()
  persistEvents()
}
const pageServer = createServer((_request, response) => {
  response.setHeader('content-type', 'text/html; charset=utf-8')
  response.end(`<!doctype html><title>Unsaved local page</title><style>body{font:16px 'Segoe UI',sans-serif;padding:28px;background:#eef2ff;color:#1e293b}textarea{display:block;width:90%;height:150px;margin-top:16px;font:inherit;padding:12px}p{max-width:650px}</style><h1>Unsaved local acceptance page</h1><p>This page is served only on this fixture's loopback server. Its textarea has a real beforeunload handler. Canceling quit must preserve the same document and value.</p><textarea aria-label="Unsaved note">Do not lose this draft — 验收草稿</textarea><script>window.addEventListener('beforeunload',event=>{event.preventDefault();event.returnValue='Unsaved fixture draft'})</script>`)
})
await new Promise(resolve => pageServer.listen(0, '127.0.0.1', resolve))
const pageOrigin = `http://127.0.0.1:${pageServer.address().port}`

function startTask() {
  if (task?.exitCode === null && task?.signalCode === null) return
  ticks = 0
  task = spawn(process.execPath, ['-e', `let n=0;setInterval(()=>process.send?.({tick:++n}),200);process.on('message',m=>{if(m==='stop')process.exit(0)})`], {
    env: { ...process.env, ELECTRON_RUN_AS_NODE: '1' }, stdio: ['ignore', 'ignore', 'pipe', 'ipc'], windowsHide: true,
  })
  task.on('message', value => { if (Number.isInteger(value?.tick)) ticks = value.tick })
  task.stderr.resume()
  task.on('error', error => log('synthetic_task_error', { error: String(error) }))
  if (env) env.gatewayProcess = task
}
async function stopTask() {
  const child = task
  if (!child || child.exitCode !== null || child.signalCode !== null) return
  const closed = once(child, 'close')
  try { child.send('stop', error => { if (error && child.exitCode === null) child.kill('SIGKILL') }) }
  catch { if (child.exitCode === null) child.kill('SIGKILL') }
  const timer = setTimeout(() => { if (child.exitCode === null) child.kill('SIGKILL') }, 2_000)
  try { await closed } finally { clearTimeout(timer) }
}
function taskAlive() { return Boolean(task && task.exitCode === null && task.signalCode === null) }
function pages() { return manager ? [...manager.surfaces.values()].filter(record => record.kind === 'url-preview' && !record.disposed) : [] }
function nativeWindowState(window) {
  if (window.isDestroyed()) return { id: window.id, destroyed: true }
  const parent = window.getParentWindow()
  const parentBounds = parent && !parent.isDestroyed() ? parent.getBounds() : null
  return { id: window.id, title: window.getTitle(), visible: window.isVisible(), focused: window.isFocused(),
    modal: window.isModal(), enabled: window.isEnabled(), opacity: window.getOpacity(), bounds: window.getBounds(),
    contentBounds: window.getContentBounds(), parentId: parent?.id ?? null, parentEnabled: parent?.isEnabled() ?? null,
    parentBounds, workArea: screen.getDisplayMatching(parentBounds ?? window.getBounds()).workArea }
}
function snapshot() {
  return { scope: 'isolated native fixture; synthetic task/Gateway adapters', profile,
    electronVersion: process.versions.electron, productionMainSha256,
    ...settings, resetting, resetGeneration, taskAlive: taskAlive(), taskPid: task?.pid ?? null, ticks, pages: pages().length,
    phase: env?.appExitPhase, isQuitting: env?.isQuitting, quitConfirmed: env?.quitConfirmed,
    writerActive: env?.desktopWriters.activeCount, writerClosed: env?.desktopWriters.closed,
    confirmationPending: env?.quitConfirmationPromise !== null,
    ownerVisible: Boolean(owner && !owner.isDestroyed() && owner.isVisible()), dialogCount, ledger, logs: logs.slice(-8),
    nativeWindows: BrowserWindow.getAllWindows().map(nativeWindowState), nativeWindowEvents: nativeWindowEvents.slice(-80),
    dialogResponseReceipts }
}
function renderOwner() {
  if (!owner || owner.isDestroyed() || owner.webContents.isDestroyed()) return
  const state = snapshot()
  void owner.webContents.executeJavaScript(`document.getElementById('status').textContent=${JSON.stringify(`Synthetic task ${state.taskAlive ? 'running' : 'stopped'} · tick ${ticks} · pages ${state.pages} · phase ${state.phase}\n${env?.gatewayState.error ?? ''}`)}`).catch(() => {})
}
function setPageBounds() {
  if (!owner || owner.isDestroyed()) return
  const [width, height] = owner.getContentSize()
  for (const record of pages()) manager.setSurfaceRect({ surfaceId: record.id, x: 24, y: 122,
    width: Math.max(100, width - 48), height: Math.max(100, height - 146), visible: record.id === pages().at(-1)?.id })
}
async function addPage() {
  if (!owner || owner.isDestroyed()) await makeOwner()
  const surfaceId = `acceptance-page-${++pageNumber}`
  const result = await manager.createSurface({ version: 2, surfaceId, kind: 'url-preview',
    payload: { url: `${pageOrigin}/${surfaceId}`, scopeId: 'pr1793-native-acceptance' } })
  if (!result.ok) throw new Error(result.message)
  manager.activateSurface(surfaceId)
  setPageBounds()
  return surfaceId
}
async function makeOwner() {
  owner = new BrowserWindow({ title: 'OpenSquilla — isolated task and page', x: 460, y: 80, width: 980, height: 720,
    show: true, webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true } })
  await owner.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(`<!doctype html><title>OpenSquilla — isolated task and page</title><style>body{font:14px 'Segoe UI',sans-serif;margin:24px;background:Canvas;color:CanvasText;color-scheme:light dark}h1{font-size:20px}pre{white-space:pre-wrap}</style><h1>OpenSquilla · native acceptance</h1><pre id="status">Synthetic task starting…</pre>`))
  owner.on('close', event => { if (!resetting && env) runInContext('handleMainWindowClose(mainWindow, nativeCloseEvent)', Object.assign(env, { mainWindow: owner, nativeCloseEvent: event })) })
  owner.on('resize', setPageBounds)
  manager = new NativeWorkbenchSurfaceManager({ getWindow: () => owner, emit: event => log(`page_${event.type}`, { surfaceId: event.surfaceId }) })
  if (env) { env.mainWindow = owner; env.nativeWorkbenchSurfaces = manager }
}
function makeEnvironment() {
  const shim = new EventEmitter()
  shim.quit = () => {
    ledger.quitRequests += 1
    let prevented = false
    shim.emit('before-quit', { preventDefault() { prevented = true } })
    if (!prevented) { ledger.acceptedExit += 1; log('fixture_ordinary_exit') }
  }
  shim.exit = code => { ledger.acceptedExit += 1; log('fixture_exit_committed', { code }) }
  const cleanupChild = async () => {
    ledger.drain += 1
    if (settings.cleanupFailure) { env.quitUnconfirmedProcessTrees.add(task); return false }
    await stopTask()
    env.quitUnconfirmedProcessTrees.clear()
    env.gatewayProcess = null
    return true
  }
  const fixtureDialog = { ...dialog, showErrorBox: (title, content) => {
    ledger.errorBoxes.push({ title, content, native: settings.manualNativeErrors })
    log('fixture_cleanup_error_box', { title, native: settings.manualNativeErrors })
    renderOwner()
    if (settings.manualNativeErrors) dialog.showErrorBox(title, content)
  } }
  env = createContext({ app: shim, process, BrowserWindow, ipcMain, screen, dialog: fixtureDialog, join, __dirname: distDir,
    buildDesktopQuitDialogHtml, parseDesktopQuitDialogResponse, DesktopQuitConfirmation, DesktopQuitBudget,
    mainWindowCloseAction, desktopQuitConfirmation: new DesktopQuitConfirmation(), desktopLocale: normalizeGatewayLocale(settings.locale),
    mainWindow: owner, mainWindowClosePrompt: null, windowsTray: {}, appExitPhase: 'running', isQuitting: false,
    systemSessionEnding: false, updateApplying: false, updateInstallHandoffReady: false,
    windowsSessionEndPreviousPhase: null, windowsSessionEndResetTimer: null,
    quitRequestedDuringUpdateDrain: false, quitGatewayDrainPromise: null, quitDeferredForDesktopWriters: false,
    quitWriterAdmission: null, quitFromSignal: false, quitConfirmed: false, quitConfirmationPromise: null,
    quitConfirmationWantsExit: false, quitUnconfirmedProcessTrees: new Set(),
    gatewayProcessCleanupContexts: new Map(), gatewayProcessCleanupOperations: new WeakMap(),
    desktopWriters: new DesktopWriterAdmission(), nativeWorkbenchSurfaces: manager,
    gatewayProcess: taskAlive() ? task : null, gatewayState: { status: 'ready', owned: true, url: 'synthetic://fixture', error: null },
    hasGatewayProcessExited: child => !child || child.exitCode !== null || child.signalCode !== null,
    liveLifecycleOwnedGatewayProcesses: () => taskAlive() ? [task] : [],
    quitOwnedGateway: cleanupChild, drainOwnedGatewayForLegacyQuit: cleanupChild,
    ownedGatewayRecord: () => ({ synthetic: true }),
    readVerifiedDesktopGatewayActivity: async () => settings.activity === 'unknown' ? { kind: 'unreachable' }
      : { kind: 'ok', activeCount: settings.activity === 'active' && taskAlive() ? 1 : 0 },
    desktopLog: log, setAppExitPhase: phase => { env.appExitPhase = phase; log('fixture_phase', { phase }) },
    desktopGatewayConnectionSuspendedForExit: () => ['draining', 'committed'].includes(env.appExitPhase),
    currentMainWindow: () => owner && !owner.isDestroyed() ? owner : null, currentOnboardingWindow: () => null,
    loadDesktopPreferencesRecord: () => ({ value: { main_window_close_behavior: settings.behavior } }),
    saveDesktopPreferences: async value => { settings.behavior = value.mainWindowCloseBehavior ?? settings.behavior;
      ledger.savedPreferences.push(value); writeFileSync(join(profile, 'preferences.json'), JSON.stringify(settings)); return value },
    hideMainWindow: window => { ledger.hide += 1; window.hide(); log('fixture_main_hidden') }, focusOnboardingWindow() {},
    waitForDesktopWriterOperations: () => env.desktopWriters.waitForAtMost(0),
    publishGatewayConnection: renderOwner, cancelGatewayUnexpectedExitRestart() {},
    desktopUpdateCheckScheduler: { stop() {} }, desktopReliabilityTelemetry: { finishSession() {} },
    artifactPreviewLeaseBroker: { clear() {}, shutdown: async () => {} }, desktopBrowser: { close: async () => {} },
    destroyWindowsTray() {}, stopGateway() {}, setQuitStatus: key => log('fixture_quit_status', { key }),
    setTimeout, clearTimeout, setImmediate, console,
  })
  runInContext(`globalThis.DESKTOP_MESSAGES=${messages};\n${required.map(name => functions.get(name)).join('\n')}\napp.on('before-quit',${beforeQuit});`, env)
}
async function command(action, options = {}) {
  if (resetting && action !== 'stop-fixture') throw new Error('The isolated session reset is still running.')
  if (action === 'configure') {
    Object.assign(settings, options)
    env.desktopLocale = normalizeGatewayLocale(settings.locale)
    nativeTheme.themeSource = settings.theme
    if (settings.activity === 'active') startTask()
  } else if (action === 'show-main') { if (!owner || owner.isDestroyed()) await makeOwner(); owner.show(); owner.focus() }
  else if (action === 'close-main') owner.close()
  else if (action === 'quit') env.app.quit()
  else if (action === 'repeat-quit') { for (let index = 0; index < 4; index += 1) env.app.quit() }
  else if (action === 'signal') env.shutdownFromSignal()
  else if (action === 'add-page') await addPage()
  else if (action === 'remove-pages') await manager.destroyAll()
  else if (action === 'navigate-page') { const record = pages().at(-1); if (record) await record.contents.loadURL(`${pageOrigin}/navigation-${Date.now()}`) }
  else if (action === 'hold-writer') { if (!finishWriter) finishWriter = env.desktopWriters.begin('isolated acceptance writer') }
  else if (action === 'finish-writer') { finishWriter?.(); finishWriter = null }
  else if (action === 'long-copy') {
    const copy = runInContext(`({title:desktopT('quit.title'),message:desktopT('quit.unknown'),detail:desktopT('quit.pagesDetail').replace('{count}','888888888'),keepRunning:desktopT('quit.keepRunning'),confirm:desktopT('quit.confirm')})`, env)
    void env.showDesktopQuitConfirmation({ ...copy, parent: owner }).then(result => log('fixture_long_copy_decision', { result }))
  } else if (action === 'reset') {
    resetting = true
    finishWriter?.(); finishWriter = null
    for (const window of BrowserWindow.getAllWindows()) if (window !== controller && window !== owner) window.destroy()
    await env.quitConfirmationPromise?.catch(() => {})
    await env.quitGatewayDrainPromise?.catch(() => {})
    await manager.destroyAll()
    await stopTask()
    if (owner && !owner.isDestroyed()) owner.destroy()
    settings.cleanupFailure = false
    settings.activity = 'active'
    ledger.hide = 0; ledger.quitRequests = 0; ledger.drain = 0; ledger.acceptedExit = 0; ledger.errorBoxes = []; ledger.savedPreferences = []
    dialogCount = 0; ledger.dialogWindows = []
    nativeWindowEvents.length = 0
    await makeOwner(); startTask(); makeEnvironment(); await addPage()
    resetting = false
    resetGeneration += 1
    log('fixture_reset_ready', { resetGeneration })
  } else if (action === 'stop-fixture') { await shutdown(); app.exit(0) }
  else throw new Error(`Unknown acceptance command: ${action}`)
  renderOwner()
  return snapshot()
}
async function shutdown() {
  if (stopped) return
  stopped = true
  resetting = true
  finishWriter?.()
  await manager?.destroyAll()
  await stopTask()
  pageServer.close()
}
app.on('before-quit', event => {
  if (stopped) return
  event.preventDefault()
  void shutdown().then(() => app.exit(0))
})
app.on('browser-window-created', (_event, window) => {
  for (const event of ['ready-to-show', 'show', 'hide', 'focus', 'blur', 'closed']) {
    window.on(event, () => {
      nativeWindowEvents.push({ event, ...nativeWindowState(window) })
      if (nativeWindowEvents.length > 100) nativeWindowEvents.shift()
      persistEvents()
    })
  }
  if (window.webContents.getURL()) return
  window.webContents.once('did-finish-load', () => {
    if (window === owner || window === controller || !window.webContents.getURL().startsWith('data:text/html')) return
    dialogCount += 1
    ledger.dialogWindows.push({ id: window.id, width: window.getSize()[0], height: window.getSize()[1] })
    log('fixture_production_dialog_opened', { id: window.id })
  })
})
void app.whenReady().then(async () => {
  Menu.setApplicationMenu(null)
  nativeTheme.themeSource = settings.theme
  await makeOwner(); startTask(); makeEnvironment(); await addPage()
  controller = new BrowserWindow({ title: 'OpenSquilla PR1793 Acceptance', x: 20, y: 40, width: 420, height: 860, show: true,
    webPreferences: { preload: join(fixtureDir, 'preload.cjs'), contextIsolation: true, nodeIntegration: false, sandbox: true } })
  ipcMain.handle('quit-acceptance:snapshot', event => {
    if (!controller || controller.isDestroyed()) return null
    if (event.sender !== controller.webContents) throw new Error('Unexpected acceptance IPC sender')
    return snapshot()
  })
  ipcMain.handle('quit-acceptance:command', (event, action, options) => {
    if (event.sender !== controller.webContents) throw new Error('Unexpected acceptance IPC sender')
    return command(action, options)
  })
  await controller.loadFile(join(fixtureDir, 'index.html'))
  controller.on('closed', () => { void shutdown().then(() => app.exit(0)) })
  setInterval(renderOwner, 250).unref()
  globalThis.__opensquillaQuitAcceptance = { command, snapshot, get owner() { return owner }, get controller() { return controller }, get env() { return env }, get manager() { return manager } }
  log('fixture_ready')
}).catch(error => {
  console.error('Native quit acceptance startup failed:', error)
  writeFileSync(join(profile, 'startup-error.log'), String(error?.stack ?? error))
  void shutdown().then(() => app.exit(1))
})
