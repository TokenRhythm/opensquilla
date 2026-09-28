import {
  expect,
  test,
  type ConsoleMessage,
  type Page,
  type WebSocketRoute,
} from '@playwright/test'
import { helloOkResponse } from './support/gateway-fixture'
import {
  chatHistoryPayload,
  sessionMessagesHydratePayload,
  sessionMessagesSnapshotPayload,
  sessionMessagesSubscribePayload,
} from './support/session-read-fixtures'

const CONTROL_URL = '/control/'
const RELEASE_ITERATIONS = Number(process.env.OPENSQUILLA_P1_5_ITERATIONS || '1')
const FIRST_TEXT = 'P1-5 deterministic first send'
const SECOND_TEXT = 'P1-5 deterministic follow-up'
const FATAL_RENDERER_PATTERN = /(?:emitsOptions|exposed|nextSibling|getNextHostNode|Teleport\.process)/
const DELIVERY_PRINCIPAL = {
  role: 'operator',
  authenticated: true,
  isOwner: true,
  authState: 'authenticated',
  scopes: ['operator.admin', 'operator.read', 'operator.write'],
  capabilities: [],
  tokenPublicId: 'synthetic_p1_5_owner',
  guestOwnerId: null,
}

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

type CommittedTurn = {
  taskId: string
  sessionKey: string
  messageId: string
  message: string
  createdAt: number
  transcriptId: number
  finished: boolean
  terminalStatus?: 'succeeded' | 'cancelled'
}

type MockGatewayState = {
  chatSends: Array<Record<string, unknown>>
  receiptQueries: Array<Record<string, unknown>>
  dispatchMessages: string[]
  dispatchCount: number
  enqueueCount: number
  firstSessionKey: string
  handoffTargets: Record<string, string>
  pendingRows: PendingRow[]
  reorderCount: number
  supportsPendingQueue: boolean
  turns: CommittedTurn[]
  streamSeq: number
  liveEvents: Array<{ event: string; payload: Record<string, unknown> }>
}

type MockGateway = {
  chatSends: Array<Record<string, unknown>>
  receiptQueries: Array<Record<string, unknown>>
  connectionCount: number
  subscribedConnection: number
  dispatchMessages: string[]
  dispatchCount: number
  enqueueCount: number
  finishFirst: () => void
  pendingRow: () => PendingRow | null
  pendingRows: () => PendingRow[]
  reorderCount: number
  releaseFirstAck: () => void
  aborts: Array<Record<string, unknown>>
  socketCount: number
  waitingHandshakeCount: number
  receiptInFlight: number
  peakReceiptInFlight: number
  dropFirstAck: () => void
  holdConnections: () => void
  releaseConnections: () => void
  holdReceiptReplies: () => void
  releaseReceiptReplies: () => void
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
    'sandbox.run_mode.preference.get': { runMode: 'full', source: 'config' },
    'sessions.list': { sessions: [], count: 0, ts: 1_800_000_000, has_more: false },
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
  return helloOkResponse({
    protocol: 4,
    policy: { concurrent_history_reads: true },
    features: {
      methods: [
        'sessions.messages.subscribe',
        'sessions.messages.snapshot',
        'sessions.messages.hydrate',
        'turns.receipt.get',
        ...pendingMethods,
      ],
      events: [
        'session.event.provider_activity',
        'session.event.text_delta',
        'session.event.done',
      ],
    },
    auth: {
      principal: DELIVERY_PRINCIPAL,
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
  await page.route('**/api/system/update', route => route.fulfill({ json: {} }))
  await page.route('**/api/elevated-mode', route => route.fulfill({ json: { enabled: false } }))
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
    receiptQueries: [],
    dispatchMessages: [],
    dispatchCount: 0,
    enqueueCount: 0,
    firstSessionKey: '',
    handoffTargets: {},
    pendingRows: [],
    reorderCount: 0,
    supportsPendingQueue: true,
    turns: [],
    streamSeq: 0,
    liveEvents: [],
  }
}

function commitTurn(
  state: MockGatewayState,
  sessionKey: string,
  taskId: string,
  params: Record<string, unknown>,
) {
  state.turns.push({
    taskId, sessionKey,
    messageId: String(params.clientMessageId || `synthetic-user-${taskId}`),
    message: String(params.message || ''),
    createdAt: 1_800_000_000_000 + state.turns.length * 10_000,
    transcriptId: state.turns.length * 2 + 1,
    finished: false,
  })
}

function committedHistory(state: MockGatewayState, params: Record<string, unknown>) {
  const turns = state.turns.filter(turn => turn.sessionKey === params.sessionKey)
  const messages = turns.flatMap(turn => {
    const user = {
      role: 'user', text: turn.message, id: turn.messageId, message_id: turn.messageId,
      client_message_id: turn.messageId, timestamp: turn.createdAt,
      transcript_id: turn.transcriptId, turn_id: turn.taskId,
      turn_context: { turn_id: turn.taskId },
    }
    return turn.finished && turn.terminalStatus !== 'cancelled' ? [user, {
      ...user, role: 'assistant', text: 'ok', id: `synthetic-assistant-${turn.taskId}`,
      message_id: `synthetic-assistant-${turn.taskId}`, client_message_id: '',
      timestamp: turn.createdAt + 1_000, transcript_id: turn.transcriptId + 1,
    }] : [user]
  })
  const cursor = (message: typeof messages[number]) => `${message.timestamp}|${message.transcript_id}`
  const afterIndex = params.after ? messages.findIndex(message => cursor(message) === params.after) : -1
  const beforeIndex = params.before ? messages.findIndex(message => cursor(message) === params.before) : -1
  const page = beforeIndex >= 0 ? messages.slice(0, beforeIndex) : messages.slice(afterIndex + 1)
  return chatHistoryPayload(page, {
    oldest_cursor: page.length ? cursor(page[0]!) : null,
    newest_cursor: page.length ? cursor(page[page.length - 1]!) : null,
    turn_outcomes: turns.filter(turn => turn.finished).map(turn => ({
      task_id: turn.taskId, turn_id: turn.taskId, status: turn.terminalStatus || 'succeeded',
      outcome: { kind: turn.terminalStatus === 'cancelled' ? 'cancelled' : 'completed' }, started_at: turn.createdAt,
      finished_at: turn.createdAt + 1_000,
      activity_snapshot: {
        version: 2, task_id: turn.taskId, turn_id: turn.taskId, complete: true,
        reasoning_utf16_length: 0, entries: [{
          type: 'phase', id: `synthetic-provider-${turn.taskId}`, order: 1,
          kind: 'provider', phase: 'requesting', at: turn.createdAt,
          ended_at: turn.createdAt + 1_000,
        }],
      },
    })),
  })
}

function committedMetadata(state: MockGatewayState, sessionKey: string) {
  const tasks = state.turns.filter(turn => turn.sessionKey === sessionKey).map(turn => ({
    task_id: turn.taskId, turn_id: turn.taskId, session_id: `synthetic-session-${sessionKey}`,
    status: turn.finished ? turn.terminalStatus || 'succeeded' : 'running', queue_mode: 'followup',
    created_at: turn.createdAt, started_at: turn.createdAt,
    ...(turn.finished ? { finished_at: turn.createdAt + 1_000, terminal_reason: turn.terminalStatus === 'cancelled' ? 'user_abort' : 'completed' } : {}),
  }))
  const active = tasks.find(task => task.status === 'running') || null
  return {
    epoch: 1, tasks, active_task: active, last_task: tasks.at(-1) || null,
    queued_task_ids: [], run_status: active ? 'running' : 'idle',
  }
}

async function installMockGateway(
  page: Page,
  scenario: Scenario,
  state: MockGatewayState = createMockGatewayState(),
): Promise<MockGateway> {
  const sockets = new Set<WebSocketRoute>()
  let firstAck: (() => void) | null = null
  const firstTaskId = 'p1-5-first-task'
  let connectionCount = 0
  let subscribedConnection = 0
  let holdingConnections = false
  let holdingReceiptReplies = false
  let peakReceiptInFlight = 0
  const waitingHandshakes = new Set<WebSocketRoute>()
  const receiptReplies = new Map<() => void, WebSocketRoute>()
  const aborts: Array<Record<string, unknown>> = []
  const forgetSocket = (socket: WebSocketRoute) => {
    sockets.delete(socket)
    waitingHandshakes.delete(socket)
    for (const [reply, owner] of receiptReplies) if (owner === socket) receiptReplies.delete(reply)
  }

  const emit = (event: string, payload: Record<string, unknown>) => {
    if (event !== 'session.event.done') state.liveEvents.push({ event, payload })
    for (const socket of sockets) socket.send(eventFrame(event, payload))
  }

  const sendDone = (taskId: string) => {
    const turn = state.turns.find(turn => turn.taskId === taskId)
    if (!turn || turn.finished) return
    // A terminal push follows durable transcript/outcome persistence. Subsequent
    // reads must observe the same completed task, including instant completions.
    turn.finished = true
    state.liveEvents = state.liveEvents.filter(event => event.payload.task_id !== taskId)
    emit('session.event.done', {
      key: turn.sessionKey, sessionKey: turn.sessionKey, task_id: taskId, epoch: 1,
      stream_generation: 'p1-5-generation', stream_seq: ++state.streamSeq,
      status: 'succeeded', reason: 'completed', text_snapshot: 'ok',
    })
  }

  await page.routeWebSocket(/\/ws$/, ws => {
    let connection = 0
    sockets.add(ws)
    ws.onClose(() => forgetSocket(ws))
    if (holdingConnections) waitingHandshakes.add(ws)
    else ws.send(eventFrame('connect.challenge', {}))
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
        connection = ++connectionCount
        ws.send(hello(state.supportsPendingQueue))
        return
      }
      if (method === 'chat.history') {
        ws.send(successResponse(frame.id, committedHistory(state, frame.params || {})))
        return
      }
      if (method === 'turns.receipt.get') {
        const params = frame.params || {}
        state.receiptQueries.push(params)
        const reply = () => {
          if (!receiptReplies.delete(reply)) return
          const original = (params.originalRequest || {}) as Record<string, unknown>
          const requestId = String(original.clientRequestId || '')
          const targetSessionKey = state.handoffTargets[requestId]
          if (params.operation !== 'chat.send' || !targetSessionKey) {
            ws.send(successResponse(frame.id, { status: 'not_found', accepted: null }))
            return
          }
          // Admission committed before ACK loss. Read the original receipt;
          // never invoke chat.send or manufacture another task while checking.
          state.firstSessionKey = targetSessionKey
          const turn = state.turns.find(turn => turn.taskId === firstTaskId)
          ws.send(successResponse(frame.id, {
            status: 'found',
            accepted: true,
            requestFingerprint: `sha256:${'a'.repeat(64)}`,
            receipt: {
              requestSessionKey: original.sessionKey,
              sessionKey: targetSessionKey,
              sessionId: `synthetic-session-${targetSessionKey}`,
              sessionEpoch: 1,
              clientRequestId: requestId,
              messageId: original.clientMessageId,
              taskId: firstTaskId,
              taskStatus: turn?.finished ? turn.terminalStatus || 'succeeded' : 'running',
            },
          }))
        }
        receiptReplies.set(reply, ws)
        peakReceiptInFlight = Math.max(peakReceiptInFlight, receiptReplies.size)
        if (!holdingReceiptReplies) reply()
        return
      }
      if (method === 'chat.abort') {
        const params = { ...(frame.params || {}) }
        aborts.push(params)
        const turn = state.turns.find(turn => turn.taskId === params.taskId && turn.sessionKey === params.sessionKey)
        if (!turn) {
          ws.send(successResponse(frame.id, { aborted: false, reason: 'task_not_active' }))
          return
        }
        turn.finished = true
        turn.terminalStatus = 'cancelled'
        state.liveEvents = state.liveEvents.filter(event => event.payload.task_id !== turn.taskId)
        ws.send(successResponse(frame.id, { aborted: true, key: turn.sessionKey }))
        return
      }
      if (method === 'sessions.messages.snapshot') {
        const key = String(frame.params?.key || '')
        const metadata = committedMetadata(state, key)
        ws.send(successResponse(frame.id, sessionMessagesSnapshotPayload(key, {
          current_stream_seq: state.streamSeq,
          stream_generation: 'p1-5-generation',
          task_id: metadata.active_task?.task_id || null,
          events: state.liveEvents.filter(event => event.payload.key === key),
        })))
        return
      }
      if (method === 'sessions.messages.subscribe' || method === 'sessions.messages.hydrate') {
        if (method === 'sessions.messages.subscribe' && frame.params?.key === state.firstSessionKey) {
          subscribedConnection = connection
        }
        const key = String(frame.params?.key || '')
        const payload = method === 'sessions.messages.subscribe'
          ? sessionMessagesSubscribePayload : sessionMessagesHydratePayload
        const gap = Number(frame.params?.since_stream_seq ?? state.streamSeq) < state.streamSeq
        ws.send(successResponse(frame.id, payload(key, {
          current_stream_seq: state.streamSeq,
          stream_generation: 'p1-5-generation',
          ...committedMetadata(state, key),
          ...(method === 'sessions.messages.subscribe' ? {
            replay_complete: !gap, replay_gap_reason: gap ? 'buffer_window_missed' : null,
          } : {}),
        })))
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
        if (committed) commitTurn(state, state.firstSessionKey, queuedTaskId, committed)
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
        // Real admission commits the user transcript, task, and receipt before
        // returning ACK. Keep reads consistent even if routing races that ACK.
        commitTurn(state, responseSessionKey, taskId, params)
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
              stream_seq: ++state.streamSeq,
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
              stream_seq: ++state.streamSeq,
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
    chatSends: state.chatSends,
    receiptQueries: state.receiptQueries,
    get connectionCount() { return connectionCount },
    get subscribedConnection() { return subscribedConnection },
    aborts,
    get socketCount() { return sockets.size },
    get waitingHandshakeCount() { return waitingHandshakes.size },
    get receiptInFlight() { return receiptReplies.size },
    get peakReceiptInFlight() { return peakReceiptInFlight },
    dropFirstAck() {
      if (!firstAck) throw new Error('No committed first send is awaiting its lost ACK')
      firstAck = null
      holdingConnections = true
      for (const socket of [...sockets]) {
        socket.close({ code: 1012, reason: 'Synthetic accepted send lost ACK' })
        // onClose handles the page closing its side; this branch owns the
        // synthetic server close and retires its held replies immediately.
        forgetSocket(socket)
      }
    },
    holdConnections() { holdingConnections = true },
    releaseConnections() {
      holdingConnections = false
      for (const socket of waitingHandshakes) socket.send(eventFrame('connect.challenge', {}))
      waitingHandshakes.clear()
    },
    holdReceiptReplies() { holdingReceiptReplies = true },
    releaseReceiptReplies() {
      holdingReceiptReplies = false
      for (const reply of [...receiptReplies.keys()]) reply()
    },
    dispatchMessages: state.dispatchMessages,
    get dispatchCount() { return state.dispatchCount },
    get enqueueCount() { return state.enqueueCount },
    finishFirst() {
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
  await expect.poll(() => page.evaluate(() => localStorage.getItem('opensquilla.deliverySalt.v1')))
    .toMatch(/^[0-9a-f]{32}$/)
  await page.evaluate(async ({ seed, principal }) => {
    // Freeze this synthetic connection's credential-free delivery-v1 identity.
    // This works with the production bundle without development-only hooks.
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
    const target = JSON.stringify(['browser', `${protocol}//${location.host}/ws`, ''])
    const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(JSON.stringify([
      localStorage.getItem('opensquilla.deliverySalt.v1'), target,
    ])))
    const targetId = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('')
    const deliveryIdentity = JSON.stringify([
      'delivery-v1', targetId, principal.role, principal.authState,
      principal.authenticated, principal.isOwner, [...principal.scopes].sort(),
      [...principal.capabilities].sort(), principal.tokenPublicId, principal.guestOwnerId,
    ])
    const open = indexedDB.open('opensquilla-chat-pending-inputs', 3)
    const database = await new Promise<IDBDatabase>((resolve, reject) => {
      open.onupgradeneeded = () => {
        const db = open.result
        if (!db.objectStoreNames.contains('pending_chat_inputs')) {
          const store = db.createObjectStore('pending_chat_inputs', { keyPath: 'pendingInputId' })
          store.createIndex('session_created', ['sessionKey', 'createdAt'], { unique: false })
        }
        if (!db.objectStoreNames.contains('response_handoffs')) {
          const handoffs = db.createObjectStore('response_handoffs', { keyPath: 'ownerRequestId' })
          handoffs.createIndex('recovery_state', 'recoveryState')
          handoffs.createIndex('task_scope', 'taskScope')
          handoffs.createIndex('handoff_session', 'handoff.requestSessionKey')
          handoffs.createIndex('steer_scope', 'steerScope')
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
      const params = {
        clientRequestId: seed.ownerRequestId,
        clientMessageId: seed.clientMessageId,
        message: 'P1-5 durable fork prompt',
        queueMode: 'followup',
        sessionKey: seed.parentSessionKey,
        forkBeforeMessageId: 'synthetic-parent-message',
        source: { channel: 'webui' },
      }
      transaction.objectStore('response_handoffs').put({
        schemaVersion: 2,
        ownerRequestId: seed.ownerRequestId,
        deliveryIdentity,
        requestSessionKey: seed.parentSessionKey,
        request: { kind: 'send', request: { kind: 'new-turn', params } },
        phase: 'unknown',
        recoveryState: 'pending',
        revision: 1,
        handoff: {
          schemaVersion: 1,
          ownerRequestId: seed.ownerRequestId,
          requestSessionKey: seed.parentSessionKey,
          clientRequestId: seed.ownerRequestId,
          clientMessageId: seed.clientMessageId,
          params,
          composerText: 'P1-5 durable fork prompt',
          recoveryAttachments: [],
          state: 'submitting',
          createdAt: now,
          updatedAt: now,
        },
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
          deliveryIdentity,
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
  }, { seed: input, principal: DELIVERY_PRINCIPAL })
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
    // A connected pill can still belong to the socket scheduled for closure.
    // Observe a new Hello and this session's subscription on that connection
    // before completing this scenario's task on the replacement socket.
    await expect.poll(() => gateway.connectionCount).toBe(2)
    await expect.poll(() => gateway.subscribedConnection).toBe(2)
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
    await expect(page.locator('.chat-pending-card').filter({ hasText: SECOND_TEXT })).toHaveCount(0)
    expect(gateway.pendingRows()).toEqual([])
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

  test('ordinary unknown Stop survives chat unmount and full App reload without duplicate admission', async ({ page }, testInfo) => {
    test.setTimeout(45_000)
    await page.setViewportSize({ width: 1440, height: 900 })
    await preparePage(page)
    const errors = collectRendererErrors(page)
    const state = createMockGatewayState()
    state.supportsPendingQueue = false
    const gateway = await installMockGateway(page, 'delayed', state)
    const sessionKey = 'agent:main:webchat:synthetic-ordinary-stop-reload'
    const text = 'Synthetic ordinary request whose accepted ACK is lost.'
    let requestId = ''

    // Read only the actual application-created record. This test neither seeds
    // a delivery owner nor rewrites its phase, identity, Stop, lease, or clock.
    const readDelivery = () => page.evaluate(async id => {
      const database = await new Promise<IDBDatabase>((resolve, reject) => {
        const request = indexedDB.open('opensquilla-chat-pending-inputs', 3)
        request.onsuccess = () => resolve(request.result)
        request.onerror = () => reject(request.error)
      })
      try {
        return await new Promise<{
          count: number
          record: {
            schemaVersion: number; revision: number; ownerRequestId: string; deliveryIdentity: string;
            requestSessionKey: string; phase: string; response?: { taskId?: string };
            request: { kind: string; request: { kind: string; params: Record<string, unknown> } };
            stop?: { requested: boolean; completed?: boolean; request?: Record<string, unknown> };
            lease?: { owner: string; epoch: number; expiresAt: number };
          }
        }>((resolve, reject) => {
          const transaction = database.transaction('response_handoffs', 'readonly')
          const store = transaction.objectStore('response_handoffs')
          const get = store.get(id)
          const count = store.count()
          transaction.oncomplete = () => resolve({ count: count.result, record: get.result })
          transaction.onabort = () => reject(transaction.error)
        })
      } finally { database.close() }
    }, requestId)

    await page.goto(`${CONTROL_URL}chat?session=${encodeURIComponent(sessionKey)}`)
    await expect(page.locator('.conn-pill.connected')).toBeVisible()
    const composer = page.locator('.chat-textarea')
    await expect(composer).toBeEditable()
    await composer.fill(text)
    await page.locator('.chat-send-btn[aria-label="Send"]').click()
    await expect.poll(() => gateway.chatSends.length).toBe(1)
    const original = structuredClone(gateway.chatSends[0]!)
    requestId = String(original.clientRequestId)
    expect(original.clientRequestId).toEqual(expect.any(String))
    expect(requestId).not.toBe('')
    expect(original.clientMessageId).toEqual(expect.any(String))
    expect(original.clientMessageId).not.toBe('')
    expect(original).toMatchObject({ sessionKey, message: text })
    expect(state.turns).toHaveLength(1)
    expect(state.turns[0]).toMatchObject({ sessionKey, taskId: 'p1-5-first-task', finished: false })

    // Acceptance exists server-side, but the socket loses its ACK before any
    // task event. Hold the next Hello so Stop must own this unknown request,
    // not a task opportunistically learned from a later history hydration.
    gateway.dropFirstAck()
    await expect(page.locator('.conn-pill.connected')).toHaveCount(0)
    await expect.poll(async () => (await readDelivery()).record.phase).toBe('unknown')
    expect((await readDelivery()).record.response).toBeUndefined()
    const stopButton = page.getByRole('button', { name: 'Stop current response', exact: true })
    await expect(stopButton).toBeVisible()
    await stopButton.click()
    await expect.poll(async () => (await readDelivery()).record.stop).toMatchObject({ requested: true })
    const originalRecord = (await readDelivery()).record
    expect(originalRecord.stop?.completed).not.toBe(true)
    expect(originalRecord.deliveryIdentity).toBeTruthy()
    expect(originalRecord.request).toMatchObject({ kind: 'send', request: { kind: 'new-turn' } })
    const frozenDomainParams = structuredClone(originalRecord.request.request.params)
    // WAL preserves domain fields (such as source), while the adapter owns
    // wire aliases (_source). Check each complete snapshot in its own domain.
    expect(frozenDomainParams).toMatchObject({ sessionKey, message: text,
      clientRequestId: requestId, clientMessageId: original.clientMessageId })
    expect(gateway.aborts).toEqual([])
    expect(gateway.receiptQueries).toEqual([])
    const initialOwner = originalRecord.lease?.owner
    expect(initialOwner).toBeTruthy()

    gateway.releaseConnections()
    await expect(page.locator('.conn-pill.connected')).toBeVisible()
    await expect.poll(() => gateway.receiptQueries.length).toBeGreaterThan(0)
    const connectedBeforeNavigation = gateway.connectionCount
    await page.locator('a[href$="/usage"]').first().click()
    await expect(page).toHaveURL(/\/usage(?:\?|$)/)
    await expect(page.locator('.chat')).toHaveCount(0)
    const notice = page.getByTestId('delivery-recovery-notice')
    await expect(notice).toBeVisible()
    // ACK loss surfaces the existing transient failure toast. A user can
    // dismiss it before using the bottom recovery notice; keep real clicks.
    const lostAckToast = page.getByTestId('toast').filter({ hasText: 'The task did not finish. Please try again later.' })
    await expect(lostAckToast).toBeVisible()
    await lostAckToast.getByRole('button', { name: 'Dismiss notification', exact: true }).click()
    await expect(lostAckToast).toHaveCount(0)
    await notice.getByRole('button', { name: 'View details', exact: true }).click({ timeout: 5_000 })
    await expect(notice).toContainText('Stop will continue after the original task is confirmed.')
    const noticeScreenshot = testInfo.outputPath('usage-expanded-delivery-notice.png')
    await page.screenshot({ path: noticeScreenshot })
    await testInfo.attach('Usage delivery actions after chat unmount', {
      path: noticeScreenshot, contentType: 'image/png',
    })
    // Use the same App and unresolved delivery at a narrow viewport. Close the
    // real sidebar before it becomes a modal drawer; do not remove overlays.
    await page.getByTestId('sidebar-toggle-expanded').click()
    await page.setViewportSize({ width: 390, height: 844 })
    await notice.getByRole('button', { name: 'Hide details', exact: true }).click({ timeout: 5_000 })
    await notice.getByRole('button', { name: 'View details', exact: true }).click({ timeout: 5_000 })
    await expect(notice).toContainText('Stop will continue after the original task is confirmed.')
    await notice.getByRole('button', { name: 'Open conversation', exact: true }).click({ trial: true, timeout: 5_000 })
    const mobileNoticeScreenshot = testInfo.outputPath('usage-mobile-expanded-delivery-notice.png')
    await page.screenshot({ path: mobileNoticeScreenshot })
    await testInfo.attach('Narrow Usage recovery controls remain reachable', {
      path: mobileNoticeScreenshot, contentType: 'image/png',
    })
    await page.setViewportSize({ width: 1440, height: 900 })
    await page.getByTestId('sidebar-toggle-collapsed').click()
    // The actual App owner finishes its finite not-found round after ChatView
    // has unmounted. Its lease stays with the same owner, and is released.
    await expect.poll(() => gateway.receiptQueries.length, { timeout: 10_000 }).toBe(4)
    await expect.poll(async () => (await readDelivery()).record.lease?.expiresAt).toBe(0)
    await expect(notice).toContainText('Automatic checks have paused.')
    expect((await readDelivery()).record.lease?.owner).toBe(initialOwner)
    await notice.getByRole('button', { name: 'Open conversation', exact: true }).click()
    await expect(page).toHaveURL(url => url.pathname.endsWith('/chat') && url.searchParams.get('session') === sessionKey)
    await expectSingletonChat(page)
    expect(gateway.connectionCount).toBe(connectedBeforeNavigation)
    expect((await readDelivery()).record.lease?.owner).toBe(initialOwner)
    expect((await readDelivery()).record.stop?.requested).toBe(true)
    expect(gateway.chatSends).toHaveLength(1)
    expect(gateway.aborts).toEqual([])
    await page.locator('a[href$="/usage"]').first().click()
    await expect(page.locator('.chat')).toHaveCount(0)

    // Recreate the whole production main/App in the same origin, retaining
    // native IndexedDB. A cached identity alone must not authorize recovery.
    gateway.holdConnections()
    gateway.holdReceiptReplies()
    await page.reload()
    await expect.poll(() => gateway.waitingHandshakeCount).toBe(1)
    await expect(page.getByTestId('delivery-recovery-notice')).toBeVisible()
    const unconfirmed = await readDelivery()
    expect(unconfirmed.count).toBe(1)
    expect(unconfirmed.record).toMatchObject({ ownerRequestId: requestId, phase: 'unknown',
      deliveryIdentity: originalRecord.deliveryIdentity, stop: { requested: true } })
    expect(unconfirmed.record.stop?.completed).not.toBe(true)
    expect(gateway.receiptQueries).toHaveLength(4)
    expect(gateway.aborts).toEqual([])

    // Same authenticated Gateway + account; the already committed receipt now
    // becomes available. Keep the read pending to observe real single-flight
    // and lease ownership while the global notice also requests a recheck.
    state.handoffTargets[requestId] = sessionKey
    gateway.releaseConnections()
    await expect(page.locator('.conn-pill.connected')).toBeVisible()
    await expect.poll(() => gateway.receiptInFlight).toBe(1)
    const replacementRecord = (await readDelivery()).record
    expect(replacementRecord.lease?.owner).toBeTruthy()
    expect(replacementRecord.lease?.owner).not.toBe(initialOwner)
    expect(replacementRecord.lease!.epoch).toBeGreaterThan(originalRecord.lease!.epoch)
    expect(replacementRecord.lease!.expiresAt).toBeGreaterThan(Date.now())
    const replacementNotice = page.getByTestId('delivery-recovery-notice')
    await replacementNotice.getByRole('button', { name: 'View details', exact: true }).click({ timeout: 5_000 })
    await replacementNotice.getByRole('button', { name: 'Check again', exact: true }).click()
    await expect.poll(async () => (await readDelivery()).record.revision).toBeGreaterThan(replacementRecord.revision)
    await expect(replacementNotice.getByRole('button', { name: 'Check again', exact: true })).toBeEnabled()
    expect(gateway.socketCount).toBe(1)
    expect(gateway.receiptInFlight).toBe(1)
    expect(gateway.receiptQueries).toHaveLength(5)
    expect(gateway.peakReceiptInFlight).toBe(1)
    expect(gateway.aborts).toEqual([])
    gateway.releaseReceiptReplies()
    await expect.poll(() => gateway.aborts.length).toBe(1)
    expect(gateway.aborts[0]).toMatchObject({ sessionKey, taskId: 'p1-5-first-task', scope: 'task' })
    await expect.poll(async () => (await readDelivery()).record.stop?.completed).toBe(true)
    await expect(replacementNotice).toHaveCount(0)
    const completed = await readDelivery()
    expect(completed.count).toBe(1)
    expect(completed.record).toMatchObject({ ownerRequestId: requestId, phase: 'accepted',
      deliveryIdentity: originalRecord.deliveryIdentity, response: { taskId: 'p1-5-first-task' },
      lease: { owner: replacementRecord.lease!.owner }, stop: { requested: true, completed: true } })
    expect(completed.record.request.request.params).toEqual(frozenDomainParams)
    for (const query of gateway.receiptQueries) expect(query).toEqual({ operation: 'chat.send', originalRequest: original })
    expect(gateway.chatSends).toEqual([original])
    expect(gateway.aborts).toHaveLength(1)
    expect(gateway.receiptQueries).toHaveLength(5)
    expect(gateway.receiptInFlight).toBe(0)
    expect(gateway.peakReceiptInFlight).toBe(1)
    expect(state.turns).toHaveLength(1)
    expect(state.turns[0]?.terminalStatus).toBe('cancelled')
    expect(errors.pageErrors).toEqual([])
    expect(errors.consoleErrors.filter(message => FATAL_RENDERER_PATTERN.test(message))).toEqual([])
    // These are operational owner proxies (lease writer, in-flight read, and
    // exact mutation counts), not a claim to count dormant JS owner objects.
  })

  test('refresh reads a fork receipt without resending and moves owner follow-ups exactly once', async ({ page }) => {
    test.setTimeout(45_000)
    const errors = collectRendererErrors(page)
    await preparePage(page)
    const state = createMockGatewayState()
    const parentSessionKey = 'agent:main:webchat:handoff-parent'
    const childSessionKey = 'agent:main:webchat:handoff-child'
    const ownerRequestId = 'request-durable-handoff'
    state.handoffTargets[ownerRequestId] = childSessionKey
    state.firstSessionKey = childSessionKey
    commitTurn(state, childSessionKey, 'p1-5-first-task', {
      clientMessageId: 'message-durable-handoff', message: 'P1-5 durable fork prompt',
    })
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
    await expect.poll(() => gateway.receiptQueries.length).toBe(1)
    expect(gateway.chatSends).toHaveLength(0)
    expect(gateway.receiptQueries[0]).toEqual({
      operation: 'chat.send',
      originalRequest: {
        clientRequestId: ownerRequestId,
        clientMessageId: 'message-durable-handoff',
        sessionKey: parentSessionKey,
        forkBeforeMessageId: 'synthetic-parent-message',
        message: 'P1-5 durable fork prompt',
        queueMode: 'followup',
        _source: { channel: 'webui' },
      },
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
    await expect.poll(() => page.evaluate(async requestId => {
      const request = indexedDB.open('opensquilla-chat-pending-inputs')
      const database = await new Promise<IDBDatabase>((resolve, reject) => {
        request.onsuccess = () => resolve(request.result)
        request.onerror = () => reject(request.error)
      })
      try {
        const transaction = database.transaction('response_handoffs', 'readonly')
        const row = await new Promise<Record<string, unknown>>((resolve, reject) => {
          const get = transaction.objectStore('response_handoffs').get(requestId)
          get.onsuccess = () => resolve(get.result)
          get.onerror = () => reject(get.error)
        })
        return row
      } finally {
        database.close()
      }
    }, ownerRequestId)).toMatchObject({
      schemaVersion: 2,
      ownerRequestId,
      phase: 'accepted',
      request: {
        kind: 'send',
        request: { kind: 'new-turn', params: { clientRequestId: ownerRequestId } },
      },
      response: { sessionKey: childSessionKey, taskId: 'p1-5-first-task', replayed: true },
      handoff: undefined,
    })
    expect(gateway.receiptQueries).toHaveLength(1)
    expect(gateway.chatSends).toHaveLength(0)

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
