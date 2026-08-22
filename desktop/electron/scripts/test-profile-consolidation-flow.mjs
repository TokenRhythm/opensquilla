import { strict as assert } from 'node:assert'
import { spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { createServer } from 'node:http'
import {
  mkdir,
  mkdtemp,
  readFile,
  readdir,
  realpath,
  rm,
  stat,
  utimes,
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
const screenshotPath = String(
  process.env.OPENSQUILLA_DESKTOP_CONSOLIDATION_SCREENSHOT || '',
).trim()
const olderRecoveryId = '11234567-89ab-4cde-8fab-0123456789ab'
const newerRecoveryId = '21234567-89ab-4cde-8fab-0123456789ab'
const configOnlyRecoveryId = '31234567-89ab-4cde-8fab-0123456789ab'
const invalidCredentialRecoveryId = '41234567-89ab-4cde-8fab-0123456789ab'
const credentialOnlyRecoveryId = '51234567-89ab-4cde-8fab-0123456789ab'
const newerCredentialMarker = 'synthetic-newest-recovery-credential'
const observedRendererPages = new WeakSet()
const rendererDiagnostics = []
const PROFILE_CLI_TIMEOUT_MS = 120_000

async function waitFor(check, label, timeoutMs = 120_000) {
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

async function pathExists(path) {
  try {
    await stat(path)
    return true
  } catch (error) {
    if (error?.code === 'ENOENT') return false
    throw error
  }
}

function launchEnvironment(isolatedHome, sourceEnvironment = process.env) {
  const inherited = { ...sourceEnvironment }
  for (const name of Object.keys(inherited)) {
    if (name === 'DISPLAY' || name === 'XAUTHORITY') continue
    const upperName = name.toUpperCase()
    if (
      upperName.startsWith('OPENSQUILLA_')
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
    OPENSQUILLA_DESKTOP_DISABLE_AUTO_UPDATE: '1',
    OPENSQUILLA_OPENROUTER_LIVE_PRICING: '0',
    OPENSQUILLA_GATEWAY_WORKSPACE_DIR: '',
    OPENSQUILLA_WORKSPACE_DIR: '',
    OPENSQUILLA_GATEWAY_STATE_DIR: '',
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

function runProfileConsolidationCli(userData, primaryHome, isolatedHome) {
  const result = spawnSync(
    'uv',
    [
      'run',
      'opensquilla',
      'recovery',
      'consolidate-profiles',
      '--user-data', userData,
      '--primary-home', primaryHome,
      '--json',
    ],
    {
      cwd: repoRoot,
      encoding: 'utf8',
      env: {
        ...launchEnvironment(isolatedHome),
        OPENSQUILLA_RECOVERY_OFFLINE: '1',
        UV_CACHE_DIR: join(tmpdir(), 'opensquilla-consolidation-e2e-uv-cache'),
      },
      maxBuffer: 4 * 1024 * 1024,
      timeout: PROFILE_CLI_TIMEOUT_MS,
      killSignal: 'SIGTERM',
    },
  )
  if (result.error || result.signal || result.status !== 0) {
    const outcome = result.error?.code === 'ETIMEDOUT'
      ? `timed out after ${PROFILE_CLI_TIMEOUT_MS}ms`
      : `status=${result.status ?? 'null'} signal=${result.signal ?? 'null'}`
    throw new Error(
      `Profile consolidation fixture command failed (${outcome}): `
      + `${result.stderr || result.stdout}`,
    )
  }
  return JSON.parse(result.stdout)
}

async function startFakeProvider() {
  const server = createServer(async (request, response) => {
    for await (const _chunk of request) {
      // Drain request bodies so clients can reuse the loopback connection.
    }
    if (request.method === 'GET' && request.url?.endsWith('/models')) {
      response.writeHead(200, { 'content-type': 'application/json' })
      response.end(JSON.stringify({
        object: 'list',
        data: [{ id: 'synthetic-consolidation-model' }],
      }))
      return
    }
    response.writeHead(404, { 'content-type': 'application/json' })
    response.end(JSON.stringify({ error: { message: 'synthetic endpoint not found' } }))
  })
  await new Promise((resolveListen, rejectListen) => {
    server.once('error', rejectListen)
    server.listen(0, '127.0.0.1', resolveListen)
  })
  const address = server.address()
  assert(address && typeof address === 'object')
  return {
    port: address.port,
    close: () => new Promise((resolveClose, rejectClose) => {
      server.close((error) => error ? rejectClose(error) : resolveClose())
    }),
  }
}

function observeRenderer(page) {
  if (observedRendererPages.has(page)) return
  observedRendererPages.add(page)
  page.on('console', (message) => {
    rendererDiagnostics.push({
      type: `console:${message.type()}`,
      text: message.text().slice(0, 1_000),
    })
  })
  page.on('pageerror', (error) => {
    rendererDiagnostics.push({
      type: 'pageerror',
      text: String(error?.message || error).slice(0, 1_000),
    })
  })
}

async function controlPage(app) {
  try {
    return await waitFor(async () => {
      for (const candidate of app.windows()) {
        if (candidate.isClosed()) continue
        observeRenderer(candidate)
        await candidate.waitForLoadState('domcontentloaded', { timeout: 5_000 }).catch(() => {})
        let pathname = ''
        try {
          pathname = new URL(candidate.url()).pathname
        } catch {
          pathname = ''
        }
        if (pathname !== '/control/chat' && pathname !== '/control/chat/new') continue
        if (await candidate.locator('.chat-textarea').count().catch(() => 0)) return candidate
      }
      return null
    }, 'consolidated primary Control UI')
  } catch (error) {
    const windows = await Promise.all(app.windows().map(async (page) => ({
      url: page.url(),
      title: await page.title().catch(() => ''),
      body: await page.locator('body').innerText().catch(() => '').then(
        (value) => value.slice(0, 1_500),
      ),
    })))
    throw new Error(
      `${error.message}; windows=${JSON.stringify(windows)}; `
      + `renderer=${JSON.stringify(rendererDiagnostics.slice(-30))}`,
    )
  }
}

async function onboardingPage(app) {
  return await waitFor(async () => {
    for (const candidate of app.windows()) {
      if (candidate.isClosed()) continue
      observeRenderer(candidate)
      await candidate.waitForLoadState('domcontentloaded', { timeout: 5_000 }).catch(() => {})
      if (await candidate.locator('#setup-form').count().catch(() => 0)) return candidate
    }
    return null
  }, 'normal primary onboarding')
}

async function createLegacyRecovery({
  userData,
  recoveryId,
  providerPort,
  marker,
  credentialMarker = marker,
  credentialBytes = null,
  omitCredential = false,
  omitConfig = false,
  modifiedAt,
}) {
  const root = join(userData, 'recovery-profiles', recoveryId)
  const home = join(root, 'opensquilla')
  const workspace = join(home, 'workspace')
  const state = join(home, 'state')
  const sessionArchive = join(state, 'session-archive')
  await mkdir(workspace, { recursive: true })
  await mkdir(sessionArchive, { recursive: true })
  if (!omitConfig) {
    await writeFile(
      join(home, 'config.toml'),
      [
        '[llm]',
        'provider = "minimax_openai"',
        `model = ${JSON.stringify(marker)}`,
        `base_url = ${JSON.stringify(`http://127.0.0.1:${providerPort}/v1`)}`,
        '',
      ].join('\n'),
      'utf8',
    )
  }
  if (!omitCredential) {
    await writeFile(
      join(root, 'desktop-credential.json'),
      credentialBytes ?? JSON.stringify({
        provider: 'minimax_openai',
        model: marker,
        baseUrl: `http://127.0.0.1:${providerPort}/v1`,
        encryptedApiKey: credentialMarker,
        modelRoutingMode: 'direct',
        routerMode: 'disabled',
        searchProvider: 'duckduckgo',
        encryption: 'plain',
        createdAt: '2026-07-20T00:00:00.000Z',
        updatedAt: '2026-07-20T00:00:00.000Z',
      }, null, 2),
      'utf8',
    )
  }
  await writeFile(join(workspace, `${marker}.md`), `${marker}\n`, 'utf8')
  await writeFile(join(state, `${marker}.txt`), `${marker}\n`, 'utf8')
  await writeFile(join(sessionArchive, `${marker}.txt`), `${marker}\n`, 'utf8')
  await utimes(root, modifiedAt, modifiedAt)
}

const root = await realpath(await mkdtemp(join(tmpdir(), 'opensquilla-consolidation-e2e-')))
const userData = join(root, 'user-data')
const isolatedHome = join(root, 'home')
const primaryHome = join(userData, 'opensquilla')
const primaryCredential = join(userData, 'desktop-credential.json')
const recoveryProfiles = join(userData, 'recovery-profiles')
const contextPath = join(userData, 'desktop-profile-context.json')
const fakeProvider = await startFakeProvider()
let app

try {
  await mkdir(userData, { recursive: true })
  await mkdir(isolatedHome, { recursive: true })
  await createLegacyRecovery({
    userData,
    recoveryId: olderRecoveryId,
    providerPort: fakeProvider.port,
    marker: 'synthetic-older-recovery-model',
    modifiedAt: new Date('2026-07-20T00:00:00.000Z'),
  })
  await createLegacyRecovery({
    userData,
    recoveryId: newerRecoveryId,
    providerPort: fakeProvider.port,
    marker: 'synthetic-consolidation-model',
    credentialMarker: newerCredentialMarker,
    modifiedAt: new Date('2026-07-21T00:00:00.000Z'),
  })
  await writeFile(
    contextPath,
    JSON.stringify({
      schema_version: 1,
      active_profile_kind: 'recovery',
      active_recovery_id: olderRecoveryId,
      attention_acknowledgement: null,
      updated_at: '2026-07-21T00:00:00.000Z',
    }, null, 2),
    'utf8',
  )
  // A failed/partial first-run can leave only an empty credential shell. It is
  // not primary configuration authority and must be atomically replaced by
  // the selected recovery credential after consolidation.
  await writeFile(primaryCredential, '{}\n', 'utf8')

  // Simulate Electron stopping after the offline consolidation CLI returned
  // but before safeStorage credential adoption. The durable receipt must let
  // the next launch finish exactly once.
  const prelaunchConsolidation = runProfileConsolidationCli(
    userData,
    primaryHome,
    isolatedHome,
  )
  assert.equal(prelaunchConsolidation.outcome, 'consolidated')
  assert.equal(prelaunchConsolidation.credential_adoption_status, 'pending')
  assert.equal(
    prelaunchConsolidation.configuration_source_recovery_id,
    newerRecoveryId,
  )
  const archivedCredentialBytes = await readFile(
    prelaunchConsolidation.configuration_source_credential_path,
  )
  assert.equal(
    prelaunchConsolidation.configuration_source_credential_size,
    archivedCredentialBytes.length,
  )
  assert.equal(
    prelaunchConsolidation.configuration_source_credential_sha256,
    createHash('sha256').update(archivedCredentialBytes).digest('hex'),
  )
  assert.equal(await pathExists(primaryCredential), true)
  assert.equal(await readFile(primaryCredential, 'utf8'), '{}\n')
  assert.equal(await pathExists(recoveryProfiles), false)
  const consolidationReceiptPath = prelaunchConsolidation.receipt_path
  assert.equal(typeof consolidationReceiptPath, 'string')
  assert.equal(
    JSON.parse(await readFile(consolidationReceiptPath, 'utf8')).credential_adoption_status,
    'pending',
  )

  app = await electron.launch({
    args: ['--use-mock-keychain', `--user-data-dir=${userData}`, packageRoot],
    env: launchEnvironment(isolatedHome),
  })
  const page = await controlPage(app)

  assert.equal(await pathExists(recoveryProfiles), false)
  assert.equal(await pathExists(join(primaryHome, 'config.toml')), true)
  assert.equal(await pathExists(primaryCredential), true)
  assert.match(await readFile(join(primaryHome, 'config.toml'), 'utf8'), /synthetic-consolidation-model/)
  const credential = JSON.parse(await readFile(primaryCredential, 'utf8'))
  assert.equal(credential.model, 'synthetic-consolidation-model')
  assert.equal(credential.encryptedApiKey, newerCredentialMarker)
  assert.equal(
    JSON.parse(await readFile(consolidationReceiptPath, 'utf8')).credential_adoption_status,
    'complete',
  )
  assert.equal(
    await readFile(
      join(primaryHome, 'workspace', 'synthetic-older-recovery-model.md'),
      'utf8',
    ),
    'synthetic-older-recovery-model\n',
  )
  assert.equal(
    await readFile(join(primaryHome, 'workspace', 'synthetic-consolidation-model.md'), 'utf8'),
    'synthetic-consolidation-model\n',
  )
  assert.equal(
    await readFile(
      join(
        primaryHome,
        'state',
        'session-archive',
        'synthetic-older-recovery-model.txt',
      ),
      'utf8',
    ),
    'synthetic-older-recovery-model\n',
  )
  assert.equal(
    await readFile(
      join(
        primaryHome,
        'state',
        'session-archive',
        'synthetic-consolidation-model.txt',
      ),
      'utf8',
    ),
    'synthetic-consolidation-model\n',
  )
  assert.equal(
    await pathExists(join(primaryHome, 'state', 'synthetic-older-recovery-model.txt')),
    false,
  )
  assert.equal(
    await pathExists(join(primaryHome, 'state', 'synthetic-consolidation-model.txt')),
    false,
  )
  assert.equal(
    await readFile(
      join(
        primaryHome,
        'recovered-data',
        olderRecoveryId,
        'profile',
        'state',
        'synthetic-older-recovery-model.txt',
      ),
      'utf8',
    ),
    'synthetic-older-recovery-model\n',
  )
  assert.equal(
    await readFile(
      join(
        primaryHome,
        'recovered-data',
        newerRecoveryId,
        'profile',
        'state',
        'synthetic-consolidation-model.txt',
      ),
      'utf8',
    ),
    'synthetic-consolidation-model\n',
  )

  const context = JSON.parse(await readFile(contextPath, 'utf8'))
  assert.equal(context.active_profile_kind, 'primary')
  assert.equal(context.active_recovery_id, null)

  const backupRoot = join(userData, 'backups', 'profile-consolidation')
  const transactions = await readdir(backupRoot)
  assert.equal(transactions.length, 1)
  const archivedProfiles = join(backupRoot, transactions[0], 'recovery-profiles')
  assert.deepEqual((await readdir(archivedProfiles)).sort(), [
    olderRecoveryId,
    newerRecoveryId,
  ])
  assert.equal(
    await readFile(
      join(
        archivedProfiles,
        olderRecoveryId,
        'opensquilla',
        'state',
        'synthetic-older-recovery-model.txt',
      ),
      'utf8',
    ),
    'synthetic-older-recovery-model\n',
  )
  assert.equal(
    await readFile(
      join(
        archivedProfiles,
        newerRecoveryId,
        'opensquilla',
        'state',
        'synthetic-consolidation-model.txt',
      ),
      'utf8',
    ),
    'synthetic-consolidation-model\n',
  )

  for (const removedId of [
    'recoveryProfiles',
    'copyCredential',
    'continueRecovery',
    'createRecovery',
    'retryPrimary',
    'returnPrimary',
  ]) {
    assert.equal(await page.locator(`#${removedId}`).count(), 0, removedId)
  }
  if (screenshotPath) {
    await mkdir(dirname(screenshotPath), { recursive: true })
    await page.screenshot({ path: screenshotPath })
  }

  await app.close()
  app = undefined

  // A completed receipt is never replayed. If the user later removes the
  // primary credential, startup must offer normal onboarding instead of
  // resurrecting the archived historical secret.
  await rm(primaryCredential)
  app = await electron.launch({
    args: ['--use-mock-keychain', `--user-data-dir=${userData}`, packageRoot],
    env: launchEnvironment(isolatedHome),
  })
  await onboardingPage(app)
  assert.equal(await pathExists(primaryCredential), false)
  assert.equal(
    JSON.parse(await readFile(consolidationReceiptPath, 'utf8')).credential_adoption_status,
    'complete',
  )
  const completedReceiptLog = await readFile(join(userData, 'logs', 'desktop.log'), 'utf8')
  assert.equal(
    completedReceiptLog
      .trim()
      .split('\n')
      .map((line) => JSON.parse(line))
      .filter((event) => event.event === 'desktop_profile_consolidation_credential_adopted')
      .length,
    1,
  )

  await app.close()
  app = undefined

  // A recovery may be the newest valid configuration source without carrying
  // a Desktop credential. Consolidation still succeeds, consumes the legacy
  // profile, and presents normal primary onboarding for a fresh credential.
  const configOnlyUserData = join(root, 'config-only-user-data')
  const configOnlyHome = join(root, 'config-only-home')
  const configOnlyPrimaryHome = join(configOnlyUserData, 'opensquilla')
  await mkdir(configOnlyUserData, { recursive: true })
  await mkdir(configOnlyHome, { recursive: true })
  await createLegacyRecovery({
    userData: configOnlyUserData,
    recoveryId: configOnlyRecoveryId,
    providerPort: fakeProvider.port,
    marker: 'synthetic-config-only-recovery-model',
    omitCredential: true,
    modifiedAt: new Date('2026-07-22T00:00:00.000Z'),
  })
  app = await electron.launch({
    args: ['--use-mock-keychain', `--user-data-dir=${configOnlyUserData}`, packageRoot],
    env: launchEnvironment(configOnlyHome),
  })
  await onboardingPage(app)
  assert.equal(await pathExists(join(configOnlyUserData, 'recovery-profiles')), false)
  assert.equal(await pathExists(join(configOnlyPrimaryHome, 'config.toml')), true)
  assert.equal(await pathExists(join(configOnlyUserData, 'desktop-credential.json')), false)
  assert.match(
    await readFile(join(configOnlyPrimaryHome, 'config.toml'), 'utf8'),
    /synthetic-config-only-recovery-model/,
  )
  const configOnlyTransactions = await readdir(
    join(configOnlyUserData, 'backups', 'profile-consolidation'),
  )
  assert.equal(configOnlyTransactions.length, 1)
  assert.equal(
    JSON.parse(await readFile(
      join(
        configOnlyUserData,
        'backups',
        'profile-consolidation',
        configOnlyTransactions[0],
        'receipt.json',
      ),
      'utf8',
    )).credential_adoption_status,
    'not_required',
  )

  await app.close()
  app = undefined

  // A credential-only recovery still contains enough provider authority for
  // Electron to generate a canonical primary config. The config and
  // credential must be published as one settings transaction, without
  // interrupting startup with onboarding.
  const credentialOnlyUserData = join(root, 'credential-only-user-data')
  const credentialOnlyHome = join(root, 'credential-only-home')
  const credentialOnlyPrimaryHome = join(credentialOnlyUserData, 'opensquilla')
  const credentialOnlyPrimaryCredential = join(
    credentialOnlyUserData,
    'desktop-credential.json',
  )
  await mkdir(credentialOnlyUserData, { recursive: true })
  await mkdir(credentialOnlyHome, { recursive: true })
  await createLegacyRecovery({
    userData: credentialOnlyUserData,
    recoveryId: credentialOnlyRecoveryId,
    providerPort: fakeProvider.port,
    marker: 'synthetic-consolidation-model',
    credentialMarker: 'synthetic-credential-only-secret',
    omitConfig: true,
    modifiedAt: new Date('2026-07-22T12:00:00.000Z'),
  })
  app = await electron.launch({
    args: ['--use-mock-keychain', `--user-data-dir=${credentialOnlyUserData}`, packageRoot],
    env: launchEnvironment(credentialOnlyHome),
  })
  const credentialOnlyPage = await controlPage(app)
  assert.equal(await credentialOnlyPage.locator('#setup-form').count(), 0)
  assert.equal(await pathExists(join(credentialOnlyUserData, 'recovery-profiles')), false)
  const generatedConfig = await readFile(
    join(credentialOnlyPrimaryHome, 'config.toml'),
    'utf8',
  )
  assert.match(generatedConfig, /provider = "minimax_openai"/)
  assert.match(generatedConfig, /model = "synthetic-consolidation-model"/)
  assert.match(
    generatedConfig,
    new RegExp(`base_url = "http://127\\.0\\.0\\.1:${fakeProvider.port}/v1"`),
  )
  const generatedCredential = JSON.parse(
    await readFile(credentialOnlyPrimaryCredential, 'utf8'),
  )
  assert.equal(generatedCredential.provider, 'minimax_openai')
  assert.equal(generatedCredential.model, 'synthetic-consolidation-model')
  assert.equal(generatedCredential.encryptedApiKey, 'synthetic-credential-only-secret')
  assert.equal(generatedCredential.configAuthority, 'generated')
  assert.equal(generatedCredential.importTransactionId, '')
  const credentialOnlyTransactions = await readdir(
    join(credentialOnlyUserData, 'backups', 'profile-consolidation'),
  )
  assert.equal(credentialOnlyTransactions.length, 1)
  assert.equal(
    JSON.parse(await readFile(
      join(
        credentialOnlyUserData,
        'backups',
        'profile-consolidation',
        credentialOnlyTransactions[0],
        'receipt.json',
      ),
      'utf8',
    )).credential_adoption_status,
    'complete',
  )

  await app.close()
  app = undefined

  // Corrupt historical credential bytes are archived and reported, but they
  // must not make startup permanently fail. The copied primary configuration
  // remains usable and normal onboarding collects a new credential.
  const invalidUserData = join(root, 'invalid-credential-user-data')
  const invalidHome = join(root, 'invalid-credential-home')
  const invalidPrimaryHome = join(invalidUserData, 'opensquilla')
  await mkdir(invalidUserData, { recursive: true })
  await mkdir(invalidHome, { recursive: true })
  await createLegacyRecovery({
    userData: invalidUserData,
    recoveryId: invalidCredentialRecoveryId,
    providerPort: fakeProvider.port,
    marker: 'synthetic-invalid-credential-model',
    credentialBytes: '{ definitely-not-valid-json',
    modifiedAt: new Date('2026-07-23T00:00:00.000Z'),
  })
  app = await electron.launch({
    args: ['--use-mock-keychain', `--user-data-dir=${invalidUserData}`, packageRoot],
    env: launchEnvironment(invalidHome),
  })
  await onboardingPage(app)
  assert.equal(await pathExists(join(invalidUserData, 'recovery-profiles')), false)
  assert.equal(await pathExists(join(invalidPrimaryHome, 'config.toml')), true)
  assert.equal(await pathExists(join(invalidUserData, 'desktop-credential.json')), false)
  const invalidCredentialLog = await readFile(
    join(invalidUserData, 'logs', 'desktop.log'),
    'utf8',
  )
  const skippedCredentialEvent = invalidCredentialLog
    .trim()
    .split('\n')
    .map((line) => JSON.parse(line))
    .find((event) => event.event === 'desktop_profile_consolidation_credential_skipped')
  assert.equal(skippedCredentialEvent?.sourceRecoveryId, invalidCredentialRecoveryId)
  assert.equal(skippedCredentialEvent?.stableCode, 'archived_credential_invalid')
  const invalidTransactions = await readdir(
    join(invalidUserData, 'backups', 'profile-consolidation'),
  )
  assert.equal(invalidTransactions.length, 1)
  assert.equal(
    JSON.parse(await readFile(
      join(
        invalidUserData,
        'backups',
        'profile-consolidation',
        invalidTransactions[0],
        'receipt.json',
      ),
      'utf8',
    )).credential_adoption_status,
    'complete',
  )

  console.log(JSON.stringify({
    ok: true,
    activeProfile: 'primary',
    consumedRecoveryIds: [olderRecoveryId, newerRecoveryId],
    configurationSourceRecoveryId: newerRecoveryId,
    recoveryChoiceUiPresent: false,
    pendingReceiptRecoveredAfterCrash: true,
    completedReceiptDidNotResurrectCredential: true,
    configOnlySourceOnboarding: true,
    credentialOnlySourceGeneratedPrimary: true,
    invalidCredentialOnboarding: true,
    invalidCredentialStableCode: skippedCredentialEvent.stableCode,
  }, null, 2))
} catch (error) {
  const desktopLog = await readFile(join(userData, 'logs', 'desktop.log'), 'utf8').catch(() => '')
  const gatewayLog = await readFile(join(userData, 'logs', 'gateway.log'), 'utf8').catch(() => '')
  console.error(JSON.stringify({
    desktopLogTail: desktopLog.slice(-8_000),
    gatewayLogTail: gatewayLog.slice(-8_000),
    rendererDiagnostics: rendererDiagnostics.slice(-30),
  }, null, 2))
  throw error
} finally {
  await app?.close().catch(() => {})
  await fakeProvider.close().catch(() => {})
  await rm(root, { recursive: true, force: true }).catch(() => {})
}
