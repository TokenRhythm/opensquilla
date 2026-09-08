// Test-only Electron host. The runner inserts unmodified production declarations
// and IPC/exit registrations below; it never edits the shipped main process.
import { app, BrowserWindow, ipcMain, protocol, net, dialog } from 'electron'
import { spawn } from 'node:child_process'
import { once } from 'node:events'
import { appendFileSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'

const config = JSON.parse(readFileSync(process.env.OPENSQUILLA_TEST_FIXTURE_CONFIG, 'utf8'))
const moduleAt = name => import(pathToFileURL(join(config.packageRoot, 'dist', name)).href)
const { DesktopWriterAdmission } = await moduleAt('desktop-writer-admission.js')
const { mainWindowCloseAction } = await moduleAt('desktop-window-lifecycle.js')
const { WindowsUpdateCoordinator, WindowsUpdatePreparationError } = await moduleAt('windows-update-coordinator.js')
const { WindowsUpdateSecurityError } = await moduleAt('windows-update-security.js')
const { WindowsUpdateHandoffError, launchWindowsInstaller: realLaunchWindowsInstaller } = await moduleAt('windows-update-handoff.js')
const { loadWindowsUpdateCache, saveWindowsUpdateCache, verifyCachedInstaller: realVerifyCachedInstaller } = await moduleAt('windows-update-cache.js')
const { UpdateChannelError, updateAssetUrl } = await moduleAt('update-channel.js')
const { isDesktopRendererDocumentUrl } = await moduleAt('desktop-renderer-protocol.js')

const counts = { signatures: 0, registry: 0, stops: 0, launches: 0, resumes: 0 }
let mode = { signature: 'ok', launch: 'missing', holdVerification: false, holdDrain: false }
let releaseVerification = null
let releaseDrain = null
const owned = new Set()
let mainWindow = null
let gatewayProcess = null
const gatewayState = { owned: true, url: '' }
let isQuitting = false
let appExitPhase = 'running'
let updateApplying = false
let updateInstallHandoffReady = false
let updateDownloadInProgress = false
let manualInstallerActionInProgress = false
let updateGatewayShutdownProcess = null
let quitRequestedDuringUpdateDrain = false
let quitGatewayDrainPromise = null
let quitDeferredForDesktopWriters = false
let quitWriterAdmission = null
const systemSessionEnding = false
let windowsTray = {}
const desktopWriters = new DesktopWriterAdmission()
const windowsUpdateCoordinator = new WindowsUpdateCoordinator(5000)
let windowsUpdateRecoveryGeneration = 0
let windowsUpdateCacheDescriptor = null
let windowsUpdateCacheRestore = null
let windowsUpdateCacheRestoreAttempted = false
let verifiedManualInstallerPath = null
let desktopUpdateCandidate = null
let desktopUpdateStatus = 'idle'
let desktopUpdateLatestVersion = null
let downloadedUpdateVersion = null
let desktopUpdateProgress = null
let desktopUpdateCheckedAt = null
let desktopUpdateError = null
let desktopUpdateErrorCode = null
let desktopUpdateReleaseUrl = null
let desktopUpdateSource = null
let desktopUpdateFallbackUsed = false
const lastSuccessfulUpdateSource = 'oss'
const desktopLocale = 'en'

// These are explicit test boundaries, not a production verifier bypass. Every
// executable cache still uses the real canonical metadata, size and SHA checks.
const desktopUpdateManaged = () => true
const desktopUpdateInstallMode = () => 'manual'
const mockUpdateVersion = () => null
const nativeAutoUpdateEnabled = () => false
const loadDesktopUpdatePersistence = () => {}
const activeDesktopUpdateSnoozeFor = () => null
const clearDesktopUpdateSnoozeIfVersionChanged = () => {}
const desktopProfileKey = () => 'synthetic-fixture-profile'
const currentMainWindow = () => mainWindow && !mainWindow.isDestroyed() ? mainWindow : null
const currentOnboardingWindow = () => null
const loadDesktopPreferencesRecord = () => ({ value: { main_window_close_behavior: 'ask' } })
const hideMainWindow = window => window.hide()
const focusOnboardingWindow = () => { throw new Error('unexpected onboarding in test fixture') }
const promptForMainWindowClose = () => { throw new Error('unexpected close prompt during update') }
const createApplicationMenu = () => {}
const createWindowsTray = () => { windowsTray = {} }
const destroyWindowsTray = () => { windowsTray = null }
const desktopUpdateCheckScheduler = { stop: () => {} }
const artifactPreviewLeaseBroker = { clear: () => {}, revokeAll: async () => {} }
const nativeWorkbenchSurfaces = { destroyAll: async () => {} }
const desktopArtifactBridgeLoopback = { close: async () => {} }
const desktopT = key => ({
  'update.signatureUnavailable': 'Windows could not verify the installer signature. Try again.',
  'update.signatureInvalid': 'The installer signature is invalid. Download it again.',
  'update.installFailed': 'The installer could not start. Try again.',
}[key] ?? key)

function desktopLog(event, detail = {}) {
  appendFileSync(config.logPath, `${JSON.stringify({ event, ...detail, at: Date.now() })}\n`)
}
function setAppExitPhase(phase, reason) {
  desktopLog('phase', { from: appExitPhase, to: phase, reason })
  appExitPhase = phase
}
function hasGatewayProcessExited(child) { return child.exitCode !== null || child.signalCode !== null }
function liveLifecycleOwnedGatewayProcesses() { return [...owned].filter(child => !hasGatewayProcessExited(child)) }
async function startSyntheticGateway() {
  const child = spawn(config.nodePath, ['-e', 'process.stdin.resume()'], {
    shell: false, windowsHide: true, stdio: ['pipe', 'ignore', 'ignore'],
  })
  await once(child, 'spawn')
  owned.add(child)
  gatewayProcess = child
  child.once('exit', () => { owned.delete(child); desktopLog('gateway_exit', { pid: child.pid }) })
  desktopLog('gateway_spawn', { pid: child.pid })
}
async function stopSyntheticGateway(child) {
  if (hasGatewayProcessExited(child)) return true
  const exit = once(child, 'exit')
  child.stdin.end()
  await exit
  return true
}
const stopGateway = () => { for (const child of owned) child.stdin.end() }
const drainOwnedGatewayForQuit = child => stopSyntheticGateway(child)
async function openOrResumeDesktopApp() {
  counts.resumes += 1
  await startSyntheticGateway()
  mainWindow?.show()
}
async function verifyCachedInstaller(directory, descriptor, currentVersion, options = {}) {
  return realVerifyCachedInstaller(directory, descriptor, currentVersion, {
    ...options,
    verifySignature: async () => {
      counts.signatures += 1
      if (mode.holdVerification) await new Promise(resolve => { releaseVerification = resolve })
      if (mode.signature !== 'ok') throw new WindowsUpdateSecurityError(mode.signature, 'test-only signature boundary')
    },
  })
}
async function assertUnambiguousWindowsInstallation() { counts.registry += 1 }
async function stopOwnedGatewaysForUpdate() {
  counts.stops += 1
  if (mode.holdDrain) await new Promise(resolve => { releaseDrain = resolve })
  return (await Promise.all([...owned].map(stopSyntheticGateway))).every(Boolean)
}
async function launchWindowsInstaller() {
  counts.launches += 1
  const executable = mode.launch === 'node' ? config.nodePath : join(config.isolationRoot, 'missing-installer.exe')
  // The only real success child is node.exe --updated (no script, no NSIS).
  await realLaunchWindowsInstaller(executable)
  desktopLog('synthetic_spawn_succeeded', { executable })
}

// PRODUCTION_DECLARATIONS

protocol.registerSchemesAsPrivileged([{ scheme: 'opensquilla-app', privileges: { standard: true, secure: true, supportFetchAPI: true } }])
app.name = 'OpenSquilla update lifecycle test'
// Playwright's Electron loader attaches before releasing app.whenReady().
// Return from the main module instead of top-level-awaiting that handshake.
void app.whenReady().then(async () => {
protocol.handle('opensquilla-app', request => {
  const path = new URL(request.url).pathname
  const asset = path.startsWith('/assets/') ? path.slice(1) : 'renderer.html'
  return net.fetch(pathToFileURL(join(config.rendererRoot, asset)).href)
})
await startSyntheticGateway()
mainWindow = new BrowserWindow({
  width: 1000, height: 720, show: true,
  webPreferences: { preload: join(config.packageRoot, 'dist', 'preload.cjs'), contextIsolation: true, nodeIntegration: false, sandbox: true },
})
mainWindow.on('close', event => handleMainWindowClose(mainWindow, event))
await mainWindow.loadURL('opensquilla-app://desktop/chat')
await restoreWindowsUpdateCache()
globalThis.windowsUpdateFixture = {
  configure: patch => { Object.assign(mode, patch) },
  releaseVerification: () => { const release = releaseVerification; releaseVerification = null; mode.holdVerification = false; release?.() },
  releaseDrain: () => { const release = releaseDrain; releaseDrain = null; mode.holdDrain = false; release?.() },
  snapshot: () => ({
    counts, state: desktopUpdateSnapshot(), appExitPhase, updateApplying, updateInstallHandoffReady,
    quitRequestedDuringUpdateDrain, gatewayPids: liveLifecycleOwnedGatewayProcesses().map(child => child.pid),
    userData: app.getPath('userData'), home: process.env.HOME,
    runtime: { electron: process.versions.electron, node: process.versions.node, platform: process.platform, arch: process.arch },
    verificationHeld: releaseVerification !== null, drainHeld: releaseDrain !== null,
    visible: mainWindow?.isVisible(), destroyed: mainWindow?.isDestroyed(),
  }),
  show: () => mainWindow.show(),
  closeWindow: () => mainWindow.close(),
}
app.on('before-quit', event => desktopLog('before_quit_observed', {
  prevented: event.defaultPrevented, handoffReady: updateInstallHandoffReady,
}))
app.on('will-quit', () => desktopLog('will_quit', { gatewayCount: liveLifecycleOwnedGatewayProcesses().length }))
}).catch(error => { console.error(error); app.exit(1) })
