import { test as base, expect, _electron as electron } from '@playwright/test'
import { mkdir, writeFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { resolve } from 'node:path'

export const test = base.extend({
  page: async ({ page }, use, testInfo) => {
    if (process.env.OPENSQUILLA_E2E_NATIVE_DESKTOP !== '1') {
      await use(page)
      return
    }
    const repo = resolve(import.meta.dirname, '../../..')
    const desktop = resolve(repo, 'desktop/electron')
    const require = createRequire(resolve(desktop, 'package.json'))
    // Gateway ownership filenames include hashes; avoid MAX_PATH failures
    // caused by nesting a profile below Playwright's full test-title folder.
    const isolation = resolve(testInfo.project.outputDir, '..', `native-${process.pid}-${testInfo.workerIndex}`)
    const userData = resolve(isolation, 'profile')
    const isolatedHome = resolve(isolation, 'home')
    await mkdir(userData, { recursive: true })
    await mkdir(isolatedHome, { recursive: true })
    const now = new Date().toISOString()
    await writeFile(resolve(userData, 'desktop-credential.json'), JSON.stringify({
      provider: 'ollama', model: 'synthetic-ensemble', baseUrl: 'http://127.0.0.1:11434',
      apiKeyEnv: '', encryptedApiKey: '', modelRoutingMode: 'direct', routerMode: 'disabled',
      routerDefaultTier: 'c1', routerTiers: {}, searchProvider: 'duckduckgo',
      searchApiKeyEnv: '', encryptedSearchApiKey: '', encryption: 'plain',
      disableNetworkObservability: true, createdAt: now, updatedAt: now,
    }))
    const env: Record<string, string> = {}
    for (const name of Object.keys(process.env)) {
      if (/(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)/i.test(name)) continue
      if (process.env[name] !== undefined) env[name] = process.env[name]!
    }
    const app = await electron.launch({
      executablePath: require('electron'),
      args: [`--user-data-dir=${userData}`, desktop],
      env: { ...env, HOME: isolatedHome, USERPROFILE: isolatedHome,
        OPENSQUILLA_DESKTOP_REPO_ROOT: repo, OPENSQUILLA_DESKTOP_SECRET_STORAGE: 'plain',
        OPENSQUILLA_DESKTOP_DISABLE_AUTO_UPDATE: '1', PYTHONUTF8: '1' },
      timeout: 60_000,
    })
    try {
      const nativePage = await app.firstWindow()
      await nativePage.waitForURL('opensquilla-app://desktop/**', { timeout: 120_000 })
      await expect.poll(() => nativePage.evaluate(async () =>
        (await window.opensquillaDesktop?.getGatewayStatus())?.status),
      { timeout: 120_000 }).toBe('ready')
      await testInfo.attach('native-runtime', {
        body: JSON.stringify(await app.evaluate(({ app }) => ({
          platform: process.platform, versions: process.versions,
          appVersion: app.getVersion(), appPath: app.getAppPath(), userData: app.getPath('userData'),
        }))), contentType: 'application/json',
      })
      await use(nativePage)
    } finally {
      await app.close()
    }
  },
})
