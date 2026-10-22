import { test, expect, type Page } from '@playwright/test'
import { helloOkResponse } from './support/gateway-fixture'
import {
  chatHistoryPayload, sessionMessagesHydratePayload,
  sessionMessagesSnapshotPayload, sessionMessagesSubscribePayload,
} from './support/session-read-fixtures'

const SESSION_A = 'agent:main:webchat:e2e-compact-recovery-a'
const SESSION_B = 'agent:main:webchat:e2e-compact-recovery-b'
const COMPACTION_ID = 'cmp-e2e-warm-history-recovery'
const GENERATION = 'e2e-stream-generation'
const BASE_TIME = Math.floor(Date.now() / 1_000) - 120

async function installCompactionRecovery(page: Page) {
  await page.addInitScript(() => localStorage.setItem('opensquilla-locale', 'en'))
  await page.route('**/api/**', route => route.fulfill({ json: {} }))
  await page.route('**/api/approvals', route => route.fulfill({
    json: { pending: [], mode: 'prompt', allowPatterns: [], denyPatterns: [] },
  }))
  const messages = Array.from({ length: 19 }, (_, index) => ({
    id: `a-message-${index}`,
    role: index < 18 && index % 3 === 0 ? 'user' : 'assistant',
    text: `Synthetic original ${index < 18 && index % 3 === 0 ? 'query' : 'output'} ${index}.`,
    timestamp: BASE_TIME + index,
  }))
  const userIds = messages.filter(message => message.role === 'user').map(message => message.id)
  let historyBusy = false
  let running = false
  let completed = false
  let completedAt = 0
  let historyFailures = 0
  let emit: (status: 'started' | 'completed', sequence: number) => void = () => {}
  await page.routeWebSocket(/\/ws$/, socket => {
    const progress = (status: string, sequence: number) => ({
      key: SESSION_A, source: 'manual', compaction_id: COMPACTION_ID,
      status, sequence, stream_seq: sequence, stream_generation: GENERATION,
      ...(status === 'observed' ? { heartbeat: true, stage: 'summarizing' } : {}),
      ...(status === 'completed' ? { durability: 'durable', applied: true } : {}),
    })
    emit = (status, sequence) => socket.send(JSON.stringify({
      type: 'event', event: 'session.event.compaction', payload: progress(status, sequence),
    }))
    socket.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    socket.onMessage(raw => {
      const frame = JSON.parse(String(raw))
      if (frame.type === 'ping') { socket.send(JSON.stringify({ type: 'pong', nonce: frame.nonce })); return }
      if (frame.type !== 'req') return
      if (frame.method === 'connect') {
        socket.send(helloOkResponse({ features: {
          methods: ['sessions.messages.subscribe', 'sessions.messages.hydrate', 'sessions.messages.snapshot'],
          events: ['session.event.compaction'],
        } }))
        return
      }
      const key = String(frame.params?.key || frame.params?.sessionKey || SESSION_A)
      if (frame.method === 'chat.history' && key === SESSION_A && historyBusy) {
        historyFailures++
        socket.send(JSON.stringify({ type: 'res', id: frame.id, ok: false, error: {
          code: 'STORAGE_BUSY', message: 'Synthetic session history lock is busy.',
          retryable: true, retry_after_ms: 100,
          details: { operation: 'chat.history', stage: 'lock_acquire', resource: 'session_mutation_lock' },
        } }))
        return
      }
      const currentSeq = key === SESSION_A && running ? 2 : completed && key === SESSION_A ? 3 : 0
      const payloads: Record<string, unknown> = {
        'chat.history': chatHistoryPayload(key === SESSION_A ? messages : [{
          id: 'b-user', role: 'user', text: 'The other synthetic task.', timestamp: BASE_TIME,
        }], { compaction_summaries: key === SESSION_A && completed ? [{
          id: 'saved-recovery-summary', compaction_id: COMPACTION_ID, compaction_index: 1,
          trigger_reason: 'manual', summary_text: 'Synthetic saved checkpoint.',
          removed_count: 19, kept_count: 0, covered_through_id: 19,
          created_at: completedAt,
        }] : [] }),
        'sessions.messages.subscribe': sessionMessagesSubscribePayload(key, { current_stream_seq: currentSeq }),
        'sessions.messages.snapshot': sessionMessagesSnapshotPayload(key, {
          current_stream_seq: currentSeq,
          events: key === SESSION_A && running ? [{
            event: 'session.event.compaction', payload: progress('observed', 2),
          }] : [],
        }),
        'sessions.messages.hydrate': sessionMessagesHydratePayload(key),
        'sessions.messages.unsubscribe': { subscribed: false },
        'sessions.resolve': { session_key: key, session_id: key },
        'sessions.list': {
          sessions: [SESSION_A, SESSION_B].map((sessionKey, index) => ({
            key: sessionKey, title: index === 0 ? 'Compact recovery A' : 'Compact recovery B',
            sessionKind: 'chat', surface: 'webchat', conversationKind: 'direct',
            effectiveAgentId: 'main', updatedAt: BASE_TIME - index,
            messageCount: index === 0 ? 19 : 1, status: 'ok', runStatus: 'idle',
          })), count: 2, ts: BASE_TIME, has_more: false,
        },
        'agents.list': { agents: [] }, 'commands.list_for_surface': { commands: [] },
        'config.get': { squilla_router: { enabled: false, rollout_phase: 'observe', tiers: {} }, permissions: {}, skills: {} },
        'onboarding.status': { audioConfigured: false }, 'usage.status': { sessions: [] },
        'models.routing.get': { mode: 'direct' },
        'sandbox.run_mode.preference.get': { runMode: 'full', source: 'config' },
      }
      socket.send(JSON.stringify({ type: 'res', id: frame.id, ok: true, payload: payloads[frame.method] ?? {} }))
    })
  })
  return {
    userIds,
    historyFailures: () => historyFailures,
    start: () => { running = true; emit('started', 1) },
    failHistory: () => { historyBusy = true },
    complete: () => { running = false; completed = true; completedAt = Date.now(); historyBusy = false; emit('completed', 3) },
  }
}

test('manual compaction keeps warm queries through A/B/A and a failed history replacement', async ({ page }, testInfo) => {
  // Entire transport is synthetic. No provider call or real profile is used.
  const gateway = await installCompactionRecovery(page)
  await page.goto('/control/chat?session=' + encodeURIComponent(SESSION_A))
  for (const id of gateway.userIds) await expect(page.locator(`.msg-user[data-message-id="${id}"]`)).toHaveCount(1)
  await expect(page.locator('.chat-message-list')).toHaveAttribute('data-rendered-message-count', '19')
  gateway.start()
  const operation = page.getByTestId('compaction-event')
  const ownedOperation = page.locator(`[data-testid="compaction-event"][data-compaction-id="${COMPACTION_ID}"]`)
  const ownedRow = page.getByTestId('chat-message-row').filter({ has: ownedOperation })
  await expect(ownedOperation).toHaveCount(1)
  await expect(ownedOperation).toHaveAttribute('data-status', 'running')
  await expect(ownedRow).toHaveAttribute('data-chat-message-index', '19')

  await page.locator(`[data-session-key="${SESSION_B}"] .sidebar-history-item`).click()
  await expect(page.locator('.chat-thread')).toHaveAttribute('data-session-key', SESSION_B)
  await expect(page.locator('.msg-user')).toContainText('The other synthetic task.')
  gateway.failHistory()
  await page.locator(`[data-session-key="${SESSION_A}"] .sidebar-history-item`).click()
  await expect(page.locator('.chat-thread')).toHaveAttribute('data-session-key', SESSION_A)
  await expect.poll(gateway.historyFailures).toBeGreaterThan(0)
  await expect(page.getByTestId('chat-session-recovery-status')).toHaveAttribute('data-recovery-state', 'history-error')
  for (const id of gateway.userIds) await expect(page.locator(`.msg-user[data-message-id="${id}"]`)).toHaveCount(1)
  await expect(ownedOperation).toHaveCount(1)
  await expect(ownedRow).toHaveAttribute('data-chat-message-index', '19')
  await expect(page.locator('[data-testid="compaction-event"][data-placement="turn-boundary"]')).toHaveCount(0)
  await testInfo.attach('warm history during synthetic storage failure', { body: await page.screenshot(), contentType: 'image/png' })

  gateway.complete()
  await expect(ownedOperation).toHaveAttribute('data-status', 'completed')
  await expect(ownedOperation).toHaveAttribute('data-durability', 'durable')
  await expect(ownedOperation).toHaveCount(1)
  await expect(ownedRow).toHaveAttribute('data-chat-message-index', '19')
  await expect(operation).toHaveCount(1)
  await expect(page.getByTestId('chat-session-recovery-status')).toHaveCount(0)
  for (const id of gateway.userIds) await expect(page.locator(`.msg-user[data-message-id="${id}"]`)).toHaveCount(1)
  await expect(page.locator('.chat-message-list')).toHaveAttribute('data-rendered-message-count', '20')
})
