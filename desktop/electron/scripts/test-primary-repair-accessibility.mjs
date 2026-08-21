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

// Every locale exercises the repair page with the schema-too-new fixture, so
// the expected heading is the update-specific title, not the generic one.
const LOCALES = {
  en: {
    title: 'Starting OpenSquilla',
    bootAria: 'OpenSquilla startup',
    repairTitle: 'Update OpenSquilla to open this profile',
    retry: 'Retry',
  },
  'zh-Hans': {
    title: '正在启动 OpenSquilla',
    bootAria: 'OpenSquilla 启动',
    repairTitle: '请更新 OpenSquilla 以打开此配置',
    retry: '重试',
  },
  ja: {
    title: 'OpenSquilla を起動しています',
    bootAria: 'OpenSquilla の起動',
    repairTitle: 'このプロファイルを開くには OpenSquilla の更新が必要です',
    retry: '再試行',
  },
  fr: {
    title: "Démarrage d'OpenSquilla",
    bootAria: "Démarrage d'OpenSquilla",
    repairTitle: 'Mettez à jour OpenSquilla pour ouvrir ce profil',
    retry: 'Réessayer',
  },
  de: {
    title: 'OpenSquilla wird gestartet',
    bootAria: 'OpenSquilla-Start',
    repairTitle: 'Aktualisieren Sie OpenSquilla, um dieses Profil zu öffnen',
    retry: 'Wiederholen',
  },
  es: {
    title: 'Iniciando OpenSquilla',
    bootAria: 'Inicio de OpenSquilla',
    repairTitle: 'Actualiza OpenSquilla para abrir este perfil',
    retry: 'Reintentar',
  },
}

// After the recovery-governance downgrade, the only fixture-synthesizable
// hard startup gate is a config authored by a newer build. Every other
// historical blocking code now starts with a warning (or is repaired
// automatically), so each locale exercises the repair page with the same
// schema-too-new authority.
const BLOCKING_CASES = Object.fromEntries(
  Object.keys(LOCALES).map((locale) => [
    locale,
    { fixture: 'future-config', stableCode: 'config_schema_too_new' },
  ]),
)

const REMOVED_PROFILE_CONTROLS = [
  'recoveryProfiles',
  'copyCredential',
  'continueRecovery',
  'createRecovery',
  'retryPrimary',
  'returnPrimary',
  'abandonCleanup',
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

  assert.equal(blockingCase.fixture, 'future-config')
  await writeFile(join(primaryHome, 'config.toml'), 'config_version = 999\n', 'utf8')
  await writeFile(join(userData, 'desktop-locale'), locale, 'utf8')

  return {
    root,
    userData,
    isolatedHome,
    primaryHome,
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
    const progress = document.querySelector('#startupProgress')
    const reducedStyle = getComputedStyle(progress, '::before')
    const loaderStyle = getComputedStyle(document.querySelector('.loader span'))
    const snapshot = {
      mediaMatches: matchMedia('(prefers-reduced-motion: reduce)').matches,
      blockedAnimationName,
      animationName: reducedStyle.animationName,
      progressRole: progress.getAttribute('role'),
      progressNow: progress.getAttribute('aria-valuenow'),
      progressMax: progress.getAttribute('aria-valuemax'),
      loaderAnimationDuration: loaderStyle.animationDuration,
      loaderAnimationIterationCount: loaderStyle.animationIterationCount,
      scrollBehavior: getComputedStyle(document.documentElement).scrollBehavior,
    }
    if (bodyWasErrored) document.body.classList.add('errored')
    return snapshot
  })
  assert.equal(result.mediaMatches, true)
  assert.equal(result.blockedAnimationName, 'none')
  assert.equal(result.animationName, 'none')
  assert.equal(result.progressRole, 'progressbar')
  assert.equal(Number(result.progressNow) >= 0, true)
  assert.equal(result.progressMax, '4')
  assert(
    durationSeconds(result.loaderAnimationDuration) <= 0.00001,
    result.loaderAnimationDuration,
  )
  assert.equal(result.loaderAnimationIterationCount, '1')
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
    assert.equal(await page.locator('#recoveryTitle').innerText(), expected.repairTitle)
    assert.equal(await page.locator('#recoveryPanel').getAttribute('role'), 'region')
    assert.equal(await page.locator('#recoveryPanel').getAttribute('aria-labelledby'), 'recoveryTitle')
    assert.equal(await page.locator('#recoveryStatus').getAttribute('role'), 'status')
    assert.equal(await page.locator('#recoveryStatus').getAttribute('aria-live'), 'polite')
    assert.equal(await page.evaluate(() => document.activeElement?.id), 'recoveryTitle')
    assert.equal(await page.locator('#recoveryRetry').innerText(), expected.retry)
    assert.equal(await page.locator('#recoveryRetry').isVisible(), true)
    assert.equal(await page.locator('#recoveryUpdate').isVisible(), true)

    for (const removedId of REMOVED_PROFILE_CONTROLS) {
      assert.equal(await page.locator(`#${removedId}`).count(), 0, `${locale}: ${removedId}`)
    }
    const bridgeShape = await page.evaluate(() => ({
      state: typeof window.opensquillaDesktop.getRecoveryState,
      chooseWorkspace: typeof window.opensquillaDesktop.chooseRecoveryWorkspace,
      recoverTransaction: typeof window.opensquillaDesktop.recoverProfileTransaction,
      abandonCleanup: typeof window.opensquillaDesktop.abandonCleanupTransaction,
      openDownload: typeof window.opensquillaDesktop.openLatestDownloadPage,
      retryStartup: typeof window.opensquillaDesktop.retryStartup,
      launchSafeProfile: typeof window.opensquillaDesktop.launchSafeProfile,
      retryPrimaryProfile: typeof window.opensquillaDesktop.retryPrimaryProfile,
      returnPrimaryProfile: typeof window.opensquillaDesktop.returnPrimaryProfile,
      getDesktopProfileKind: typeof window.opensquillaDesktop.getDesktopProfileKind,
    }))
    assert.equal(bridgeShape.state, 'function')
    assert.equal(bridgeShape.chooseWorkspace, 'function')
    assert.equal(bridgeShape.recoverTransaction, 'function')
    assert.equal(bridgeShape.abandonCleanup, 'undefined')
    assert.equal(bridgeShape.openDownload, 'function')
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
