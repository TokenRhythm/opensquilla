import { expect, test, type Locator, type Page } from '@playwright/test'
import { helloOkResponse } from './support/gateway-fixture'
import {
  chatHistoryPayload,
  sessionMessagesHydratePayload,
  sessionMessagesSnapshotPayload,
  sessionMessagesSubscribePayload,
} from './support/session-read-fixtures'

const CONTROL_URL = '/control/'
const SESSION_KEY = 'agent:main:webchat:e2eworkbench'
const PNG_1x1 = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==',
  'base64',
)

const ARTIFACTS = [
  {
    id: 'workbench-notes',
    name: 'notes.txt',
    mime: 'text/plain',
    size: 18,
    download_url: '/api/v1/artifacts/workbench-notes',
  },
  {
    id: 'workbench-guide',
    name: 'guide.md',
    mime: 'text/markdown',
    size: 28,
    download_url: '/api/v1/artifacts/workbench-guide',
  },
  {
    id: 'workbench-demo',
    name: 'demo.html',
    mime: 'text/html',
    size: 80,
    download_url: '/api/v1/artifacts/workbench-demo',
  },
]

async function installWorkbenchGateway(
  page: Page,
  requests: Map<string, number> = new Map(),
  artifacts = ARTIFACTS,
  sends: Record<string, unknown>[] = [],
  delayedDrafts?: { requested: boolean; release?: () => void },
) {
  await page.route('**/api/**', route => route.fulfill({
    status: 404,
    body: 'Unmocked Workbench API request',
  }))
  await page.route('**/api/system/update', route => route.fulfill({ json: {} }))
  await page.route('**/api/elevated-mode', route => route.fulfill({
    json: { enabled: false },
  }))
  await page.route('**/opensquilla-mark.png', route => route.fulfill({
    contentType: 'image/png',
    body: PNG_1x1,
  }))
  await page.route('**/api/approvals', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ pending: [], mode: 'prompt', allowPatterns: [], denyPatterns: [] }),
  }))
  await page.route('**/api/v1/artifacts/**', route => {
    const pathname = new URL(route.request().url()).pathname
    requests.set(pathname, (requests.get(pathname) || 0) + 1)
    if (pathname.endsWith('/workbench-notes')) {
      return route.fulfill({
        status: 200,
        contentType: 'text/plain',
        body: 'Workbench notes stay mounted.',
      })
    }
    if (pathname.endsWith('/workbench-guide')) {
      return route.fulfill({
        status: 200,
        contentType: 'text/markdown',
        body: '# Guide\n\nPersistent markdown preview.',
      })
    }
    if (pathname.endsWith('/workbench-demo')) {
      return route.fulfill({
        status: 200,
        contentType: 'text/html',
        body: '<!doctype html><title>Demo</title><p id="preview">Offline demo</p><button>Preview action</button>',
      })
    }
    if (pathname.endsWith('/workbench-report')) {
      return route.fulfill({
        status: 200,
        contentType: 'application/pdf',
        body: Buffer.from('%PDF-1.4\\n%EOF\\n'),
      })
    }
    if (pathname.endsWith('/workbench-image-a') || pathname.endsWith('/workbench-image-b')) {
      return route.fulfill({
        status: 200,
        contentType: 'image/png',
        body: PNG_1x1,
      })
    }
    return route.fulfill({ status: 404, body: 'missing artifact' })
  })
  await page.routeWebSocket(/\/ws$/, ws => {
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(message => {
      let frame: Record<string, unknown>
      try {
        frame = JSON.parse(String(message)) as Record<string, unknown>
      } catch {
        return
      }
      if (frame.type !== 'req') return
      const method = String(frame.method || '')
      if (method === 'connect') {
        ws.send(helloOkResponse({
          ...(delayedDrafts ? { features: { methods: ['meta.drafts.list'] } } : {}),
          auth: {
          principal: { isOwner: true, authenticated: true, authState: 'authenticated' },
          runModePolicy: { allowedRunModes: ['safe', 'full'], defaultRunMode: 'full' },
        } }))
        return
      }
      if (method === 'meta.drafts.list') {
        const reply = () => ws.send(JSON.stringify({ type: 'res', id: frame.id, ok: true,
          payload: { durable: true, drafts: [{
            sessionKey: 'agent:main:webchat:old-unfinished-draft', clientRequestId: 'synthetic-draft',
            name: 'meta-example', launchText: '/meta run meta-example',
            createdAt: 1_800_000_000, expiresAt: 2_000_000_000, sessionExists: false,
          }] },
        }))
        const params = frame.params as Record<string, unknown> | undefined
        if (delayedDrafts && params?.agentId && !delayedDrafts.requested) {
          delayedDrafts.requested = true
          delayedDrafts.release = reply
        } else {
          ws.send(JSON.stringify({ type: 'res', id: frame.id, ok: true,
            payload: { durable: true, drafts: [] } }))
        }
        return
      }
      if (method === 'chat.send') {
        const params = frame.params as Record<string, unknown>
        sends.push(params)
        ws.send(JSON.stringify({ type: 'res', id: frame.id, ok: true, payload: {
          accepted: true, sessionKey: params.sessionKey, session: params.sessionKey,
          task_id: 'workbench-browser-task', stream_seq: 1,
          user_message_id: params.clientMessageId,
        } }))
        return
      }
      if (method === 'chat.history') {
        ws.send(JSON.stringify({
          type: 'res',
          id: frame.id,
          ok: true,
          payload: chatHistoryPayload([
            {
              role: 'user',
              text: 'Create previewable files.',
              id: 'workbench-user',
              timestamp: Math.floor(Date.now() / 1000) - 120,
            },
            {
              role: 'assistant',
              text: 'The files are ready.',
              id: 'workbench-assistant',
              timestamp: Math.floor(Date.now() / 1000) - 60,
              artifacts,
            },
          ]),
        }))
        return
      }
      const params = frame.params as Record<string, unknown> | undefined
      const key = String(params?.key || params?.sessionKey || SESSION_KEY)
      const payloads: Record<string, unknown> = {
        'agents.list': { agents: [] },
        'commands.list_for_surface': { commands: [] },
        'config.get': {
          squilla_router: { enabled: false, rollout_phase: 'observe', tiers: {} },
          permissions: {},
          skills: {},
        },
        'onboarding.status': { audioConfigured: false },
        'sessions.list': { sessions: [{
          key: SESSION_KEY, title: 'Browser workbench task', sessionKind: 'chat',
          surface: 'webchat', conversationKind: 'direct', effectiveAgentId: 'main',
          updatedAt: 1_800_000_000, messageCount: 2, status: 'ok', runStatus: 'idle',
        }], count: 1, ts: 1_800_000_000, has_more: false },
        'sessions.messages.subscribe': sessionMessagesSubscribePayload(key),
        'sessions.messages.snapshot': sessionMessagesSnapshotPayload(key),
        'sessions.messages.hydrate': sessionMessagesHydratePayload(key),
        'sandbox.run_mode.preference.get': { runMode: 'full', source: 'config' },
        'usage.status': { sessions: [] },
      }
      ws.send(JSON.stringify({
        type: 'res',
        id: frame.id,
        ok: true,
        payload: payloads[method] ?? {},
      }))
    })
  })
}

async function openWorkbenchSession(
  page: Page,
  requests: Map<string, number> = new Map(),
  artifacts = ARTIFACTS,
) {
  await page.addInitScript(() => {
    window.OPENSQUILLA_FEATURES = {
      ...(window.OPENSQUILLA_FEATURES || {}),
      artifactWorkbench: true,
    }
  })
  await installWorkbenchGateway(page, requests, artifacts)
  await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(SESSION_KEY))
  await expect(page.locator('.conn-pill')).toBeVisible({ timeout: 10000 })
  await expect(page.locator([
    '.msg-artifact-chip',
    '.msg-media-card',
    '.msg-audio-card',
    '.msg-video-card',
  ].join(','))).toHaveCount(
    artifacts.length,
    { timeout: 10000 },
  )
}

async function installDesktopWorkbenchV2Bridge(
  page: Page,
  mode?: 'full' | 'offline',
) {
  await page.addInitScript(({ previewMode }) => {
    const initialMode = previewMode === 'offline' ? 'offline' : 'full'
    const expiresAt = '2099-01-01T00:00:00Z'
    const leaseResponse = (mode: 'full' | 'offline', sequence: number) => {
      const token = mode === 'full'
        ? '11111111111111111111111111111111'
        : '22222222222222222222222222222222'
      const previewOrigin = `http://p-${token}.localhost:48721`
      return {
        ok: true,
        status: 201,
        payload: {
          version: 1,
          lease_id: `apl-e2e-${mode}-${sequence}`,
          effective_mode: mode,
          launch_url: `${previewOrigin}/index.html`,
          entrypoint: 'index.html',
          expires_at: expiresAt,
          preview_origin: previewOrigin,
          idle_timeout_seconds: 28_800,
          source: {
            kind: 'single_file',
            collection_status: 'not_applicable',
            file_count: 1,
            total_bytes: 80,
            warning_codes: [],
          },
        },
      }
    }
    let resolveLease!: (value: unknown) => void
    const pendingLease = new Promise(resolve => {
      resolveLease = resolve
    })
    const probe = {
      activations: [] as string[],
      createRequests: [] as Array<Record<string, unknown>>,
      destroyedSurfaces: [] as string[],
      leaseRequests: [] as Array<Record<string, unknown>>,
      rectRequests: [] as Array<Record<string, unknown>>,
      releaseLease: () => resolveLease(leaseResponse(initialMode, 1)),
      surfaceListener: null as ((payload: unknown) => void) | null,
    }
    const desktopPreferences = {
      schemaVersion: 1,
      mainWindowCloseBehavior: 'quit',
      canRunInBackground: false,
      platform: 'darwin',
      workbenchPreviewNoticeShown: true,
      workbenchPreviewForcedOffline: false,
      ...(previewMode
        ? {
            workbenchPreviewMode: previewMode,
            effectiveWorkbenchPreviewMode: previewMode,
          }
        : {}),
    }
    const bridge = {
      getOsLocale: async () => 'en-US',
      isAutoUpdateEnabled: async () => false,
      isDesktopUpdateManaged: async () => false,
      getGatewayStatus: async () => ({
        url: '',
        port: 0,
        owned: true,
        status: 'ready',
        logPath: '',
      }),
      revealGatewayLog: async () => true,
      getDesktopSettings: async () => ({}),
      saveDesktopSettings: async () => ({}),
      resetDesktopSettings: async () => ({ ok: true }),
      getDesktopPreferences: async () => desktopPreferences,
      saveDesktopPreferences: async () => desktopPreferences,
      setNativeTheme: async () => undefined,
      openArtifact: async () => ({ ok: true }),
      chooseProjectDirectory: async () => null,
      getWorkbenchCapabilities: async () => ({
        protocolVersions: [1, 2],
        modes: ['full', 'offline'],
        maxSurfaces: 8,
      }),
      createArtifactPreviewLease: async (request: Record<string, unknown>) => {
        probe.leaseRequests.push(request)
        if (probe.leaseRequests.length === 1) return pendingLease
        const requestedMode = request.mode === 'offline' ? 'offline' : 'full'
        return leaseResponse(requestedMode, probe.leaseRequests.length)
      },
      renewArtifactPreviewLease: async (request: Record<string, unknown>) => ({
        ok: true,
        status: 200,
        payload: {
          version: 1,
          lease_id: request.leaseId,
          expires_at: expiresAt,
        },
      }),
      revokeArtifactPreviewLease: async () => ({
        ok: true,
        status: 204,
      }),
      createWorkbenchSurface: async (request: Record<string, unknown>) => {
        probe.createRequests.push(request)
        const surfaceId = String(request.surfaceId || '')
        queueMicrotask(() => probe.surfaceListener?.({
          version: 2,
          surfaceId,
          type: 'ready',
        }))
        return { ok: true }
      },
      setWorkbenchSurfaceRect: async (request: Record<string, unknown>) => {
        probe.rectRequests.push(request)
        return { ok: true }
      },
      activateWorkbenchSurface: async (surfaceId: string) => {
        probe.activations.push(surfaceId)
        return { ok: true }
      },
      destroyWorkbenchSurface: async (surfaceId: string) => {
        probe.destroyedSurfaces.push(surfaceId)
        return { ok: true }
      },
      onWorkbenchSurfaceEvent: (callback: (payload: unknown) => void) => {
        probe.surfaceListener = callback
        return () => {
          if (probe.surfaceListener === callback) probe.surfaceListener = null
        }
      },
      getOnboardingDefaults: async () => ({}),
      saveOnboarding: async () => ({}),
      cancelOnboarding: async () => ({}),
      getBootState: async () => ({}),
      retryStartup: async () => ({}),
      quitApp: async () => ({}),
      onBootStatus: () => () => {},
      onBootError: () => () => {},
    }
    const testWindow = window as unknown as {
      __opensquillaNativeWorkbenchProbe: typeof probe
      opensquillaDesktop: typeof bridge
    }
    testWindow.__opensquillaNativeWorkbenchProbe = probe
    testWindow.opensquillaDesktop = bridge
  }, { previewMode: mode })
}

async function deliverablesHeaderAction(page: Page): Promise<Locator> {
  const direct = page.locator('[data-testid="chat-session-action-deliverables"]:visible').first()
  const primary = page.locator(
    '[data-testid="chat-header-primary-action"][data-action="deliverables"]:visible',
  ).first()
  const action = direct.or(primary).first()
  await expect(action).toBeVisible()
  return action
}

async function tabUntilFocused(page: Page, target: Locator, attempts = 8) {
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    await page.keyboard.press('Tab')
    if (await target.evaluate(element => element === document.activeElement)) return
  }
  await expect(target).toBeFocused()
}

test.describe('Application Workbench', () => {
  for (const theme of ['dark', 'light'] as const) {
    test(`${theme} pointer resize keeps a single divider and restores keyboard focus`, async ({ page }, testInfo) => {
      await page.setViewportSize({ width: 1440, height: 900 })
      await page.addInitScript(value => localStorage.setItem('opensquilla-theme', value), theme)
      await openWorkbenchSession(page)
      await page.locator('.msg-artifact-chip', { hasText: 'demo.html' })
        .getByRole('button', { name: 'Open demo.html' }).click()
      const resizer = page.getByTestId('workbench-resizer')
      await expect(resizer).toBeVisible()
      await expect(page.locator('.artifact-preview__frame--html')).toBeVisible()

      // Exercise pointer focus from the composer and then from a
      // keyboard-focused divider, which previously kept its outline on drag.
      await page.locator('.chat-textarea').click()
      const initialWidth = Number(await resizer.getAttribute('aria-valuenow'))
      const box = (await resizer.boundingBox())!
      const dragY = box.y + box.height / 2
      await page.mouse.move(box.x + 3, dragY)
      await page.mouse.down()
      await page.mouse.move(box.x - 77, dragY, { steps: 16 })
      await expect(resizer).toHaveAttribute('aria-valuenow', String(initialWidth + 80))
      await page.screenshot({ path: testInfo.outputPath('pointer-drag.png') })
      await expect(resizer).toHaveCSS('outline-style', 'none')
      expect(await resizer.evaluate(element => getComputedStyle(element, '::before').width)).toBe('2px')

      await page.mouse.up()
      await page.mouse.move(40, 40)
      await expect(resizer).toBeFocused()
      await expect(resizer).toHaveCSS('outline-style', 'none')
      await page.screenshot({ path: testInfo.outputPath('pointer-released.png') })

      await page.keyboard.press('ArrowLeft')
      await expect(resizer).toHaveAttribute('aria-valuenow', String(initialWidth + 88))
      await expect(resizer).toHaveCSS('outline-style', 'solid')
      await expect(resizer).toHaveCSS('outline-width', '2px')
      await page.keyboard.press('Tab')
      await expect(resizer).not.toBeFocused()
      await page.keyboard.press('Shift+Tab')
      await expect(resizer).toBeFocused()
      await expect(resizer).toHaveCSS('outline-style', 'solid')
      await page.screenshot({ path: testInfo.outputPath('keyboard-focus.png') })

      const nextBox = (await resizer.boundingBox())!
      await page.mouse.move(nextBox.x + 3, dragY)
      await page.mouse.down()
      await page.mouse.move(nextBox.x + 43, dragY, { steps: 8 })
      await expect(resizer).toHaveAttribute('aria-valuenow', String(initialWidth + 48))
      await expect(resizer).toHaveCSS('outline-style', 'none')
      await page.keyboard.press('Escape')
      await page.mouse.up()
      await expect(resizer).toHaveAttribute('aria-valuenow', String(initialWidth + 88))
      await expect(page.locator('html')).not.toHaveClass(/is-workbench-resizing/)

      await page.emulateMedia({ forcedColors: 'active' })
      await page.keyboard.press('ArrowRight')
      await expect(resizer).toHaveAttribute('aria-valuenow', String(initialWidth + 80))
      await expect(resizer).toHaveCSS('outline-style', 'solid')
      await expect(resizer).toHaveCSS('outline-width', '2px')
    })
  }

  test('opens manual browser tabs from a fresh draft and preserves their owner on first send', async ({ page }) => {
    const sends: Record<string, unknown>[] = []
    const delayedDrafts: { requested: boolean; release?: () => void } = { requested: false }
    await installDesktopWorkbenchV2Bridge(page)
    await page.addInitScript(() => {
      window.OPENSQUILLA_FEATURES = { ...(window.OPENSQUILLA_FEATURES || {}), artifactWorkbench: true }
    })
    await installWorkbenchGateway(page, new Map(), [], sends, delayedDrafts)
    await page.goto(CONTROL_URL + 'chat/new')
    const toggle = page.getByTestId('topbar-workbench-toggle')
    const composer = page.locator('.chat-textarea')
    const draft = 'Inspect the webpage already open beside this task.'
    const add = page.getByRole('button', { name: 'Add', exact: true })
    const menu = page.getByRole('menu', { name: 'Add', exact: true })
    const openBrowserUse = async () => {
      await add.click()
      await menu.getByRole('menuitem', { name: /^Browser Use\b/ }).click()
      await expect(menu).toBeHidden()
      await expect(add).toHaveAttribute('aria-expanded', 'false')
      await expect(composer).toHaveValue(draft)
    }
    await expect(toggle).toBeVisible()
    await composer.fill(draft)
    await openBrowserUse()
    const workbench = page.getByTestId('workbench-host')
    const start = workbench.getByTestId('browser-start')
    await expect(start).toBeVisible()
    const address = start.getByRole('textbox', { name: 'Web address' })
    await address.fill('javascript:alert(1)')
    await start.getByRole('button', { name: 'Go', exact: true }).click()
    await expect(start.getByRole('alert')).toHaveText('Enter a valid HTTP or HTTPS web address.')
    const creates = () => page.evaluate(() => (window as unknown as {
      __opensquillaNativeWorkbenchProbe: { createRequests: Array<{
        surfaceId: string; payload: { url: string; scopeId: string }
      }> }
    }).__opensquillaNativeWorkbenchProbe.createRequests)
    expect(await creates()).toHaveLength(0)
    await address.fill('localhost:18807/check')
    await start.getByRole('button', { name: 'Go', exact: true }).click()
    await expect(workbench.getByRole('tab')).toHaveCount(1)
    await expect.poll(creates).toHaveLength(1)
    const first = (await creates())[0]!
    expect(first.payload.url).toBe('http://localhost:18807/check')
    expect(first.payload.scopeId).toMatch(/^agent:main:webchat:/)
    await expect.poll(() => delayedDrafts.requested).toBe(true)
    delayedDrafts.release!()
    await page.evaluate(() => new Promise<void>(resolve => {
      requestAnimationFrame(() => requestAnimationFrame(() => resolve()))
    }))
    await expect(page.locator('.chat-thread')).toHaveAttribute('data-session-key', first.payload.scopeId)

    await toggle.click()
    await expect(workbench).toBeHidden()
    await openBrowserUse()
    await expect(workbench).toBeVisible()
    await expect(workbench.getByRole('tab')).toHaveCount(1)
    expect(await creates()).toHaveLength(1)
    expect((await creates())[0]!.surfaceId).toBe(first.surfaceId)

    await workbench.getByTestId('workbench-new-browser-tab').click()
    await expect(start).toBeVisible()
    await expect(workbench.getByRole('tab')).toHaveCount(1)
    await address.fill('localhost:18807/check')
    await start.getByRole('button', { name: 'Go', exact: true }).click()
    await expect(workbench.getByRole('tab')).toHaveCount(2)
    await expect.poll(creates).toHaveLength(2)
    expect((await creates())[1]!.surfaceId).not.toBe(first.surfaceId)
    expect((await creates())[1]!.payload.scopeId).toBe(first.payload.scopeId)

    await expect(composer).toHaveValue(draft)
    await page.locator('.chat-send-btn[aria-label="Send"]').click()
    await expect.poll(() => sends.length).toBe(1)
    expect(sends[0]!.sessionKey).toBe(first.payload.scopeId)
    await expect.poll(() => new URL(page.url()).searchParams.get('session')).toBe(first.payload.scopeId)
    await expect(workbench.getByRole('tab')).toHaveCount(2)
    expect(await creates()).toHaveLength(2)

    await workbench.getByRole('button', { name: 'Close tab: localhost', exact: true }).last().click()
    await workbench.getByTestId('workbench-new-browser-tab').click()
    await start.getByRole('button', { name: 'Reopen closed tab' }).click()
    await expect(workbench.getByRole('tab')).toHaveCount(2)
    await workbench.getByRole('button', { name: 'Close tab: localhost', exact: true }).last().click()
    await workbench.getByRole('button', { name: 'Close tab: localhost', exact: true }).click()
    await expect(start).toBeVisible()
    await expect(toggle).toHaveAttribute('aria-expanded', 'true')
    await start.getByRole('button', { name: 'Reopen closed tab' }).click()
    await expect(workbench.getByRole('tab')).toHaveCount(1)
    await page.getByRole('button', { name: 'New task', exact: true }).click()
    await expect(start).toBeVisible()
    await expect(workbench.getByRole('tab')).toHaveCount(0)
    await expect(toggle).toBeVisible()
  })

  test('reopens retained agent pages after collapse and task changes', async ({ page }) => {
    await installDesktopWorkbenchV2Bridge(page)
    await openWorkbenchSession(page)
    const emitPage = async (surfaceId: string) => page.evaluate(({ surfaceId, sessionKey }) => {
      const probe = (window as unknown as {
        __opensquillaNativeWorkbenchProbe: { surfaceListener: (event: unknown) => void }
      }).__opensquillaNativeWorkbenchProbe
      probe.surfaceListener({ version: 4, surfaceId, type: 'browser-opened', detail: {
        sessionKey, url: 'https://example.test/cart', title: 'Browser cart', targetRef: `page-${surfaceId}`,
      } })
    }, { surfaceId, sessionKey: SESSION_KEY })
    const counts = () => page.evaluate(() => {
      const probe = (window as unknown as {
        __opensquillaNativeWorkbenchProbe: {
          createRequests: unknown[]; destroyedSurfaces: string[]; rectRequests: Array<{ surfaceId: string; visible: boolean }>
        }
      }).__opensquillaNativeWorkbenchProbe
      return { creates: probe.createRequests.length, destroyed: probe.destroyedSurfaces,
        lastRect: probe.rectRequests.at(-1) }
    })
    await emitPage('browser-retained')
    const workbench = page.getByTestId('workbench-host')
    const reopen = page.getByTestId('topbar-workbench-toggle')
    await expect(workbench).toBeVisible()
    await expect(page.locator('.topbar-right').getByTestId('topbar-workbench-toggle')).toBeVisible()
    await expect(page.getByTestId('workbench-reopen-browser')).toHaveCount(0)
    await expect(reopen).toHaveAttribute('aria-label', 'Collapse workbench')
    await expect(page.getByRole('button', { name: 'Collapse workbench', exact: true })).toHaveCount(1)
    await reopen.click()
    await expect(workbench).toBeHidden()
    await expect(reopen).toBeVisible()
    await expect(reopen).toHaveAttribute('title', 'Open workbench')
    await reopen.click()
    await expect(workbench).toBeVisible()
    await expect.poll(counts).toMatchObject({ creates: 0, destroyed: [] })

    await page.getByRole('button', { name: 'New task', exact: true }).click()
    await expect(workbench.getByTestId('browser-start')).toBeVisible()
    await expect(reopen).toBeVisible()
    await expect(workbench.getByRole('tab')).toHaveCount(0)
    await expect.poll(counts).toMatchObject({ creates: 0, destroyed: [] })
    await page.getByRole('button', { name: 'Browser workbench task', exact: true }).click()
    await expect(reopen).toBeVisible()
    await expect(workbench).toBeVisible()
    await expect(workbench.getByRole('tab')).toHaveCount(1)
    await expect.poll(counts).toMatchObject({ creates: 0, destroyed: [] })

    await emitPage('browser-second')
    await expect(workbench.getByRole('tab')).toHaveCount(2)
    await workbench.getByRole('button', { name: 'Close tab: Browser cart', exact: true }).last().click()
    await expect(workbench.getByRole('tab')).toHaveCount(1)
    await expect.poll(counts).toMatchObject({ destroyed: ['browser-second'] })
    await reopen.click()
    await reopen.click()
    await expect(workbench).toBeVisible()
    await expect.poll(counts).toMatchObject({ creates: 0, destroyed: ['browser-second'] })
    await workbench.getByRole('button', { name: 'Close tab: Browser cart', exact: true }).click()
    await expect(workbench.getByTestId('browser-start')).toBeVisible()
    await expect(reopen).toBeVisible()
    await workbench.getByRole('button', { name: 'Reopen closed tab', exact: true }).click()
    await expect(workbench).toBeVisible()
    await expect.poll(counts).toMatchObject({ creates: 1, destroyed: ['browser-second', 'browser-retained'] })
    await expect.poll(() => page.evaluate(() => {
      const probe = (window as unknown as {
        __opensquillaNativeWorkbenchProbe: { createRequests: Array<Record<string, unknown>> }
      }).__opensquillaNativeWorkbenchProbe
      return probe.createRequests[0]
    })).toEqual({ version: 2, surfaceId: 'browser-retained', kind: 'url-preview',
      payload: { url: 'https://example.test/cart', scopeId: SESSION_KEY } })
  })

  for (const mode of ['full', 'offline'] as const) {
    test(`Desktop v2 ${mode} preview is positioned when its slot becomes ready`, async ({
      page,
    }) => {
      // The full case omits a stored preference so it covers the fresh-profile
      // default. Offline remains covered as a valid persisted user choice.
      await installDesktopWorkbenchV2Bridge(page, mode === 'full' ? undefined : mode)
      await openWorkbenchSession(page)

      const htmlArtifact = page.locator('.msg-artifact-chip', { hasText: 'demo.html' })
      await htmlArtifact.getByRole('button', { name: 'Open demo.html' }).click()

      const workbench = page.getByTestId('workbench-host')
      await expect(workbench).toBeVisible()
      await expect.poll(() => page.evaluate(() => {
        const probe = (window as unknown as {
          __opensquillaNativeWorkbenchProbe: {
            leaseRequests: unknown[]
          }
        }).__opensquillaNativeWorkbenchProbe
        return probe.leaseRequests.length
      })).toBe(1)

      // Hold the lease response so the native slot is guaranteed to be absent
      // during the first measurement, matching the real loading -> ready race.
      await expect(workbench.locator('[data-workbench-native-surface-slot]')).toHaveCount(0)
      expect(await page.evaluate(() => {
        const probe = (window as unknown as {
          __opensquillaNativeWorkbenchProbe: {
            rectRequests: Array<{ visible?: boolean }>
          }
        }).__opensquillaNativeWorkbenchProbe
        return probe.rectRequests.some(request => request.visible === true)
      })).toBe(false)

      await page.evaluate(() => {
        const probe = (window as unknown as {
          __opensquillaNativeWorkbenchProbe: {
            releaseLease: () => void
          }
        }).__opensquillaNativeWorkbenchProbe
        probe.releaseLease()
      })

      await expect(workbench.locator('[data-workbench-native-surface-slot]')).toBeVisible()
      await expect.poll(() => page.evaluate((previewMode) => {
        const probe = (window as unknown as {
          __opensquillaNativeWorkbenchProbe: {
            createRequests: Array<{
              payload?: { mode?: string }
              version?: number
            }>
          }
        }).__opensquillaNativeWorkbenchProbe
        return probe.createRequests.some(request =>
          request.version === 2 && request.payload?.mode === previewMode)
      }, mode)).toBe(true)
      await expect.poll(() => page.evaluate(() => {
        const probe = (window as unknown as {
          __opensquillaNativeWorkbenchProbe: {
            rectRequests: Array<{
              height?: number
              visible?: boolean
              width?: number
            }>
          }
        }).__opensquillaNativeWorkbenchProbe
        return probe.rectRequests.some(request =>
          request.visible === true
          && Number(request.width) > 0
          && Number(request.height) > 0)
      })).toBe(true)

      const resizer = page.getByTestId('workbench-resizer')
      await expect(resizer).toBeVisible()
      const workbenchBox = await workbench.boundingBox()
      const resizerBox = await resizer.boundingBox()
      expect(workbenchBox).not.toBeNull()
      expect(resizerBox).not.toBeNull()
      expect(resizerBox!.x).toBeLessThan(workbenchBox!.x)
      expect(resizerBox!.width).toBeGreaterThanOrEqual(16)

      const initialValue = Number(await resizer.getAttribute('aria-valuenow'))
      const initialNativeWidth = await page.evaluate(() => {
        const probe = (window as unknown as {
          __opensquillaNativeWorkbenchProbe: {
            rectRequests: Array<{ visible?: boolean; width?: number }>
          }
        }).__opensquillaNativeWorkbenchProbe
        const visibleWidths = probe.rectRequests
          .filter(request => request.visible === true)
          .map(request => Number(request.width))
          .filter(width => Number.isFinite(width))
        return visibleWidths.at(-1) ?? 0
      })

      // Start from the chat-side half of the handle while the pointer is over
      // the native preview's content area. This is the regression case for the
      // clipped/covered resizer hit target.
      const dragY = workbenchBox!.y + workbenchBox!.height / 2
      await page.mouse.move(resizerBox!.x + 3, dragY)
      await page.mouse.down()
      await page.mouse.move(resizerBox!.x - 37, dragY, { steps: 4 })
      await expect.poll(async () => Number(await resizer.getAttribute('aria-valuenow')))
        .toBeGreaterThan(initialValue)
      await page.mouse.up()

      await expect.poll(() => page.evaluate(() => {
        const probe = (window as unknown as {
          __opensquillaNativeWorkbenchProbe: {
            rectRequests: Array<{ visible?: boolean; width?: number }>
          }
        }).__opensquillaNativeWorkbenchProbe
        const visibleWidths = probe.rectRequests
          .filter(request => request.visible === true)
          .map(request => Number(request.width))
          .filter(width => Number.isFinite(width))
        return visibleWidths.at(-1) ?? 0
      })).toBeGreaterThan(initialNativeWidth)

      await expect.poll(() => page.evaluate(() => {
        const raw = localStorage.getItem('opensquilla.workbench.width.v1')
        if (!raw) return null
        try {
          return JSON.parse(raw) as { source?: string; width?: number }
        } catch {
          return null
        }
      })).toMatchObject({ source: 'user' })
      expect(await page.evaluate(() => {
        const raw = localStorage.getItem('opensquilla.workbench.width.v1')
        if (!raw) return 0
        try {
          return Number((JSON.parse(raw) as { width?: number }).width)
        } catch {
          return 0
        }
      })).toBeGreaterThan(initialValue)

      // The native bridge must remain active after the divider drag and keep
      // reporting a visible surface.
      expect(await page.evaluate(() => {
        const probe = (window as unknown as {
          __opensquillaNativeWorkbenchProbe: {
            activations: string[]
          }
        }).__opensquillaNativeWorkbenchProbe
        return probe.activations.length
      })).toBeGreaterThan(0)
    })
  }

  test('preview mode switches immediately without a confirmation dialog', async ({ page }) => {
    await installDesktopWorkbenchV2Bridge(page)
    await openWorkbenchSession(page)

    const htmlArtifact = page.locator('.msg-artifact-chip', { hasText: 'demo.html' })
    await htmlArtifact.getByRole('button', { name: 'Open demo.html' }).click()

    await expect.poll(() => page.evaluate(() => {
      const probe = (window as unknown as {
        __opensquillaNativeWorkbenchProbe: {
          leaseRequests: unknown[]
        }
      }).__opensquillaNativeWorkbenchProbe
      return probe.leaseRequests.length
    })).toBe(1)
    await page.evaluate(() => {
      const probe = (window as unknown as {
        __opensquillaNativeWorkbenchProbe: {
          releaseLease: () => void
        }
      }).__opensquillaNativeWorkbenchProbe
      probe.releaseLease()
    })

    const modeSelect = page.locator('.app-workbench__mode-select:visible').first()
    await expect(modeSelect).toHaveValue('full')
    await modeSelect.selectOption('offline')

    await expect(page.getByRole('dialog', { name: 'Change preview mode?' })).toHaveCount(0)
    await expect.poll(() => page.evaluate(() => {
      const probe = (window as unknown as {
        __opensquillaNativeWorkbenchProbe: {
          leaseRequests: Array<{ mode?: string }>
        }
      }).__opensquillaNativeWorkbenchProbe
      return probe.leaseRequests.map(request => request.mode)
    })).toEqual(['full', 'offline'])
    await expect.poll(() => page.evaluate(() => {
      const probe = (window as unknown as {
        __opensquillaNativeWorkbenchProbe: {
          createRequests: Array<{ payload?: { mode?: string } }>
        }
      }).__opensquillaNativeWorkbenchProbe
      return probe.createRequests.map(request => request.payload?.mode)
    })).toEqual(['full', 'offline'])
    await expect(modeSelect).toHaveValue('offline')
  })

  test('header opens the latest preview and uses a compact artifact switcher', async ({ page }) => {
    const requests = new Map<string, number>()
    await openWorkbenchSession(page, requests)

    const deliverables = await deliverablesHeaderAction(page)
    await deliverables.click()

    const workbench = page.getByTestId('workbench-host')
    await expect(workbench).toBeVisible()
    await expect(workbench).toHaveAttribute('role', 'complementary')
    await expect(workbench.locator('.workbench-host__single-title'))
      .toContainText('demo.html')
    await expect(workbench.locator('[data-workbench-item-id]')).toHaveCount(1)
    await expect(workbench.locator('.artifact-preview__frame--html')).toBeVisible()
    expect(requests.get('/api/v1/artifacts/workbench-demo')).toBeGreaterThanOrEqual(1)

    const switcher = workbench.getByTestId('workbench-artifact-switcher')
    await expect(switcher).toHaveCount(1)
    await expect(switcher.locator('option')).toHaveCount(3)
    await switcher.selectOption({ label: 'notes.txt' })
    await expect(workbench.locator('.workbench-host__tabs').getByRole('tab')).toHaveCount(2)
    await expect(workbench.locator('.artifact-preview__text'))
      .toContainText('Workbench notes stay mounted.')
    const notesRequestCount = requests.get('/api/v1/artifacts/workbench-notes') ?? 0
    expect(notesRequestCount).toBeGreaterThanOrEqual(1)

    await switcher.selectOption({ label: 'notes.txt' })
    await expect(workbench.locator('.workbench-host__tabs').getByRole('tab')).toHaveCount(2)
    expect(requests.get('/api/v1/artifacts/workbench-notes')).toBe(notesRequestCount)

    await switcher.selectOption({ label: 'guide.md' })
    await expect(workbench.locator('.workbench-host__tabs').getByRole('tab')).toHaveCount(3)
    await expect(workbench.locator('.artifact-preview__markdown')).toContainText('Guide')
    const guideRequestCount = requests.get('/api/v1/artifacts/workbench-guide') ?? 0
    expect(guideRequestCount).toBeGreaterThanOrEqual(1)

    await page.getByTestId('topbar-workbench-toggle').click()
    await expect(workbench).toBeHidden()

    await (await deliverablesHeaderAction(page)).click()
    await expect(workbench).toBeVisible()
    await expect(workbench.locator('.artifact-preview__markdown')).toContainText('Guide')
    expect(requests.get('/api/v1/artifacts/workbench-guide')).toBeGreaterThan(guideRequestCount)
  })

  test('download-only deliverables remain downloadable and open in the Deliverables drawer', async ({ page }) => {
    const downloadOnlyArtifacts = [{
      id: 'workbench-data',
      name: 'data.json',
      mime: 'application/json',
      size: 24,
      download_url: '/api/v1/artifacts/workbench-data',
    }]
    await openWorkbenchSession(page, new Map(), downloadOnlyArtifacts)

    const workbench = page.getByTestId('workbench-host')
    await expect(workbench).toHaveCount(0)
    const deliverables = await deliverablesHeaderAction(page)
    await expect(deliverables).toHaveAccessibleName(/Deliverables \(1\)/)
    await deliverables.click()
    const drawer = page.getByRole('dialog', { name: 'Deliverables (1)' })
    await expect(drawer).toBeVisible()
    await expect(drawer.getByRole('button', { name: /Open data\.json/ })).toBeVisible()
    await expect(workbench).toHaveCount(0)
    await expect(page.locator('.msg-artifact-chip', { hasText: 'data.json' })
      .getByRole('button', { name: 'Download data.json' })).toBeVisible()
  })

  test('the compact switcher lists every deliverable without eagerly fetching PPTX', async ({ page }) => {
    const requests = new Map<string, number>()
    const artifacts = [
      {
        id: 'workbench-slides',
        name: 'deck.pptx',
        mime: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
        size: 48,
        download_url: '/api/v1/artifacts/workbench-slides',
      },
      ARTIFACTS[2],
    ]
    await openWorkbenchSession(page, requests, artifacts)

    const deliverables = await deliverablesHeaderAction(page)
    await expect(deliverables).toHaveAccessibleName(/Deliverables \(2\)/)
    await deliverables.click()

    const workbench = page.getByTestId('workbench-host')
    await expect(workbench).toBeVisible()
    await expect(workbench.locator('.artifact-preview__frame--html')).toBeVisible()
    const switcher = workbench.getByTestId('workbench-artifact-switcher')
    await expect(switcher.locator('option')).toHaveText([
      'deck.pptx',
      'demo.html',
    ])
    await expect(page.locator('.msg-artifact-chip', { hasText: 'deck.pptx' })
      .getByRole('button', { name: 'Download deck.pptx' })).toBeVisible()
    expect(requests.get('/api/v1/artifacts/workbench-slides')).toBeUndefined()
  })

  test('the compact navigator lists document resources and excludes media files', async ({ page }) => {
    const requests = new Map<string, number>()
    const mixedArtifacts = [
      {
        id: 'workbench-data',
        name: 'data.json',
        mime: 'application/json',
        size: 24,
        download_url: '/api/v1/artifacts/workbench-data',
      },
      {
        id: 'workbench-audio',
        name: 'sample.wav',
        mime: 'audio/wav',
        size: 44,
        download_url: '/api/v1/artifacts/workbench-audio',
      },
      {
        id: 'workbench-video',
        name: 'sample.webm',
        mime: 'video/webm',
        size: 44,
        download_url: '/api/v1/artifacts/workbench-video',
      },
      {
        id: 'workbench-image-a',
        name: 'poster.png',
        mime: 'image/png',
        size: PNG_1x1.length,
        download_url: '/api/v1/artifacts/workbench-image-a',
        thumbnail_url: '/api/v1/artifacts/workbench-image-a?variant=thumb',
      },
      {
        id: 'workbench-slides',
        name: 'deck.pptx',
        mime: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
        size: 48,
        download_url: '/api/v1/artifacts/workbench-slides',
      },
      ARTIFACTS[0],
      ARTIFACTS[2],
    ]
    await openWorkbenchSession(page, requests, mixedArtifacts)

    const deliverables = await deliverablesHeaderAction(page)
    await expect(deliverables).toHaveAccessibleName(/Deliverables \(7\)/)
    await deliverables.click()
    const drawer = page.getByRole('dialog', { name: 'Deliverables (7)' })
    await expect(drawer).toBeVisible()
    await drawer.getByRole('button', { name: 'Close' }).click()
    await page.locator('.msg-artifact-chip', { hasText: 'demo.html' })
      .getByRole('button', { name: 'Open demo.html' }).click()

    const workbench = page.getByTestId('workbench-host')
    await expect(workbench).toBeVisible()
    await expect(workbench.locator('.artifact-preview__frame--html')).toBeVisible()
    const switcher = workbench.getByTestId('workbench-artifact-switcher')
    await expect(switcher.locator('option')).toHaveCount(3)
    await expect(switcher.locator('option')).toHaveText([
      'deck.pptx',
      'notes.txt',
      'demo.html',
    ])

    await switcher.selectOption({ label: 'notes.txt' })
    await expect(workbench).toBeVisible()
    await expect(workbench.locator('.artifact-preview__text'))
      .toContainText('Workbench notes stay mounted.')
    await expect(workbench.locator('[data-preview-kind="unsupported"]')).toHaveCount(0)
    await expect(page.locator('.msg-artifact-chip', { hasText: 'deck.pptx' })
      .getByRole('button', { name: 'Download deck.pptx' })).toBeVisible()
  })

  test('opening the same artifact card reuses one tab after the Workbench collapses', async ({ page }) => {
    await openWorkbenchSession(page)
    const notes = page.locator('.msg-artifact-chip', { hasText: 'notes.txt' })
    const open = notes.getByRole('button', { name: 'Open notes.txt' })

    await open.click()
    const workbench = page.getByTestId('workbench-host')
    await expect(workbench).toBeVisible()
    await expect(workbench.locator('[data-workbench-item-id]')).toHaveCount(1)

    await open.click()
    await expect(workbench).toBeVisible()
    await expect(workbench.locator('[data-workbench-item-id]')).toHaveCount(1)
    await expect(workbench.locator('.workbench-host__tabs')).toHaveCount(0)

    await page.getByTestId('topbar-workbench-toggle').click()
    await expect(workbench).toBeHidden()

    await open.click()
    await expect(workbench).toBeVisible()
    await expect(workbench.locator('[data-workbench-item-id]')).toHaveCount(1)
  })

  test('mobile HTML preview enters the dialog Tab order and bridges Escape', async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 667 })
    await openWorkbenchSession(page)

    const deliverables = await deliverablesHeaderAction(page)
    await deliverables.click()

    const workbench = page.getByTestId('workbench-host')
    await expect(workbench).toBeVisible()
    await expect(workbench).toHaveAttribute('role', 'dialog')
    await expect(workbench).toHaveAttribute('aria-modal', 'true')
    await expect(workbench).toHaveCSS('width', '375px')
    const mobileFrame = workbench.locator('.artifact-preview__frame--html')
    await expect(mobileFrame).not.toHaveAttribute('aria-hidden', 'true')
    await expect(mobileFrame).toHaveAttribute('tabindex', '0')
    await expect(mobileFrame).toHaveCSS('pointer-events', 'auto')

    await expect(
      workbench.locator('.workbench-host__actions')
        .getByRole('button', { name: 'Collapse workbench' }),
    ).toBeFocused()
    await tabUntilFocused(page, mobileFrame)

    // The opaque sandbox cannot bubble key events to the parent document.
    // Its injected bridge posts a narrow Escape message instead.
    await page.keyboard.press('Escape')
    await expect(workbench).toBeHidden()
    await expect(page.getByTestId('chat-header-primary-action')).toBeFocused()
  })

  test('mobile PDF preview offers a focus-revealed exit after the browser viewer', async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 667 })
    await openWorkbenchSession(page, new Map(), [{
      id: 'workbench-report',
      name: 'report.pdf',
      mime: 'application/pdf',
      size: 16,
      download_url: '/api/v1/artifacts/workbench-report',
    }])

    const deliverables = await deliverablesHeaderAction(page)
    await deliverables.click()

    const workbench = page.getByTestId('workbench-host')
    const pdfFrame = workbench.locator('.artifact-preview__frame--pdf')
    const frameExit = workbench.locator('.artifact-preview__frame-exit')
    await expect(workbench).toHaveAttribute('role', 'dialog')
    await expect(pdfFrame).toHaveAttribute('tabindex', '0')

    await expect(
      workbench.locator('.workbench-host__actions')
        .getByRole('button', { name: 'Collapse workbench' }),
    ).toBeFocused()
    await tabUntilFocused(page, pdfFrame)

    // Chromium's built-in PDF viewer owns its document and cannot receive our
    // injected HTML Escape bridge. A trailing skip-style control provides a
    // deterministic keyboard exit without replacing the viewer with PDF.js.
    await frameExit.focus()
    await expect(frameExit).toBeFocused()
    await expect(frameExit).toBeVisible()
    await expect(frameExit).toHaveCSS('position', 'static')
    const pdfBox = await pdfFrame.boundingBox()
    const exitBox = await frameExit.boundingBox()
    expect(pdfBox).not.toBeNull()
    expect(exitBox).not.toBeNull()
    expect(exitBox!.y).toBeGreaterThanOrEqual(pdfBox!.y + pdfBox!.height)
    await page.keyboard.press('Enter')
    await expect(workbench).toBeHidden()
    await expect(deliverables).toBeFocused()
  })

  test('mobile image cards keep using the transcript Lightbox outside Workbench navigation', async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 667 })
    const artifacts = [
      {
        id: 'workbench-image-a',
        name: 'first.png',
        mime: 'image/png',
        size: PNG_1x1.length,
        download_url: '/api/v1/artifacts/workbench-image-a',
        thumbnail_url: '/api/v1/artifacts/workbench-image-a?variant=thumb',
      },
      ARTIFACTS[2],
    ]
    await openWorkbenchSession(page, new Map(), artifacts)

    const deliverables = await deliverablesHeaderAction(page)
    await deliverables.click()
    const drawer = page.getByRole('dialog', { name: 'Deliverables (2)' })
    await expect(drawer).toBeVisible()
    await drawer.getByRole('button', { name: 'Close' }).click()
    const openHtml = page.locator('.msg-artifact-chip', { hasText: 'demo.html' })
      .getByRole('button', { name: 'Open demo.html' })
    await openHtml.click()
    const workbench = page.getByTestId('workbench-host')
    await expect(workbench).toBeVisible()
    await expect(workbench.getByTestId('workbench-artifact-switcher')).toHaveCount(0)
    await workbench.getByRole('button', { name: 'Collapse workbench' }).click()
    await expect(workbench).toBeHidden()

    const imageTrigger = page.locator('.msg-media-card__img')
    await expect(imageTrigger.locator('img')).toBeVisible()
    await imageTrigger.click()

    const lightbox = page.locator('.deliv-preview[role="dialog"]')
    await expect(lightbox).toBeVisible()
    await expect(workbench).toBeHidden()

    await page.keyboard.press('Escape')
    await expect(lightbox).toHaveCount(0)
    await expect(imageTrigger).toBeFocused()

    await openHtml.click()
    await expect(workbench).toBeVisible()
    await expect(workbench.locator('.artifact-preview__frame--html')).toBeVisible()
  })
})
