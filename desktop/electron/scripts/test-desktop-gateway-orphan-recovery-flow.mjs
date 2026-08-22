import assert from 'node:assert/strict'
import { execFile } from 'node:child_process'
import { mkdir, mkdtemp, readdir, rm, writeFile } from 'node:fs/promises'
import net from 'node:net'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'
import { fileURLToPath } from 'node:url'
import { _electron as electron } from 'playwright'

import {
  loadDesktopGatewayOwnershipRecord,
  requestVerifiedDesktopGatewayShutdown,
  verifyDesktopGatewayOwnership,
  waitForDesktopGatewayOwnershipRelease,
} from '../dist/desktop-gateway-ownership.js'
import { DESKTOP_GATEWAY_STARTUP_TIMEOUT_MS } from '../dist/gateway-lifecycle.js'

const scriptDir = dirname(fileURLToPath(import.meta.url))
const packageRoot = resolve(scriptDir, '..')
const repoRoot = resolve(packageRoot, '../..')

// Keep these phase budgets aligned with the product lifecycle in main.ts:
// orphan recovery can legitimately spend up to 80s releasing the verified
// predecessor, Gateway health owns a 120s cold-start budget, and Control UI
// readiness has a final 45s route budget. A phase shares one deadline instead
// of accidentally receiving a fresh timeout for every individual assertion.
const VERIFIED_ORPHAN_GATEWAY_RELEASE_TIMEOUT_MS = 80_000
const CONTROL_UI_ROUTE_TIMEOUT_MS = 45_000
const INITIAL_DESKTOP_STARTUP_BUDGET_MS = (
  DESKTOP_GATEWAY_STARTUP_TIMEOUT_MS + CONTROL_UI_ROUTE_TIMEOUT_MS
)
const ORPHAN_RECOVERY_STARTUP_BUDGET_MS = (
  VERIFIED_ORPHAN_GATEWAY_RELEASE_TIMEOUT_MS + INITIAL_DESKTOP_STARTUP_BUDGET_MS
)
const CRASH_EXIT_BUDGET_MS = 15_000
const WINDOWS_ELECTRON_CHILD_CLEANUP_COMMAND_TIMEOUT_MS = 20_000
const WINDOWS_ELECTRON_CHILD_CLEANUP_BUDGET_MS = 30_000

function createPhaseBudget(name, timeoutMs) {
  const startedAt = Date.now()
  return {
    name,
    timeoutMs,
    elapsedMs: () => Date.now() - startedAt,
    remainingMs(step) {
      const elapsedMs = Date.now() - startedAt
      const remainingMs = timeoutMs - elapsedMs
      if (remainingMs <= 0) {
        throw new Error(
          `DESKTOP_E2E_PHASE_TIMEOUT: phase=${name} step=${step} `
          + `elapsedMs=${elapsedMs} budgetMs=${timeoutMs}`,
        )
      }
      return remainingMs
    },
  }
}

function appProcessState(app) {
  if (!app) return null
  const child = app.process()
  return {
    pid: child.pid ?? null,
    exitCode: child.exitCode,
    signalCode: child.signalCode,
    killed: child.killed,
  }
}

async function ownershipDiagnostics(userDataDir) {
  const root = join(userDataDir, 'gateway-ownership')
  const entries = await readdir(root, { withFileTypes: true }).catch(() => [])
  return entries.filter(entry => entry.isDirectory()).map((entry) => {
    const ownershipDir = join(root, entry.name)
    const loaded = loadDesktopGatewayOwnershipRecord(ownershipDir)
    return {
      directory: entry.name,
      status: loaded.status,
      pid: loaded.status === 'valid' ? loaded.record.pid : null,
      port: loaded.status === 'valid' ? loaded.record.port : null,
      version: loaded.status === 'valid' ? loaded.record.version : null,
    }
  })
}

async function phaseDiagnostics(app, userDataDir, phase) {
  let windows = []
  try {
    windows = app ? await Promise.all(app.windows().map(async (page) => {
      const url = page.url()
      try {
        return {
          url,
          title: await page.title(),
          bodyText: await page.locator('body').innerText({ timeout: 1_000 }).then(
            text => text.slice(0, 2_000),
          ).catch(() => null),
        }
      } catch (error) {
        return { url, diagnosticError: error?.message || String(error) }
      }
    })) : []
  } catch (error) {
    windows = [{ diagnosticError: error?.message || String(error) }]
  }
  return {
    phase: phase.name,
    elapsedMs: phase.elapsedMs(),
    budgetMs: phase.timeoutMs,
    process: appProcessState(app),
    windows,
    ownership: await ownershipDiagnostics(userDataDir),
  }
}

async function phaseError(message, app, userDataDir, phase, cause = null) {
  const diagnostics = await phaseDiagnostics(app, userDataDir, phase).catch(error => ({
    diagnosticError: error?.message || String(error),
  }))
  const causeSuffix = cause ? ` Cause: ${cause?.message || cause}` : ''
  return new Error(
    `DESKTOP_E2E_PHASE_FAILED: phase=${phase.name} ${message}.${causeSuffix} `
    + `Diagnostics: ${JSON.stringify(diagnostics)}`,
  )
}

function assertAppRunning(app, phase, step) {
  const state = appProcessState(app)
  if (state && (state.exitCode !== null || state.signalCode !== null)) {
    throw new Error(
      `DESKTOP_E2E_PROCESS_EXITED: phase=${phase.name} step=${step} `
      + `process=${JSON.stringify(state)}`,
    )
  }
}

async function withPhaseDeadline(promise, phase, step, app, userDataDir) {
  const timeoutMs = phase.remainingMs(step)
  let timer = null
  try {
    return await Promise.race([
      promise,
      new Promise((_, rejectTimeout) => {
        timer = setTimeout(() => rejectTimeout(new Error(
          `DESKTOP_E2E_PHASE_TIMEOUT: phase=${phase.name} step=${step} `
          + `elapsedMs=${phase.elapsedMs()} budgetMs=${phase.timeoutMs}`,
        )), timeoutMs)
      }),
    ])
  } catch (error) {
    throw await phaseError(`step=${step}`, app, userDataDir, phase, error)
  } finally {
    if (timer) clearTimeout(timer)
  }
}

async function waitFor(check, label, timeoutMs, diagnose = null) {
  const startedAt = Date.now()
  while (Date.now() - startedAt < timeoutMs) {
    const value = await check()
    if (value) return value
    await delay(200)
  }
  let diagnostic = null
  if (diagnose) {
    diagnostic = await diagnose().catch(error => ({
      diagnosticError: error?.message || String(error),
    }))
  }
  const suffix = diagnostic === null ? '' : ` Diagnostics: ${JSON.stringify(diagnostic)}`
  throw new Error(`Timed out waiting for ${label}.${suffix}`)
}

async function freeLoopbackPort() {
  const server = net.createServer()
  await new Promise((resolveListen, rejectListen) => {
    server.once('error', rejectListen)
    server.listen(0, '127.0.0.1', resolveListen)
  })
  const address = server.address()
  assert.ok(address && typeof address === 'object')
  await new Promise((resolveClose) => server.close(resolveClose))
  return address.port
}

async function ownershipDirectory(userDataDir, app, phase) {
  const root = join(userDataDir, 'gateway-ownership')
  return await waitFor(async () => {
    assertAppRunning(app, phase, 'ownership-record')
    const entries = await readdir(root, { withFileTypes: true }).catch(() => [])
    for (const entry of entries) {
      if (!entry.isDirectory()) continue
      const candidate = join(root, entry.name)
      if (loadDesktopGatewayOwnershipRecord(candidate).status === 'valid') return candidate
    }
    return null
  }, 'Desktop Gateway ownership record', phase.remainingMs('ownership-record'), () => (
    phaseDiagnostics(app, userDataDir, phase)
  ))
}

function processAlive(pid) {
  try {
    process.kill(pid, 0)
    return true
  } catch (error) {
    return error?.code !== 'ESRCH'
  }
}

async function waitForControlUi(app, userDataDir, phase) {
  let page
  try {
    page = await app.firstWindow({ timeout: phase.remainingMs('first-window') })
  } catch (error) {
    throw await phaseError('step=first-window', app, userDataDir, phase, error)
  }
  await waitFor(() => {
    assertAppRunning(app, phase, 'control-ui-route')
    return page.url().includes('/control/')
  }, 'Desktop Control UI', phase.remainingMs('control-ui-route'), () => (
    phaseDiagnostics(app, userDataDir, phase)
  ))
  return page
}

async function stopExitedElectronChildrenOnWindows(parentPid) {
  if (process.platform !== 'win32') return
  await new Promise((resolveStop, rejectStop) => {
    const command = [
      `Get-CimInstance Win32_Process -Filter \"ParentProcessId = ${parentPid}\"`,
      "| Where-Object { $_.Name -ieq 'electron.exe' }",
      '| ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }',
    ].join(' ')
    execFile(
      'powershell.exe',
      ['-NoProfile', '-NonInteractive', '-Command', command],
      {
        windowsHide: true,
        timeout: WINDOWS_ELECTRON_CHILD_CLEANUP_COMMAND_TIMEOUT_MS,
        killSignal: 'SIGKILL',
      },
      (error) => error ? rejectStop(error) : resolveStop(),
    )
  })
  await delay(250)
}

async function removeSyntheticProfile(root) {
  for (let attempt = 0; attempt < 5; attempt += 1) {
    try {
      await rm(root, { recursive: true, force: true })
      return
    } catch (error) {
      if (process.platform !== 'win32' || error?.code !== 'EBUSY') throw error
      if (attempt === 4) {
        console.error(`Retained orphan-recovery diagnostics at ${root}: ${error.message}`)
        return
      }
      await delay(250 * (attempt + 1))
    }
  }
}

const isolationRoot = await mkdtemp(join(tmpdir(), 'opensquilla-orphan-recovery-e2e-'))
const userDataDir = join(isolationRoot, 'chromium-user-data')
const isolatedHome = join(isolationRoot, 'home')
const port = await freeLoopbackPort()
let firstApp
let secondApp
let firstOwnershipDir = null
let firstRecord = null
const ownedInstances = []
let flowSucceeded = false

const launchEnvironment = {
  ...process.env,
  HOME: isolatedHome,
  USERPROFILE: isolatedHome,
  OPENSQUILLA_DESKTOP_REPO_ROOT: repoRoot,
  OPENSQUILLA_DESKTOP_SECRET_STORAGE: 'plain',
  OPENSQUILLA_DESKTOP_GATEWAY_PORT: String(port),
  OPENSQUILLA_DESKTOP_DISABLE_AUTO_UPDATE: '1',
  LANG: 'en_US.UTF-8',
  LC_ALL: 'en_US.UTF-8',
}

async function launchDesktop(phase, step) {
  try {
    return await electron.launch({
      args: [
        '--use-mock-keychain',
        `--user-data-dir=${userDataDir}`,
        packageRoot,
      ],
      env: launchEnvironment,
      timeout: phase.remainingMs(step),
    })
  } catch (error) {
    throw await phaseError(`step=${step}`, null, userDataDir, phase, error)
  }
}

try {
  await mkdir(userDataDir, { recursive: true })
  await mkdir(isolatedHome, { recursive: true })
  const now = new Date().toISOString()
  await writeFile(join(userDataDir, 'desktop-credential.json'), JSON.stringify({
    provider: 'ollama',
    model: 'opensquilla-orphan-recovery-test-model',
    baseUrl: 'http://127.0.0.1:11434',
    apiKeyEnv: '',
    encryptedApiKey: '',
    modelRoutingMode: 'direct',
    routerMode: 'disabled',
    routerDefaultTier: 'c1',
    routerTiers: {},
    searchProvider: 'duckduckgo',
    searchApiKeyEnv: '',
    encryptedSearchApiKey: '',
    encryption: 'plain',
    disableNetworkObservability: false,
    createdAt: now,
    updatedAt: now,
  }, null, 2), { mode: 0o600 })

  const initialStartup = createPhaseBudget(
    'initial-desktop-startup',
    INITIAL_DESKTOP_STARTUP_BUDGET_MS,
  )
  firstApp = await launchDesktop(initialStartup, 'electron-launch')
  await waitForControlUi(firstApp, userDataDir, initialStartup)
  firstOwnershipDir = await ownershipDirectory(userDataDir, firstApp, initialStartup)
  const firstLoaded = loadDesktopGatewayOwnershipRecord(firstOwnershipDir)
  assert.equal(firstLoaded.status, 'valid')
  firstRecord = firstLoaded.record
  ownedInstances.push({ ownershipDir: firstOwnershipDir, record: firstRecord })
  assert.equal(
    await withPhaseDeadline(
      verifyDesktopGatewayOwnership(firstRecord),
      initialStartup,
      'verify-initial-gateway-ownership',
      firstApp,
      userDataDir,
    ),
    true,
  )

  // Simulate a hard Electron crash. The detached dev Gateway must survive with
  // its profile lock and ownership record, reproducing the real orphan case.
  const firstMain = firstApp.process()
  const firstMainPid = firstMain.pid
  assert.ok(firstMainPid)
  const firstMainExit = new Promise((resolveExit) => firstMain.once('exit', resolveExit))
  const crashExit = createPhaseBudget('hard-crash-exit', CRASH_EXIT_BUDGET_MS)
  firstMain.kill('SIGKILL')
  await withPhaseDeadline(
    firstMainExit,
    crashExit,
    'electron-main-exit',
    firstApp,
    userDataDir,
  )
  // Windows process termination does not reliably reap Chromium child
  // processes.  Target only Electron children; the detached Python Gateway is
  // intentionally left alive and verified below.
  const electronChildCleanup = createPhaseBudget(
    'windows-electron-child-cleanup',
    WINDOWS_ELECTRON_CHILD_CLEANUP_BUDGET_MS,
  )
  await withPhaseDeadline(
    stopExitedElectronChildrenOnWindows(firstMainPid),
    electronChildCleanup,
    'terminate-electron-children',
    firstApp,
    userDataDir,
  )
  assert.equal(
    await withPhaseDeadline(
      verifyDesktopGatewayOwnership(firstRecord),
      electronChildCleanup,
      'verify-orphan-survived',
      firstApp,
      userDataDir,
    ),
    true,
  )
  firstApp = null

  const orphanRecoveryStartup = createPhaseBudget(
    'verified-orphan-recovery-and-restart',
    ORPHAN_RECOVERY_STARTUP_BUDGET_MS,
  )
  secondApp = await launchDesktop(orphanRecoveryStartup, 'electron-relaunch')
  await waitForControlUi(secondApp, userDataDir, orphanRecoveryStartup)
  const secondOwnershipDir = await ownershipDirectory(
    userDataDir,
    secondApp,
    orphanRecoveryStartup,
  )
  assert.equal(secondOwnershipDir, firstOwnershipDir)
  const secondRecord = await waitFor(() => {
    assertAppRunning(secondApp, orphanRecoveryStartup, 'replacement-ownership-record')
    const loaded = loadDesktopGatewayOwnershipRecord(secondOwnershipDir)
    return loaded.status === 'valid' && loaded.record.pid !== firstRecord.pid
      ? loaded.record
      : null
  }, 'replacement Desktop Gateway ownership record', orphanRecoveryStartup.remainingMs(
    'replacement-ownership-record',
  ), () => phaseDiagnostics(secondApp, userDataDir, orphanRecoveryStartup))
  ownedInstances.push({ ownershipDir: secondOwnershipDir, record: secondRecord })

  assert.notEqual(secondRecord.pid, firstRecord.pid)
  assert.equal(
    await withPhaseDeadline(
      verifyDesktopGatewayOwnership(secondRecord),
      orphanRecoveryStartup,
      'verify-replacement-gateway-ownership',
      secondApp,
      userDataDir,
    ),
    true,
  )
  await waitFor(() => {
    assertAppRunning(secondApp, orphanRecoveryStartup, 'orphan-process-exit')
    return !processAlive(firstRecord.pid)
  }, 'orphan Gateway process exit', orphanRecoveryStartup.remainingMs(
    'orphan-process-exit',
  ), () => phaseDiagnostics(secondApp, userDataDir, orphanRecoveryStartup))

  await secondApp.close()
  secondApp = null
  assert.equal(
    await waitForDesktopGatewayOwnershipRelease(secondOwnershipDir, secondRecord, {
      timeoutMs: 15_000,
      pollIntervalMs: 100,
    }),
    true,
  )

  console.log(JSON.stringify({ ok: true, orphanPid: firstRecord.pid, replacementPid: secondRecord.pid }))
  flowSucceeded = true
} finally {
  if (secondApp) await secondApp.close().catch(() => null)
  if (firstApp) await firstApp.close().catch(() => null)
  for (const { ownershipDir, record } of ownedInstances.reverse()) {
    if (processAlive(record.pid) && await verifyDesktopGatewayOwnership(record).catch(() => false)) {
      await requestVerifiedDesktopGatewayShutdown(record).catch(() => false)
      await waitForDesktopGatewayOwnershipRelease(ownershipDir, record, {
        timeoutMs: 10_000,
        pollIntervalMs: 100,
      }).catch(() => false)
    }
  }
  // Never remove a synthetic profile from underneath a process that did not
  // accept the bounded cleanup request; retain it for CI diagnostics instead.
  const stillLive = ownedInstances.filter(({ record }) => processAlive(record.pid))
  if (flowSucceeded && stillLive.length === 0) {
    // Chromium can retain DIPS briefly after Electron exits on Windows.
    await removeSyntheticProfile(isolationRoot)
  } else {
    console.error(`Retained orphan-recovery diagnostics at ${isolationRoot}`)
  }
}
