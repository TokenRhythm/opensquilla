import {
  expect,
  test,
  type Page,
  type WebSocketRoute,
} from '@playwright/test'

const CONTROL_URL = '/control/'
const SESSION_A = 'agent:main:webchat:e2e-workspace-a'
const SESSION_B = 'agent:main:webchat:e2e-workspace-b'
const WORKSPACE_A = 'workspace-e2e-a'
const WORKSPACE_B = 'workspace-e2e-b'
const STALE_A_TEXT = 'This late Workspace A event must never render in Workspace B.'
const FRESH_B_TEXT = 'Workspace B continues on the original transport.'
const PRESERVED_DRAFT = 'Keep this Workspace B draft while live updates recover.'

type RpcRequest = {
  id?: string | number
  method?: string
  params?: Record<string, unknown>
  type?: string
}

type HeldRequest = {
  frame: RpcRequest
  socket: WebSocketRoute
}

type WireRequest = {
  key: string
  method: string
  socketIndex: number
}

function response(id: RpcRequest['id'], payload: unknown) {
  return JSON.stringify({ type: 'res', id, ok: true, payload })
}

function eventFrame(event: string, payload: Record<string, unknown>) {
  return JSON.stringify({ type: 'event', event, payload })
}

function workspace(id: string, name: string, path: string) {
  return {
    id,
    name,
    path,
    taskCount: 1,
    pinned: false,
    available: true,
    removed: false,
  }
}

function session(key: string, title: string, workspaceId: string, path: string, updatedAt: number) {
  return {
    key,
    title,
    sessionKind: 'chat',
    surface: 'webchat',
    conversationKind: 'direct',
    effectiveAgentId: 'main',
    updatedAt,
    messageCount: 1,
    status: 'ok',
    runStatus: 'idle',
    workspaceId,
    workspace: path,
  }
}

function subscriptionPayload(key: string) {
  const isA = key === SESSION_A
  return {
    subscribed: true,
    hydration_complete: true,
    replay_complete: true,
    current_stream_seq: isA ? 10 : 20,
    stream_generation: 'workspace-switch-generation',
    run_status: 'idle',
    workspaceId: isA ? WORKSPACE_A : WORKSPACE_B,
    projectWorkspace: isA
      ? workspace(WORKSPACE_A, 'Workspace A', '/fixtures/workspace-a')
      : workspace(WORKSPACE_B, 'Workspace B', '/fixtures/workspace-b'),
  }
}

function snapshotPayload(key: string) {
  return {
    key,
    events: [],
    current_stream_seq: key === SESSION_A ? 10 : 20,
    stream_generation: 'workspace-switch-generation',
    run_status: 'idle',
  }
}

function basePayload(method: string): unknown {
  const payloads: Record<string, unknown> = {
    'agents.list': { agents: [] },
    'commands.list_for_surface': { commands: [] },
    'config.get': {
      squilla_router: { enabled: false, rollout_phase: 'observe', tiers: {} },
      permissions: {},
      skills: {},
    },
    'models.routing.get': { mode: 'direct' },
    'onboarding.status': { audioConfigured: false },
    'sessions.subscribe': { subscribed: true },
    'usage.status': { sessions: [] },
  }
  return payloads[method] ?? {}
}

async function preparePage(page: Page) {
  await page.addInitScript(() => {
    window.localStorage.setItem('opensquilla-locale', 'en')
    const surfaceOk = async () => ({ ok: true })
    // Exercise the release-default prompt-annotation path, not a browser-only
    // feature override. The complete native v3 bridge enables the same eager
    // per-session annotations.list read used by packaged Desktop.
    window.opensquillaDesktop = {
      getOsLocale: async () => 'en',
      isAutoUpdateEnabled: async () => false,
      getGatewayStatus: async () => ({
        url: '', port: 0, owned: true, status: 'ready', logPath: '',
      }),
      revealGatewayLog: async () => false,
      getDesktopSettings: async () => ({
        provider: '', model: '', baseUrl: '', apiKeyConfigured: false,
        searchProvider: '', searchApiKeyEnv: '', searchApiKeyConfigured: false,
        disableNetworkObservability: false,
        gateway: { url: '', port: 0, owned: true, status: 'ready', logPath: '' },
      }),
      saveDesktopSettings: async () => { throw new Error('not used') },
      resetDesktopSettings: async () => ({ ok: true }),
      getOnboardingDefaults: async () => ({}),
      saveOnboarding: async () => ({}),
      cancelOnboarding: async () => ({}),
      retryStartup: async () => ({ ok: true }),
      quitApp: async () => ({}),
      getBootState: async () => ({}),
      onBootStatus: () => () => undefined,
      onBootError: () => () => undefined,
      openArtifact: async () => ({ ok: false }),
      chooseProjectDirectory: async () => null,
      createArtifactPreviewLease: surfaceOk,
      renewArtifactPreviewLease: surfaceOk,
      revokeArtifactPreviewLease: surfaceOk,
      createWorkbenchSurface: surfaceOk,
      setWorkbenchSurfaceRect: surfaceOk,
      activateWorkbenchSurface: surfaceOk,
      destroyWorkbenchSurface: surfaceOk,
      onWorkbenchSurfaceEvent: () => () => undefined,
      getArtifactAnnotationCapabilities: async () => ({
        version: 3, available: true, picker: true, trustedOverlay: true,
      }),
      setArtifactAnnotationMode: surfaceOk,
      showArtifactAnnotationOverlay: surfaceOk,
      closeArtifactAnnotationOverlay: surfaceOk,
      screenshot: async () => ({}),
    } as unknown as OpenSquillaDesktopApi
  })
  await page.route('**/api/approvals', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ pending: [], mode: 'prompt', allowPatterns: [], denyPatterns: [] }),
  }))
}

test('workspace navigation keeps one transport while the target subscription recovers', async ({
  page,
}) => {
  test.setTimeout(30_000)
  await preparePage(page)

  const sockets: WebSocketRoute[] = []
  const held = new Map<string, HeldRequest>()
  const wire: WireRequest[] = []
  const sends: Array<Record<string, unknown>> = []
  let bArrivedBeforeUnsubscribeAck = false

  await page.routeWebSocket(/\/ws$/, socket => {
    const socketIndex = sockets.push(socket) - 1
    socket.send(eventFrame('connect.challenge', {}))
    socket.onMessage(raw => {
      let frame: RpcRequest
      try {
        frame = JSON.parse(String(raw)) as RpcRequest
      } catch {
        return
      }
      if (frame.type === 'ping') {
        socket.send(JSON.stringify({ type: 'pong' }))
        return
      }
      if (frame.type !== 'req') return

      const method = String(frame.method || '')
      const key = String(frame.params?.key || frame.params?.sessionKey || '')
      wire.push({ key, method, socketIndex })

      if (method === 'connect') {
        socket.send(JSON.stringify({
          protocol: 3,
          server: { version: 'e2e', conn_id: 'workspace-switch-conn' },
          policy: { tick_interval_ms: 30_000 },
          features: {
            methods: [
              'sessions.messages.subscribe',
              'sessions.messages.unsubscribe',
              'sessions.messages.snapshot',
              'sessions.messages.hydrate',
              'sessions.routing.get',
              'sessions.routing.set',
              'workspaces.list',
              'artifacts.list',
              'artifacts.prompt_annotations.list',
              'config.patch.safe',
            ],
            events: ['session.event.text_delta'],
          },
          auth: {
            principal: { isOwner: true, authState: 'authenticated' },
            runModePolicy: { allowedRunModes: ['safe', 'full'], defaultRunMode: 'full' },
          },
        }))
        return
      }
      if (method === 'sessions.list') {
        socket.send(response(frame.id, {
          sessions: [
            session(SESSION_B, 'Workspace B task', WORKSPACE_B, '/fixtures/workspace-b', 200),
            session(SESSION_A, 'Workspace A task', WORKSPACE_A, '/fixtures/workspace-a', 100),
          ],
          has_more: false,
        }))
        return
      }
      if (method === 'workspaces.list') {
        socket.send(response(frame.id, {
          workspaces: [
            workspace(WORKSPACE_A, 'Workspace A', '/fixtures/workspace-a'),
            workspace(WORKSPACE_B, 'Workspace B', '/fixtures/workspace-b'),
          ],
        }))
        return
      }
      if (method === 'artifacts.list') {
        socket.send(response(frame.id, { artifacts: [], has_more: false }))
        return
      }
      if (method === 'artifacts.prompt_annotations.list') {
        socket.send(response(frame.id, { annotations: [] }))
        return
      }
      if (method === 'chat.history') {
        socket.send(response(frame.id, {
          messages: [{
            role: 'user',
            text: key === SESSION_A ? 'Workspace A history' : 'Workspace B history',
            message_id: key === SESSION_A ? 'history-a' : 'history-b',
            timestamp: '2026-08-26T00:00:00.000Z',
          }],
          has_more: false,
          canonical_complete: true,
        }))
        return
      }
      if (method === 'sessions.messages.subscribe' && key === SESSION_A) {
        socket.send(response(frame.id, subscriptionPayload(key)))
        return
      }
      if (
        method === 'sessions.messages.subscribe'
        || method === 'sessions.messages.unsubscribe'
      ) {
        held.set(`${method}:${key}`, { frame, socket })
        if (method === 'sessions.messages.subscribe' && key === SESSION_B) {
          const pendingRelease = held.get(
            `sessions.messages.unsubscribe:${SESSION_A}`,
          )
          if (pendingRelease) {
            bArrivedBeforeUnsubscribeAck = true
            pendingRelease.socket.send(response(
              pendingRelease.frame.id,
              { subscribed: false },
            ))
            held.delete(`sessions.messages.unsubscribe:${SESSION_A}`)
          }
        }
        return
      }
      if (method === 'sessions.messages.snapshot') {
        socket.send(response(frame.id, snapshotPayload(key)))
        return
      }
      if (method === 'sessions.messages.hydrate') {
        socket.send(response(frame.id, subscriptionPayload(key)))
        return
      }
      if (method === 'sessions.routing.get') {
        socket.send(response(frame.id, {
          key,
          mode: 'direct',
          revision: 0,
        }))
        return
      }
      if (method === 'chat.send') {
        sends.push({ ...(frame.params || {}) })
        socket.send(response(frame.id, {
          ok: true,
          sessionKey: key,
          status: 'accepted',
          task_id: 'workspace-b-send',
          message_id: 'workspace-b-message',
        }))
        return
      }
      socket.send(response(frame.id, basePayload(method)))
    })
  })

  const release = (method: string, key: string, payload: unknown) => {
    const request = held.get(`${method}:${key}`)
    if (!request) throw new Error(`Missing held request ${method}:${key}`)
    request.socket.send(response(request.frame.id, payload))
    held.delete(`${method}:${key}`)
  }

  await page.goto(`${CONTROL_URL}chat?session=${encodeURIComponent(SESSION_A)}`)
  await expect.poll(() => sockets.length).toBe(1)
  await expect.poll(() => wire.some(entry => (
    entry.method === 'sessions.messages.subscribe' && entry.key === SESSION_A
  ))).toBe(true)
  await expect.poll(() => wire.some(entry => (
    entry.method === 'sessions.messages.snapshot' && entry.key === SESSION_A
  ))).toBe(true)
  await expect(page.locator('.conn-pill.connected')).toBeVisible()

  const workspaceBRow = page.locator(`[data-session-key="${SESSION_B}"]`)
  await expect(workspaceBRow).toBeVisible()
  await workspaceBRow.locator('.sidebar-history-item').click()
  await expect.poll(() => new URL(page.url()).searchParams.get('session')).toBe(SESSION_B)

  await expect.poll(() => held.has(`sessions.messages.subscribe:${SESSION_B}`)).toBe(true)
  expect(bArrivedBeforeUnsubscribeAck).toBe(true)
  await expect.poll(() => wire.some(entry => (
    entry.method === 'sessions.messages.snapshot' && entry.key === SESSION_B
  ))).toBe(true)
  await expect.poll(() => wire.some(entry => (
    entry.method === 'artifacts.list' && entry.key === SESSION_B
  ))).toBe(true)
  await expect.poll(() => wire.some(entry => (
    entry.method === 'artifacts.prompt_annotations.list' && entry.key === SESSION_B
  ))).toBe(true)
  await expect.poll(() => wire.some(entry => (
    entry.method === 'sessions.routing.get' && entry.key === SESSION_B
  ))).toBe(true)
  expect(sockets).toHaveLength(1)

  const bSubscribeIndex = wire.findIndex(entry => (
    entry.method === 'sessions.messages.subscribe' && entry.key === SESSION_B
  ))
  const bSnapshotIndex = wire.findIndex(entry => (
    entry.method === 'sessions.messages.snapshot' && entry.key === SESSION_B
  ))
  for (const optionalMethod of [
    'sessions.routing.get',
    'artifacts.list',
    'artifacts.prompt_annotations.list',
  ]) {
    const optionalIndex = wire.findIndex(entry => (
      entry.method === optionalMethod && entry.key === SESSION_B
    ))
    expect(optionalIndex).toBeGreaterThan(bSubscribeIndex)
    expect(optionalIndex).toBeGreaterThan(bSnapshotIndex)
  }

  const criticalWire = wire
    .filter(entry => [
      'sessions.messages.subscribe',
      'sessions.messages.snapshot',
      'sessions.messages.unsubscribe',
    ].includes(entry.method))
    .map(entry => `${entry.method}:${entry.key}`)
  expect(criticalWire.slice(0, 5)).toEqual([
    `sessions.messages.subscribe:${SESSION_A}`,
    `sessions.messages.snapshot:${SESSION_A}`,
    `sessions.messages.unsubscribe:${SESSION_A}`,
    `sessions.messages.subscribe:${SESSION_B}`,
    `sessions.messages.snapshot:${SESSION_B}`,
  ])
  expect(wire.every(entry => entry.socketIndex === 0)).toBe(true)
  await expect(page.locator('.conn-pill.connected')).toBeVisible()

  const liveRecovery = page.locator(
    '[data-testid="chat-session-recovery-status"][data-recovery-state="live-connecting"]',
  )
  await expect(liveRecovery).toContainText(
    'Gateway connected. Restoring live updates for this session',
  )

  const composer = page.locator('.chat-textarea')
  const sendButton = page.locator('.chat-send-btn.btn--primary')
  await composer.fill(PRESERVED_DRAFT)
  await expect(composer).toHaveValue(PRESERVED_DRAFT)
  await expect(sendButton).toBeDisabled()

  sockets[0]!.send(eventFrame('session.event.text_delta', {
    key: SESSION_A,
    task_id: 'late-workspace-a-task',
    stream_generation: 'workspace-switch-generation',
    stream_seq: 11,
    text: STALE_A_TEXT,
  }))
  await expect(page.getByText(STALE_A_TEXT, { exact: true })).toHaveCount(0)

  await expect(liveRecovery).toBeVisible()
  await expect(composer).toHaveValue(PRESERVED_DRAFT)

  release('sessions.messages.subscribe', SESSION_B, subscriptionPayload(SESSION_B))

  await expect(liveRecovery).toHaveCount(0)
  await expect(page.locator('.conn-pill.connected')).toBeVisible()
  await expect(composer).toHaveValue(PRESERVED_DRAFT)
  await expect(sendButton).toBeEnabled()
  expect(sockets).toHaveLength(1)

  await sendButton.click()
  await expect.poll(() => sends.length).toBe(1)
  expect(sends[0]).toMatchObject({
    message: PRESERVED_DRAFT,
    sessionKey: SESSION_B,
  })
  sockets[0]!.send(eventFrame('session.event.text_delta', {
    key: SESSION_B,
    task_id: 'workspace-b-send',
    stream_generation: 'workspace-switch-generation',
    stream_seq: 21,
    text: FRESH_B_TEXT,
  }))
  await expect(page.getByText(FRESH_B_TEXT, { exact: true })).toBeVisible()
  await expect(page.getByText(STALE_A_TEXT, { exact: true })).toHaveCount(0)
})
