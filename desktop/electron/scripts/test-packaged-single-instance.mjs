// Native L1 acceptance. One scenario per fresh evidence root. Never uses a
// real profile, replaces IPC, releases the lock, or kills a process by name.
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { createHash, randomUUID } from 'node:crypto'
import { createReadStream } from 'node:fs'
import { mkdir, readFile, realpath, stat, writeFile } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { performance } from 'node:perf_hooks'
import { setTimeout as delay } from 'node:timers/promises'
import { fileURLToPath } from 'node:url'
import { inside, isolatedEnvironment, syntheticConfig } from './test-packaged-gateway-reliability.mjs'

export function parseArguments(args) {
  const values = {}
  for (let index = 0; index < args.length; index++) {
    const key = args[index]
    assert.ok(['--executable', '--workdir', '--output', '--scenario', '--disable-gpu', '--startup-timing'].includes(key), 'Unknown argument')
    assert.ok(!Object.hasOwn(values, key), 'Duplicate argument')
    if (['--disable-gpu', '--startup-timing'].includes(key)) values[key] = true
    else {
      assert.ok(args[index + 1] && !args[index + 1].startsWith('--'), 'Missing argument value')
      values[key] = args[++index]
    }
  }
  for (const key of ['--executable', '--workdir', '--output', '--scenario']) assert.ok(values[key], 'Missing required argument')
  assert.ok(['activation', 'relaunch'].includes(values['--scenario']), 'Unsupported scenario')
  assert.equal(values['--disable-gpu'], true, 'Native probes require --disable-gpu')
  return { executable: resolve(values['--executable']), workdir: resolve(values['--workdir']), output: resolve(values['--output']),
    scenario: values['--scenario'], disableGpu: true, startupTiming: Boolean(values['--startup-timing']) }
}

function records(text) {
  return text.split(/\r?\n/).flatMap(line => {
    try { const value = JSON.parse(line); return value && typeof value === 'object' ? [value] : [] } catch { return [] }
  })
}

// Only fixed event classifications and bounded numbers leave the local log.
export function activationEvidence(text) {
  const items = records(text)
  return { accepted: items.filter(item => item.event === 'single_instance_activation_accepted').length,
    forwarded: items.filter(item => item.event === 'launch_forwarded_to_existing_instance').length,
    aborted: items.filter(item => item.event === 'launch_aborted_lock_held').length,
    secondInstanceEvents: items.filter(item => item.event === 'second_instance').length,
    draining: items.some(item => item.event === 'desktop_exit_phase' && item.to === 'draining'),
    lockAttempts: items.filter(item => item.event === 'single_instance_lock_acquired')
      .map(item => item.attempt).filter(value => Number.isSafeInteger(value) && value > 0 && value < 100),
    gatewaySpawns: items.filter(item => item.event === 'gateway_spawned').length }
}

export function startupEvidence(text) {
  const items = records(text)
  const launch = items.find(item => item.event === 'launch')
  const acquired = items.find(item => item.event === 'single_instance_lock_acquired')
  const difference = Date.parse(acquired?.at) - Date.parse(launch?.at)
  return { launchToLockAcquiredMs: Number.isFinite(difference) && difference >= 0 ? difference : null,
    lockAttempt: Number.isSafeInteger(acquired?.attempt) ? acquired.attempt : null,
    lockElapsedMs: Number.isFinite(acquired?.elapsedMs) && acquired.elapsedMs >= 0 ? acquired.elapsedMs : null,
    boundary: 'The lock-acquired log precedes request disposal; this segment does not measure all activation-helper cost or pre-import startup.' }
}

export function gatewayShutdownEvidence(text) {
  const exits = records(text).filter(item => item.event === 'quit_gateway_exit')
  return { exits: exits.length,
    gracefulExits: exits.filter(item => item.exited === true && item.hardTerminated === false).length,
    hardTerminations: exits.filter(item => item.hardTerminated === true).length,
    unprovenExits: exits.filter(item => item.exited !== true || typeof item.hardTerminated !== 'boolean').length }
}

async function hash(path) {
  const result = createHash('sha256')
  for await (const data of createReadStream(path)) result.update(data)
  return result.digest('hex')
}

async function bounded(operation, timeoutMs = 5_000) {
  let timer
  try {
    return await Promise.race([operation, new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error('operation-timeout')), timeoutMs)
    })])
  } finally { clearTimeout(timer) }
}

export async function run(options) {
  assert.equal(process.platform, 'win32', 'Windows native acceptance only')
  assert.equal(options.disableGpu, true, 'Native probes require --disable-gpu')
  assert.ok((await stat(options.executable)).isFile(), 'Missing packaged executable')
  await mkdir(dirname(options.workdir), { recursive: true })
  await mkdir(options.workdir) // Reject reuse before importing Playwright or launching anything.
  const root = await realpath(options.workdir)
  const userData = join(root, 'user-data')
  const profile = join(userData, 'opensquilla')
  const env = isolatedEnvironment(process.env, root, options)
  for (const path of [userData, profile, env.HOME, env.APPDATA, env.LOCALAPPDATA, env.TEMP, env.OPENSQUILLA_USER_STATE_DIR]) {
    assert.ok(inside(root, path)); await mkdir(path, { recursive: true })
  }
  const report = { schemaVersion: 1, scenario: options.scenario, ok: false, outcome: 'failed',
    executableSha256: await hash(options.executable), harnessSha256: await hash(fileURLToPath(import.meta.url)),
    provenanceBoundary: 'Outer EXE and harness hashes; pair with the separate full build manifest.',
    fixture: { synthetic: true, disableGpu: true, startupTiming: options.startupTiming,
      sameUserDataWithinScenario: true, modelCallsRequestedByHarness: false, provider: 'loopback-port-9' },
    phases: [], owners: [], processes: [], cleanup: { verified: false } }
  await mkdir(dirname(options.output), { recursive: true })
  await writeFile(options.output, JSON.stringify(report, null, 2) + '\n', { flag: 'wx' })
  const persist = () => writeFile(options.output, JSON.stringify(report, null, 2) + '\n')
  const started = performance.now()
  let phase = 'setup'
  const mark = value => {
    phase = value
    const item = { phase, ms: Math.round(performance.now() - started) }
    report.phases.push(item)
    console.log(JSON.stringify({ event: 'single_instance_acceptance_phase', ...item }))
  }
  const [{ _electron: electron }, smoke, cleanup, shutdown, ownership] = await Promise.all([
    import('playwright'), import('./packaged-smoke-helpers.mjs'), import('./packaged-first-send-cleanup.mjs'),
    import('./e2e-shutdown-helpers.mjs'), import('../dist/desktop-gateway-ownership.js')])
  report.ownershipHelperSha256 = await hash(fileURLToPath(new URL('../dist/desktop-gateway-ownership.js', import.meta.url)))
  const args = ['--use-mock-keychain', `--user-data-dir=${userData}`, '--disable-gpu']
  const fingerprint = ownership.desktopProfileFingerprint(profile)
  const ownershipDir = join(userData, 'gateway-ownership', fingerprint)
  const owners = new Map()
  const apps = []
  let secondary, monitor, stopped = false, observationFailed = false, launchUncaptured = false
  const log = () => readFile(join(userData, 'logs', 'desktop.log'), 'utf8')
  const currentOwner = () => {
    const result = ownership.loadDesktopGatewayOwnershipRecord(ownershipDir)
    return result.status === 'valid' ? result.record : null
  }
  const observeOwner = () => {
    const owner = currentOwner()
    if (!owner) return
    assert.equal(owner.profile_fingerprint, fingerprint)
    const key = `${owner.pid}:${owner.start_identity}`
    if (owners.has(key)) {
      assert.ok(ownership.sameDesktopGatewayOwnershipInstance(owners.get(key), owner)); return
    }
    assert.equal(ownership.desktopProcessStartIdentity(owner.pid), owner.start_identity)
    owners.set(key, owner) // Ownership nonce never leaves memory.
    report.owners.push({ pid: owner.pid, startIdentity: owner.start_identity, port: owner.port,
      firstSeenMs: Math.round(performance.now() - started) })
  }
  async function until(check, timeoutMs = 30_000, intervalMs = 50) {
    const deadline = performance.now() + timeoutMs
    while (performance.now() < deadline) {
      assert.equal(observationFailed, false, 'ownership-observation-failed')
      if (await check()) return
      await delay(intervalMs)
    }
    throw new Error('condition-timeout')
  }
  async function windowState(app) {
    return bounded(app.evaluate(({ BrowserWindow }) => {
      const window = BrowserWindow.getAllWindows().find(item => item.webContents.getURL().startsWith('opensquilla-app://desktop/'))
      return window ? { id: window.id, webContentsId: window.webContents.id,
        visible: window.isVisible(), focused: window.isFocused(), minimized: window.isMinimized() } : null
    }))
  }
  async function launch(role) {
    const launchAt = performance.now()
    launchUncaptured = true
    const app = await electron.launch({ executablePath: options.executable, args, env, timeout: 150_000 })
    // Retain the exact wrapper before any follow-up operation can fail.
    const entry = { app, child: app.process(), identity: null, role, naturallyExited: false }
    apps.push(entry)
    launchUncaptured = false
    entry.identity = await cleanup.captureElectronProcessIdentity(app)
    assert.ok(entry.identity.electronPid && entry.identity.wrapperPid, 'process-identity-unavailable')
    entry.startIdentity = ownership.desktopProcessStartIdentity(entry.identity.electronPid)
    assert.ok(entry.startIdentity, 'process-identity-unavailable')
    const actual = await bounded(app.evaluate(({ app }) => ({ userData: app.getPath('userData'), version: app.getVersion() })))
    assert.equal(resolve(actual.userData).toLowerCase(), userData.toLowerCase())
    report.version = actual.version
    report.processes.push({ role, ...entry.identity, startIdentity: entry.startIdentity,
      launchToProtocolMs: Math.round(performance.now() - launchAt) })
    await until(() => {
      entry.page = app.windows().find(page => page.url().startsWith('opensquilla-app://desktop/'))
      return entry.page
    }, 150_000)
    entry.page.setDefaultTimeout(30_000)
    await until(async () => await bounded(entry.page.evaluate(async () =>
      (await window.opensquillaDesktop.getGatewayConnection()).status === 'ready')), 180_000)
    observeOwner()
    const owner = currentOwner()
    assert.ok(owner && await ownership.verifyDesktopGatewayOwnership(owner), 'gateway-identity-unavailable')
    entry.owner = owner
    await entry.page.locator('.conn-pill.connected').waitFor({ state: 'visible', timeout: 45_000 })
    await entry.page.locator('.chat-textarea').waitFor({ state: 'visible', timeout: 45_000 })
    report.processes.at(-1).launchToUiConnectedMs = Math.round(performance.now() - launchAt)
    return entry
  }
  async function naturalExit(entry, timeoutMs = 100_000) {
    await until(() => {
      assert.equal(entry.child.signalCode, null, 'not-natural-zero-exit')
      if (entry.child.exitCode !== null) assert.equal(entry.child.exitCode, 0, 'not-natural-zero-exit')
      const state = cleanup.electronProcessSnapshot(entry.identity)
      return entry.child.exitCode !== null && state.wrapperPidExists === false && state.electronPidExists === false
    }, timeoutMs)
    assert.equal(entry.child.exitCode, 0, 'not-natural-zero-exit')
    assert.equal(entry.child.signalCode, null, 'not-natural-zero-exit')
    entry.naturallyExited = true
  }
  try {
    await writeFile(join(profile, 'config.toml'), syntheticConfig(profile, 'http://127.0.0.1:9'), { flag: 'wx' })
    await smoke.writeSyntheticCredential(userData, { baseUrl: 'http://127.0.0.1:9',
      model: 'opensquilla-gateway-reliability', disableNetworkObservability: true })
    monitor = (async () => {
      while (!stopped) {
        try { observeOwner() } catch { observationFailed = true }
        await delay(100)
      }
    })()
    mark('primary-launch')
    const primary = await launch('primary')
    mark('primary-ready')
    report.coldStartup = startupEvidence(await log())
    if (options.scenario === 'activation') {
      const sentinel = randomUUID()
      await primary.page.evaluate(value => { window.__singleInstanceDocument = value }, sentinel)
      const originalWindow = await windowState(primary.app)
      assert.ok(originalWindow)
      // Real close handler must hide the window and preserve its Gateway.
      await primary.app.evaluate(({ BrowserWindow }, id) => BrowserWindow.fromId(id).close(), originalWindow.id)
      await until(async () => (await windowState(primary.app))?.visible === false)
      const checkpoint = await log()
      const secondaryAt = performance.now()
      mark('secondary-launch')
      secondary = spawn(options.executable, args, { cwd: root, env, windowsHide: true, stdio: 'ignore' })
      let spawnFailed = false
      secondary.on('error', () => { spawnFailed = true })
      report.secondary = { pid: secondary.pid ?? null, naturalExit: false }
      await until(() => {
        assert.equal(spawnFailed, false, 'secondary-spawn-failed')
        return secondary.exitCode !== null || secondary.signalCode !== null
      }, 8_000)
      report.secondary.exitMs = Math.round(performance.now() - secondaryAt)
      assert.equal(secondary.exitCode, 0, 'not-natural-zero-exit')
      assert.equal(secondary.signalCode, null, 'not-natural-zero-exit')
      report.secondary.naturalExit = true
      await until(async () => {
        const value = await windowState(primary.app)
        return value?.id === originalWindow.id && value.webContentsId === originalWindow.webContentsId
          && value.visible && value.focused && !value.minimized
      }, 5_000)
      assert.equal(await primary.page.evaluate(() => window.__singleInstanceDocument), sentinel)
      assert.ok(ownership.sameDesktopGatewayOwnershipInstance(primary.owner, currentOwner()))
      assert.ok(await ownership.verifyDesktopGatewayOwnership(primary.owner))
      const after = await log()
      assert.ok(after.startsWith(checkpoint), 'log-rotated')
      report.activation = activationEvidence(after.slice(checkpoint.length))
      assert.equal(report.activation.accepted, 1)
      assert.equal(report.activation.forwarded, 1)
      assert.equal(report.activation.aborted, 0)
      assert.equal(report.activation.gatewaySpawns, 0)
      assert.equal(owners.size, 1)
      assert.ok(report.secondary.exitMs < 4_000, 'secondary-not-fast')
      report.activation.sameWindow = true
      report.activation.sameDocument = true
      report.activation.sameGateway = true
      mark('activation-passed')
    } else {
      const checkpoint = await log()
      mark('primary-quit-request')
      // Invoke the actual exposed quit action, not app.exit or a lock override.
      const quit = primary.page.evaluate(() => window.opensquillaDesktop.quitApp()).catch(() => undefined)
      await until(async () => {
        const text = await log()
        assert.ok(text.startsWith(checkpoint), 'log-rotated')
        return activationEvidence(text.slice(checkpoint.length)).draining
      }, 10_000, 20)
      const oldStillLive = ownership.desktopProcessStartIdentity(primary.identity.electronPid) === primary.startIdentity
      report.relaunch = { oldElectronLiveAtLaunch: oldStillLive, lockCompetitionExercised: false }
      if (!oldStillLive) { report.outcome = 'inconclusive'; throw new Error('exit-race-not-exercised') }
      mark('replacement-launch-during-drain')
      const replacement = await launch('replacement')
      await naturalExit(primary)
      await bounded(quit)
      assert.equal(shutdown.gatewayProcessSnapshot(primary.owner).alive, false)
      assert.ok(!ownership.sameDesktopGatewayOwnershipInstance(primary.owner, replacement.owner))
      assert.equal(owners.size, 2)
      const after = await log()
      assert.ok(after.startsWith(checkpoint), 'log-rotated')
      const evidence = activationEvidence(after.slice(checkpoint.length))
      report.relaunch.evidence = evidence
      assert.equal(evidence.forwarded, 0)
      assert.equal(evidence.aborted, 0)
      assert.equal(evidence.gatewaySpawns, 1)
      report.relaunch.lockCompetitionExercised = evidence.lockAttempts.some(attempt => attempt > 1)
      if (!report.relaunch.lockCompetitionExercised) {
        report.outcome = 'inconclusive'; throw new Error('exit-race-not-exercised')
      }
      report.relaunch.newGatewayReady = true
      report.relaunch.originalExitedNaturally = true
      mark('relaunch-passed')
    }
    report.outcome = 'passed'
  } catch (error) {
    const allowed = new Set(['exit-race-not-exercised', 'secondary-not-fast', 'secondary-spawn-failed',
      'ownership-observation-failed', 'condition-timeout', 'operation-timeout', 'not-natural-zero-exit',
      'gateway-identity-unavailable', 'process-identity-unavailable', 'log-rotated'])
    report.failure = { phase, reason: allowed.has(error?.message) ? error.message : 'details-redacted' }
  } finally {
    mark('cleanup')
    let cleanupFailed = launchUncaptured
    if (secondary && secondary.exitCode === null && secondary.signalCode === null) {
      try {
        const result = await shutdown.closeElectronWithDeadline({
          app: { process: () => secondary, close: () => new Promise(() => {}) }, phase: 'owned-secondary-containment',
          timeoutMs: 1, diagnostics: () => ({}), emit: () => {} })
        report.secondary.containmentForced = result.forcedExitSucceeded === true
      } catch { report.secondary.containmentForced = false }
      cleanupFailed = true // Containment of an old-package dialog is never a natural pass.
    }
    for (const entry of apps.toReversed()) {
      if (entry.naturallyExited) continue
      try {
        if (entry.child.exitCode !== null || entry.child.signalCode !== null) {
          // quitApp may already have disposed the Playwright dispatcher. Check
          // its retained child and both observed PIDs instead of closing again.
          await naturalExit(entry, 5_000)
        } else {
          await cleanup.cleanupPackagedFirstSend({ app: entry.app, processIdentity: entry.identity,
            diagnostics: () => ({}), emit: () => {} })
        }
        entry.naturallyExited = true
      } catch { cleanupFailed = true }
    }
    stopped = true
    await monitor
    try {
      assert.equal(observationFailed, false)
      assert.ok(owners.size > 0)
      for (const owner of owners.values()) assert.equal(shutdown.gatewayProcessSnapshot(owner).alive, false)
      assert.equal(ownership.loadDesktopGatewayOwnershipRecord(ownershipDir).status, 'missing')
      const text = await log()
      const spawned = records(text).filter(item => item.event === 'gateway_spawned')
      report.observedLifecycle = activationEvidence(text)
      report.gatewayShutdown = gatewayShutdownEvidence(text)
      assert.equal(spawned.length, owners.size)
      assert.ok(spawned.every(item => [...owners.values()].some(owner => owner.pid === item.pid && owner.port === item.port)))
      // Electron's own zero exit does not establish graceful Gateway drain:
      // product hard termination can also produce a zero Electron exit.
      assert.equal(report.gatewayShutdown.exits, owners.size)
      assert.equal(report.gatewayShutdown.gracefulExits, owners.size)
    } catch { cleanupFailed = true }
    report.cleanup = { verified: !cleanupFailed, allCapturedAppsExitedNaturally: apps.every(entry => entry.naturallyExited),
      uncapturedLaunch: launchUncaptured, profilePreserved: true }
    report.ok = report.outcome === 'passed' && report.cleanup.verified
    if (cleanupFailed) report.outcome = 'failed'
    await persist()
  }
  assert.equal(report.ok, true, 'Native single-instance acceptance did not pass; inspect the redacted report')
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try { await run(parseArguments(process.argv.slice(2))) }
  catch { console.error('Native single-instance acceptance failed; existing profiles were not accepted. Inspect the requested report if created.'); process.exitCode = 1 }
}
