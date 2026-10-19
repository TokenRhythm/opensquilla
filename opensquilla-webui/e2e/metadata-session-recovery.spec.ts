import { expect, test, type Page, type WebSocketRoute } from '@playwright/test'
import { writeFile } from 'node:fs/promises'
import { helloOkResponse } from './support/gateway-fixture'
import { chatHistoryPayload, sessionMessagesHydratePayload, sessionMessagesSnapshotPayload, sessionMessagesSubscribePayload } from './support/session-read-fixtures'
import { expectComposerValue } from './utils'

const SESSION = 'agent:main:webchat:metadata-recovery'
const HISTORY = 'Earlier conversation remains readable while task state recovers.'
const DRAFT = 'Draft entered while the conversation metadata is unavailable.'
const EDITED = 'The user edited this draft while metadata recovery was pending.'
const LIVE_REPLY = 'Current live reply still arrives after metadata recovery has stopped.'
const GENERATION = 'synthetic-metadata-stream'
const SNAPSHOT_READ = 'sessions.messages.snapshot.read'
const SNAPSHOT_RESUME = 'sessions.messages.resume'
const SNAPSHOT_RELEASE = 'sessions.messages.snapshot.release'

type Request = { type: string; id: string; method: string; params: Record<string, unknown>; nonce?: string }
type Observed = Request & { connection: number }
const observedByPage = new WeakMap<Page, Observed[]>()

test.afterEach(async ({ page }) => {
  const requests = observedByPage.get(page)
  if (requests) await writeFile(test.info().outputPath('observed-requests.json'), JSON.stringify(requests, null, 2))
})

/** Real mounted WebUI and transport; only Gateway I/O and time are synthetic. */
async function prepare(page: Page, fault: 'transient-busy' | 'persistent-busy' | 'warm-then-busy' | 'incomplete', busyRetryAfterMs = 1_500) {
  await page.clock.install({ time: new Date('2026-01-01T00:00:00Z') })
  await page.addInitScript(() => localStorage.setItem('opensquilla-locale', 'en'))
  await page.route('**/api/**', route => route.fulfill({ json: {} }))
  await page.route('**/api/approvals', route => route.fulfill({
    json: { pending: [], mode: 'prompt', allowPatterns: [], denyPatterns: [] },
  }))
  const requests: Observed[] = []
  observedByPage.set(page, requests)
  const sockets: WebSocketRoute[] = []
  const history: Record<string, unknown>[] = []
  let releaseFirstBusy: (() => void) | undefined
  let hydrated = 0
  let persistentBusy = fault === 'persistent-busy'
  let activeTask: string | null = null
  await page.routeWebSocket(/\/ws$/, socket => {
    const connection = sockets.push(socket)
    const deliveryEpoch = `metadata-delivery-${connection}`
    let nextDelivery = 1
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
      const respond = (payload: unknown) => socket.send(JSON.stringify({ type: 'res', id: frame.id, ok: true, payload }))
      if (frame.method === 'connect') {
        socket.send(helloOkResponse({
          protocol: 4,
          server: { conn_id: `synthetic-metadata-${connection}` },
          features: { methods: [
            SNAPSHOT_READ, SNAPSHOT_RESUME, SNAPSHOT_RELEASE, 'transport.flow.update',
            'sessions.messages.subscribe', 'sessions.messages.hydrate',
            'sessions.messages.unsubscribe', 'chat.history',
          ] },
          policy: { transport_probe_nonce: true,
            cancellable_request_methods: [SNAPSHOT_READ, SNAPSHOT_RESUME, 'sessions.messages.hydrate', 'chat.history'],
            transport_flow: { delivery_epoch: deliveryEpoch, window_frames: 128, window_bytes: 4 * 1024 * 1024 },
          },
        }))
        return
      }
      if (frame.method === SNAPSHOT_READ) {
        const bytes = Buffer.from(JSON.stringify(sessionMessagesSnapshotPayload(key, {
          stream_generation: GENERATION, task_id: activeTask,
        })))
        respond({
          key, sync_revision: observed.params.sync_revision,
          snapshot_id: `metadata-snapshot-${connection}-${observed.params.sync_revision}`,
          segment_index: 0, segment_count: 1, byte_length: bytes.length,
          encoding: 'base64-json-utf8', data: bytes.toString('base64'),
          stream_generation: GENERATION, current_stream_seq: 0, task_id: activeTask,
          session_id: null, session_epoch: null,
          delivery: { delivery_epoch: deliveryEpoch, delivery_id: nextDelivery++ },
        })
        return
      }
      if (frame.method === SNAPSHOT_RESUME) {
        respond({ ...observed.params, session_id: null, session_epoch: null,
          replay_to_seq: observed.params.stream_seq })
        return
      }
      if (frame.method === SNAPSHOT_RELEASE) {
        respond({ ...observed.params, retired: true })
        return
      }
      if (frame.method === 'transport.flow.update') {
        respond({ delivery_epoch: deliveryEpoch, ack_delivery_id: observed.params.ack_delivery_id ?? 0,
          dirty_keys: [], global_dirty: false })
        return
      }
      if (frame.method === 'sessions.messages.hydrate') {
        hydrated++
        const busy = () => socket.send(JSON.stringify({ type: 'res', id: frame.id, ok: false,
          error: { code: 'STORAGE_BUSY', message: 'Synthetic bounded storage contention', retryable: true, retry_after_ms: busyRetryAfterMs },
        }))
        if (fault === 'transient-busy' && hydrated === 1) {
          releaseFirstBusy = busy
        } else if (persistentBusy) busy()
        else respond(sessionMessagesHydratePayload(key, {
          hydration_complete: fault !== 'incomplete',
          run_status: activeTask ? 'running' : 'idle',
          active_task: activeTask ? { task_id: activeTask, status: 'running' } : null,
        }))
        return
      }
      if (frame.method === 'chat.send') {
        activeTask = 'metadata-user-turn'
        history.push({ role: 'user', text: observed.params.message, id: 'metadata-user-message',
          client_message_id: observed.params.clientMessageId, turn_id: activeTask })
        respond({ accepted: true, session: key, task_id: activeTask })
        socket.send(JSON.stringify({ type: 'event', event: 'task.running', payload: {
          key, session_key: key, task_id: activeTask, status: 'running', stream_generation: GENERATION, stream_seq: 1,
        } }))
        return
      }
      const payloads: Record<string, unknown> = {
        'agents.list': { agents: [] },
        'commands.list_for_surface': { commands: [] },
        'config.get': { squilla_router: { enabled: false, tiers: {} }, permissions: {}, skills: {} },
        'models.routing.get': { mode: 'direct' },
        'sessions.list': { sessions: [{ key: SESSION, title: 'Metadata recovery session',
          sessionKind: 'chat', surface: 'webchat', conversationKind: 'direct', effectiveAgentId: 'main',
          updatedAt: 100, messageCount: 1, status: 'ok', runStatus: 'idle',
        }], count: 1, ts: 1_800_000_000, has_more: false },
        'sessions.messages.subscribe': sessionMessagesSubscribePayload(key, {
          stream_generation: GENERATION, hydration_complete: false,
        }),
        'sessions.messages.unsubscribe': null,
        'chat.history': chatHistoryPayload([{ role: 'user', text: HISTORY, message_id: 'metadata-history',
          timestamp: '2026-01-01T00:00:00Z' }, ...history]),
        'sessions.subscribe': { subscribed: true },
        'usage.status': { sessions: [] },
      }
      respond(Object.hasOwn(payloads, frame.method) ? payloads[frame.method] : {})
    })
  })
  return {
    requests, sockets,
    calls: (method: string) => requests.filter(request => request.method === method),
    failMetadata() { persistentBusy = true },
    restoreMetadata() { persistentBusy = false },
    releaseBusy() {
      if (!releaseFirstBusy) throw new Error('Initial metadata request was not observed')
      releaseFirstBusy()
      releaseFirstBusy = undefined
    },
    emitCurrentReply() {
      activeTask = 'metadata-live-turn'
      const socket = sockets.at(-1)!
      for (const [event, streamSeq, payload] of [
        ['task.running', 1, { status: 'running' }],
        ['session.event.text_delta', 2, { text: LIVE_REPLY }],
      ] as const) {
        socket.send(JSON.stringify({ type: 'event', event, payload: {
          key: SESSION, session_key: SESSION, task_id: activeTask,
          stream_generation: GENERATION, stream_seq: streamSeq, ...payload,
        } }))
      }
    },
  }
}

async function advance(page: Page, durationMs: number) {
  for (let elapsed = 0; elapsed < durationMs; elapsed += 1_000) {
    await page.clock.runFor(Math.min(1_000, durationMs - elapsed))
    await page.evaluate(() => Promise.resolve())
  }
}

async function evidence(gateway: Awaited<ReturnType<typeof prepare>>, details: Record<string, unknown>) {
  const body = JSON.stringify({
    evidence: 'Chromium with a synthetic Gateway and controlled clock; actual mounted ChatView, no private Vue mutation',
    requests: gateway.requests, ...details,
  }, null, 2)
  await writeFile(test.info().outputPath('metadata-recovery-outcome.json'), body)
  await test.info().attach('metadata-recovery-outcome.json', { body, contentType: 'application/json' })
}

test('recovers busy metadata quietly, preserves an edited draft, and accepts one later manual send', async ({ page }) => {
  test.setTimeout(90_000)
  const gateway = await prepare(page, 'transient-busy')
  await page.goto(`/control/chat?session=${encodeURIComponent(SESSION)}`)
  await expect.poll(() => gateway.calls('sessions.messages.hydrate').length).toBe(1)
  // Initial presentation may await metadata, but history and live installation
  // must complete without waiting for that independent metadata response.
  await expect.poll(() => gateway.calls(SNAPSHOT_RESUME).length).toBe(1)
  expect(gateway.calls('chat.history').length).toBeGreaterThan(0)
  const input = page.locator('.chat-textarea')
  const send = page.locator('.chat-send-btn[aria-label="Send"]')
  await expect(page.locator('.chat-send-btn[aria-label="Send"]:enabled')).toHaveCount(0)
  await input.fill(DRAFT)
  await input.fill(EDITED)
  const editor = await input.elementHandle()
  gateway.releaseBusy()
  await advance(page, 1_000)
  await expect(page.getByText(HISTORY, { exact: true })).toBeVisible()
  expect(gateway.calls('sessions.messages.hydrate')).toHaveLength(1)
  await expect(page.getByTestId('chat-session-recovery-status')).toHaveCount(0)
  await advance(page, 3_000)
  await expect(send).toBeEnabled()
  expect(gateway.calls('sessions.messages.hydrate')).toHaveLength(2)
  expect(gateway.calls('sessions.messages.subscribe')).toHaveLength(1)
  expect(gateway.calls('chat.send')).toHaveLength(0)
  await expectComposerValue(input, EDITED)
  expect(await input.evaluate((node, original) => node === original, editor)).toBe(true)
  await input.press('Enter')
  await expect.poll(() => gateway.calls('chat.send').length).toBe(1)
  expect(gateway.calls('chat.send')[0]!.params).toMatchObject({ sessionKey: SESSION, message: EDITED })
  await expectComposerValue(input, '')
  await advance(page, 10_000)
  expect(gateway.calls('chat.send')).toHaveLength(1)
  await expect(page.getByText(EDITED, { exact: true })).toHaveCount(1)
  await expect(page.getByTestId('chat-session-recovery-status')).toHaveCount(0)
  await evidence(gateway, { hydrated: true, acceptedSends: 1, originalDraftSent: false })
})

test('stops persistent metadata retries at the incident deadline and preserves that stop across reconnection', async ({ page }) => {
  test.setTimeout(90_000)
  const gateway = await prepare(page, 'persistent-busy')
  await page.goto(`/control/chat?session=${encodeURIComponent(SESSION)}`)
  await expect.poll(() => gateway.calls('sessions.messages.hydrate').length).toBeGreaterThan(0)
  await expect(page.getByText(HISTORY, { exact: true })).toBeVisible()
  const input = page.locator('.chat-textarea')
  const send = page.locator('.chat-send-btn[aria-label="Send"]')
  await input.fill(DRAFT)
  await advance(page, 60_000)
  await expect(page.getByTestId('chat-session-recovery-status')).toHaveCount(0)
  await advance(page, 61_000)
  const hydratesAtDeadline = gateway.calls('sessions.messages.hydrate').length
  expect(hydratesAtDeadline).toBeGreaterThan(1)
  await expect(page.locator('.chat-send-btn[aria-label="Send"]:enabled')).toHaveCount(0)
  await expect(page.getByTestId('chat-session-recovery-status')).toHaveCount(0)
  await expect(page.getByTestId('chat-composer-recovery')).toBeEnabled()
  await expect(page.getByTestId('chat-session-recovery-dismiss')).toHaveCount(0)
  await expectComposerValue(input, DRAFT)
  await expect(page.getByText(HISTORY, { exact: true })).toBeVisible()
  await advance(page, 30_000)
  expect(gateway.calls('sessions.messages.hydrate')).toHaveLength(hydratesAtDeadline)
  gateway.sockets.at(-1)!.close({ code: 1012, reason: 'synthetic metadata reconnect' })
  await advance(page, 2_000)
  await expect.poll(() => gateway.calls('connect').length).toBe(2)
  await expect.poll(() => gateway.calls('sessions.messages.subscribe').length).toBe(2)
  await advance(page, 30_000)
  expect(gateway.calls('sessions.messages.hydrate')).toHaveLength(hydratesAtDeadline)
  expect(gateway.calls('chat.send')).toHaveLength(0)
  await expect(page.locator('.chat-send-btn[aria-label="Send"]:enabled')).toHaveCount(0)
  await expectComposerValue(input, DRAFT)
  await expect(page.getByText(HISTORY, { exact: true })).toBeVisible()
  gateway.emitCurrentReply()
  await expect(page.getByRole('button', { name: 'Stop current response', exact: true })).toBeEnabled()
  await advance(page, 1_000)
  await expect(page.getByText(LIVE_REPLY, { exact: true })).toBeVisible()
  await advance(page, 2_000)
  expect(gateway.calls('sessions.messages.hydrate')).toHaveLength(hydratesAtDeadline)
  expect(gateway.calls('chat.send')).toHaveLength(0)
  await expectComposerValue(input, DRAFT)
  await expect(page.getByTestId('chat-session-recovery-status')).toHaveCount(0)
  await evidence(gateway, { hydrated: false, hydratesAtDeadline, acceptedSends: 0,
    survivedConnectionReplacement: true, postDeadlineLiveReplyVisible: true, currentTaskStopAvailable: true })
})

test('treats an incomplete hydrate response as terminal without a retry loop or false Send readiness', async ({ page }) => {
  test.setTimeout(90_000)
  const gateway = await prepare(page, 'incomplete')
  await page.goto(`/control/chat?session=${encodeURIComponent(SESSION)}`)
  await expect.poll(() => gateway.calls('sessions.messages.hydrate').length).toBe(1)
  await expect(page.getByText(HISTORY, { exact: true })).toBeVisible()
  const input = page.locator('.chat-textarea')
  await input.fill(DRAFT)
  await advance(page, 31_000)
  expect(gateway.calls('sessions.messages.hydrate')).toHaveLength(1)
  expect(gateway.calls('sessions.messages.subscribe')).toHaveLength(1)
  expect(gateway.calls('chat.send')).toHaveLength(0)
  await expect(page.locator('.chat-send-btn[aria-label="Send"]')).toBeDisabled()
  await expect(page.getByTestId('chat-composer-recovery')).toHaveCount(0)
  await expectComposerValue(input, DRAFT)
  await expect(page.getByText(HISTORY, { exact: true })).toBeVisible()
  await evidence(gateway, { hydrated: false, terminalContractFailure: true, acceptedSends: 0 })
})

test('explicit metadata retry after exhaustion restores Send and preserves an edited draft without sending it', async ({ page }) => {
  test.setTimeout(90_000)
  const gateway = await prepare(page, 'persistent-busy')
  await page.goto(`/control/chat?session=${encodeURIComponent(SESSION)}`)
  await expect.poll(() => gateway.calls('sessions.messages.hydrate').length).toBeGreaterThan(0)
  await expect(page.getByText(HISTORY, { exact: true })).toBeVisible()
  const input = page.locator('.chat-textarea')
  const send = page.locator('.chat-send-btn[aria-label="Send"]')
  const retry = page.getByTestId('chat-composer-recovery')
  await input.fill(DRAFT)
  await advance(page, 121_000)
  const hydratesAtDeadline = gateway.calls('sessions.messages.hydrate').length
  expect(hydratesAtDeadline).toBeGreaterThan(1)
  await expect(page.locator('.chat-send-btn[aria-label="Send"]:enabled')).toHaveCount(0)
  await expect(page.getByTestId('chat-session-recovery-status')).toHaveCount(0)
  await expect(retry).toBeEnabled()

  // A restored server and a new physical connection are not user consent to
  // renew an exhausted metadata episode.
  gateway.restoreMetadata()
  gateway.sockets.at(-1)!.close({ code: 1012, reason: 'metadata restored after exhaustion' })
  await advance(page, 2_000)
  await expect.poll(() => gateway.calls('connect').length).toBe(2)
  await expect.poll(() => gateway.calls(SNAPSHOT_RESUME).length).toBe(2)
  await advance(page, 10_000)
  expect(gateway.calls('sessions.messages.hydrate')).toHaveLength(hydratesAtDeadline)
  await expect(page.locator('.chat-send-btn[aria-label="Send"]:enabled')).toHaveCount(0)
  await input.fill(EDITED)
  const editor = await input.elementHandle()
  await page.screenshot({ path: test.info().outputPath('metadata-exhausted-retry.png'), fullPage: true })
  await retry.click()
  await expect.poll(() => gateway.calls('sessions.messages.hydrate').length).toBe(hydratesAtDeadline + 1)
  await expect(send).toBeEnabled()
  await expect(page.getByTestId('chat-session-recovery-status')).toHaveCount(0)
  await expectComposerValue(input, EDITED)
  expect(await input.evaluate((node, original) => node === original, editor)).toBe(true)
  expect(gateway.calls('chat.send')).toHaveLength(0)
  await page.screenshot({ path: test.info().outputPath('metadata-restored-send.png'), fullPage: true })
  await advance(page, 5_000)
  expect(gateway.calls('sessions.messages.hydrate')).toHaveLength(hydratesAtDeadline + 1)
  expect(gateway.calls('chat.send')).toHaveLength(0)

  await input.press('Enter')
  await expect.poll(() => gateway.calls('chat.send').length).toBe(1)
  expect(gateway.calls('chat.send')[0]!.params).toMatchObject({ sessionKey: SESSION, message: EDITED })
  await expectComposerValue(input, '')
  await advance(page, 10_000)
  expect(gateway.calls('chat.send')).toHaveLength(1)
  await expect(page.getByText(EDITED, { exact: true })).toHaveCount(1)
  await evidence(gateway, { hydrated: true, explicitMetadataRetry: true, hydratesAtDeadline,
    acceptedSends: 1, retryAutoSent: false, originalDraftSent: false,
    reconnectRenewedMetadata: false, originalEditorPreserved: true })
})

test('explicit metadata retry retains a 180-second server Retry-After and joins repeated retry clicks', async ({ page }) => {
  test.setTimeout(90_000)
  const gateway = await prepare(page, 'persistent-busy', 180_000)
  await page.goto(`/control/chat?session=${encodeURIComponent(SESSION)}`)
  await expect.poll(() => gateway.calls('sessions.messages.hydrate').length).toBe(1)
  const input = page.locator('.chat-textarea')
  const send = page.locator('.chat-send-btn[aria-label="Send"]')
  await input.fill(EDITED)
  await advance(page, 121_000)
  const retry = page.getByTestId('chat-composer-recovery')
  await expect(retry).toBeEnabled()
  expect(gateway.calls('sessions.messages.hydrate')).toHaveLength(1)
  gateway.restoreMetadata()
  // Dispatch real DOM button clicks in one tick, exercising the same public
  // handler before its disabled/hidden state can update. No Vue state is changed.
  await retry.evaluate((button: HTMLButtonElement) => {
    button.click()
    button.click()
    button.click()
  })
  await advance(page, 50_000)
  expect(gateway.calls('sessions.messages.hydrate')).toHaveLength(1)
  await expect(retry).toBeDisabled()
  await expect(page.locator('.chat-send-btn[aria-label="Send"]:enabled')).toHaveCount(0)
  await expectComposerValue(input, EDITED)
  expect(gateway.calls('chat.send')).toHaveLength(0)
  await advance(page, 11_000)
  await expect.poll(() => gateway.calls('sessions.messages.hydrate').length).toBe(2)
  await expect(send).toBeEnabled()
  await expectComposerValue(input, EDITED)
  await expect(page.getByTestId('chat-session-recovery-status')).toHaveCount(0)
  await advance(page, 30_000)
  expect(gateway.calls('sessions.messages.hydrate')).toHaveLength(2)
  expect(gateway.calls('chat.send')).toHaveLength(0)
  await evidence(gateway, { hydrated: true, serverRetryAfterMs: 180_000,
    initialBudgetExhausted: true, noEarlyReadAfterManualRetry: true,
    repeatedPublicClicks: 3, physicalReadsAfterManualRetry: 1, acceptedSends: 0 })
})

test('a manually renewed metadata episode keeps its deadline across repeated actions and reconnection', async ({ page }) => {
  test.setTimeout(90_000)
  const gateway = await prepare(page, 'persistent-busy')
  await page.goto(`/control/chat?session=${encodeURIComponent(SESSION)}`)
  await expect.poll(() => gateway.calls('sessions.messages.hydrate').length).toBeGreaterThan(0)
  const input = page.locator('.chat-textarea')
  const send = page.locator('.chat-send-btn[aria-label="Send"]')
  await input.fill(DRAFT)
  await advance(page, 121_000)
  const initialHydrates = gateway.calls('sessions.messages.hydrate').length
  const retry = page.getByTestId('chat-composer-recovery')
  await expect(retry).toBeEnabled()
  await retry.evaluate((button: HTMLButtonElement) => {
    button.click()
    button.click()
    button.click()
  })
  await advance(page, 60_000)
  expect(gateway.calls('sessions.messages.hydrate').length).toBeGreaterThan(initialHydrates)
  await expect(retry).toBeDisabled()
  gateway.sockets.at(-1)!.close({ code: 1012, reason: 'reconnect during renewed metadata episode' })
  await advance(page, 2_000)
  await expect.poll(() => gateway.calls('connect').length).toBe(2)
  await advance(page, 59_000)
  const hydratesAtRenewedDeadline = gateway.calls('sessions.messages.hydrate').length
  await expect(retry).toBeEnabled()
  await expect(page.locator('.chat-send-btn[aria-label="Send"]:enabled')).toHaveCount(0)
  await expectComposerValue(input, DRAFT)
  gateway.restoreMetadata()
  gateway.sockets.at(-1)!.close({ code: 1012, reason: 'reconnect after renewed metadata deadline' })
  await advance(page, 2_000)
  await expect.poll(() => gateway.calls('connect').length).toBe(3)
  await advance(page, 30_000)
  expect(gateway.calls('sessions.messages.hydrate')).toHaveLength(hydratesAtRenewedDeadline)
  expect(gateway.calls('chat.send')).toHaveLength(0)
  await expect(page.locator('.chat-send-btn[aria-label="Send"]:enabled')).toHaveCount(0)
  await expect(retry).toBeEnabled()
  await evidence(gateway, { hydrated: false, initialHydrates, hydratesAtRenewedDeadline,
    renewedBudgetMs: 120_000, repeatedPublicClicks: 3,
    reconnectRenewedMetadata: false, acceptedSends: 0 })
})

test('releases a warm cached view for current live replies when metadata stays terminal across reconnection', async ({ page }) => {
  test.setTimeout(90_000)
  const gateway = await prepare(page, 'warm-then-busy')
  await page.goto(`/control/chat?session=${encodeURIComponent(SESSION)}`)
  const input = page.locator('.chat-textarea')
  const send = page.locator('.chat-send-btn[aria-label="Send"]')
  await expect(send).toBeEnabled()
  await expect(page.getByText(HISTORY, { exact: true })).toBeVisible()
  expect(gateway.calls('sessions.messages.hydrate')).toHaveLength(1)
  await input.fill(DRAFT)
  const editor = await input.elementHandle()

  gateway.failMetadata()
  gateway.sockets.at(-1)!.close({ code: 1012, reason: 'warm view metadata failure' })
  await advance(page, 2_000)
  await expect.poll(() => gateway.calls('connect').length).toBe(2)
  await expect.poll(() => gateway.calls(SNAPSHOT_RESUME).length).toBe(2)
  await advance(page, 121_000)
  const hydratesAtDeadline = gateway.calls('sessions.messages.hydrate').length
  expect(hydratesAtDeadline).toBeGreaterThan(1)
  await expect(page.locator('.chat-send-btn[aria-label="Send"]:enabled')).toHaveCount(0)
  await expect(page.getByTestId('chat-session-recovery-status')).toHaveCount(0)
  await expect(page.getByTestId('chat-session-recovery-dismiss')).toHaveCount(0)
  await expectComposerValue(input, DRAFT)
  await expect(page.getByText(HISTORY, { exact: true })).toBeVisible()
  await advance(page, 30_000)
  expect(gateway.calls('sessions.messages.hydrate')).toHaveLength(hydratesAtDeadline)

  gateway.sockets.at(-1)!.close({ code: 1012, reason: 'retain warm metadata terminal' })
  await advance(page, 2_000)
  await expect.poll(() => gateway.calls('connect').length).toBe(3)
  await expect.poll(() => gateway.calls(SNAPSHOT_RESUME).length).toBe(3)
  await advance(page, 30_000)
  expect(gateway.calls('sessions.messages.hydrate')).toHaveLength(hydratesAtDeadline)
  gateway.emitCurrentReply()
  await expect(page.getByRole('button', { name: 'Stop current response', exact: true })).toBeEnabled()
  await advance(page, 1_000)
  await expect(page.getByText(LIVE_REPLY, { exact: true })).toBeVisible()
  await expect(page.getByText(HISTORY, { exact: true })).toBeVisible()
  await expectComposerValue(input, DRAFT)
  expect(await input.evaluate((node, original) => node === original, editor)).toBe(true)
  expect(gateway.calls('sessions.messages.hydrate')).toHaveLength(hydratesAtDeadline)
  expect(gateway.calls('chat.send')).toHaveLength(0)
  await expect(page.getByTestId('chat-session-recovery-status')).toHaveCount(0)
  await evidence(gateway, { initialMetadataReady: true, hydrated: false, hydratesAtDeadline,
    acceptedSends: 0, survivedConnectionReplacement: true,
    postDeadlineLiveReplyVisible: true, currentTaskStopAvailable: true, warmPreviewReleased: true })
})
