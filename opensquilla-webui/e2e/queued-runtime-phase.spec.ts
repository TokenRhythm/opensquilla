import { expect, test, type Page } from '@playwright/test'
import { helloOkResponse } from './support/gateway-fixture'
import {
  chatHistoryPayload, sessionMessagesHydratePayload,
  sessionMessagesSnapshotPayload, sessionMessagesSubscribePayload,
} from './support/session-read-fixtures'

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
  let sends = 0

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
  await page.route('**/api/**', route => route.fulfill({ json: {} }))
  await page.route('**/api/approvals', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ mode: 'prompt', pending: [] }),
  }))
  await page.route('**/api/elevated-mode', route => route.fulfill({ json: { enabled: false } }))
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
        ws.send(helloOkResponse())
        return
      }
      if (method === 'chat.send') {
        sends += 1
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
        'chat.history': chatHistoryPayload(),
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
        'models.routing.get': { mode: 'router' },
        'sessions.routing.get': { key: SESSION_KEY, mode: 'router', revision: 0 },
        'sandbox.run_mode.preference.get': { runMode: 'full', source: 'config' },
        'sessions.list': { sessions: [], count: 0, ts: 1_800_000_000, has_more: false },
        'sessions.messages.subscribe': sessionMessagesSubscribePayload(SESSION_KEY),
        'sessions.messages.hydrate': sessionMessagesHydratePayload(SESSION_KEY),
        'sessions.messages.snapshot': sessionMessagesSnapshotPayload(SESSION_KEY),
        'usage.status': { sessions: [] },
      }
      ws.send(wsResponse(
        frame.id as string | number | undefined,
        payloads[method] ?? {},
      ))
    })
  })

  return {
    get sends() { return sends },
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
  const textarea = page.locator('.chat-textarea')
  const send = page.locator('.chat-send-btn[aria-label="Send"]')
  await expect(textarea).toBeVisible()
  await textarea.fill('Wait for a real runtime slot.')
  await expect(send).toBeEnabled({ timeout: 10_000 })
  await send.click()
  await expect.poll(() => lifecycle.sends).toBe(1)

  const activity = page.locator('.assistant-activity--live')
  const header = activity.locator('.assistant-activity__live-elapsed')
  const stop = page.getByRole('button', { name: 'Stop current response', exact: true })
  await expect(activity).toBeVisible({ timeout: 10_000 })
  await expect(header).toHaveText('Queued')
  await expect(header).not.toContainText('0s')
  await expect(stop).toBeEnabled()
  await expect(activity).not.toContainText('Waiting for model')
  await expect(activity.locator('.assistant-activity__stale-note')).toHaveCount(0)
  await expect(page.locator('.router-fx')).toHaveCount(0)

  lifecycle.markRunning()
  await expect(header).toHaveText(/^Time spent /)
  await expect(activity.locator('.assistant-activity__sr-only')).toHaveText('Working')
  await expect(stop).toBeEnabled()
  await expect(page.locator('.router-fx')).toHaveCount(0)

  lifecycle.markProviderActive()
  await expect(header).toHaveText(/^Time spent /)
  await expect(activity.locator('.assistant-activity__sr-only')).toHaveText('Working')
  await expect(activity).not.toContainText('Waiting for model')
  await expect(page.locator('.router-fx')).toHaveCount(0)

  lifecycle.decideRoute()
  await expect(page.locator('.router-fx')).toHaveCount(1)
  await expect(page.locator('.router-fx')).toContainText('router-selected')
  expect(lifecycle.sends).toBe(1)
})
