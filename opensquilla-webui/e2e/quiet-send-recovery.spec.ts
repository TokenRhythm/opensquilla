import { expect, test, type Page, type WebSocketRoute } from '@playwright/test'
import { writeFile } from 'node:fs/promises'
import { helloOkResponse } from './support/gateway-fixture'
import { chatHistoryPayload, sessionMessagesHydratePayload, sessionMessagesSnapshotPayload, sessionMessagesSubscribePayload } from './support/session-read-fixtures'
import { expectComposerValue } from './utils'

const SESSION = 'agent:main:webchat:quiet-send'
const GENERATION = 'quiet-send-stream'
const DRAFT = 'Submit this message only once while reception is pending.'
const HISTORY = 'Existing conversation stays readable during background recovery.'
const READ = 'sessions.messages.snapshot.read'
const RESUME = 'sessions.messages.resume'
const RELEASE = 'sessions.messages.snapshot.release'
type Request = { type: string; id: string; method: string; params: Record<string, unknown>; nonce?: string; connection: number }
type Options = { metadataBusy?: boolean; retryAfterMs?: number }
const requestEvidence = new WeakMap<Page, Request[]>()
const browserErrors = new WeakMap<Page, string[]>()

test.beforeEach(async ({ page }) => {
  const errors: string[] = []
  browserErrors.set(page, errors)
  page.on('pageerror', error => errors.push(error.message))
})

test.afterEach(async ({ page }, info) => {
  await writeFile(info.outputPath('quiet-send-network.json'), JSON.stringify({
    evidence: 'Actual mounted WebUI in Chromium; synthetic Gateway I/O and controlled browser clock. No provider calls.',
    requests: requestEvidence.get(page) ?? [],
    pageErrors: browserErrors.get(page) ?? [],
  }, null, 2))
  expect(browserErrors.get(page) ?? []).toEqual([])
})

/** Faults are at the public transport boundary; no Vue state is mutated. */
async function prepare(page: Page, options: Options = {}) {
  await page.clock.install({ time: new Date('2026-01-01T00:00:00Z') })
  await page.addInitScript(() => localStorage.setItem('opensquilla-locale', 'en'))
  await page.route('**/api/**', route => route.fulfill({ json: {} }))
  await page.route('**/api/approvals', route => route.fulfill({ json: {
    pending: [], mode: 'prompt', allowPatterns: [], denyPatterns: [],
  } }))
  const requests: Request[] = []
  requestEvidence.set(page, requests)
  const sockets: WebSocketRoute[] = []
  const history: Record<string, unknown>[] = []
  const sends: Array<{ request: Request; accept: () => void; reject: () => void }> = []
  let activeTask: string | null = null
  let sequence = 0
  let metadataBusy = Boolean(options.metadataBusy)
  let online = true
  let receiptAvailable = false
  let lastAccepted: Request | null = null
  const metadata = () => ({ stream_generation: GENERATION, current_stream_seq: sequence,
    run_status: activeTask ? 'running' : 'idle',
    active_task: activeTask ? { task_id: activeTask, status: 'running' } : null,
    tasks: activeTask ? [{ task_id: activeTask, status: 'running' }] : [],
  })
  const emit = (event: string, payload: Record<string, unknown> = {}) => {
    sockets.at(-1)!.send(JSON.stringify({ type: 'event', event, payload: {
      key: SESSION, session_key: SESSION, task_id: activeTask,
      stream_generation: GENERATION, stream_seq: ++sequence, ...payload,
    } }))
  }
  await page.routeWebSocket(/\/ws$/, socket => {
    const connection = sockets.push(socket)
    if (!online) { socket.close({ code: 1012, reason: 'Synthetic Gateway unavailable' }); return }
    const epoch = `quiet-send-delivery-${connection}`
    let deliveryId = 0
    socket.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    socket.onMessage(raw => {
      const frame = JSON.parse(String(raw)) as Request
      if (frame.type === 'ping') { socket.send(JSON.stringify({ type: 'pong', nonce: frame.nonce })); return }
      if (frame.type !== 'req') return
      const request = { ...frame, params: frame.params ?? {}, connection }
      requests.push(request)
      const key = String(request.params.key ?? request.params.sessionKey ?? SESSION)
      const reply = (payload: unknown) => socket.send(JSON.stringify({ type: 'res', id: request.id, ok: true, payload }))
      if (request.method === 'connect') {
        socket.send(helloOkResponse({ protocol: 4, server: { conn_id: `quiet-send-${connection}` },
          features: { methods: [READ, RESUME, RELEASE, 'transport.flow.update', 'sessions.messages.subscribe',
            'sessions.messages.hydrate', 'sessions.messages.unsubscribe', 'chat.history', 'turns.receipt.get'] },
          policy: { transport_probe_nonce: true,
            cancellable_request_methods: [READ, RESUME, 'sessions.messages.hydrate', 'chat.history'],
            transport_flow: { delivery_epoch: epoch, window_frames: 128, window_bytes: 4 * 1024 * 1024 } },
        }))
        return
      }
      if (request.method === READ) {
        const bytes = Buffer.from(JSON.stringify(sessionMessagesSnapshotPayload(key, {
          stream_generation: GENERATION, current_stream_seq: sequence, task_id: activeTask,
        })))
        reply({ key, sync_revision: request.params.sync_revision, snapshot_id: `quiet-${connection}-${request.id}`,
          segment_index: 0, segment_count: 1, byte_length: bytes.length, encoding: 'base64-json-utf8',
          data: bytes.toString('base64'), stream_generation: GENERATION, current_stream_seq: sequence,
          task_id: activeTask, session_id: null, session_epoch: null,
          delivery: { delivery_epoch: epoch, delivery_id: ++deliveryId } })
        return
      }
      if (request.method === RESUME) { reply({ ...request.params, session_id: null, session_epoch: null, replay_to_seq: request.params.stream_seq }); return }
      if (request.method === RELEASE) { reply({ ...request.params, retired: true }); return }
      if (request.method === 'transport.flow.update') { reply({ delivery_epoch: epoch, ack_delivery_id: request.params.ack_delivery_id ?? 0, dirty_keys: [], global_dirty: false }); return }
      if (request.method === 'sessions.messages.hydrate') {
        if (metadataBusy) socket.send(JSON.stringify({ type: 'res', id: request.id, ok: false,
          error: { code: 'STORAGE_BUSY', message: 'Synthetic metadata contention', retryable: true,
            retry_after_ms: options.retryAfterMs ?? 1_500 } }))
        else reply(sessionMessagesHydratePayload(key, metadata()))
        return
      }
      if (request.method === 'chat.send') {
        sends.push({ request,
          accept: () => {
            lastAccepted = request
            activeTask = `quiet-send-task-${sends.length}`
            history.push({ role: 'user', text: request.params.message, id: `quiet-user-${sends.length}`,
              client_message_id: request.params.clientMessageId, turn_id: activeTask })
            reply({ accepted: true, session: key, task_id: activeTask })
          },
          reject: () => socket.send(JSON.stringify({ type: 'res', id: request.id, ok: false,
            error: { code: 'INVALID_REQUEST', message: 'Synthetic known rejection', retryable: false, accepted: false } })),
        })
        return
      }
      if (request.method === 'turns.receipt.get') {
        const original = request.params.originalRequest as Record<string, unknown>
        if (!receiptAvailable) { reply({ status: 'not_found', accepted: null }); return }
        lastAccepted ??= sends[0]!.request
        activeTask ??= 'quiet-send-receipt-task'
        reply({ status: 'found', accepted: true, requestFingerprint: `sha256:${'a'.repeat(64)}`,
          receipt: { requestSessionKey: original.sessionKey, sessionKey: SESSION, sessionId: 'quiet-session',
            sessionEpoch: 1, clientRequestId: original.clientRequestId, messageId: original.clientMessageId,
            taskId: activeTask, taskStatus: 'running' } })
        return
      }
      if (request.method === 'chat.abort') { reply({ ok: true }); emit('task.cancelled', { status: 'cancelled' }); activeTask = null; return }
      const payloads: Record<string, unknown> = {
        'agents.list': { agents: [] },
        'commands.list_for_surface': { commands: [] },
        'config.get': { squilla_router: { enabled: false, tiers: {} }, permissions: {}, skills: {} },
        'models.routing.get': { mode: 'direct' },
        'sessions.list': { sessions: [{ key: SESSION, title: 'Quiet send recovery', sessionKind: 'chat', surface: 'webchat',
          conversationKind: 'direct', effectiveAgentId: 'main', updatedAt: 100, messageCount: history.length + 1,
          status: 'ok', runStatus: activeTask ? 'running' : 'idle' }], count: 1, ts: 1_800_000_000, has_more: false },
        'sessions.messages.subscribe': sessionMessagesSubscribePayload(key, { ...metadata(), hydration_complete: false }),
        'sessions.messages.unsubscribe': null,
        'chat.history': chatHistoryPayload([{ role: 'user', text: HISTORY, message_id: 'quiet-history',
          timestamp: '2026-01-01T00:00:00Z' }, ...history]),
        'sessions.subscribe': { subscribed: true }, 'usage.status': { sessions: [] },
      }
      reply(Object.hasOwn(payloads, request.method) ? payloads[request.method] : {})
    })
  })
  return {
    requests, sends, sockets,
    calls: (method: string) => requests.filter(request => request.method === method),
    restoreMetadata() { metadataBusy = false },
    startExecution() { emit('task.running', { status: 'running' }) },
    revealReceipt() { receiptAvailable = true },
    disconnect() { online = false; sockets.at(-1)!.close({ code: 1012, reason: 'Synthetic complete disconnect' }) },
    reconnect() { online = true },
    breakConnection() { sockets.at(-1)!.close({ code: 1012, reason: 'Synthetic lost acknowledgement' }) },
    tick() { if (online) sockets.at(-1)?.send(JSON.stringify({ type: 'event', event: 'tick', payload: { time_ms: Date.now() } })) },
  }
}

async function advance(page: Page, gateway: Awaited<ReturnType<typeof prepare>>, milliseconds: number) {
  for (let elapsed = 0; elapsed < milliseconds; elapsed += 1_000) {
    gateway.tick()
    await page.clock.runFor(Math.min(1_000, milliseconds - elapsed))
    await page.evaluate(() => Promise.resolve())
  }
}

async function open(page: Page, options: Options = {}) {
  const gateway = await prepare(page, options)
  await page.goto(`/control/chat?session=${encodeURIComponent(SESSION)}`)
  await expect(page.getByText(HISTORY, { exact: true })).toBeVisible()
  return gateway
}

async function expectNoRecoveryPanels(page: Page) {
  await expect(page.getByTestId('chat-session-recovery-status')).toHaveCount(0)
  await expect(page.getByTestId('chat-delivery-recovery')).toHaveCount(0)
  await expect(page.locator('.toast--danger, .toast--warn')).toHaveCount(0)
}

test('pending reception uses only the send control, resists repeated submit, then switches to Stop', async ({ page }) => {
  const gateway = await open(page)
  const input = page.locator('.chat-textarea')
  const send = page.locator('.chat-send-btn[aria-label="Send"]')
  await input.fill(DRAFT)
  await expect(send).toBeEnabled()
  await send.click()
  await expect.poll(() => gateway.sends.length).toBe(1)
  await expect(send).toHaveAttribute('aria-busy', 'true')
  await expect(send.locator('.loading-spinner')).toHaveCount(1)
  await expect(send).toBeDisabled()
  await input.press('Enter')
  await send.evaluate((button: HTMLButtonElement) => { button.click(); button.click() })
  await advance(page, gateway, 9_000)
  expect(gateway.sends).toHaveLength(1)
  await expect(page.locator('.msg-user .loading-spinner')).toHaveCount(0)
  await expectNoRecoveryPanels(page)
  await page.screenshot({ path: test.info().outputPath('send-pending.png') })
  gateway.sends[0]!.accept()
  // Reception itself ends the submit animation; it must not wait for an
  // assistant delta or a separate task.running event to arrive.
  await expect(page.locator('.chat-send-btn .loading-spinner')).toHaveCount(0)
  gateway.startExecution()
  await expect(page.getByRole('button', { name: 'Stop current response', exact: true })).toBeEnabled()
  await expect(page.locator('.chat-send-btn .loading-spinner')).toHaveCount(0)
  await expectComposerValue(input, '')
  await expect(page.getByText(DRAFT, { exact: true })).toHaveCount(1)
  expect(gateway.sends).toHaveLength(1)
  await page.screenshot({ path: test.info().outputPath('send-accepted-stop.png') })
})

test('known rejection ends the busy control and only a later deliberate send makes another request', async ({ page }) => {
  const gateway = await open(page)
  const input = page.locator('.chat-textarea')
  const send = page.locator('.chat-send-btn[aria-label="Send"]')
  await input.fill(DRAFT)
  await send.click()
  await expect.poll(() => gateway.sends.length).toBe(1)
  gateway.sends[0]!.reject()
  await expect(page.locator('.chat-send-btn .loading-spinner')).toHaveCount(0)
  await advance(page, gateway, 31_000)
  expect(gateway.sends).toHaveLength(1)
  await input.fill(DRAFT)
  await expect(send).toBeEnabled()
  await send.click()
  await expect.poll(() => gateway.sends.length).toBe(2)
  gateway.sends[1]!.accept()
  gateway.startExecution()
  await expect(page.getByRole('button', { name: 'Stop current response', exact: true })).toBeEnabled()
  await expect(page.locator('.chat-send-btn .loading-spinner')).toHaveCount(0)
})

test('background metadata contention stays quiet and does not start a submit animation or auto-send the draft', async ({ page }) => {
  const gateway = await open(page, { metadataBusy: true })
  const input = page.locator('.chat-textarea')
  await input.fill(DRAFT)
  await advance(page, gateway, 15_000)
  await expectNoRecoveryPanels(page)
  await expect(page.getByTestId('connection-status')).toHaveCount(0)
  await expect(page.locator('.chat-send-btn .loading-spinner')).toHaveCount(0)
  expect(gateway.sends).toHaveLength(0)
  gateway.restoreMetadata()
  await advance(page, gateway, 10_000)
  await expect(page.locator('.chat-send-btn[aria-label="Send"]')).toBeEnabled()
  await expectComposerValue(input, DRAFT)
  expect(gateway.sends).toHaveLength(0)
})

test('an unknown admission parks after bounded receipt reads, ends animation and never resends the command', async ({ page }) => {
  test.setTimeout(90_000)
  const gateway = await open(page)
  const input = page.locator('.chat-textarea')
  await input.fill(DRAFT)
  await page.locator('.chat-send-btn[aria-label="Send"]').click()
  await expect.poll(() => gateway.sends.length).toBe(1)
  // Close only the transport: the client cannot infer whether the original
  // command committed. Receipt reads remain unavailable for this test.
  gateway.breakConnection()
  await advance(page, gateway, 2_000)
  await expect.poll(() => gateway.calls('connect').length).toBe(2)
  await advance(page, gateway, 31_000)
  await expect.poll(() => gateway.calls('turns.receipt.get').length).toBe(4)
  const readsAtBudget = gateway.calls('turns.receipt.get').length
  await expect(page.locator('.chat-send-btn .loading-spinner')).toHaveCount(0)
  await expectNoRecoveryPanels(page)
  await expect(page.getByTestId('connection-status')).toHaveCount(0)
  const nextDraft = 'Keep this next draft while the earlier receipt remains unknown.'
  await input.fill(nextDraft)
  await advance(page, gateway, 35_000)
  expect(gateway.calls('turns.receipt.get')).toHaveLength(readsAtBudget)
  expect(gateway.sends).toHaveLength(1)
  await expectComposerValue(input, nextDraft)
  const check = page.getByTestId('chat-composer-recovery')
  await expect(check).toHaveAttribute('data-recovery-action', 'check')
  await expect(check).toBeEnabled()
  // Enter activates the same explicit check action; concurrent button clicks
  // join it, and none is interpreted as sending the next draft.
  await input.press('Enter')
  await check.evaluate((button: HTMLButtonElement) => { button.click(); button.click(); button.click() })
  await advance(page, gateway, 31_000)
  expect(gateway.calls('turns.receipt.get')).toHaveLength(readsAtBudget + 4)
  await expect(check).toBeEnabled()
  await expect(check.locator('.loading-spinner')).toHaveCount(0)
  await expectComposerValue(input, nextDraft)
  expect(gateway.sends).toHaveLength(1)
  for (const receipt of gateway.calls('turns.receipt.get')) {
    expect(receipt.params).toEqual({ operation: 'chat.send', originalRequest: gateway.sends[0]!.request.params })
  }
  await page.screenshot({ path: test.info().outputPath('receipt-exhausted-quiet.png') })
  gateway.revealReceipt()
  await check.click()
  await expect(page.getByRole('button', { name: 'Stop current response', exact: true })).toBeEnabled()
  await expect(check).toHaveCount(0)
  await expect(page.locator('.chat-send-btn .loading-spinner')).toHaveCount(0)
  await expectComposerValue(input, nextDraft)
  expect(gateway.sends).toHaveLength(1)
})

test('a healthy socket with a permanently missing admission ACK cannot spin or resubmit indefinitely', async ({ page }) => {
  test.setTimeout(90_000)
  const gateway = await open(page)
  await page.locator('.chat-textarea').fill(DRAFT)
  await page.locator('.chat-send-btn[aria-label="Send"]').click()
  await expect.poll(() => gateway.sends.length).toBe(1)
  await expect(page.locator('.chat-send-btn .loading-spinner')).toHaveCount(1)
  await advance(page, gateway, 61_000)
  await expect(page.locator('.topbar[data-connection-state="connected"]')).toBeVisible()
  await expect(page.locator('.chat-send-btn .loading-spinner')).toHaveCount(0)
  await expect(page.getByTestId('chat-composer-recovery')).toHaveAttribute('data-recovery-action', 'check')
  await expectNoRecoveryPanels(page)
  expect(gateway.sends).toHaveLength(1)
  expect(gateway.calls('turns.receipt.get')).toHaveLength(4)
  await advance(page, gateway, 31_000)
  expect(gateway.sends).toHaveLength(1)
  expect(gateway.calls('turns.receipt.get')).toHaveLength(4)
})

test('quiet composer retry retains a long metadata Retry-After, joins duplicate actions and preserves the draft', async ({ page }) => {
  test.setTimeout(90_000)
  const gateway = await open(page, { metadataBusy: true, retryAfterMs: 180_000 })
  const input = page.locator('.chat-textarea')
  await input.fill(DRAFT)
  await advance(page, gateway, 121_000)
  expect(gateway.calls('sessions.messages.hydrate')).toHaveLength(1)
  await expectNoRecoveryPanels(page)
  const retry = page.getByTestId('chat-composer-recovery')
  await expect(retry).toHaveAttribute('data-recovery-action', 'retry')
  await expect(retry).toBeEnabled()
  await expect(retry.locator('.loading-spinner')).toHaveCount(0)
  gateway.restoreMetadata()
  await retry.evaluate((button: HTMLButtonElement) => { button.click(); button.click(); button.click() })
  await expect(retry).toBeDisabled()
  await expect(retry.locator('.loading-spinner')).toHaveCount(1)
  await advance(page, gateway, 50_000)
  expect(gateway.calls('sessions.messages.hydrate')).toHaveLength(1)
  expect(gateway.sends).toHaveLength(0)
  await expectComposerValue(input, DRAFT)
  await advance(page, gateway, 11_000)
  await expect(page.locator('.chat-send-btn[aria-label="Send"]')).toBeEnabled()
  expect(gateway.calls('sessions.messages.hydrate')).toHaveLength(2)
  await expect(retry).toHaveCount(0)
  await expect(page.locator('.chat-send-btn .loading-spinner')).toHaveCount(0)
  await expectComposerValue(input, DRAFT)
  expect(gateway.sends).toHaveLength(0)
  await input.press('Enter')
  await expect.poll(() => gateway.sends.length).toBe(1)
  gateway.sends[0]!.accept()
  gateway.startExecution()
  await expect(page.getByRole('button', { name: 'Stop current response', exact: true })).toBeEnabled()
  await expectNoRecoveryPanels(page)
})

test('short disconnect stays silent and only persistent total Gateway loss shows the existing global control', async ({ page }) => {
  const gateway = await open(page)
  const input = page.locator('.chat-textarea')
  await input.fill(DRAFT)
  gateway.disconnect()
  await advance(page, gateway, 2_000)
  await expectNoRecoveryPanels(page)
  await expect(page.getByTestId('connection-status')).toHaveCount(0)
  gateway.reconnect()
  await advance(page, gateway, 3_000)
  await expect(page.locator('.topbar[data-connection-state="connected"]')).toBeVisible()
  await expect(page.getByTestId('connection-status')).toHaveCount(0)
  gateway.disconnect()
  await advance(page, gateway, 6_000)
  await expect(page.getByTestId('connection-status')).toBeVisible()
  await expectNoRecoveryPanels(page)
  await expect(page.locator('.chat-send-btn .loading-spinner')).toHaveCount(0)
  await expectComposerValue(input, DRAFT)
  expect(gateway.sends).toHaveLength(0)
  await page.screenshot({ path: test.info().outputPath('sustained-disconnect.png') })
  gateway.reconnect()
  await advance(page, gateway, 15_000)
  await expect(page.locator('.topbar[data-connection-state="connected"]')).toBeVisible()
  await expect(page.getByTestId('connection-status')).toHaveCount(0)
  await expectComposerValue(input, DRAFT)
})
