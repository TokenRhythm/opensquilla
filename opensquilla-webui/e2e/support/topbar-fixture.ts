import { expect, type Page, type WebSocketRoute } from '@playwright/test'

export const CONTROL_URL = '/control/'
export const TOPBAR_FIXED_TIME = new Date('2024-01-15T08:00:00.000Z')
export const TOPBAR_SESSION_KEY = 'agent:main:webchat:e2e-topbar'
export const TOPBAR_SESSION_TITLE = 'Responsive header fixture'

export const TOPBAR_GEOMETRY_VIEWPORTS = [
  { width: 320, height: 720 },
  { width: 375, height: 812 },
  { width: 400, height: 800 },
  { width: 480, height: 800 },
  { width: 768, height: 900 },
  { width: 769, height: 900 },
  { width: 959, height: 900 },
  { width: 960, height: 900 },
  { width: 1440, height: 1000 },
] as const

export type TopbarLocale = 'en' | 'zh-Hans' | 'de'
export type TopbarUpdateStatus =
  | 'idle'
  | 'checking'
  | 'available'
  | 'downloading'
  | 'downloaded'
  | 'not-available'
  | 'error'
  | 'applying'

export type TopbarUpdateState = {
  status: TopbarUpdateStatus
  currentVersion: string
  latestVersion: string | null
  progress: number | null
  checkedAt: string | null
  error: string | null
  errorCode: string | null
  snoozedUntil: string | null
  canCheck: boolean
  canNativeInstall: boolean
  installMode: 'native' | 'manual' | 'unsupported'
  releaseUrl: string | null
  source: 'oss' | 'github' | null
  fallbackUsed: boolean
}

export type TopbarScenario = {
  sessionKey?: string
  title?: string
  locale?: TopbarLocale
  theme?: string
  deliverableCount?: number
  approvalCount?: number
  bgm?: {
    enabled: boolean
    playing: boolean
  }
  update?: Partial<TopbarUpdateState> | null
}

export type TopbarGeometryProbe = {
  clientWidth: number
  scrollWidth: number
  topbarClientWidth: number
  topbarScrollWidth: number
  controls: Array<{
    label: string
    left: number
    right: number
    top: number
    bottom: number
    width: number
    height: number
  }>
  outsideViewport: string[]
  overlaps: string[]
  missedCenters: string[]
  undersizedTargets: string[]
  routeSystemOverlap: boolean
  legacyReserveValues: string[]
}

type RpcFrame = {
  id?: string | number
  method?: string
  params?: Record<string, unknown>
  type?: string
}

type BrowserUpdateBridge = Window & {
  __opensquillaTopbarE2E?: {
    pushUpdate: (patch: Partial<TopbarUpdateState>) => void
  }
}

const consoleIssuesByPage = new WeakMap<Page, string[]>()

export function monitorTopbarConsole(page: Page) {
  if (consoleIssuesByPage.has(page)) return
  const issues: string[] = []
  consoleIssuesByPage.set(page, issues)
  page.on('console', message => {
    if (message.type() === 'warning' || message.type() === 'error') {
      issues.push(`console.${message.type()}: ${message.text()}`)
    }
  })
  page.on('pageerror', error => {
    issues.push(`pageerror: ${error.message}`)
  })
}

export function expectTopbarConsoleClean(page: Page) {
  expect(consoleIssuesByPage.get(page) ?? []).toEqual([])
}

function completeUpdateState(patch: Partial<TopbarUpdateState> = {}): TopbarUpdateState {
  return {
    status: 'idle',
    currentVersion: '1.8.0',
    latestVersion: null,
    progress: null,
    checkedAt: TOPBAR_FIXED_TIME.toISOString(),
    error: null,
    errorCode: null,
    snoozedUntil: null,
    canCheck: true,
    canNativeInstall: true,
    installMode: 'native',
    releaseUrl: 'https://example.invalid/opensquilla/releases/2.0.0',
    source: 'github',
    fallbackUsed: false,
    ...patch,
  }
}

function response(id: string | number | undefined, payload: unknown) {
  return JSON.stringify({ type: 'res', id, ok: true, payload })
}

function approvalSnapshot(sessionKey: string, count: number) {
  return Array.from({ length: count }, (_, index) => ({
    id: `approval-topbar-${index + 1}`,
    namespace: 'exec',
    toolName: 'shell',
    command: `synthetic command ${index + 1}`,
    sessionKey,
    created_at: TOPBAR_FIXED_TIME.getTime() / 1000 + index,
  }))
}

function historyPayload(deliverableCount: number) {
  const artifacts = Array.from({ length: deliverableCount }, (_, index) => ({
    id: `artifact-topbar-${index + 1}`,
    name: `status-report-${index + 1}.csv`,
    mime: 'text/csv',
    size: 2048 + index,
  }))
  return {
    messages: [
      {
        role: 'user',
        text: 'Review the responsive topbar contract.',
        message_id: 'msg-topbar-user',
        timestamp: '2024-01-15T07:58:00.000Z',
      },
      {
        role: 'assistant',
        text: 'The deterministic topbar fixture is ready.',
        message_id: 'msg-topbar-assistant',
        timestamp: '2024-01-15T07:59:00.000Z',
        artifacts,
      },
    ],
    has_more: false,
    canonical_complete: true,
  }
}

async function installPreferences(page: Page, scenario: Required<Pick<
  TopbarScenario,
  'locale' | 'theme' | 'bgm'
>>) {
  await page.addInitScript(({ locale, theme, bgm }) => {
    localStorage.setItem('opensquilla-locale', locale)
    localStorage.setItem('opensquilla-theme', theme)
    localStorage.setItem('opensquilla-bgm', JSON.stringify({
      enabled: bgm.enabled,
      playing: bgm.playing,
      trackId: bgm.playing ? 'topbar-synthetic-track' : '',
      volume: 0.35,
    }))

    if (bgm.playing) {
      // The shipped playlist is intentionally empty. Stub only the media
      // settlement and provide one synthetic manifest entry so "playing" is a
      // stable UI state without making an audio or third-party network request.
      HTMLMediaElement.prototype.play = function play() {
        this.dispatchEvent(new Event('play'))
        return Promise.resolve()
      }
      HTMLMediaElement.prototype.pause = function pause() {
        this.dispatchEvent(new Event('pause'))
      }
    }
  }, scenario)
}

async function installDesktopUpdateBridge(
  page: Page,
  initialPatch: Partial<TopbarUpdateState>,
) {
  const initial = completeUpdateState(initialPatch)
  await page.addInitScript((initialState: TopbarUpdateState) => {
    let state = { ...initialState }
    const listeners = new Set<(payload: unknown) => void>()
    const gateway = {
      url: location.origin,
      port: Number(location.port || 80),
      owned: true,
      status: 'ready' as const,
      logPath: '/synthetic/opensquilla.log',
    }
    const settings = {
      provider: 'openai',
      model: 'synthetic-model',
      baseUrl: '',
      apiKeyConfigured: true,
      searchProvider: 'none',
      searchApiKeyEnv: '',
      searchApiKeyConfigured: false,
      disableNetworkObservability: false,
      gateway,
    }
    const publish = (patch: Partial<TopbarUpdateState>) => {
      state = { ...state, ...patch }
      for (const listener of listeners) listener({ ...state })
    }

    ;(window as BrowserUpdateBridge).__opensquillaTopbarE2E = { pushUpdate: publish }
    window.opensquillaDesktop = {
      getOsLocale: async () => 'en-US',
      isAutoUpdateEnabled: async () => true,
      isDesktopUpdateManaged: async () => true,
      getUpdateState: async () => ({ ...state }),
      checkForUpdates: async () => ({ ...state }),
      downloadUpdate: async () => ({ ...state }),
      relaunchToUpdate: async () => ({ ...state }),
      dismissUpdate: async () => ({ ...state }),
      onUpdateState(callback) {
        listeners.add(callback)
        return () => listeners.delete(callback)
      },
      getGatewayStatus: async () => gateway,
      revealGatewayLog: async () => false,
      retryStartup: async () => ({ ok: true }),
      getDesktopSettings: async () => settings,
      saveDesktopSettings: async () => settings,
      resetDesktopSettings: async () => ({ ok: true }),
      getDesktopPreferences: async () => ({
        mainWindowCloseBehavior: 'background',
        canRunInBackground: true,
        platform: 'darwin',
      }),
      saveDesktopPreferences: async () => ({
        mainWindowCloseBehavior: 'background',
        canRunInBackground: true,
        platform: 'darwin',
      }),
      setNativeTheme: async () => undefined,
      openArtifact: async () => ({ ok: false, error: 'synthetic fixture' }),
      chooseProjectDirectory: async () => null,
      getOnboardingDefaults: async () => ({}),
      saveOnboarding: async () => ({}),
      cancelOnboarding: async () => ({}),
      getBootState: async () => ({ status: 'ready' }),
      quitApp: async () => undefined,
      onBootStatus: () => () => undefined,
      onBootError: () => () => undefined,
    }
  }, initial)
}

async function installStaticRoutes(
  page: Page,
  sessionKey: string,
  approvals: ReturnType<typeof approvalSnapshot>,
  bgmEnabled: boolean,
) {
  await page.route('**/api/system/update', route => route.fulfill({ json: {} }))
  await page.route('**/api/elevated-mode', route => route.fulfill({
    json: { enabled: false },
  }))
  await page.route('**/api/approvals', route => route.fulfill({
    json: { pending: approvals, mode: 'prompt', allowPatterns: [], denyPatterns: [] },
  }))
  if (bgmEnabled) {
    const playlist = {
      tracks: [{
        id: 'topbar-synthetic-track',
        title: 'Synthetic silence',
        src: 'https://example.invalid/topbar-silence.mp3',
      }],
    }
    await page.route('**/music/playlist.local.json', route => route.fulfill({ json: playlist }))
    await page.route('**/music/playlist.json', route => route.fulfill({ json: playlist }))
  }

  // Keep the route's session explicit even when a gateway with unrelated
  // operator data is behind the page. The WebSocket below is fully synthetic.
  await page.route(`**/api/approvals/resolve`, route => route.fulfill({
    json: { ok: true, sessionKey },
  }))
}

async function installMockGateway(
  page: Page,
  sessionKey: string,
  title: string,
  deliverableCount: number,
) {
  let forcedDisconnected = false
  const sockets = new Set<WebSocketRoute>()

  await page.routeWebSocket(/\/ws$/, ws => {
    sockets.add(ws)
    ws.onClose(() => sockets.delete(ws))
    if (forcedDisconnected) {
      void ws.close({ code: 1012, reason: 'synthetic offline state' })
      return
    }
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(message => {
      let frame: RpcFrame
      try {
        frame = JSON.parse(String(message)) as RpcFrame
      } catch {
        return
      }
      if (frame.type !== 'req') return
      const method = String(frame.method || '')

      if (method === 'connect') {
        ws.send(JSON.stringify({
          protocol: 3,
          policy: { tick_interval_ms: 30_000 },
          auth: {
            runModePolicy: {
              allowedRunModes: ['safe', 'full'],
              defaultRunMode: 'full',
            },
          },
        }))
        return
      }
      if (method === 'chat.history') {
        ws.send(response(frame.id, historyPayload(deliverableCount)))
        return
      }
      if (method === 'sessions.messages.subscribe') {
        ws.send(response(frame.id, {
          subscribed: true,
          replay_complete: true,
          current_stream_seq: 0,
          run_status: 'idle',
          active_task: null,
        }))
        return
      }
      if (method === 'sandbox.run_mode.preference.get') {
        ws.send(response(frame.id, { runMode: 'full', source: 'config' }))
        return
      }

      const payloads: Record<string, unknown> = {
        'agents.list': { agents: [] },
        'commands.list_for_surface': { commands: [] },
        'config.get': {
          squilla_router: { enabled: false, rollout_phase: 'observe', tiers: {} },
          permissions: {},
          skills: {},
        },
        'onboarding.status': { audioConfigured: false },
        'sandbox.capability.status': { available: false },
        'sessions.list': {
          sessions: [{
            key: sessionKey,
            title,
            sessionKind: 'chat',
            surface: 'webchat',
            conversationKind: 'direct',
            effectiveAgentId: 'main',
            updatedAt: TOPBAR_FIXED_TIME.getTime() / 1000,
            messageCount: 2,
            status: 'ok',
            runStatus: 'idle',
          }],
          has_more: false,
        },
        'sessions.messages.unsubscribe': { subscribed: false },
        'usage.status': { sessions: [] },
      }
      ws.send(response(frame.id, payloads[method] ?? {}))
    })
  })

  return {
    async disconnect() {
      forcedDisconnected = true
      await Promise.all([...sockets].map(socket => socket.close({
        code: 1012,
        reason: 'synthetic offline state',
      })))
    },
  }
}

export async function waitForTopbarStable(page: Page, theme?: string) {
  await expect(page.locator('.conn-pill.connected').first()).toBeVisible({ timeout: 10_000 })
  await expect(page.getByTestId('route-header-host').locator('.chat-header')).toHaveCount(1)
  await expect(page.locator('.msg-ai-main').last()).toBeVisible({ timeout: 10_000 })
  if (theme) await expect(page.locator('html')).toHaveAttribute('data-theme', theme)
  await page.evaluate(async () => {
    await document.fonts.ready
    await new Promise<void>(resolve => requestAnimationFrame(() => resolve()))
    await new Promise<void>(resolve => requestAnimationFrame(() => resolve()))
  })
}

export async function openTopbarSession(page: Page, options: TopbarScenario = {}) {
  monitorTopbarConsole(page)
  const sessionKey = options.sessionKey || TOPBAR_SESSION_KEY
  const title = options.title || TOPBAR_SESSION_TITLE
  const locale = options.locale || 'en'
  const theme = options.theme || 'light'
  const deliverableCount = Math.max(0, Math.floor(options.deliverableCount ?? 1))
  const approvalCount = Math.max(0, Math.floor(options.approvalCount ?? 0))
  const bgm = options.bgm || { enabled: false, playing: false }
  const approvals = approvalSnapshot(sessionKey, approvalCount)

  await page.clock.setFixedTime(TOPBAR_FIXED_TIME)
  await installPreferences(page, { locale, theme, bgm })
  if (options.update) await installDesktopUpdateBridge(page, options.update)
  await installStaticRoutes(page, sessionKey, approvals, bgm.enabled)
  const gateway = await installMockGateway(page, sessionKey, title, deliverableCount)

  await page.goto(`${CONTROL_URL}chat?session=${encodeURIComponent(sessionKey)}`)
  await expect(page.locator('html')).toHaveAttribute('lang', locale)
  await waitForTopbarStable(page, theme)
  if (approvalCount > 0) {
    await expect(page.getByTestId('chat-system-status')).toHaveAttribute('data-severity', 'danger')
  }
  if (bgm.playing) {
    await expect(page.getByTestId('bgm-toggle')).toHaveAttribute('aria-pressed', 'true')
  }

  return {
    sessionKey,
    disconnect: gateway.disconnect,
    async pushUpdate(patch: Partial<TopbarUpdateState>) {
      await page.evaluate((next) => {
        ;(window as BrowserUpdateBridge).__opensquillaTopbarE2E?.pushUpdate(next)
      }, patch)
      await page.evaluate(async () => {
        await new Promise<void>(resolve => requestAnimationFrame(() => resolve()))
      })
    },
  }
}

export async function probeTopbarGeometry(
  page: Page,
  minimumTargetSize = 0,
): Promise<TopbarGeometryProbe> {
  return page.evaluate((minTargetSize) => {
    const isVisible = (element: HTMLElement) => {
      const style = getComputedStyle(element)
      const box = element.getBoundingClientRect()
      return style.display !== 'none'
        && style.visibility !== 'hidden'
        && Number(style.opacity) > 0
        && box.width > 0
        && box.height > 0
    }
    const labelFor = (element: HTMLElement, index: number) =>
      element.dataset.testid
      || element.getAttribute('aria-label')
      || element.getAttribute('title')
      || element.textContent?.trim()
      || `${element.tagName.toLowerCase()}[${index}]`
    const actionSelector = [
      '.topbar button',
      '.topbar a[href]',
      '.chat-header button',
      '.chat-header a[href]',
      '[data-chat-topbar-popover] button',
      '[data-chat-topbar-popover] a[href]',
    ].join(', ')
    const elements = Array.from(new Set(
      document.querySelectorAll<HTMLElement>(actionSelector),
    )).filter(isVisible)
    const controls = elements.map((element, index) => {
      const box = element.getBoundingClientRect()
      return {
        element,
        composite: element.closest('.bgm-menu-wrap'),
        label: labelFor(element, index),
        left: box.left,
        right: box.right,
        top: box.top,
        bottom: box.bottom,
        width: box.width,
        height: box.height,
      }
    })
    const outsideViewport = controls
      .filter(control => control.left < -0.5
        || control.right > window.innerWidth + 0.5
        || control.top < -0.5
        || control.bottom > window.innerHeight + 0.5)
      .map(control => control.label)
    const overlaps: string[] = []
    for (let leftIndex = 0; leftIndex < controls.length; leftIndex += 1) {
      for (let rightIndex = leftIndex + 1; rightIndex < controls.length; rightIndex += 1) {
        const left = controls[leftIndex]
        const right = controls[rightIndex]
        // BGM is an intentional split button; its two controls share a border.
        if (left.composite && left.composite === right.composite) continue
        const overlapWidth = Math.min(left.right, right.right) - Math.max(left.left, right.left)
        const overlapHeight = Math.min(left.bottom, right.bottom) - Math.max(left.top, right.top)
        if (overlapWidth > 0.5 && overlapHeight > 0.5) {
          overlaps.push(`${left.label} <> ${right.label}`)
        }
      }
    }
    const missedCenters = controls.flatMap(control => {
      const x = (control.left + control.right) / 2
      const y = (control.top + control.bottom) / 2
      const hit = document.elementFromPoint(x, y)
      return hit && (hit === control.element || control.element.contains(hit))
        ? []
        : [control.label]
    })
    const undersizedTargets = minTargetSize > 0
      ? controls
          .filter(control => control.width + 0.5 < minTargetSize
            || control.height + 0.5 < minTargetSize)
          .map(control => `${control.label} (${control.width.toFixed(1)}x${control.height.toFixed(1)})`)
      : []

    const topbar = document.querySelector<HTMLElement>('.topbar')
    const routeHeader = document.querySelector<HTMLElement>('.topbar-route-header')
    const systemHeader = document.querySelector<HTMLElement>('.topbar-right')
    const routeBox = routeHeader?.getBoundingClientRect()
    const systemBox = systemHeader?.getBoundingClientRect()
    const routeSystemOverlap = Boolean(routeBox && systemBox
      && Math.min(routeBox.right, systemBox.right) - Math.max(routeBox.left, systemBox.left) > 0.5
      && Math.min(routeBox.bottom, systemBox.bottom) - Math.max(routeBox.top, systemBox.top) > 0.5)
    const reserveNodes = [
      document.documentElement,
      document.getElementById('app'),
      topbar,
      routeHeader,
      document.querySelector<HTMLElement>('.chat-header'),
    ].filter((node): node is HTMLElement => node instanceof HTMLElement)
    const legacyReserveValues = reserveNodes.flatMap(node => {
      const style = getComputedStyle(node)
      return [
        style.getPropertyValue('--topbar-right-reserve').trim(),
        style.paddingInlineEnd,
        style.paddingRight,
      ].filter(value => value === '224px')
    })

    return {
      clientWidth: document.documentElement.clientWidth,
      scrollWidth: document.documentElement.scrollWidth,
      topbarClientWidth: topbar?.clientWidth ?? 0,
      topbarScrollWidth: topbar?.scrollWidth ?? 0,
      controls: controls.map(({ element: _element, composite: _composite, ...control }) => control),
      outsideViewport,
      overlaps,
      missedCenters,
      undersizedTargets,
      routeSystemOverlap,
      legacyReserveValues,
    }
  }, minimumTargetSize)
}

export async function expectTopbarGeometry(
  page: Page,
  options: { minimumTargetSize?: number } = {},
) {
  const probe = await probeTopbarGeometry(page, options.minimumTargetSize || 0)
  const detail = JSON.stringify(probe.controls, null, 2)
  expect(probe.controls.length).toBeGreaterThan(0)
  expect(probe.scrollWidth, detail).toBeLessThanOrEqual(probe.clientWidth)
  expect(probe.topbarScrollWidth, detail).toBeLessThanOrEqual(probe.topbarClientWidth)
  expect(probe.outsideViewport, detail).toEqual([])
  expect(probe.overlaps, detail).toEqual([])
  expect(probe.missedCenters, detail).toEqual([])
  expect(probe.undersizedTargets, detail).toEqual([])
  expect(probe.routeSystemOverlap, detail).toBe(false)
  expect(probe.legacyReserveValues, detail).toEqual([])
}
