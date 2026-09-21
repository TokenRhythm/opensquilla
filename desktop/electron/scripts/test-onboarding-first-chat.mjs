import { strict as assert } from 'node:assert'
import { access, mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { createServer } from 'node:http'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'
import { fileURLToPath } from 'node:url'
import { _electron as electron } from 'playwright'

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
const draft = 'Reply with the fixture confirmation. Do not call any tools.'
const userDataRoot = await mkdtemp(join(tmpdir(), 'opensquilla-first-chat-'))
const isolatedHome = join(userDataRoot, 'home')
const userDataDir = join(userDataRoot, 'chromium-user-data')
await mkdir(isolatedHome, { recursive: true })
const requests = []
const diagnostics = []
const stages = []
let app
let desktop
let passed = false

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

// Do not let real model credentials, provider overrides, or proxy configuration
// from the caller leak into this isolated first-run acceptance test.
const env = Object.fromEntries(Object.entries(process.env).filter(([name]) => !(
  /(?:API_?KEY|ACCESS_TOKEN|AUTH_TOKEN|SECRET|PASSWORD|BASE_URL|_PROXY)$/i.test(name)
  || name.startsWith('OPENSQUILLA_')
)))
Object.assign(env, {
  HOME: isolatedHome, USERPROFILE: isolatedHome,
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
        if (connection?.status === 'ready') return page
      }
    }
    return null
  }, 'Gateway readiness before any setup input')
  const notice = desktop.locator('.chat-model-setup-notice')
  await notice.waitFor({ state: 'visible', timeout: 30_000 })
  assert.equal(await onboarding.locator('#apiKey').inputValue(), '')
  assert.equal(requests.filter(request => request.method === 'POST').length, 0)
  await screenshot('01-client-ready-before-setup')
  complete('empty first run reaches Gateway ready and shows the model setup notice')

  await onboarding.locator('#skip').click()
  await waitFor(() => onboarding.isClosed(), 'skip dismisses onboarding')
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
  assert.equal(await composer.inputValue(), draft, 'settings must retain the existing composer draft')
  await screenshot('03-configured-draft-preserved')
  complete('settings UI saves the local provider without a connection test and preserves the draft')

  await composer.press('Enter')
  await waitFor(async () => (await desktop.locator('.chat-thread').innerText()).includes(fixtureAnswer), 'the real Gateway/provider reply in the transcript', 60_000)
  assert.ok(requests.some(request => request.method === 'POST' && request.url === '/v1/chat/completions' && request.model === fixtureModel))
  await screenshot('04-first-chat-reply')
  complete('the first chat reaches the local OpenAI fixture and renders its response')
  passed = true
  if (outputDir) await writeFile(join(outputDir, 'report.json'), JSON.stringify({ ok: true, stages, requests, diagnostics }, null, 2) + '\n')
} catch (error) {
  await screenshot('failure').catch(() => {})
  if (outputDir) {
    // Logs contain only synthetic credentials, but avoid copying them into reports.
    const log = await readFile(join(userDataDir, 'logs', 'desktop.log'), 'utf8').catch(() => '')
    await writeFile(join(outputDir, 'report.json'), JSON.stringify({ ok: false, error: error.message, retainedFixtureProfile: userDataRoot, stages, requests, diagnostics, desktopLogTail: log.split('\n').slice(-50).join('\n').replaceAll(syntheticKey, '[fixture-key]') }, null, 2) + '\n')
  }
  throw error
} finally {
  await app?.close().catch(() => {})
  server.closeAllConnections()
  await new Promise(resolveClose => server.close(resolveClose))
  if (passed || !outputDir) await rm(userDataRoot, { recursive: true, force: true })
}
