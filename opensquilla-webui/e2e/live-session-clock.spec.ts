import { expect, test } from '@playwright/test'

const CONTROL_URL = '/control/'
const SESSION_A = 'agent:main:webchat:e2e-live-clock-a'
const SESSION_B = 'agent:main:webchat:e2e-live-clock-b'
const SESSION_A_TITLE = 'Live clock session A'
const SESSION_B_TITLE = 'Idle session B'
const TASK_ID = 'task-e2e-live-clock'
const STREAM_GENERATION = 'live-clock-generation'
const TASK_STARTED_AT = Date.parse('2026-08-17T00:00:00Z')

function response(id: string | number | undefined, payload: unknown) {
  return JSON.stringify({ type: 'res', id, ok: true, payload })
}

function event(name: string, payload: Record<string, unknown>) {
  return JSON.stringify({ type: 'event', event: name, payload })
}

function elapsedSeconds(value: string): number {
  const match = value.match(/(\d+)s/)
  return match ? Number(match[1]) : -1
}

test('keeps one task clock across A to B to A without sending again', async ({ page }) => {
  let taskRunning = false
  let chatSendCount = 0
  const abortCalls: Array<Record<string, unknown>> = []

  await page.clock.install({ time: new Date(TASK_STARTED_AT) })
  await page.addInitScript(() => {
    window.localStorage.setItem('opensquilla-locale', 'en')
  })
  await page.route('**/api/approvals', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ pending: [] }),
  }))
  await page.routeWebSocket(/\/ws$/, ws => {
    ws.send(event('connect.challenge', {}))
    ws.onMessage(message => {
      try {
        const frame = JSON.parse(String(message)) as {
          type?: string
          id?: string | number
          method?: string
          params?: Record<string, unknown>
        }
        if (frame.type !== 'req') return
        const method = String(frame.method || '')
        const key = String(frame.params?.key || frame.params?.sessionKey || '')

        if (method === 'connect') {
          ws.send(JSON.stringify({
            protocol: 3,
            policy: {
              tick_interval_ms: 30_000,
              webui_stream_idle_grace_ms: 1_260_000,
            },
          }))
          return
        }
        if (method === 'chat.send') {
          chatSendCount += 1
          taskRunning = true
          ws.send(response(frame.id, {
            accepted: true,
            session: SESSION_A,
            sessionKey: SESSION_A,
            task_id: TASK_ID,
            stream_seq: 1,
            user_message_id: 'user-e2e-live-clock',
          }))
          ws.send(event('task.running', {
            key: SESSION_A,
            session_key: SESSION_A,
            task_id: TASK_ID,
            stream_generation: STREAM_GENERATION,
            stream_seq: 1,
          }))
          ws.send(event('session.event.provider_activity', {
            key: SESSION_A,
            session_key: SESSION_A,
            task_id: TASK_ID,
            stream_generation: STREAM_GENERATION,
            stream_seq: 2,
            schema_version: 1,
            activity_id: 'provider-e2e-live-clock',
            phase: 'reasoning',
            reason: 'initial',
            started_at: TASK_STARTED_AT,
            heartbeat: false,
          }))
          return
        }
        if (method === 'chat.abort') {
          abortCalls.push({ ...(frame.params || {}) })
          ws.send(response(frame.id, { aborted: true, key: SESSION_A }))
          return
        }
        if (method === 'sessions.messages.subscribe') {
          ws.send(response(frame.id, {
            subscribed: true,
            hydration_complete: false,
            replay_complete: true,
            stream_generation: STREAM_GENERATION,
            current_stream_seq: key === SESSION_A && taskRunning ? 2 : 0,
            run_status: 'idle',
          }))
          return
        }
        if (method === 'sessions.messages.snapshot') {
          const live = key === SESSION_A && taskRunning
          ws.send(response(frame.id, {
            key,
            task_id: live ? TASK_ID : null,
            stream_generation: STREAM_GENERATION,
            current_stream_seq: live ? 2 : 0,
            events: live
              ? [{
                  event: 'session.event.provider_activity',
                  payload: {
                    key: SESSION_A,
                    session_key: SESSION_A,
                    task_id: TASK_ID,
                    stream_generation: STREAM_GENERATION,
                    stream_seq: 2,
                    schema_version: 1,
                    activity_id: 'provider-e2e-live-clock',
                    phase: 'reasoning',
                    reason: 'initial',
                    started_at: TASK_STARTED_AT,
                    heartbeat: false,
                  },
                }]
              : [],
          }))
          return
        }
        if (method === 'sessions.messages.hydrate') {
          const live = key === SESSION_A && taskRunning
          ws.send(response(frame.id, {
            key,
            hydration_complete: true,
            run_status: live ? 'running' : 'idle',
            active_task: live
              ? {
                  task_id: TASK_ID,
                  turn_id: TASK_ID,
                  status: 'running',
                  started_at: TASK_STARTED_AT,
                }
              : null,
            tasks: live
              ? [{ task_id: TASK_ID, status: 'running', started_at: TASK_STARTED_AT }]
              : [],
            active_task_group_ids: [],
          }))
          return
        }
        if (method === 'chat.history') {
          const live = key === SESSION_A && taskRunning
          ws.send(response(frame.id, {
            messages: live
              ? [{
                  role: 'user',
                  text: 'Keep working while I switch sessions.',
                  id: 'user-e2e-live-clock',
                  message_id: 'user-e2e-live-clock',
                  timestamp: Math.floor(TASK_STARTED_AT / 1_000),
                }]
              : [],
            has_more: false,
            canonical_complete: true,
          }))
          return
        }
        const payloads: Record<string, unknown> = {
          'agents.list': { agents: [] },
          'commands.list_for_surface': { commands: [] },
          'config.get': {
            squilla_router: { enabled: false, rollout_phase: 'observe', tiers: {} },
            permissions: {},
            skills: {},
          },
          'models.routing.get': { mode: 'direct' },
          'onboarding.status': { audioConfigured: false },
          'sessions.list': {
            sessions: [
              {
                key: SESSION_B,
                title: SESSION_B_TITLE,
                sessionKind: 'chat',
                surface: 'webchat',
                conversationKind: 'direct',
                effectiveAgentId: 'main',
                updatedAt: 200,
                messageCount: 0,
                status: 'ok',
                runStatus: 'idle',
              },
              {
                key: SESSION_A,
                title: SESSION_A_TITLE,
                sessionKind: 'chat',
                surface: 'webchat',
                conversationKind: 'direct',
                effectiveAgentId: 'main',
                updatedAt: 100,
                messageCount: taskRunning ? 1 : 0,
                status: 'ok',
                runStatus: taskRunning ? 'running' : 'idle',
                active_task: taskRunning
                  ? { task_id: TASK_ID, status: 'running', started_at: TASK_STARTED_AT }
                  : null,
              },
            ],
            has_more: false,
          },
          'sessions.messages.unsubscribe': null,
          'usage.status': { sessions: [] },
        }
        ws.send(response(frame.id, payloads[method] ?? {}))
      } catch {}
    })
  })

  await page.goto(`${CONTROL_URL}chat?session=${encodeURIComponent(SESSION_A)}`)
  await expect(page.locator('.conn-pill.connected')).toBeVisible({ timeout: 10_000 })
  await expect(page.locator('.chat-textarea')).toBeVisible()

  await page.locator('.chat-textarea').fill('Keep working while I switch sessions.')
  await page.getByRole('button', { name: 'Send', exact: true }).click()
  const elapsed = page.locator('.assistant-activity__live-elapsed')
  await expect(elapsed).toBeVisible()
  await page.clock.runFor(5_000)
  const beforeSwitch = elapsedSeconds(await elapsed.innerText())
  expect(beforeSwitch).toBeGreaterThanOrEqual(5)

  await page
    .locator('.sidebar-history-row[data-family="chats"]')
    .filter({ hasText: SESSION_B_TITLE })
    .locator('.sidebar-history-item')
    .click()
  await expect.poll(() => new URL(page.url()).searchParams.get('session')).toBe(SESSION_B)
  await page.clock.runFor(4_000)

  await page
    .locator('.sidebar-history-row[data-family="chats"]')
    .filter({ hasText: SESSION_A_TITLE })
    .locator('.sidebar-history-item')
    .click()
  await expect.poll(() => new URL(page.url()).searchParams.get('session')).toBe(SESSION_A)
  await expect(elapsed).toBeVisible()
  await expect.poll(async () => elapsedSeconds(await elapsed.innerText())).toBeGreaterThanOrEqual(
    beforeSwitch + 4,
  )
  expect(chatSendCount).toBe(1)

  await page.getByRole('button', { name: 'Stop current response' }).click()
  await expect.poll(() => abortCalls.length).toBe(1)
  expect(abortCalls).toEqual([{
    sessionKey: SESSION_A,
    taskId: TASK_ID,
    source: 'webui_stop',
    scope: 'task',
  }])
})
