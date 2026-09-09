import assert from 'node:assert/strict'
import { execFile } from 'node:child_process'
import { randomBytes } from 'node:crypto'
import { lstat, mkdir, readFile, realpath, writeFile } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { promisify } from 'node:util'
import { _electron as electron } from 'playwright'
import { environmentWithoutProviderSecrets, requiredOption, waitFor } from './packaged-smoke-helpers.mjs'
import { captureElectronProcessIdentity, closeElectronAndObserveExit } from './packaged-first-send-cleanup.mjs'
import { desktopShutdownEvidenceSince, gatewayProcessSnapshot } from './e2e-shutdown-helpers.mjs'
import { desktopProfileFingerprint, loadDesktopGatewayOwnershipRecord, verifyDesktopGatewayOwnership } from '../dist/desktop-gateway-ownership.js'
import { DESKTOP_GATEWAY_STARTUP_TIMEOUT_MS } from '../dist/gateway-lifecycle.js'
import { assertPreservedInputs, assertStopEvidence, auditMessages, sha256, verifyAuditInputs } from './fixtures/packaged-retained-interaction/contract.mjs'
import { installRetainedRpcProbe } from './fixtures/packaged-retained-interaction/browser-probe.mjs'
import { startRetainedProvider } from './fixtures/packaged-retained-interaction/provider.mjs'

// This is an opt-in native probe. It never creates/reconfigures a Desktop
// profile, installs software, or weakens the existing fresh-profile gate.
// The outer signed-update audit must pin its synthetic retained inputs first.
const runFile = promisify(execFile)
const scriptDir = dirname(fileURLToPath(import.meta.url))
const repository = resolve(scriptDir, '../../..')
const timeoutMs = 45_000
const oldSessions = [
  'agent:main:webchat:release-recovery-long-session',
  'agent:main:webchat:release-recovery-switch-session',
]
const report = {
  ok: false, status: 'running', startedAt: new Date().toISOString(),
  scope: 'Installed B retained synthetic profile; real UI and Gateway with a loopback synthetic provider. No installer, signature, UAC, or real-provider claim.',
  cycles: [], screenshots: [],
}
let plan
let provider
let active
let outputCreated = false

async function persist() {
  if (outputCreated) await writeFile(join(plan.outputDir, 'report.json'), JSON.stringify(report, null, 2) + '\n')
}
async function stage(name) {
  report.stage = name
  await persist()
  console.error(JSON.stringify({ event: 'retained_interaction_stage', stage: name }))
}
async function preserved(stageName) {
  await assertPreservedInputs(plan)
  const args = [join(repository, '.github/scripts/verify-release-profile-preservation.py'), 'verify-runtime', '--home', plan.profile, '--label', plan.seedLabel]
  if (plan.externalSentinelsDir) args.push('--external-root', plan.externalSentinelsDir)
  const pythonIndex = process.argv.indexOf('--python')
  const python = pythonIndex < 0 ? 'python' : process.argv[pythonIndex + 1]
  const result = await runFile(python, args, { windowsHide: true, timeout: 30_000, maxBuffer: 1024 * 1024 })
  await writeFile(join(plan.outputDir, `preservation-${stageName}.log`), result.stdout + result.stderr)
}
async function noExistingDesktop() {
  // Deliberately conservative: any installed Desktop process prevents this
  // probe from accidentally joining an existing single-instance owner.
  const source = "$ErrorActionPreference='Stop'; @(Get-CimInstance Win32_Process -Filter \"Name='OpenSquilla.exe'\").Count"
  const result = await runFile('powershell.exe', ['-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand', Buffer.from(source, 'utf16le').toString('base64')], { windowsHide: true, timeout: 15_000 })
  assert.equal(result.stdout.trim(), '0', 'Close existing Desktop instances before this isolated probe')
}
async function snapshot(page) {
  return page.evaluate(() => JSON.parse(JSON.stringify(globalThis.__retainedAuditRpc)))
}
async function screenshot(page, name) {
  const path = join(plan.outputDir, `${name}.png`)
  await page.screenshot({ path, fullPage: true })
  report.screenshots.push({ path, sha256: sha256(await readFile(path)) })
}
async function ready(page) {
  await page.locator('.conn-pill.connected').waitFor({ state: 'visible', timeout: timeoutMs })
  await page.locator('.chat-textarea').waitFor({ state: 'visible', timeout: timeoutMs })
}
async function launch(name) {
  await assertPreservedInputs(plan)
  await noExistingDesktop()
  const env = environmentWithoutProviderSecrets(process.env)
  for (const key of Object.keys(env)) {
    if (/^OPENSQUILLA_/i.test(key) || /^ELECTRON_RUN_AS_NODE$/i.test(key)) delete env[key]
  }
  const app = await electron.launch({
    executablePath: plan.executablePath,
    args: ['--use-mock-keychain', `--user-data-dir=${plan.userDataDir}`],
    env: { ...env, OPENSQUILLA_DESKTOP_SECRET_STORAGE: 'plain', OPENSQUILLA_DESKTOP_DISABLE_AUTO_UPDATE: '1', OPENSQUILLA_RECOVERY_OFFLINE: '1', OPENSQUILLA_TESTING: '0', GITHUB_ACTIONS: '0', NO_PROXY: '127.0.0.1,localhost,::1', no_proxy: '127.0.0.1,localhost,::1' },
    timeout: DESKTOP_GATEWAY_STARTUP_TIMEOUT_MS + timeoutMs,
  })
  active = { app, name }
  const identity = await captureElectronProcessIdentity(app)
  active.identity = identity
  const actual = await app.evaluate(({ app }) => ({ version: app.getVersion(), userData: app.getPath('userData') }))
  assert.equal(actual.version, plan.expectedVersion)
  assert.equal(resolve(actual.userData).toLowerCase(), resolve(plan.userDataDir).toLowerCase())
  const page = await app.firstWindow()
  active.page = page
  await page.waitForURL(url => url.protocol === 'opensquilla-app:' && url.hostname === 'desktop', { timeout: timeoutMs })
  await page.locator('.conn-pill.connected').waitFor({ state: 'visible', timeout: DESKTOP_GATEWAY_STARTUP_TIMEOUT_MS + timeoutMs })
  const pageErrors = []
  page.on('pageerror', error => pageErrors.push(error.message))
  active.pageErrors = pageErrors
  const ownership = await waitFor(async () => {
    const loaded = loadDesktopGatewayOwnershipRecord(join(plan.userDataDir, 'gateway-ownership', desktopProfileFingerprint(plan.profile)))
    return loaded.status === 'valid' && await verifyDesktopGatewayOwnership(loaded.record) ? loaded.record : null
  }, 'the actual owned Gateway identity', timeoutMs)
  assert.equal(ownership.version, plan.expectedVersion)
  active.ownership = ownership
  await page.addInitScript(installRetainedRpcProbe)
  await page.evaluate(installRetainedRpcProbe)
  await page.setViewportSize({ width: 1440, height: 900 })
  report.cycles.push({ name, ...identity, version: actual.version, userData: actual.userData, gatewayPid: ownership.pid, gatewayStartIdentity: ownership.start_identity })
  return page
}
async function navigateSession(page, key) {
  const url = new URL(page.url())
  url.pathname = key ? '/chat' : '/chat/new'
  url.search = key ? new URLSearchParams({ session: key }).toString() : ''
  url.hash = ''
  if (page.url() !== url.toString()) await page.goto(url.toString(), { waitUntil: 'domcontentloaded' })
  await ready(page)
}
async function oldSessionUi(page, name) {
  await navigateSession(page, oldSessions[0])
  const expected = `Synthetic retained history message 0320 (${plan.seedLabel})`
  await page.getByText(expected, { exact: true }).first().waitFor({ state: 'visible', timeout: timeoutMs })
  await page.locator(`[data-session-key="${oldSessions[1]}"] .sidebar-history-item`).click()
  await waitFor(() => new URL(page.url()).searchParams.get('session') === oldSessions[1], 'retained peer session navigation', timeoutMs)
  await ready(page)
  await page.locator(`[data-session-key="${oldSessions[0]}"] .sidebar-history-item`).click()
  await waitFor(() => new URL(page.url()).searchParams.get('session') === oldSessions[0], 'return to retained long session', timeoutMs)
  await page.getByText(expected, { exact: true }).first().waitFor({ state: 'visible', timeout: timeoutMs })
  await screenshot(page, `${name}-old-session`)
}
async function send(page, message, answer) {
  await ready(page)
  await page.locator('.chat-textarea').fill(message)
  const button = page.locator('.chat-send-btn.btn--primary')
  await waitFor(async () => await button.count() === 1 && await button.isEnabled(), 'enabled real Send', timeoutMs)
  await button.click()
  await page.locator('.msg-ai-text').filter({ hasText: answer }).last().waitFor({ state: 'visible', timeout: timeoutMs })
  await waitFor(async () => await button.count() === 1 && await button.isEnabled(), 'completed real Send', timeoutMs)
  const rpc = await snapshot(page)
  assert.equal(rpc.requests.filter(request => request.method === 'chat.send' && request.params.message === message).length, 1, 'One user click must produce exactly one chat.send')
}
async function quit() {
  const current = active
  current.quitRequested = true
  const checkpoint = await readFile(plan.logPath, 'utf8')
  // Playwright ElectronApplication.close invokes app.quit(), including the
  // production before-quit handler. No force kill or timeout fallback passes.
  const close = closeElectronAndObserveExit(current.app, current.identity, 100_000)
  let timer
  try {
    await Promise.race([close, new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('Normal app.quit exceeded 100 seconds; preserve the synthetic scene')), 100_000) })])
  } finally { clearTimeout(timer) }
  assert.equal(gatewayProcessSnapshot(current.ownership).alive, false, 'The owned Gateway must exit naturally')
  const evidence = desktopShutdownEvidenceSince(checkpoint, await readFile(plan.logPath, 'utf8'))
  assert.deepEqual(evidence, { gatewayExitLogged: true, committedExitLogged: true }, 'Quit must commit after clean Gateway exit without hard termination')
  assert.deepEqual(current.pageErrors, [], 'Unexpected renderer exceptions')
  Object.assign(report.cycles.at(-1), { normalQuitVerified: true, shutdown: evidence })
  active = null
  await assertPreservedInputs(plan)
}

try {
  assert.equal(process.platform, 'win32', 'This installed-executable probe is Windows-only')
  plan = await verifyAuditInputs(requiredOption('--audit-manifest'), requiredOption('--output-dir'))
  // mkdir without recursive fails if evidence already exists. Never overwrite
  // an old success or accept evidence through a redirected parent directory.
  const parent = dirname(plan.outputDir)
  assert.ok((await lstat(parent)).isDirectory())
  assert.equal((await realpath(parent)).toLowerCase(), resolve(parent).toLowerCase())
  await mkdir(plan.outputDir)
  outputCreated = true
  Object.assign(report, { auditId: plan.auditId, sourceSha: plan.sourceSha, executableSha256: plan.executableSha256, credentialSha256: plan.credentialSha256, configSha256: plan.configSha256, markerSha256: plan.markerSha256 })
  report.probeSources = await Promise.all(['test-packaged-retained-interaction.mjs', 'fixtures/packaged-retained-interaction/contract.mjs', 'fixtures/packaged-retained-interaction/provider.mjs', 'fixtures/packaged-retained-interaction/browser-probe.mjs'].map(async file => ({ file, sha256: sha256(await readFile(join(scriptDir, file))) })))
  await stage('preservation-before')
  await noExistingDesktop()
  await preserved('before')
  const messages = auditMessages(plan.auditId)
  const sentinelToken = `OPENSQUILLA_RETAINED_${randomBytes(32).toString('hex')}`
  const sentinelPath = join(plan.workspace, `retained-interaction-${plan.auditId}.txt`)
  await writeFile(sentinelPath, sentinelToken + '\n', { flag: 'wx', mode: 0o600 })
  report.sentinel = { path: sentinelPath, tokenSha256: sha256(sentinelToken) }
  provider = await startRetainedProvider({ ...plan.provider, messages, sentinelPath, sentinelTokenSha256: sha256(sentinelToken) })
  await stage('retained-profile-launch')
  let page = await launch('initial')
  await oldSessionUi(page, 'initial')
  await navigateSession(page, null)
  await stage('first-send')
  await send(page, messages.first, messages.firstAnswer)
  const sessionKey = new URL(page.url()).searchParams.get('session')
  assert.ok(sessionKey && !oldSessions.includes(sessionKey), 'New probe messages must not mutate either seeded session')
  report.sessionKey = sessionKey
  report.firstSendVerified = true
  await screenshot(page, 'first-send')
  await stage('real-read-file')
  await send(page, messages.tool, messages.toolAnswer)
  assert.equal(provider.snapshot().toolResults, 1)
  report.toolReadVerified = true
  await screenshot(page, 'read-file')
  await stage('real-stop')
  await page.locator('.chat-textarea').fill(messages.stop)
  await page.locator('.chat-send-btn.btn--primary').click()
  await page.locator('.msg-ai-text').filter({ hasText: messages.stopPartial }).last().waitFor({ state: 'visible', timeout: timeoutMs })
  assert.equal(provider.snapshot().held, 1)
  await screenshot(page, 'held-stream-before-stop')
  await page.locator('.chat-send-btn.btn--danger').click()
  await waitFor(async () => {
    assert.equal(provider.snapshot().cancelledBeforeCleanup, 1)
    return assertStopEvidence(await snapshot(page), sessionKey)
  }, 'a real Stop RPC, matching cancelled task event, and provider stream cancellation', timeoutMs)
  report.stop = assertStopEvidence(await snapshot(page), sessionKey)
  report.stopVerified = true
  await send(page, messages.afterStop, messages.afterStopAnswer)
  await screenshot(page, 'after-stop')
  report.initialRpc = await snapshot(page)
  assert.equal(report.initialRpc.requests.filter(request => request.method === 'chat.send').length, 4)
  await stage('normal-quit-before-restart')
  await quit()
  await preserved('before-restart')
  await stage('same-profile-restart')
  page = await launch('restart')
  assert.notEqual(report.cycles[0].gatewayStartIdentity, report.cycles[1].gatewayStartIdentity)
  await oldSessionUi(page, 'restart')
  await navigateSession(page, sessionKey)
  for (const answer of [messages.firstAnswer, messages.toolAnswer, messages.afterStopAnswer]) {
    await page.locator('.msg-ai-text').filter({ hasText: answer }).last().waitFor({ state: 'visible', timeout: timeoutMs })
  }
  await send(page, messages.restart, messages.restartAnswer)
  report.restartRpc = await snapshot(page)
  assert.equal(report.restartRpc.requests.filter(request => request.method === 'chat.send').length, 1)
  report.restartVerified = true
  await screenshot(page, 'restart-send')
  await stage('final-normal-quit')
  await quit()
  await preserved('after')
  report.provider = provider.snapshot()
  assert.deepEqual(report.provider, { first: 1, toolCalls: 1, toolResults: 1, held: 1, cancelledBeforeCleanup: 1, afterStop: 1, restart: 1, errors: [] })
  await provider.close()
  provider = null
  Object.assign(report, { ok: true, status: 'passed', stage: 'complete', credentialPreserved: true, configPreserved: true, oldSessionsVerified: true, oldSessionsUiVerified: true, normalQuitVerified: true, completedAt: new Date().toISOString() })
  await persist()
  console.log(JSON.stringify({ ok: true, report: join(plan.outputDir, 'report.json') }))
} catch (error) {
  Object.assign(report, { ok: false, status: 'failed', error: error.message, failedAt: new Date().toISOString(), provider: provider?.snapshot(), activeProcess: active?.identity })
  if (active?.page) {
    try { await screenshot(active.page, 'failure') } catch { /* Keep the primary failure. */ }
  }
  // Freeze Stop evidence before cleanup. Closing this fixture cannot be
  // credited as a user cancellation. Only this launch's app may be asked to
  // quit, once; never force-kill or rewrite a retained profile on failure.
  await provider?.close()
  provider = null
  if (active?.ownership && active?.identity && !active.quitRequested) {
    try {
      await quit()
      report.failureCleanup = { naturalQuit: true }
    } catch (cleanupError) {
      report.failureCleanup = { naturalQuit: false, error: cleanupError.message }
    }
  }
  if (active) report.operatorQuitRequired = true
  await persist()
  console.error(JSON.stringify({ ok: false, stage: report.stage, error: error.message, report: outputCreated ? join(plan.outputDir, 'report.json') : null }))
  process.exitCode = 1
} finally {
  await provider?.close()
}
