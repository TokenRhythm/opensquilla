import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { createReadStream } from 'node:fs'
import { mkdir, readFile, readdir, stat, writeFile } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { _electron as electron } from 'playwright'
import {
  environmentWithoutProviderSecrets, requiredOption, waitFor, writeSyntheticCredential,
} from './packaged-smoke-helpers.mjs'
import { captureElectronProcessIdentity, cleanupPackagedFirstSend } from './packaged-first-send-cleanup.mjs'
import { desktopShutdownEvidenceSince, gatewayProcessSnapshot } from './e2e-shutdown-helpers.mjs'
import {
  desktopProfileFingerprint, loadDesktopGatewayOwnershipRecord, verifyDesktopGatewayOwnership,
} from '../dist/desktop-gateway-ownership.js'
import { DESKTOP_GATEWAY_STARTUP_TIMEOUT_MS } from '../dist/gateway-lifecycle.js'

// Intended for disposable native runners. Each case owns a new profile; this
// probe neither installs software nor uses the signed installer's real profile.
const executable = resolve(requiredOption('--executable'))
const workdir = resolve(requiredOption('--workdir'))
const output = resolve(requiredOption('--output'))
const scenario = requiredOption('--scenario')
const legacyKey = 'OPENSQUILLA_GATEWAY_SANDBOX__AUTO_SETUP'
const startupTimeout = DESKTOP_GATEWAY_STARTUP_TIMEOUT_MS + 30_000
assert.equal(process.platform, 'win32', 'This packaged startup probe requires native Windows')
assert.ok(['compatibility', 'long-path'].includes(scenario), 'Unknown startup scenario')
assert.ok((await stat(executable)).isFile(), 'Packaged Electron executable is missing')
await mkdir(dirname(workdir), { recursive: true })
await mkdir(workdir) // Refuse to overwrite existing evidence or reuse a real profile.
await mkdir(dirname(output), { recursive: true })
const registry = execFileSync('reg.exe', [
  'query', 'HKLM\\SYSTEM\\CurrentControlSet\\Control\\FileSystem',
], { encoding: 'utf8', windowsHide: true })
const policyMatch = registry.match(/LongPathsEnabled\s+REG_DWORD\s+0x([0-9a-f]+)/i)
const binaryHash = createHash('sha256')
for await (const chunk of createReadStream(executable)) binaryHash.update(chunk)
const report = {
  ok: false, scenario, executableSha256: binaryHash.digest('hex'),
  longPathsEnabled: policyMatch ? Number.parseInt(policyMatch[1], 16) : 0, cases: [],
}
await writeFile(output, JSON.stringify(report, null, 2) + '\n', { flag: 'wx' })
const persist = () => writeFile(output, JSON.stringify(report, null, 2) + '\n')
const cleanEnv = environmentWithoutProviderSecrets(process.env)
for (const key of Object.keys(cleanEnv)) {
  if (/^(?:OPENSQUILLA_|UV_)/i.test(key)
    || /^(?:ELECTRON_RUN_AS_NODE|PYTHONPATH|PYTHONHOME)$/i.test(key)) delete cleanEnv[key]
}

function isolatedEnvironment(root) {
  return {
    ...cleanEnv,
    HOME: join(root, 'home'), USERPROFILE: join(root, 'home'),
    APPDATA: join(root, 'appdata'), LOCALAPPDATA: join(root, 'localappdata'),
    TEMP: join(root, 'temp'), TMP: join(root, 'temp'),
    OPENSQUILLA_USER_STATE_DIR: join(root, 'user-state'),
    OPENSQUILLA_TEST_PROFILE_LOCK_ROOT: '1',
    OPENSQUILLA_DESKTOP_SECRET_STORAGE: 'plain', OPENSQUILLA_DESKTOP_DISABLE_AUTO_UPDATE: '1',
    OPENSQUILLA_OPENROUTER_LIVE_PRICING: '0', OPENSQUILLA_TESTING: '0', GITHUB_ACTIONS: '0',
    // Use the ordinary dotenv path. RECOVERY_OFFLINE would conceal the bug.
    HTTP_PROXY: 'http://127.0.0.1:1', HTTPS_PROXY: 'http://127.0.0.1:1', ALL_PROXY: 'http://127.0.0.1:1',
    http_proxy: 'http://127.0.0.1:1', https_proxy: 'http://127.0.0.1:1', all_proxy: 'http://127.0.0.1:1',
    NO_PROXY: '127.0.0.1,localhost,::1', no_proxy: '127.0.0.1,localhost,::1',
  }
}

function longUserData(base, recordLength) {
  const suffix = join('gateway-ownership', 'x'.repeat(64), 'desktop-gateway.json')
  const padding = recordLength - join(base, suffix).length - 1
  assert.ok(padding >= 1 && padding <= 255, 'Use a shorter --workdir for the MAX_PATH boundary probe')
  return join(base, 'x'.repeat(padding))
}

async function runCase(plan, result) {
  const root = join(workdir, plan.name)
  const userData = plan.recordLength
    ? longUserData(join(root, 'user-data'), plan.recordLength)
    : join(root, 'user-data')
  const profile = join(userData, 'opensquilla')
  const env = isolatedEnvironment(root)
  for (const directory of [profile, env.HOME, env.APPDATA, env.LOCALAPPDATA, env.TEMP]) {
    await mkdir(directory, { recursive: true })
  }
  const config = [
    'config_version = 1', `state_dir = ${JSON.stringify(join(profile, 'state'))}`,
    `workspace_dir = ${JSON.stringify(join(profile, 'workspace'))}`,
    '[llm]', 'provider = "ollama"', 'model = "packaged-startup-compatibility"',
    'base_url = "http://127.0.0.1:9"', 'context_window_tokens = 131072',
    '[squilla_router]', 'enabled = false', '[llm_ensemble]', 'enabled = false',
    '[privacy]', 'disable_network_observability = true',
    ...(plan.kind === 'toml' ? ['[sandbox]', `auto_setup = ${plan.value}`] : []),
  ].join('\n') + '\n'
  await writeFile(join(profile, 'config.toml'), config)
  const dotenv = `${legacyKey}=${plan.value}\n`
  if (plan.kind === 'dotenv') await writeFile(join(profile, '.env'), dotenv)
  if (plan.kind === 'process') env[legacyKey] = plan.value
  await writeSyntheticCredential(userData, {
    baseUrl: 'http://127.0.0.1:9', model: 'packaged-startup-compatibility', disableNetworkObservability: true,
  })
  const fingerprint = desktopProfileFingerprint(profile)
  const ownershipDir = join(userData, 'gateway-ownership', fingerprint)
  const recordPath = join(ownershipDir, 'desktop-gateway.json')
  const lockPath = join(ownershipDir, 'desktop-gateway.lock')
  const logPath = join(userData, 'logs', 'desktop.log')
  result.recordPathLength = recordPath.length
  result.lockPathLength = lockPath.length
  if (plan.recordLength) assert.equal(recordPath.length, plan.recordLength)
  let previousOwner
  let lockIdentity
  for (const attempt of [1, 2]) {
    const boot = { attempt, ok: false }
    result.boots.push(boot)
    await persist()
    let app
    let identity
    let owner
    let page
    let checkpoint = ''
    let error
    const started = Date.now()
    const pageErrors = []
    try {
      app = await electron.launch({
        executablePath: executable,
        args: ['--use-mock-keychain', `--user-data-dir=${userData}`], env, timeout: startupTimeout,
      })
      identity = await captureElectronProcessIdentity(app)
      const actual = await app.evaluate(({ app }) => ({ version: app.getVersion(), userData: app.getPath('userData') }))
      assert.equal(resolve(actual.userData).toLowerCase(), userData.toLowerCase())
      page = await app.firstWindow({ timeout: startupTimeout })
      page.on('pageerror', failure => pageErrors.push(failure.message))
      await page.waitForURL(url => url.protocol === 'opensquilla-app:' && url.hostname === 'desktop', { timeout: startupTimeout })
      await page.locator('.conn-pill.connected').waitFor({ state: 'visible', timeout: startupTimeout })
      await page.locator('.chat-textarea').waitFor({ state: 'visible', timeout: 30_000 })
      await waitFor(async () => (
        await page.evaluate(() => window.opensquillaDesktop?.getGatewayConnection?.())
      )?.status === 'ready', 'the packaged Gateway connection to become ready', startupTimeout)
      await waitFor(async () => {
        const loaded = loadDesktopGatewayOwnershipRecord(ownershipDir)
        if (loaded.status !== 'valid' || !await verifyDesktopGatewayOwnership(loaded.record)) return false
        owner = loaded.record
        return true
      }, 'the signed Gateway ownership identity', 30_000)
      assert.equal(owner.profile_fingerprint, fingerprint)
      assert.equal(owner.version, actual.version)
      assert.notEqual(`${owner.pid}:${owner.start_identity}`, previousOwner)
      previousOwner = `${owner.pid}:${owner.start_identity}`
      const currentLock = (await stat(lockPath, { bigint: true })).ino.toString()
      if (lockIdentity !== undefined) assert.equal(currentLock, lockIdentity)
      lockIdentity = currentLock
      const temporaryPathLength = join(ownershipDir, `.desktop-gateway.json.${owner.pid}.${'0'.repeat(16)}.tmp`).length
      if (plan.recordLength) assert.ok(temporaryPathLength > 260)
      Object.assign(boot, {
        readyMs: Date.now() - started, version: actual.version, identityVerified: true,
        gatewayPid: owner.pid, gatewayStartIdentity: owner.start_identity, fingerprint, temporaryPathLength,
      })
      await page.screenshot({ path: join(root, `boot-${attempt}.png`) })
      checkpoint = await readFile(logPath, 'utf8')
    } catch (failure) {
      error = failure
      if (page) {
        await page.screenshot({ path: join(root, `boot-${attempt}-failed.png`) }).catch(() => {})
        const connection = await page.evaluate(() => window.opensquillaDesktop?.getGatewayConnection?.()).catch(() => null)
        boot.connection = connection && { status: connection.status, error: connection.error }
      }
    } finally {
      try {
        await cleanupPackagedFirstSend({
          app, processIdentity: identity,
          diagnostics: () => ({ case: plan.name, attempt, pageErrors }),
        })
        if (owner) {
          assert.equal(gatewayProcessSnapshot(owner).alive, false)
          assert.deepEqual(desktopShutdownEvidenceSince(checkpoint, await readFile(logPath, 'utf8')), {
            gatewayExitLogged: true, committedExitLogged: true,
          })
          assert.equal(loadDesktopGatewayOwnershipRecord(ownershipDir).status, 'missing')
          assert.equal((await stat(lockPath, { bigint: true })).ino.toString(), lockIdentity)
          assert.deepEqual(await readdir(ownershipDir), ['desktop-gateway.lock'])
          boot.normalQuitVerified = true
        }
      } catch (cleanupError) {
        error ??= cleanupError
        boot.cleanupError = cleanupError.message
      }
      boot.pageErrors = pageErrors
      for (const log of ['desktop', 'gateway']) {
        const content = await readFile(join(userData, 'logs', `${log}.log`), 'utf8').catch(() => '')
        await writeFile(join(root, `boot-${attempt}-${log}.log`), content)
      }
    }
    if (error) throw error
    assert.deepEqual(pageErrors, [])
    assert.equal(boot.normalQuitVerified, true)
    if (plan.kind === 'dotenv') assert.equal(await readFile(join(profile, '.env'), 'utf8'), dotenv)
    boot.ok = true
    await persist()
  }
  result.ok = true
}

const plans = scenario === 'compatibility'
  ? [
      { name: 'clean', kind: 'clean' },
      ...['process', 'dotenv', 'toml'].flatMap(kind => ['true', 'false'].map(value => ({ name: `${kind}-${value}`, kind, value }))),
    ]
  : [
      { name: 'temporary-over-max-path', recordLength: 245 },
      { name: 'record-lock-over-max-path', recordLength: 310 },
    ]
for (const plan of plans) {
  const result = { name: plan.name, ok: false, boots: [] }
  report.cases.push(result)
  try {
    await runCase(plan, result)
  } catch (error) {
    result.error = error.stack || error.message || String(error)
  }
  await persist()
  console.log(JSON.stringify({ case: plan.name, ok: result.ok, error: result.error }))
}
report.ok = report.cases.length === plans.length && report.cases.every(result => result.ok)
await persist()
if (!report.ok) process.exitCode = 1
