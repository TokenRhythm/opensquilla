import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { EventEmitter } from 'node:events'
import { mkdir, mkdtemp, readFile, realpath, rm, stat, symlink, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, relative, resolve } from 'node:path'
import vm from 'node:vm'
import ts from 'typescript'
import { DesktopWriterAdmission } from '../dist/desktop-writer-admission.js'
import { mainWindowCloseAction } from '../dist/desktop-window-lifecycle.js'
import { WindowsUpdateCoordinator, WindowsUpdatePreparationError } from '../dist/windows-update-coordinator.js'
import { WindowsUpdateSecurityError } from '../dist/windows-update-security.js'
import { WindowsUpdateHandoffError, launchWindowsInstaller } from '../dist/windows-update-handoff.js'
import { UpdateChannelError, updateAssetUrl } from '../dist/update-channel.js'
import {
  createWindowsUpdateCacheDescriptor, loadWindowsUpdateCache, saveWindowsUpdateCache, verifyCachedInstaller,
} from '../dist/windows-update-cache.js'

// Extract the actual production functions, including recovery/cache logic. The
// VM supplies OS boundaries; it does not replace the code under test with a copy.
const source = await readFile(new URL('../src/main.ts', import.meta.url), 'utf8')
const parsed = ts.createSourceFile('main.ts', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS)
const names = [
  'windowsInstallerActionsSupported', 'windowsUpdateDownloadDirectory',
  'clearWindowsUpdateCache', 'publishVerifiedWindowsInstaller', 'restoreWindowsUpdateCache',
  'revalidateReadyWindowsInstaller', 'restoreDownloadedUpdateRetryState',
  'classifyDesktopUpdateError', 'desktopUpdateErrorMessage', 'applyWindowsInstaller',
  'downloadDesktopUpdate', 'showUpdateError', 'handleMainWindowClose',
]
const declarations = new Map(parsed.statements.filter(ts.isFunctionDeclaration)
  .filter((statement) => statement.name).map((statement) => [statement.name.text, statement]))
for (const name of names) assert.ok(declarations.has(name), `production function ${name} must still exist`)
const beforeQuitCallbacks = []
function collectBeforeQuit(node) {
  if (ts.isCallExpression(node) && ts.isPropertyAccessExpression(node.expression)
    && node.expression.expression.getText(parsed) === 'app' && node.expression.name.text === 'on'
    && ts.isStringLiteral(node.arguments[0]) && node.arguments[0].text === 'before-quit') {
    assert.ok(ts.isArrowFunction(node.arguments[1]) || ts.isFunctionExpression(node.arguments[1]))
    beforeQuitCallbacks.push(node.arguments[1].getText(parsed))
  }
  ts.forEachChild(node, collectBeforeQuit)
}
collectBeforeQuit(parsed)
assert.equal(beforeQuitCallbacks.length, 1, 'exercise the single actual production before-quit callback')
const extracted = names.map((name) => declarations.get(name).getText(parsed)).join('\n')
const compiled = ts.transpileModule(`${extracted}\nconst beforeQuit = ${beforeQuitCallbacks[0]};\nglobalThis.subject = { applyWindowsInstaller, restoreWindowsUpdateCache, windowsInstallerActionsSupported, downloadDesktopUpdate, revalidateReadyWindowsInstaller, beforeQuit, handleMainWindowClose };`,
  { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None } }).outputText

function deferred() {
  let resolve
  let reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

async function until(predicate, label) {
  for (let attempt = 0; attempt < 80; attempt += 1) {
    if (predicate()) return
    await new Promise(setImmediate)
  }
  assert.fail(`production update did not reach ${label}`)
}

function fixture(options = {}) {
  const candidate = { tag: 'v0.5.5', version: '0.5.5', installer: 'OpenSquilla-0.5.5-win-x64.exe',
    baseVersion: '0.5.5', prerelease: false, releaseUrl: 'https://github.com/TokenRhythm/opensquilla/releases/tag/v0.5.5', feed: 'latest.yml' }
  const descriptor = { schemaVersion: 1, tag: candidate.tag, version: candidate.version,
    installer: candidate.installer, bytes: 10, sha256: 'a'.repeat(64) }
  const installerPath = 'C:\\Synthetic\\update-downloads\\OpenSquilla-0.5.5-win-x64.exe'
  const runtime = new EventEmitter()
  const calls = { verify: 0, registry: 0, stops: 0, launches: 0, quit: 0, resumes: 0, saves: 0, loads: 0, unrefs: 0 }
  let live = [runtime]
  let stored = descriptor
  let child = null
  let spawned = false
  const events = []
  const state = { status: 'downloaded' }
  const globals = {
    process: { platform: 'win32', arch: 'x64', env: options.gateOff ? {} : { OPENSQUILLA_DESKTOP_ENABLE_WIN_INSTALL: '1' } },
    join, setImmediate,
    WindowsUpdateSecurityError, WindowsUpdateHandoffError, WindowsUpdatePreparationError, UpdateChannelError,
    updateAssetUrl,
    app: {
      getVersion: () => '0.5.4', getPath: (kind) => kind === 'exe' ? 'C:\\SyntheticApp\\OpenSquilla.exe' : 'C:\\Synthetic',
      quit: () => { assert.equal(spawned, true, 'the app may quit only after the real launch wrapper receives spawn'); calls.quit += 1 },
    },
    desktopUpdateManaged: () => true,
    desktopUpdateInstallMode: () => 'manual',
    desktopUpdateCandidate: candidate,
    desktopUpdateStatus: 'downloaded',
    verifiedManualInstallerPath: installerPath,
    windowsUpdateCacheDescriptor: descriptor,
    windowsUpdateCacheRestore: null,
    windowsUpdateCacheRestoreAttempted: true,
    windowsUpdateCoordinator: new WindowsUpdateCoordinator(1000),
    windowsUpdateRecoveryGeneration: 0,
    updateApplying: false, isQuitting: false, appExitPhase: 'running',
    updateInstallHandoffReady: false, updateDownloadInProgress: false,
    manualInstallerActionInProgress: false, updateGatewayShutdownProcess: null,
    downloadedUpdateVersion: null, quitRequestedDuringUpdateDrain: false,
    lastSuccessfulUpdateSource: 'oss',
    desktopWriters: new DesktopWriterAdmission(),
    desktopProfileKey: () => 'unchanged-profile',
    liveLifecycleOwnedGatewayProcesses: () => live,
    loadDesktopUpdatePersistence: () => {},
    loadWindowsUpdateCache: async () => { calls.loads += 1; return stored },
    saveWindowsUpdateCache: async (_directory, value) => { calls.saves += 1; stored = value },
    verifyCachedInstaller: async (...args) => {
      calls.verify += 1
      if (options.verify) return await options.verify(calls.verify, args, { candidate, descriptor, path: installerPath })
      return { candidate, descriptor, path: installerPath }
    },
    assertUnambiguousWindowsInstallation: async () => { calls.registry += 1 },
    stopOwnedGatewaysForUpdate: async () => {
      calls.stops += 1
      if (options.stop) await options.stop()
      live = []
      return true
    },
    launchWindowsInstaller: async (path) => {
      calls.launches += 1
      return await launchWindowsInstaller(path, { spawn: () => {
        child = new EventEmitter()
        child.unref = () => { calls.unrefs += 1 }
        if (!options.holdSpawn) queueMicrotask(() => {
          if (options.spawnError) child.emit('error', Object.assign(new Error('synthetic launch error'), { code: 'EACCES' }))
          else { spawned = true; child.emit('spawn') }
        })
        return child
      } })
    },
    createApplicationMenu: () => {}, createWindowsTray: () => {},
    desktopT: (key) => key,
    desktopLog: (name, detail) => { events.push({ name, detail }) },
    setAppExitPhase: (phase) => { globals.appExitPhase = phase },
    setDesktopUpdateState: (patch) => { Object.assign(state, patch); if (patch.status) globals.desktopUpdateStatus = patch.status; return { ...state } },
    openOrResumeDesktopApp: async () => { calls.resumes += 1 },
  }
  const context = vm.createContext(globals)
  vm.runInContext(compiled, context, { filename: 'production-windows-update-main.js' })
  return { context, subject: context.subject, calls, state, events,
    spawn: () => { assert.ok(child); spawned = true; child.emit('spawn') },
    get child() { return child }, get stored() { return stored }, get live() { return live } }
}

function electronEvent() {
  return { defaultPrevented: false, preventDefault() { this.defaultPrevented = true } }
}

function lifecycleFixture(options = {}) {
  const f = fixture(options)
  const calls = { quitEvents: [], exits: [], hidden: 0, normalDrains: 0, schedulerStops: 0 }
  const window = { hide: () => { calls.hidden += 1 } }
  Object.assign(f.context, {
    mainWindowCloseAction,
    systemSessionEnding: false,
    windowsTray: {},
    currentOnboardingWindow: () => null,
    loadDesktopPreferencesRecord: () => ({ value: { main_window_close_behavior: 'ask' } }),
    hideMainWindow: (target) => target.hide(),
    focusOnboardingWindow: () => { assert.fail('no onboarding exists in this lifecycle fixture') },
    promptForMainWindowClose: () => { assert.fail('close during a deferred/draining exit must not reopen a prompt') },
    desktopUpdateCheckScheduler: { stop: () => { calls.schedulerStops += 1 } },
    artifactPreviewLeaseBroker: { clear: () => {}, revokeAll: async () => {} },
    nativeWorkbenchSurfaces: { destroyAll: async () => {} },
    desktopBrowser: { close: async () => {} },
    destroyWindowsTray: () => { f.context.windowsTray = null },
    stopGateway: () => {},
    quitGatewayDrainPromise: null,
    quitDeferredForDesktopWriters: false,
    quitWriterAdmission: null,
    gatewayProcess: null,
    gatewayState: { owned: false },
    drainOwnedGatewayForQuit: async () => { calls.normalDrains += 1; return true },
    dialog: { showErrorBox: () => { assert.fail('synthetic normal quit drain must succeed') } },
    app: { ...f.context.app,
      quit: () => {
        f.calls.quit += 1
        const event = electronEvent()
        f.subject.beforeQuit(event)
        calls.quitEvents.push(event)
        return event
      },
      exit: (code) => { calls.exits.push(code) },
    },
  })
  return { ...f, lifecycleCalls: calls,
    get child() { return f.child },
    requestQuit: () => f.context.app.quit(),
    closeWindow: () => { const event = electronEvent(); f.subject.handleMainWindowClose(window, event); return event },
  }
}

const downloadBytes = Buffer.from('synthetic complete installer bytes; never executed')
const downloadDigest = createHash('sha256').update(downloadBytes).digest('hex')

function downloadFixture(userData, options = {}) {
  const f = fixture()
  const candidate = f.context.desktopUpdateCandidate
  const directory = join(userData, 'update-downloads')
  const lexicalPath = join(directory, candidate.installer)
  const calls = { signatures: 0, sources: 0, digests: 0, downloads: 0, revealed: [], states: [] }
  const updateState = f.context.setDesktopUpdateState
  Object.assign(f.context, {
    app: { ...f.context.app, getPath: (kind) => kind === 'exe' ? 'C:\\SyntheticApp\\OpenSquilla.exe' : userData },
    desktopUpdateSource: null,
    desktopUpdateStatus: 'available',
    windowsUpdateCacheDescriptor: null,
    windowsUpdateCacheRestoreAttempted: false,
    verifiedManualInstallerPath: null,
    desktopUpdateSnapshot: () => ({ ...f.state }),
    mockUpdateVersion: () => null,
    desktopUpdateCheckScheduler: { consumeManualRequest: () => false },
    checkForUpdates: async () => { assert.fail('the known candidate/cache needs no discovery request') },
    loadWindowsUpdateCache,
    saveWindowsUpdateCache: async (cacheDirectory, descriptor) => {
      await saveWindowsUpdateCache(cacheDirectory, descriptor)
      if (descriptor && options.replaceAfterSave) await writeFile(lexicalPath, Buffer.alloc(downloadBytes.length, 1))
    },
    verifyCachedInstaller: (cacheDirectory, descriptor, currentVersion, constraints = {}) => verifyCachedInstaller(
      cacheDirectory, descriptor, currentVersion, { ...constraints, verifySignature: async (path) => {
        calls.signatures += 1
        assert.equal(path, await realpath(lexicalPath), 'signature verification receives the canonical file path')
        assert.ok(await loadWindowsUpdateCache(directory), 'SHA-checked metadata must persist before OS signature verification')
        assert.notEqual(f.state.status, 'downloaded', 'a fresh installer cannot be published before its first signature succeeds')
        if (options.signatureError) throw new WindowsUpdateSecurityError(options.signatureError, 'synthetic signature failure')
      } },
    ),
    createWindowsUpdateCacheDescriptor,
    stat,
    chooseDesktopUpdateSource: async () => {
      calls.sources += 1
      assert.ok(!options.offline, 'offline restoration must not probe download sources')
      return { source: 'oss', fallbackUsed: false }
    },
    fetchCanonicalWindowsInstallerDigest: async () => {
      calls.digests += 1
      assert.ok(!options.offline, 'offline restoration must not fetch checksum metadata')
      return downloadDigest
    },
    downloadVerifiedWindowsInstallerWithFallback: async () => {
      calls.downloads += 1
      assert.ok(!options.offline, 'offline restoration must reuse downloaded bytes')
      await mkdir(directory, { recursive: true })
      await writeFile(lexicalPath, downloadBytes)
      return { path: lexicalPath, source: 'github', fallbackUsed: true }
    },
    rememberSuccessfulUpdateSource: (source) => { f.context.lastSuccessfulUpdateSource = source },
    shell: { showItemInFolder: (path) => { calls.revealed.push(path) } },
    setDesktopUpdateState: (patch) => { const state = updateState(patch); calls.states.push(state); return state },
  })
  f.context.setDesktopUpdateState({ status: 'available' })
  return { ...f, downloadCalls: calls, directory, lexicalPath }
}

{
  const f = fixture({ gateOff: true })
  assert.equal(f.subject.windowsInstallerActionsSupported(), false)
  await f.subject.applyWindowsInstaller()
  assert.equal(f.calls.verify, 0)
  assert.equal(f.calls.stops, 0)
  assert.equal(f.calls.launches, 0)
}

{
  const verification = deferred()
  const f = fixture({ verify: async (count, _args, verified) => {
    if (count === 1) await verification.promise
    return verified
  } })
  const first = f.subject.applyWindowsInstaller()
  await until(() => f.calls.verify === 1, 'initial verification')
  const duplicate = f.subject.applyWindowsInstaller()
  verification.resolve()
  await Promise.all([first, duplicate])
  assert.equal(f.calls.launches, 1)
  assert.equal(f.calls.quit, 1)
  assert.equal(f.calls.verify, 2, 'verify again after the Gateway drain before launching')
  assert.equal(f.calls.resumes, 0)
}

for (const code of ['signature_invalid', 'signature_unavailable']) {
  const f = fixture({ verify: async () => { throw new WindowsUpdateSecurityError(code, 'synthetic verifier failure') } })
  await f.subject.applyWindowsInstaller()
  assert.equal(f.calls.stops, 0, 'signature errors must leave the current Gateway running')
  assert.equal(f.live.length, 1)
  assert.equal(f.calls.launches, 0)
  assert.equal(f.calls.quit, 0)
  assert.equal(f.context.updateApplying, false)
  assert.equal(f.context.desktopWriters.closed, false)
  assert.equal(f.state.errorCode, code)
  assert.equal(f.state.status, code === 'signature_invalid' ? 'error' : 'downloaded')
  assert.equal(f.stored === null, code === 'signature_invalid')
}

{
  const drain = deferred()
  let replaced = false
  const f = fixture({ stop: () => drain.promise, verify: async (_count, _args, verified) => replaced ? null : verified })
  const update = f.subject.applyWindowsInstaller()
  await until(() => f.calls.stops === 1, 'Gateway drain')
  replaced = true
  drain.resolve()
  await update
  assert.equal(f.calls.launches, 0, 'a replacement during drain must not reach spawn')
  assert.equal(f.calls.quit, 0)
  assert.equal(f.calls.resumes, 1)
  assert.equal(f.context.isQuitting, false)
  assert.equal(f.state.errorCode, 'integrity_failed')
}

{
  const f = fixture({ spawnError: true })
  await f.subject.applyWindowsInstaller()
  assert.equal(f.calls.launches, 1)
  assert.equal(f.calls.quit, 0)
  assert.equal(f.calls.resumes, 1)
  assert.equal(f.context.desktopWriters.closed, false)
  assert.equal(f.context.isQuitting, false)
  assert.equal(f.context.updateApplying, false)
  assert.equal(f.state.status, 'downloaded')
  assert.equal(f.state.errorCode, 'install_failed')
  assert.ok(f.context.verifiedManualInstallerPath)
}

{
  const f = fixture({ holdSpawn: true })
  const update = f.subject.applyWindowsInstaller()
  await until(() => f.child !== null, 'installer process creation request')
  assert.equal(f.calls.quit, 0)
  assert.equal(f.context.updateInstallHandoffReady, false)
  assert.equal(f.context.desktopWriters.closed, true)
  f.spawn()
  await update
  assert.equal(f.calls.quit, 1)
  assert.equal(f.context.updateInstallHandoffReady, true)
  assert.equal(f.context.appExitPhase, 'committed')
  assert.equal(f.calls.resumes, 0)
}

{
  const verification = deferred()
  const f = fixture({ verify: async (_count, _args, verified) => { await verification.promise; return verified } })
  const update = f.subject.applyWindowsInstaller()
  await until(() => f.calls.verify === 1, 'verification before competing lifecycle')
  const foreign = f.context.desktopWriters.tryBeginExclusive('synthetic cleanup')
  assert.ok(foreign)
  f.context.isQuitting = true
  f.context.appExitPhase = 'draining'
  verification.resolve()
  await update
  assert.equal(f.context.isQuitting, true, 'update recovery must not reset another lifecycle owner')
  assert.equal(f.context.appExitPhase, 'draining')
  assert.equal(f.context.desktopWriters.hasOwner(foreign.admissionToken), true)
  assert.equal(f.context.updateApplying, false)
  assert.equal(f.calls.stops, 0)
  assert.equal(f.calls.launches, 0)
  assert.equal(f.calls.resumes, 0)
  foreign.finish()
  f.context.desktopWriters.reopen(foreign.admissionToken)
}

{
  const f = fixture({ verify: async (count, _args, verified) => {
    if (count === 1) throw new WindowsUpdateSecurityError('signature_unavailable', 'temporary OS verifier outage')
    return verified
  } })
  f.context.windowsUpdateCacheRestoreAttempted = false
  f.context.desktopUpdateCandidate = null
  f.context.verifiedManualInstallerPath = null
  f.context.desktopUpdateStatus = 'idle'
  await f.subject.restoreWindowsUpdateCache()
  assert.equal(f.calls.verify, 1)
  assert.ok(f.stored, 'an unavailable verifier must preserve the descriptor')
  await f.subject.restoreWindowsUpdateCache()
  assert.equal(f.calls.verify, 1, 'state polling must not repeatedly invoke the OS verifier')
  await f.subject.restoreWindowsUpdateCache(true)
  assert.equal(f.calls.verify, 2, 'explicit retry must recheck cached bytes without a feed request')
  assert.equal(f.context.desktopUpdateStatus, 'downloaded')
  assert.equal(f.context.desktopUpdateCandidate.version, '0.5.5')
  assert.equal(f.calls.launches, 0)
}

{
  const finalVerification = deferred()
  const f = fixture({ verify: async (count, _args, verified) => {
    if (count === 2) await finalVerification.promise
    return verified
  } })
  const update = f.subject.applyWindowsInstaller()
  await until(() => f.calls.verify === 2, 'final verification after Gateway drain')
  const foreignToken = f.context.desktopWriters.close('synthetic later lifecycle owner')
  finalVerification.resolve()
  await update
  assert.equal(f.calls.launches, 0, 'handoff invariants must be rechecked after final verification awaits')
  assert.equal(f.calls.quit, 0)
  assert.equal(f.context.desktopWriters.hasOwner(foreignToken), true)
  assert.equal(f.calls.resumes, 0)
  f.context.desktopWriters.reopen(foreignToken)
}

// Dispatch actual production before-quit and close handlers while the update
// awaits injected OS work. Electron app/window effects remain test boundaries.
{
  const verification = deferred()
  const f = lifecycleFixture({ verify: async () => {
    await verification.promise
    throw new WindowsUpdateSecurityError('signature_unavailable', 'synthetic delayed verification failure')
  } })
  const update = f.subject.applyWindowsInstaller()
  await until(() => f.calls.verify === 1, 'verification before user Quit')
  assert.equal(f.requestQuit().defaultPrevented, true)
  assert.equal(f.context.quitRequestedDuringUpdateDrain, true)
  assert.equal(f.context.appExitPhase, 'deferred')
  assert.equal(f.closeWindow().defaultPrevented, true)
  assert.equal(f.lifecycleCalls.hidden, 1)
  assert.equal(f.calls.stops, 0)
  assert.equal(f.lifecycleCalls.normalDrains, 0)
  assert.deepEqual(f.lifecycleCalls.exits, [])
  verification.resolve()
  await update
  assert.equal(f.context.quitRequestedDuringUpdateDrain, false, 'failed verification must consume the deferred user Quit')
  await until(() => f.lifecycleCalls.exits.length === 1, 'normal Gateway quit after verification failure')
  assert.equal(f.calls.stops, 0, 'a failed update verification must not use the update Gateway stop path')
  assert.equal(f.lifecycleCalls.normalDrains, 1, 'the remembered user Quit must run the ordinary quit drain')
  assert.deepEqual(f.lifecycleCalls.exits, [0])
  assert.equal(f.calls.resumes, 0, 'update recovery must respect user Quit instead of restarting the Gateway')
  assert.equal(f.calls.launches, 0)
  assert.equal(f.context.appExitPhase, 'committed')
}

{
  const drain = deferred()
  const f = lifecycleFixture({ stop: () => drain.promise, spawnError: true })
  const update = f.subject.applyWindowsInstaller()
  await until(() => f.calls.stops === 1, 'update Gateway drain before failed spawn')
  assert.equal(f.closeWindow().defaultPrevented, true)
  assert.equal(f.requestQuit().defaultPrevented, true)
  await f.subject.applyWindowsInstaller()
  assert.equal(f.calls.launches, 0)
  assert.deepEqual(f.lifecycleCalls.exits, [])
  drain.resolve()
  await update
  assert.equal(f.context.quitRequestedDuringUpdateDrain, false)
  await until(() => f.lifecycleCalls.quitEvents.some((event) => !event.defaultPrevented), 'remembered Quit after asynchronous spawn failure')
  assert.equal(f.calls.launches, 1)
  assert.equal(f.context.updateInstallHandoffReady, false, 'failed spawn cannot commit installer handoff')
  assert.equal(f.calls.resumes, 0, 'user Quit after a drain suppresses Gateway recovery')
  assert.equal(f.lifecycleCalls.normalDrains, 0, 'the completed update drain already joined the Gateway')
  assert.equal(f.context.appExitPhase, 'committed')
}

{
  const drain = deferred()
  const f = lifecycleFixture({ stop: () => drain.promise, holdSpawn: true })
  const update = f.subject.applyWindowsInstaller()
  await until(() => f.calls.stops === 1, 'Gateway drain before concurrent close and Quit')
  assert.equal(f.context.appExitPhase, 'draining')
  assert.equal(f.closeWindow().defaultPrevented, true)
  assert.equal(f.requestQuit().defaultPrevented, true)
  assert.equal(f.requestQuit().defaultPrevented, true)
  assert.equal(f.closeWindow().defaultPrevented, true)
  await f.subject.applyWindowsInstaller()
  assert.equal(f.calls.stops, 1)
  assert.equal(f.calls.launches, 0)
  assert.equal(f.lifecycleCalls.hidden, 2)
  drain.resolve()
  await until(() => f.child !== null, 'spawn event after deferred Quit')
  assert.equal(f.context.updateInstallHandoffReady, false)
  assert.ok(f.lifecycleCalls.quitEvents.every((event) => event.defaultPrevented), 'every pre-spawn Quit must remain deferred')
  assert.deepEqual(f.lifecycleCalls.exits, [])
  f.spawn()
  await update
  assert.equal(f.calls.launches, 1)
  assert.equal(f.lifecycleCalls.quitEvents.filter((event) => !event.defaultPrevented).length, 1)
  assert.equal(f.context.updateInstallHandoffReady, true)
  assert.equal(f.context.appExitPhase, 'committed')
  assert.equal(f.closeWindow().defaultPrevented, false, 'a committed installer handoff must allow the window to close')
  assert.equal(f.calls.resumes, 0)
  assert.equal(f.lifecycleCalls.normalDrains, 0)
}

// Exercise the real cache implementation beneath the extracted production
// download function. A Windows junction is an ancestor of userData, while the
// final cache directory remains a normal directory as required by the policy.
const temporaryRoot = await mkdtemp(join(tmpdir(), 'opensquilla-update-integration-'))
try {
  const realRoot = join(temporaryRoot, 'real')
  const aliasRoot = join(temporaryRoot, 'alias')
  await mkdir(realRoot)
  await symlink(realRoot, aliasRoot, process.platform === 'win32' ? 'junction' : 'dir')
  {
    const f = downloadFixture(join(aliasRoot, 'first-download'))
    await f.subject.downloadDesktopUpdate()
    const canonicalPath = await realpath(f.lexicalPath)
    assert.notEqual(canonicalPath, f.lexicalPath, 'the fixture must exercise an actual ancestor junction')
    assert.equal(f.context.verifiedManualInstallerPath, canonicalPath)
    assert.equal(f.state.status, 'downloaded')
    assert.equal(f.state.source, 'github')
    assert.equal(f.state.fallbackUsed, true)
    assert.equal(f.downloadCalls.downloads, 1)
    // Further verification is allowed in the downloaded state; only the
    // initial verification must precede publication.
    f.context.verifyCachedInstaller = (directory, descriptor, current, constraints) => verifyCachedInstaller(
      directory, descriptor, current, { ...constraints, verifySignature: async () => {} },
    )
    assert.equal(await f.subject.revalidateReadyWindowsInstaller(), canonicalPath)
    await f.subject.downloadDesktopUpdate()
    assert.deepEqual(f.downloadCalls.revealed, [canonicalPath])
    assert.equal(f.downloadCalls.downloads, 1, 'revealing a verified junction-backed cache must not download again')
  }
  {
    const userData = join(aliasRoot, 'unavailable-verifier')
    const failed = downloadFixture(userData, { signatureError: 'signature_unavailable' })
    await failed.subject.downloadDesktopUpdate()
    assert.equal(failed.state.status, 'error')
    assert.equal(failed.state.errorCode, 'signature_unavailable')
    assert.equal(failed.context.verifiedManualInstallerPath, null)
    assert.ok(await loadWindowsUpdateCache(failed.directory), 'a complete download must survive verifier unavailability')
    assert.ok(!failed.downloadCalls.states.some((state) => state.status === 'downloaded'))
    const restarted = downloadFixture(userData, { offline: true })
    restarted.context.desktopUpdateCandidate = null
    restarted.context.setDesktopUpdateState({ status: 'idle' })
    await restarted.subject.restoreWindowsUpdateCache()
    assert.equal(restarted.state.status, 'downloaded')
    assert.equal(restarted.context.verifiedManualInstallerPath, await realpath(failed.lexicalPath))
    assert.equal(restarted.downloadCalls.signatures, 1)
    assert.equal(restarted.downloadCalls.sources + restarted.downloadCalls.digests + restarted.downloadCalls.downloads, 0)
  }
  for (const signatureError of ['signature_invalid', null]) {
    const f = downloadFixture(join(aliasRoot, signatureError ?? 'replaced-before-verification'), {
      signatureError, replaceAfterSave: !signatureError,
    })
    await f.subject.downloadDesktopUpdate()
    assert.equal(f.state.status, 'error')
    assert.equal(f.state.errorCode, signatureError ?? 'integrity_failed')
    assert.equal(f.context.verifiedManualInstallerPath, null)
    assert.equal(await loadWindowsUpdateCache(f.directory), null, 'invalid completed downloads must lose their metadata')
    assert.ok(!f.downloadCalls.states.some((state) => state.status === 'downloaded'))
    assert.equal((await stat(f.lexicalPath)).size, downloadBytes.length, 'clearing the record must preserve the file')
    assert.equal(f.downloadCalls.signatures, signatureError ? 1 : 0)
  }
} finally {
  // Validate the exact generated temp target before recursive cleanup. Node
  // removes the junction itself rather than recursing through its destination.
  const cleanupRoot = resolve(temporaryRoot)
  const fromTemp = relative(resolve(tmpdir()), cleanupRoot)
  assert.ok(fromTemp && !fromTemp.startsWith('..') && !fromTemp.includes(':'))
  await rm(cleanupRoot, { recursive: true, force: true })
}

console.log('Windows update production-main integration checks passed (AST-extracted functions, injected OS boundaries; no installer executed).')
