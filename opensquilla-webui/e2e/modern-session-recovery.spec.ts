import { expect, test, type Page, type WebSocketRoute } from '@playwright/test'
import { helloOkResponse } from './support/gateway-fixture'
import {
  chatHistoryPayload,
  sessionMessagesHydratePayload,
  sessionMessagesSnapshotPayload,
  sessionMessagesSubscribePayload,
} from './support/session-read-fixtures'

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

async function prepare(page: Page, mode: 'stalled' | 'progressing' | 'healthy' | 'lost-subscribe') {
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
        socket.send(helloOkResponse({
          protocol: 4,
          server: { conn_id: `synthetic-modern-${connection}` },
          features: { methods: [
            READ, RESUME, RELEASE, 'transport.flow.update', 'sessions.messages.subscribe',
            'sessions.messages.hydrate', 'sessions.messages.unsubscribe', 'chat.history',
          ] },
          policy: {
            transport_probe_nonce: true,
            cancellable_request_methods: [READ, RESUME, 'sessions.messages.hydrate', 'chat.history'],
            transport_flow: { delivery_epoch: epoch, window_frames: 128, window_bytes: 4 * 1024 * 1024 },
          },
        }))
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
        if (key === SESSION && (mode === 'stalled' || mode === 'progressing')) {
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
      if (frame.method === 'chat.send') {
        expect(activeTurn).toBeNull()
        activeTurn = { socket, key, task: 'synthetic-modern-send' }
        sentHistory.push({ role: 'user', text: observed.params.message, id: 'modern-recovery-user',
          client_message_id: observed.params.clientMessageId, turn_id: activeTurn.task })
        respond({ accepted: true, session: key, task_id: activeTurn.task })
        emitTurn('task.running', { status: 'running' })
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
      respond(Object.hasOwn(payloads, frame.method) ? payloads[frame.method] : {})
    })
  })
  const tick = () => sockets.forEach(socket => socket.send(JSON.stringify({
    type: 'event', event: 'tick', payload: { time_ms: Date.now() }, seq: 1,
  })))
  return { sockets, requests, held, staged, tick, rejectedResumes, heldSubscriptions,
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
    releaseFault: () => { faultActive = false } }
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

test('automatically replaces a stale snapshot after a lost subscribe and hydrates the recovered ACK', async ({ page }) => {
  const gateway = await prepare(page, 'lost-subscribe')
  await page.goto(`/control/chat?session=${encodeURIComponent(SESSION)}`)
  await expect.poll(() => calls(gateway.requests, READ).length).toBe(1)
  await expect.poll(() => gateway.staged.length).toBe(1)
  const input = page.locator('.chat-textarea')
  const send = page.locator('.chat-send-btn[aria-label="Send"]')
  await input.fill(DRAFT)
  const editor = await input.elementHandle()
  const originalUrl = page.url()
  await advance(page, 7_100)
  const notice = page.getByTestId('chat-session-recovery-status')
  await expect(notice).toHaveAttribute('data-recovery-state', 'live-degraded')
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
  expect(calls(gateway.requests, 'sessions.messages.hydrate')).toHaveLength(1)
  await expect(notice).toHaveCount(0)
  await expect(send).toBeEnabled()
  await expect(input).toHaveValue(DRAFT)
  expect(await input.evaluate((node, original) => node === original, editor)).toBe(true)
  expect(await input.evaluate(node => document.activeElement === node)).toBe(true)
  expect(page.url()).toBe(originalUrl)
  expect(gateway.sockets).toHaveLength(1)
  expect(calls(gateway.requests, 'chat.send')).toHaveLength(0)
  await expect(page.getByText('Synthetic cached history remains available.', { exact: true })).toHaveCount(1)
  await advance(page, 15_000)
  expect(calls(gateway.requests, RESUME)).toHaveLength(2)
  expect(calls(gateway.requests, 'sessions.messages.hydrate')).toHaveLength(1)
  expect(calls(gateway.requests, 'chat.send')).toHaveLength(0)
  await expect(input).toHaveValue(DRAFT)
  expect(await input.evaluate(node => document.activeElement === node)).toBe(true)
  expect(page.url()).toBe(originalUrl)

  // A new user action must work on the recovered lease, with real consumer
  // rendering before the terminal frame and no repeated draft submission.
  await input.press('Enter')
  await expect.poll(() => calls(gateway.requests, 'chat.send').length).toBe(1)
  expect(calls(gateway.requests, 'chat.send')[0]!.params).toMatchObject({ sessionKey: SESSION, message: DRAFT })
  await expect(input).toHaveValue('')
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

test('stays degraded beyond the foreground deadline and does not resurrect a dismissed notice', async ({ page }) => {
  const gateway = await prepare(page, 'stalled')
  await page.goto(`/control/chat?session=${encodeURIComponent(SESSION)}`)
  await expect.poll(() => gateway.held.length).toBeGreaterThan(0)
  await page.locator('.chat-textarea').fill(DRAFT)
  await advance(page, 16_000)
  const notice = page.getByTestId('chat-session-recovery-status')
  await expect(notice).toHaveAttribute('data-recovery-state', 'live-degraded')
  await expect(page.getByText('Synthetic cached history remains available.', { exact: true })).toBeVisible()
  const retry = page.getByTestId('chat-session-recovery-retry')
  await expect(retry).toBeVisible()
  await retry.click()
  await expect(notice).toHaveAttribute('data-recovery-state', 'live-degraded')
  await notice.getByRole('button', { name: 'Close', exact: true }).click()
  await expect(notice).toHaveCount(0)

  for (let seconds = 0; seconds < 60; seconds += 5) {
    await page.evaluate(() => {
      window.dispatchEvent(new Event('online'))
      document.dispatchEvent(new Event('visibilitychange'))
    })
    await advance(page, 5_000)
    await expect(notice).toHaveCount(0)
    await expect(page.locator('.chat-textarea')).toHaveValue(DRAFT)
  }
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
  const notice = page.getByTestId('chat-session-recovery-status')
  for (let index = 0; index < 8; index++) {
    await expect.poll(() => gateway.held.length).toBe(index + 1)
    const piece = gateway.held[index]!
    expect(piece.index).toBe(index)
    expect(piece.frame.params.sync_revision).toBe(revision)
    await advance(page, 6_000)
    if (index >= 2) await expect(notice).toHaveAttribute('data-recovery-state', 'live-degraded')
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
  await expect(page.locator('.chat-textarea')).toHaveValue(DRAFT)
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
  gateway.sockets[0]!.close({ code: 1012, reason: 'synthetic restart' })
  await advance(page, 2_000)
  await expect.poll(() => calls(gateway.requests, RESUME, 2).length).toBe(1)
  await expect(send).toBeEnabled()
  await expect(input).toHaveValue(DRAFT)
  expect(page.url()).toBe(originalUrl)
  expect(calls(gateway.requests, READ, 2)[0]!.params.sync_revision).not.toBe(firstRevision)
  expect(calls(gateway.requests, RELEASE, 2).some(item => item.params.sync_revision === firstRevision)).toBe(false)
  expect(calls(gateway.requests, 'chat.send')).toHaveLength(0)
  await input.press('Enter')
  await expect.poll(() => calls(gateway.requests, 'chat.send').length).toBe(1)
  expect(calls(gateway.requests, 'chat.send')[0]!.params).toMatchObject({ sessionKey: SESSION, message: DRAFT })
  await expect(input).toHaveValue('')
  await advance(page, 10_000)
  expect(calls(gateway.requests, 'chat.send')).toHaveLength(1)
  expect(gateway.sockets).toHaveLength(2)
})
