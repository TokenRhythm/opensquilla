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

async function mainWindowSnapshot(app) {
  return await app.evaluate(({ BrowserWindow }) => {
    const window = BrowserWindow.getAllWindows().find((candidate) => (
      candidate.webContents.getURL().startsWith('opensquilla-app://desktop/')
    ))
    if (!window) return null
    return {
      browserWindowId: window.id,
      webContentsId: window.webContents.id,
      url: window.webContents.getURL(),
      visible: window.isVisible(),
      minimized: window.isMinimized(),
      destroyed: window.isDestroyed(),
    }
  })
}

const isolationRoot = await mkdtemp(join(tmpdir(), 'opensquilla-electron-window-close-test-'))
const userDataDir = join(isolationRoot, 'chromium-user-data')
const isolatedHome = join(isolationRoot, 'home')
let desktopApp

try {
  await mkdir(userDataDir, { recursive: true })
  await mkdir(isolatedHome, { recursive: true })

  // Use a synthetic keyless profile so the lifecycle test reaches the Control
  // UI without reading developer credentials or requiring an external model.
  const now = new Date().toISOString()
  await writeFile(join(userDataDir, 'desktop-credential.json'), JSON.stringify({
    provider: 'ollama',
    model: 'opensquilla-window-close-test-model',
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
      OPENSQUILLA_AUTH_MODE: 'token',
      OPENSQUILLA_AUTH_TOKEN: 'synthetic-window-flow-operator-token',
    },
  })

  const runtimeIsolation = await desktopApp.evaluate(({ app }) => ({
    userData: app.getPath('userData'),
    platform: process.platform,
  }))
  assert.equal(await realpath(runtimeIsolation.userData), await realpath(userDataDir))

  const page = await desktopApp.firstWindow({ timeout: 60_000 })
  await page.waitForLoadState('domcontentloaded', { timeout: 60_000 }).catch(() => {})
  await waitFor(
    async () => page.url().startsWith('opensquilla-app://desktop/chat'),
    'Desktop renderer to load on Chat',
  )
  await waitFor(
    async () => (await page.evaluate(
      () => window.opensquillaDesktop?.getGatewayConnection?.(),
    ))?.status === 'ready',
    'Desktop Gateway readiness',
  )
  const gatewayAccess = await page.evaluate(async () => {
    const connection = await window.opensquillaDesktop?.getGatewayConnection?.()
    const response = await fetch('/api/system/status', {
      headers: { Authorization: 'Bearer stale-renderer-token' },
    })
    return {
      authToken: connection?.authToken || '',
      status: response.status,
    }
  })
  assert.match(gatewayAccess.authToken, /^[0-9a-f]{64}$/)
  assert.equal(gatewayAccess.status, 200)

  const preferences = await page.evaluate(
    () => window.opensquillaDesktop.getDesktopPreferences?.(),
  )
  assert.ok(preferences, 'new desktop shell must expose window-close preferences')

  const backgroundSupported = runtimeIsolation.platform === 'darwin'
    || runtimeIsolation.platform === 'win32'
  assert.equal(preferences.canRunInBackground, backgroundSupported)
  assert.equal(
    preferences.mainWindowCloseBehavior,
    backgroundSupported ? 'background' : 'quit',
  )

  if (!backgroundSupported) {
    console.log(JSON.stringify({
      ok: true,
      platform: runtimeIsolation.platform,
      behavior: preferences.mainWindowCloseBehavior,
      backgroundSupported,
    }, null, 2))
  } else {
    const marker = `renderer-${Date.now()}`
    await page.evaluate((value) => {
      window.__opensquillaWindowLifecycleMarker = value
    }, marker)

    const before = await waitFor(
      async () => {
        const snapshot = await mainWindowSnapshot(desktopApp)
        return snapshot?.visible ? snapshot : null
      },
      'visible main window',
    )

    await desktopApp.evaluate(({ BrowserWindow }) => {
      const window = BrowserWindow.getAllWindows().find((candidate) => (
        candidate.webContents.getURL().startsWith('opensquilla-app://desktop/')
      ))
      if (!window) throw new Error('Main Desktop window is unavailable.')
      window.close()
    })

    const hidden = await waitFor(
      async () => {
        const snapshot = await mainWindowSnapshot(desktopApp)
        return snapshot && !snapshot.visible ? snapshot : null
      },
      'main window to hide without closing',
    )
    assert.equal(hidden.destroyed, false)
    assert.equal(hidden.browserWindowId, before.browserWindowId)
    assert.equal(hidden.webContentsId, before.webContentsId)
    // The live SPA may move between client-side routes while hidden, so only
    // require that the window was not navigated away from the Desktop renderer
    // or reloaded to the boot splash.
    // Renderer continuity itself is proven by the marker check below.
    assert.ok(
      hidden.url.startsWith('opensquilla-app://desktop/'),
      `hidden window must stay on the Desktop renderer, got ${hidden.url}`,
    )
    assert.equal(page.isClosed(), false)

    await desktopApp.evaluate(({ app }) => {
      app.emit('activate')
    })

    const revealed = await waitFor(
      async () => {
        const snapshot = await mainWindowSnapshot(desktopApp)
        return snapshot?.visible ? snapshot : null
      },
      'same main window to be revealed',
    )
    assert.equal(revealed.browserWindowId, before.browserWindowId)
    assert.equal(revealed.webContentsId, before.webContentsId)
    assert.ok(
      revealed.url.startsWith('opensquilla-app://desktop/'),
      `revealed window must stay on the Desktop renderer, got ${revealed.url}`,
    )
    assert.equal(
      await page.evaluate(() => window.__opensquillaWindowLifecycleMarker),
      marker,
      'revealing a hidden desktop window must preserve renderer state',
    )

    await desktopApp.evaluate(({ app, BrowserWindow }) => {
      const window = BrowserWindow.getAllWindows().find((candidate) => (
        candidate.webContents.getURL().startsWith('opensquilla-app://desktop/')
      ))
      if (!window) throw new Error('Main Desktop window is unavailable.')
      // Reproduce the activation race deterministically: activateMainWindow()
      // focuses synchronously, then continues across an asynchronous startup
      // boundary. A user hide after that first focus must not be undone by a
      // delayed second focus.
      app.emit('activate')
      window.hide()
    })
    await waitFor(
      async () => {
        const snapshot = await mainWindowSnapshot(desktopApp)
        return snapshot && !snapshot.visible ? snapshot : null
      },
      'main window to hide before deep-link activation',
    )
    await delay(350)
    assert.equal(
      (await mainWindowSnapshot(desktopApp))?.visible,
      false,
      'an asynchronous activation tail must not reveal a window hidden afterward',
    )

    await desktopApp.evaluate(({ app }) => {
      app.emit(
        'second-instance',
        {},
        ['OpenSquilla', 'opensquilla://unknown'],
        process.cwd(),
        {},
      )
    })
    await delay(350)
    assert.equal(
      (await mainWindowSnapshot(desktopApp))?.visible,
      false,
      'an unknown deep-link action must not reveal the window',
    )

    await desktopApp.evaluate(({ app }) => {
      app.emit(
        'second-instance',
        {},
        ['OpenSquilla', 'opensquilla://open'],
        process.cwd(),
        {},
      )
    })
    const secondInstanceRevealed = await waitFor(
      async () => {
        const snapshot = await mainWindowSnapshot(desktopApp)
        return snapshot?.visible ? snapshot : null
      },
      'second-instance deep link to reveal the same main window',
    )
    assert.equal(secondInstanceRevealed.browserWindowId, before.browserWindowId)
    assert.equal(secondInstanceRevealed.webContentsId, before.webContentsId)

    await desktopApp.evaluate(({ BrowserWindow }) => {
      const window = BrowserWindow.getAllWindows().find((candidate) => (
        candidate.webContents.getURL().startsWith('opensquilla-app://desktop/')
      ))
      if (!window) throw new Error('Main Desktop window is unavailable.')
      window.hide()
    })
    const openUrlPrevented = await desktopApp.evaluate(({ app }) => {
      let prevented = false
      app.emit('open-url', {
        preventDefault() {
          prevented = true
        },
      }, 'opensquilla://open')
      return prevented
    })
    assert.equal(openUrlPrevented, true)
    const openUrlRevealed = await waitFor(
      async () => {
        const snapshot = await mainWindowSnapshot(desktopApp)
        return snapshot?.visible ? snapshot : null
      },
      'open-url deep link to reveal the same main window',
    )
    assert.equal(openUrlRevealed.browserWindowId, before.browserWindowId)
    assert.equal(openUrlRevealed.webContentsId, before.webContentsId)

    const minimizable = await desktopApp.evaluate(({ BrowserWindow }) => {
      const window = BrowserWindow.getAllWindows().find((candidate) => (
        candidate.webContents.getURL().startsWith('opensquilla-app://desktop/')
      ))
      if (!window || !window.isMinimizable()) return false
      window.minimize()
      return true
    })
    const minimized = minimizable && Boolean(await waitFor(
      async () => {
        const snapshot = await mainWindowSnapshot(desktopApp)
        return snapshot?.minimized ? snapshot : null
      },
      'main window to minimize before deep-link activation',
      5_000,
    ).catch(() => null))
    if (minimized) {
      await desktopApp.evaluate(({ app }) => {
        app.emit(
          'second-instance',
          {},
          ['OpenSquilla', 'opensquilla://open'],
          process.cwd(),
          {},
        )
      })
      const restored = await waitFor(
        async () => {
          const snapshot = await mainWindowSnapshot(desktopApp)
          return snapshot?.visible && !snapshot.minimized ? snapshot : null
        },
        'deep link to restore a minimized main window',
      )
      assert.equal(restored.browserWindowId, before.browserWindowId)
      assert.equal(restored.webContentsId, before.webContentsId)
    }

    assert.equal(
      await page.evaluate(() => window.__opensquillaWindowLifecycleMarker),
      marker,
      'deep-link activation must preserve renderer state',
    )

    console.log(JSON.stringify({
      ok: true,
      platform: runtimeIsolation.platform,
      behavior: preferences.mainWindowCloseBehavior,
      backgroundSupported,
      browserWindowId: revealed.browserWindowId,
      webContentsId: revealed.webContentsId,
      rendererPreserved: true,
      secondInstanceDeepLink: true,
      openUrlDeepLink: true,
      minimizedRestored: minimized,
    }, null, 2))
  }
} catch (error) {
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
    windows,
    desktopLog,
  }, null, 2))
  throw error
} finally {
  await desktopApp?.close().catch(() => {})
  await rm(isolationRoot, { recursive: true, force: true }).catch(() => {})
}
