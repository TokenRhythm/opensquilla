import { strict as assert } from 'node:assert'
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

const LOCALES = {
  en: {
    title: 'Starting OpenSquilla',
    bootAria: 'OpenSquilla startup',
    repairTitle: 'Your primary profile needs recovery',
    cleanupTitle: 'A data cleanup stopped partway through',
    retry: 'Retry',
  },
  'zh-Hans': {
    title: '正在启动 OpenSquilla',
    bootAria: 'OpenSquilla 启动',
    repairTitle: '主配置需要恢复',
    cleanupTitle: '数据清理在中途停止',
    retry: '重试',
  },
  ja: {
    title: 'OpenSquilla を起動しています',
    bootAria: 'OpenSquilla の起動',
    repairTitle: 'プライマリプロファイルの復旧が必要です',
    cleanupTitle: 'データのクリーンアップが途中で停止しました',
    retry: '再試行',
  },
  fr: {
    title: "Démarrage d'OpenSquilla",
    bootAria: "Démarrage d'OpenSquilla",
    repairTitle: 'Le profil principal doit être récupéré',
    cleanupTitle: 'Un nettoyage des données s’est arrêté en cours de route',
    retry: 'Réessayer',
  },
  de: {
    title: 'OpenSquilla wird gestartet',
    bootAria: 'OpenSquilla-Start',
    repairTitle: 'Das Hauptprofil muss wiederhergestellt werden',
    cleanupTitle: 'Eine Datenbereinigung wurde unterbrochen',
    retry: 'Wiederholen',
  },
  es: {
    title: 'Iniciando OpenSquilla',
    bootAria: 'Inicio de OpenSquilla',
    repairTitle: 'El perfil principal necesita recuperación',
    cleanupTitle: 'Una limpieza de datos se detuvo a mitad de camino',
    retry: 'Reintentar',
  },
}

const BLOCKING_CASES = {
  en: { fixture: 'missing-workspace', stableCode: 'effective_workspace_missing' },
  'zh-Hans': { fixture: 'corrupt-config', stableCode: 'config_invalid' },
  ja: { fixture: 'future-config', stableCode: 'config_schema_too_new' },
  fr: { fixture: 'unfinished-transaction', stableCode: 'transaction_incomplete' },
  de: { fixture: 'unsafe-database', stableCode: 'state_database_invalid' },
  es: { fixture: 'cleanup-transaction', stableCode: 'cleanup_transaction_incomplete' },
}

const REMOVED_PROFILE_CONTROLS = [
  'recoveryProfiles',
  'copyCredential',
  'continueRecovery',
  'createRecovery',
  'retryPrimary',
  'returnPrimary',
]

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

function launchEnvironment(isolatedHome) {
  const inherited = { ...process.env }
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
    OPENSQUILLA_DESKTOP_GATEWAY_PORT: '18897',
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

async function createFixture(locale, blockingCase) {
  const root = await realpath(await mkdtemp(join(tmpdir(), `opensquilla-primary-repair-${locale}-`)))
  const userData = join(root, 'user-data')
  const isolatedHome = join(root, 'home')
  const primaryHome = join(userData, 'opensquilla')
  const primaryWorkspace = join(primaryHome, 'workspace')
  const primaryState = join(primaryHome, 'state')
  const missingWorkspace = join(root, 'missing-external-workspace')

  await mkdir(primaryWorkspace, { recursive: true })
  await mkdir(primaryState, { recursive: true })
  await mkdir(isolatedHome, { recursive: true })
  for (const [name, text] of [
    ['USER.md', 'synthetic accessibility user\n'],
    ['SOUL.md', 'synthetic accessibility soul\n'],
    ['IDENTITY.md', 'synthetic accessibility identity\n'],
    ['MEMORY.md', 'synthetic accessibility memory\n'],
  ]) {
    await writeFile(join(primaryWorkspace, name), text, 'utf8')
  }
  await writeFile(join(primaryState, 'primary-must-not-change.txt'), 'unchanged\n', 'utf8')

  let config = `state_dir = ${JSON.stringify(primaryState)}\n`
  if (blockingCase.fixture === 'missing-workspace') {
    config = [
      `state_dir = ${JSON.stringify(primaryState)}`,
      `workspace_dir = ${JSON.stringify(missingWorkspace)}`,
      '',
    ].join('\n')
  } else if (blockingCase.fixture === 'corrupt-config') {
    config = 'workspace_dir = "unterminated\n'
  } else if (blockingCase.fixture === 'future-config') {
    config = 'config_version = 999\n'
  }
  await writeFile(join(primaryHome, 'config.toml'), config, 'utf8')
  if (blockingCase.fixture === 'unsafe-database') {
    await writeFile(join(primaryState, 'sessions.db'), 'not a sqlite database', 'utf8')
  }

  let journalPath = null
  let journalBytes = null
  if (blockingCase.fixture === 'unfinished-transaction') {
    journalPath = join(userData, '.opensquilla.profile-replace.json')
    journalBytes = '{"schema_version":1,"phase":"prepared"}\n'
    await writeFile(journalPath, journalBytes, 'utf8')
  } else if (blockingCase.fixture === 'cleanup-transaction') {
    journalPath = join(userData, '.opensquilla.profile-cleanup.json')
    journalBytes = 'synthetic interrupted cleanup authority\n'
    await writeFile(journalPath, journalBytes, 'utf8')
  }
  await writeFile(join(userData, 'desktop-locale'), locale, 'utf8')

  return {
    root,
    userData,
    isolatedHome,
    primaryHome,
    journalPath,
    journalBytes,
    primaryBefore: await snapshotTree(primaryHome),
  }
}

async function repairPage(app) {
  return await waitFor(async () => {
    for (const page of app.windows()) {
      if (page.isClosed()) continue
      await page.waitForLoadState('domcontentloaded', { timeout: 5_000 }).catch(() => {})
      if (await page.locator('#recoveryPanel.visible').count().catch(() => 0)) return page
    }
    return null
  }, 'localized primary repair page')
}

function durationSeconds(value) {
  if (value.endsWith('ms')) return Number.parseFloat(value) / 1_000
  if (value.endsWith('s')) return Number.parseFloat(value)
  return Number.NaN
}

async function assertReducedMotion(page) {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  const result = await page.evaluate(() => {
    const bodyWasErrored = document.body.classList.contains('errored')
    const blockedStyle = getComputedStyle(document.querySelector('.status-line'), '::before')
    const blockedAnimationName = blockedStyle.animationName
    document.body.classList.remove('errored')
    const reducedStyle = getComputedStyle(document.querySelector('.status-line'), '::before')
    const snapshot = {
      mediaMatches: matchMedia('(prefers-reduced-motion: reduce)').matches,
      blockedAnimationName,
      animationName: reducedStyle.animationName,
      animationDuration: reducedStyle.animationDuration,
      animationIterationCount: reducedStyle.animationIterationCount,
      scrollBehavior: getComputedStyle(document.documentElement).scrollBehavior,
    }
    if (bodyWasErrored) document.body.classList.add('errored')
    return snapshot
  })
  assert.equal(result.mediaMatches, true)
  assert.equal(result.blockedAnimationName, 'none')
  assert.equal(result.animationName, 'progress')
  assert(durationSeconds(result.animationDuration) <= 0.00001, result.animationDuration)
  assert.equal(result.animationIterationCount, '1')
  assert.equal(result.scrollBehavior, 'auto')
}

const completedLocales = []
const completedBlockingCodes = []
for (const [locale, expected] of Object.entries(LOCALES)) {
  const blockingCase = BLOCKING_CASES[locale]
  assert(blockingCase, `missing blocking fixture for ${locale}`)
  const fixture = await createFixture(locale, blockingCase)
  let app
  try {
    app = await electron.launch({
      args: ['--use-mock-keychain', `--user-data-dir=${fixture.userData}`, packageRoot],
      env: launchEnvironment(fixture.isolatedHome),
    })
    const page = await repairPage(app)
    await waitFor(
      async () => await page.locator('html').getAttribute('lang') === locale,
      `${locale} locale application`,
    )
    assert.equal(await page.title(), expected.title)
    assert.equal(await page.locator('main.boot').getAttribute('aria-label'), expected.bootAria)
    assert.equal(await page.locator('#recoveryCode').innerText(), blockingCase.stableCode)
    assert.equal(
      await page.locator('#recoveryTitle').innerText(),
      blockingCase.fixture === 'cleanup-transaction'
        ? expected.cleanupTitle
        : expected.repairTitle,
    )
    assert.equal(await page.locator('#recoveryPanel').getAttribute('role'), 'region')
    assert.equal(await page.locator('#recoveryPanel').getAttribute('aria-labelledby'), 'recoveryTitle')
    assert.equal(await page.locator('#recoveryStatus').getAttribute('role'), 'status')
    assert.equal(await page.locator('#recoveryStatus').getAttribute('aria-live'), 'polite')
    assert.equal(await page.evaluate(() => document.activeElement?.id), 'recoveryTitle')
    assert.equal(await page.locator('#recoveryRetry').innerText(), expected.retry)
    assert.equal(await page.locator('#recoveryRetry').isVisible(), true)

    for (const removedId of REMOVED_PROFILE_CONTROLS) {
      assert.equal(await page.locator(`#${removedId}`).count(), 0, `${locale}: ${removedId}`)
    }
    const bridgeShape = await page.evaluate(() => ({
      state: typeof window.opensquillaDesktop.getRecoveryState,
      chooseWorkspace: typeof window.opensquillaDesktop.chooseRecoveryWorkspace,
      recoverTransaction: typeof window.opensquillaDesktop.recoverProfileTransaction,
      abandonCleanup: typeof window.opensquillaDesktop.abandonCleanupTransaction,
      retryStartup: typeof window.opensquillaDesktop.retryStartup,
      launchSafeProfile: typeof window.opensquillaDesktop.launchSafeProfile,
      retryPrimaryProfile: typeof window.opensquillaDesktop.retryPrimaryProfile,
      returnPrimaryProfile: typeof window.opensquillaDesktop.returnPrimaryProfile,
      getDesktopProfileKind: typeof window.opensquillaDesktop.getDesktopProfileKind,
    }))
    assert.equal(bridgeShape.state, 'function')
    assert.equal(bridgeShape.chooseWorkspace, 'function')
    assert.equal(bridgeShape.recoverTransaction, 'function')
    assert.equal(bridgeShape.abandonCleanup, 'function')
    assert.equal(bridgeShape.retryStartup, 'function')
    assert.equal(bridgeShape.launchSafeProfile, 'undefined')
    assert.equal(bridgeShape.retryPrimaryProfile, 'undefined')
    assert.equal(bridgeShape.returnPrimaryProfile, 'undefined')
    assert.equal(bridgeShape.getDesktopProfileKind, 'undefined')

    if (locale === 'en') {
      await page.locator('#recoveryRetry').click()
      await waitFor(async () => (
        await page.locator('#recoveryPanel').isVisible()
        && await page.locator('#recoveryCode').innerText() === blockingCase.stableCode
        && !await page.locator('#recoveryRetry').isDisabled()
      ), 'generic retry returning to the unchanged primary repair state')
    }

    const recoveryState = await page.evaluate(() => window.opensquillaDesktop.getRecoveryState())
    assert.equal('activeProfile' in recoveryState, false)
    assert.equal('recoveryProfiles' in recoveryState, false)
    assert.deepEqual(await snapshotTree(fixture.primaryHome), fixture.primaryBefore)
    if (fixture.journalPath) {
      assert.equal(await readFile(fixture.journalPath, 'utf8'), fixture.journalBytes)
    }
    if (locale === 'en') await assertReducedMotion(page)
    completedLocales.push(locale)
    completedBlockingCodes.push(blockingCase.stableCode)
  } finally {
    await app?.close().catch(() => {})
    await rm(fixture.root, { recursive: true, force: true }).catch(() => {})
  }
}

assert.deepEqual(completedLocales, Object.keys(LOCALES))
assert.deepEqual(
  completedBlockingCodes,
  Object.values(BLOCKING_CASES).map((item) => item.stableCode),
)
console.log(JSON.stringify({
  ok: true,
  locales: completedLocales,
  blockingCodes: completedBlockingCodes,
  profileChoiceUiPresent: false,
  genericRetryVerified: true,
  primaryBytesUnchanged: true,
  reducedMotionVerified: true,
}))
