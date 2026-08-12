import { expect, test, type Page } from '@playwright/test'

const CONTROL_URL = '/control/'
const SESSION_KEY = 'agent:main:webchat:e2e-queued-runtime-phase'
const TASK_ID = 'queued-runtime-phase-task'

function wsResponse(id: string | number | undefined, payload: unknown) {
  return JSON.stringify({ type: 'res', id, ok: true, payload })
}

function wsEvent(event: string, payload: unknown) {
  return JSON.stringify({ type: 'event', event, payload })
}

async function mockQueuedRouterLifecycle(page: Page) {
  let sendFrame: ((frame: string) => void) | null = null
  let streamSeq = 2

  const emit = (event: string, payload: Record<string, unknown> = {}) => {
    if (!sendFrame) throw new Error('queued lifecycle websocket is not connected')
    sendFrame(wsEvent(event, {
      key: SESSION_KEY,
      session_key: SESSION_KEY,
      task_id: TASK_ID,
      stream_seq: streamSeq++,
      ...payload,
    }))
  }

  await page.addInitScript(() => {
    window.localStorage.setItem('opensquilla-locale', 'en')
    window.localStorage.setItem('opensquilla.routerVisualEffects', '1')
  })
  await page.route('**/api/approvals', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ pending: [] }),
  }))
  await page.routeWebSocket(/\/ws$/, ws => {
    sendFrame = frame => ws.send(frame)
    ws.send(wsEvent('connect.challenge', {}))
    ws.onMessage(message => {
      let frame: Record<string, unknown>
      try {
        frame = JSON.parse(String(message)) as Record<string, unknown>
      } catch {
        return
      }
      if (frame.type !== 'req') return
      const method = String(frame.method || '')
      if (method === 'connect') {
        ws.send(JSON.stringify({ protocol: 3, policy: { tick_interval_ms: 30_000 } }))
        return
      }
      if (method === 'chat.send') {
        ws.send(wsResponse(frame.id as string | number | undefined, {
          accepted: true,
          session: SESSION_KEY,
          sessionKey: SESSION_KEY,
          task_id: TASK_ID,
          stream_seq: 1,
          user_message_id: 'queued-runtime-phase-user',
        }))
        ws.send(wsEvent('task.queued', {
          key: SESSION_KEY,
          session_key: SESSION_KEY,
          task_id: TASK_ID,
          stream_seq: 1,
          queue_depth: 2,
          queue_position: 2,
        }))
        return
      }

      const payloads: Record<string, unknown> = {
        'agents.list': { agents: [] },
        'chat.history': { messages: [], has_more: false },
        'commands.list_for_surface': { commands: [] },
        'config.get': {
          squilla_router: {
            enabled: true,
            rollout_phase: 'full',
            tiers: {
              c0: { model: 'test/router-small' },
              c1: { model: 'test/router-selected' },
              c2: { model: 'test/router-large' },
            },
          },
          llm_ensemble: { enabled: false },
          permissions: {},
          skills: {},
        },
        'onboarding.status': { audioConfigured: false },
        'sessions.list': { sessions: [], has_more: false },
        'sessions.messages.subscribe': {
          subscribed: true,
          replay_complete: true,
          current_stream_seq: 0,
          run_status: 'idle',
        },
        'usage.status': { sessions: [] },
      }
      ws.send(wsResponse(
        frame.id as string | number | undefined,
        payloads[method] ?? {},
      ))
    })
  })

  return {
    markRunning() {
      emit('task.running')
    },
    markProviderActive() {
      emit('session.event.state_change', { to_state: 'thinking' })
    },
    decideRoute() {
      emit('session.event.router_decision', {
        tier: 'c1',
        model: 'test/router-selected',
        source: 'squilla_router',
        routing_applied: true,
      })
    },
  }
}

test('durable queued stays queued until running and an authoritative router decision', async ({
  page,
}) => {
  const lifecycle = await mockQueuedRouterLifecycle(page)
  await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(SESSION_KEY))
  await expect(page.locator('.conn-pill.connected')).toBeVisible({ timeout: 10_000 })

  await page.locator('.chat-textarea').fill('Wait for a real runtime slot.')
  await page.locator('.chat-send-btn[aria-label="Send"]').click()

  const activity = page.locator('.assistant-activity--live')
  await expect(activity).toBeVisible({ timeout: 10_000 })
  await expect(activity.locator('.assistant-activity__live-label')).toHaveText('Queued')
  await expect(activity).not.toContainText('Waiting for model')
  await expect(page.locator('.router-fx')).toHaveCount(0)

  lifecycle.markRunning()
  await expect(activity.locator('.assistant-activity__live-label')).toHaveText('Running')
  await expect(page.locator('.router-fx')).toHaveCount(0)

  lifecycle.markProviderActive()
  await expect(activity.locator('.assistant-activity__live-label')).toHaveText(
    'Working',
  )
  await expect(activity).not.toContainText('Waiting for model')
  await expect(page.locator('.router-fx')).toHaveCount(0)

  lifecycle.decideRoute()
  await expect(page.locator('.router-fx')).toHaveCount(1)
  await expect(page.locator('.router-fx')).toContainText('router-selected')
})
