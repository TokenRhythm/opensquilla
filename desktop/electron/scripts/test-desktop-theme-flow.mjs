import { strict as assert } from 'node:assert'
import { mkdir, mkdtemp, readFile, realpath, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'
import { fileURLToPath } from 'node:url'
import { _electron as electron } from 'playwright'

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

async function themeSnapshot(app, page) {
  const [native, renderer] = await Promise.all([
    app.evaluate(({ nativeTheme }) => ({
      source: nativeTheme.themeSource,
      shouldUseDarkColors: nativeTheme.shouldUseDarkColors,
    })),
    page.evaluate(() => ({
      dataTheme: document.documentElement.getAttribute('data-theme'),
      prefersDark: window.matchMedia('(prefers-color-scheme: dark)').matches,
      storedTheme: localStorage.getItem('opensquilla-theme'),
      selectedTheme: document.querySelector(
        'input[name="appearance-theme"]:checked',
      )?.value ?? null,
    })),
  ])
  return { native, renderer }
}

async function waitForThemeState(
  app,
  page,
  {
    label,
    source,
    storedTheme,
    selectedTheme,
    dataTheme,
    followsSystem = false,
    shouldUseDarkColors,
  },
) {
  // Do not accept a transient match before the renderer's fire-and-forget IPC
  // reaches the main process. The regression briefly exposed `system` at boot,
  // then pinned Electron to the resolved light/dark value a tick later.
  let matchingSince = 0
  const snapshot = await waitFor(async () => {
    const current = await themeSnapshot(app, page)
    const expectedNativeTheme = current.native.shouldUseDarkColors ? 'dark' : 'light'
    const expectedRendererTheme = current.renderer.prefersDark ? 'dark' : 'light'
    const matches = current.native.source === source
      && current.renderer.storedTheme === storedTheme
      && (selectedTheme === undefined || current.renderer.selectedTheme === selectedTheme)
      && (dataTheme === undefined || current.renderer.dataTheme === dataTheme)
      && (
        !followsSystem
        || (
          current.renderer.dataTheme === expectedRendererTheme
          && expectedRendererTheme === expectedNativeTheme
        )
      )
      && (
        shouldUseDarkColors === undefined
        || current.native.shouldUseDarkColors === shouldUseDarkColors
      )

    if (!matches) {
      matchingSince = 0
      throw new Error(JSON.stringify(current))
    }
    if (!matchingSince) matchingSince = Date.now()
    return Date.now() - matchingSince >= 400 ? current : null
  }, label)

  assert.equal(snapshot.native.source, source, `${label}: native source`)
  assert.equal(snapshot.renderer.storedTheme, storedTheme, `${label}: persisted theme`)
  if (selectedTheme !== undefined) {
    assert.equal(snapshot.renderer.selectedTheme, selectedTheme, `${label}: selected theme`)
  }
  if (dataTheme !== undefined) {
    assert.equal(snapshot.renderer.dataTheme, dataTheme, `${label}: DOM theme`)
  }
  if (followsSystem) {
    assert.equal(
      snapshot.renderer.dataTheme,
      snapshot.renderer.prefersDark ? 'dark' : 'light',
      `${label}: DOM theme must resolve from the renderer media query`,
    )
    assert.equal(
      snapshot.renderer.prefersDark,
      snapshot.native.shouldUseDarkColors,
      `${label}: renderer media query must reflect Electron's system appearance`,
    )
  }
  if (shouldUseDarkColors !== undefined) {
    assert.equal(
      snapshot.native.shouldUseDarkColors,
      shouldUseDarkColors,
      `${label}: native dark appearance`,
    )
  }
  return snapshot
}

async function selectTheme(page, theme) {
  const input = page.locator(`input[name="appearance-theme"][value="${theme}"]`)
  await input.waitFor({ state: 'attached', timeout: 20_000 })
  await input.check({ force: true })
}

const isolationRoot = await mkdtemp(join(tmpdir(), 'opensquilla-electron-theme-test-'))
const userDataDir = join(isolationRoot, 'chromium-user-data')
const isolatedHome = join(isolationRoot, 'home')
let desktopApp
let page

try {
  await mkdir(userDataDir, { recursive: true })
  await mkdir(isolatedHome, { recursive: true })

  // Use a synthetic keyless profile so the theme flow reaches the real Control
  // UI without reading developer credentials or requiring an external model.
  const now = new Date().toISOString()
  await writeFile(join(userDataDir, 'desktop-credential.json'), JSON.stringify({
    provider: 'ollama',
    model: 'opensquilla-theme-test-model',
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

  desktopApp = await electron.launch({
    args: [
      '--use-mock-keychain',
      `--user-data-dir=${userDataDir}`,
      packageRoot,
    ],
    env: {
      ...process.env,
      HOME: isolatedHome,
      USERPROFILE: isolatedHome,
      OPENSQUILLA_DESKTOP_REPO_ROOT: repoRoot,
      OPENSQUILLA_DESKTOP_SECRET_STORAGE: 'plain',
      OPENSQUILLA_DESKTOP_DISABLE_AUTO_UPDATE: '1',
    },
  })

  const runtimeIsolation = await desktopApp.evaluate(({ app }) => ({
    userData: app.getPath('userData'),
    platform: process.platform,
  }))
  assert.equal(await realpath(runtimeIsolation.userData), await realpath(userDataDir))

  page = await desktopApp.firstWindow({ timeout: 60_000 })
  // Playwright contexts default colorScheme to light, including Electron pages.
  // Clear that test-only override so prefers-color-scheme once again reflects
  // Electron nativeTheme; otherwise a dark host reports native dark while the
  // renderer is artificially held on light.
  await page.emulateMedia({ colorScheme: null })
  await page.waitForLoadState('domcontentloaded', { timeout: 60_000 }).catch(() => {})
  await waitFor(
    async () => page.url().includes('/control/chat'),
    'Control UI to load on Chat',
  )
  await page.waitForSelector('html[data-theme]', { timeout: 20_000 })

  // A fresh profile has no saved choice, so the renderer starts in System. It
  // must leave Electron on `system`, rather than pinning the launch-time result.
  const initialSystem = await waitForThemeState(desktopApp, page, {
    label: 'initial System theme',
    source: 'system',
    storedTheme: null,
    followsSystem: true,
  })

  // Exercise the same SPA navigation path as an operator. A hard page.goto()
  // can race a still-settling draft ChatView canonicalization on slower
  // Windows runners and be replaced by /chat/new even though the renderer is
  // healthy; clicking the permanent settings control also proves the shell is
  // still interactive before testing the theme panel.
  const settingsButton = page.locator('button.sidebar-fn-item[data-icon="settings"]')
  await settingsButton.waitFor({ state: 'visible', timeout: 20_000 })
  await settingsButton.click()
  await page.waitForURL(url => url.pathname.includes('/control/settings'), {
    timeout: 20_000,
  })
  const interfaceTab = page.locator('#settings-rail-interface')
  await interfaceTab.waitFor({ state: 'visible', timeout: 20_000 })
  await interfaceTab.click()
  await page.waitForURL(url => url.pathname.endsWith('/control/settings/interface'), {
    timeout: 20_000,
  })
  await page.waitForSelector('input[name="appearance-theme"][value="system"]', {
    timeout: 20_000,
  })

  await selectTheme(page, 'dark')
  await waitForThemeState(desktopApp, page, {
    label: 'explicit Dark theme',
    source: 'dark',
    storedTheme: 'dark',
    selectedTheme: 'dark',
    dataTheme: 'dark',
    shouldUseDarkColors: true,
  })

  // Electron's explicit Dark source makes this a same-colour transition on every
  // host. Watching only the resolved DOM value must not prevent the semantic
  // System change reaching Electron and clearing its explicit override.
  await selectTheme(page, 'system')
  const darkToSystem = await waitForThemeState(desktopApp, page, {
    label: 'Dark to System transition',
    source: 'system',
    storedTheme: 'system',
    selectedTheme: 'system',
    followsSystem: true,
  })

  await selectTheme(page, 'crt-green')
  await waitForThemeState(desktopApp, page, {
    label: 'explicit CRT Green theme',
    source: 'dark',
    storedTheme: 'crt-green',
    selectedTheme: 'crt-green',
    dataTheme: 'crt-green',
    shouldUseDarkColors: true,
  })

  await selectTheme(page, 'system')
  const crtGreenToSystem = await waitForThemeState(desktopApp, page, {
    label: 'CRT Green to System transition',
    source: 'system',
    storedTheme: 'system',
    selectedTheme: 'system',
    followsSystem: true,
  })

  console.log(JSON.stringify({
    ok: true,
    platform: runtimeIsolation.platform,
    initialSystem,
    darkToSystem,
    crtGreenToSystem,
  }, null, 2))
} catch (error) {
  const currentTheme = desktopApp && page
    ? await themeSnapshot(desktopApp, page).catch(() => null)
    : null
  const windows = desktopApp
    ? await desktopApp.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().map(
        (window) => ({
          destroyed: window.isDestroyed(),
          title: window.getTitle(),
          url: window.webContents.getURL(),
          visible: window.isVisible(),
        }),
      )).catch(() => [])
    : []
  const desktopLog = await readFile(
    join(userDataDir, 'logs', 'desktop.log'),
    'utf8',
  ).catch(() => '')
  console.error(JSON.stringify({
    error: error instanceof Error ? error.message : String(error),
    currentTheme,
    windows,
    desktopLog,
  }, null, 2))
  throw error
} finally {
  await desktopApp?.close().catch(() => {})
  await rm(isolationRoot, { recursive: true, force: true }).catch(() => {})
}
