import { expect, type Page, type WebSocketRoute } from '@playwright/test'
import { writeFile } from 'node:fs/promises'
import { test } from './support/native-desktop-fixture'
import { helloOkResponse } from './support/gateway-fixture'
import {
  chatHistoryPayload, sessionMessagesHydratePayload,
  sessionMessagesSnapshotPayload, sessionMessagesSubscribePayload,
} from './support/session-read-fixtures'
import { expectComposerValue } from './utils'

const SESSION = 'agent:main:webchat:e2e-preserve-bridge-live'
const TASK = 'synthetic-bridge-live-task'
const GENERATION = 'synthetic-bridge-generation'
const ANSWER = 'Keep this already visible answer until its canonical history is installed.'
const DRAFT = 'My next question must remain editable while history catches up.'

type Frame = { type: string; id: string; method: string; nonce?: string; params?: Record<string, unknown> }

/** Deliberate protocol fixture: this isolates a paginated history failure in
 * the real browser/Electron renderer, not production backend persistence. */
async function installHistoryBridgeFault(page: Page) {
  const sends: Frame[] = []
  const aborts: Frame[] = []
  const history: Array<Record<string, unknown>> = []
  let socket: WebSocketRoute
  let sequence = 0
  let running = false
  let completed = false
  let newHistory = false
  let bridgeAvailable = false
  let earlierReads = 0
  let failedBridges = 0
  let successfulBridges = 0
  const historicalRow = (index: number) => ({
    id: `history-${index}`, role: index % 2 ? 'user' : 'assistant',
    text: `History row ${index}. ${'Previously saved context. '.repeat(12)}`,
    timestamp: 1_800_000_000 + index,
  })
  const outcome = { task_id: TASK, turn_id: TASK, status: 'succeeded',
    started_at: 1_800_000_100, finished_at: 1_800_000_110, outcome: { kind: 'completed' } }
  const latest = () => chatHistoryPayload([
    { id: 'late-peer-user', role: 'user', text: 'A peer message that requires history catch-up.',
      timestamp: 1_800_000_108, turn_id: TASK },
    { id: 'canonical-answer', role: 'assistant', text: ANSWER,
      timestamp: 1_800_000_109, turn_id: TASK, turn_context: { turn_id: TASK } },
  ], { has_more: true, oldest_cursor: 'cursor-108', newest_cursor: 'cursor-109',
    history_scope: 'latest_window', turn_outcomes: [outcome] })
  const metadata = () => ({ stream_generation: GENERATION, current_stream_seq: sequence,
    run_status: running ? 'running' : 'idle',
    active_task: running ? { task_id: TASK, status: 'running' } : null,
    tasks: running ? [{ task_id: TASK, status: 'running' }] : [],
    last_task: completed ? { task_id: TASK, status: 'succeeded' } : null })
  const emit = (event: string, payload: Record<string, unknown>) => socket.send(JSON.stringify({
    type: 'event', event, payload: { key: SESSION, session_key: SESSION,
      task_id: TASK, turn_id: TASK, stream_generation: GENERATION, stream_seq: ++sequence, ...payload },
  }))
  const refresh = () => emit('session.event.input_disposition', {
    user_message_id: 'late-peer-user', client_message_id: 'late-peer-client',
    intent: 'send', disposition: 'applied', revision: sequence + 1,
  })
  await page.addInitScript(() => localStorage.setItem('opensquilla-locale', 'en'))
  await page.route('**/api/**', route => route.fulfill({ json: {} }))
  await page.route('**/api/approvals', route => route.fulfill({ json: {
    pending: [], mode: 'prompt', allowPatterns: [], denyPatterns: [],
  } }))
  await page.routeWebSocket(/\/ws$/, ws => {
    socket = ws
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(raw => {
      const frame = JSON.parse(String(raw)) as Frame
      const respond = (payload: unknown) => ws.send(JSON.stringify({ type: 'res', id: frame.id, ok: true, payload }))
      if (frame.type === 'ping') { ws.send(JSON.stringify({ type: 'pong', nonce: frame.nonce })); return }
      if (frame.type !== 'req') return
      if (frame.method === 'connect') {
        ws.send(helloOkResponse({ features: { methods: [
          'sessions.messages.subscribe', 'sessions.messages.hydrate',
          'sessions.messages.snapshot', 'sessions.messages.unsubscribe',
        ] }, policy: { concurrent_history_reads: true } }))
        return
      }
      if (frame.method === 'chat.send') {
        sends.push(frame); running = true
        respond({ accepted: true, session: SESSION, task_id: TASK })
        emit('task.running', { status: 'running' })
        emit('session.event.text_delta', { text: ANSWER, assistant_message_id: 'canonical-answer' })
        return
      }
      if (frame.method === 'chat.abort') { aborts.push(frame); respond({ aborted: true }); return }
      if (frame.method === 'chat.history') {
        history.push({ ...frame.params })
        if (frame.params?.after != null) {
          if (!bridgeAvailable) {
            failedBridges++
            respond(chatHistoryPayload([], { canonical_available: false, canonical_complete: false }))
          } else {
            successfulBridges++; running = false; completed = true
            const final = latest()
            respond({ ...final, has_more: false, oldest_cursor: 'cursor-101',
              messages: [...Array.from({ length: 7 }, (_, index) => historicalRow(index + 101)),
                ...(final.messages as Record<string, unknown>[])], history_scope: 'complete' })
          }
          return
        }
        if (frame.params?.before != null) {
          earlierReads++
          respond(chatHistoryPayload(Array.from({ length: 50 }, (_, index) => historicalRow(index + 1)), {
            oldest_cursor: 'cursor-1', newest_cursor: 'cursor-50', has_more: false,
          }))
          return
        }
        if (newHistory) respond(latest())
        else respond(chatHistoryPayload(Array.from({ length: 50 }, (_, index) => historicalRow(index + 51)), {
          oldest_cursor: 'cursor-51', newest_cursor: 'cursor-100', has_more: true, history_scope: 'latest_window',
        }))
        return
      }
      const payloads: Record<string, unknown> = {
        'agents.list': { agents: [] }, 'commands.list_for_surface': { commands: [] },
        'config.get': { squilla_router: { enabled: false, tiers: {} }, permissions: {}, skills: {} },
        'models.routing.get': { mode: 'direct' }, 'sandbox.run_mode.preference.get': { runMode: 'full', source: 'config' },
        'sessions.list': { sessions: [{ key: SESSION, title: 'History bridge reliability', sessionKind: 'chat',
          surface: 'webchat', conversationKind: 'direct', effectiveAgentId: 'main', updatedAt: 100,
          messageCount: 100, status: 'ok', runStatus: running ? 'running' : 'idle' }],
        count: 1, ts: 1_800_000_000, has_more: false },
        'sessions.messages.subscribe': sessionMessagesSubscribePayload(SESSION, metadata()),
        'sessions.messages.hydrate': sessionMessagesHydratePayload(SESSION, metadata()),
        'sessions.messages.snapshot': sessionMessagesSnapshotPayload(SESSION, {
          ...metadata(), task_id: running ? TASK : null,
        }),
        'sessions.messages.unsubscribe': { subscribed: false }, 'sessions.subscribe': { subscribed: true },
        'usage.status': { sessions: [] },
      }
      respond(payloads[frame.method] ?? {})
    })
  })
  return { sends, aborts, history, earlierReads: () => earlierReads,
    failedBridges: () => failedBridges, successfulBridges: () => successfulBridges,
    failBridge() { newHistory = true; refresh() },
    recoverBridge() { bridgeAvailable = true; refresh() },
  }
}

test('history catch-up failure preserves the displayed answer and draft until a complete bridge installs', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  const gateway = await installHistoryBridgeFault(page)
  const root = process.env.OPENSQUILLA_E2E_NATIVE_DESKTOP === '1'
    ? 'opensquilla-app://desktop/' : '/control/'
  await page.goto(`${root}chat?session=${encodeURIComponent(SESSION)}`)
  await expect(page.locator('.topbar[data-connection-state="connected"]')).toBeVisible()
  const thread = page.locator('.chat-thread')
  await expect(page.getByText(/^History row 100\./)).toBeVisible()
  await thread.hover()
  await page.mouse.wheel(0, -100_000)
  await expect.poll(() => gateway.earlierReads()).toBeGreaterThan(0)
  await thread.hover()
  await page.mouse.wheel(0, 100_000)

  const input = page.locator('.chat-textarea')
  await input.fill('Continue this long conversation.')
  await page.locator('.chat-send-btn[aria-label="Send"]').click()
  await expect(page.locator('.msg-ai').filter({ hasText: ANSWER })).toBeVisible()
  await input.fill(DRAFT)
  gateway.failBridge()
  await expect.poll(() => gateway.failedBridges()).toBeGreaterThan(0)
  // The latest page already contains a terminal outcome. Settling its controls
  // must preserve readable output while the canonical bridge is unavailable.
  await expect(page.locator('.msg-ai').filter({ hasText: ANSWER })).toBeVisible()
  await expectComposerValue(input, DRAFT)
  expect(gateway.sends).toHaveLength(1)
  expect(gateway.aborts).toEqual([])
  await page.screenshot({ path: testInfo.outputPath('answer-preserved-during-bridge-failure.png') })

  gateway.recoverBridge()
  await expect.poll(() => gateway.successfulBridges()).toBeGreaterThan(0)
  await expect(page.locator('.msg-ai').filter({ hasText: ANSWER })).toHaveCount(1)
  await expect(page.getByRole('button', { name: 'Stop current response', exact: true })).toHaveCount(0)
  await expect(page.locator('.chat-send-btn[aria-label="Send"]')).toBeEnabled()
  await expectComposerValue(input, DRAFT)
  expect(gateway.sends).toHaveLength(1)
  expect(gateway.aborts).toEqual([])
  const evidencePath = testInfo.outputPath('history-bridge-requests.json')
  await writeFile(evidencePath, JSON.stringify({ userAgent: await page.evaluate(() => navigator.userAgent),
    requests: gateway.history, sends: gateway.sends.length,
    aborts: gateway.aborts.length, failedBridges: gateway.failedBridges(),
    successfulBridges: gateway.successfulBridges() }, null, 2))
  await testInfo.attach('history-bridge-requests', { path: evidencePath, contentType: 'application/json' })
})
