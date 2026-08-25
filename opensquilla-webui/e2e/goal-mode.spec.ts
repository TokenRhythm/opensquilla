import { expect, test, type Page } from '@playwright/test'

import { startRealGoalGateway } from './real-goal-gateway'
import { test as isolatedGatewayTest } from './real-gateway.fixture'

const CONTROL_URL = '/control/'
const SESSION_KEY = 'agent:main:webchat:e2e-goal-mode'
const SESSION_ID = 'session-e2e-goal-mode'
const GOAL_ID = 'goal-e2e-mocked-snapshots'
const GOAL_SOURCE_MESSAGE_ID = 'message-goal-source'
const OBJECTIVE = 'Produce and verify a deterministic release report'
const REAL_FIRST_REPLY = 'The release inputs are inspected; final verification still remains.'
const REAL_FINAL_REPLY = 'The deterministic release report is complete and verified.'
const LIFECYCLE_FIRST_REPLY = 'Task one completed after the lifecycle checks.'
const LIFECYCLE_SECOND_REPLY = 'Task two completed after Goal removal.'

type GoalProgress = {
  explanation: string | null
  steps: Array<{
    text: string
    status: 'pending' | 'in_progress' | 'completed'
  }>
}

type GoalFixture = ReturnType<typeof goalSnapshot>

type MockGoalGateway = {
  acceptGoal: () => void
  emitGoal: (goal: GoalFixture) => void
  methods: string[]
  setParams: Array<Record<string, unknown>>
}

function response(id: string | number | undefined, payload: unknown) {
  return JSON.stringify({ type: 'res', id, ok: true, payload })
}

function goalSnapshot(overrides: Record<string, unknown> = {}) {
  return {
    goalId: GOAL_ID,
    sessionKey: SESSION_KEY,
    sessionId: SESSION_ID,
    epoch: 1,
    objective: OBJECTIVE,
    status: 'active',
    stateRevision: 1,
    objectiveRevision: 1,
    progressRevision: 0,
    progress: null as GoalProgress | null,
    continuationSeq: 0,
    activeTaskId: 'task-goal-first-turn',
    sourceMessageId: GOAL_SOURCE_MESSAGE_ID,
    executionState: 'working',
    continuationDeferredReason: null,
    turnsStarted: 1,
    turnsSettled: 0,
    windowTurnsStarted: 1,
    activeTimeMs: 0,
    windowActiveTimeMs: 0,
    usage: {
      inputTokens: 0,
      outputTokens: 0,
      reasoningTokens: 0,
      cacheReadTokens: 0,
      cacheWriteTokens: 0,
      totalTokens: 0,
    },
    pauseReason: null,
    blockedReason: null,
    terminalReason: null,
    createdAt: 1_000,
    updatedAt: 1_000,
    finishedAt: null,
    ...overrides,
  }
}

async function installStableHttpStubs(page: Page): Promise<void> {
  await page.route('**/api/approvals', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ pending: [] }),
  }))
  await page.route('**/api/elevated-mode', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ enabled: false }),
  }))
  await page.route('**/api/system/update', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      current: '0.0.0-e2e',
      latest: null,
      available: false,
      url: null,
      checkedAt: null,
    }),
  }))
}

async function installFakeGoalGateway(page: Page): Promise<MockGoalGateway> {
  const methods: string[] = []
  const setParams: Array<Record<string, unknown>> = []
  let sendFrame: ((frame: string) => void) | null = null
  let pendingGoalRequest: {
    id: string | number | undefined
    params: Record<string, unknown>
  } | null = null
  let streamSeq = 0

  await page.addInitScript(() => {
    window.localStorage.setItem('opensquilla-locale', 'en')
  })
  await installStableHttpStubs(page)
  await page.routeWebSocket(/\/ws$/, ws => {
    sendFrame = frame => ws.send(frame)
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
      methods.push(method)

      if (method === 'connect') {
        ws.send(JSON.stringify({
          type: 'hello-ok',
          protocol: 3,
          server: { version: 'e2e', conn_id: 'goal-mode-fake-gateway' },
          features: {
            methods: ['goals.capabilities', 'goals.set'],
            events: ['session.event.goal'],
          },
          snapshot: {},
          policy: { tick_interval_ms: 30_000 },
          auth: { principal: { isOwner: true } },
        }))
        return
      }
      if (method === 'goals.set') {
        const params = frame.params && typeof frame.params === 'object'
          ? frame.params as Record<string, unknown>
          : {}
        setParams.push(params)
        pendingGoalRequest = {
          id: frame.id as string | number | undefined,
          params,
        }
        return
      }

      const payloads: Record<string, unknown> = {
        'agents.list': { agents: [] },
        'chat.history': {
          messages: [],
          has_more: false,
          canonical_complete: true,
        },
        'commands.list_for_surface': {
          commands: [{
            name: '/goal',
            cmd: '/goal',
            label: '/goal',
            description: 'Set a persistent goal.',
            aliases: [],
            execution: { action: 'goal.set' },
          }],
        },
        'config.get': {
          squilla_router: { enabled: false, rollout_phase: 'observe', tiers: {} },
          permissions: {},
          skills: {},
        },
        'goals.capabilities': {
          supported: true,
          executionEnabled: true,
          maxTurns: 50,
          runtimeBudgetSeconds: 3_600,
          methods: ['goals.set'],
        },
        'models.routing.get': { mode: 'direct' },
        'onboarding.status': { audioConfigured: false },
        'sessions.list': { sessions: [], has_more: false },
        'sessions.messages.snapshot': {
          key: SESSION_KEY,
          events: [],
          current_stream_seq: 0,
        },
        'sessions.messages.subscribe': {
          key: SESSION_KEY,
          sessionId: SESSION_ID,
          epoch: 1,
          subscribed: true,
          hydration_complete: false,
          replay_complete: true,
          current_stream_seq: 0,
          run_status: 'idle',
          goal: null,
          goalSnapshotStreamSeq: null,
          deferred_fields: ['goal', 'goalSnapshotStreamSeq'],
        },
        'sessions.messages.hydrate': {
          key: SESSION_KEY,
          sessionId: SESSION_ID,
          epoch: 1,
          hydration_complete: true,
          run_status: 'idle',
          goal: null,
          goalSnapshotStreamSeq: 0,
        },
        'usage.status': { sessions: [] },
      }
      ws.send(response(
        frame.id as string | number | undefined,
        payloads[method] ?? {},
      ))
    })
  })

  return {
    methods,
    setParams,
    acceptGoal() {
      if (!sendFrame || !pendingGoalRequest) {
        throw new Error('fake Goal gateway has no pending goals.set request')
      }
      const { id, params } = pendingGoalRequest
      pendingGoalRequest = null
      sendFrame(response(id, {
        accepted: true,
        clientRequestId: params.clientRequestId,
        sessionKey: SESSION_KEY,
        sessionId: SESSION_ID,
        epoch: 1,
        taskId: 'task-goal-first-turn',
        userMessageId: GOAL_SOURCE_MESSAGE_ID,
        previousGoalId: null,
        goal: goalSnapshot(),
      }))
    },
    emitGoal(goal) {
      if (!sendFrame) throw new Error('fake Goal gateway is not connected')
      streamSeq += 1
      sendFrame(JSON.stringify({
        type: 'event',
        event: 'session.event.goal',
        payload: {
          session_key: SESSION_KEY,
          session_id: SESSION_ID,
          epoch: 1,
          stream_seq: streamSeq,
          event_type: 'updated',
          state_revision: goal.stateRevision,
          progress_revision: goal.progressRevision,
          previous_goal_id: null,
          goal,
        },
      }))
    },
  }
}

test('Goal mode renders mocked continuation snapshots without correctness polling', async ({ page }) => {
  const gateway = await installFakeGoalGateway(page)
  await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(SESSION_KEY))
  await expect(page.locator('.conn-pill.connected')).toBeVisible({ timeout: 10_000 })
  const composer = page.locator('.chat-textarea')
  await expect(composer).toBeEditable({ timeout: 10_000 })

  // The composer can become editable just before the asynchronous slash
  // catalog is adopted. Fence on the visible command instead of racing that
  // bootstrap work (or freezing it behind a virtual clock).
  await composer.fill('/goal')
  await expect(page.locator('.chat-slash-item').filter({ hasText: '/goal' })).toBeVisible()
  await composer.fill(`/goal ${OBJECTIVE}`)
  await page.locator('.chat-send-btn[aria-label="Send"]').click()

  await expect.poll(() => gateway.methods).toContain('goals.set')
  await expect.poll(() => gateway.setParams).toHaveLength(1)
  await expect(page.locator('.msg-user')).toHaveCount(0)
  gateway.acceptGoal()
  const ribbon = page.locator('.goal-ribbon')
  await expect(ribbon).toBeVisible()
  await expect(ribbon).toContainText('Goal in progress')
  await expect(ribbon).toContainText(OBJECTIVE)
  await expect(ribbon).toContainText('working')
  expect(gateway.setParams[0]).toMatchObject({
    sessionKey: SESSION_KEY,
    objective: OBJECTIVE,
  })
  expect(gateway.setParams[0]?.clientRequestId).toMatch(/^[0-9a-f-]{36}$/)
  expect(gateway.setParams[0]?.clientMessageId).toMatch(/^[0-9a-f-]{36}$/)

  const goalSource = page.locator('.msg-user').filter({
    has: page.locator('.msg-user-bubble').filter({ hasText: OBJECTIVE }),
  })
  await expect(goalSource).toHaveCount(1)
  await expect(goalSource).toHaveAttribute('data-message-id', GOAL_SOURCE_MESSAGE_ID)
  await expect(goalSource.locator('.msg-user-bubble')).toHaveText(OBJECTIVE)
  await expect(goalSource.locator('.msg-user-goal-origin')).toContainText('Sent as goal')
  await expect(page.locator('.msg-ai')).toHaveCount(0)

  // The first provider turn settles and the first automatic continuation owns
  // task 2. Structured progress is durable Goal state, not assistant prose.
  gateway.emitGoal(goalSnapshot({
    stateRevision: 2,
    progressRevision: 1,
    progress: {
      explanation: 'The fake provider inspected the release inputs.',
      steps: [
        { text: 'Inspect release inputs', status: 'completed' },
        { text: 'Draft the report', status: 'in_progress' },
        { text: 'Verify the report', status: 'pending' },
      ],
    },
    continuationSeq: 1,
    activeTaskId: 'task-goal-auto-1',
    turnsStarted: 2,
    turnsSettled: 1,
    windowTurnsStarted: 2,
    activeTimeMs: 2_000,
    windowActiveTimeMs: 2_000,
    usage: {
      inputTokens: 40,
      outputTokens: 20,
      reasoningTokens: 5,
      cacheReadTokens: 0,
      cacheWriteTokens: 0,
      totalTokens: 65,
    },
    updatedAt: 2_000,
  }))
  await expect(ribbon).toContainText('1 turns')
  await expect(ribbon.locator('summary')).toHaveText('Progress 1/3')
  await ribbon.locator('summary').click()
  await expect(ribbon.getByText('Inspect release inputs')).toBeVisible()
  await expect(ribbon.getByText('Draft the report')).toBeVisible()

  // The first automatic turn settles and a second automatic continuation owns
  // task 3. The UI must adopt the newer state/progress revisions from one event.
  gateway.emitGoal(goalSnapshot({
    stateRevision: 3,
    progressRevision: 2,
    progress: {
      explanation: 'The fake provider drafted the report and started verification.',
      steps: [
        { text: 'Inspect release inputs', status: 'completed' },
        { text: 'Draft the report', status: 'completed' },
        { text: 'Verify the report', status: 'in_progress' },
      ],
    },
    continuationSeq: 2,
    activeTaskId: 'task-goal-auto-2',
    turnsStarted: 3,
    turnsSettled: 2,
    windowTurnsStarted: 3,
    activeTimeMs: 6_000,
    windowActiveTimeMs: 6_000,
    usage: {
      inputTokens: 100,
      outputTokens: 80,
      reasoningTokens: 20,
      cacheReadTokens: 10,
      cacheWriteTokens: 0,
      totalTokens: 210,
    },
    updatedAt: 3_000,
  }))
  await expect(ribbon).toContainText('2 turns')
  await expect(ribbon.locator('summary')).toHaveText('Progress 2/3')
  await expect(ribbon.locator('li[data-status="completed"]')).toHaveCount(2)
  await expect(ribbon.locator('li[data-status="in_progress"]')).toHaveCount(1)

  // Keep the Goal active beyond the retired five-second correctness-poll
  // interval. Any reintroduced poll would be observable as goals.status.
  await page.waitForTimeout(5_250)
  const forbiddenGoalMethods = ['goals.observe', 'goals.unobserve', 'goals.status']
  expect(gateway.methods.filter(method => forbiddenGoalMethods.includes(method))).toEqual([])
  expect(gateway.methods.filter(method => method === 'goals.set')).toHaveLength(1)

  gateway.emitGoal(goalSnapshot({
    status: 'complete',
    stateRevision: 4,
    progressRevision: 3,
    progress: {
      explanation: 'The fake provider verified the final report.',
      steps: [
        { text: 'Inspect release inputs', status: 'completed' },
        { text: 'Draft the report', status: 'completed' },
        { text: 'Verify the report', status: 'completed' },
      ],
    },
    continuationSeq: 2,
    activeTaskId: null,
    executionState: 'idle',
    turnsStarted: 3,
    turnsSettled: 3,
    windowTurnsStarted: 3,
    activeTimeMs: 9_000,
    windowActiveTimeMs: 9_000,
    usage: {
      inputTokens: 150,
      outputTokens: 140,
      reasoningTokens: 30,
      cacheReadTokens: 20,
      cacheWriteTokens: 20,
      totalTokens: 360,
    },
    terminalReason: 'complete',
    updatedAt: 4_000,
    finishedAt: 4_000,
  }))

  await expect(ribbon).toHaveCount(0)
  const outcome = page.locator('.goal-outcome[data-status="complete"]')
  await expect(outcome).toBeVisible()
  await expect(outcome).toContainText('Goal complete')
  await expect(outcome).toContainText(OBJECTIVE)
  await expect(outcome).toContainText('3 turns')
  await expect(outcome).toContainText('360 tokens')
  expect(gateway.methods.filter(method => forbiddenGoalMethods.includes(method))).toEqual([])
})

test('Composer Add menu stays above active Goal progress across responsive layouts', async ({ page }) => {
  await page.setViewportSize({ width: 1368, height: 546 })
  await page.emulateMedia({ colorScheme: 'dark', reducedMotion: 'no-preference' })
  const gateway = await installFakeGoalGateway(page)
  await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(SESSION_KEY))
  await expect(page.locator('.conn-pill.connected')).toBeVisible({ timeout: 10_000 })
  await expect(page.locator('.chat-textarea')).toBeEditable({ timeout: 10_000 })

  gateway.emitGoal(goalSnapshot({
    stateRevision: 2,
    objective: 'Verify a responsive Goal workflow with a deliberately long objective that wraps on narrow windows without covering the Composer Add menu.',
  }))
  const goalDock = page.locator('.goal-run-dock')
  await expect(goalDock).toBeVisible()

  const addButton = page.getByRole('button', { name: 'Add', exact: true })
  await addButton.click()
  const addMenu = page.locator('.composer-add-menu')
  await expect(addMenu).toBeVisible()

  const assertClearance = async () => {
    await expect.poll(async () => page.evaluate(() => {
      const menu = document.querySelector<HTMLElement>('.composer-add-menu')
      const goal = document.querySelector<HTMLElement>('.goal-run-dock')
      if (!menu || !goal) return -1
      return Math.floor(goal.getBoundingClientRect().top - menu.getBoundingClientRect().bottom)
    })).toBeGreaterThanOrEqual(8)

    const frames = await page.evaluate(async () => {
      const samples: Array<{ clearance: number; menuTop: number }> = []
      for (let index = 0; index < 12; index += 1) {
        await new Promise<void>(resolve => requestAnimationFrame(() => resolve()))
        const menu = document.querySelector<HTMLElement>('.composer-add-menu')!
        const goal = document.querySelector<HTMLElement>('.goal-run-dock')!
        samples.push({
          clearance: goal.getBoundingClientRect().top - menu.getBoundingClientRect().bottom,
          menuTop: menu.getBoundingClientRect().top,
        })
      }
      return samples
    })
    expect(frames.every(frame => frame.clearance >= 7)).toBe(true)
    expect(frames.every(frame => frame.menuTop >= 7)).toBe(true)
  }

  await assertClearance()
  await page.setViewportSize({ width: 390, height: 844 })
  await page.emulateMedia({ colorScheme: 'light', reducedMotion: 'reduce' })
  await assertClearance()

  await addMenu.getByRole('menuitem', { name: /Attach files/ }).focus()
  await expect(addMenu.getByRole('menuitem', { name: /Attach files/ })).toBeFocused()
  await addMenu.press('Escape')
  await expect(addMenu).toHaveCount(0)

  await page.evaluate(() => {
    localStorage.setItem('opensquilla.composerFx', JSON.stringify({ enabled: false }))
  })
  await page.reload()
  await expect(page.locator('.conn-pill.connected')).toBeVisible({ timeout: 10_000 })
  await expect(page.locator('.chat-textarea')).toBeEditable({ timeout: 10_000 })
  await expect(page.locator('.chat')).not.toHaveClass(/chat--composer-floating/)

  gateway.emitGoal(goalSnapshot({
    stateRevision: 3,
    objective: 'Verify the same Goal and Add menu clearance in the original docked Composer layout.',
  }))
  await expect(goalDock).toBeVisible()
  await addButton.click()
  await expect(addMenu).toBeVisible()
  await assertClearance()

  gateway.emitGoal(goalSnapshot({
    status: 'complete',
    stateRevision: 4,
    activeTaskId: null,
    executionState: 'idle',
    turnsSettled: 1,
    terminalReason: 'complete',
    finishedAt: 4_000,
  }))
  await expect(goalDock).toHaveCount(0)
  await expect.poll(async () => page.evaluate(() => {
    const menu = document.querySelector<HTMLElement>('.composer-add-menu')
    const button = document.querySelector<HTMLElement>('.chat-plus-btn')
    if (!menu || !button) return 1
    return Math.ceil(menu.getBoundingClientRect().bottom - button.getBoundingClientRect().top)
  })).toBeLessThanOrEqual(-7)
})

test('Goal mode continues through a real Gateway, refresh, and deterministic provider', async ({
  page,
  baseURL,
}, testInfo) => {
  test.setTimeout(90_000)
  if (!baseURL) throw new Error('Goal browser E2E requires a Playwright baseURL')
  expect(OBJECTIVE.toLowerCase()).not.toMatch(
    /(?:first|second)\s+(?:turn|round)|(?:phase|stage)\s+(?:one|two)/,
  )

  const gateway = await startRealGoalGateway({
    outputDir: testInfo.outputPath('real-goal-gateway'),
    webuiOrigin: new URL(baseURL).origin,
  })
  try {
    const sentRpcMethods: string[] = []
    const rpcFrames: Array<Record<string, unknown> & { direction: 'sent' | 'received' }> = []
    page.on('websocket', socket => {
      socket.on('framesent', ({ payload }) => {
        try {
          const frame = JSON.parse(String(payload)) as { type?: string; method?: string }
          rpcFrames.push({ ...frame, direction: 'sent' })
          if (frame.type === 'req' && frame.method) sentRpcMethods.push(frame.method)
        } catch {
          // Binary/non-JSON frames are outside the RPC contract under test.
        }
      })
      socket.on('framereceived', ({ payload }) => {
        try {
          const frame = JSON.parse(String(payload)) as Record<string, unknown>
          rpcFrames.push({ ...frame, direction: 'received' })
        } catch {
          // Binary/non-JSON frames are outside the RPC contract under test.
        }
      })
    })
    await page.addInitScript((wsUrl) => {
      window.localStorage.setItem('opensquilla-locale', 'en')
      window.localStorage.setItem('opensquilla.wsUrl', wsUrl)
    }, gateway.wsUrl)
    await installStableHttpStubs(page)

    await page.goto(CONTROL_URL + 'chat')
    await expect(page.locator('.conn-pill.connected')).toBeVisible({ timeout: 15_000 })
    const composer = page.locator('.chat-textarea')
    await expect(composer).toBeEditable({ timeout: 15_000 })
    await composer.fill('/goal')
    await expect(page.locator('.chat-slash-item').filter({ hasText: '/goal' }))
      .toBeVisible({ timeout: 15_000 })
    await composer.fill(`/goal ${OBJECTIVE}`)
    await page.locator('.chat-send-btn[aria-label="Send"]').click()
    await expect.poll(() => [...sentRpcMethods], { timeout: 10_000 })
      .toContain('goals.set')
    await expect.poll(() => {
      const request = rpcFrames.find(frame => (
        frame.direction === 'sent' && frame.type === 'req' && frame.method === 'goals.set'
      ))
      const response = request && rpcFrames.find(frame => (
        frame.direction === 'received' && frame.type === 'res' && frame.id === request.id
      ))
      return Boolean(response)
    }, { timeout: 10_000 }).toBe(true)
    const goalSetRequest = rpcFrames.find(frame => (
      frame.direction === 'sent' && frame.type === 'req' && frame.method === 'goals.set'
    ))
    const goalSetResponse = goalSetRequest && rpcFrames.find(frame => (
      frame.direction === 'received' && frame.type === 'res' && frame.id === goalSetRequest.id
    ))
    if (goalSetResponse?.ok !== true) {
      throw new Error(`goals.set RPC rejected: ${JSON.stringify(goalSetResponse)}`)
    }
    const goalSetPayload = goalSetResponse.payload && typeof goalSetResponse.payload === 'object'
      ? goalSetResponse.payload as Record<string, unknown>
      : null
    const acceptedGoal = goalSetPayload?.goal && typeof goalSetPayload.goal === 'object'
      ? goalSetPayload.goal as Record<string, unknown>
      : null
    const sourceMessageId = String(goalSetPayload?.userMessageId || '')
    expect(sourceMessageId).not.toBe('')
    expect(acceptedGoal?.sourceMessageId).toBe(sourceMessageId)
    const goalSource = page.locator('.msg-user').filter({
      has: page.locator('.msg-user-bubble').filter({ hasText: OBJECTIVE }),
    })
    await expect(goalSource).toHaveCount(1)
    await expect(goalSource).toHaveAttribute('data-message-id', sourceMessageId)
    await expect(goalSource.locator('.msg-user-bubble')).toHaveText(OBJECTIVE)
    await expect(goalSource.locator('.msg-user-goal-origin')).toContainText('Sent as goal')
    const createIndex = rpcFrames.findIndex(frame => (
      frame.direction === 'sent' && frame.type === 'req' && frame.method === 'sessions.create'
    ))
    const subscribeIndex = rpcFrames.findIndex((frame, index) => (
      index > createIndex
      && frame.direction === 'sent'
      && frame.type === 'req'
      && frame.method === 'sessions.messages.subscribe'
    ))
    const setIndex = rpcFrames.findIndex(frame => (
      frame.direction === 'sent' && frame.type === 'req' && frame.method === 'goals.set'
    ))
    expect(createIndex).toBeGreaterThanOrEqual(0)
    expect(subscribeIndex).toBeGreaterThan(createIndex)
    expect(setIndex).toBeGreaterThan(subscribeIndex)
    const createRequest = rpcFrames[createIndex]
    const createResponse = rpcFrames.find(frame => (
      frame.direction === 'received'
      && frame.type === 'res'
      && frame.id === createRequest?.id
    ))
    const createPayload = createResponse?.payload && typeof createResponse.payload === 'object'
      ? createResponse.payload as Record<string, unknown>
      : null
    const createdSessionKey = String(createPayload?.key || '')
    expect(createdSessionKey).not.toBe('')
    expect((rpcFrames[subscribeIndex]?.params as Record<string, unknown>)?.key)
      .toBe(createdSessionKey)
    expect((rpcFrames[setIndex]?.params as Record<string, unknown>)?.sessionKey)
      .toBe(createdSessionKey)

    // Provider call 2 is the first request from the automatically-created
    // second AgentTask. It waits on a file until the browser has inspected the
    // durable intermediate state, so this assertion cannot race completion.
    await expect.poll(
      async () => (await gateway.readProviderCalls()).map(call => call.callNumber),
      { timeout: 30_000 },
    ).toEqual([1, 2])

    const callsDuringContinuation = await gateway.readProviderCalls()
    expect(callsDuringContinuation[0]?.toolNames).toEqual(expect.arrayContaining([
      'update_goal',
      'update_goal_progress',
    ]))
    expect(callsDuringContinuation[1]?.toolNames).toEqual(expect.arrayContaining([
      'update_goal',
      'update_goal_progress',
    ]))
    expect(callsDuringContinuation[1]).toMatchObject({
      callNumber: 2,
      objectiveInRequestContext: true,
      progressIsNull: true,
      firstReplyInAssistantHistory: true,
      requestHasInternalContinuation: true,
    })
    await expect(goalSource).toHaveCount(1)
    await expect(goalSource).toHaveAttribute('data-message-id', sourceMessageId)

    let ribbon = page.locator('.goal-ribbon')
    await expect(ribbon).toBeVisible({ timeout: 15_000 })
    await expect(ribbon).toContainText('Goal in progress')
    await expect(ribbon).toContainText(OBJECTIVE)
    await expect(ribbon).toContainText('1 turns')
    await expect(ribbon.locator('.goal-ribbon__progress')).toHaveCount(0)
    await expect(page.locator('.msg-ai').filter({ hasText: REAL_FIRST_REPLY })).toBeVisible()

    // Reload while Task 2 is blocked inside the real provider. The new page
    // must reconnect, hydrate the persisted Goal snapshot/transcript, and keep
    // receiving the eventual terminal events from the same Gateway process.
    const reattachCountBeforeReload = sentRpcMethods
      .filter(method => method === 'goals.reattach').length
    await page.reload()
    await expect(page.locator('.conn-pill.connected')).toBeVisible({ timeout: 15_000 })
    await expect.poll(
      () => sentRpcMethods.filter(method => method === 'goals.reattach').length,
      { timeout: 15_000 },
    ).toBe(reattachCountBeforeReload + 1)
    const reattachRequests = rpcFrames.filter(frame => (
      frame.direction === 'sent' && frame.type === 'req' && frame.method === 'goals.reattach'
    ))
    const reattachRequest = reattachRequests.at(-1)
    await expect.poll(() => {
      // Request ids are connection-local and may restart after page reload.
      // Select the latest matching response rather than an older response
      // from the pre-reload socket that happened to reuse the same id.
      const response = rpcFrames.filter(frame => (
        frame.direction === 'received'
        && frame.type === 'res'
        && frame.id === reattachRequest?.id
      )).at(-1)
      const payload = response?.payload && typeof response.payload === 'object'
        ? response.payload as Record<string, unknown>
        : null
      const goal = payload?.goal && typeof payload.goal === 'object'
        ? payload.goal as Record<string, unknown>
        : null
      return {
        ok: response?.ok,
        accepted: payload?.accepted,
        status: goal?.status,
        deferredReason: goal?.continuationDeferredReason,
      }
    }, { timeout: 15_000 }).toEqual({
      ok: true,
      accepted: true,
      status: 'active',
      deferredReason: null,
    })
    ribbon = page.locator('.goal-ribbon')
    await expect(ribbon).toBeVisible({ timeout: 15_000 })
    await expect(ribbon).toContainText('Goal in progress')
    await expect(ribbon.locator('.goal-ribbon__progress')).toHaveCount(0)
    await expect(page.locator('.msg-ai').filter({ hasText: REAL_FIRST_REPLY })).toHaveCount(1)
    await expect(goalSource).toHaveCount(1)
    await expect(goalSource).toHaveAttribute('data-message-id', sourceMessageId)
    await expect(goalSource.locator('.msg-user-goal-origin')).toContainText('Sent as goal')

    await gateway.releaseSecondTask()
    await expect(ribbon).toHaveCount(0, { timeout: 30_000 })
    const terminalAssistant = page.locator('.msg-ai').filter({ hasText: REAL_FINAL_REPLY })
    await expect(terminalAssistant).toHaveCount(1, { timeout: 30_000 })
    await expect(terminalAssistant.locator('.msg-goal-outcome')).toContainText('Goal achieved')
    await expect(goalSource).toHaveCount(1)

    await expect.poll(
      async () => (await gateway.readProviderCalls()).map(call => call.callNumber),
      { timeout: 15_000 },
    ).toEqual([1, 2, 3])
    const completedCalls = await gateway.readProviderCalls()
    expect(completedCalls[2]?.toolNames).toEqual([])
  } finally {
    await gateway.stop()
  }
})

test('Goal lifecycle controls preserve the current Task and serialize later continuation', async ({
  page,
  baseURL,
}, testInfo) => {
  test.setTimeout(120_000)
  if (!baseURL) throw new Error('Goal browser E2E requires a Playwright baseURL')

  const gateway = await startRealGoalGateway({
    outputDir: testInfo.outputPath('real-goal-lifecycle-gateway'),
    webuiOrigin: new URL(baseURL).origin,
    scenario: 'lifecycle',
  })
  try {
    type RpcFrame = Record<string, unknown> & { direction: 'sent' | 'received' }
    const rpcFrames: RpcFrame[] = []
    page.on('websocket', socket => {
      socket.on('framesent', ({ payload }) => {
        try {
          rpcFrames.push({
            ...(JSON.parse(String(payload)) as Record<string, unknown>),
            direction: 'sent',
          })
        } catch {
          // Binary/non-JSON frames are outside the RPC contract under test.
        }
      })
      socket.on('framereceived', ({ payload }) => {
        try {
          rpcFrames.push({
            ...(JSON.parse(String(payload)) as Record<string, unknown>),
            direction: 'received',
          })
        } catch {
          // Binary/non-JSON frames are outside the RPC contract under test.
        }
      })
    })

    const sentRequests = (method: string) => rpcFrames.filter(frame => (
      frame.direction === 'sent' && frame.type === 'req' && frame.method === method
    ))
    const responseAfter = (request: RpcFrame) => {
      const requestIndex = rpcFrames.indexOf(request)
      return rpcFrames.slice(requestIndex + 1).find(frame => (
        frame.direction === 'received'
        && frame.type === 'res'
        && frame.id === request.id
      ))
    }
    const waitForRpcPayload = async (method: string, ordinal: number) => {
      let matchedResponse: RpcFrame | undefined
      await expect.poll(() => {
        const request = sentRequests(method)[ordinal - 1]
        matchedResponse = request ? responseAfter(request) : undefined
        return Boolean(matchedResponse)
      }, { timeout: 15_000 }).toBe(true)
      expect(matchedResponse?.ok).toBe(true)
      const payload = matchedResponse?.payload
      expect(payload).toBeTruthy()
      expect(typeof payload).toBe('object')
      return payload as Record<string, unknown>
    }
    const providerCallNumbers = async () => (
      (await gateway.readProviderCalls()).map(call => call.callNumber)
    )
    const providerWaitingCalls = async () => (
      (await gateway.readProviderEvents())
        .filter(event => event.event === 'provider.waiting')
        .map(event => event.callNumber)
    )

    await page.addInitScript((wsUrl) => {
      window.localStorage.setItem('opensquilla-locale', 'en')
      window.localStorage.setItem('opensquilla.wsUrl', wsUrl)
    }, gateway.wsUrl)
    await installStableHttpStubs(page)

    await page.goto(CONTROL_URL + 'chat')
    await expect(page.locator('.conn-pill.connected')).toBeVisible({ timeout: 15_000 })
    const composer = page.locator('.chat-textarea')
    await expect(composer).toBeEditable({ timeout: 15_000 })
    await composer.fill('/goal')
    await expect(page.locator('.chat-slash-item').filter({ hasText: '/goal' }))
      .toBeVisible({ timeout: 15_000 })
    await composer.fill(`/goal ${OBJECTIVE}`)
    await page.locator('.chat-send-btn[aria-label="Send"]').click()

    const setPayload = await waitForRpcPayload('goals.set', 1)
    expect(setPayload.accepted).toBe(true)
    await expect.poll(providerWaitingCalls, { timeout: 30_000 }).toContain(1)
    await expect.poll(providerCallNumbers).toEqual([1])

    const ribbon = page.locator('.goal-ribbon')
    const lifecycleAction = ribbon.getByTestId('goal-lifecycle-action')
    const stopButton = page.locator('.chat-send-btn[aria-label="Stop current response"]')
    await expect(ribbon).toHaveAttribute('data-status', 'active')
    await expect(lifecycleAction).toHaveText(/Pause after this turn/)
    await expect(stopButton).toBeVisible()

    // Pause only disables a future automatic continuation. Task 1 remains the
    // owner and stays blocked inside the deterministic provider.
    await lifecycleAction.click()
    const firstPause = await waitForRpcPayload('goals.pause', 1)
    const firstPausedGoal = firstPause.goal as Record<string, unknown>
    expect(firstPause.accepted).toBe(true)
    expect(firstPausedGoal.status).toBe('paused')
    expect(firstPausedGoal.executionState).toBe('working')
    expect(firstPausedGoal.activeTaskId).toBeTruthy()
    const firstTaskId = firstPausedGoal.activeTaskId
    await expect(ribbon).toHaveAttribute('data-status', 'paused')
    await expect(lifecycleAction).toHaveText(/Resume automatic continuation/)
    await expect(stopButton).toBeVisible()
    await expect(page.locator('.msg-ai').filter({ hasText: LIFECYCLE_FIRST_REPLY }))
      .toHaveCount(0)

    // Resuming while that owner is still running is a state transition only;
    // it must not schedule a duplicate AgentTask or Provider call.
    await lifecycleAction.click()
    const ownerResume = await waitForRpcPayload('goals.resume', 1)
    const ownerResumedGoal = ownerResume.goal as Record<string, unknown>
    expect(ownerResume.accepted).toBe(true)
    expect(ownerResumedGoal.status).toBe('active')
    expect(ownerResumedGoal.activeTaskId).toBe(firstTaskId)
    expect(ownerResumedGoal.executionState).toBe('working')
    await expect(lifecycleAction).toHaveText(/Pause after this turn/)
    await expect.poll(providerCallNumbers).toEqual([1])

    // Pause again before releasing Task 1. Once it settles, the Goal must be
    // paused and idle, with no automatic Task 2.
    await lifecycleAction.click()
    const secondPause = await waitForRpcPayload('goals.pause', 2)
    const secondPausedGoal = secondPause.goal as Record<string, unknown>
    expect(secondPausedGoal.status).toBe('paused')
    expect(secondPausedGoal.activeTaskId).toBe(firstTaskId)
    await gateway.releaseFirstTask()
    await expect(page.locator('.msg-ai').filter({ hasText: LIFECYCLE_FIRST_REPLY }))
      .toHaveCount(1, { timeout: 30_000 })
    await expect(lifecycleAction).toHaveText(/Resume goal/, { timeout: 30_000 })
    await expect(ribbon).toHaveAttribute('data-status', 'paused')
    await expect(page.locator('.chat-send-btn[aria-label="Send"]')).toBeVisible()
    await expect.poll(providerCallNumbers).toEqual([1])

    // Resuming the idle Goal creates exactly one next Task. Gate Task 2 inside
    // the provider so Clear can be proven not to cancel it.
    await lifecycleAction.click()
    const idleResume = await waitForRpcPayload('goals.resume', 2)
    expect(idleResume.accepted).toBe(true)
    expect((idleResume.goal as Record<string, unknown>).status).toBe('active')
    await expect.poll(providerWaitingCalls, { timeout: 30_000 }).toEqual([1, 2])
    await expect.poll(providerCallNumbers).toEqual([1, 2])
    const calls = await gateway.readProviderCalls()
    expect(calls[1]).toMatchObject({
      callNumber: 2,
      objectiveInRequestContext: true,
      firstReplyInAssistantHistory: true,
      requestHasInternalContinuation: true,
    })
    await expect(stopButton).toBeVisible()

    // Remove tracking through the production menu and confirmation dialog.
    // The ribbon disappears, but the still-gated Task 2 remains stoppable and
    // has not produced its response yet.
    await ribbon.getByRole('button', { name: 'Goal actions' }).click()
    await ribbon.getByRole('menuitem', { name: 'Remove goal' }).click()
    const confirmDialog = page.getByRole('dialog', { name: 'Remove this goal?' })
    await expect(confirmDialog).toBeVisible()
    await confirmDialog.getByRole('button', { name: 'Remove goal' }).click()
    const clearPayload = await waitForRpcPayload('goals.clear', 1)
    expect(clearPayload.accepted).toBe(true)
    await expect(ribbon).toHaveCount(0)
    await expect(stopButton).toBeVisible()
    await expect(page.locator('.msg-ai').filter({ hasText: LIFECYCLE_SECOND_REPLY }))
      .toHaveCount(0)
    await expect.poll(providerCallNumbers).toEqual([1, 2])

    await gateway.releaseSecondTask()
    await expect(page.locator('.msg-ai').filter({ hasText: LIFECYCLE_SECOND_REPLY }))
      .toHaveCount(1, { timeout: 30_000 })
    await expect(page.locator('.chat-send-btn[aria-label="Send"]'))
      .toBeVisible({ timeout: 30_000 })

    // Hydrate through the public session RPC after Task 2 settles. An idle
    // snapshot with no Goal is the stable fence proving Clear did not revive
    // tracking or enqueue a third turn.
    const hydrateCountBeforeReload = sentRequests('sessions.messages.hydrate').length
    await page.reload()
    await expect(page.locator('.conn-pill.connected')).toBeVisible({ timeout: 15_000 })
    const hydratePayload = await waitForRpcPayload(
      'sessions.messages.hydrate',
      hydrateCountBeforeReload + 1,
    )
    expect(hydratePayload).toMatchObject({
      hydration_complete: true,
      run_status: 'idle',
      goal: null,
    })
    await expect(page.locator('.goal-ribbon')).toHaveCount(0)
    await expect(page.locator('.msg-ai').filter({ hasText: LIFECYCLE_FIRST_REPLY }))
      .toHaveCount(1)
    await expect(page.locator('.msg-ai').filter({ hasText: LIFECYCLE_SECOND_REPLY }))
      .toHaveCount(1)
    await expect.poll(providerCallNumbers).toEqual([1, 2])
    expect(sentRequests('goals.reattach')).toHaveLength(0)
  } finally {
    await gateway.stop()
  }
})

isolatedGatewayTest.describe('Goal silent-reply normalization through an isolated real Gateway', () => {
  isolatedGatewayTest.use({ isolatedRealGatewayScenario: 'silent-reply' })

  isolatedGatewayTest('keeps protocol sentinels out of live, done, and hydrated UI', async ({
    page,
    isolatedRealGateway,
  }) => {
    isolatedGatewayTest.setTimeout(120_000)
    const initialReply = 'The initial Goal turn completed normally.'
    const mixedBody = 'The deterministic silent-reply body is visible.'
    const formattedBody = 'The formatted heartbeat body is visible.'
    type RpcFrame = Record<string, unknown> & { direction: 'sent' | 'received' }
    const frames: RpcFrame[] = []
    const socketUrls: string[] = []
    page.on('websocket', socket => {
      socketUrls.push(socket.url())
      socket.on('framesent', ({ payload }) => {
        try {
          frames.push({
            ...(JSON.parse(String(payload)) as Record<string, unknown>),
            direction: 'sent',
          })
        } catch {
          // Binary/non-JSON frames are outside the RPC contract under test.
        }
      })
      socket.on('framereceived', ({ payload }) => {
        try {
          frames.push({
            ...(JSON.parse(String(payload)) as Record<string, unknown>),
            direction: 'received',
          })
        } catch {
          // Binary/non-JSON frames are outside the RPC contract under test.
        }
      })
    })
    const sentRequests = (method: string) => frames.filter(frame => (
      frame.direction === 'sent' && frame.type === 'req' && frame.method === method
    ))
    const responseAfter = (request: RpcFrame) => {
      const index = frames.indexOf(request)
      return frames.slice(index + 1).find(frame => (
        frame.direction === 'received'
        && frame.type === 'res'
        && frame.id === request.id
      ))
    }
    const eventPayloads = (event: string) => frames.flatMap(frame => {
      if (
        frame.direction !== 'received'
        || frame.type !== 'event'
        || frame.event !== event
        || !frame.payload
        || typeof frame.payload !== 'object'
      ) return []
      return [frame.payload as Record<string, unknown>]
    })
    const providerCallNumbers = async () => (
      (await isolatedRealGateway.readProviderCalls()).map(call => call.callNumber)
    )
    const providerWaitingCalls = async () => (
      (await isolatedRealGateway.readProviderEvents())
        .filter(event => event.event === 'provider.waiting')
        .map(event => event.callNumber)
    )
    const assertNoProtocolText = async () => {
      await expect(page.locator('.chat-thread')).not.toContainText('NO_REPLY')
      await expect(page.locator('.chat-thread')).not.toContainText('HEARTBEAT_OK')
    }
    const eventFrameIndex = (
      event: string,
      predicate: (payload: Record<string, unknown>) => boolean,
      startAt = 0,
    ) => frames.findIndex((frame, index) => {
      if (
        index < startAt
        || frame.direction !== 'received'
        || frame.type !== 'event'
        || frame.event !== event
        || !frame.payload
        || typeof frame.payload !== 'object'
      ) return false
      return predicate(frame.payload as Record<string, unknown>)
    })
    const assertCanonicalHistoryPayload = (payload: unknown) => {
      const serialized = JSON.stringify(payload)
      expect(serialized).not.toMatch(/NO_REPLY|HEARTBEAT_OK/)
      const historyMessages = (
        payload
        && typeof payload === 'object'
        && Array.isArray((payload as { messages?: unknown }).messages)
      ) ? (payload as { messages: Array<Record<string, unknown>> }).messages : []
      expect(historyMessages.filter(message => message.text === initialReply)).toHaveLength(1)
      expect(historyMessages.filter(message => message.text === mixedBody)).toHaveLength(1)
      expect(historyMessages.filter(message => message.text === formattedBody)).toHaveLength(1)
    }
    const assertVisibleTurnReceipt = async (
      body: string,
      inputTokens: number,
      outputTokens: number,
    ) => {
      const message = page.locator('.msg-ai').filter({ hasText: body })
      await expect(message).toHaveCount(1)
      const trigger = message.locator('.msg-meta__more-btn')
      await expect(trigger).toHaveCount(1)
      await trigger.click()
      await expect(trigger).toHaveAttribute('aria-expanded', 'true')
      const usage = message.locator('.msg-meta-popover')
      await expect(usage).toBeVisible()
      await expect(usage).toContainText('qwen3:4b')
      await expect(usage).toContainText(`↑${inputTokens} ↓${outputTokens}`)
    }

    await page.goto(`${isolatedRealGateway.controlUrl}chat/new`)
    await expect(page.locator('.conn-pill.connected')).toBeVisible({ timeout: 15_000 })
    expect(socketUrls).toContain(
      isolatedRealGateway.webuiOrigin.replace(/^http:/, 'ws:') + '/ws',
    )
    expect(socketUrls).not.toContain(isolatedRealGateway.wsUrl)

    const composer = page.locator('.chat-textarea')
    await expect(composer).toBeEditable({ timeout: 15_000 })
    await composer.fill('/goal')
    await expect(page.locator('.chat-slash-item').filter({ hasText: '/goal' }))
      .toBeVisible({ timeout: 15_000 })
    await composer.fill(`/goal ${OBJECTIVE}`)
    await page.locator('.chat-send-btn[aria-label="Send"]').click()

    // Call 1 is ordinary Goal-set ingress. Call 2 is the first automatic
    // system event and is held after its raw mixed provider delta, before Done.
    await expect.poll(providerCallNumbers, { timeout: 30_000 }).toEqual([1, 2])
    await expect.poll(providerWaitingCalls).toEqual([2])
    await expect(page.locator('.goal-ribbon')).toBeVisible()
    await expect(page.locator('.msg-ai').filter({ hasText: initialReply })).toHaveCount(1)
    await expect(page.locator('.msg-ai').filter({ hasText: mixedBody })).toHaveCount(0)
    await assertNoProtocolText()
    // Buffered internal text is not released until it has been normalized at
    // Done, so neither the raw marker nor the body appears as a premature delta.
    expect(eventPayloads('session.event.text_delta').map(payload => payload.text))
      .not.toContain(expect.stringContaining('NO_REPLY'))

    await isolatedRealGateway.releaseFirstTask()

    // Call 2 settles as canonical mixed text; call 3 is pure NO_REPLY and
    // suppressed; call 4 keeps a body after a formatted HEARTBEAT_OK line;
    // call 5 then waits before update_goal so this state is reloadable.
    await expect.poll(providerCallNumbers, { timeout: 30_000 })
      .toEqual([1, 2, 3, 4, 5])
    await expect.poll(providerWaitingCalls).toEqual([2, 5])
    await expect(page.locator('.msg-ai').filter({ hasText: mixedBody })).toHaveCount(1)
    await expect(page.locator('.msg-ai').filter({ hasText: formattedBody })).toHaveCount(1)
    await assertNoProtocolText()

    // The pure NO_REPLY turn (call 3) is accounted for by the Goal ledger but
    // creates no ghost bubble. Every visible turn exposes its own settled usage
    // through the shared completion receipt, never through the legacy footer.
    await expect(page.locator('.chat-message-surface .msg-ai')).toHaveCount(3)
    await assertVisibleTurnReceipt(initialReply, 12, 4)
    await assertVisibleTurnReceipt(mixedBody, 11, 5)
    await assertVisibleTurnReceipt(formattedBody, 10, 4)

    const donePayloads = eventPayloads('session.event.done')
    expect(donePayloads).toEqual(expect.arrayContaining([
      expect.objectContaining({
        delivery: 'visible',
        suppression_reason: null,
        text: mixedBody,
        text_snapshot: mixedBody,
        input_mode: 'system_event',
        run_kind: 'goal',
      }),
      expect.objectContaining({
        delivery: 'suppressed',
        suppression_reason: 'no_reply',
        text: '',
        text_snapshot: '',
        input_mode: 'system_event',
        run_kind: 'goal',
      }),
      expect.objectContaining({
        delivery: 'visible',
        suppression_reason: null,
        text: formattedBody,
        text_snapshot: formattedBody,
        input_mode: 'system_event',
        run_kind: 'goal',
      }),
    ]))
    for (const payload of eventPayloads('session.event.text_delta')) {
      expect(String(payload.text || '')).not.toMatch(/NO_REPLY|HEARTBEAT_OK/)
    }
    const mixedDeltaIndex = eventFrameIndex(
      'session.event.text_delta',
      payload => String(payload.text || '').includes(mixedBody),
    )
    const mixedDoneIndex = eventFrameIndex(
      'session.event.done',
      payload => payload.text_snapshot === mixedBody,
      mixedDeltaIndex + 1,
    )
    const formattedDeltaIndex = eventFrameIndex(
      'session.event.text_delta',
      payload => String(payload.text || '').includes(formattedBody),
    )
    const formattedDoneIndex = eventFrameIndex(
      'session.event.done',
      payload => payload.text_snapshot === formattedBody,
      formattedDeltaIndex + 1,
    )
    expect(mixedDeltaIndex).toBeGreaterThanOrEqual(0)
    expect(mixedDoneIndex).toBeGreaterThan(mixedDeltaIndex)
    expect(formattedDeltaIndex).toBeGreaterThanOrEqual(0)
    expect(formattedDoneIndex).toBeGreaterThan(formattedDeltaIndex)

    const callsBeforeCompletion = await isolatedRealGateway.readProviderCalls()
    expect(callsBeforeCompletion[1]).toMatchObject({
      callNumber: 2,
      requestHasInternalContinuation: true,
    })
    expect(callsBeforeCompletion[4]).toMatchObject({
      callNumber: 5,
      historyHasSilentSentinel: false,
      silentVisibleBodyInAssistantHistory: true,
    })

    // A new real socket hydrates the SQLite transcript while call 5 remains
    // gated. Canonical bodies render once and neither suppressed turn grows a
    // ghost assistant row.
    const hydrateCountBeforeReload = sentRequests('sessions.messages.hydrate').length
    const historyCountBeforeReload = sentRequests('chat.history').length
    const socketCountBeforeReload = socketUrls.length
    await page.reload()
    await expect(page.locator('.conn-pill.connected')).toBeVisible({ timeout: 15_000 })
    await expect.poll(() => socketUrls.length, { timeout: 15_000 })
      .toBeGreaterThan(socketCountBeforeReload)
    await expect.poll(
      () => sentRequests('sessions.messages.hydrate').length,
      { timeout: 15_000 },
    ).toBeGreaterThan(hydrateCountBeforeReload)
    await expect.poll(
      () => sentRequests('chat.history').length,
      { timeout: 15_000 },
    ).toBeGreaterThan(historyCountBeforeReload)
    const hydrateRequest = sentRequests('sessions.messages.hydrate').at(-1)!
    await expect.poll(() => Boolean(responseAfter(hydrateRequest)), { timeout: 15_000 })
      .toBe(true)
    expect(responseAfter(hydrateRequest)?.payload).toMatchObject({
      hydration_complete: true,
    })
    expect(JSON.stringify(responseAfter(hydrateRequest)?.payload))
      .not.toMatch(/NO_REPLY|HEARTBEAT_OK/)
    const historyRequest = sentRequests('chat.history').at(-1)!
    await expect.poll(() => Boolean(responseAfter(historyRequest)), { timeout: 15_000 })
      .toBe(true)
    assertCanonicalHistoryPayload(responseAfter(historyRequest)?.payload)
    await expect(page.locator('.msg-ai').filter({ hasText: initialReply })).toHaveCount(1)
    await expect(page.locator('.msg-ai').filter({ hasText: mixedBody })).toHaveCount(1)
    await expect(page.locator('.msg-ai').filter({ hasText: formattedBody })).toHaveCount(1)
    await expect(page.locator('.chat-message-surface .msg-ai')).toHaveCount(3)
    await assertVisibleTurnReceipt(initialReply, 12, 4)
    await assertVisibleTurnReceipt(mixedBody, 11, 5)
    await assertVisibleTurnReceipt(formattedBody, 10, 4)
    await assertNoProtocolText()

    await isolatedRealGateway.releaseSecondTask()
    await expect.poll(providerCallNumbers, { timeout: 30_000 })
      .toEqual([1, 2, 3, 4, 5, 6])
    await expect(page.locator('.goal-ribbon')).toHaveCount(0, { timeout: 30_000 })
    const outcome = page.locator('.goal-outcome').last()
    await expect(outcome).toBeVisible({ timeout: 30_000 })
    await expect(outcome).toContainText('Goal achieved')
    await assertNoProtocolText()
    await expect(page.locator('.msg-ai').filter({ hasText: mixedBody })).toHaveCount(1)
    await expect(page.locator('.msg-ai').filter({ hasText: formattedBody })).toHaveCount(1)

    await expect.poll(() => eventPayloads('session.event.done').some(payload => (
      payload.delivery === 'suppressed'
      && payload.suppression_reason === 'heartbeat_ack'
      && payload.text === ''
      && payload.text_snapshot === ''
    )), { timeout: 15_000 }).toBe(true)
    let terminalGoal: Record<string, unknown> | null = null
    await expect.poll(() => {
      terminalGoal = eventPayloads('session.event.goal')
        .map(payload => payload.goal)
        .filter((goal): goal is Record<string, unknown> => (
          Boolean(goal) && typeof goal === 'object'
        ))
        .filter(goal => goal.status === 'complete')
        .at(-1) ?? null
      return Boolean(terminalGoal)
    }, { timeout: 15_000 }).toBe(true)
    expect(terminalGoal?.objective).toBe(OBJECTIVE)
    const terminalUsage = terminalGoal?.usage as Record<string, unknown> | undefined
    const terminalTurns = Number(
      terminalGoal?.turnsSettled ?? terminalGoal?.turns_settled ?? 0,
    )
    const terminalTokens = Number(
      terminalUsage?.totalTokens ?? terminalUsage?.total_tokens ?? 0,
    )
    const uniqueDoneByTask = new Map<string, Record<string, unknown>>()
    for (const payload of eventPayloads('session.event.done')) {
      const taskId = String(payload.task_id ?? payload.taskId ?? '')
      expect(taskId).not.toBe('')
      uniqueDoneByTask.set(taskId, payload)
    }
    const allSettledProviderTokens = [...uniqueDoneByTask.values()].reduce(
      (sum, payload) => sum
        + Number(payload.input_tokens ?? payload.inputTokens ?? 0)
        + Number(payload.output_tokens ?? payload.outputTokens ?? 0),
      0,
    )
    expect(terminalTurns).toBeGreaterThan(0)
    expect(terminalTokens).toBe(allSettledProviderTokens)
    // The visible text receipts account for 46 tokens. The larger Goal total
    // proves that suppressed and tool-only work remains in the billing ledger.
    expect(terminalTokens).toBeGreaterThan(46)
    await expect(outcome).toContainText(`${terminalTurns} turns`)
    await expect(outcome).toContainText(`${terminalTokens} tokens`)

    const completedCalls = await isolatedRealGateway.readProviderCalls()
    expect(completedCalls[5]).toMatchObject({
      callNumber: 6,
      toolNames: [],
      historyHasSilentSentinel: false,
      silentVisibleBodyInAssistantHistory: true,
    })

    // Terminal refresh exercises the persisted fallback Goal outcome as well
    // as the sanitized transcript one final time.
    const finalHydrateCount = sentRequests('sessions.messages.hydrate').length
    const finalHistoryCount = sentRequests('chat.history').length
    const finalSocketCount = socketUrls.length
    await page.reload()
    await expect(page.locator('.conn-pill.connected')).toBeVisible({ timeout: 15_000 })
    await expect.poll(() => socketUrls.length, { timeout: 15_000 })
      .toBeGreaterThan(finalSocketCount)
    await expect.poll(() => sentRequests('sessions.messages.hydrate').length, {
      timeout: 15_000,
    }).toBeGreaterThan(finalHydrateCount)
    await expect.poll(() => sentRequests('chat.history').length, { timeout: 15_000 })
      .toBeGreaterThan(finalHistoryCount)
    const terminalHydrateRequest = sentRequests('sessions.messages.hydrate').at(-1)!
    const terminalHistoryRequest = sentRequests('chat.history').at(-1)!
    await expect.poll(
      () => Boolean(responseAfter(terminalHydrateRequest)),
      { timeout: 15_000 },
    ).toBe(true)
    await expect.poll(
      () => Boolean(responseAfter(terminalHistoryRequest)),
      { timeout: 15_000 },
    ).toBe(true)
    expect(JSON.stringify(responseAfter(terminalHydrateRequest)?.payload))
      .not.toMatch(/NO_REPLY|HEARTBEAT_OK/)
    assertCanonicalHistoryPayload(responseAfter(terminalHistoryRequest)?.payload)
    await expect(page.locator('.msg-ai').filter({ hasText: mixedBody })).toHaveCount(1)
    await expect(page.locator('.msg-ai').filter({ hasText: formattedBody })).toHaveCount(1)
    const hydratedOutcome = page.locator('.goal-outcome').last()
    await expect(hydratedOutcome).toBeVisible({ timeout: 15_000 })
    await expect(hydratedOutcome).toContainText(`${terminalTurns} turns`)
    await expect(hydratedOutcome).toContainText(`${terminalTokens} tokens`)
    await assertNoProtocolText()
  })
})
