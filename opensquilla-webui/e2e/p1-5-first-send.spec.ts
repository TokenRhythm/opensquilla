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
  position: number
  requestFingerprint: string
  revision: number
}

type MockGatewayState = {
  chatSends: Array<Record<string, unknown>>
  dispatchMessages: string[]
  dispatchCount: number
  enqueueCount: number
  firstFinished: boolean
  firstSessionKey: string
  handoffTargets: Record<string, string>
  pendingRows: PendingRow[]
  reorderCount: number
  supportsPendingQueue: boolean
}

type MockGateway = {
  chatSends: Array<Record<string, unknown>>
  dispatchMessages: string[]
  dispatchCount: number
  enqueueCount: number
  finishFirst: () => void
  pendingRow: () => PendingRow | null
  pendingRows: () => PendingRow[]
  reorderCount: number
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

function hello(supportsPendingQueue = true) {
  const pendingMethods = supportsPendingQueue
    ? [
        'sessions.pending_inputs.enqueue',
        'sessions.pending_inputs.list',
        'sessions.pending_inputs.dispatch',
        'sessions.pending_inputs.cancel',
        'sessions.pending_inputs.reorder',
      ]
    : []
  return JSON.stringify({
    protocol: 3,
    policy: { tick_interval_ms: 30_000, concurrent_history_reads: true },
    features: {
      methods: [
        'sessions.messages.subscribe',
        'sessions.messages.snapshot',
        'sessions.messages.hydrate',
        ...pendingMethods,
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

function createMockGatewayState(): MockGatewayState {
  return {
    chatSends: [],
    dispatchMessages: [],
    dispatchCount: 0,
    enqueueCount: 0,
    firstFinished: false,
    firstSessionKey: '',
    handoffTargets: {},
    pendingRows: [],
    reorderCount: 0,
    supportsPendingQueue: true,
  }
}

async function installMockGateway(
  page: Page,
  scenario: Scenario,
  state: MockGatewayState = createMockGatewayState(),
): Promise<MockGateway> {
  const sockets = new Set<WebSocketRoute>()
  let firstAck: (() => void) | null = null
  let firstTaskId = 'p1-5-first-task'
  let streamSeq = 0

  const emit = (event: string, payload: Record<string, unknown>) => {
    for (const socket of sockets) socket.send(eventFrame(event, payload))
  }

  const sendDone = (taskId: string) => emit('session.event.done', {
    key: state.firstSessionKey,
    sessionKey: state.firstSessionKey,
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
        ws.send(hello(state.supportsPendingQueue))
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
        const running = Boolean(state.firstSessionKey && !state.firstFinished)
        ws.send(successResponse(frame.id, {
          key: String(frame.params?.key || ''),
          events: [],
          current_stream_seq: streamSeq,
          stream_generation: 'p1-5-generation',
          run_status: running ? 'running' : 'idle',
          active_task: running ? { task_id: firstTaskId, state: 'running' } : null,
        }))
        return
      }
      if (method === 'sessions.messages.subscribe' || method === 'sessions.messages.hydrate') {
        const running = Boolean(state.firstSessionKey && !state.firstFinished)
        ws.send(successResponse(frame.id, {
          subscribed: true,
          hydration_complete: true,
          replay_complete: true,
          current_stream_seq: streamSeq,
          stream_generation: 'p1-5-generation',
          workspaceId: null,
          run_status: running ? 'running' : 'idle',
          active_task: running ? { task_id: firstTaskId, state: 'running' } : null,
        }))
        return
      }
      if (method === 'sessions.pending_inputs.list') {
        ws.send(successResponse(frame.id, {
          items: state.pendingRows
            .slice()
            .sort((left, right) => left.position - right.position)
            .map(row => ({ ...row, status: 'staged' })),
        }))
        return
      }
      if (method === 'sessions.pending_inputs.enqueue') {
        state.enqueueCount += 1
        const params = frame.params || {}
        const pendingInputId = String(params.pendingInputId || '')
        let row = state.pendingRows.find(item => item.pendingInputId === pendingInputId)
        row ||= {
          pendingInputId: String(params.pendingInputId || ''),
          clientRequestId: String(params.clientRequestId || ''),
          clientMessageId: String(params.clientMessageId || ''),
          requestFingerprint: `fingerprint:${String(params.pendingInputId || '')}`,
          message: String(params.message || ''),
          position: Number.isSafeInteger(params.position)
            ? Number(params.position)
            : state.pendingRows.length,
          revision: 1,
        }
        if (!state.pendingRows.includes(row)) state.pendingRows.push(row)
        ws.send(successResponse(frame.id, { ...row, status: 'staged' }))
        return
      }
      if (method === 'sessions.pending_inputs.reorder') {
        state.reorderCount += 1
        const requested = Array.isArray(frame.params?.items) ? frame.params.items : []
        const byId = new Map(state.pendingRows.map(row => [row.pendingInputId, row]))
        state.pendingRows = requested.map((item, position) => {
          const raw = item as Record<string, unknown>
          const row = byId.get(String(raw.pendingInputId || ''))!
          row.position = position
          row.revision += 1
          return row
        })
        ws.send(successResponse(frame.id, {
          status: 'reordered',
          items: state.pendingRows.map(row => ({ ...row, status: 'staged' })),
        }))
        return
      }
      if (method === 'sessions.pending_inputs.dispatch') {
        state.dispatchCount += 1
        const pendingInputId = String(frame.params?.pendingInputId || '')
        const rowIndex = state.pendingRows.findIndex(row => row.pendingInputId === pendingInputId)
        const [committed] = rowIndex >= 0 ? state.pendingRows.splice(rowIndex, 1) : []
        if (committed) state.dispatchMessages.push(committed.message)
        const queuedTaskId = `p1-5-queued-task-${state.dispatchCount}`
        ws.send(successResponse(frame.id, {
          accepted: true,
          replayed: !committed,
          sessionKey: state.firstSessionKey,
          task_id: queuedTaskId,
          message_id: committed?.clientMessageId,
        }))
        queueMicrotask(() => sendDone(queuedTaskId))
        return
      }
      if (method === 'sessions.pending_inputs.cancel') {
        const pendingInputId = String(frame.params?.pendingInputId || '')
        state.pendingRows = state.pendingRows.filter(row => row.pendingInputId !== pendingInputId)
        ws.send(successResponse(frame.id, { cancelled: true }))
        return
      }
      if (method === 'chat.send') {
        const params = { ...(frame.params || {}) }
        state.chatSends.push(params)
        const ordinal = state.chatSends.length
        const sessionKey = String(params.sessionKey || '')
        const responseSessionKey = state.handoffTargets[String(params.clientRequestId || '')]
          || sessionKey
        if (ordinal === 1) state.firstSessionKey = responseSessionKey
        const taskId = ordinal === 1 ? firstTaskId : `p1-5-follow-up-${ordinal}`
        const acknowledge = () => {
          ws.send(successResponse(frame.id, {
            sessionKey: responseSessionKey,
            task_id: taskId,
            status: 'accepted',
          }))
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
        queueMicrotask(() => {
          if (taskId === firstTaskId) state.firstFinished = true
          sendDone(taskId)
        })
        return
      }

      ws.send(successResponse(frame.id, basePayload(method)))
    })
  })

  return {
    chatSends: state.chatSends,
    dispatchMessages: state.dispatchMessages,
    get dispatchCount() { return state.dispatchCount },
    get enqueueCount() { return state.enqueueCount },
    finishFirst() {
      state.firstFinished = true
      sendDone(firstTaskId)
    },
    pendingRow: () => state.pendingRows[0] || null,
    pendingRows: () => state.pendingRows.slice(),
    get reorderCount() { return state.reorderCount },
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
    const request = indexedDB.open('opensquilla-chat-pending-inputs')
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

async function seedDurableHandoff(
  page: Page,
  input: {
    ownerRequestId: string
    parentSessionKey: string
    clientMessageId: string
    followups: string[]
  },
) {
  await page.evaluate(async seed => {
    const open = indexedDB.open('opensquilla-chat-pending-inputs', 2)
    const database = await new Promise<IDBDatabase>((resolve, reject) => {
      open.onupgradeneeded = () => {
        const db = open.result
        if (!db.objectStoreNames.contains('pending_chat_inputs')) {
          const store = db.createObjectStore('pending_chat_inputs', { keyPath: 'pendingInputId' })
          store.createIndex('session_created', ['sessionKey', 'createdAt'], { unique: false })
        }
        if (!db.objectStoreNames.contains('response_handoffs')) {
          db.createObjectStore('response_handoffs', { keyPath: 'ownerRequestId' })
        }
      }
      open.onsuccess = () => resolve(open.result)
      open.onerror = () => reject(open.error)
    })
    try {
      const transaction = database.transaction(
        ['pending_chat_inputs', 'response_handoffs'],
        'readwrite',
      )
      const now = Date.now()
      transaction.objectStore('response_handoffs').put({
        schemaVersion: 1,
        ownerRequestId: seed.ownerRequestId,
        requestSessionKey: seed.parentSessionKey,
        clientRequestId: seed.ownerRequestId,
        clientMessageId: seed.clientMessageId,
        params: {
          clientRequestId: seed.ownerRequestId,
          clientMessageId: seed.clientMessageId,
          message: 'P1-5 durable fork prompt',
          queueMode: 'followup',
          sessionKey: seed.parentSessionKey,
          forkBeforeMessageId: 'synthetic-parent-message',
          _source: { channel: 'webui' },
        },
        composerText: 'P1-5 durable fork prompt',
        recoveryAttachments: [],
        state: 'submitting',
        createdAt: now,
        updatedAt: now,
      })
      seed.followups.forEach((message, position) => {
        const pendingInputId = `pending-handoff-${position}`
        transaction.objectStore('pending_chat_inputs').put({
          schemaVersion: 1,
          pendingInputId,
          sessionKey: seed.parentSessionKey,
          clientRequestId: `request-handoff-${position}`,
          clientMessageId: `message-handoff-${position}`,
          text: message,
          attachments: [],
          intent: null,
          ownerRequestId: seed.ownerRequestId,
          state: 'saving',
          mayHaveServerCopy: false,
          position,
          walRevision: 1,
          createdAt: now + position,
          updatedAt: now + position,
        })
      })
      await new Promise<void>((resolve, reject) => {
        transaction.oncomplete = () => resolve()
        transaction.onerror = () => reject(transaction.error)
        transaction.onabort = () => reject(transaction.error)
      })
    } finally {
      database.close()
    }
  }, input)
}

async function pendingCardOrder(page: Page): Promise<string[]> {
  return page.locator('.chat-pending-card .chat-pending-text').allTextContents()
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

test.describe('durable handoff and pending order release gate', () => {
  test.describe.configure({ mode: 'serial' })

  test('refresh replays a fork receipt and moves owner follow-ups exactly once', async ({ page }) => {
    test.setTimeout(45_000)
    const errors = collectRendererErrors(page)
    await preparePage(page)
    const state = createMockGatewayState()
    const parentSessionKey = 'agent:main:webchat:handoff-parent'
    const childSessionKey = 'agent:main:webchat:handoff-child'
    const ownerRequestId = 'request-durable-handoff'
    state.handoffTargets[ownerRequestId] = childSessionKey
    const gateway = await installMockGateway(page, 'immediate', state)

    await page.goto(`${CONTROL_URL}chat?session=${encodeURIComponent(parentSessionKey)}`)
    await expect(page.locator('.conn-pill.connected')).toBeVisible({ timeout: 10_000 })
    await seedDurableHandoff(page, {
      ownerRequestId,
      parentSessionKey,
      clientMessageId: 'message-durable-handoff',
      followups: ['handoff follow-up A', 'handoff follow-up B'],
    })

    await page.reload()
    await expect(page).toHaveURL(url => url.searchParams.get('session') === childSessionKey)
    await expect.poll(() => gateway.chatSends.length).toBe(1)
    expect(gateway.chatSends[0]).toMatchObject({
      clientRequestId: ownerRequestId,
      clientMessageId: 'message-durable-handoff',
      sessionKey: parentSessionKey,
      forkBeforeMessageId: 'synthetic-parent-message',
    })
    await expect.poll(() => gateway.enqueueCount).toBe(2)
    // The fork task remains the delivery barrier. Complete it only after the
    // refreshed page has adopted the child and staged every owner follow-up.
    gateway.finishFirst()
    await expect.poll(() => gateway.dispatchMessages, { timeout: 15_000 }).toEqual([
      'handoff follow-up A',
      'handoff follow-up B',
    ])
    await expect.poll(() => gateway.pendingRows()).toEqual([])
    await expect.poll(() => page.evaluate(async () => {
      const request = indexedDB.open('opensquilla-chat-pending-inputs')
      const database = await new Promise<IDBDatabase>((resolve, reject) => {
        request.onsuccess = () => resolve(request.result)
        request.onerror = () => reject(request.error)
      })
      try {
        const transaction = database.transaction('response_handoffs', 'readonly')
        const rows = await new Promise<unknown[]>((resolve, reject) => {
          const all = transaction.objectStore('response_handoffs').getAll()
          all.onsuccess = () => resolve(all.result)
          all.onerror = () => reject(all.error)
        })
        return rows.length
      } finally {
        database.close()
      }
    })).toBe(0)

    const allErrors = [...errors.pageErrors, ...errors.consoleErrors]
    expect(allErrors, allErrors.join('\n')).toEqual([])
  })

  test('server reorder survives route refresh, reconnect, and a peer tab', async ({
    page,
    context,
  }) => {
    test.setTimeout(60_000)
    await preparePage(page)
    const state = createMockGatewayState()
    const gateway = await installMockGateway(page, 'delayed', state)

    await page.goto(CONTROL_URL)
    await expect(page.locator('.conn-pill.connected')).toBeVisible({ timeout: 10_000 })
    await page.locator('.sidebar-new-session').click()
    const composer = page.locator('.chat-textarea')
    await composer.fill('keep task active for durable reorder')
    await page.locator('.chat-send-btn[aria-label="Send"]').click()
    await expect.poll(() => gateway.chatSends.length).toBe(1)

    for (const message of ['queue A', 'queue B', 'queue C']) {
      await composer.fill(message)
      await composer.press('Enter')
    }
    await expect.poll(() => gateway.enqueueCount).toBe(3)
    await expect.poll(() => pendingCardOrder(page)).toEqual(['queue A', 'queue B', 'queue C'])

    const queueC = page.locator('.chat-pending-card').filter({ hasText: 'queue C' })
    await queueC.press('Alt+ArrowUp')
    await expect.poll(() => gateway.reorderCount).toBe(1)
    await expect.poll(() => pendingCardOrder(page)).toEqual(['queue A', 'queue C', 'queue B'])
    await expect(queueC).toHaveAttribute('tabindex', '0')
    await queueC.press('Alt+ArrowUp')
    await expect.poll(() => gateway.reorderCount).toBe(2)
    await expect.poll(() => pendingCardOrder(page)).toEqual(['queue C', 'queue A', 'queue B'])

    gateway.releaseFirstAck()
    await expect(page).toHaveURL(/\/chat\?session=/)
    const materializedUrl = page.url()
    await page.reload()
    await expect(page.locator('.conn-pill.connected')).toBeVisible({ timeout: 10_000 })
    await expect.poll(() => pendingCardOrder(page)).toEqual(['queue C', 'queue A', 'queue B'])

    const peer = await context.newPage()
    await preparePage(peer)
    await installMockGateway(peer, 'immediate', state)
    await peer.goto(materializedUrl)
    await expect(peer.locator('.conn-pill.connected')).toBeVisible({ timeout: 10_000 })
    await expect.poll(() => pendingCardOrder(peer)).toEqual(['queue C', 'queue A', 'queue B'])
    await peer.close()

    gateway.finishFirst()
    await expect.poll(() => gateway.dispatchMessages, { timeout: 15_000 }).toEqual([
      'queue C',
      'queue A',
      'queue B',
    ])
    await expect.poll(() => gateway.pendingRows()).toEqual([])
  })

  test('IndexedDB-only reorder survives refresh against an older Gateway', async ({ page }) => {
    test.setTimeout(45_000)
    await preparePage(page)
    const state = createMockGatewayState()
    state.supportsPendingQueue = false
    const gateway = await installMockGateway(page, 'delayed', state)

    await page.goto(CONTROL_URL)
    await expect(page.locator('.conn-pill.connected')).toBeVisible({ timeout: 10_000 })
    await page.locator('.sidebar-new-session').click()
    const composer = page.locator('.chat-textarea')
    await composer.fill('keep old Gateway task active')
    await page.locator('.chat-send-btn[aria-label="Send"]').click()
    await expect.poll(() => gateway.chatSends.length).toBe(1)

    for (const message of ['local A', 'local B', 'local C']) {
      await composer.fill(message)
      await composer.press('Enter')
    }
    await expect.poll(() => pendingCardOrder(page)).toEqual(['local A', 'local B', 'local C'])
    const localC = page.locator('.chat-pending-card').filter({ hasText: 'local C' })
    await expect(localC).toHaveAttribute('aria-keyshortcuts', /Alt\+ArrowUp/)
    await localC.press('Alt+ArrowUp')
    await expect.poll(() => pendingCardOrder(page)).toEqual(['local A', 'local C', 'local B'])
    await expect(localC).toHaveAttribute('tabindex', '0')
    await localC.press('Alt+ArrowUp')
    await expect.poll(() => pendingCardOrder(page)).toEqual(['local C', 'local A', 'local B'])
    // The preview order changes synchronously, while IndexedDB commits it
    // behind a delivery barrier. Wait for keyboard reordering to return before
    // reloading so this test exercises the durable order, not an in-flight
    // optimistic preview.
    await expect(localC).toHaveAttribute('aria-keyshortcuts', /Alt\+ArrowUp/)
    expect(gateway.reorderCount).toBe(0)

    gateway.releaseFirstAck()
    await expect(page).toHaveURL(/\/chat\?session=/)
    await page.reload()
    await expect(page.locator('.conn-pill.connected')).toBeVisible({ timeout: 10_000 })
    await expect.poll(() => pendingCardOrder(page)).toEqual(['local C', 'local A', 'local B'])

    gateway.finishFirst()
    await expect.poll(() => gateway.chatSends.map(send => String(send.message || '')), {
      timeout: 15_000,
    }).toEqual([
      'keep old Gateway task active',
      'local C',
      'local A',
      'local B',
    ])
  })
})
