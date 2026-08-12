import {
  expect,
  test,
  type ConsoleMessage,
  type Page,
  type WebSocketRoute,
} from '@playwright/test'

const CONTROL_URL = '/control/'
const RELEASE_ITERATIONS = Number(process.env.OPENSQUILLA_P1_5_ITERATIONS || '1')
const FIRST_TEXT = 'P1-5 deterministic first send'
const SECOND_TEXT = 'P1-5 deterministic follow-up'
const FATAL_RENDERER_PATTERN = /(?:emitsOptions|exposed|nextSibling|getNextHostNode|Teleport\.process)/

type Scenario = 'immediate' | 'delayed' | 'event-before-ack' | 'reconnect' | 'queued-wal'
type RpcRequest = {
  id?: string | number
  method?: string
  params?: Record<string, unknown>
  type?: string
}

type PendingRow = {
  clientMessageId: string
  clientRequestId: string
  message: string
  pendingInputId: string
  requestFingerprint: string
}

type MockGateway = {
  chatSends: Array<Record<string, unknown>>
  dispatchCount: number
  enqueueCount: number
  finishFirst: () => void
  pendingRow: () => PendingRow | null
  releaseFirstAck: () => void
}

function successResponse(id: string | number | undefined, payload: unknown) {
  return JSON.stringify({ type: 'res', id, ok: true, payload })
}

function eventFrame(event: string, payload: Record<string, unknown>) {
  return JSON.stringify({ type: 'event', event, payload })
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
    'sessions.list': { sessions: [], has_more: false },
    'sessions.messages.unsubscribe': { subscribed: false },
    'sessions.subscribe': { subscribed: true },
    'usage.status': { sessions: [] },
  }
  return payloads[method] ?? {}
}

function hello() {
  return JSON.stringify({
    protocol: 3,
    policy: { tick_interval_ms: 30_000, concurrent_history_reads: true },
    features: {
      methods: [
        'sessions.messages.subscribe',
        'sessions.messages.snapshot',
        'sessions.messages.hydrate',
        'sessions.pending_inputs.enqueue',
        'sessions.pending_inputs.list',
        'sessions.pending_inputs.dispatch',
        'sessions.pending_inputs.cancel',
      ],
      events: [
        'session.event.provider_activity',
        'session.event.text_delta',
        'session.event.done',
      ],
    },
    auth: {
      principal: { isOwner: true },
      runModePolicy: { allowedRunModes: ['safe', 'full'], defaultRunMode: 'full' },
    },
  })
}

async function preparePage(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem('opensquilla-locale', 'en')
  })
  await page.route('**/api/approvals', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ pending: [], mode: 'prompt', allowPatterns: [], denyPatterns: [] }),
  }))
  // `vite preview` owns only the built frontend. The packaged Gateway normally
  // serves this backend-owned brand asset from static/img; keep the standalone
  // production-bundle fixture console-clean without starting a second server.
  await page.route('**/control/static/dist/opensquilla-mark.png', route => route.fulfill({
    status: 204,
    contentType: 'image/png',
    body: '',
  }))
}

async function installMockGateway(page: Page, scenario: Scenario): Promise<MockGateway> {
  const sockets = new Set<WebSocketRoute>()
  const chatSends: Array<Record<string, unknown>> = []
  let firstAck: (() => void) | null = null
  let firstSessionKey = ''
  let firstTaskId = 'p1-5-first-task'
  let streamSeq = 0
  let dispatchCount = 0
  let enqueueCount = 0
  let row: PendingRow | null = null

  const emit = (event: string, payload: Record<string, unknown>) => {
    for (const socket of sockets) socket.send(eventFrame(event, payload))
  }

  const sendDone = (taskId: string) => emit('session.event.done', {
    key: firstSessionKey,
    sessionKey: firstSessionKey,
    task_id: taskId,
    stream_generation: 'p1-5-generation',
    stream_seq: ++streamSeq,
    status: 'succeeded',
    reason: 'completed',
    text_snapshot: 'ok',
  })

  await page.routeWebSocket(/\/ws$/, ws => {
    sockets.add(ws)
    ws.onClose(() => sockets.delete(ws))
    ws.send(eventFrame('connect.challenge', {}))
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
        ws.send(hello())
        return
      }
      if (method === 'chat.history') {
        ws.send(successResponse(frame.id, {
          messages: [],
          has_more: false,
          canonical_complete: true,
        }))
        return
      }
      if (method === 'sessions.messages.snapshot') {
        ws.send(successResponse(frame.id, {
          key: String(frame.params?.key || ''),
          events: [],
          current_stream_seq: streamSeq,
          stream_generation: 'p1-5-generation',
          run_status: 'idle',
        }))
        return
      }
      if (method === 'sessions.messages.subscribe' || method === 'sessions.messages.hydrate') {
        ws.send(successResponse(frame.id, {
          subscribed: true,
          hydration_complete: true,
          replay_complete: true,
          current_stream_seq: streamSeq,
          stream_generation: 'p1-5-generation',
          workspaceId: null,
          run_status: 'idle',
          active_task: null,
        }))
        return
      }
      if (method === 'sessions.pending_inputs.list') {
        ws.send(successResponse(frame.id, { items: row ? [{ ...row, status: 'staged' }] : [] }))
        return
      }
      if (method === 'sessions.pending_inputs.enqueue') {
        enqueueCount += 1
        const params = frame.params || {}
        row ||= {
          pendingInputId: String(params.pendingInputId || ''),
          clientRequestId: String(params.clientRequestId || ''),
          clientMessageId: String(params.clientMessageId || ''),
          requestFingerprint: `fingerprint:${String(params.pendingInputId || '')}`,
          message: String(params.message || ''),
        }
        ws.send(successResponse(frame.id, { ...row, status: 'staged' }))
        return
      }
      if (method === 'sessions.pending_inputs.dispatch') {
        dispatchCount += 1
        const committed = row
        row = null
        ws.send(successResponse(frame.id, {
          accepted: true,
          replayed: dispatchCount > 1,
          sessionKey: firstSessionKey,
          task_id: 'p1-5-queued-task',
          message_id: committed?.clientMessageId,
        }))
        queueMicrotask(() => sendDone('p1-5-queued-task'))
        return
      }
      if (method === 'sessions.pending_inputs.cancel') {
        row = null
        ws.send(successResponse(frame.id, { cancelled: true }))
        return
      }
      if (method === 'chat.send') {
        const params = { ...(frame.params || {}) }
        chatSends.push(params)
        const ordinal = chatSends.length
        const sessionKey = String(params.sessionKey || '')
        if (ordinal === 1) firstSessionKey = sessionKey
        const taskId = ordinal === 1 ? firstTaskId : `p1-5-follow-up-${ordinal}`
        const acknowledge = () => {
          ws.send(successResponse(frame.id, { sessionKey, task_id: taskId, status: 'accepted' }))
        }

        if (ordinal === 1 && scenario !== 'immediate') {
          firstAck = acknowledge
          if (scenario === 'event-before-ack') {
            emit('session.event.provider_activity', {
              key: sessionKey,
              task_id: taskId,
              stream_generation: 'p1-5-generation',
              stream_seq: ++streamSeq,
              schema_version: 1,
              activity_id: 'p1-5-activity',
              phase: 'reasoning',
              reason: 'reasoning_only',
              retry_attempt: 0,
              retry_limit: 0,
              retry_after_ms: 0,
              started_at: Date.now(),
              heartbeat: false,
            })
            emit('session.event.text_delta', {
              key: sessionKey,
              task_id: taskId,
              stream_generation: 'p1-5-generation',
              stream_seq: ++streamSeq,
              text: 'event before durable acknowledgement',
            })
          }
          return
        }

        acknowledge()
        queueMicrotask(() => sendDone(taskId))
        return
      }

      ws.send(successResponse(frame.id, basePayload(method)))
    })
  })

  return {
    chatSends,
    get dispatchCount() { return dispatchCount },
    get enqueueCount() { return enqueueCount },
    finishFirst() { sendDone(firstTaskId) },
    pendingRow: () => row,
    releaseFirstAck() {
      const release = firstAck
      if (!release) throw new Error('first chat.send acknowledgement is not pending')
      firstAck = null
      release()
      if (scenario === 'reconnect') {
        for (const socket of sockets) {
          setTimeout(() => void socket.close({ code: 1012, reason: 'P1-5 ack reconnect' }), 10)
        }
      }
    },
  }
}

function collectRendererErrors(page: Page) {
  const pageErrors: string[] = []
  const consoleErrors: string[] = []
  page.on('pageerror', error => pageErrors.push(error.stack || error.message))
  page.on('console', (message: ConsoleMessage) => {
    if (message.type() === 'error') {
      const source = message.location().url
      consoleErrors.push(source ? `${message.text()} (${source})` : message.text())
    }
  })
  return { pageErrors, consoleErrors }
}

async function expectSingletonChat(page: Page) {
  await expect(page.getByTestId('route-header-host')).toHaveCount(1)
  await expect(page.locator('.chat')).toHaveCount(1)
  await expect(page.locator('.chat-textarea')).toHaveCount(1)
  await expect(page.getByTestId('chat-header-actions')).toHaveCount(1)
}

async function expectWalContains(page: Page, text: string) {
  await expect.poll(() => page.evaluate(async expectedText => {
    const request = indexedDB.open('opensquilla-chat-pending-inputs', 1)
    const database = await new Promise<IDBDatabase>((resolve, reject) => {
      request.onsuccess = () => resolve(request.result)
      request.onerror = () => reject(request.error)
    })
    try {
      if (!database.objectStoreNames.contains('pending_chat_inputs')) return false
      const transaction = database.transaction('pending_chat_inputs', 'readonly')
      const rows = await new Promise<Array<{ message?: string; text?: string }>>((resolve, reject) => {
        const all = transaction.objectStore('pending_chat_inputs').getAll()
        all.onsuccess = () => resolve(all.result)
        all.onerror = () => reject(all.error)
      })
      return rows.some(row => (row.message || row.text) === expectedText)
    } finally {
      database.close()
    }
  }, text)).toBe(true)
}

async function runFirstSendIteration(page: Page, scenario: Scenario, iteration: number) {
  const errors = collectRendererErrors(page)
  await preparePage(page)
  const gateway = await installMockGateway(page, scenario)

  // Enter through the deployment root, then use the product's own draft
  // navigation. The release bundle deliberately uses relative asset URLs;
  // loading a deep route directly would test the preview server rather than
  // the Gateway's /control fallback behavior.
  await page.goto(CONTROL_URL)
  await expect(page.locator('.conn-pill.connected')).toBeVisible({ timeout: 10_000 })
  if ((page.viewportSize()?.width || 0) < 600) {
    await page.getByTestId('sidebar-toggle-collapsed').click()
  }
  await page.locator('.sidebar-new-session').click()
  await expect(page).toHaveURL(/\/chat\/new(?:\?|$)/)
  await expectSingletonChat(page)
  const header = page.getByTestId('chat-header-actions')
  await expect(header).toBeHidden()
  await header.evaluate(element => { element.setAttribute('data-p1-5-identity', 'stable') })

  const composer = page.locator('.chat-textarea')
  await composer.fill(`${FIRST_TEXT} ${iteration}`)
  await page.locator('.chat-send-btn[aria-label="Send"]').click()
  await expect.poll(() => gateway.chatSends.length).toBe(1)
  // A synchronous ACK is allowed to materialize immediately. Every held-ACK
  // row must prove that optimistic UI does not consume the draft route early.
  if (scenario !== 'immediate') await expect(page).toHaveURL(/\/chat\/new/)
  await expect(page.locator('.msg-user').filter({ hasText: FIRST_TEXT })).toBeVisible()
  await expect(header).toBeVisible()
  await expectSingletonChat(page)

  if (scenario === 'queued-wal') {
    await composer.fill(`${SECOND_TEXT} ${iteration}`)
    await composer.press('Enter')
    await expect.poll(() => gateway.enqueueCount).toBe(1)
    await expect(page.locator('.chat-pending-card').filter({ hasText: SECOND_TEXT })).toBeVisible()
    await expectWalContains(page, `${SECOND_TEXT} ${iteration}`)
  }

  if (scenario === 'delayed') await page.waitForTimeout(2_000)
  if (scenario !== 'immediate') gateway.releaseFirstAck()

  await expect(page).toHaveURL(/\/chat\?session=agent(?::|%3A)main(?::|%3A)webchat(?::|%3A)/)
  await expect(header).toHaveAttribute('data-p1-5-identity', 'stable')
  await expectSingletonChat(page)

  if (scenario === 'reconnect') {
    await expect(page.locator('.conn-pill.connected')).toBeVisible({ timeout: 10_000 })
  }
  gateway.finishFirst()

  if (scenario === 'queued-wal') {
    await expect.poll(() => gateway.dispatchCount, { timeout: 10_000 }).toBe(1)
    await expect.poll(() => gateway.pendingRow()).toBeNull()
    await expect(page.locator('.chat-pending-card').filter({ hasText: SECOND_TEXT })).toHaveCount(0)
    expect(gateway.chatSends).toHaveLength(1)
  } else {
    await expect(page.locator('.chat-send-btn[aria-label="Send"]')).toBeVisible({ timeout: 10_000 })
    await composer.fill(`${SECOND_TEXT} ${iteration}`)
    await page.locator('.chat-send-btn[aria-label="Send"]').click()
    await expect.poll(() => gateway.chatSends.length).toBe(2)
    expect(gateway.chatSends.filter(send => String(send.message || '') === `${SECOND_TEXT} ${iteration}`))
      .toHaveLength(1)
  }

  await expectSingletonChat(page)
  const allErrors = [...errors.pageErrors, ...errors.consoleErrors]
  expect(allErrors, allErrors.join('\n')).toEqual([])
  expect(allErrors.some(message => FATAL_RENDERER_PATTERN.test(message))).toBe(false)
}

test.describe('P1-5 first-send renderer release gate', () => {
  test.describe.configure({ mode: 'serial' })

  for (const viewport of [
    { name: 'wide', width: 1440, height: 900 },
    { name: 'tight', width: 390, height: 844 },
  ]) {
    for (const scenario of [
      'immediate',
      'delayed',
      'event-before-ack',
      'reconnect',
      'queued-wal',
    ] as const) {
      test(`${viewport.name}: ${scenario}`, async ({ page }) => {
        test.setTimeout(Math.max(30_000, RELEASE_ITERATIONS * 15_000))
        await page.setViewportSize(viewport)
        for (let iteration = 1; iteration <= RELEASE_ITERATIONS; iteration += 1) {
          await runFirstSendIteration(page, scenario, iteration)
        }
      })
    }
  }
})
