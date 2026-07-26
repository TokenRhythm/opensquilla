import { strict as assert } from 'node:assert'
import { spawnSync } from 'node:child_process'
import { createServer } from 'node:http'
import {
  lstat,
  mkdir,
  mkdtemp,
  readFile,
  readdir,
  realpath,
  rm,
  writeFile,
} from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'
import { fileURLToPath } from 'node:url'
import { _electron as electron } from 'playwright'

const scriptDir = dirname(fileURLToPath(import.meta.url))
const packageRoot = resolve(scriptDir, '..')
const repoRoot = resolve(packageRoot, '../..')
const screenshotPath = String(process.env.OPENSQUILLA_DESKTOP_RECOVERY_SCREENSHOT || '').trim()
const PRIMARY_SENTINEL = 'synthetic-primary-credential-must-not-be-copied'
const RECOVERY_SYNTHETIC_KEY = 'synthetic-loopback-only-recovery-key'
const FIRST_PROMPT = 'RECOVERY_E2E_FIRST_PROMPT'
const SECOND_PROMPT = 'RECOVERY_E2E_SECOND_PROMPT'
const REPLY = 'RECOVERY_E2E_REPLY'
const observedRendererPages = new WeakSet()
const rendererDiagnostics = []

async function waitFor(check, label, timeoutMs = 90_000) {
  const startedAt = Date.now()
  let lastError
  while (Date.now() - startedAt < timeoutMs) {
    try {
      const value = await check()
      if (value) return value
    } catch (error) {
      lastError = error
    }
    await delay(250)
  }
  throw new Error(`Timed out waiting for ${label}: ${lastError?.message || lastError || ''}`)
}

function runPython(source, args) {
  const result = spawnSync('uv', ['run', 'python', '-c', source, ...args], {
    cwd: repoRoot,
    encoding: 'utf8',
  })
  if (result.status !== 0) {
    throw new Error(`Python fixture command failed: ${result.stderr || result.stdout}`)
  }
  return result.stdout.trim()
}

async function snapshotTree(root) {
  const result = {}
  async function visit(path, relative = '') {
    const info = await lstat(path)
    assert.equal(info.isSymbolicLink(), false, `fixture must not contain symlinks: ${path}`)
    if (info.isDirectory()) {
      result[`${relative || '.'}/`] = 'directory'
      for (const name of (await readdir(path)).sort()) {
        await visit(join(path, name), relative ? `${relative}/${name}` : name)
      }
      return
    }
    assert.equal(info.isFile(), true, `fixture must contain only files/directories: ${path}`)
    result[relative] = (await readFile(path)).toString('base64')
  }
  await visit(root)
  return result
}

async function snapshotPrimary(profile, credentialPath) {
  return {
    profile: await snapshotTree(profile),
    credential: (await readFile(credentialPath)).toString('base64'),
  }
}

async function recoveryPage(app) {
  return await waitFor(async () => {
    for (const page of app.windows()) {
      if (page.isClosed()) continue
      await page.waitForLoadState('domcontentloaded', { timeout: 5_000 }).catch(() => {})
      if (await page.locator('#recoveryPanel.visible').count().catch(() => 0)) return page
    }
    return null
  }, 'unsafe-primary recovery page')
}

async function onboardingPage(app) {
  return await waitFor(async () => {
    for (const page of app.windows()) {
      if (page.isClosed()) continue
      await page.waitForLoadState('domcontentloaded', { timeout: 5_000 }).catch(() => {})
      if (await page.locator('#setup-form').count().catch(() => 0)) return page
    }
    return null
  }, 'recovery-profile onboarding window')
}

async function controlPage(app) {
  const observe = (page) => {
    if (observedRendererPages.has(page)) return
    observedRendererPages.add(page)
    page.on('console', (message) => {
      rendererDiagnostics.push({ type: `console:${message.type()}`, text: message.text().slice(0, 1_000) })
    })
    page.on('pageerror', (error) => {
      rendererDiagnostics.push({ type: 'pageerror', text: String(error?.message || error).slice(0, 1_000) })
    })
  }
  try {
    const page = await waitFor(async () => {
      for (const candidate of app.windows()) {
        if (candidate.isClosed()) continue
        observe(candidate)
        await candidate.waitForLoadState('domcontentloaded', { timeout: 5_000 }).catch(() => {})
        let pathname = ''
        try { pathname = new URL(candidate.url()).pathname } catch { pathname = '' }
        if (pathname !== '/control/chat' && pathname !== '/control/chat/new') continue
        if (await candidate.locator('.chat-textarea').count().catch(() => 0)) return candidate
      }
      return null
    }, 'recovery-profile Control UI', 120_000)
    await waitFor(() => {
      try { return new URL(page.url()).pathname === '/control/chat/new' } catch { return false }
    }, 'new-chat draft route', 30_000)
    return page
  } catch (error) {
    const windows = await Promise.all(app.windows().map(async (page) => ({
      url: page.url(),
      title: await page.title().catch(() => ''),
      body: await page.locator('body').innerText().catch(() => '').then((value) => value.slice(0, 1_500)),
    })))
    throw new Error(
      `${error.message}; windows=${JSON.stringify(windows)}; renderer=${JSON.stringify(rendererDiagnostics.slice(-30))}`,
    )
  }
}

async function sendChat(page, prompt) {
  const textarea = page.locator('.chat-textarea')
  await textarea.waitFor({ state: 'visible', timeout: 30_000 })
  try {
    await waitFor(async () => {
      // The Control UI can finish one last reactive render after its textarea
      // first becomes visible. Refill before sending if that render replaced
      // the input; never press Enter until the exact synthetic prompt remains.
      if (await textarea.inputValue().catch(() => '') !== prompt) {
        await textarea.fill(prompt)
      }
      return await page.locator('.chat-send-btn.is-ready').count().catch(() => 0)
    }, 'ready recovery chat composer', 10_000)
  } catch (error) {
    const state = await page.evaluate(() => ({
      href: window.location.href,
      sessionKey: document.querySelector('.chat-label')?.getAttribute('title') || '',
      textareaValue: document.querySelector('.chat-textarea')?.value || '',
      sendButtonClass: document.querySelector('.chat-send-btn')?.className || '',
      bodyText: document.body.innerText.slice(0, 1_000),
    })).catch(() => ({ unavailable: true }))
    throw new Error(`${error.message}; composer=${JSON.stringify(state)}`)
  }
  await textarea.press('Enter')
  await page.locator('.msg-ai').filter({ hasText: REPLY }).last().waitFor({
    state: 'visible',
    timeout: 60_000,
  })
  await waitFor(async () => (
    await page.locator('.chat-thread').getAttribute('aria-busy') === 'false'
  ), 'completed recovery chat turn', 60_000)
}

function launchEnvironment(isolatedHome, providerPort, sourceEnvironment = process.env) {
  const inherited = { ...sourceEnvironment }
  for (const name of Object.keys(inherited)) {
    if (name === 'DISPLAY' || name === 'XAUTHORITY') continue
    const upperName = name.toUpperCase()
    if (
      name.startsWith('OPENSQUILLA_')
      || ['HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY'].includes(upperName)
      || /(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH)/i.test(name)
      || /^(?:AWS|AZURE|GOOGLE|ANTHROPIC|OPENAI|OPENROUTER|MINIMAX|DEEPSEEK|GROQ|MISTRAL|COHERE|GEMINI|OLLAMA|XAI|MOONSHOT|DASHSCOPE|SILICONFLOW|ZHIPU|BAIDU|VOLCENGINE|TENCENT|ALIYUN|HF|HUGGINGFACE)_/i.test(name)
    ) {
      delete inherited[name]
    }
  }
  return {
    ...inherited,
    HOME: isolatedHome,
    USERPROFILE: isolatedHome,
    OPENSQUILLA_DESKTOP_REPO_ROOT: repoRoot,
    OPENSQUILLA_DESKTOP_SECRET_STORAGE: 'plain',
    OPENSQUILLA_USER_STATE_DIR: join(isolatedHome, 'user-state'),
    OPENSQUILLA_TEST_PROFILE_LOCK_ROOT: '1',
    OPENSQUILLA_DESKTOP_GATEWAY_PORT: '18898',
    OPENSQUILLA_DESKTOP_DISABLE_AUTO_UPDATE: '1',
    OPENSQUILLA_OPENROUTER_LIVE_PRICING: '0',
    OPENSQUILLA_GATEWAY_WORKSPACE_DIR: '',
    OPENSQUILLA_WORKSPACE_DIR: '',
    OPENSQUILLA_GATEWAY_STATE_DIR: '',
    OPENSQUILLA_E2E_PROVIDER_PORT: String(providerPort),
    PYTHONFAULTHANDLER: '1',
    HTTP_PROXY: 'http://127.0.0.1:1',
    HTTPS_PROXY: 'http://127.0.0.1:1',
    ALL_PROXY: 'http://127.0.0.1:1',
    NO_PROXY: '127.0.0.1,localhost',
    http_proxy: 'http://127.0.0.1:1',
    https_proxy: 'http://127.0.0.1:1',
    all_proxy: 'http://127.0.0.1:1',
    no_proxy: '127.0.0.1,localhost',
    LANG: 'en_US.UTF-8',
    LC_ALL: 'en_US.UTF-8',
  }
}

async function launchDesktop(userData, isolatedHome, providerPort) {
  return await electron.launch({
    args: ['--use-mock-keychain', `--user-data-dir=${userData}`, packageRoot],
    env: launchEnvironment(isolatedHome, providerPort),
  })
}

async function startFakeProvider() {
  const requests = []
  const server = createServer(async (request, response) => {
    const chunks = []
    for await (const chunk of request) chunks.push(chunk)
    const raw = Buffer.concat(chunks).toString('utf8')
    let payload = {}
    try { payload = raw ? JSON.parse(raw) : {} } catch { payload = {} }
    requests.push({ method: request.method, url: request.url, payload })

    if (request.method === 'GET' && request.url?.endsWith('/models')) {
      response.writeHead(200, { 'content-type': 'application/json' })
      response.end(JSON.stringify({ object: 'list', data: [{ id: 'synthetic-recovery-model' }] }))
      return
    }
    if (request.method === 'GET' && request.url?.endsWith('/api/tags')) {
      response.writeHead(200, { 'content-type': 'application/json' })
      response.end(JSON.stringify({ models: [{ name: 'synthetic-recovery-model' }] }))
      return
    }
    if (request.method !== 'POST' || !request.url?.endsWith('/chat/completions')) {
      response.writeHead(404, { 'content-type': 'application/json' })
      response.end(JSON.stringify({ error: { message: 'synthetic endpoint not found' } }))
      return
    }
    if (payload.stream === false) {
      response.writeHead(200, { 'content-type': 'application/json' })
      response.end(JSON.stringify({
        id: 'chatcmpl-recovery-title',
        object: 'chat.completion',
        model: 'synthetic-recovery-model',
        choices: [{ index: 0, message: { role: 'assistant', content: 'Recovery chat' }, finish_reason: 'stop' }],
        usage: { prompt_tokens: 8, completion_tokens: 2, total_tokens: 10 },
      }))
      return
    }
    response.writeHead(200, {
      'content-type': 'text/event-stream',
      'cache-control': 'no-cache',
      connection: 'close',
    })
    response.write(`data: ${JSON.stringify({
      id: 'chatcmpl-recovery-e2e',
      object: 'chat.completion.chunk',
      model: 'synthetic-recovery-model',
      choices: [{ index: 0, delta: { role: 'assistant', content: REPLY }, finish_reason: null }],
    })}\n\n`)
    response.write(`data: ${JSON.stringify({
      id: 'chatcmpl-recovery-e2e',
      object: 'chat.completion.chunk',
      model: 'synthetic-recovery-model',
      choices: [{ index: 0, delta: {}, finish_reason: 'stop' }],
      usage: { prompt_tokens: 12, completion_tokens: 3, total_tokens: 15 },
    })}\n\n`)
    response.end('data: [DONE]\n\n')
  })
  await new Promise((resolveListen, rejectListen) => {
    server.once('error', rejectListen)
    server.listen(0, '127.0.0.1', resolveListen)
  })
  const address = server.address()
  assert(address && typeof address === 'object')
  return {
    port: address.port,
    requests,
    close: () => new Promise((resolveClose, rejectClose) => {
      server.close((error) => error ? rejectClose(error) : resolveClose())
    }),
  }
}

const root = await realpath(await mkdtemp(join(tmpdir(), 'opensquilla-electron-recovery-test-')))
const userData = join(root, 'user-data')
const isolatedHome = join(root, 'home')
const primaryHome = join(userData, 'opensquilla')
const primaryWorkspace = join(primaryHome, 'workspace')
const primaryState = join(primaryHome, 'state')
const primaryDatabase = join(primaryState, 'sessions.db')
const primaryCredential = join(userData, 'desktop-credential.json')
const missingWorkspace = join(root, 'missing-external-workspace')

await mkdir(primaryWorkspace, { recursive: true })
await mkdir(primaryState, { recursive: true })
await mkdir(isolatedHome, { recursive: true })
for (const [name, text] of [
  ['USER.md', 'synthetic primary user\n'],
  ['SOUL.md', 'synthetic primary soul\n'],
  ['IDENTITY.md', 'synthetic primary identity\n'],
  ['MEMORY.md', 'synthetic primary memory\n'],
]) {
  await writeFile(join(primaryWorkspace, name), text, 'utf8')
}
await writeFile(
  join(primaryHome, 'config.toml'),
  [
    `state_dir = ${JSON.stringify(primaryState)}`,
    `workspace_dir = ${JSON.stringify(missingWorkspace)}`,
    '',
    '[llm]',
    'provider = "ollama"',
    'model = "primary-must-not-run"',
    'base_url = "http://127.0.0.1:9/v1"',
    '',
  ].join('\n'),
  'utf8',
)
await writeFile(
  primaryCredential,
  JSON.stringify({
    provider: 'ollama',
    model: 'primary-must-not-run',
    baseUrl: 'http://127.0.0.1:9/v1',
    encryptedApiKey: PRIMARY_SENTINEL,
    modelRoutingMode: 'direct',
    routerMode: 'disabled',
    searchProvider: 'duckduckgo',
    encryption: 'plain',
    createdAt: '2026-07-11T00:00:00.000Z',
    updatedAt: '2026-07-11T00:00:00.000Z',
  }, null, 2),
  'utf8',
)
runPython(
  'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); '
  + 'c.execute("CREATE TABLE synthetic_primary_sessions (id TEXT PRIMARY KEY, body TEXT)"); '
  + 'c.execute("INSERT INTO synthetic_primary_sessions VALUES (?, ?)", '
  + '("primary-session", "primary transcript must remain unchanged")); c.commit(); c.close()',
  [primaryDatabase],
)

const primaryBefore = await snapshotPrimary(primaryHome, primaryCredential)
const fakeProvider = await startFakeProvider()
const scrubbedEnvironmentProbe = launchEnvironment(isolatedHome, fakeProvider.port, {
  ...process.env,
  OPENAI_API_KEY: 'synthetic-real-provider-key-must-not-leak',
  AWS_PROFILE: 'synthetic-real-provider-profile-must-not-leak',
  OPENSQUILLA_STATE_DIR: '/synthetic/external/state/must-not-leak',
})
assert.equal(scrubbedEnvironmentProbe.OPENAI_API_KEY, undefined)
assert.equal(scrubbedEnvironmentProbe.AWS_PROFILE, undefined)
assert.equal(scrubbedEnvironmentProbe.OPENSQUILLA_STATE_DIR, undefined)
assert.equal(scrubbedEnvironmentProbe.HTTP_PROXY, 'http://127.0.0.1:1')
assert.equal(scrubbedEnvironmentProbe.NO_PROXY, '127.0.0.1,localhost')
let app
try {
  app = await launchDesktop(userData, isolatedHome, fakeProvider.port)
  const recovery = await recoveryPage(app)
  assert.equal(await recovery.locator('#recoveryCode').innerText(), 'effective_workspace_missing')
  assert.equal(await recovery.locator('#copyCredential').isChecked(), false)
  if (screenshotPath) {
    await mkdir(dirname(screenshotPath), { recursive: true })
    await recovery.screenshot({ path: screenshotPath })
  }
  await recovery.locator('#createRecovery').click()

  const setup = await onboardingPage(app)
  await setup.locator('[data-screen="0"].active .next-button').click()
  await setup.locator('[data-screen="1"].active').waitFor({ state: 'visible', timeout: 10_000 })
  await setup.locator('#providerMoreToggle').click()
  await setup.locator('#providerGrid [data-provider="minimax_openai"]').click()
  await setup.locator('#baseUrl').fill(`http://127.0.0.1:${fakeProvider.port}/v1`)
  await setup.locator('#model').fill('synthetic-recovery-model')
  await setup.locator('#apiKey').fill(RECOVERY_SYNTHETIC_KEY)
  await setup.locator('[data-screen="1"].active .next-button').click()
  await setup.locator('[data-screen="4"].active').waitFor({ state: 'visible', timeout: 10_000 })
  await setup.locator('#finish').click()

  const firstControl = await controlPage(app)
  const recoveryIds = (await readdir(join(userData, 'recovery-profiles'))).sort()
  assert.equal(recoveryIds.length, 1)
  const recoveryId = recoveryIds[0]
  assert.match(recoveryId, /^[0-9a-f-]{36}$/i)
  const recoveryRoot = join(userData, 'recovery-profiles', recoveryId)
  const recoveryHome = join(recoveryRoot, 'opensquilla')
  const recoveryWorkspace = join(recoveryHome, 'workspace')
  const recoveryState = join(recoveryHome, 'state')
  const recoveryCredential = await readFile(join(recoveryRoot, 'desktop-credential.json'), 'utf8')
  assert.doesNotMatch(recoveryCredential, new RegExp(PRIMARY_SENTINEL))
  assert.equal(JSON.parse(recoveryCredential).provider, 'minimax_openai')
  for (const name of ['USER.md', 'SOUL.md', 'IDENTITY.md', 'MEMORY.md']) {
    assert.equal((await lstat(join(recoveryWorkspace, name))).isFile(), true)
  }
  assert.deepEqual(await snapshotPrimary(primaryHome, primaryCredential), primaryBefore)

  await sendChat(firstControl, FIRST_PROMPT)
  await waitFor(
    () => fakeProvider.requests.some((item) => JSON.stringify(item.payload).includes(FIRST_PROMPT)),
    'first prompt at local fake provider',
  )
  assert.deepEqual(await snapshotPrimary(primaryHome, primaryCredential), primaryBefore)
  const recoverySessionKey = await waitFor(() => {
    try { return new URL(firstControl.url()).searchParams.get('session') } catch { return '' }
  }, 'first recovery chat route')

  await app.close()
  app = null
  const recoveryDatabase = join(recoveryState, 'sessions.db')
  const persistedSessions = JSON.parse(runPython(
    'import json,sqlite3,sys; c=sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True); '
    + 'rows=c.execute("SELECT session_key,display_name,derived_title,subject,status '
    + 'FROM sessions ORDER BY session_key").fetchall(); '
    + 'c.close(); print(json.dumps(rows))',
    [recoveryDatabase],
  ))
  assert.equal(persistedSessions.length, 1, JSON.stringify(persistedSessions))
  assert.equal(persistedSessions[0][0], recoverySessionKey)
  const firstTranscript = JSON.parse(runPython(
    'import json,sqlite3,sys; c=sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True); '
    + 'rows=c.execute("SELECT role,content FROM transcript_entries ORDER BY id").fetchall(); '
    + 'c.close(); print(json.dumps(rows))',
    [recoveryDatabase],
  ))
  assert(firstTranscript.some(([role, content]) => role === 'user' && String(content).includes(FIRST_PROMPT)))
  assert(firstTranscript.some(([role, content]) => role === 'assistant' && String(content).includes(REPLY)))
  assert.deepEqual(await snapshotPrimary(primaryHome, primaryCredential), primaryBefore)

  const persistedBeforeRestart = JSON.parse(
    await readFile(join(userData, 'desktop-profile-context.json'), 'utf8'),
  )
  assert.equal(persistedBeforeRestart.active_profile_kind, 'recovery')
  assert.equal(persistedBeforeRestart.active_recovery_id, recoveryId)

  app = await launchDesktop(userData, isolatedHome, fakeProvider.port)
  const restartedRecovery = await recoveryPage(app)
  assert.equal(
    await restartedRecovery.locator('#recoveryCode').innerText(),
    'desktop_recovery_profile_confirmation_required',
  )
  assert.equal(
    await restartedRecovery.locator('#recoveryTitle').innerText(),
    'Confirm recovery profile',
  )
  assert.equal(
    await restartedRecovery.locator('#recoveryIntro').innerText(),
    'OpenSquilla is waiting for you to confirm the selected isolated recovery profile before it starts. This does not mean your primary profile is unsafe.',
  )
  const persistedWhileBlocked = JSON.parse(
    await readFile(join(userData, 'desktop-profile-context.json'), 'utf8'),
  )
  assert.equal(persistedWhileBlocked.active_profile_kind, 'recovery')
  assert.equal(persistedWhileBlocked.active_recovery_id, recoveryId)
  const existingOptions = await restartedRecovery.locator('#recoveryProfiles option').evaluateAll((items) => (
    items.map((item) => ({ value: item.value, label: item.textContent || '' }))
  ))
  assert(existingOptions.some((item) => item.value === recoveryId && item.label.includes(recoveryHome)))
  await restartedRecovery.locator('#recoveryProfiles').selectOption(recoveryId)
  await restartedRecovery.locator('#continueRecovery').click()

  const secondControl = await controlPage(app)
  const activeState = await secondControl.evaluate(() => window.opensquillaDesktop.getRecoveryState())
  assert.equal(activeState.activeProfile.kind, 'recovery')
  assert.equal(activeState.activeProfile.recoveryId, recoveryId)
  assert.equal(activeState.activeProfile.home, recoveryHome)
  const persistedConversations = secondControl.locator('.sidebar-history-item')
  const persistedConversation = persistedConversations.first()
  await persistedConversation.waitFor({ state: 'visible', timeout: 30_000 })
  assert.equal(await persistedConversations.count(), 1)
  await persistedConversation.click()
  await waitFor(() => {
    try {
      return new URL(secondControl.url()).searchParams.get('session') === recoverySessionKey
    } catch {
      return false
    }
  }, 'persisted recovery chat route')
  await secondControl.locator('.msg-user').filter({ hasText: FIRST_PROMPT }).last().waitFor({
    state: 'visible',
    timeout: 30_000,
  })
  await secondControl.locator('.msg-ai').filter({ hasText: REPLY }).last().waitFor({
    state: 'visible',
    timeout: 30_000,
  })
  await sendChat(secondControl, SECOND_PROMPT)
  await waitFor(
    () => fakeProvider.requests.some((item) => JSON.stringify(item.payload).includes(SECOND_PROMPT)),
    'second prompt at local fake provider',
  )

  // Reset is a credential/onboarding action, not a profile wipe. Trigger it
  // from the live Control UI and prove that config, identity Markdown, and the
  // recovery chat database remain in place after the gateway has drained.
  const recoveryConfigBeforeReset = await readFile(join(recoveryHome, 'config.toml'))
  await secondControl.evaluate(() => {
    void window.opensquillaDesktop.resetDesktopSettings()
    return true
  })
  await onboardingPage(app)
  assert.deepEqual(
    await readFile(join(recoveryHome, 'config.toml')),
    recoveryConfigBeforeReset,
    'Reset setup must preserve config.toml byte-for-byte',
  )
  await assert.rejects(
    lstat(join(recoveryRoot, 'desktop-credential.json')),
    (error) => error && error.code === 'ENOENT',
  )
  for (const name of ['USER.md', 'SOUL.md', 'IDENTITY.md', 'MEMORY.md']) {
    assert.equal((await lstat(join(recoveryWorkspace, name))).isFile(), true)
  }

  await app.close()
  app = null
  const finalTranscript = JSON.parse(runPython(
    'import json,sqlite3,sys; c=sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True); '
    + 'rows=c.execute("SELECT role,content FROM transcript_entries ORDER BY id").fetchall(); '
    + 'c.close(); print(json.dumps(rows))',
    [recoveryDatabase],
  ))
  for (const prompt of [FIRST_PROMPT, SECOND_PROMPT]) {
    assert(finalTranscript.some(([role, content]) => role === 'user' && String(content).includes(prompt)))
  }
  assert(finalTranscript.filter(([role, content]) => role === 'assistant' && String(content).includes(REPLY)).length >= 2)
  assert.deepEqual(await snapshotPrimary(primaryHome, primaryCredential), primaryBefore)

  console.log(JSON.stringify({
    ok: true,
    recoveryId,
    chatsCompleted: 2,
    explicitContinueVerified: true,
    resetPreservedProfileData: true,
    primaryBytesUnchanged: true,
    localProviderRequests: fakeProvider.requests.length,
  }, null, 2))
} catch (error) {
  const requestSummary = fakeProvider.requests.map((item) => ({
    method: item.method,
    url: item.url,
    stream: item.payload?.stream,
  }))
  const recoveryIds = await readdir(join(userData, 'recovery-profiles')).catch(() => [])
  const desktopLog = await readFile(join(userData, 'logs', 'desktop.log'), 'utf8').catch(() => '')
  const gatewayPid = desktopLog
    .trim()
    .split('\n')
    .reverse()
    .map((line) => {
      try { return JSON.parse(line) } catch { return null }
    })
    .find((entry) => entry?.event === 'gateway_spawned' && Number.isSafeInteger(entry.pid))
    ?.pid
  let gatewayProcessTree = []
  const gatewaySamples = []
  let faulthandlerSignal = { attempted: false }
  let sessionsDbLsof = { attempted: false }
  if (process.platform === 'darwin' && gatewayPid && process.env.CI_REPORT_DIR) {
    const processResult = spawnSync('/bin/ps', ['-axo', 'pid=,ppid=,command='], {
      encoding: 'utf8',
      timeout: 10_000,
    })
    const processes = String(processResult.stdout || '')
      .split('\n')
      .map((line) => line.match(/^\s*(\d+)\s+(\d+)\s+(.*)$/))
      .filter(Boolean)
      .map((match) => ({ pid: Number(match[1]), ppid: Number(match[2]), command: match[3] }))
    const descendantPids = new Set([gatewayPid])
    let changed = true
    while (changed) {
      changed = false
      for (const candidate of processes) {
        if (!descendantPids.has(candidate.ppid) || descendantPids.has(candidate.pid)) continue
        descendantPids.add(candidate.pid)
        changed = true
      }
    }
    gatewayProcessTree = processes.filter((candidate) => descendantPids.has(candidate.pid))
    for (const candidate of gatewayProcessTree) {
      const samplePath = join(
        process.env.CI_REPORT_DIR,
        `gateway-process-${candidate.pid}.sample.txt`,
      )
      const result = spawnSync(
        '/usr/bin/sample',
        [String(candidate.pid), '3', '-file', samplePath],
        { encoding: 'utf8', timeout: 10_000 },
      )
      gatewaySamples.push({
        pid: candidate.pid,
        status: result.status,
        signal: result.signal,
        error: result.error?.message || '',
        stderr: String(result.stderr || '').slice(-1_000),
      })
    }
    if (recoveryIds.length === 1) {
      const sessionsDb = join(
        userData,
        'recovery-profiles',
        recoveryIds[0],
        'opensquilla',
        'state',
        'sessions.db',
      )
      const result = spawnSync('/usr/sbin/lsof', ['-n', '-P', '--', sessionsDb], {
        encoding: 'utf8',
        timeout: 10_000,
      })
      const output = `${result.stdout || ''}${result.stderr || ''}`
      await writeFile(join(process.env.CI_REPORT_DIR, 'sessions-db-lsof.txt'), output, 'utf8')
      sessionsDbLsof = { attempted: true, status: result.status, bytes: output.length }
    }
    const pythonChild = gatewayProcessTree.find((candidate) => (
      candidate.pid !== gatewayPid && /(?:^|[/\\])python(?:3(?:\.\d+)?)?(?:\s|$)/i.test(candidate.command)
    ))
    if (pythonChild) {
      try {
        process.kill(pythonChild.pid, 'SIGABRT')
        faulthandlerSignal = { attempted: true, pid: pythonChild.pid, sent: true }
        await delay(1_000)
      } catch (signalError) {
        faulthandlerSignal = {
          attempted: true,
          pid: pythonChild.pid,
          sent: false,
          error: signalError?.message || String(signalError),
        }
      }
    }
  }
  const gatewayLog = recoveryIds.length === 1
    ? await readFile(
      join(userData, 'recovery-profiles', recoveryIds[0], 'logs', 'gateway.log'),
      'utf8',
    ).catch(() => '')
    : ''
  const debugLog = recoveryIds.length === 1
    ? await readFile(
      join(userData, 'recovery-profiles', recoveryIds[0], 'opensquilla', 'logs', 'debug.log'),
      'utf8',
    ).catch(() => '')
    : ''
  const recoveryStateEntries = recoveryIds.length === 1
    ? await readdir(
      join(userData, 'recovery-profiles', recoveryIds[0], 'opensquilla', 'state'),
      { withFileTypes: true },
    ).then((entries) => entries.map((entry) => ({
      name: entry.name,
      kind: entry.isFile() ? 'file' : entry.isDirectory() ? 'directory' : 'other',
    }))).catch(() => [])
    : []
  console.error(JSON.stringify({
    requestSummary,
    desktopLogTail: desktopLog.slice(-4000),
    gatewayProcessTree,
    gatewaySamples,
    faulthandlerSignal,
    sessionsDbLsof,
    gatewayLogTail: gatewayLog.slice(-4000),
    debugLogTail: debugLog.slice(-8000),
    recoveryStateEntries,
  }, null, 2))
  throw error
} finally {
  await app?.close().catch(() => {})
  await fakeProvider.close().catch(() => {})
  await rm(root, { recursive: true, force: true }).catch(() => {})
}
