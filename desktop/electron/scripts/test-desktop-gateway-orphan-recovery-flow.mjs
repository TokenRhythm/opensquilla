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

const scriptDir = dirname(fileURLToPath(import.meta.url))
const packageRoot = resolve(scriptDir, '..')
const repoRoot = resolve(packageRoot, '../..')

async function waitFor(check, label, timeoutMs = 60_000) {
  const startedAt = Date.now()
  let lastError
  while (Date.now() - startedAt < timeoutMs) {
    try {
      const value = await check()
      if (value) return value
    } catch (error) {
      lastError = error
    }
    await delay(200)
  }
  const suffix = lastError ? ` Last error: ${lastError.message || lastError}` : ''
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

async function ownershipDirectory(userDataDir) {
  const root = join(userDataDir, 'gateway-ownership')
  return await waitFor(async () => {
    const entries = await readdir(root, { withFileTypes: true }).catch(() => [])
    for (const entry of entries) {
      if (!entry.isDirectory()) continue
      const candidate = join(root, entry.name)
      if (loadDesktopGatewayOwnershipRecord(candidate).status === 'valid') return candidate
    }
    return null
  }, 'Desktop Gateway ownership record')
}

function processAlive(pid) {
  try {
    process.kill(pid, 0)
    return true
  } catch (error) {
    return error?.code !== 'ESRCH'
  }
}

async function waitForControlUi(app) {
  const page = await app.firstWindow({ timeout: 60_000 })
  await waitFor(() => page.url().includes('/control/'), 'Desktop Control UI', 60_000)
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
      { windowsHide: true },
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

async function launchDesktop() {
  return await electron.launch({
    args: [
      '--use-mock-keychain',
      `--user-data-dir=${userDataDir}`,
      packageRoot,
    ],
    env: launchEnvironment,
  })
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

  firstApp = await launchDesktop()
  await waitForControlUi(firstApp)
  firstOwnershipDir = await ownershipDirectory(userDataDir)
  const firstLoaded = loadDesktopGatewayOwnershipRecord(firstOwnershipDir)
  assert.equal(firstLoaded.status, 'valid')
  firstRecord = firstLoaded.record
  ownedInstances.push({ ownershipDir: firstOwnershipDir, record: firstRecord })
  assert.equal(await verifyDesktopGatewayOwnership(firstRecord), true)

  // Simulate a hard Electron crash. The detached dev Gateway must survive with
  // its profile lock and ownership record, reproducing the real orphan case.
  const firstMain = firstApp.process()
  const firstMainPid = firstMain.pid
  assert.ok(firstMainPid)
  const firstMainExit = new Promise((resolveExit) => firstMain.once('exit', resolveExit))
  firstMain.kill('SIGKILL')
  await firstMainExit
  firstApp = null
  // Windows process termination does not reliably reap Chromium child
  // processes.  Target only Electron children; the detached Python Gateway is
  // intentionally left alive and verified below.
  await stopExitedElectronChildrenOnWindows(firstMainPid)
  assert.equal(await verifyDesktopGatewayOwnership(firstRecord), true)

  secondApp = await launchDesktop()
  await waitForControlUi(secondApp)
  const secondOwnershipDir = await ownershipDirectory(userDataDir)
  assert.equal(secondOwnershipDir, firstOwnershipDir)
  const secondRecord = await waitFor(() => {
    const loaded = loadDesktopGatewayOwnershipRecord(secondOwnershipDir)
    return loaded.status === 'valid' && loaded.record.pid !== firstRecord.pid
      ? loaded.record
      : null
  }, 'replacement Desktop Gateway ownership record')
  ownedInstances.push({ ownershipDir: secondOwnershipDir, record: secondRecord })

  assert.notEqual(secondRecord.pid, firstRecord.pid)
  assert.equal(await verifyDesktopGatewayOwnership(secondRecord), true)
  await waitFor(() => !processAlive(firstRecord.pid), 'orphan Gateway process exit')

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
  if (stillLive.length === 0) {
    // Chromium can retain DIPS briefly after Electron exits on Windows.
    await removeSyntheticProfile(isolationRoot)
  } else {
    console.error(`Retained orphan-recovery diagnostics at ${isolationRoot}`)
  }
}
