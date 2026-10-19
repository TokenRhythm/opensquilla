import { expect, test, type Page, type WebSocketRoute } from '@playwright/test'
import { writeFile } from 'node:fs/promises'
import { helloOkResponse } from './support/gateway-fixture'
import {
  chatHistoryPayload,
  sessionMessagesHydratePayload,
  sessionMessagesSnapshotPayload,
  sessionMessagesSubscribePayload,
} from './support/session-read-fixtures'
import { expectComposerValue } from './utils'

const SESSION = 'agent:main:webchat:modern-recovery'
const OTHER = 'agent:main:webchat:modern-recovery-other'
const GENERATION = 'synthetic-modern-stream'
const DRAFT = 'Synthetic draft survives recovery without being submitted automatically.'
const REPLY = 'Synthetic live reply after stale snapshot recovery.'
const READ = 'sessions.messages.snapshot.read'
const RESUME = 'sessions.messages.resume'
const RELEASE = 'sessions.messages.snapshot.release'
const PIECE = 192 * 1024

type Request = {
  type: string
  id: string
  method: string
  params: Record<string, unknown>
  nonce?: string
}
type Observed = Request & { connection: number }
type HeldPiece = { frame: Observed; send: () => void; index: number; delivery: number }

async function prepare(page: Page, mode: 'stalled' | 'progressing' | 'healthy' | 'lost-subscribe' | 'goal-gap') {
  await page.clock.install({ time: new Date('2026-01-01T00:00:00Z') })
  await page.addInitScript(() => localStorage.setItem('opensquilla-locale', 'en'))
  await page.route('**/api/**', route => route.fulfill({ json: {} }))
  await page.route('**/api/approvals', route => route.fulfill({
    json: { pending: [], mode: 'prompt', allowPatterns: [], denyPatterns: [] },
  }))
  const sockets: WebSocketRoute[] = []
  const requests: Observed[] = []
  const held: HeldPiece[] = []
  const staged: Array<{ connection: number; delivery: number }> = []
  const snapshots = new Map<string, { id: string; bytes: Buffer; subscribed: boolean }>()
  const rejectedResumes: Observed[] = []
  const heldSubscriptions: Observed[] = []
  const initialSubscriptionReplies: Array<() => void> = []
  let holdInitialSubscription = false
  let holdHandshake = false
  const heldHandshakes: Array<() => void> = []
  const hydratedPlanRevisions: string[] = []
  const sentHistory: Array<Record<string, unknown>> = []
  const liveFrames: string[] = []
  let activeTurn: { socket: WebSocketRoute; key: string; task: string } | null = null
  let streamSequence = 0
  let lastTask: Record<string, unknown> | null = null
  let faultActive = mode === 'lost-subscribe'
  const emitTurn = (event: string, payload: Record<string, unknown>) => {
    if (!activeTurn) throw new Error('No explicit user turn to emit')
    const frame = JSON.stringify({ type: 'event', event, payload: {
      key: activeTurn.key, session_key: activeTurn.key, task_id: activeTurn.task,
      stream_generation: GENERATION, stream_seq: ++streamSequence, ...payload,
    } })
    liveFrames.push(frame)
    activeTurn.socket.send(frame)
  }
  const metadata = () => ({ stream_generation: GENERATION, current_stream_seq: streamSequence,
    run_status: activeTurn ? 'running' : 'idle',
    active_task: activeTurn ? { task_id: activeTurn.task, status: 'running' } : null,
    tasks: activeTurn ? [{ task_id: activeTurn.task, status: 'running' }] : [], last_task: lastTask })

  await page.routeWebSocket(/\/ws$/, socket => {
    const connection = sockets.push(socket)
    const epoch = `synthetic-delivery-${connection}`
    let nextDelivery = 1
    let acknowledged = 0
    const subscriptions = new Set<string>()
    socket.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    socket.onMessage(raw => {
      const frame = JSON.parse(String(raw)) as Request
      if (frame.type === 'ping') {
        socket.send(JSON.stringify({ type: 'pong', nonce: frame.nonce }))
        return
      }
      if (frame.type !== 'req') return
      const observed = { ...frame, params: frame.params ?? {}, connection }
      requests.push(observed)
      const key = String(observed.params.key ?? observed.params.sessionKey ?? SESSION)
      const respond = (payload: unknown) => socket.send(JSON.stringify({
        type: 'res', id: frame.id, ok: true, payload,
      }))
      if (faultActive && key === SESSION && (
        frame.method === 'sessions.messages.subscribe' || frame.method === 'chat.history'
      )) {
        if (frame.method === 'sessions.messages.subscribe') heldSubscriptions.push(observed)
        return
      }
      if (frame.method === 'sessions.messages.subscribe') subscriptions.add(key)
      if (frame.method === 'connect') {
        const sendHello = () => socket.send(helloOkResponse({
          protocol: 4,
          server: { conn_id: `synthetic-modern-${connection}` },
          features: { methods: [
            READ, RESUME, RELEASE, 'transport.flow.update', 'sessions.messages.subscribe',
            'sessions.messages.hydrate', 'sessions.messages.unsubscribe', 'chat.history',
            ...(mode === 'goal-gap' ? ['goals.set', 'goals.capabilities', 'sessions.create'] : []),
          ] },
          policy: {
            transport_probe_nonce: true,
            cancellable_request_methods: [READ, RESUME, 'sessions.messages.hydrate', 'chat.history'],
            transport_flow: { delivery_epoch: epoch, window_frames: 128, window_bytes: 4 * 1024 * 1024 },
          },
        }))
        if (holdHandshake) heldHandshakes.push(sendHello)
        else sendHello()
        return
      }
      if (frame.method === READ) {
        const revision = String(observed.params.sync_revision)
        const owner = `${connection}:${key}:${revision}`
        let snapshot = snapshots.get(owner)
        if (!snapshot) {
          const large = mode === 'progressing' && key === SESSION
          const value = sessionMessagesSnapshotPayload(key, {
            stream_generation: GENERATION,
            // A complete JSON snapshot requires eight physical 192 KiB pieces.
            // Every piece is confirmed through the real consumption adapter.
            events: large ? [{ event: 'session.event.text_delta', payload: {
              key, task_id: 'synthetic-progress-task', stream_generation: GENERATION,
              stream_seq: 1, text: 'x'.repeat(7 * PIECE),
            } }] : [],
            task_id: large ? 'synthetic-progress-task' : null,
            current_stream_seq: large ? 1 : 0,
          })
          snapshot = {
            id: `synthetic-snapshot-${snapshots.size + 1}`,
            bytes: Buffer.from(JSON.stringify(value)), subscribed: subscriptions.has(key),
          }
          snapshots.set(owner, snapshot)
        }
        const index = Number(observed.params.segment_index ?? 0)
        const delivery = nextDelivery++
        const payload = {
          key, sync_revision: revision, snapshot_id: snapshot.id,
          segment_index: index, segment_count: Math.ceil(snapshot.bytes.length / PIECE),
          byte_length: snapshot.bytes.length, encoding: 'base64-json-utf8',
          data: snapshot.bytes.subarray(index * PIECE, (index + 1) * PIECE).toString('base64'),
          stream_generation: GENERATION, current_stream_seq: mode === 'progressing' && key === SESSION ? 1 : 0,
          task_id: mode === 'progressing' && key === SESSION ? 'synthetic-progress-task' : null,
          session_id: null, session_epoch: null,
          delivery: { delivery_epoch: epoch, delivery_id: delivery },
        }
        if (key === SESSION && (mode === 'stalled' || mode === 'progressing' || mode === 'goal-gap')) {
          held.push({ frame: observed, send: () => respond(payload), index, delivery })
        } else respond(payload)
        return
      }
      if (frame.method === 'transport.flow.update') {
        expect(observed.params.resume ?? []).toEqual([])
        acknowledged = Math.max(acknowledged, Number(observed.params.ack_delivery_id ?? 0))
        for (const delivery of (observed.params.staged_delivery_ids ?? []) as number[]) {
          staged.push({ connection, delivery })
        }
        respond({ delivery_epoch: epoch, ack_delivery_id: acknowledged, dirty_keys: [], global_dirty: false })
        return
      }
      if (frame.method === RESUME) {
        expect(observed.params).not.toHaveProperty('ack_delivery_id')
        const snapshot = snapshots.get(`${connection}:${key}:${observed.params.sync_revision}`)
        if (mode === 'lost-subscribe' && !snapshot?.subscribed) {
          rejectedResumes.push(observed)
          socket.send(JSON.stringify({ type: 'res', id: frame.id, ok: false, error: {
            code: 'SNAPSHOT_STALE', message: 'Snapshot installation is no longer available',
            retryable: false, accepted: false,
          } }))
          return
        }
        respond({ ...observed.params, session_id: null, session_epoch: null,
          replay_to_seq: observed.params.stream_seq })
        return
      }
      if (frame.method === RELEASE) {
        respond({ ...observed.params, retired: true })
        return
      }
      if (frame.method === 'sessions.create' && mode === 'goal-gap') {
        respond({ key: SESSION, sessionId: 'synthetic-goal-session' })
        return
      }
      if (frame.method === 'goals.set' && mode === 'goal-gap') {
        const goal = {
          goalId: 'synthetic-recovery-goal', sessionKey: key, sessionId: 'synthetic-goal-session', epoch: 1,
          objective: observed.params.objective, status: 'active', stateRevision: 1, objectiveRevision: 1,
          progressRevision: 0, progress: null, continuationSeq: 0, activeTaskId: 'synthetic-goal-task',
          sourceMessageId: 'synthetic-goal-message', executionState: 'working', continuationDeferredReason: null,
          turnsStarted: 1, turnsSettled: 0, windowTurnsStarted: 1, activeTimeMs: 0, windowActiveTimeMs: 0,
          usage: { inputTokens: 0, outputTokens: 0, reasoningTokens: 0, cacheReadTokens: 0,
            cacheWriteTokens: 0, totalTokens: 0 },
          pauseReason: null, blockedReason: null, terminalReason: null, createdAt: 1, updatedAt: 1, finishedAt: null,
        }
        respond({ accepted: true, clientRequestId: observed.params.clientRequestId, sessionKey: key,
          sessionId: goal.sessionId, epoch: 1, taskId: goal.activeTaskId, userMessageId: goal.sourceMessageId,
          previousGoalId: null, goal })
        return
      }
      if (frame.method === 'chat.send') {
        expect(activeTurn).toBeNull()
        activeTurn = { socket, key, task: 'synthetic-modern-send' }
        sentHistory.push({ role: 'user', text: observed.params.message, id: 'modern-recovery-user',
          client_message_id: observed.params.clientMessageId, turn_id: activeTurn.task })
        respond({ accepted: true, session: key, task_id: activeTurn.task })
        emitTurn('task.running', { status: 'running' })
        return
      }
      if (frame.method === 'sessions.messages.hydrate' && mode === 'lost-subscribe') {
        // The initial read can predate the missed plan update. Recovery must
        // fetch current control state rather than reinstall its cached Promise.
        const revision = hydratedPlanRevisions.length === 0 ? 'initial-plan' : 'recovered-plan'
        hydratedPlanRevisions.push(revision)
        respond(sessionMessagesHydratePayload(key, {
          ...metadata(),
          currentPlan: {
            revisionId: revision, planId: 'recovery-plan', generation: hydratedPlanRevisions.length,
            title: revision === 'initial-plan' ? 'Outdated recovery proposal' : 'Current recovery proposal',
            markdown: 'Synthetic proposal changed while delivery was unavailable.',
            current: true, steps: [{ stepId: 'inspect', title: 'Inspect the recovered state' }],
          },
        }))
        return
      }
      const payloads: Record<string, unknown> = {
        'agents.list': { agents: [] },
        'commands.list_for_surface': { commands: [] },
        'config.get': { squilla_router: { enabled: false, tiers: {} }, permissions: {}, skills: {} },
        'models.routing.get': { mode: 'direct' },
        'sessions.list': { sessions: [SESSION, OTHER].map((item, index) => ({
          key: item, title: index ? 'Other recovery session' : 'Modern recovery session',
          sessionKind: 'chat', surface: 'webchat', conversationKind: 'direct',
          effectiveAgentId: 'main', updatedAt: 100 + index, messageCount: 1,
          status: 'ok', runStatus: 'idle',
        })), count: 2, ts: 1_800_000_000, has_more: false },
        'chat.history': chatHistoryPayload([{
          role: 'user', text: 'Synthetic cached history remains available.',
          message_id: `history-${key}`, timestamp: '2026-01-01T00:00:00Z',
        }, ...(key === SESSION ? sentHistory : [])]),
        'sessions.messages.subscribe': sessionMessagesSubscribePayload(key, {
          stream_generation: GENERATION,
          ...(mode === 'lost-subscribe' ? { hydration_complete: false } : {}),
        }),
        'sessions.messages.hydrate': sessionMessagesHydratePayload(key, metadata()),
        'sessions.messages.unsubscribe': null,
        'sessions.subscribe': { subscribed: true },
        'usage.status': { sessions: [] },
      }
      const response = Object.hasOwn(payloads, frame.method) ? payloads[frame.method] : {}
      if (frame.method === 'sessions.messages.subscribe' && holdInitialSubscription) {
        initialSubscriptionReplies.push(() => respond(response))
      } else respond(response)
    })
  })
  const tick = () => sockets.forEach(socket => socket.send(JSON.stringify({
    type: 'event', event: 'tick', payload: { time_ms: Date.now() }, seq: 1,
  })))
  return { sockets, requests, held, staged, tick, rejectedResumes, heldSubscriptions, hydratedPlanRevisions,
    beginReply: () => emitTurn('session.event.text_delta', { text: REPLY }),
    finishReply: () => {
      if (!activeTurn) throw new Error('No explicit user turn to finish')
      const socket = activeTurn.socket
      sentHistory.push({ role: 'assistant', text: REPLY, id: 'modern-recovery-answer', turn_id: activeTurn.task })
      emitTurn('session.event.done', { reason: 'completed', text_snapshot: REPLY })
      emitTurn('task.succeeded', { status: 'succeeded', terminal_reason: 'succeeded' })
      lastTask = { task_id: activeTurn.task, status: 'succeeded' }
      activeTurn = null
      socket.send(JSON.stringify({ type: 'event', event: 'sessions.changed',
        payload: { key: SESSION, session_key: SESSION, reason: 'task_terminal', ...metadata() } }))
    },
    replayTurn: () => { for (const frame of liveFrames) sockets.at(-1)!.send(frame) },
    releaseFault: () => { faultActive = false },
    holdNextHandshake: () => { holdHandshake = true },
    releaseHandshake: () => {
      holdHandshake = false
      for (const respond of heldHandshakes.splice(0)) respond()
    },
    invalidateSession: (key = SESSION) => {
      const connection = sockets.length
      sockets.at(-1)!.send(JSON.stringify({ type: 'event', event: 'transport.flow.dirty', payload: {
        delivery_epoch: `synthetic-delivery-${connection}`, dirty_keys: [key], global_dirty: false,
      } }))
    },
    holdNextSubscription: () => { holdInitialSubscription = true },
    releaseInitialSubscription: () => {
      holdInitialSubscription = false
      for (const respond of initialSubscriptionReplies.splice(0)) respond()
    } }
}

async function advance(page: Page, milliseconds: number) {
  for (let elapsed = 0; elapsed < milliseconds; elapsed += 1_000) {
    await page.clock.runFor(Math.min(1_000, milliseconds - elapsed))
    // Permit WebSocket response promises and Vue rendering between clock steps.
    await page.evaluate(() => Promise.resolve())
  }
}

function calls(requests: Observed[], method: string, connection?: number) {
  return requests.filter(item => item.method === method
    && (connection === undefined || item.connection === connection))
}

async function recordRecoveryOutcome(page: Page, requests: Observed[], started: number, ready: boolean) {
  const work = Object.fromEntries([...new Set(requests.map(item => item.method))]
    .map(method => [method, calls(requests, method).length]))
  const body = JSON.stringify({ evidence: 'Chromium with a synthetic Gateway and controlled clock',
    ready, observedRecoveryMs: (await page.evaluate(() => performance.now())) - started,
    requestsByMethod: work }, null, 2)
  await writeFile(test.info().outputPath('recovery-outcome.json'), body)
  await test.info().attach('recovery-outcome.json', { body, contentType: 'application/json' })
}

async function recordHandoffOutcome(requests: Observed[], entry = 'warm public route/history-state handoff contract') {
  const body = JSON.stringify({
    evidence: `Chromium with a synthetic Gateway; ${entry}`,
    acceptedRequests: calls(requests, 'chat.send').map(request => request.params),
    connectionCount: calls(requests, 'connect').length,
  }, null, 2)
  await writeFile(test.info().outputPath('handoff-outcome.json'), body)
  await test.info().attach('handoff-outcome.json', { body, contentType: 'application/json' })
}

async function openWarmAutoSendHandoff(page: Page) {
  const gateway = await prepare(page, 'healthy')
  await page.goto(`/control/chat?session=${encodeURIComponent(SESSION)}`)
  await expect(page.locator('.chat-send-btn[aria-label="Send"]')).toBeEnabled()
  // History state is a supported consumer contract. Mount a fresh ChatView
  // with an established transport, using only public navigation/state inputs.
  // This does not establish a currently shipped autosend-producing UI button.
  await page.getByRole('link', { name: 'Skills & Channels', exact: true }).click()
  await expect(page).toHaveURL(/\/control\/skills$/)
  await expect(page.locator('.chat-textarea')).toHaveCount(0)
  gateway.holdNextSubscription()
  await page.evaluate(prefill => {
    window.history.pushState({ ...window.history.state, prefill, autosend: true }, '', '/control/chat/new?agent=main')
    window.dispatchEvent(new PopStateEvent('popstate', { state: window.history.state }))
  }, DRAFT)
  await expect.poll(() => calls(gateway.requests, 'sessions.messages.subscribe').length).toBe(2)
  const input = page.locator('.chat-textarea')
  await expectComposerValue(input, DRAFT)
  expect(calls(gateway.requests, 'chat.send')).toHaveLength(0)
  return { gateway, input }
}

test('automatically sends an unchanged new-task handoff exactly once after subscription readiness', async ({ page }) => {
  test.setTimeout(90_000)
  const { gateway, input } = await openWarmAutoSendHandoff(page)
  const subscribedKey = calls(gateway.requests, 'sessions.messages.subscribe')[1]!.params.key
  gateway.releaseInitialSubscription()
  await expect.poll(() => calls(gateway.requests, 'chat.send').length).toBe(1)
  expect(calls(gateway.requests, 'chat.send')[0]!.params).toMatchObject({
    sessionKey: subscribedKey, message: DRAFT, intent: 'new_chat',
  })
  await expectComposerValue(input, '')
  await advance(page, 10_000)
  expect(calls(gateway.requests, 'chat.send')).toHaveLength(1)
  await expect(page.getByText(DRAFT, { exact: true })).toHaveCount(1)
  await recordHandoffOutcome(gateway.requests)
})

test('suppresses new-task handoff autosend after an edit and sends the edited draft only on user action', async ({ page }) => {
  test.setTimeout(90_000)
  const { gateway, input } = await openWarmAutoSendHandoff(page)
  const edited = 'User edited the automatic handoff while waiting for the initial subscription.'
  await input.fill(edited)
  gateway.releaseInitialSubscription()
  const send = page.locator('.chat-send-btn[aria-label="Send"]')
  await expect(send).toBeEnabled()
  await advance(page, 10_000)
  expect(calls(gateway.requests, 'chat.send')).toHaveLength(0)
  await expectComposerValue(input, edited)

  await input.press('Enter')
  await expect.poll(() => calls(gateway.requests, 'chat.send').length).toBe(1)
  expect(calls(gateway.requests, 'chat.send')[0]!.params).toMatchObject({ message: edited, intent: 'new_chat' })
  await expectComposerValue(input, '')
  await advance(page, 10_000)
  expect(calls(gateway.requests, 'chat.send')).toHaveLength(1)
  await expect(page.getByText(edited, { exact: true })).toHaveCount(1)
  await expect(page.getByText(DRAFT, { exact: true })).toHaveCount(0)
  await recordHandoffOutcome(gateway.requests)
})

test('keeps autosend cancelled after edit then undo until an explicit send', async ({ page }) => {
  test.setTimeout(90_000)
  const { gateway, input } = await openWarmAutoSendHandoff(page)
  await input.fill('A deliberate temporary user edit.')
  await input.fill(DRAFT)
  gateway.releaseInitialSubscription()
  await expect(page.locator('.chat-send-btn[aria-label="Send"]')).toBeEnabled()
  await advance(page, 10_000)
  expect(calls(gateway.requests, 'chat.send')).toHaveLength(0)
  await expectComposerValue(input, DRAFT)
  await input.press('Enter')
  await expect.poll(() => calls(gateway.requests, 'chat.send').length).toBe(1)
  expect(calls(gateway.requests, 'chat.send')[0]!.params).toMatchObject({ message: DRAFT, intent: 'new_chat' })
  await expectComposerValue(input, '')
  await advance(page, 10_000)
  expect(calls(gateway.requests, 'chat.send')).toHaveLength(1)
  await expect(page.getByText(DRAFT, { exact: true })).toHaveCount(1)
  await recordHandoffOutcome(gateway.requests)
})

test('automatically replaces a stale snapshot after a lost subscribe and hydrates the recovered ACK', async ({ page }) => {
  const gateway = await prepare(page, 'lost-subscribe')
  await page.goto(`/control/chat?session=${encodeURIComponent(SESSION)}`)
  // Bound page startup and the mocked Gateway handshake separately from the
  // fake-clock recovery budgets.
  await expect.poll(() => ({
    connects: calls(gateway.requests, 'connect').length,
    reads: calls(gateway.requests, READ).length,
  }), { timeout: 15_000 }).toEqual({ connects: 1, reads: 1 })
  await expect.poll(() => gateway.staged.length).toBe(1)
  const input = page.locator('.chat-textarea')
  const send = page.locator('.chat-send-btn[aria-label="Send"]')
  await input.fill(DRAFT)
  const editor = await input.elementHandle()
  const originalUrl = page.url()
  const recoveryStarted = await page.evaluate(() => performance.now())
  await advance(page, 7_100)
  const notice = page.getByTestId('chat-session-recovery-status')
  await expect(notice).toHaveCount(0)
  await expect(send).toBeDisabled()

  gateway.releaseFault()
  // Exercise production recovery scheduling without clicking, changing focus,
  // reconnecting the transport, or changing any of its timeout budgets.
  for (let seconds = 0; seconds < 30; seconds++) {
    await advance(page, 1_000)
    if (calls(gateway.requests, RESUME).length >= 2
      && calls(gateway.requests, 'sessions.messages.hydrate').length > 0
      && await send.isEnabled()) break
  }
  expect(gateway.rejectedResumes).toHaveLength(1)
  const reads = calls(gateway.requests, READ)
  const resumes = calls(gateway.requests, RESUME)
  expect(reads).toHaveLength(2)
  expect(resumes).toHaveLength(2)
  expect(resumes.map(call => call.params.sync_revision)).toEqual(reads.map(call => call.params.sync_revision))
  expect(reads[1]!.params.sync_revision).not.toBe(reads[0]!.params.sync_revision)
  expect(calls(gateway.requests, RELEASE).map(call => call.params.sync_revision)).toEqual([reads[0]!.params.sync_revision])
  expect(gateway.heldSubscriptions.length).toBeGreaterThan(0)
  expect(calls(gateway.requests, 'sessions.messages.subscribe')).toHaveLength(gateway.heldSubscriptions.length + 1)
  expect(calls(gateway.requests, 'sessions.messages.hydrate')).toHaveLength(2)
  expect(gateway.hydratedPlanRevisions).toEqual(['initial-plan', 'recovered-plan'])
  await expect(page.getByText('Current recovery proposal', { exact: true })).toBeVisible()
  await expect(page.getByText('Outdated recovery proposal', { exact: true })).toHaveCount(0)
  await expect(notice).toHaveCount(0)
  await expect(send).toBeEnabled()
  await recordRecoveryOutcome(page, gateway.requests, recoveryStarted, true)
  await expectComposerValue(input, DRAFT)
  expect(await input.evaluate((node, original) => node === original, editor)).toBe(true)
  expect(await input.evaluate(node => document.activeElement === node)).toBe(true)
  expect(page.url()).toBe(originalUrl)
  expect(gateway.sockets).toHaveLength(1)
  expect(calls(gateway.requests, 'chat.send')).toHaveLength(0)
  await expect(page.getByText('Synthetic cached history remains available.', { exact: true })).toHaveCount(1)
  await advance(page, 15_000)
  expect(calls(gateway.requests, RESUME)).toHaveLength(2)
  expect(calls(gateway.requests, 'sessions.messages.hydrate')).toHaveLength(2)
  expect(gateway.hydratedPlanRevisions).toEqual(['initial-plan', 'recovered-plan'])
  expect(calls(gateway.requests, 'chat.send')).toHaveLength(0)
  await expectComposerValue(input, DRAFT)
  expect(await input.evaluate(node => document.activeElement === node)).toBe(true)
  expect(page.url()).toBe(originalUrl)

  // A new user action must work on the recovered lease, with real consumer
  // rendering before the terminal frame and no repeated draft submission.
  await input.press('Enter')
  await expect.poll(() => calls(gateway.requests, 'chat.send').length).toBe(1)
  expect(calls(gateway.requests, 'chat.send')[0]!.params).toMatchObject({ sessionKey: SESSION, message: DRAFT })
  await expectComposerValue(input, '')
  gateway.beginReply()
  const reply = page.locator('.msg-ai-text').filter({ hasText: REPLY })
  await expect(reply).toHaveCount(1)
  await expect(reply).toHaveText(REPLY)
  gateway.finishReply()
  await advance(page, 1_000)
  gateway.replayTurn()
  await advance(page, 10_000)
  await expect(reply).toHaveCount(1)
  await expect(reply).toHaveText(REPLY)
  await expect(page.getByText(DRAFT, { exact: true })).toHaveCount(1)
  expect(calls(gateway.requests, 'chat.send')).toHaveLength(1)
  await expect(send).toBeEnabled()
  await expect(notice).toHaveCount(0)
  expect(page.url()).toBe(originalUrl)
  expect(await input.evaluate((node, original) => node === original, editor)).toBe(true)
  expect(gateway.sockets).toHaveLength(1)
})

test('keeps exhausted live recovery quiet and exposes a bounded composer retry', async ({ page }) => {
  const gateway = await prepare(page, 'stalled')
  await page.goto(`/control/chat?session=${encodeURIComponent(SESSION)}`)
  await expect.poll(() => gateway.held.length).toBeGreaterThan(0)
  await page.locator('.chat-textarea').fill(DRAFT)
  const recoveryStarted = await page.evaluate(() => performance.now())
  await advance(page, 16_000)
  const notice = page.getByTestId('chat-session-recovery-status')
  await expect(notice).toHaveCount(0)
  await expect(page.getByText('Synthetic cached history remains available.', { exact: true })).toBeVisible()
  await expect(page.locator('.chat-send-btn[aria-label="Send"]')).toBeDisabled()
  await advance(page, 105_000)
  await expect(notice).toHaveCount(0)
  const retry = page.getByTestId('chat-composer-recovery')
  await expect(retry).toHaveAttribute('data-recovery-action', 'retry')
  await expect(retry).toBeEnabled()
  const readsAtDeadline = calls(gateway.requests, READ).length
  await expect(notice).toHaveCount(0)

  for (let seconds = 0; seconds < 60; seconds += 5) {
    await page.evaluate(() => {
      window.dispatchEvent(new Event('online'))
      document.dispatchEvent(new Event('visibilitychange'))
    })
    await advance(page, 5_000)
    await expect(notice).toHaveCount(0)
    await expectComposerValue(page.locator('.chat-textarea'), DRAFT)
  }
  expect(calls(gateway.requests, READ)).toHaveLength(readsAtDeadline)
  await recordRecoveryOutcome(page, gateway.requests, recoveryStarted, false)
  expect(gateway.sockets).toHaveLength(1)
  expect(calls(gateway.requests, 'chat.send')).toHaveLength(0)
  expect(calls(gateway.requests, 'sessions.messages.snapshot')).toHaveLength(0)
  const abandoned = calls(gateway.requests, READ).at(-1)!.params.sync_revision
  await page.locator(`[data-session-key="${OTHER}"] .sidebar-history-item`).click()
  await expect.poll(() => calls(gateway.requests, RELEASE).some(item => (
    item.params.key === SESSION && item.params.sync_revision === abandoned
  ))).toBe(true)
  await expect.poll(() => calls(gateway.requests, RESUME).some(item => item.params.key === OTHER)).toBe(true)
  await expect(notice).toHaveCount(0)
})

test('finishes a progressing 48 second transfer with its original sync revision', async ({ page }) => {
  const gateway = await prepare(page, 'progressing')
  await page.goto(`/control/chat?session=${encodeURIComponent(SESSION)}`)
  await expect.poll(() => gateway.held.length).toBe(1)
  await page.locator('.chat-textarea').fill(DRAFT)
  const revision = gateway.held[0]!.frame.params.sync_revision
  const recoveryStarted = await page.evaluate(() => performance.now())
  const notice = page.getByTestId('chat-session-recovery-status')
  for (let index = 0; index < 8; index++) {
    await expect.poll(() => gateway.held.length).toBe(index + 1)
    const piece = gateway.held[index]!
    expect(piece.index).toBe(index)
    expect(piece.frame.params.sync_revision).toBe(revision)
    await advance(page, 6_000)
    await expect(notice).toHaveCount(0)
    // Production sends an ordinary heartbeat every 30 seconds. It must not
    // become a Conversation invalidation and queue another complete snapshot.
    if (index === 4) gateway.tick()
    piece.send()
    // Flow credit has a scheduled flush; let the production timer run.
    await advance(page, 100)
    await expect.poll(() => gateway.staged.some(item => item.delivery === piece.delivery)).toBe(true)
  }
  await expect.poll(() => calls(gateway.requests, RESUME).length).toBe(1)
  expect(calls(gateway.requests, RESUME)[0]!.params.sync_revision).toBe(revision)
  await expect(notice).toHaveCount(0)
  // This snapshot contains an active task, so authoritative task control is
  // the visible readiness affordance rather than an idle Send button.
  await expect(page.getByRole('button', { name: 'Stop current response', exact: true })).toBeEnabled()
  await recordRecoveryOutcome(page, gateway.requests, recoveryStarted, true)
  await expectComposerValue(page.locator('.chat-textarea'), DRAFT)
  expect(calls(gateway.requests, READ)).toHaveLength(8)
  expect(calls(gateway.requests, RELEASE)).toHaveLength(0)
  expect(calls(gateway.requests, 'chat.send')).toHaveLength(0)
  expect(gateway.sockets).toHaveLength(1)
})

test('reconnects through modern resume while preserving the draft and sends it exactly once', async ({ page }) => {
  const gateway = await prepare(page, 'healthy')
  await page.goto(`/control/chat?session=${encodeURIComponent(SESSION)}`)
  await expect.poll(() => calls(gateway.requests, RESUME, 1).length).toBe(1)
  const input = page.locator('.chat-textarea')
  await input.fill(DRAFT)
  const send = page.locator('.chat-send-btn[aria-label="Send"]')
  await expect(send).toBeEnabled()
  const originalUrl = page.url()
  const firstRevision = calls(gateway.requests, READ, 1)[0]!.params.sync_revision
  expect(JSON.stringify(calls(gateway.requests, 'connect')[0]!.params)).toContain('transport.recovery.v1')
  const recoveryStarted = await page.evaluate(() => performance.now())
  gateway.sockets[0]!.close({ code: 1012, reason: 'synthetic restart' })
  await advance(page, 2_000)
  await expect.poll(() => calls(gateway.requests, RESUME, 2).length).toBe(1)
  await expect(send).toBeEnabled()
  await recordRecoveryOutcome(page, gateway.requests, recoveryStarted, true)
  await expectComposerValue(input, DRAFT)
  expect(page.url()).toBe(originalUrl)
  expect(calls(gateway.requests, READ, 2)[0]!.params.sync_revision).not.toBe(firstRevision)
  expect(calls(gateway.requests, RELEASE, 2).some(item => item.params.sync_revision === firstRevision)).toBe(false)
  expect(calls(gateway.requests, 'chat.send')).toHaveLength(0)
  await input.press('Enter')
  await expect.poll(() => calls(gateway.requests, 'chat.send').length).toBe(1)
  expect(calls(gateway.requests, 'chat.send')[0]!.params).toMatchObject({ sessionKey: SESSION, message: DRAFT })
  await expectComposerValue(input, '')
  await advance(page, 10_000)
  expect(calls(gateway.requests, 'chat.send')).toHaveLength(1)
  expect(gateway.sockets).toHaveLength(2)
})


for (const { edit, entry } of [
  { edit: 'unchanged', entry: '/control/chat/new?agent=main' },
  { edit: 'edited', entry: '/control/chat/new?agent=main' },
  { edit: 'edit-undo', entry: '/control/chat/new?agent=main' },
  { edit: 'unchanged', entry: '/control/chat' },
] as const) {
  test(`cold ${entry === '/control/chat' ? 'canonical route ' : ''}handoff first authority binding ${edit === 'unchanged' ? 'sends once' : `preserves ${edit} cancellation`}`, async ({ page }) => {
    test.setTimeout(90_000)
    const gateway = await prepare(page, 'healthy')
    gateway.holdNextHandshake()
    await page.addInitScript(prefill => {
      window.history.replaceState({ ...window.history.state, prefill, autosend: true }, '', window.location.href)
    }, DRAFT)
    await page.goto(entry)
    const input = page.locator('.chat-textarea')
    await expectComposerValue(input, DRAFT)
    await expect.poll(() => calls(gateway.requests, 'connect').length).toBe(1)
    expect(calls(gateway.requests, 'sessions.messages.subscribe')).toHaveLength(0)
    expect(calls(gateway.requests, 'chat.send')).toHaveLength(0)
    const edited = 'User edited a cold handoff before the first authority proof.'
    if (edit !== 'unchanged') await input.fill(edited)
    if (edit === 'edit-undo') await input.fill(DRAFT)
    gateway.releaseHandshake()
    if (edit === 'unchanged') {
      try {
        await expect.poll(() => calls(gateway.requests, 'chat.send').length).toBe(1)
      } finally {
        const observation = { url: page.url(), entry, requests: gateway.requests,
          composerText: await input.innerText(),
          sendEnabled: await page.locator('.chat-send-btn[aria-label="Send"]').isEnabled().catch(() => false),
          historyState: await page.evaluate(() => window.history.state),
        }
        await writeFile(test.info().outputPath('cold-observation.json'), JSON.stringify(observation, null, 2))
      }
    } else {
      await expect(page.locator('.chat-send-btn[aria-label="Send"]')).toBeEnabled()
      await advance(page, 10_000)
      expect(calls(gateway.requests, 'chat.send')).toHaveLength(0)
      await expectComposerValue(input, edit === 'edited' ? edited : DRAFT)
      await input.press('Enter')
      await expect.poll(() => calls(gateway.requests, 'chat.send').length).toBe(1)
    }
    expect(calls(gateway.requests, 'chat.send')[0]!.params).toMatchObject({
      message: edit === 'edited' ? edited : DRAFT, intent: 'new_chat',
    })
    await expectComposerValue(input, '')
    await advance(page, 10_000)
    expect(calls(gateway.requests, 'chat.send')).toHaveLength(1)
    expect(calls(gateway.requests, 'connect')).toHaveLength(1)
    await recordHandoffOutcome(gateway.requests, `cold initial mount, held first authenticated hello, ${edit}`)
  })
}

test('Goal materialization waits for a fresh read after a wire gap during initial installation', async ({ page }) => {
  test.setTimeout(90_000)
  const gateway = await prepare(page, 'goal-gap')
  await page.goto('/control/chat/new?agent=main')
  const input = page.locator('.chat-textarea')
  const send = page.locator('.chat-send-btn[aria-label="Send"]')
  await expect(send).toBeEnabled()
  await expect.poll(() => calls(gateway.requests, RESUME).length).toBe(1)
  await advance(page, 100)
  await page.getByRole('button', { name: 'Add', exact: true }).click()
  await expect(page.getByRole('menuitem', { name: /Goal mode/ })).toBeVisible()
  await page.getByRole('menuitem', { name: /Goal mode/ }).click()
  await expect(page.locator('.composer-goal-mode')).toBeVisible()
  const objective = 'Wait for the current session recovery before accepting this Goal.'
  await input.fill(objective)
  await send.click()
  await expect.poll(() => calls(gateway.requests, 'sessions.create').length).toBe(1)
  await expect.poll(() => gateway.held.length).toBe(1)
  expect(calls(gateway.requests, 'goals.set')).toHaveLength(0)
  gateway.invalidateSession()
  await advance(page, 100)
  gateway.held[0]!.send()
  await advance(page, 100)
  await expect.poll(() => gateway.held.length).toBe(2)
  expect(calls(gateway.requests, 'goals.set')).toHaveLength(0)
  await advance(page, 1_000)
  expect(calls(gateway.requests, 'goals.set')).toHaveLength(0)
  // Only the read launched after the dirty event may complete admission.
  gateway.held[1]!.send()
  await advance(page, 100)
  await expect.poll(() => calls(gateway.requests, 'goals.set').length).toBe(1)
  expect(calls(gateway.requests, 'goals.set')[0]!.params).toMatchObject({ sessionKey: SESSION, objective })
  await expect(page.locator('.goal-ribbon')).toContainText(objective)
  await advance(page, 10_000)
  expect(calls(gateway.requests, 'goals.set')).toHaveLength(1)
  expect(calls(gateway.requests, 'chat.send')).toHaveLength(0)
  const reads = calls(gateway.requests, READ).filter(item => item.params.key === SESSION)
  const resumes = calls(gateway.requests, RESUME).filter(item => item.params.key === SESSION)
  expect(reads).toHaveLength(2)
  // The real adapter invalidates the first installation before resume. This
  // public browser path complements the composed stale-result unit contract.
  expect(resumes).toHaveLength(1)
  expect(resumes[0]!.params.sync_revision).toBe(reads[1]!.params.sync_revision)
  expect(reads[1]!.params.sync_revision).not.toBe(reads[0]!.params.sync_revision)
  const body = JSON.stringify({ evidence: 'Chromium public Goal composer, materialization RPC and wire dirty event; synthetic Gateway',
    requests: gateway.requests, predecessor: reads[0]!.params.sync_revision, successor: reads[1]!.params.sync_revision,
    goalsSetCount: calls(gateway.requests, 'goals.set').length }, null, 2)
  await writeFile(test.info().outputPath('goal-readiness-outcome.json'), body)
  await test.info().attach('goal-readiness-outcome.json', { body, contentType: 'application/json' })
})


test('a reconnect replacement becomes ready after the previous connection queued gap recovery', async ({ page }) => {
  test.setTimeout(90_000)
  const gateway = await prepare(page, 'stalled')
  await page.goto(`/control/chat?session=${encodeURIComponent(SESSION)}`)
  await expect.poll(() => gateway.held.length).toBe(1)
  const input = page.locator('.chat-textarea')
  const send = page.locator('.chat-send-btn[aria-label="Send"]')
  await input.fill(DRAFT)
  await expect(send).toBeDisabled()
  const originalRevision = gateway.held[0]!.frame.params.sync_revision
  const recoveryStarted = await page.evaluate(() => performance.now())
  gateway.invalidateSession()
  await advance(page, 100)
  gateway.sockets[0]!.close({ code: 1012, reason: 'synthetic reconnect with queued gap recovery' })
  await advance(page, 2_000)
  await expect.poll(() => gateway.held.filter(piece => piece.frame.connection === 2).length).toBe(1)
  const current = gateway.held.find(piece => piece.frame.connection === 2)!
  expect(current.frame.params.sync_revision).not.toBe(originalRevision)
  // A real browser response completes asynchronously. The separate unit test
  // covers synchronous Promise completion during coordinator replacement.
  current.send()
  await advance(page, 100)
  await expect.poll(() => calls(gateway.requests, RESUME, 2).length).toBe(1)
  await expect(send).toBeEnabled()
  await expectComposerValue(input, DRAFT)
  expect(calls(gateway.requests, 'chat.send')).toHaveLength(0)
  await advance(page, 10_000)
  await expect(send).toBeEnabled()
  expect(calls(gateway.requests, READ, 2)).toHaveLength(1)
  await input.press('Enter')
  await expect.poll(() => calls(gateway.requests, 'chat.send').length).toBe(1)
  expect(calls(gateway.requests, 'chat.send')[0]!.params).toMatchObject({ sessionKey: SESSION, message: DRAFT })
  await expectComposerValue(input, '')
  await recordRecoveryOutcome(page, gateway.requests, recoveryStarted, true)
})


for (const edit of ['unchanged', 'edited', 'edit-undo'] as const) {
  test(`same ChatView route handoff ${edit === 'unchanged' ? 'sends once' : `preserves ${edit} cancellation`}`, async ({ page }) => {
    test.setTimeout(90_000)
    const gateway = await prepare(page, 'healthy')
    await page.goto(`/control/chat?session=${encodeURIComponent(SESSION)}`)
    const input = page.locator('.chat-textarea')
    await expect(page.locator('.chat-send-btn[aria-label="Send"]')).toBeEnabled()
    const originalEditor = await input.elementHandle()
    gateway.holdNextSubscription()
    // Supported history-state consumer contract on the existing ChatView.
    // No private Vue/router state or currently shipped producer is assumed.
    await page.evaluate(prefill => {
      window.history.pushState({ ...window.history.state, prefill, autosend: true }, '', '/control/chat/new?agent=main')
      window.dispatchEvent(new PopStateEvent('popstate', { state: window.history.state }))
    }, DRAFT)
    await expect.poll(() => calls(gateway.requests, 'sessions.messages.subscribe').length).toBe(2)
    await expectComposerValue(input, DRAFT)
    expect(await input.evaluate((node, original) => node === original, originalEditor)).toBe(true)
    expect(calls(gateway.requests, 'chat.send')).toHaveLength(0)
    const edited = 'User edited a prefill while reusing the current ChatView.'
    if (edit !== 'unchanged') await input.fill(edited)
    if (edit === 'edit-undo') await input.fill(DRAFT)
    gateway.releaseInitialSubscription()
    if (edit === 'unchanged') {
      await expect.poll(() => calls(gateway.requests, 'chat.send').length).toBe(1)
    } else {
      await expect(page.locator('.chat-send-btn[aria-label="Send"]')).toBeEnabled()
      await advance(page, 10_000)
      expect(calls(gateway.requests, 'chat.send')).toHaveLength(0)
      await expectComposerValue(input, edit === 'edited' ? edited : DRAFT)
      await input.press('Enter')
      await expect.poll(() => calls(gateway.requests, 'chat.send').length).toBe(1)
    }
    expect(calls(gateway.requests, 'chat.send')[0]!.params).toMatchObject({
      message: edit === 'edited' ? edited : DRAFT, intent: 'new_chat',
    })
    await expectComposerValue(input, '')
    await advance(page, 10_000)
    expect(calls(gateway.requests, 'chat.send')).toHaveLength(1)
    expect(await input.evaluate((node, original) => node === original, originalEditor)).toBe(true)
    await recordHandoffOutcome(gateway.requests, `same ChatView and composer DOM, public history-state route entry, ${edit}`)
  })
}


for (const editUndo of [false, true]) {
  test(`cold canonical handoff waits for real attachment draft restoration${editUndo ? ' and retains edit-undo cancellation' : ''}`, async ({ page }) => {
    test.setTimeout(90_000)
    const gateway = await prepare(page, 'healthy')
    gateway.holdNextHandshake()
    await page.addInitScript(prefill => {
      window.history.replaceState({ ...window.history.state, prefill, autosend: true }, '', window.location.href)
      // Fault injection at the public IndexedDB API: let the native database
      // open and upgrade normally, but hold its success notification. The
      // real application load/empty transaction still executes after release.
      const originalOpen = IDBFactory.prototype.open
      let holding = true
      const releases: Array<() => void> = []
      const observation = { held: 0, released: 0 }
      Object.defineProperty(window, '__attachmentDbGate', { value: observation })
      IDBFactory.prototype.open = function(name, version) {
        const request = version === undefined ? originalOpen.call(this, name) : originalOpen.call(this, name, version)
        if (name !== 'opensquilla-attachment-drafts') return request
        request.addEventListener('success', event => {
          if (!holding) return
          event.stopImmediatePropagation()
          observation.held++
          releases.push(() => {
            observation.released++
            request.dispatchEvent(new Event('success'))
          })
        }, { once: true })
        return request
      }
      window.addEventListener('e2e-release-attachment-db', () => {
        holding = false
        for (const release of releases.splice(0)) release()
      })
    }, DRAFT)
    await page.goto('/control/chat')
    const input = page.locator('.chat-textarea')
    await expectComposerValue(input, DRAFT)
    gateway.releaseHandshake()
    await expect.poll(() => page.evaluate(() => (
      window as unknown as { __attachmentDbGate: { held: number } }
    ).__attachmentDbGate.held)).toBe(1)
    await expect.poll(() => calls(gateway.requests, RESUME).length).toBeGreaterThan(0)
    await expect(page).toHaveURL(/\/control\/chat\/new$/)
    await advance(page, 2_000)
    expect(calls(gateway.requests, 'chat.send')).toHaveLength(0)
    await expectComposerValue(input, DRAFT)
    if (editUndo) {
      await input.fill('A deliberate edit while attachment restoration is pending.')
      await input.fill(DRAFT)
    }
    await page.evaluate(() => window.dispatchEvent(new Event('e2e-release-attachment-db')))
    if (editUndo) {
      await expect(page.locator('.chat-send-btn[aria-label="Send"]')).toBeEnabled()
      await advance(page, 10_000)
      expect(calls(gateway.requests, 'chat.send')).toHaveLength(0)
      await expectComposerValue(input, DRAFT)
      await input.press('Enter')
    }
    await expect.poll(() => calls(gateway.requests, 'chat.send').length).toBe(1)
    expect(calls(gateway.requests, 'chat.send')[0]!.params).toMatchObject({ message: DRAFT, intent: 'new_chat' })
    await expectComposerValue(input, '')
    await advance(page, 10_000)
    expect(calls(gateway.requests, 'chat.send')).toHaveLength(1)
    await expect(page.getByText('Wait for file attachment processing to finish', { exact: true })).toHaveCount(0)
    const observation = await page.evaluate(() => (
      window as unknown as { __attachmentDbGate: { held: number; released: number } }
    ).__attachmentDbGate)
    expect(observation).toEqual({ held: 1, released: 1 })
    const body = JSON.stringify({ evidence: 'Native IndexedDB with held public open success notification, ordinary ChatView and synthetic Gateway',
      editUndo, observation, requests: gateway.requests }, null, 2)
    await writeFile(test.info().outputPath('attachment-restore-outcome.json'), body)
    await test.info().attach('attachment-restore-outcome.json', { body, contentType: 'application/json' })
  })
}
