import { expect, test, type WebSocketRoute } from '@playwright/test'
import { helloOkResponse } from './support/gateway-fixture'
import {
  chatHistoryPayload, sessionMessagesHydratePayload,
  sessionMessagesSnapshotPayload, sessionMessagesSubscribePayload,
} from './support/session-read-fixtures'

const SESSION = 'agent:main:webchat:ordinary-progress'

test('Default task progress updates live and survives refresh without a Plan or Goal', { tag: '@plan-goal-runtime' }, async ({ page }) => {
  let progress = { revision: 1, explanation: 'Check the ordinary task.', steps: [{ step: 'Inspect files', status: 'in_progress' }] }
  let socket: WebSocketRoute | undefined
  await page.addInitScript(() => localStorage.setItem('opensquilla-locale', 'en'))
  await page.route('**/api/approvals', route => route.fulfill({ json: { mode: 'prompt', pending: [] } }))
  await page.route('**/api/elevated-mode', route => route.fulfill({ json: { enabled: false } }))
  await page.route('**/api/system/update', route => route.fulfill({ json: {} }))
  await page.routeWebSocket(/\/ws$/, ws => {
    socket = ws
    const reply = (id: string, payload: unknown) => ws.send(JSON.stringify({ type: 'res', id, ok: true, payload }))
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(message => {
      const frame = JSON.parse(String(message))
      if (frame.type !== 'req') return
      if (frame.method === 'connect') {
        ws.send(helloOkResponse({ features: { methods: [], events: ['session.event.progress'] }, auth: { principal: { isOwner: true } } }))
        return
      }
      const task = { task_id: 'ordinary-task', status: 'running', progress }
      const metadata = { epoch: 1, collaboration: { mode: 'default', revision: 1 }, tasks: [task], active_task: task, run_status: 'running' }
      const payloads: Record<string, unknown> = {
        'chat.history': chatHistoryPayload(),
        'sessions.messages.subscribe': sessionMessagesSubscribePayload(SESSION, metadata),
        'sessions.messages.hydrate': sessionMessagesHydratePayload(SESSION, metadata),
        'sessions.messages.snapshot': sessionMessagesSnapshotPayload(SESSION),
        'agents.list': { agents: [] }, 'commands.list_for_surface': { commands: [] },
        'config.get': { squilla_router: { enabled: false, tiers: {} }, permissions: {}, skills: {} },
        'models.routing.get': { mode: 'direct' }, 'onboarding.status': { audioConfigured: false },
        'sessions.list': { sessions: [], count: 0, ts: 1_800_000_000, has_more: false },
        'usage.status': { sessions: [] },
      }
      reply(frame.id, payloads[frame.method] ?? {})
    })
  })
  await page.setViewportSize({ width: 390, height: 900 })
  await page.goto(`/control/chat?session=${encodeURIComponent(SESSION)}`)
  const dock = page.locator('[data-task-progress-id="ordinary-task"]')
  await expect(dock.getByText('Progress 0/1')).toBeVisible()
  await dock.locator('summary').click()
  await expect(dock.getByText('Inspect files')).toBeVisible()
  await expect(page.locator('.plan-card, .plan-run, .goal-ribbon')).toHaveCount(0)

  progress = { revision: 2, explanation: 'Files inspected.', steps: [{ step: 'Inspect files', status: 'completed' }] }
  const event = (epoch: number, value = progress) => socket!.send(JSON.stringify({
    type: 'event', event: 'session.event.progress', payload: { session_key: SESSION, epoch, task_id: 'ordinary-task', progress: value },
  }))
  event(1)
  await expect(dock.getByText('Progress 1/1')).toBeVisible()
  event(0, { revision: 20, explanation: 'Old generation', steps: [{ step: 'Stale', status: 'in_progress' }] })
  await page.reload()
  await expect(dock.getByText('Progress 1/1')).toBeVisible()
  await dock.locator('summary').click()
  await expect(dock.getByText('Files inspected.')).toBeVisible()
  await expect(dock.getByText('Stale')).toHaveCount(0)
  const bounds = await dock.boundingBox()
  expect(bounds!.x).toBeGreaterThanOrEqual(0)
  expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(390)
})
