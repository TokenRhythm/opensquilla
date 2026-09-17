import { expect, type Page, type WebSocketRoute } from '@playwright/test'
import { test } from './support/native-desktop-fixture'
import { helloOkResponse } from './support/gateway-fixture'
import {
  chatHistoryPayload,
  sessionMessagesHydratePayload,
  sessionMessagesSnapshotPayload,
  sessionMessagesSubscribePayload,
} from './support/session-read-fixtures'

const SESSION = 'agent:main:webchat:e2e-send-lifecycle'
const GENERATION = 'send-lifecycle-generation'
const REJECTION = 'Synthetic admission failed. Please retry.'
type TerminalStatus = 'succeeded' | 'failed' | 'cancelled'
type Request = { id: string; method: string; params?: Record<string, unknown>; type: string }

async function installGateway(page: Page, holdFirstSend = false, serverQueue = false) {
  const sends: Array<Record<string, unknown>> = []
  const enqueues: Array<Record<string, unknown>> = []
  const dispatches: Array<Record<string, unknown>> = []
  const pendingItems = new Map<string, Record<string, unknown>>()
  const aborts: Array<Record<string, unknown>> = []
  const history: Array<Record<string, unknown>> = []
  let socket: WebSocketRoute
  let sequence = 0
  let activeTask: { task_id: string; status: string } | null = null
  let lastTask: { task_id: string; status: string } | null = null
  let heldSend: Request | null = null
  let connectionCount = 0
  let holdConnections = false
  let waitingConnection: WebSocketRoute | null = null
  let changedAuthority = false

  const response = (id: string, payload: unknown) => socket.send(JSON.stringify({
    type: 'res', id, ok: true, payload,
  }))
  const emit = (event: string, payload: Record<string, unknown>) => socket.send(JSON.stringify({
    type: 'event', event, payload: {
      key: SESSION, session_key: SESSION, stream_generation: GENERATION,
      stream_seq: ++sequence, ...payload,
    },
  }))
  const metadata = () => ({
    stream_generation: GENERATION,
    current_stream_seq: sequence,
    run_status: activeTask ? 'running' : 'idle',
    active_task: activeTask,
    last_task: lastTask,
    tasks: activeTask ? [activeTask] : [],
  })
  const finish = (status: TerminalStatus) => {
    if (!activeTask) throw new Error('No accepted task to finish')
    const taskId = activeTask.task_id
    activeTask = null
    lastTask = { task_id: taskId, status }
    if (status === 'succeeded') {
      const text = 'Synthetic response completed.'
      history.push({ role: 'assistant', text, id: `answer-${taskId}`, turn_id: taskId })
      emit('session.event.text_delta', { task_id: taskId, text })
      emit('session.event.done', { task_id: taskId, reason: 'completed', text_snapshot: text })
    }
    emit(`task.${status}`, {
      task_id: taskId, status,
      terminal_reason: status === 'cancelled' ? 'user_abort' : status,
      terminal_message: status === 'failed' ? 'Synthetic provider failure.' : undefined,
    })
    emit('sessions.changed', { reason: 'task_terminal', ...metadata() })
  }

  await page.addInitScript(() => localStorage.setItem('opensquilla-locale', 'en'))
  await page.route('**/api/**', route => route.fulfill({ json: {} }))
  await page.route('**/api/approvals', route => route.fulfill({
    json: { pending: [], mode: 'prompt', allowPatterns: [], denyPatterns: [] },
  }))
  await page.routeWebSocket(/\/ws$/, ws => {
    socket = ws
    connectionCount += 1
    if (holdConnections) waitingConnection = ws
    else emit('connect.challenge', {})
    ws.onMessage(raw => {
      const frame = JSON.parse(String(raw)) as Request
      if (frame.type === 'ping') {
        ws.send(JSON.stringify({ type: 'pong' }))
        return
      }
      if (frame.type !== 'req') return
      if (frame.method === 'connect') {
        ws.send(helloOkResponse({
          features: { methods: [
            'sessions.messages.subscribe', 'sessions.messages.hydrate',
            'sessions.messages.snapshot', 'sessions.messages.unsubscribe',
            ...(serverQueue ? [
              'sessions.pending_inputs.enqueue', 'sessions.pending_inputs.list',
              'sessions.pending_inputs.cancel', 'sessions.pending_inputs.dispatch',
            ] : []),
          ] },
          auth: {
            principal: {
              role: 'operator', isOwner: true, authenticated: true,
              authState: 'authenticated', scopes: ['operator.read', 'operator.write'],
              capabilities: changedAuthority ? ['chat.read'] : ['chat.read', 'chat.write'],
            },
            runModePolicy: { allowedRunModes: ['safe', 'full'], defaultRunMode: 'full' },
          },
        }))
        return
      }
      if (frame.method === 'sessions.pending_inputs.enqueue') {
        enqueues.push({ ...frame.params })
        const pendingInputId = String(frame.params?.pendingInputId)
        const item = { ...frame.params, requestFingerprint: 'synthetic-fingerprint', revision: 1 }
        pendingItems.set(pendingInputId, item)
        response(frame.id, { requestFingerprint: 'synthetic-fingerprint', revision: 1 })
        return
      }
      if (frame.method === 'chat.send' || frame.method === 'sessions.pending_inputs.dispatch') {
        let params = frame.params
        if (frame.method === 'sessions.pending_inputs.dispatch') {
          dispatches.push({ ...frame.params })
          const id = String(frame.params?.pendingInputId)
          params = pendingItems.get(id)
          pendingItems.delete(id)
        }
        sends.push({ ...params })
        if (holdFirstSend && sends.length === 1) {
          heldSend = frame
          return
        }
        activeTask = { task_id: `task-send-${sends.length}`, status: 'running' }
        history.push({
          role: 'user', text: params?.message, id: `user-${sends.length}`,
          client_message_id: params?.clientMessageId,
          turn_id: activeTask.task_id,
        })
        response(frame.id, { accepted: true, session: SESSION, task_id: activeTask.task_id })
        emit('task.running', { task_id: activeTask.task_id })
        return
      }
      if (frame.method === 'chat.abort') {
        aborts.push({ ...frame.params })
        response(frame.id, { aborted: true, key: SESSION })
        finish('cancelled')
        return
      }
      const payloads: Record<string, unknown> = {
        'agents.list': { agents: [] },
        'commands.list_for_surface': { commands: [] },
        'config.get': {
          squilla_router: { enabled: false, tiers: {} }, permissions: {}, skills: {},
        },
        'models.routing.get': { mode: 'direct' },
        'sandbox.run_mode.preference.get': { runMode: 'full', source: 'config' },
        'sessions.list': {
          sessions: [{
            key: SESSION, title: 'Synthetic send lifecycle', sessionKind: 'chat',
            surface: 'webchat', conversationKind: 'direct', effectiveAgentId: 'main',
            updatedAt: 100, messageCount: history.length, status: 'ok',
            runStatus: activeTask ? 'running' : 'idle',
          }], count: 1, ts: 1_800_000_000, has_more: false,
        },
        'chat.history': chatHistoryPayload(history),
        'sessions.messages.subscribe': sessionMessagesSubscribePayload(SESSION, metadata()),
        'sessions.messages.hydrate': sessionMessagesHydratePayload(SESSION, metadata()),
        'sessions.messages.snapshot': sessionMessagesSnapshotPayload(SESSION, {
          ...metadata(), task_id: activeTask?.task_id ?? null,
        }),
        'sessions.messages.unsubscribe': { subscribed: false },
        'sessions.subscribe': { subscribed: true },
        'sessions.pending_inputs.list': { items: [...pendingItems.values()] },
        'usage.status': { sessions: [] },
      }
      response(frame.id, payloads[frame.method] ?? {})
    })
  })

  return {
    sends, aborts, finish, enqueues, dispatches,
    connectionCount: () => connectionCount,
    disconnect: () => {
      holdConnections = true
      socket.close({ code: 1012, reason: 'Synthetic transport restart' })
    },
    reconnect: (changeAuthority = false) => {
      changedAuthority = changeAuthority
      holdConnections = false
      if (waitingConnection) {
        waitingConnection.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
        waitingConnection = null
      }
    },
    rejectHeldSend: () => {
      if (!heldSend) throw new Error('No pending send to reject')
      socket.send(JSON.stringify({
        type: 'res', id: heldSend.id, ok: false,
        error: { code: 'SYNTHETIC_ADMISSION_FAILURE', message: REJECTION, accepted: false, retryable: true },
      }))
      heldSend = null
    },
  }
}

async function openChat(page: Page, expectedState: 'idle' | 'running' = 'idle') {
  const root = process.env.OPENSQUILLA_E2E_NATIVE_DESKTOP === '1'
    ? 'opensquilla-app://desktop/' : '/control/'
  await page.goto(`${root}chat?session=${encodeURIComponent(SESSION)}`)
  await expect(page.locator('.conn-pill.connected')).toBeVisible()
  const readyAction = expectedState === 'running'
    ? page.getByRole('button', { name: 'Stop current response' })
    : page.locator('.chat-send-btn[aria-label="Send"]')
  await expect(readyAction).toBeEnabled()
}

for (const status of ['succeeded', 'failed', 'cancelled'] as const) {
  test(`sends again in the same session after a ${status} task`, async ({ page }) => {
    const gateway = await installGateway(page)
    await openChat(page)
    const originalUrl = page.url()
    const input = page.locator('.chat-textarea')
    const send = page.locator('.chat-send-btn[aria-label="Send"]')
    const stop = page.getByRole('button', { name: 'Stop current response' })

    await input.fill('Synthetic first request.')
    await send.click()
    await expect.poll(() => gateway.sends.length).toBe(1)
    await expect(stop).toBeVisible()
    await input.fill('Synthetic next request without changing projects.')
    if (status === 'cancelled') {
      await stop.click()
      await expect.poll(() => gateway.aborts.length).toBe(1)
      expect(gateway.aborts[0]).toMatchObject({ sessionKey: SESSION, taskId: 'task-send-1' })
    } else gateway.finish(status)

    await expect(stop).toHaveCount(0)
    await expect(send).toBeEnabled()
    await expect(input).toHaveValue('Synthetic next request without changing projects.')
    await send.click()
    await expect.poll(() => gateway.sends.length).toBe(2)
    expect(gateway.sends[1]).toMatchObject({
      sessionKey: SESSION, message: 'Synthetic next request without changing projects.',
    })
    expect(page.url()).toBe(originalUrl)
    expect(gateway.connectionCount()).toBe(1)
    await expect(stop).toBeVisible()
    gateway.finish('succeeded')
    await expect(send).toBeEnabled()
  })
}

test('pending admission is visible and a rejected send restores an editable retry', async ({ page }) => {
  const gateway = await installGateway(page, true)
  await openChat(page)
  const input = page.locator('.chat-textarea')
  const send = page.locator('.chat-send-btn[aria-label="Send"]')
  await input.fill('Synthetic request to retry.')
  await send.click()
  await expect.poll(() => gateway.sends.length).toBe(1)
  await expect(page.getByRole('button', { name: 'Stop current response' })).toBeVisible()
  await expect(page.locator('.chat-composer-send-pending')).toContainText('Sending')
  await expect(page.locator('.msg-user')).toContainText('Synthetic request to retry.')

  gateway.rejectHeldSend()
  await expect(page.locator('.chat-thread')).toContainText(REJECTION)
  await expect(input).toHaveValue('Synthetic request to retry.')
  await expect(send).toBeEnabled()
  await expect(page.locator('.chat-composer-send-pending')).toHaveCount(0)
  await page.locator('.toast__action').filter({ hasText: 'Retry' }).click()
  await expect.poll(() => gateway.sends.length).toBe(2)
  expect(gateway.sends[1]?.message).toBe('Synthetic request to retry.')
  gateway.finish('succeeded')
  await expect(send).toBeEnabled()
})

for (const { changeAuthority, reload, label } of [
  { changeAuthority: false, reload: false, label: 'is sent once after the same identity reconnects' },
  { changeAuthority: true, reload: false, label: 'stays local when authority changes' },
  { changeAuthority: false, reload: true, label: 'survives a reload with the same proven identity' },
]) {
  test(`offline draft ${label}`, async ({ page }) => {
    const gateway = await installGateway(page, false, true)
    await openChat(page)
    const input = page.locator('.chat-textarea')
    const send = page.locator('.chat-send-btn[aria-label="Send"]')
    gateway.disconnect()
    await expect(page.locator('.conn-pill.connected')).toHaveCount(0)
    await input.fill('Synthetic never-sent offline draft.')
    await expect(send).toBeEnabled()
    await send.click()
    await expect(page.locator('.chat-pending-save-status')).toContainText('Saved locally')
    await expect(input).toHaveValue('')
    expect(gateway.sends).toHaveLength(0)
    expect(gateway.enqueues).toHaveLength(0)

    gateway.reconnect(changeAuthority)
    if (reload) await page.reload()
    await expect(page.locator('.conn-pill.connected')).toBeVisible()
    if (changeAuthority) {
      await expect(page.locator('.chat-pending-save-status')).toContainText('changed')
      expect(gateway.sends).toHaveLength(0)
      expect(gateway.enqueues).toHaveLength(0)
      expect(gateway.dispatches).toHaveLength(0)
    } else {
      await expect.poll(() => gateway.sends.length).toBe(1)
      expect(gateway.enqueues).toHaveLength(1)
      expect(gateway.dispatches).toHaveLength(1)
      expect(gateway.dispatches[0]?.pendingInputId).toBe(gateway.enqueues[0]?.pendingInputId)
      expect(gateway.sends[0]?.message).toBe('Synthetic never-sent offline draft.')
      gateway.finish('succeeded')
      await expect(send).toBeEnabled()
      expect(gateway.sends).toHaveLength(1)
    }
  })
}

test('a new browser tab recovers the original identity and durable offline draft', async ({ page }) => {
  test.skip(process.env.OPENSQUILLA_E2E_NATIVE_DESKTOP === '1', 'Browser tab replacement uses a browser context')
  const gateway = await installGateway(page, false, true)
  await openChat(page)
  gateway.disconnect()
  await expect(page.locator('.conn-pill.connected')).toHaveCount(0)
  await page.locator('.chat-textarea').fill('Synthetic durable draft for a replacement tab.')
  await page.locator('.chat-send-btn[aria-label="Send"]').click()
  await expect(page.locator('.chat-pending-save-status')).toContainText('Saved locally')
  const context = page.context()
  await page.close()
  const replacement = await context.newPage()
  try {
    const recovered = await installGateway(replacement, false, true)
    // Recovery dispatches the durable draft automatically; the idle Send
    // button can disappear before navigation readiness is observed.
    await openChat(replacement, 'running')
    await expect.poll(async () => ({
      sends: recovered.sends.length,
      enqueues: recovered.enqueues.length,
      cards: await replacement.locator('.chat-pending-card').count(),
      queued: await replacement.locator('.chat-pending-save-status').allTextContents(),
    })).toEqual({ sends: 1, enqueues: 1, cards: 0, queued: [] })
    expect(recovered.sends[0]?.message).toBe('Synthetic durable draft for a replacement tab.')
    expect(recovered.enqueues).toHaveLength(1)
    expect(recovered.dispatches).toHaveLength(1)
    recovered.finish('succeeded')
    await expect(replacement.locator('.chat-send-btn[aria-label="Send"]')).toBeEnabled()
  } finally { await replacement.close() }
})
