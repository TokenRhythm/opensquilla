import { expect, test, type Page, type WebSocketRoute } from '@playwright/test'
import { writeFile } from 'node:fs/promises'
import { helloOkResponse } from './support/gateway-fixture'
import { chatHistoryPayload, sessionMessagesHydratePayload, sessionMessagesSnapshotPayload, sessionMessagesSubscribePayload } from './support/session-read-fixtures'

const SESSION = 'agent:main:webchat:goal-recovery'
const OTHER = 'agent:main:webchat:goal-recovery-other'
const SESSION_ID = 'synthetic-goal-session'
const GENERATION = 'synthetic-goal-generation'
const OBJECTIVE = 'Verify current ownership before continuing this Goal.'
const TOKEN = 'synthetic-goal-continuity-token'
const READ = 'sessions.messages.snapshot.read'
const RESUME = 'sessions.messages.resume'
const RELEASE = 'sessions.messages.snapshot.release'
type Request = { type: string; id: string; method: string; params: Record<string, unknown>; nonce?: string; connection: number }
type Goal = ReturnType<typeof goalSnapshot>

function goalSnapshot(overrides: Record<string, unknown> = {}) {
  return {
    goalId: 'synthetic-goal', sessionKey: SESSION, sessionId: SESSION_ID, epoch: 1,
    objective: OBJECTIVE, status: 'active', stateRevision: 1, objectiveRevision: 1, progressRevision: 0,
    progress: null, continuationSeq: 0, activeTaskId: null, sourceMessageId: 'synthetic-goal-source',
    executionState: 'idle', continuationDeferredReason: null, turnsStarted: 1, turnsSettled: 1,
    windowTurnsStarted: 1, activeTimeMs: 0, windowActiveTimeMs: 0,
    usage: { inputTokens: 0, outputTokens: 0, reasoningTokens: 0, cacheReadTokens: 0, cacheWriteTokens: 0, totalTokens: 0 },
    pauseReason: null, blockedReason: null, terminalReason: null, createdAt: 1, updatedAt: 1, finishedAt: null,
    ...overrides,
  }
}

async function advance(page: Page, milliseconds: number) {
  for (let elapsed = 0; elapsed < milliseconds; elapsed += 1_000) {
    await page.clock.runFor(Math.min(1_000, milliseconds - elapsed))
    await page.evaluate(() => Promise.resolve())
  }
}

async function prepare(page: Page, options: {
  goal?: Goal | null; reattach?: 'terminal' | 'transient' | 'persistent'; holdCatalog?: boolean
  retryAfterMs?: number
} = {}) {
  await page.clock.install({ time: new Date('2026-01-01T00:00:00Z') })
  await page.addInitScript(() => localStorage.setItem('opensquilla-locale', 'en'))
  await page.route('**/api/**', route => route.fulfill({ json: {} }))
  await page.route('**/api/approvals', route => route.fulfill({ json: { pending: [], mode: 'prompt', allowPatterns: [], denyPatterns: [] } }))
  const requests: Request[] = []
  const sockets: WebSocketRoute[] = []
  const heldReads: Array<{ request: Request; release: () => void }> = []
  const catalogs: Array<() => void> = []
  let currentGoal: Goal | null = options.goal ?? null
  let streamSeq = 0
  let holdReads = false
  let holdCatalog = Boolean(options.holdCatalog)
  let reattachMode = options.reattach
  const metadata = (key: string) => ({ stream_generation: GENERATION, current_stream_seq: streamSeq,
    sessionId: key === SESSION ? SESSION_ID : 'synthetic-other-session', epoch: 1,
    goal: key === SESSION ? currentGoal : null, goalSnapshotStreamSeq: streamSeq,
  })
  await page.routeWebSocket(/\/ws$/, socket => {
    const connection = sockets.push(socket)
    const deliveryEpoch = `synthetic-goal-delivery-${connection}`
    let deliveryId = 0
    socket.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    socket.onMessage(raw => {
      const frame = JSON.parse(String(raw)) as Request
      if (frame.type === 'ping') { socket.send(JSON.stringify({ type: 'pong', nonce: frame.nonce })); return }
      if (frame.type !== 'req') return
      const request = { ...frame, params: frame.params ?? {}, connection }
      requests.push(request)
      const key = String(request.params.key ?? request.params.sessionKey ?? SESSION)
      const reply = (payload: unknown) => socket.send(JSON.stringify({ type: 'res', id: frame.id, ok: true, payload }))
      if (frame.method === 'connect') {
        socket.send(helloOkResponse({ protocol: 4, server: { conn_id: `synthetic-goal-${connection}` },
          features: { methods: [READ, RESUME, RELEASE, 'transport.flow.update', 'sessions.messages.subscribe',
            'sessions.messages.hydrate', 'sessions.messages.unsubscribe', 'chat.history', 'goals.capabilities',
            'goals.set', 'goals.reattach', 'goals.pause', 'goals.resume', 'goals.clear', 'goals.edit'],
            events: ['session.event.goal'] },
          policy: { transport_probe_nonce: true, cancellable_request_methods: [READ, RESUME, 'sessions.messages.hydrate', 'chat.history'],
            transport_flow: { delivery_epoch: deliveryEpoch, window_frames: 128, window_bytes: 4 * 1024 * 1024 } },
        }))
        return
      }
      if (frame.method === READ) {
        const snapshot = sessionMessagesSnapshotPayload(key, { stream_generation: GENERATION, current_stream_seq: streamSeq,
          session_id: key === SESSION ? SESSION_ID : 'synthetic-other-session', session_epoch: 1 })
        const bytes = Buffer.from(JSON.stringify(snapshot))
        const payload = { key, sync_revision: request.params.sync_revision, snapshot_id: `snapshot-${connection}-${request.id}`,
          segment_index: 0, segment_count: 1, byte_length: bytes.length, encoding: 'base64-json-utf8', data: bytes.toString('base64'),
          stream_generation: GENERATION, current_stream_seq: streamSeq, task_id: null,
          session_id: key === SESSION ? SESSION_ID : 'synthetic-other-session', session_epoch: 1,
          delivery: { delivery_epoch: deliveryEpoch, delivery_id: ++deliveryId } }
        if (holdReads && key === SESSION) heldReads.push({ request, release: () => reply(payload) })
        else reply(payload)
        return
      }
      if (frame.method === RESUME) { reply({ ...request.params, session_id: key === SESSION ? SESSION_ID : 'synthetic-other-session', session_epoch: 1, replay_to_seq: request.params.stream_seq }); return }
      if (frame.method === RELEASE) { reply({ ...request.params, retired: true }); return }
      if (frame.method === 'transport.flow.update') { reply({ delivery_epoch: deliveryEpoch, ack_delivery_id: request.params.ack_delivery_id ?? 0, dirty_keys: [], global_dirty: false }); return }
      if (frame.method === 'goals.set') {
        currentGoal = goalSnapshot({ objective: request.params.objective })
        reply({ accepted: true, clientRequestId: request.params.clientRequestId, sessionKey: SESSION, sessionId: SESSION_ID, epoch: 1,
          taskId: null, userMessageId: currentGoal.sourceMessageId, previousGoalId: null, goal: currentGoal, continuityToken: TOKEN })
        return
      }
      if (frame.method === 'goals.reattach') {
        const ordinal = requests.filter(item => item.method === 'goals.reattach').length
        const fail = !request.params.takeover && (reattachMode === 'terminal' || reattachMode === 'persistent'
          || reattachMode === 'transient' && ordinal === 1)
        if (fail) {
          socket.send(JSON.stringify({ type: 'res', id: frame.id, ok: false, error: reattachMode === 'terminal'
            ? { code: 'UNAVAILABLE', message: 'Connection continuity cannot be restored.', retryable: false, accepted: false,
              details: { recoveryAction: 'require-takeover' } }
            : { code: 'STORAGE_BUSY', message: 'Synthetic transient continuity storage contention.', retryable: true,
              retry_after_ms: options.retryAfterMs ?? (reattachMode === 'persistent' ? 5_000 : 1_500), accepted: false,
              details: { recoveryAction: 'retry' } } }))
        } else {
          currentGoal = goalSnapshot({ ...currentGoal, continuationDeferredReason: null })
          reply({ accepted: true, sessionKey: SESSION, sessionId: SESSION_ID, epoch: 1, goal: currentGoal, continuityToken: TOKEN })
        }
        return
      }
      if (['goals.pause', 'goals.resume', 'goals.edit', 'goals.clear'].includes(frame.method)) {
        const previous = currentGoal!
        currentGoal = frame.method === 'goals.clear' ? null : goalSnapshot({ ...previous,
          status: frame.method === 'goals.pause' ? 'paused' : 'active', stateRevision: Number(previous.stateRevision) + 1,
          ...(frame.method === 'goals.edit' ? { objective: request.params.objective, objectiveRevision: Number(previous.objectiveRevision) + 1 } : {}) })
        reply({ accepted: true, clientRequestId: request.params.clientRequestId, sessionKey: SESSION, sessionId: SESSION_ID, epoch: 1,
          previousGoalId: frame.method === 'goals.clear' ? previous.goalId : null, goal: currentGoal })
        return
      }
      const payloads: Record<string, unknown> = {
        'agents.list': { agents: [] },
        'commands.list_for_surface': { commands: [{ name: '/goal', cmd: '/goal', label: '/goal', description: 'Set a persistent goal.', aliases: [], execution: { action: 'goal.set' } }] },
        'config.get': { squilla_router: { enabled: false, tiers: {} }, permissions: {}, skills: {} },
        'sandbox.run_mode.preference.get': { runMode: 'full', source: 'config' },
        'models.routing.get': { mode: 'direct' },
        'sessions.list': { sessions: [SESSION, OTHER].map((key, i) => ({ key, title: i ? 'Other Goal session' : 'Goal recovery session', sessionKind: 'chat', surface: 'webchat', conversationKind: 'direct', effectiveAgentId: 'main', updatedAt: 100 + i, messageCount: 1, status: 'ok', runStatus: 'idle' })), count: 2, has_more: false, ts: 1_800_000_000 },
        'sessions.messages.subscribe': sessionMessagesSubscribePayload(key, { ...metadata(key), hydration_complete: false, deferred_fields: ['goal', 'goalSnapshotStreamSeq'] }),
        'sessions.messages.hydrate': sessionMessagesHydratePayload(key, metadata(key)),
        'chat.history': chatHistoryPayload([{ role: 'user', text: 'Synthetic Goal recovery history.', message_id: `history-${key}`, timestamp: '2026-01-01T00:00:00Z' }]),
        'sessions.messages.unsubscribe': null, 'sessions.subscribe': { subscribed: true }, 'usage.status': { sessions: [] },
      }
      if (frame.method === 'commands.list_for_surface' && holdCatalog) catalogs.push(() => reply(payloads[frame.method]))
      else reply(Object.hasOwn(payloads, frame.method) ? payloads[frame.method] : {})
    })
  })
  const calls = (method: string) => requests.filter(item => item.method === method)
  return { requests, sockets, heldReads, calls,
    restoreContinuity: () => { reattachMode = undefined },
    detach: () => { currentGoal = goalSnapshot({ ...currentGoal, continuationDeferredReason: 'owner_disconnected' }) },
    beginRecovery: () => {
      holdReads = true
      sockets.at(-1)!.send(JSON.stringify({ type: 'event', event: 'transport.flow.dirty', payload: {
        delivery_epoch: `synthetic-goal-delivery-${sockets.length}`, dirty_keys: [SESSION], global_dirty: false,
      } }))
    },
    releaseRead: () => { holdReads = false; for (const item of heldReads.splice(0)) item.release() },
    releaseCatalog: () => { holdCatalog = false; for (const release of catalogs.splice(0)) release() },
    emitGoal: (overrides: Record<string, unknown>) => {
      currentGoal = goalSnapshot({ ...currentGoal, ...overrides })
      sockets.at(-1)!.send(JSON.stringify({ type: 'event', event: 'session.event.goal', payload: {
        key: SESSION, session_key: SESSION, session_id: SESSION_ID, epoch: 1, stream_generation: GENERATION, stream_seq: ++streamSeq,
        event_type: 'updated', state_revision: currentGoal.stateRevision, progress_revision: currentGoal.progressRevision,
        previous_goal_id: null, goal: currentGoal,
      } }))
    },
  }
}

async function record(gateway: Awaited<ReturnType<typeof prepare>>, name: string) {
  const body = JSON.stringify({ evidence: 'Production ChatView and Gateway adapters in Chromium; synthetic Gateway and controlled clock', requests: gateway.requests }, null, 2)
  await writeFile(test.info().outputPath(`${name}.json`), body)
  await test.info().attach(`${name}.json`, { body, contentType: 'application/json' })
}

async function establishContinuity(page: Page, mode: 'terminal' | 'transient' | 'persistent', retryAfterMs?: number) {
  const gateway = await prepare(page, { reattach: mode, retryAfterMs })
  await page.goto(`/control/chat?session=${encodeURIComponent(SESSION)}`)
  await expect(page.locator('.chat-send-btn[aria-label="Send"]')).toBeEnabled()
  await page.getByRole('button', { name: 'Add', exact: true }).click()
  await page.getByRole('menuitem', { name: /Goal mode/ }).click()
  await page.locator('.chat-textarea').fill(OBJECTIVE)
  await page.locator('.chat-send-btn[aria-label="Send"]').click()
  await expect.poll(() => gateway.calls('goals.set').length).toBe(1)
  await expect(page.locator('.goal-ribbon')).toContainText(OBJECTIVE)
  await expect.poll(() => page.evaluate(() => Object.keys(sessionStorage).filter(key => key.startsWith('opensquilla.goal.continuity.v1:')).length)).toBe(1)
  gateway.detach()
  await page.reload()
  await expect.poll(() => gateway.calls('goals.reattach').length).toBe(1)
  return gateway
}

test('nonretryable reattach stays terminal until an explicit user takeover', async ({ page }) => {
  test.setTimeout(90_000)
  const gateway = await establishContinuity(page, 'terminal')
  const action = page.getByTestId('goal-lifecycle-action')
  await expect(action).toHaveAttribute('data-action', 'takeover')
  await advance(page, 121_000)
  expect(gateway.calls('goals.reattach')).toHaveLength(1)
  expect(gateway.calls('goals.resume')).toHaveLength(0)
  const hydratesBeforeRecovery = gateway.calls('sessions.messages.hydrate').length
  gateway.beginRecovery()
  await expect.poll(() => gateway.heldReads.length).toBe(1)
  gateway.releaseRead()
  await advance(page, 1_000)
  await expect.poll(() => gateway.calls('sessions.messages.hydrate').length).toBeGreaterThan(hydratesBeforeRecovery)
  expect(gateway.calls('goals.reattach')).toHaveLength(1)
  await expect(action).toHaveAttribute('data-action', 'takeover')
  await action.click()
  await expect.poll(() => gateway.calls('goals.reattach').length).toBe(2)
  expect(gateway.calls('goals.reattach')[1]!.params).toMatchObject({ takeover: true })
  expect(gateway.calls('goals.reattach')[1]!.params).not.toHaveProperty('continuityToken')
  await expect(action).toHaveAttribute('data-action', 'pause')
  await record(gateway, 'terminal-reattach')
})

test('transient reattach respects its retry hint and restores the same token without Resume', async ({ page }) => {
  const gateway = await establishContinuity(page, 'transient')
  await advance(page, 1_000)
  expect(gateway.calls('goals.reattach')).toHaveLength(1)
  await advance(page, 1_000)
  await expect.poll(() => gateway.calls('goals.reattach').length).toBe(2)
  for (const request of gateway.calls('goals.reattach')) {
    expect(request.params).toMatchObject({ continuityToken: TOKEN, expectedGoalId: 'synthetic-goal' })
    expect(request.params).not.toHaveProperty('takeover')
  }
  await expect(page.getByTestId('goal-lifecycle-action')).toHaveAttribute('data-action', 'pause')
  await advance(page, 15_000)
  expect(gateway.calls('goals.reattach')).toHaveLength(2)
  expect(gateway.calls('goals.resume')).toHaveLength(0)
  await record(gateway, 'transient-reattach')
})

test('a newer detached Goal event preserves the required explicit takeover action', async ({ page }) => {
  test.setTimeout(90_000)
  const gateway = await establishContinuity(page, 'terminal')
  const action = page.getByTestId('goal-lifecycle-action')
  await expect(action).toHaveAttribute('data-action', 'takeover')
  const updatedObjective = 'A newer detached event must retain the rejected continuity action.'
  gateway.emitGoal({ objective: updatedObjective, objectiveRevision: 2, stateRevision: 2,
    continuationDeferredReason: 'owner_disconnected' })
  await expect(page.locator('.goal-ribbon')).toContainText(updatedObjective)
  await expect(action).toHaveAttribute('data-action', 'takeover')
  await advance(page, 121_000)
  expect(gateway.calls('goals.reattach')).toHaveLength(1)
  expect(gateway.calls('goals.resume')).toHaveLength(0)
  await expect(action).toHaveAttribute('data-action', 'takeover')
  await action.click()
  await expect.poll(() => gateway.calls('goals.reattach').length).toBe(2)
  expect(gateway.calls('goals.reattach')[1]!.params).toMatchObject({
    takeover: true, expectedGoalId: 'synthetic-goal',
  })
  expect(gateway.calls('goals.reattach')[1]!.params).not.toHaveProperty('continuityToken')
  await expect(action).toHaveAttribute('data-action', 'pause')
  await record(gateway, 'takeover-after-newer-detached-event')
})

test('explicit retry after continuity exhaustion restores the same token without lifecycle mutation', async ({ page }) => {
  test.setTimeout(90_000)
  const gateway = await establishContinuity(page, 'persistent')
  await advance(page, 121_000)
  const action = page.getByTestId('goal-lifecycle-action')
  await expect(action).toHaveAttribute('data-action', 'retry-connection')
  const exhaustedCalls = gateway.calls('goals.reattach').length
  expect(exhaustedCalls).toBeGreaterThan(1)
  gateway.restoreContinuity()
  gateway.emitGoal({ stateRevision: 2, continuationDeferredReason: 'owner_disconnected' })
  gateway.sockets.at(-1)!.close({ code: 1012, reason: 'dependency restored after the exhausted incident' })
  await advance(page, 2_000)
  await expect.poll(() => gateway.sockets.length).toBe(3)
  await advance(page, 30_000)
  expect(gateway.calls('goals.reattach')).toHaveLength(exhaustedCalls)
  await expect(action).toHaveAttribute('data-action', 'retry-connection')
  await action.click()
  await expect.poll(() => gateway.calls('goals.reattach').length).toBe(exhaustedCalls + 1)
  const explicit = gateway.calls('goals.reattach')[exhaustedCalls]!
  expect(explicit.params).toMatchObject({ continuityToken: TOKEN, expectedGoalId: 'synthetic-goal' })
  expect(explicit.params).not.toHaveProperty('takeover')
  await expect(action).toHaveAttribute('data-action', 'pause')
  await advance(page, 30_000)
  expect(gateway.calls('goals.reattach')).toHaveLength(exhaustedCalls + 1)
  for (const method of ['goals.pause', 'goals.resume', 'goals.clear']) expect(gateway.calls(method)).toHaveLength(0)
  expect(gateway.calls('goals.set')).toHaveLength(1)
  await record(gateway, 'explicit-same-token-retry-after-exhaustion')
})

test('explicit retry preserves a server Retry-After extending beyond the expired continuity budget', async ({ page }) => {
  test.setTimeout(90_000)
  const gateway = await establishContinuity(page, 'persistent', 180_000)
  await advance(page, 121_000)
  const action = page.getByTestId('goal-lifecycle-action')
  await expect(action).toHaveAttribute('data-action', 'retry-connection')
  expect(gateway.calls('goals.reattach')).toHaveLength(1)
  gateway.restoreContinuity()
  await action.click()
  // A new user-owned budget does not erase the prior server admission hint.
  await advance(page, 50_000)
  expect(gateway.calls('goals.reattach')).toHaveLength(1)
  await advance(page, 11_000)
  await expect.poll(() => gateway.calls('goals.reattach').length).toBe(2)
  expect(gateway.calls('goals.reattach')[1]!.params).toMatchObject({
    continuityToken: TOKEN, expectedGoalId: 'synthetic-goal',
  })
  expect(gateway.calls('goals.reattach')[1]!.params).not.toHaveProperty('takeover')
  await expect(action).toHaveAttribute('data-action', 'pause')
  expect(gateway.calls('goals.resume')).toHaveLength(0)
  await record(gateway, 'explicit-retry-preserves-retry-after')
})

test('persistent reattach stops at its deadline and reconnection cannot renew it', async ({ page }) => {
  test.setTimeout(90_000)
  const gateway = await establishContinuity(page, 'persistent')
  await advance(page, 60_000)
  expect(gateway.calls('goals.reattach').length).toBeGreaterThan(1)
  gateway.sockets.at(-1)!.close({ code: 1012, reason: 'synthetic reconnect during continuity incident' })
  await advance(page, 2_000)
  await expect.poll(() => gateway.sockets.length).toBe(3)
  await advance(page, 59_000)
  const callsAtDeadline = gateway.calls('goals.reattach').length
  expect(callsAtDeadline).toBeGreaterThan(1)
  expect(callsAtDeadline).toBeLessThanOrEqual(25)
  await advance(page, 30_000)
  expect(gateway.calls('goals.reattach')).toHaveLength(callsAtDeadline)
  const hydratesAtDeadline = gateway.calls('sessions.messages.hydrate').length
  gateway.beginRecovery()
  await expect.poll(() => gateway.heldReads.length).toBe(1)
  gateway.releaseRead()
  await advance(page, 1_000)
  await expect.poll(() => gateway.calls('sessions.messages.hydrate').length).toBeGreaterThan(hydratesAtDeadline)
  expect(gateway.calls('goals.reattach')).toHaveLength(callsAtDeadline)
  gateway.sockets.at(-1)!.close({ code: 1012, reason: 'synthetic reconnect after continuity deadline' })
  await advance(page, 2_000)
  await expect.poll(() => gateway.sockets.length).toBe(4)
  await advance(page, 30_000)
  expect(gateway.calls('goals.reattach')).toHaveLength(callsAtDeadline)
  expect(gateway.calls('goals.resume')).toHaveLength(0)
  await record(gateway, 'bounded-reattach')
})

for (const superseded of ['no', 'navigation', 'goal-revision', 'replacement-goal'] as const) {
  test(`Resume waits for current read and ${superseded === 'no' ? 'dispatches once' : `rejects ${superseded}`}`, async ({ page }) => {
    test.setTimeout(90_000)
    const gateway = await prepare(page, { goal: goalSnapshot({ status: 'paused' }) })
    await page.goto(`/control/chat?session=${encodeURIComponent(SESSION)}`)
    const action = page.getByTestId('goal-lifecycle-action')
    await expect(action).toHaveAttribute('data-action', 'resume')
    gateway.beginRecovery()
    await expect.poll(() => gateway.heldReads.length).toBe(1)
    await action.click()
    await advance(page, 500)
    expect(gateway.calls('goals.resume')).toHaveLength(0)
    if (superseded === 'navigation') {
      await page.locator(`[data-session-key="${OTHER}"] .sidebar-history-item`).click()
      await expect.poll(() => new URL(page.url()).searchParams.get('session')).toBe(OTHER)
    } else if (superseded === 'goal-revision' || superseded === 'replacement-goal') {
      const objective = superseded === 'goal-revision' ? 'A newer revision owns this Goal.' : 'A replacement Goal owns this session.'
      gateway.emitGoal({ ...(superseded === 'replacement-goal' ? { goalId: 'replacement-goal' } : {}), objective, stateRevision: 2 })
      await expect(page.locator('.goal-ribbon')).toContainText(objective)
    }
    gateway.releaseRead()
    await advance(page, 1_000)
    if (superseded === 'no') {
      await expect.poll(() => gateway.calls('goals.resume').length).toBe(1)
      expect(gateway.calls('goals.resume')[0]!.params).toMatchObject({ expectedGoalId: 'synthetic-goal', expectedStateRevision: 1 })
      await expect(action).toHaveAttribute('data-action', 'pause')
    } else expect(gateway.calls('goals.resume')).toHaveLength(0)
    await record(gateway, `resume-${superseded}`)
  })
}

for (const method of ['pause', 'clear'] as const) {
  test(`${method} remains a direct control command while the session read is recovering`, async ({ page }) => {
    const gateway = await prepare(page, { goal: goalSnapshot() })
    await page.goto(`/control/chat?session=${encodeURIComponent(SESSION)}`)
    const ribbon = page.locator('.goal-ribbon')
    await expect(ribbon).toContainText(OBJECTIVE)
    gateway.beginRecovery()
    await expect.poll(() => gateway.heldReads.length).toBe(1)
    if (method === 'pause') await page.getByTestId('goal-lifecycle-action').click()
    else {
      await ribbon.getByRole('button', { name: 'Goal actions' }).click()
      await ribbon.getByRole('menuitem', { name: 'Remove goal' }).click()
      await page.getByRole('dialog', { name: 'Remove this goal?' }).getByRole('button', { name: 'Remove goal' }).click()
    }
    await expect.poll(() => gateway.calls(`goals.${method}`).length).toBe(1)
    expect(gateway.heldReads).toHaveLength(1)
    expect(gateway.calls('goals.resume')).toHaveLength(0)
    gateway.releaseRead()
    await record(gateway, `direct-${method}`)
  })
}


test('completed Goal editing through slash input stays fail-closed during recovery and works after readiness', async ({ page }) => {
  const gateway = await prepare(page, { goal: goalSnapshot({ status: 'complete', terminalReason: 'complete', finishedAt: 2 }), holdCatalog: true })
  await page.goto(`/control/chat?session=${encodeURIComponent(SESSION)}`)
  await expect(page.locator('.goal-outcome')).toBeVisible()
  await expect.poll(() => gateway.calls('commands.list_for_surface').length).toBeGreaterThan(0)
  const input = page.locator('.chat-textarea')
  const command = '/goal edit A renewed objective after current-session recovery.'
  await input.fill(command)
  await page.locator('.chat-send-btn[aria-label="Send"]').click()
  gateway.beginRecovery()
  await expect.poll(() => gateway.heldReads.length).toBe(1)
  gateway.releaseCatalog()
  await advance(page, 500)
  expect(gateway.calls('goals.edit')).toHaveLength(0)
  gateway.releaseRead()
  await expect(page.locator('.chat-send-btn[aria-label="Send"]')).toBeEnabled()
  await advance(page, 500)
  // The outer composer also fails closed: the earlier action is not silently
  // replayed. The composed Goal mutation tests cover its own readiness waiter.
  expect(gateway.calls('goals.edit')).toHaveLength(0)
  await input.press('Enter')
  await expect.poll(() => gateway.calls('goals.edit').length).toBe(1)
  expect(gateway.calls('goals.edit')[0]!.params).toMatchObject({ expectedGoalId: 'synthetic-goal', expectedStateRevision: 1,
    objective: 'A renewed objective after current-session recovery.' })
  await expect(page.locator('.goal-ribbon')).toContainText('A renewed objective after current-session recovery.')
  expect(gateway.calls('chat.send')).toHaveLength(0)
  await record(gateway, 'completed-edit')
})
