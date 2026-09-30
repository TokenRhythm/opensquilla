import { mkdir } from 'node:fs/promises'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { expect, test, type Page } from '@playwright/test'
import { installSidebarFixture, SIDEBAR_SESSIONS } from './support/sidebar-fixture'

const PATHS = ['C:\\workspace\\中文 空格 #` note.txt', "C:\\outside\\single'quote.bin"]
const LABEL = 'Reference local path (no upload)'
test.use({ reducedMotion: 'reduce' })

async function install(page: Page, options: { owned?: boolean; desktop?: boolean } = {}) {
  const state = { nativeCalls: 0, nativeDropCalls: 0, attachmentCalls: 0, sessionCreates: 0, sends: 0, uploads: 0,
    release: (_paths: string[]) => {}, errors: [] as string[], warnings: [] as string[] }
  page.on('pageerror', error => state.errors.push(error.message))
  page.on('console', message => {
    if (message.type() === 'error') state.errors.push(message.text())
    if (message.type() === 'warning') state.warnings.push(message.text())
  })
  page.on('request', request => {
    if (/\/api\/.*upload/.test(request.url()) && request.method() === 'POST') state.uploads++
  })
  await page.route('**/api/**', route => route.fulfill({ json: {} }))
  await page.route('**/control/static/dist/opensquilla-mark.png', route => route.fulfill({
    path: fileURLToPath(new URL('../public-assets/opensquilla-mark.png', import.meta.url)),
    contentType: 'image/png',
  }))
  await installSidebarFixture(page, {
    'config.patch.safe': { patched: ['control_ui.default_locale'], restartRequired: false },
    'sessions.create': () => { state.sessionCreates++; return {} },
    'session.create': () => { state.sessionCreates++; return {} },
    'chat.send': () => { state.sends++; return {} },
  })
  if (options.desktop !== false) {
    await page.exposeFunction('__localPathPicker', () => {
      state.nativeCalls++
      return new Promise<string[]>(resolve => { state.release = resolve })
    })
    await page.exposeFunction('__unexpectedAttachment', () => { state.attachmentCalls++; return [] })
    await page.exposeFunction('__resolveNativeFilePath', (name: string) => {
      state.nativeDropCalls++
      return `C:\\workspace\\${name}`
    })
    await page.addInitScript(({ owned }) => {
      const fixture = window as unknown as {
        __localPathPicker: () => Promise<string[]>
        __unexpectedAttachment: () => Promise<[]>
        __resolveNativeFilePath: (name: string) => Promise<string>
      }
      const gateway = { url: location.origin, port: Number(location.port), owned,
        status: 'ready' as const, logPath: '' }
      window.opensquillaDesktop = {
        getOsLocale: async () => 'en', isAutoUpdateEnabled: async () => false,
        isDesktopUpdateManaged: async () => true,
        getGatewayStatus: async () => gateway,
        getGatewayConnection: async () => ({ schemaVersion: 1, revision: 1, status: 'ready',
          instanceId: 'fixture-owned-child', profileFingerprint: 'fixture-profile',
          httpUrl: location.origin, wsUrl: location.origin.replace(/^http/, 'ws') + '/ws',
          authToken: owned ? 'synthetic-child-token' : null, error: null }),
        onGatewayConnectionChanged: () => () => {},
        getDesktopSettings: async () => ({ provider: 'openai', model: 'fixture', baseUrl: '',
          apiKeyConfigured: true, searchProvider: '', searchApiKeyEnv: '', searchApiKeyConfigured: false,
          disableNetworkObservability: false, gateway }),
        getDesktopPreferences: async () => ({ mainWindowCloseBehavior: 'background',
          canRunInBackground: true, platform: 'win32', sandboxUnavailableWarningSuppressed: true }),
        getOnboardingDefaults: async () => ({}),
        getBootState: async () => ({ status: 'ready' }),
        onBootStatus: () => () => {}, onBootError: () => () => {},
        setNativeTheme: async () => {},
        chooseLocalFilePaths: () => fixture.__localPathPicker(),
        resolveNativeFilePath: (file: File) => fixture.__resolveNativeFilePath(file.name),
        chooseAttachments: () => fixture.__unexpectedAttachment(),
        cancelAttachmentSelections: async () => {},
        chooseProjectDirectory: async () => null,
        openArtifact: async () => ({ ok: false }),
      } as unknown as OpenSquillaDesktopApi
    }, { owned: options.owned !== false })
  }
  return state
}

async function open(page: Page) {
  await page.goto('/control/chat/new')
  await expect(page).toHaveURL(/\/control\/chat\/new$/)
  await expect(page).toHaveTitle(/OpenSquilla/)
  await expect(page.locator('.chat-textarea')).toBeVisible()
  await expect(page.locator('.conn-pill.connected')).toBeVisible()
  await expect(page.locator('vite-error-overlay')).toHaveCount(0)
}

async function openMenu(page: Page) {
  await page.locator('.chat-composer').getByRole('button', { name: 'Add', exact: true }).click()
  await expect(page.getByRole('menu', { name: 'Add', exact: true })).toBeVisible()
}

async function screenshot(page: Page, name: string) {
  const directory = process.env.OPENSQUILLA_LOCAL_PATH_QA_DIR
  if (!directory) return
  await mkdir(directory, { recursive: true })
  await expect(page.locator('.chat-composer')).toBeVisible()
  await page.screenshot({ path: join(directory, name), fullPage: false, animations: 'disabled' })
}

test('first-message picker inserts inside/outside path references without session creation or upload; reload preserves the input', async ({ page }) => {
  const state = await install(page)
  await open(page)
  const input = page.locator('.chat-textarea')
  await input.fill('Inspect these files')
  await openMenu(page)
  await expect(page.getByRole('menuitem', { name: LABEL })).toBeVisible()
  await screenshot(page, 'local-path-desktop-menu.png')
  await page.getByRole('menuitem', { name: LABEL }).click()
  await expect.poll(() => state.nativeCalls).toBe(1)
  state.release(PATHS)
  await expect(input).toHaveValue(`Inspect these files\n${PATHS.join('\n')}`)
  await expect(page.locator('.attachment-chip')).toHaveCount(0)
  await screenshot(page, 'local-path-desktop-inserted.png')
  expect(state.sessionCreates).toBe(0)
  expect(state.sends).toBe(0)
  expect(state.uploads).toBe(0)
  expect(state.attachmentCalls).toBe(0)
  await page.reload()
  await expect(input).toHaveValue(`Inspect these files\n${PATHS.join('\n')}`)
  expect(state.nativeCalls).toBe(1)
  expect(state.errors).toEqual([])
  expect(state.warnings).toEqual([])
})

test('first-message native non-image drop inserts a path without attachment or upload work', async ({ page }) => {
  const state = await install(page)
  await open(page)
  const input = page.locator('.chat-textarea')
  await input.fill('Inspect the dropped file')
  await page.evaluate(() => {
    const dataTransfer = new DataTransfer()
    dataTransfer.items.add(new File(['80 MiB fixture'], 'large.bin', { type: 'application/octet-stream' }))
    const root = document.querySelector('.chat')
    if (!root) throw new Error('chat root missing')
    root.dispatchEvent(new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer }))
  })
  await expect(input).toHaveValue('Inspect the dropped file\nC:\\workspace\\large.bin')
  expect(state.nativeDropCalls).toBe(1)
  expect(state.nativeCalls + state.attachmentCalls + state.uploads + state.sessionCreates).toBe(0)
  expect(state.errors).toEqual([])
})

test('editing the composer while the native dialog is pending discards its result', async ({ page }) => {
  const state = await install(page)
  await open(page)
  await openMenu(page)
  await page.getByRole('menuitem', { name: LABEL }).click()
  await expect.poll(() => state.nativeCalls).toBe(1)
  await page.locator('.chat-textarea').fill('Keep my newer text')
  state.release(PATHS)
  // A second dialog acts as an async barrier after the old callback, without fixed sleeps.
  await openMenu(page)
  await page.getByRole('menuitem', { name: LABEL }).click()
  await expect.poll(() => state.nativeCalls).toBe(2)
  state.release([])
  await expect(page.locator('.chat-textarea')).toHaveValue('Keep my newer text')
  expect(state.sessionCreates + state.uploads + state.attachmentCalls).toBe(0)
  expect(state.errors).toEqual([])
})

test('an existing chat uses the same local path-input action', async ({ page }) => {
  const state = await install(page)
  await open(page)
  await page.locator(`.sidebar-history-row[data-session-key="${SIDEBAR_SESSIONS[0]!.key}"]`).click()
  await expect(page).toHaveURL(/e2e-sidebar-1/)
  await openMenu(page)
  await page.getByRole('menuitem', { name: LABEL }).click()
  await expect.poll(() => state.nativeCalls).toBe(1)
  state.release(PATHS)
  await expect(page.locator('.chat-textarea')).toHaveValue(PATHS.join('\n'))
  expect(state.sessionCreates + state.sends + state.uploads + state.attachmentCalls).toBe(0)
  expect(state.errors).toEqual([])
})

test('switching to another chat while the picker is pending does not populate that draft', async ({ page }) => {
  const state = await install(page)
  await open(page)
  await openMenu(page)
  await page.getByRole('menuitem', { name: LABEL }).click()
  await expect.poll(() => state.nativeCalls).toBe(1)
  await page.locator(`.sidebar-history-row[data-session-key="${SIDEBAR_SESSIONS[0]!.key}"]`).click()
  await expect(page).toHaveURL(/e2e-sidebar-1/)
  state.release(PATHS)
  await openMenu(page)
  await page.getByRole('menuitem', { name: LABEL }).click()
  await expect.poll(() => state.nativeCalls).toBe(2)
  state.release([])
  await expect(page.locator('.chat-textarea')).toHaveValue('')
  expect(state.sessionCreates + state.uploads + state.attachmentCalls).toBe(0)
  expect(state.errors).toEqual([])
})

for (const desktop of [false, true]) {
  test(`${desktop ? 'external loopback Desktop Gateway' : 'ordinary browser'} hides the local picker`, async ({ page }) => {
    const state = await install(page, { desktop, owned: false })
    await open(page)
    await openMenu(page)
    await expect(page.getByRole('menuitem', { name: 'Attach files', exact: true })).toBeVisible()
    await expect(page.getByRole('menuitem', { name: LABEL })).toHaveCount(0)
    expect(state.nativeCalls).toBe(0)
    expect(state.errors).toEqual([])
  })
}

test('local path action remains visible in the narrow composer menu', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  const state = await install(page)
  await open(page)
  await openMenu(page)
  const action = page.getByRole('menuitem', { name: LABEL })
  await expect(action).toBeVisible()
  await expect(action).toBeInViewport()
  await screenshot(page, 'local-path-mobile-menu.png')
  await action.click()
  await expect.poll(() => state.nativeCalls).toBe(1)
  state.release(PATHS)
  await expect(page.locator('.chat-textarea')).toHaveValue(PATHS.join('\n'))
  expect(state.errors).toEqual([])
})
