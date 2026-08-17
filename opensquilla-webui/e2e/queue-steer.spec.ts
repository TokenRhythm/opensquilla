import { expect, test, type Page } from '@playwright/test'

const CONTROL_URL = '/control/'
const SESSION_KEY = 'agent:main:webchat:e2e-queue-steer'
const TURN_ID = 'turn-e2e-queue-steer'
const ORIGINAL_TEXT = 'Original task that is still running'
const MESSAGE_TEXTBOX_NAME = /^(Message to send|要发送的消息)$/
const STEER_ACTION_NAME = /^(Steer|引导)$/
const RETRY_REJECTED_NAME = /^(Not sent · Retry|未发送 · 重试)$/
const RETRY_UNKNOWN_NAME = /^(Delivery status unknown · Retry confirmation|发送状态未知 · 重试确认)$/

type RpcRequest = {
  id?: string | number
  method?: string
  params?: Record<string, unknown>
  type?: string
}

type RpcError = {
  accepted?: boolean
  code: string
  details?: Record<string, unknown>
  message: string
  retryable?: boolean
}

type CapturedSteer = {
  params: Record<string, unknown>
  reject: (error: RpcError) => void
  resolve: (payload: Record<string, unknown>) => void
}

type MockGateway = {
  chatSendCalls: number
  emit: (event: string, payload: Record<string, unknown>) => void
  historyCalls: number
  historyMessages: Array<Record<string, unknown>>
  hydrateCalls: number
  steerRequests: CapturedSteer[]
}

function successResponse(id: string | number | undefined, payload: unknown) {
  return JSON.stringify({ type: 'res', id, ok: true, payload })
}

function errorResponse(id: string | number | undefined, error: RpcError) {
  return JSON.stringify({ type: 'res', id, ok: false, error })
}

function activeTask(includeCapability: boolean) {
  return {
    task_id: TURN_ID,
    status: 'running',
    ...(includeCapability
      ? {
          steer_capability: {
            mode: 'same_turn',
            expected_turn_id: TURN_ID,
            input_kinds: ['text'],
          },
        }
      : {}),
  }
}

function staleHistory() {
  return [{
    role: 'user',
    text: ORIGINAL_TEXT,
    message_id: 'message-e2e-original',
    timestamp: '2026-08-11T09:00:00Z',
    turn_context: { turn_id: TURN_ID },
  }]
}

async function installMockGateway(
  page: Page,
  options: { capabilityFromHydration?: boolean } = {},
): Promise<MockGateway> {
  const state: MockGateway = {
    chatSendCalls: 0,
    emit: () => { throw new Error('WebSocket is not connected') },
    historyCalls: 0,
    historyMessages: staleHistory(),
    hydrateCalls: 0,
    steerRequests: [],
  }

  await page.route('**/api/approvals', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ pending: [], mode: 'prompt', allowPatterns: [], denyPatterns: [] }),
  }))

  await page.routeWebSocket(/\/ws$/, ws => {
    state.emit = (event, payload) => {
      ws.send(JSON.stringify({ type: 'event', event, payload }))
    }
    state.emit('connect.challenge', {})

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
          policy: { tick_interval_ms: 30000, concurrent_history_reads: true },
          features: { methods: ['sessions.steer.v2'] },
          auth: {
            principal: { isOwner: true },
            runModePolicy: { allowedRunModes: ['safe', 'full'], defaultRunMode: 'full' },
          },
        }))
        return
      }

      if (method === 'chat.history') {
        state.historyCalls += 1
        ws.send(successResponse(frame.id, {
          messages: state.historyMessages,
          has_more: false,
          canonical_available: true,
          canonical_complete: true,
        }))
        return
      }

      if (method === 'sessions.messages.snapshot') {
        ws.send(successResponse(frame.id, {
          key: SESSION_KEY,
          events: [],
          current_stream_seq: 0,
        }))
        return
      }

      if (method === 'sessions.messages.subscribe') {
        ws.send(successResponse(frame.id, {
          subscribed: true,
          hydration_complete: false,
          replay_complete: true,
          current_stream_seq: 0,
          run_status: 'running',
          active_task: activeTask(!options.capabilityFromHydration),
        }))
        return
      }

      if (method === 'sessions.messages.hydrate') {
        state.hydrateCalls += 1
        ws.send(successResponse(frame.id, {
          subscribed: true,
          hydration_complete: true,
          replay_complete: true,
          current_stream_seq: 0,
          run_status: 'running',
          active_task: activeTask(true),
          workspaceId: null,
        }))
        return
      }

      if (method === 'sessions.steer.v2') {
        state.steerRequests.push({
          params: { ...(frame.params || {}) },
          reject: error => ws.send(errorResponse(frame.id, error)),
          resolve: payload => ws.send(successResponse(frame.id, payload)),
        })
        return
      }

      if (method === 'chat.send') {
        state.chatSendCalls += 1
        ws.send(errorResponse(frame.id, {
          accepted: false,
          code: 'UNEXPECTED_CHAT_SEND',
          message: 'A queued steer must not fall back to chat.send',
          retryable: false,
        }))
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
            title: 'Queue steer regression',
            sessionKind: 'chat',
            surface: 'webchat',
            conversationKind: 'direct',
            effectiveAgentId: 'main',
            updatedAt: 100,
            messageCount: 1,
            status: 'ok',
            runStatus: 'running',
          }],
          has_more: false,
        },
        'sessions.messages.unsubscribe': { subscribed: false },
        'sessions.subscribe': { subscribed: true },
        'usage.status': { sessions: [] },
      }
      ws.send(successResponse(frame.id, payloads[method] ?? {}))
    })
  })

  return state
}

async function openRunningSession(page: Page) {
  await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(SESSION_KEY))
  await expect(page.locator('.conn-pill.connected')).toBeVisible({ timeout: 10000 })
  await expect(page.getByRole('textbox', { name: MESSAGE_TEXTBOX_NAME })).toBeEditable()
  await expect(page.getByRole('button', { name: /^(Stop .*response|停止.*回复)$/ })).toBeVisible()
  await expect(page.getByText(ORIGINAL_TEXT, { exact: true })).toBeVisible()
}

async function queueAndSteer(page: Page, state: MockGateway, text: string) {
  const composer = page.getByRole('textbox', { name: MESSAGE_TEXTBOX_NAME })
  await composer.fill(text)
  // While a turn is running the action button is Stop. Enter is the supported
  // composer submission path and queues the draft without stopping the turn.
  await composer.press('Enter')

  const card = page.locator('.chat-pending-card').filter({ hasText: text })
  await expect(card).toBeVisible()
  const steer = card.getByRole('button', { name: STEER_ACTION_NAME })
  await expect(steer).toBeEnabled()
  const requestIndex = state.steerRequests.length
  await steer.click()
  await expect.poll(() => state.steerRequests.length).toBe(requestIndex + 1)
  return card
}

function acceptedSteer(
  request: CapturedSteer,
  disposition: 'applied' | 'promoted',
): Record<string, unknown> {
  const promoted = disposition === 'promoted'
  return {
    accepted: true,
    replayed: true,
    key: SESSION_KEY,
    turn_id: promoted ? 'turn-e2e-promoted' : TURN_ID,
    target_turn_id: TURN_ID,
    promoted_turn_id: promoted ? 'turn-e2e-promoted' : undefined,
    promoted_from_turn_id: promoted ? TURN_ID : undefined,
    user_message_id: 'message-e2e-steer',
    client_request_id: request.params.client_request_id,
    client_message_id: request.params.client_message_id,
    disposition,
    revision: 2,
  }
}

test.describe('Queue/Steer composer semantics', () => {
  for (const viewport of [
    { label: 'desktop', width: 1280, height: 720 },
    { label: 'narrow', width: 390, height: 720 },
  ]) {
    test(`a long queued message stays inside the composer width and ellipsizes at ${viewport.label} width`, async ({ page }) => {
      await page.setViewportSize({ width: viewport.width, height: viewport.height })
      await installMockGateway(page)
      await openRunningSession(page)

      const longText = 'queued-message-without-breaks-'.repeat(80)
      const composer = page.getByRole('textbox', { name: MESSAGE_TEXTBOX_NAME })
      await composer.fill(longText)
      await composer.press('Enter')

      const card = page.locator('.chat-pending-card').filter({ hasText: longText })
      const pendingText = card.locator('.chat-pending-text')
      await expect(card).toBeVisible()
      const layout = await page.evaluate(() => {
        const card = document.querySelector('.chat-pending-card') as HTMLElement
        const queue = document.querySelector('.chat-pending') as HTMLElement
        const text = document.querySelector('.chat-pending-text') as HTMLElement
        const cardRect = card.getBoundingClientRect()
        const queueRect = queue.getBoundingClientRect()
        return {
          cardLeft: cardRect.left,
          cardRight: cardRect.right,
          queueLeft: queueRect.left,
          queueRight: queueRect.right,
          textClientWidth: text.clientWidth,
          textScrollWidth: text.scrollWidth,
          textOverflow: getComputedStyle(text).textOverflow,
          whiteSpace: getComputedStyle(text).whiteSpace,
        }
      })
      expect(layout.cardLeft).toBeGreaterThanOrEqual(layout.queueLeft - 0.5)
      expect(layout.cardRight).toBeLessThanOrEqual(layout.queueRight + 0.5)
      expect(layout.textScrollWidth).toBeGreaterThan(layout.textClientWidth)
      expect(layout.textOverflow).toBe('ellipsis')
      expect(layout.whiteSpace).toBe('nowrap')
      await expect(pendingText).toHaveAttribute('title', longText)
    })
  }

  test('STORAGE_BUSY and an unknown response retain one exact-id Retry', async ({ page }) => {
    const state = await installMockGateway(page)
    await openRunningSession(page)

    const steerText = 'Apply this adjustment to the active turn'
    const card = await queueAndSteer(page, state, steerText)
    const userBubble = page.locator('.msg-user').filter({ hasText: steerText })
    await expect(card).toHaveAttribute('aria-busy', 'true')
    await expect(userBubble).toHaveCount(0)

    state.steerRequests[0]!.reject({
      accepted: false,
      code: 'STORAGE_BUSY',
      details: { fallback_safe: false },
      message: 'Session storage is temporarily busy',
      retryable: true,
    })
    await expect(card.getByRole('button', { name: RETRY_REJECTED_NAME })).toBeEnabled()
    await expect(userBubble).toHaveCount(0)

    const unrelatedDraft = 'Unrelated composer draft must survive the exact retry'
    const composer = page.getByRole('textbox', { name: MESSAGE_TEXTBOX_NAME })
    await composer.fill(unrelatedDraft)

    await card.getByRole('button', { name: RETRY_REJECTED_NAME }).click()
    await expect.poll(() => state.steerRequests.length).toBe(2)
    state.steerRequests[1]!.reject({
      code: 'STEER_RESPONSE_LOST',
      message: 'The acceptance response was lost',
      retryable: true,
    })
    await expect(card.getByRole('button', { name: RETRY_UNKNOWN_NAME })).toBeEnabled()
    await expect(userBubble).toHaveCount(0)

    await card.getByRole('button', { name: RETRY_UNKNOWN_NAME }).click()
    await expect.poll(() => state.steerRequests.length).toBe(3)
    const attempts = state.steerRequests
    expect(attempts[0]!.params).toMatchObject({
      key: SESSION_KEY,
      message: steerText,
      expected_turn_id: TURN_ID,
      client_request_id: expect.any(String),
      client_message_id: expect.any(String),
    })
    expect(attempts[1]!.params).toEqual(attempts[0]!.params)
    expect(attempts[2]!.params).toEqual(attempts[0]!.params)
    attempts[2]!.resolve(acceptedSteer(attempts[2]!, 'applied'))

    await expect(card).toHaveCount(0)
    await expect(userBubble).toHaveCount(1)
    await expect(userBubble.locator('.msg-user-steer-status--applied')).toHaveText(/^(Steer|引导)$/)
    await expect(composer).toHaveValue(unrelatedDraft)
    expect(state.chatSendCalls).toBe(0)
  })

  test('terminal stale history cannot erase a delayed accepted steer', async ({ page }) => {
    const state = await installMockGateway(page)
    await openRunningSession(page)

    const steerText = 'Make the ending shorter but keep the active response'
    const card = await queueAndSteer(page, state, steerText)
    const userBubble = page.locator('.msg-user').filter({ hasText: steerText })
    await expect(card).toHaveAttribute('aria-busy', 'true')
    await expect(userBubble).toHaveCount(0)

    const historyCallsBeforeTerminal = state.historyCalls
    state.emit('session.event.done', {
      key: SESSION_KEY,
      session_key: SESSION_KEY,
      task_id: TURN_ID,
      turn_id: TURN_ID,
      stream_seq: 1,
      status: 'succeeded',
      reason: 'completed',
      text_snapshot: 'Original completed answer.',
    })
    await expect.poll(() => state.historyCalls).toBeGreaterThan(historyCallsBeforeTerminal)
    await expect(userBubble).toHaveCount(0)
    await expect(page.getByRole('button', { name: /^(Stop .*response|停止.*回复)$/ })).toHaveCount(0)

    const request = state.steerRequests[0]!
    request.resolve(acceptedSteer(request, 'promoted'))
    await expect(card).toHaveCount(0)
    await expect(userBubble).toHaveCount(1)
    await expect(userBubble.locator('.msg-user-steer-status--promoted')).toHaveText(
      /^(Queued for the next turn|已排到下一轮)$/,
    )
    await expect(page.locator('.chat-pending-action--steer')).toHaveCount(0)
    expect(state.chatSendCalls).toBe(0)
  })

  test('canonical hydration reconciles an unknown steer without another send', async ({ page }) => {
    const state = await installMockGateway(page)
    await openRunningSession(page)

    const steerText = 'Use the durable history row after reconnect'
    const card = await queueAndSteer(page, state, steerText)
    const request = state.steerRequests[0]!
    const userBubble = page.locator('.msg-user').filter({ hasText: steerText })
    state.historyMessages = [
      ...staleHistory(),
      {
        role: 'user',
        text: steerText,
        message_id: 'message-e2e-hydrated-steer',
        timestamp: '2026-08-11T09:00:01Z',
        turn_context: {
          turn_id: TURN_ID,
          intent: 'steer',
          disposition: 'applied',
          revision: 2,
          client_request_id: request.params.client_request_id,
          client_message_id: request.params.client_message_id,
        },
      },
    ]
    const historyCallsBeforeUnknown = state.historyCalls

    request.reject({
      code: 'STEER_RESPONSE_LOST',
      message: 'The acceptance response was lost',
      retryable: true,
    })

    await expect.poll(() => state.historyCalls).toBeGreaterThan(historyCallsBeforeUnknown)
    await expect(card).toHaveCount(0)
    await expect(userBubble).toHaveCount(1)
    await expect(userBubble.locator('.msg-user-steer-status--applied')).toHaveText(/^(Steer|引导)$/)
    expect(state.steerRequests).toHaveLength(1)
    expect(state.chatSendCalls).toBe(0)
  })

  test('hydrated capability can steer and fallback safely to the visible queue', async ({ page }) => {
    const state = await installMockGateway(page, { capabilityFromHydration: true })
    await openRunningSession(page)
    await expect.poll(() => state.hydrateCalls).toBeGreaterThan(0)

    const steerText = 'Keep this message visible if the turn closes'
    const card = await queueAndSteer(page, state, steerText)
    const userBubble = page.locator('.msg-user').filter({ hasText: steerText })
    await expect(card).toHaveAttribute('aria-busy', 'true')
    await expect(userBubble).toHaveCount(0)

    state.steerRequests[0]!.resolve({
      accepted: false,
      key: SESSION_KEY,
      expected_turn_id: TURN_ID,
      failure_code: 'NO_ACTIVE_TURN',
      fallback_safe: true,
      disposition: 'rejected',
    })

    await expect(userBubble).toHaveCount(0)
    await expect(card).toBeVisible()
    await expect(card).toContainText(steerText)
    await expect(card.getByRole('button', { name: STEER_ACTION_NAME })).toBeEnabled()
    await expect(card.getByRole('button', { name: /Retry|重试/ })).toHaveCount(0)
    await expect(page.getByRole('textbox', { name: MESSAGE_TEXTBOX_NAME })).toHaveValue('')
    expect(state.chatSendCalls).toBe(0)
  })
})
