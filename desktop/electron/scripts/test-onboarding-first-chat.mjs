import { strict as assert } from 'node:assert'
import { access, mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { createServer } from 'node:http'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'
import { fileURLToPath } from 'node:url'
import { _electron as electron } from 'playwright'
import {
  loadDesktopGatewayOwnershipRecord,
  readVerifiedDesktopGatewayActivity,
} from '../dist/desktop-gateway-ownership.js'
import {
  canAcceptWindowsElectronShutdownFallback,
  closeElectronWithDeadline,
  closeHttpServerWithDeadline,
  desktopShutdownEvidenceSince,
  gatewayProcessSnapshot,
  trackHttpServerConnections,
} from './e2e-shutdown-helpers.mjs'
import {
  captureElectronProcessIdentity,
  captureFirstSendDiagnostic,
  electronProcessSnapshot,
} from './packaged-first-send-cleanup.mjs'

const packageRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const repoRoot = resolve(packageRoot, '../..')
const executablePath = process.env.OPENSQUILLA_DESKTOP_TEST_ELECTRON_EXECUTABLE
if (executablePath) await access(executablePath)
await access(join(packageRoot, 'dist', 'main.js'))

const outputDir = process.env.OPENSQUILLA_DESKTOP_FIRST_CHAT_OUTPUT_DIR
  ? resolve(process.env.OPENSQUILLA_DESKTOP_FIRST_CHAT_OUTPUT_DIR)
  : null
if (outputDir) await mkdir(outputDir, { recursive: true })
const fixtureAnswer = 'FIRST_CHAT_LOCAL_FIXTURE_OK'
const fixtureModel = 'gpt-4o-mini'
const syntheticKey = 'sk-local-first-chat-fixture-only'
async function composerValue(locator) {
  return await locator.evaluate((element) => {
    const value = element?.value
    return typeof value === 'string' ? value : element?.textContent ?? ''
  })
}

const draft = 'Reply with the fixture confirmation. Do not call any tools.'
const userDataRoot = await mkdtemp(join(tmpdir(), 'opensquilla-first-chat-'))
const isolatedHome = join(userDataRoot, 'home')
const userDataDir = join(userDataRoot, 'chromium-user-data')
const appData = join(isolatedHome, 'AppData', 'Roaming')
const localAppData = join(isolatedHome, 'AppData', 'Local')
const localTemp = join(localAppData, 'Temp')
await Promise.all([
  mkdir(appData, { recursive: true }),
  mkdir(localAppData, { recursive: true }),
  mkdir(localTemp, { recursive: true }),
])
const requests = []
const diagnostics = []
const stages = []
let app
let desktop
let passed = false
let runError = null
let lastActivity = null
let appIdentity = null
let gatewayRecord = null

const server = createServer((request, response) => {
  const chunks = []
  request.on('data', chunk => chunks.push(chunk))
  request.on('end', () => {
    let body = {}
    try { body = JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}') } catch {}
    // Keep diagnostics free of authorization values and request conversation text.
    requests.push({ method: request.method, url: request.url, model: body.model, stream: body.stream })
    if (request.method === 'GET' && request.url === '/v1/models') {
      response.writeHead(200, { 'content-type': 'application/json' })
      response.end(JSON.stringify({ object: 'list', data: [{ id: fixtureModel, object: 'model', owned_by: 'local-fixture' }] }))
      return
    }
    if (request.method !== 'POST' || request.url !== '/v1/chat/completions') {
      response.writeHead(404, { 'content-type': 'application/json' })
      response.end(JSON.stringify({ error: { message: 'Unexpected local fixture endpoint' } }))
      return
    }
    if (request.headers.authorization !== `Bearer ${syntheticKey}`) {
      response.writeHead(401, { 'content-type': 'application/json' })
      response.end(JSON.stringify({ error: { message: 'Synthetic credential mismatch' } }))
      return
    }
    const base = { id: 'chatcmpl-first-chat-fixture', created: 0, model: fixtureModel }
    if (!body.stream) {
      response.writeHead(200, { 'content-type': 'application/json' })
      response.end(JSON.stringify({ ...base, object: 'chat.completion', choices: [{ index: 0, message: { role: 'assistant', content: fixtureAnswer }, finish_reason: 'stop' }], usage: { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 } }))
      return
    }
    response.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' })
    const frames = [
      { ...base, object: 'chat.completion.chunk', choices: [{ index: 0, delta: { role: 'assistant', content: fixtureAnswer }, finish_reason: null }] },
      { ...base, object: 'chat.completion.chunk', choices: [{ index: 0, delta: {}, finish_reason: 'stop' }], usage: { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 } },
    ]
    response.end(frames.map(frame => `data: ${JSON.stringify(frame)}\n\n`).join('') + 'data: [DONE]\n\n')
  })
})
const serverSockets = trackHttpServerConnections(server)
await new Promise((resolveListen, rejectListen) => {
  server.once('error', rejectListen)
  server.listen(0, '127.0.0.1', resolveListen)
})
const baseUrl = `http://127.0.0.1:${server.address().port}/v1`

async function waitFor(check, label, timeoutMs = 60_000) {
  const started = Date.now()
  let lastError
  while (Date.now() - started < timeoutMs) {
    try { const value = await check(); if (value) return value } catch (error) { lastError = error }
    await delay(200)
  }
  throw new Error(`Timed out waiting for ${label}${lastError ? `: ${lastError.message}` : ''}`)
}

async function screenshot(name, page = desktop) {
  if (outputDir && page && !page.isClosed()) await page.screenshot({ path: join(outputDir, `${name}.png`), fullPage: true })
}
function complete(stage) {
  stages.push(stage)
  console.log(`PASS ${stage}`)
}

async function waitForFirstChatIdle(deadline) {
  const connection = await captureFirstSendDiagnostic(
    () => desktop.evaluate(() => window.opensquillaDesktop.getGatewayConnection()),
    Math.max(1, deadline - Date.now()),
  )
  if (connection?.diagnosticError) throw new Error(connection.diagnosticError)
  assert.equal(connection.status, 'ready')
  assert.match(connection.profileFingerprint, /^[0-9a-f]{64}$/)
  const loaded = loadDesktopGatewayOwnershipRecord(
    join(userDataDir, 'gateway-ownership', connection.profileFingerprint),
  )
  assert.equal(loaded.status, 'valid', 'the current Gateway must publish its ownership record')
  assert.equal(loaded.record.profile_fingerprint, connection.profileFingerprint)
  assert.equal(loaded.record.port, Number(new URL(connection.httpUrl).port))
  gatewayRecord = loaded.record
  while (Date.now() < deadline) {
    lastActivity = await readVerifiedDesktopGatewayActivity(loaded.record, {
      timeoutMs: Math.max(1, Math.min(1_000, deadline - Date.now())),
    })
    if (Date.now() < deadline && lastActivity.kind === 'ok' && lastActivity.activeCount === 0) return
    await delay(Math.max(0, Math.min(200, deadline - Date.now())))
  }
  throw new Error(`The first chat did not become idle within its completion budget: ${JSON.stringify(lastActivity)}`)
}

async function finishFirstChat() {
  const recordError = error => { runError ??= error }
  if (app) {
    try {
      const logPath = join(userDataDir, 'logs', 'desktop.log')
      const checkpoint = await readFile(logPath, 'utf8').catch(() => null)
      const shutdown = await closeElectronWithDeadline({
        app,
        phase: 'onboarding-first-chat',
        timeoutMs: 15_000,
        diagnostics: async () => ({ lastActivity, completedStages: [...stages] }),
      })
      if (!shutdown.closed) {
        const log = await readFile(logPath, 'utf8').catch(() => null)
        const fallbackAccepted = canAcceptWindowsElectronShutdownFallback({
          shutdown, ...desktopShutdownEvidenceSince(checkpoint, log),
        }) && electronProcessSnapshot(appIdentity).electronPidExists === false
          && gatewayRecord && !gatewayProcessSnapshot(gatewayRecord).alive
        if (!fallbackAccepted) throw shutdown.error
        console.warn(JSON.stringify({
          event: 'desktop_e2e_windows_shell_wrapper_reaped_after_commit',
          phase: 'onboarding-first-chat',
        }))
      }
      app = null
    } catch (error) { recordError(error) }
  }
  server.closeAllConnections()
  try {
    await closeHttpServerWithDeadline(server, serverSockets, {
      label: 'first-chat provider shutdown', timeoutMs: 15_000,
    })
  } catch (error) { recordError(error) }
  if (passed && !runError) {
    try { await rm(userDataRoot, { recursive: true, force: true }) } catch (error) { recordError(error) }
  }
  if (outputDir) {
    const log = runError
      ? await readFile(join(userDataDir, 'logs', 'desktop.log'), 'utf8').catch(() => '') : ''
    const report = {
      ok: passed && !runError, stages, requests, diagnostics, lastActivity,
      ...(runError ? {
        error: runError.message,
        retainedFixtureProfile: userDataRoot,
        desktopLogTail: log.split('\n').slice(-50).join('\n').replaceAll(syntheticKey, '[fixture-key]'),
      } : {}),
    }
    try {
      await writeFile(join(outputDir, 'report.json'), JSON.stringify(report, null, 2) + '\n')
    } catch (error) { recordError(error) }
  }
  if (runError) throw runError
}

// Do not let real model credentials, provider overrides, or proxy configuration
// from the caller leak into this isolated first-run acceptance test.
const env = Object.fromEntries(Object.entries(process.env).filter(([name]) => !(
  /(?:API_?KEY|ACCESS_TOKEN|AUTH_TOKEN|SECRET|PASSWORD|BASE_URL|_PROXY)$/i.test(name)
  || name.startsWith('OPENSQUILLA_')
)))
Object.assign(env, {
  HOME: isolatedHome, USERPROFILE: isolatedHome,
  APPDATA: appData, LOCALAPPDATA: localAppData,
  TEMP: localTemp, TMP: localTemp,
  OPENSQUILLA_USER_STATE_DIR: localAppData,
  OPENSQUILLA_TEST_PROFILE_LOCK_ROOT: '1',
  OPENSQUILLA_DESKTOP_REPO_ROOT: repoRoot,
  OPENSQUILLA_DESKTOP_SECRET_STORAGE: 'plain',
  OPENSQUILLA_DESKTOP_DISABLE_AUTO_UPDATE: '1',
  OPENSQUILLA_TESTING: '1',
  OPENSQUILLA_TELEMETRY_DISABLED: '1',
  OPENSQUILLA_PRIVACY_DISABLE_NETWORK_OBSERVABILITY: '1',
  OPENSQUILLA_UPDATE_CHECK_DISABLED: '1',
  UV_PROJECT_ENVIRONMENT: process.env.UV_PROJECT_ENVIRONMENT || join(repoRoot, '.venv'),
  PYTHONPATH: join(repoRoot, 'src'),
  LANG: 'en_US.UTF-8', LC_ALL: 'en_US.UTF-8',
})

try {
  app = await electron.launch({ ...(executablePath ? { executablePath } : {}), args: ['--use-mock-keychain', `--user-data-dir=${userDataDir}`, packageRoot], env })
  appIdentity = await captureElectronProcessIdentity(app)
  app.context().on('page', page => page.on('pageerror', error => diagnostics.push(error.message)))
  const onboarding = await waitFor(async () => {
    for (const page of app.windows()) {
      if (!page.isClosed() && await page.locator('#setup-form').count()) return page
    }
    return null
  }, 'onboarding page')
  desktop = await waitFor(async () => {
    for (const page of app.windows()) {
      if (!page.isClosed() && page.url().startsWith('opensquilla-app://desktop/')) {
        const connection = await page.evaluate(() => window.opensquillaDesktop?.getGatewayConnection?.())
        if (connection?.status !== 'ready') return page
      }
    }
    return null
  }, 'local Desktop renderer before setup is dismissed')
  assert.equal(await onboarding.locator('#apiKey').inputValue(), '')
  assert.equal(requests.filter(request => request.method === 'POST').length, 0)
  await screenshot('01-client-before-setup')
  complete('empty first run shows onboarding before Gateway readiness')

  await onboarding.locator('#skip').click()
  await waitFor(() => onboarding.isClosed(), 'skip dismisses onboarding')
  desktop = await waitFor(async () => {
    for (const page of app.windows()) {
      if (!page.isClosed() && page.url().startsWith('opensquilla-app://desktop/')) {
        const connection = await page.evaluate(() => window.opensquillaDesktop?.getGatewayConnection?.())
        if (connection?.status === 'ready') return page
      }
    }
    return null
  }, 'Gateway readiness after setup is dismissed')
  const notice = desktop.locator('.chat-model-setup-notice')
  await notice.waitFor({ state: 'visible', timeout: 30_000 })
  await desktop.bringToFront()
  const composer = desktop.locator('.chat-textarea')
  await composer.fill(draft)
  await notice.locator('button').click()
  await waitFor(() => desktop.url().includes('/settings/provider'), 'model settings from the notice UI')
  const settings = desktop.locator('.settings-modal')
  await settings.waitFor({ state: 'visible' })
  await desktop.locator('[data-provider-picker-trigger]').click()
  const editor = desktop.locator('#setup-provider-editor-dialog')
  await editor.getByRole('option', { name: /^OpenAI openai$/ }).click()
  await editor.locator('input[name="setup_provider_base_url"]').fill(baseUrl)
  await editor.locator('input[name="setup_provider_api_key"]').fill(syntheticKey)
  await editor.locator('input[name="setup_provider_model"]').fill(fixtureModel)
  await editor.locator('input[name="setup_provider_model"]').press('Tab')
  await screenshot('02-settings-local-provider')
  // Do not click either optional connection-test button: saving is sufficient.
  await editor.locator('.setup-provider-modal__footer .btn--primary').click()
  await editor.waitFor({ state: 'hidden', timeout: 30_000 })
  assert.equal(requests.filter(request => request.method === 'POST').length, 0, 'saving must not require a chat probe')
  await settings.getByRole('button', { name: /^(Close|关闭)$/ }).click()
  await settings.waitFor({ state: 'hidden' })
  await notice.waitFor({ state: 'hidden', timeout: 30_000 })
  assert.equal(await composerValue(composer), draft, 'settings must retain the existing composer draft')
  await screenshot('03-configured-draft-preserved')
  complete('settings UI saves the local provider without a connection test and preserves the draft')

  const firstChatDeadline = Date.now() + 60_000
  await composer.press('Enter')
  await waitFor(async () => (await desktop.locator('.chat-thread').innerText()).includes(fixtureAnswer), 'the real Gateway/provider reply in the transcript', Math.max(1, firstChatDeadline - Date.now()))
  assert.ok(requests.some(request => request.method === 'POST' && request.url === '/v1/chat/completions' && request.model === fixtureModel))
  await screenshot('04-first-chat-reply')
  complete('the first chat reaches the local OpenAI fixture and renders its response')
  // A streamed reply precedes task finalization. This acceptance case must let
  // the real task finish, so an interruption prompt remains a shutdown failure.
  await waitForFirstChatIdle(firstChatDeadline)
  complete('the first chat and Gateway background work finish before quitting')
  passed = true
} catch (error) {
  runError = error
  await screenshot('failure').catch(() => {})
} finally {
  await finishFirstChat()
}
