import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { mkdir, mkdtemp, readFile, realpath, rm, stat, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, relative, resolve } from 'node:path'
import vm from 'node:vm'
import ts from 'typescript'
import { DesktopWriterAdmission } from '../dist/desktop-writer-admission.js'
import { UpdateCheckScheduler, isUpdateCheckAllowed } from '../dist/update-check-scheduler.js'
import { UpdateChannelError, updateAssetUrl } from '../dist/update-channel.js'
import { WindowsUpdateSecurityError } from '../dist/windows-update-security.js'
import { WindowsUpdateHandoffError } from '../dist/windows-update-handoff.js'
import { WindowsUpdatePreparationError } from '../dist/windows-update-coordinator.js'
import {
  createWindowsUpdateCacheDescriptor, loadWindowsUpdateCache, saveWindowsUpdateCache, verifyCachedInstaller,
} from '../dist/windows-update-cache.js'

// Production main functions and scheduler wiring run unchanged. Only channel,
// signature and Electron boundaries are supplied; cache bytes/metadata are real
// isolated files. Nothing here signs, launches an installer, or opens a profile.
const source = await readFile(new URL('../src/main.ts', import.meta.url), 'utf8')
const parsed = ts.createSourceFile('main.ts', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS)
const declarations = new Map(parsed.statements.filter(ts.isFunctionDeclaration)
  .filter((statement) => statement.name).map((statement) => [statement.name.text, statement]))
const names = [
  'windowsInstallerActionsSupported', 'windowsUpdateDownloadDirectory', 'clearWindowsUpdateCache',
  'publishVerifiedWindowsInstaller', 'restoreWindowsUpdateCache', 'revalidateReadyWindowsInstaller',
  'activeDesktopUpdateSnoozeFor', 'clearDesktopUpdateSnoozeIfVersionChanged',
  'desktopUpdateSnapshot', 'publishDesktopUpdateState', 'setDesktopUpdateState', 'dismissDesktopUpdate',
  'classifyDesktopUpdateError', 'desktopUpdateErrorMessage', 'showUpdateError', 'downloadDesktopUpdate',
  'desktopUpdateCheckAllowed', 'runDesktopUpdateCheck', 'checkForUpdates', 'applyWindowsInstaller',
]
for (const name of names) assert.ok(declarations.has(name), `production function ${name} must exist`)
const schedulerStatement = parsed.statements.find((statement) => ts.isVariableStatement(statement)
  && statement.declarationList.declarations.some((declaration) => declaration.name.getText(parsed) === 'desktopUpdateCheckScheduler'))
assert.ok(schedulerStatement, 'extract the production scheduler wiring, not a test replacement')
const compiled = ts.transpileModule(
  `${names.map((name) => declarations.get(name).getText(parsed)).join('\n')}
   ${schedulerStatement.getText(parsed)}
   globalThis.subject = { restoreWindowsUpdateCache, checkForUpdates, dismissDesktopUpdate,
     downloadDesktopUpdate, applyWindowsInstaller, desktopUpdateSnapshot, desktopUpdateCheckAllowed };`,
  { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None } },
).outputText

function candidate(version) {
  return { tag: `v${version}`, version, installer: `OpenSquilla-${version}-win-x64.exe`,
    baseVersion: version, prerelease: false, releaseUrl: `https://github.com/TokenRhythm/opensquilla/releases/tag/v${version}`,
    feed: 'latest.yml' }
}
const B = candidate('0.5.5')
const C = candidate('0.5.6')
const bytes = Buffer.from('Synthetic cached Windows installer, never executed.')
const digest = createHash('sha256').update(bytes).digest('hex')
const descriptorB = createWindowsUpdateCacheDescriptor(B, digest, bytes.length)
const root = await mkdtemp(join(tmpdir(), 'opensquilla-update-refresh-'))
let sequence = 0
let passed = 0

function deferred() {
  let resolvePromise
  const promise = new Promise((resolveValue) => { resolvePromise = resolveValue })
  return { promise, resolve: resolvePromise }
}

async function fixture(options = {}) {
  const userData = options.userData ?? join(root, `case-${++sequence}`)
  const directory = join(userData, 'update-downloads')
  if (!options.userData) {
    await mkdir(directory, { recursive: true })
    await writeFile(join(directory, B.installer), bytes)
    await saveWindowsUpdateCache(directory, descriptorB)
  }
  const cachedPath = await realpath(join(directory, B.installer))
  const calls = { channel: 0, signatures: 0, downloads: 0, checksumFetches: 0, reveals: [], coordinator: 0, launches: 0 }
  const events = []
  const persisted = options.persisted ?? {}
  const network = { candidate: options.offered === undefined ? C : options.offered, offline: false, hold: null, entered: null }
  const signature = { error: null, hold: null, entered: null }
  const download = { hold: null, entered: null }
  const globals = {
    process: { platform: options.platform ?? 'win32', arch: 'x64', env: { OPENSQUILLA_DESKTOP_ENABLE_WIN_INSTALL: '0' } },
    join, stat, setTimeout, setImmediate, AbortController,
    UpdateCheckScheduler, isUpdateCheckAllowed, WindowsUpdateSecurityError, WindowsUpdateHandoffError,
    WindowsUpdatePreparationError, UpdateChannelError, updateAssetUrl, createWindowsUpdateCacheDescriptor,
    app: { getVersion: () => '0.5.4', getPath: (kind) => kind === 'exe' ? join(userData, 'OpenSquilla.exe') : userData },
    BrowserWindow: { getAllWindows: () => [] },
    UPDATE_SNOOZE_MS: 86400000, UPDATE_CHECK_REPEAT_DELAY_MS: 86400000,
    desktopUpdateManaged: () => true, desktopUpdateInstallMode: () => options.native ? 'native' : 'manual',
    nativeAutoUpdateEnabled: () => Boolean(options.native), mockUpdateVersion: () => null,
    autoUpdateSupported: () => Boolean(options.native), macUpdateLocationOk: () => true,
    desktopUpdateStatus: 'idle', desktopUpdateLatestVersion: null, desktopUpdateProgress: null,
    desktopUpdateCheckedAt: null, desktopUpdateError: null, desktopUpdateErrorCode: null,
    desktopUpdateReleaseUrl: null, desktopUpdateSource: null, desktopUpdateFallbackUsed: false,
    desktopUpdateSnoozedVersion: null, desktopUpdateSnoozedUntil: null,
    desktopUpdateCandidate: null, nativeUpdateReady: null, downloadedUpdateVersion: null,
    verifiedManualInstallerPath: null, windowsUpdateCacheDescriptor: null,
    windowsUpdateCacheRestore: null, windowsUpdateCacheRestoreAttempted: false,
    updateApplying: false, updateDownloadInProgress: false, manualInstallerActionInProgress: false,
    isQuitting: false, appExitPhase: 'running', desktopWriters: new DesktopWriterAdmission(),
    lastSuccessfulUpdateSource: 'oss', mockDownloadedUpdate: false,
    windowsUpdateCoordinator: { run: async () => { calls.coordinator += 1; assert.fail('a stale candidate reached installer preparation') } },
    windowsUpdateRecoveryGeneration: 0, desktopProfileKey: () => 'synthetic-profile',
    liveLifecycleOwnedGatewayProcesses: () => [],
    launchWindowsInstaller: async () => { calls.launches += 1; assert.fail('refresh must never execute an installer') },
    loadDesktopUpdatePersistence: () => {
      if (globals.desktopUpdatePersistenceLoaded) return
      globals.desktopUpdatePersistenceLoaded = true
      globals.desktopUpdateSnoozedVersion = persisted.version ?? null
      globals.desktopUpdateSnoozedUntil = persisted.until ?? null
    },
    persistDesktopUpdateState: async () => {
      persisted.version = globals.desktopUpdateSnoozedVersion
      persisted.until = globals.desktopUpdateSnoozedUntil
    },
    loadWindowsUpdateCache, saveWindowsUpdateCache,
    verifyCachedInstaller: (cacheDirectory, descriptor, version, constraints = {}) => verifyCachedInstaller(
      cacheDirectory, descriptor, version, { ...constraints, verifySignature: async (path) => {
        calls.signatures += 1
        signature.entered?.resolve()
        if (signature.hold) await signature.hold.promise
        if (signature.error) throw new WindowsUpdateSecurityError(signature.error, 'synthetic signature refusal')
        assert.ok(path.startsWith(directory), 'signature boundary stays inside this synthetic cache')
      } },
    ),
    resolveDesktopUpdate: async () => {
      calls.channel += 1
      network.entered?.resolve()
      if (network.hold) await network.hold.promise
      if (network.offline) throw new UpdateChannelError('source_unreachable', 'synthetic offline channel')
      return network.candidate ? { candidate: network.candidate, source: 'oss', fallbackUsed: false } : null
    },
    chooseDesktopUpdateSource: async () => ({ source: 'oss', fallbackUsed: false }),
    fetchCanonicalWindowsInstallerDigest: async () => { calls.checksumFetches += 1; return digest },
    downloadVerifiedWindowsInstallerWithFallback: async (offered) => {
      calls.downloads += 1
      download.entered?.resolve()
      if (download.hold) await download.hold.promise
      const path = join(directory, offered.installer)
      await writeFile(path, bytes)
      return { path, source: 'oss', fallbackUsed: false }
    },
    rememberSuccessfulUpdateSource: (value) => { globals.lastSuccessfulUpdateSource = value },
    shell: { showItemInFolder: (path) => { calls.reveals.push(path) } },
    createApplicationMenu: () => {}, desktopT: (key) => key,
    desktopLog: (name, details) => { events.push({ name, details }) },
    console: { error: (...args) => { events.push({ name: 'console-error', args }) }, warn: () => {}, log: () => {} },
  }
  const context = vm.createContext(globals)
  vm.runInContext(compiled, context, { filename: 'production-windows-update-refresh.js' })
  return { context, subject: context.subject, calls, network, signature, download, events, directory, cachedPath, userData, persisted,
    state: () => context.subject.desktopUpdateSnapshot() }
}

async function restored(options) {
  const f = await fixture(options)
  await f.subject.restoreWindowsUpdateCache()
  assert.equal(f.state().status, 'downloaded')
  assert.equal(f.context.verifiedManualInstallerPath, f.cachedPath)
  assert.equal(f.state().canInstall, false, 'cache refresh remains available with the handoff explicitly disabled')
  return f
}

async function mustNotApplyOldInstaller(f) {
  f.context.process.env.OPENSQUILLA_DESKTOP_ENABLE_WIN_INSTALL = '1'
  await f.subject.applyWindowsInstaller()
  assert.equal(f.calls.coordinator, 0)
  assert.equal(f.calls.launches, 0)
}

async function mustNotRestoreRetiredInstaller(f) {
  assert.equal(await loadWindowsUpdateCache(f.directory), null, 'retired candidate metadata is removed from disk')
  const rebooted = await fixture({ userData: f.userData, persisted: f.persisted })
  await rebooted.subject.restoreWindowsUpdateCache()
  assert.equal(rebooted.state().status, 'idle')
  assert.equal(rebooted.context.verifiedManualInstallerPath, null)
  await mustNotApplyOldInstaller(rebooted)
}

async function check(name, fn) {
  await fn()
  passed += 1
  console.log(`PASS ${name}`)
}

try {
  await check('explicitly disabled handoff still replaces cached B with available C on Check', async () => {
    const f = await restored()
    await f.subject.checkForUpdates(true)
    assert.equal(f.calls.channel, 1)
    assert.equal(f.state().status, 'available')
    assert.equal(f.state().latestVersion, C.version)
    assert.equal(f.context.desktopUpdateCandidate.version, C.version)
    assert.equal(f.context.verifiedManualInstallerPath, null)
    assert.equal(f.calls.downloads, 0)
    await mustNotApplyOldInstaller(f)
    await mustNotRestoreRetiredInstaller(f)
  })
  await check('fresh process discovers C after restoring persisted B', async () => {
    const first = await restored()
    const f = await restored({ userData: first.userData, persisted: first.persisted })
    await f.subject.checkForUpdates(false)
    assert.equal(f.calls.channel, 1)
    assert.equal(f.state().status, 'available')
    assert.equal(f.state().latestVersion, C.version)
    await mustNotApplyOldInstaller(f)
  })
  await check('Later does not prevent explicit refresh to C', async () => {
    const f = await restored()
    await f.subject.dismissDesktopUpdate()
    assert.equal(f.context.desktopUpdateSnoozedVersion, B.version)
    await f.subject.checkForUpdates(true)
    assert.equal(f.calls.channel, 1)
    assert.equal(f.state().latestVersion, C.version)
    assert.equal(f.context.desktopUpdateSnoozedVersion, null)
  })
  await check('same candidate B is reverified and reused without download', async () => {
    const f = await restored({ offered: B })
    const signaturesBefore = f.calls.signatures
    const before = await stat(f.cachedPath)
    await f.subject.checkForUpdates(true)
    assert.equal(f.calls.channel, 1)
    assert.ok(f.calls.signatures > signaturesBefore)
    assert.equal(f.state().status, 'downloaded')
    assert.equal(f.context.verifiedManualInstallerPath, f.cachedPath)
    const after = await stat(f.cachedPath)
    for (const field of ['ino', 'size', 'mtimeMs', 'ctimeMs']) assert.equal(after[field], before[field])
    assert.equal(f.calls.downloads, 0)
    assert.equal(f.calls.checksumFetches, 0)
    await f.subject.downloadDesktopUpdate()
    assert.deepEqual(f.calls.reveals, [f.cachedPath])
    assert.equal(f.calls.downloads, 0)
  })
  await check('offline refresh keeps only reverified B available', async () => {
    const f = await restored()
    const signaturesBefore = f.calls.signatures
    f.network.offline = true
    await f.subject.checkForUpdates(true)
    assert.equal(f.calls.channel, 1)
    assert.ok(f.calls.signatures > signaturesBefore)
    assert.equal(f.state().status, 'downloaded')
    assert.equal(f.state().latestVersion, B.version)
    assert.equal(f.context.verifiedManualInstallerPath, f.cachedPath)
    assert.equal(f.calls.downloads, 0)
  })
  for (const corruption of ['bytes', 'signature']) {
    await check(`offline refresh refuses ${corruption}-invalid B`, async () => {
      const f = await restored()
      f.network.offline = true
      if (corruption === 'bytes') await writeFile(f.cachedPath, Buffer.alloc(bytes.length, 1))
      else f.signature.error = 'signature_invalid'
      await f.subject.checkForUpdates(true)
      assert.equal(f.calls.channel, 1)
      assert.notEqual(f.state().status, 'downloaded')
      assert.equal(f.context.verifiedManualInstallerPath, null)
      await mustNotApplyOldInstaller(f)
    })
  }
  await check('no forward update clears the old executable action', async () => {
    const f = await restored({ offered: null })
    await f.subject.checkForUpdates(true)
    assert.equal(f.calls.channel, 1)
    assert.equal(f.state().status, 'not-available')
    assert.equal(f.context.desktopUpdateCandidate, null)
    assert.equal(f.context.verifiedManualInstallerPath, null)
    await mustNotApplyOldInstaller(f)
    await mustNotRestoreRetiredInstaller(f)
  })
  for (const flag of ['updateDownloadInProgress', 'updateApplying', 'manualInstallerActionInProgress']) {
    await check(`${flag} prevents concurrent channel replacement`, async () => {
      const f = await restored()
      f.context[flag] = true
      await f.subject.checkForUpdates(true)
      assert.equal(f.calls.channel, 0)
      assert.equal(f.context.desktopUpdateCandidate.version, B.version)
      assert.equal(f.context.verifiedManualInstallerPath, f.cachedPath)
    })
  }
  await check('real reveal verification fences a concurrent channel refresh', async () => {
    const f = await restored()
    f.signature.hold = deferred()
    f.signature.entered = deferred()
    const reveal = f.subject.downloadDesktopUpdate()
    await f.signature.entered.promise
    await f.subject.checkForUpdates(true)
    assert.equal(f.calls.channel, 0)
    assert.equal(f.context.verifiedManualInstallerPath, f.cachedPath)
    f.signature.hold.resolve()
    await reveal
    assert.deepEqual(f.calls.reveals, [f.cachedPath])
  })
  await check('an actual download keeps its candidate while a refresh is requested', async () => {
    const f = await restored()
    await f.subject.checkForUpdates(true)
    f.download.entered = deferred()
    f.download.hold = deferred()
    const downloading = f.subject.downloadDesktopUpdate()
    await f.download.entered.promise
    assert.equal(f.context.updateDownloadInProgress, true)
    const channelBefore = f.calls.channel
    await f.subject.checkForUpdates(true)
    assert.equal(f.calls.channel, channelBefore)
    assert.equal(f.context.desktopUpdateCandidate.version, C.version)
    f.download.hold.resolve()
    await downloading
    assert.equal(f.state().status, 'downloaded')
    assert.equal(f.state().latestVersion, C.version)
    assert.equal(f.calls.downloads, 1)
  })
  await check('concurrent explicit checks share one channel refresh', async () => {
    const f = await restored()
    f.network.hold = deferred()
    f.network.entered = deferred()
    const first = f.subject.checkForUpdates(true)
    await f.network.entered.promise
    const second = f.subject.checkForUpdates(true)
    f.network.hold.resolve()
    await Promise.all([first, second])
    assert.equal(f.calls.channel, 1)
    assert.equal(f.state().latestVersion, C.version)
  })
  await check('Download requested during discovery waits for the new candidate', async () => {
    const f = await restored()
    f.network.entered = deferred()
    f.network.hold = deferred()
    const refreshing = f.subject.checkForUpdates(true)
    await f.network.entered.promise
    const downloading = f.subject.downloadDesktopUpdate()
    f.network.hold.resolve()
    await Promise.all([refreshing, downloading])
    assert.equal(f.calls.channel, 1)
    assert.equal(f.calls.downloads, 1)
    assert.equal(f.state().status, 'downloaded')
    assert.equal(f.state().latestVersion, C.version)
    assert.equal(f.context.verifiedManualInstallerPath, await realpath(join(f.directory, C.installer)))
    assert.deepEqual(f.calls.reveals, [])
  })
  for (const owner of ['Quit', 'writer drain']) {
    await check(`${owner} blocks a new refresh`, async () => {
      const f = await restored()
      if (owner === 'Quit') f.context.isQuitting = true
      else f.context.desktopWriters.close('synthetic external drain')
      await f.subject.checkForUpdates(true)
      assert.equal(f.calls.channel, 0)
      assert.equal(f.context.desktopUpdateCandidate.version, B.version)
    })
    for (const boundary of ['channel resolution', 'cache revalidation']) {
      await check(`${owner} keeps its state when ${boundary} finishes`, async () => {
        const f = await restored({ offered: boundary === 'cache revalidation' ? B : C })
        const gate = boundary === 'channel resolution' ? f.network : f.signature
        gate.entered = deferred()
        gate.hold = deferred()
        const refreshing = f.subject.checkForUpdates(true)
        await gate.entered.promise
        if (owner === 'Quit') f.context.isQuitting = true
        else f.context.desktopWriters.close('synthetic external drain')
        f.context.desktopUpdateStatus = 'applying'
        const candidateBefore = f.context.desktopUpdateCandidate
        gate.hold.resolve()
        await refreshing
        assert.equal(f.state().status, 'applying', 'a completed refresh cannot publish under another lifecycle owner')
        assert.equal(f.context.desktopUpdateCandidate, candidateBefore)
        assert.equal(f.calls.downloads, 0)
      })
    }
  }
  for (const platform of ['darwin', 'win32']) {
    await check(`${platform} native downloaded update still blocks refresh`, async () => {
      const f = await fixture({ native: true, platform })
      f.context.desktopUpdateStatus = 'downloaded'
      f.context.downloadedUpdateVersion = B.version
      await f.subject.checkForUpdates(true)
      assert.equal(f.calls.channel, 0)
      assert.equal(f.calls.signatures, 0)
      assert.equal(f.context.downloadedUpdateVersion, B.version)
    })
  }
  console.log(`Windows update refresh checks passed: ${passed} (production main/scheduler/cache, synthetic channel/signature boundaries).`)
} finally {
  const cleanupRoot = await realpath(root)
  const temporaryRoot = await realpath(tmpdir())
  const within = relative(temporaryRoot, cleanupRoot)
  assert.ok(within && !within.startsWith('..') && resolve(temporaryRoot, within) === cleanupRoot)
  await rm(cleanupRoot, { recursive: true, force: true })
}
