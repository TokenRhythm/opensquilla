import {
  expect,
  test,
  type ConsoleMessage,
  type Page,
  type WebSocketRoute,
} from '@playwright/test'

const CONTROL_URL = '/control/'
const SESSION_KEY = 'agent:main:webchat:e2e-task-ownership'
const TASK_A = 'task-e2e-running-A'
const TASK_B = 'task-e2e-queued-B'
const B_EARLY_TEXT = 'Successor B output remained attached to B.'

type RpcRequest = {
  id?: string | number
  method?: string
  params?: Record<string, unknown>
  type?: string
}

function response(id: string | number | undefined, payload: unknown) {
  return JSON.stringify({ type: 'res', id, ok: true, payload })
}

function event(eventName: string, payload: Record<string, unknown>) {
  return JSON.stringify({ type: 'event', event: eventName, payload })
}

async function preparePage(page: Page) {
  await page.addInitScript(() => localStorage.setItem('opensquilla-locale', 'en'))
  await page.route('**/api/approvals', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ pending: [], mode: 'prompt', allowPatterns: [], denyPatterns: [] }),
  }))
  await page.route('**/control/static/dist/opensquilla-mark.png', route => route.fulfill({
    status: 204,
    contentType: 'image/png',
    body: '',
  }))
}

async function installOwnershipGateway(page: Page) {
  let socket: WebSocketRoute | null = null
  let streamSeq = 0
  const abortCalls: Array<Record<string, unknown>> = []

  const emit = (eventName: string, payload: Record<string, unknown>) => {
    if (!socket) throw new Error('ownership websocket is not connected')
    socket.send(event(eventName, {
      key: SESSION_KEY,
      session_key: SESSION_KEY,
      stream_generation: 'task-ownership-generation',
      stream_seq: ++streamSeq,
      ...payload,
    }))
  }

  await page.routeWebSocket(/\/ws$/, ws => {
    socket = ws
    ws.send(event('connect.challenge', {}))
    ws.onMessage(message => {
      let frame: RpcRequest
      try {
        frame = JSON.parse(String(message)) as RpcRequest
      } catch {
        return
      }
      if (frame.type !== 'req') return
      const method = String(frame.method || '')

      if (method === 'connect') {
        ws.send(JSON.stringify({
          protocol: 3,
          policy: { tick_interval_ms: 30_000 },
          features: {
            methods: [
              'sessions.messages.subscribe',
              'sessions.messages.hydrate',
              'sessions.messages.snapshot',
            ],
            events: [
              'task.queued',
              'task.running',
              'task.cancelled',
              'sessions.changed',
              'session.event.text_delta',
              'session.event.done',
            ],
          },
          auth: {
            principal: { isOwner: true },
            runModePolicy: { allowedRunModes: ['safe', 'full'], defaultRunMode: 'full' },
          },
        }))
        return
      }
      if (method === 'chat.history') {
        ws.send(response(frame.id, { messages: [], has_more: false, canonical_complete: true }))
        return
      }
      if (
        method === 'sessions.messages.subscribe'
        || method === 'sessions.messages.hydrate'
        || method === 'sessions.messages.snapshot'
      ) {
        ws.send(response(frame.id, {
          subscribed: true,
          hydration_complete: true,
          replay_complete: true,
          current_stream_seq: streamSeq,
          stream_generation: 'task-ownership-generation',
          run_status: 'running',
          active_task: { task_id: TASK_A, status: 'running' },
          queued_task_ids: [],
          tasks: [{ task_id: TASK_A, status: 'running' }],
        }))
        return
      }
      if (method === 'chat.abort') {
        abortCalls.push({ ...(frame.params || {}) })
        ws.send(response(frame.id, { aborted: true, key: SESSION_KEY }))
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
        'models.routing.get': { mode: 'direct' },
        'onboarding.status': { audioConfigured: false },
        'sessions.list': {
          sessions: [{
            key: SESSION_KEY,
            title: 'Task ownership release gate',
            sessionKind: 'chat',
            surface: 'webchat',
            conversationKind: 'direct',
            effectiveAgentId: 'main',
            updatedAt: 100,
            messageCount: 0,
            status: 'ok',
            runStatus: 'running',
          }],
          has_more: false,
        },
        'sessions.messages.unsubscribe': { subscribed: false },
        'sessions.subscribe': { subscribed: true },
        'usage.status': { sessions: [] },
      }
      ws.send(response(frame.id, payloads[method] ?? {}))
    })
  })

  return { abortCalls, emit }
}

function collectRendererErrors(page: Page) {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.stack || error.message))
  page.on('console', (message: ConsoleMessage) => {
    if (message.type() === 'error') errors.push(message.text())
  })
  return errors
}

test.describe('P1-A task ownership and exact Stop release gate', () => {
  test('queued B never steals A Stop or A rendering, then takes over after A terminal', async ({
    page,
  }) => {
    const errors = collectRendererErrors(page)
    await preparePage(page)
    const gateway = await installOwnershipGateway(page)

    await page.goto(`${CONTROL_URL}chat?session=${encodeURIComponent(SESSION_KEY)}`)
    await expect(page.locator('.conn-pill.connected')).toBeVisible({ timeout: 10_000 })
    const stop = page.getByRole('button', { name: 'Stop current response' })
    await expect(stop).toBeVisible({ timeout: 10_000 })

    gateway.emit('task.queued', {
      task_id: TASK_B,
      queue_depth: 1,
      queue_position: 1,
    })
    gateway.emit('sessions.changed', {
      reason: 'task_queued',
      run_status: 'running',
      active_task: { task_id: TASK_A, status: 'running' },
      changed_task: { task_id: TASK_B, status: 'queued' },
    })

    await stop.click()
    await expect.poll(() => gateway.abortCalls.length).toBe(1)
    expect(gateway.abortCalls).toEqual([{
      sessionKey: SESSION_KEY,
      taskId: TASK_A,
      source: 'webui_stop',
      scope: 'task',
    }])

    // TaskRuntime can grant B the same-session lane before A's terminal
    // observer finishes. B output must stay buffered under its own identity.
    gateway.emit('task.running', { task_id: TASK_B })
    gateway.emit('session.event.text_delta', { task_id: TASK_B, text: B_EARLY_TEXT })
    await expect(page.getByText(B_EARLY_TEXT, { exact: true })).toHaveCount(0)

    gateway.emit('task.cancelled', {
      task_id: TASK_A,
      status: 'cancelled',
      terminal_reason: 'user_abort',
      terminal_message: 'The response was stopped.',
    })
    gateway.emit('sessions.changed', {
      reason: 'task_terminal',
      run_status: 'running',
      active_task: { task_id: TASK_B, status: 'running' },
      last_task: { task_id: TASK_A, status: 'cancelled' },
    })

    await expect(page.getByText(B_EARLY_TEXT, { exact: true })).toBeVisible({ timeout: 10_000 })
    expect(gateway.abortCalls).toHaveLength(1)

    gateway.emit('session.event.done', {
      task_id: TASK_B,
      status: 'succeeded',
      reason: 'completed',
      text_snapshot: B_EARLY_TEXT,
    })
    await expect(page.locator('.chat-send-btn[aria-label="Send"]')).toBeVisible({
      timeout: 10_000,
    })
    expect(errors, errors.join('\n')).toEqual([])
  })
})
