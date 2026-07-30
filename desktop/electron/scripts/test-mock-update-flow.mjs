import { strict as assert } from 'node:assert'
import { mkdir, mkdtemp, realpath, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'
import { fileURLToPath } from 'node:url'
import { _electron as electron } from 'playwright'

const scriptDir = dirname(fileURLToPath(import.meta.url))
const packageRoot = resolve(scriptDir, '..')
const repoRoot = resolve(packageRoot, '../..')
const mockVersion = process.env.OPENSQUILLA_DESKTOP_MOCK_UPDATE_VERSION || '99.0.0'
const relaunchLabels = ['Relaunch to Update', '重启以更新']

async function waitFor(check, label, timeoutMs = 45_000) {
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
  const suffix = lastError ? ` Last error: ${lastError.message || lastError}` : ''
  throw new Error(`Timed out waiting for ${label}.${suffix}`)
}

async function menuLabels(app) {
  return await app.evaluate(({ Menu }) => {
    const menu = Menu.getApplicationMenu()
    const labels = []
    function walk(items) {
      for (const item of items || []) {
        if (item.label) labels.push(item.label)
        if (item.submenu) walk(item.submenu.items)
      }
    }
    if (menu) walk(menu.items)
    return labels
  })
}

async function clickRelaunchToUpdate(app) {
  return await app.evaluate(({ BrowserWindow, Menu }, labels) => {
    const menu = Menu.getApplicationMenu()
    function find(items) {
      for (const item of items || []) {
        if (item.label && labels.includes(item.label)) return item
        const child = item.submenu ? find(item.submenu.items) : null
        if (child) return child
      }
      return null
    }
    const item = menu ? find(menu.items) : null
    if (!item || typeof item.click !== 'function') return false
    item.click(undefined, BrowserWindow.getFocusedWindow() ?? BrowserWindow.getAllWindows()[0], undefined)
    return true
  }, relaunchLabels)
}

const isolationRoot = await mkdtemp(join(tmpdir(), 'opensquilla-electron-mock-update-test-'))
const userDataDir = join(isolationRoot, 'chromium-user-data')
const isolatedHome = join(isolationRoot, 'home')
let app

try {
  await mkdir(userDataDir, { recursive: true })
  await mkdir(isolatedHome, { recursive: true })

  // Seed a keyless, entirely synthetic profile so this update test reaches the
  // Control UI without depending on a developer's real desktop credential.
  const now = new Date().toISOString()
  await writeFile(join(userDataDir, 'desktop-credential.json'), JSON.stringify({
    provider: 'ollama',
    model: 'opensquilla-update-test-model',
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

  app = await electron.launch({
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
      OPENSQUILLA_DESKTOP_MOCK_UPDATE_VERSION: mockVersion,
      // mock install: OK. Availability/download now stay in renderer state.
      OPENSQUILLA_DESKTOP_MOCK_UPDATE_DIALOG_RESPONSES: '0',
    },
  })

  const runtimeIsolation = await app.evaluate(({ app: electronApp }) => ({
    userData: electronApp.getPath('userData'),
    home: process.env.HOME,
    userProfile: process.env.USERPROFILE,
  }))
  assert.equal(await realpath(runtimeIsolation.userData), await realpath(userDataDir))
  assert.equal(resolve(runtimeIsolation.home), resolve(isolatedHome))
  assert.equal(resolve(runtimeIsolation.userProfile), resolve(isolatedHome))

  const page = await app.firstWindow({ timeout: 60_000 })
  await page.waitForLoadState('domcontentloaded', { timeout: 60_000 }).catch(() => {})
  await waitFor(
    async () => page.url().includes('/control/chat'),
    'Control UI to load on Chat',
    60_000,
  )

  const nativeAutoUpdateEnabled = await page.evaluate(
    () => window.opensquillaDesktop.isAutoUpdateEnabled(),
  )
  assert.equal(nativeAutoUpdateEnabled, true, 'mock update should enable native update bridge')

  const updateBannerCount = await page.locator('[data-testid="update-banner"]').count()
  assert.equal(updateBannerCount, 0, 'desktop native update should suppress the web release banner')

  const availableState = await waitFor(async () => {
    return await page.evaluate(async () => {
      const api = window.opensquillaDesktop
      if (!api.getUpdateState) return null
      const state = await api.getUpdateState()
      return state?.status === 'available' ? state : null
    })
  }, 'mock update available renderer state')
  assert.equal(availableState.latestVersion, mockVersion)

  const updateIndicator = page.locator('[data-testid="desktop-update-indicator"]')
  await updateIndicator.waitFor({ state: 'visible', timeout: 30_000 })
  // The desktop header can still be settling while the newly discovered update
  // changes its width. Force the already-visible control so this test exercises
  // the update flow instead of Playwright's transient layout-stability heuristic.
  await updateIndicator.click({ force: true })
  await page.locator('[data-testid="desktop-update-download"]').click()

  const downloadedState = await waitFor(async () => {
    return await page.evaluate(async () => {
      const state = await window.opensquillaDesktop.getUpdateState()
      return state?.status === 'downloaded' ? state : null
    })
  }, 'mock update downloaded renderer state')
  assert.equal(downloadedState.latestVersion, mockVersion)

  const relaunchLabel = await waitFor(async () => {
    const labels = await menuLabels(app)
    return labels.find((label) => relaunchLabels.includes(label))
  }, 'Relaunch to Update menu item')
  assert.ok(relaunchLabel, 'pending mock update should expose relaunch menu item')

  const clicked = await clickRelaunchToUpdate(app)
  assert.equal(clicked, true, 'Relaunch to Update menu item should be clickable')

  await delay(500)
  assert.equal(page.isClosed(), false, 'mock install should not quit the app')
  assert.match(await page.title(), /OpenSquilla/, 'Control UI should remain available after mock install')

  const labelsAfterClick = await menuLabels(app)
  assert.ok(
    labelsAfterClick.some((label) => relaunchLabels.includes(label)),
    'mock install keeps the pending relaunch menu available for repeated inspection',
  )

  console.log(JSON.stringify({
    ok: true,
    version: mockVersion,
    updateState: downloadedState.status,
    relaunchLabel,
    url: page.url(),
    title: await page.title(),
  }, null, 2))
} finally {
  await app?.close().catch(() => {})
  await rm(isolationRoot, { recursive: true, force: true }).catch(() => {})
}
