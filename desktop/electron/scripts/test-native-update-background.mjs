import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import { readFile } from 'node:fs/promises'
import vm from 'node:vm'
import ts from '@typescript/typescript6'
import { UpdateCheckScheduler, isUpdateCheckAllowed } from '../dist/update-check-scheduler.js'
import { UpdateChannelError } from '../dist/update-channel.js'
import { WindowsUpdateSecurityError } from '../dist/windows-update-security.js'
import { WindowsUpdateHandoffError } from '../dist/windows-update-handoff.js'

// Execute the production scheduler, exact-feed checks, downloader and updater
// event handlers. Only Electron/electron-updater/network are controlled seams;
// this does not claim a signed macOS installation on Windows.
const source = await readFile(new URL('../src/main.ts', import.meta.url), 'utf8')
const parsed = ts.createSourceFile('main.ts', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS)
const declarations = new Map(parsed.statements.filter(ts.isFunctionDeclaration)
  .filter(node => node.name).map(node => [node.name.text, node.getText(parsed)]))
const names = [
  'desktopUpdateAutomaticDownloadSupported', 'desktopUpdateSnapshot', 'setDesktopUpdateState', 'publishDesktopUpdateState',
  'classifyDesktopUpdateError', 'classifyDesktopUpdateTelemetryError', 'desktopUpdateErrorMessage', 'showUpdateError',
  'downloadDesktopUpdate', 'runDesktopUpdateDownload', 'initAutoUpdater', 'nativeUpdateReadyFor',
  'checkNativeDesktopUpdate', 'downloadNativeDesktopUpdateWithFallback', 'alternateDesktopUpdateSource',
  'desktopUpdateCheckAllowed', 'runDesktopUpdateCheck', 'checkDesktopUpdateCandidate', 'checkForUpdates',
]
const scheduler = parsed.statements.find(node => ts.isVariableStatement(node)
  && node.declarationList.declarations.some(declaration => declaration.name.getText(parsed) === 'desktopUpdateCheckScheduler'))
const compiled = ts.transpileModule(`${names.map(name => {
  assert.ok(declarations.has(name), name)
  return declarations.get(name)
}).join('\n')}
${scheduler.getText(parsed)}
globalThis.subject = { checkForUpdates, downloadDesktopUpdate, desktopUpdateSnapshot };`, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None },
}).outputText

function fixture(options = {}) {
  const candidate = { tag: 'v0.5.6', version: '0.5.6', releaseUrl: 'https://github.com/TokenRhythm/opensquilla/releases/tag/v0.5.6' }
  const calls = { feeds: [], checks: 0, downloads: 0, installs: 0, states: [], telemetry: [] }
  const updater = new EventEmitter()
  Object.assign(updater, {
    checkForUpdates: async () => {
      calls.checks += 1
      return { isUpdateAvailable: true, updateInfo: { version: options.feedVersion ?? candidate.version } }
    },
    downloadUpdate: async () => {
      calls.downloads += 1
      if (options.downloadGate) await options.downloadGate
      if (options.failFirst && calls.downloads === 1) throw new Error('synthetic first source offline')
      if (options.downloadError) throw new Error('synthetic download offline')
      updater.emit('download-progress', { percent: 100 })
      if (options.verifyGate) await options.verifyGate
      updater.emit('update-downloaded', { version: options.downloadedVersion ?? candidate.version })
    },
    quitAndInstall: () => { calls.installs += 1 },
  })
  const globals = {
    process: { platform: 'darwin', arch: 'arm64', env: {} },
    app: { getVersion: () => '0.5.4' },
    BrowserWindow: { getAllWindows: () => [{ isDestroyed: () => false, webContents: {
      send: (_channel, state) => calls.states.push(state),
    } }] },
    autoUpdater: updater, autoUpdaterReady: false,
    UpdateCheckScheduler, isUpdateCheckAllowed, UpdateChannelError, WindowsUpdateSecurityError, WindowsUpdateHandoffError,
    UPDATE_CHECK_REPEAT_DELAY_MS: 86400000,
    desktopUpdateManaged: () => true, desktopUpdateInstallMode: () => 'native',
    nativeAutoUpdateEnabled: () => true, autoUpdateSupported: () => true, macUpdateLocationOk: () => true,
    windowsInstallerActionsSupported: () => false, mockUpdateVersion: () => null,
    restoreWindowsUpdateCache: async () => {}, activeDesktopUpdateSnoozeFor: () => null,
    clearDesktopUpdateSnoozeIfVersionChanged: () => {}, createApplicationMenu: () => {},
    desktopUpdateStatus: 'idle', desktopUpdateLatestVersion: null, desktopUpdateProgress: null,
    desktopUpdateCheckedAt: null, desktopUpdateError: null, desktopUpdateErrorCode: null,
    desktopUpdateReleaseUrl: null, desktopUpdateSource: null, desktopUpdateFallbackUsed: false,
    desktopUpdateCandidate: null, nativeUpdateReady: null, downloadedUpdateVersion: null,
    verifiedManualInstallerPath: null, windowsUpdateCacheRestore: null,
    desktopUpdateDownloadTask: null, desktopUpdateDownloadOrigin: null,
    updateApplying: false, updateDownloadInProgress: false, manualInstallerActionInProgress: false,
    isQuitting: false, desktopWriters: { closed: false }, mockDownloadedUpdate: false,
    resolveDesktopUpdate: async () => ({ candidate, source: 'oss', fallbackUsed: false }),
    configureDesktopUpdateFeed: resolved => calls.feeds.push(resolved.source),
    probeDesktopUpdateSource: async () => {}, rememberSuccessfulUpdateSource: () => {},
    desktopReliabilityTelemetry: { recordUpdateResult: event => calls.telemetry.push(event) },
    desktopT: key => key, desktopLog: () => {}, console: { log() {}, warn() {}, error() {} },
  }
  const context = vm.createContext(globals)
  vm.runInContext(compiled, context)
  return { context, calls, updater, candidate, subject: context.subject, state: () => context.subject.desktopUpdateSnapshot() }
}

let passed = 0
async function check(name, run) { await run(); passed += 1; console.log(`PASS ${name}`) }

await check('native discovery automatically prepares the exact release without automatic install', async () => {
  const f = fixture()
  await f.subject.checkForUpdates(false)
  await f.context.desktopUpdateDownloadTask
  assert.equal(f.state().status, 'downloaded')
  assert.equal(f.state().releaseNotesUrl, f.candidate.releaseUrl)
  assert.equal(f.state().canAutoDownload, true)
  assert.equal(f.calls.downloads, 1)
  assert.equal(f.calls.installs, 0)
  assert.equal(f.updater.autoDownload, false, 'OpenSquilla owns scheduling after feed verification')
  assert.equal(f.updater.autoInstallOnAppQuit, false)
})
await check('native automatic preparation retains controlled source fallback', async () => {
  const f = fixture({ failFirst: true })
  await f.subject.checkForUpdates(false)
  await f.context.desktopUpdateDownloadTask
  assert.equal(f.state().status, 'downloaded')
  assert.equal(f.state().fallbackUsed, true)
  assert.equal(f.calls.downloads, 2)
  assert.deepEqual(f.calls.feeds, ['oss', 'github'])
})
await check('wrong native feed version never begins download or publishes readiness', async () => {
  const f = fixture({ feedVersion: '9.9.9' })
  await f.subject.checkForUpdates(false)
  assert.equal(f.calls.downloads, 0)
  assert.equal(f.calls.states.some(state => state.status === 'downloaded'), false)
  assert.equal(f.state().releaseNotesUrl, null)
})
await check('wrong native downloaded version cannot become ready or report download success', async () => {
  const f = fixture({ downloadedVersion: '9.9.9' })
  await f.subject.checkForUpdates(false)
  await f.context.desktopUpdateDownloadTask
  assert.equal(f.state().status, 'error')
  assert.equal(f.state().errorCode, 'manifest_invalid')
  assert.equal(f.calls.states.some(state => state.status === 'downloaded'), false)
  assert.equal(f.calls.telemetry.some(event => event.updateStage === 'download' && event.outcome === 'success'), false)
})
await check('native 100 percent progress does not imply readiness before verified downloaded event', async () => {
  let release
  const verifyGate = new Promise(resolve => { release = resolve })
  const f = fixture({ verifyGate })
  await f.subject.checkForUpdates(false)
  while (f.state().progress !== 100) await new Promise(setImmediate)
  assert.equal(f.state().status, 'downloading')
  const task = f.context.desktopUpdateDownloadTask
  assert.equal(f.subject.downloadDesktopUpdate(), task)
  release()
  await task
  assert.equal(f.state().status, 'downloaded')
  assert.equal(f.calls.downloads, 1)
})
await check('background download failure retains diagnostics and clears its flight for a later retry', async () => {
  const options = { downloadError: true }
  const f = fixture(options)
  await f.subject.checkForUpdates(false)
  await f.context.desktopUpdateDownloadTask
  assert.equal(f.state().status, 'error')
  assert.equal(f.context.desktopUpdateDownloadTask, null)
  assert.equal(f.context.desktopUpdateDownloadOrigin, null)
  assert.equal(f.calls.installs, 0)
  options.downloadError = false
  await f.subject.checkForUpdates(false)
  await f.context.desktopUpdateDownloadTask
  assert.equal(f.state().status, 'downloaded')
})
console.log(`Native background update checks passed: ${passed} (production functions; controlled updater boundary, no installation).`)
