import { expect, test, type Page } from '@playwright/test'

const CONTROL_URL = '/control/'
const SESSION_KEY = 'agent:main:webchat:e2e-terminal-history-stale-live'
const TASK_ID = 'task-e2e-terminal-history-stale-live'
const BASE_TIME = 1_800_000_000_000

function response(id: unknown, payload: unknown) {
  return JSON.stringify({ type: 'res', id, ok: true, payload })
}

function terminalHistory() {
  return {
    messages: [{
      role: 'user',
      text: 'Stop after the provider retry.',
      id: 'user-terminal-history',
      message_id: 'user-terminal-history',
      timestamp: BASE_TIME - 1_000,
      turn_context: { turn_id: TASK_ID },
    }],
    turn_outcomes: [{
      turn_id: TASK_ID,
      task_id: TASK_ID,
      status: 'cancelled',
      started_at: BASE_TIME,
      finished_at: BASE_TIME + 2_000,
      outcome: { kind: 'cancelled', cancellation_source: 'webui_stop' },
      activity_snapshot: {
        version: 2,
        task_id: TASK_ID,
        turn_id: TASK_ID,
        complete: true,
        reasoning_utf16_length: 0,
        entries: [
          {
            type: 'phase', id: 'provider:requesting:1', order: 1,
            kind: 'provider', phase: 'requesting',
            at: BASE_TIME, ended_at: BASE_TIME + 500,
          },
          {
            type: 'phase', id: 'provider:retry_wait:2', order: 2,
            kind: 'provider', phase: 'retry_wait', reason: 'rate_limited',
            retry_after_ms: 1_000,
            at: BASE_TIME + 500, ended_at: BASE_TIME + 2_000,
          },
        ],
      },
    }],
    has_more: false,
    canonical_complete: true,
  }
}

async function installStaleLiveFixture(page: Page) {
  await page.addInitScript(() => localStorage.setItem('opensquilla-locale', 'en'))
  await page.route('**/api/approvals', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ pending: [], mode: 'prompt', allowPatterns: [], denyPatterns: [] }),
  }))
  await page.route('**/api/system/update', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ available: false }),
  }))
  await page.route('**/api/elevated-mode', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ enabled: false }),
  }))
  await page.routeWebSocket(/\/ws$/, ws => {
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(message => {
      const frame = JSON.parse(String(message)) as {
        id?: string | number
        method?: string
        type?: string
      }
      if (frame.type === 'ping') {
        ws.send(JSON.stringify({ type: 'pong' }))
        return
      }
      if (frame.type !== 'req') return
      if (frame.method === 'connect') {
        ws.send(JSON.stringify({
          protocol: 3,
          policy: { tick_interval_ms: 30_000, concurrent_history_reads: true },
        }))
        return
      }
      const method = String(frame.method || '')
      if (method === 'chat.history') {
        ws.send(response(frame.id, terminalHistory()))
        return
      }
      if (method === 'sessions.messages.snapshot') {
        ws.send(response(frame.id, {
          key: SESSION_KEY,
          task_id: TASK_ID,
          stream_generation: 'stale-terminal-generation',
          current_stream_seq: 2,
          events: [{
            event: 'session.event.provider_activity',
            payload: {
              session_key: SESSION_KEY,
              task_id: TASK_ID,
              turn_id: TASK_ID,
              stream_seq: 2,
              emitted_at: BASE_TIME + 1_000,
              phase: 'reasoning',
            },
          }],
        }))
        return
      }
      const staleLiveState = {
        subscribed: true,
        hydration_complete: true,
        replay_complete: true,
        stream_generation: 'stale-terminal-generation',
        current_stream_seq: 2,
        run_status: 'running',
        active_task: { task_id: TASK_ID, status: 'running', started_at: BASE_TIME },
        tasks: [{ task_id: TASK_ID, status: 'running' }],
      }
      const payloads: Record<string, unknown> = {
        'agents.list': { agents: [] },
        'commands.list_for_surface': { commands: [] },
        'config.get': {
          squilla_router: { enabled: false, rollout_phase: 'observe', tiers: {} },
          permissions: {}, skills: {},
        },
        'models.routing.get': { mode: 'direct' },
        'onboarding.status': { audioConfigured: false },
        'sessions.list': { sessions: [], has_more: false },
        'sessions.messages.subscribe': staleLiveState,
        'sessions.messages.hydrate': staleLiveState,
        'sessions.messages.unsubscribe': { subscribed: false },
        'sessions.subscribe': { subscribed: true },
        'usage.status': { sessions: [] },
      }
      ws.send(response(frame.id, payloads[method] ?? {}))
    })
  })
}

test('terminal history keeps activity-only output and rejects the same task stale live state', async ({
  page,
}) => {
  await installStaleLiveFixture(page)

  await page.goto(`${CONTROL_URL}chat?session=${encodeURIComponent(SESSION_KEY)}`)
  await expect(page.locator('.conn-pill.connected')).toBeVisible({ timeout: 10_000 })
  await expect(page.getByText('Stop after the provider retry.', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: /^Stopped/ })).toBeVisible()

  const activity = page.locator('.msg-ai .assistant-activity')
  await expect(activity).toBeVisible()
  await expect(page.locator('.assistant-activity--live')).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Stop current response' })).toHaveCount(0)
  await expect(page.locator('.chat-send-btn[aria-label="Send"]')).toBeVisible()
})
