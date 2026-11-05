import { expect, test } from '@playwright/test'
import { helloOkResponse } from './support/gateway-fixture'
import {
  sessionMessagesHydratePayload,
  sessionMessagesSnapshotPayload,
  sessionMessagesSubscribePayload,
} from './support/session-read-fixtures'

const SESSION = 'agent:main:webchat:history-size-limit'
const HISTORY_METHOD = 'sessions.history.page.v2'

test('explains a nonretryable earlier history limit while preserving loaded messages', async ({ page }) => {
  let earlierRequests = 0
  await page.addInitScript(() => localStorage.setItem('opensquilla-locale', 'en'))
  await page.route('**/api/**', route => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ pending: [], enabled: false, mode: 'prompt' }),
  }))
  await page.routeWebSocket(/\/ws$/, ws => {
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(raw => {
      const frame = JSON.parse(String(raw))
      if (frame.type === 'ping') { ws.send(JSON.stringify({ type: 'pong' })); return }
      if (frame.type !== 'req') return
      if (frame.method === 'connect') {
        ws.send(helloOkResponse({ features: { methods: [
          'sessions.read.open.v2', 'sessions.read.state.v2', 'sessions.read.install.v2',
          'sessions.read.close.v2', HISTORY_METHOD,
        ] } }))
        return
      }
      if (frame.method === HISTORY_METHOD && frame.params?.cursor != null) {
        expect(frame.params.target_bytes).toBe(1_048_576)
        earlierRequests += 1
        ws.send(JSON.stringify({
          type: 'res', id: frame.id, ok: false,
          error: {
            code: 'RESPONSE_TOO_LARGE',
            message: 'History item exceeds the requested page byte budget',
            retryable: false,
          },
        }))
        return
      }
      const messages = Array.from({ length: 50 }, (_, index) => ({
        id: `message-${index + 1}`,
        role: index % 2 === 0 ? 'user' : 'assistant',
        text: `Loaded row ${index + 1}. ${'Synthetic conversation content. '.repeat(8)}`,
        timestamp: 1_800_000_000 + index,
      }))
      const progress = { status: 'installed', base_seq: 0, target_seq: 0, next_seq: 1, consumed_through_seq: 0 }
      const payloads: Record<string, unknown> = {
        [HISTORY_METHOD]: {
          session_id: 'history-session', session_epoch: 1, projection_revision: 1,
          before_cursor: 'older-boundary', after_cursor: 'latest-boundary',
          has_more_before: true, has_more_after: false, complete_for_requested_window: true,
          canonical_available: true, canonical_complete: true, history_scope: 'latest_window',
          compaction_summaries: [], turn_outcomes: [],
          items: messages.map((message, index) => ({
            message_id: message.id, item_id: message.id, order: String(index + 1),
            role: message.role, preview: message.text, preview_complete: true,
            source_revision: `revision-${index}`, content_availability: 'ready', contents: [], message,
          })),
        },
        'sessions.read.open.v2': {
          lease_id: 'history-lease', recovery_id: 'history-recovery', session_id: 'history-session',
          session_epoch: 1, connection_epoch: 'connection', subscription_epoch: 'subscription',
          state_revision: 1, base_stream_generation: 'e2e-stream-generation', base_stream_seq: 0,
          state_manifest: { length: 0, sha256: 'a'.repeat(64), schema_version: 1, chunks: [] },
        },
        'sessions.read.state.v2': {
          lease_id: 'history-lease', recovery_id: 'history-recovery', session_id: 'history-session',
          session_epoch: 1, state_revision: 1, status: 'installed', progress,
        },
        'sessions.read.install.v2': {
          lease_id: 'history-lease', recovery_id: 'history-recovery', status: 'installed', progress,
        },
        'sessions.read.close.v2': { lease_id: 'history-lease', closed: true, status: 'retired' },
        'sessions.messages.subscribe': sessionMessagesSubscribePayload(SESSION),
        'sessions.messages.snapshot': sessionMessagesSnapshotPayload(SESSION),
        'sessions.messages.hydrate': sessionMessagesHydratePayload(SESSION),
        'agents.list': { agents: [] }, 'commands.list_for_surface': { commands: [] },
        'config.get': { squilla_router: { enabled: false, tiers: {} }, permissions: {}, skills: {} },
        'sessions.list': { sessions: [], count: 0, ts: 1_800_000_000, has_more: false },
        'sessions.resolve': { session_key: SESSION, session_id: SESSION },
        'usage.status': { sessions: [] },
      }
      ws.send(JSON.stringify({ type: 'res', id: frame.id, ok: true, payload: payloads[frame.method] ?? {} }))
    })
  })

  await page.goto('/control/chat?session=' + encodeURIComponent(SESSION))
  const thread = page.locator('.chat-thread')
  await expect(thread).toContainText('Loaded row 50.')
  await thread.hover()
  await page.mouse.wheel(0, -100_000)
  await expect.poll(() => earlierRequests).toBe(1)
  await expect(page.getByTestId('history-load-failure')).toContainText('Earlier messages exceed the loading limit.')
  await expect(page.getByTestId('history-load-failure')).toContainText('Loaded messages remain available.')
  await expect(page.getByTestId('history-load-retry')).toHaveCount(0)
  expect(earlierRequests).toBe(1)
  await expect(thread).toContainText('Loaded row 1.')
  await expect(thread).not.toContainText('RESPONSE_TOO_LARGE')
})
