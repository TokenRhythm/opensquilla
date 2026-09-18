import { expect, test, type Page } from '@playwright/test'
import { helloOkResponse } from './support/gateway-fixture'
import {
  chatHistoryPayload, sessionMessagesHydratePayload,
  sessionMessagesSnapshotPayload, sessionMessagesSubscribePayload,
} from './support/session-read-fixtures'

const SESSION = 'agent:main:webchat:presentation-e2e'
const REVISION = 'presentation-revision'

async function installGateway(page: Page, status: 'queued' | 'running') {
  let dismissed = false
  let presentationRevision = 0
  let cancelled = false
  let settleCancellation: (() => void) | undefined
  const mutations: Array<{ method: string; params: Record<string, unknown> }> = []
  const plan = {
    revisionId: REVISION, planId: 'presentation-plan', generation: 1,
    title: 'A synthetic proposal', markdown: 'Keep this proposal in history.',
    current: true, steps: [{ stepId: 'inspect', title: 'Inspect' }],
  }
  const run = () => ({
    runId: 'presentation-run', planRevisionId: REVISION,
    status: cancelled ? 'cancelled' : status, stateRevision: cancelled ? 2 : 1,
    activeTaskId: cancelled ? null : 'presentation-task',
    steps: [{ stepId: 'inspect', title: 'Inspect', status: 'in_progress' }],
  })
  const presentations = () => presentationRevision
    ? [{ revisionId: REVISION, dismissed, stateRevision: presentationRevision }]
    : []
  await page.addInitScript(() => localStorage.setItem('opensquilla-locale', 'en'))
  await page.route('**/api/approvals', route => route.fulfill({ json: { mode: 'prompt', pending: [] } }))
  await page.route('**/api/elevated-mode', route => route.fulfill({ json: { enabled: false } }))
  await page.route('**/api/system/update', route => route.fulfill({ json: {} }))
  await page.routeWebSocket(/\/ws$/, ws => {
    const reply = (id: string, payload: unknown) => ws.send(JSON.stringify({ type: 'res', id, ok: true, payload }))
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(message => {
      const frame = JSON.parse(String(message))
      if (frame.type !== 'req') return
      if (frame.method === 'connect') {
        ws.send(helloOkResponse({ features: { methods: [
          'plans.setMode', 'plans.capabilities', 'plans.implement', 'plans.revise',
          'plans.cancelRun', 'plans.setPresentation',
        ], events: [] }, auth: { principal: { isOwner: true } } }))
        return
      }
      if (frame.method === 'plans.setPresentation') {
        mutations.push({ method: frame.method, params: frame.params })
        expect(frame.params.expectedEpoch).toBe(1)
        expect(frame.params.expectedPresentationRevision).toBe(presentationRevision)
        dismissed = frame.params.dismissed
        presentationRevision += 1
        reply(frame.id, { sessionKey: SESSION, epoch: 1, accepted: true, planPresentations: presentations() })
        return
      }
      if (frame.method === 'plans.cancelRun') {
        mutations.push({ method: frame.method, params: frame.params })
        // Keep cancellation pending until the test delivers the owning task's
        // terminal event and the authoritative cancellation response.
        settleCancellation = () => {
          cancelled = true
          ws.send(JSON.stringify({ type: 'event', event: 'task.cancelled', payload: {
            key: SESSION, session_key: SESSION, task_id: 'presentation-task',
            epoch: 1, reason: 'aborted',
          } }))
          reply(frame.id, { sessionKey: SESSION, planRun: run() })
        }
        return
      }
      const metadata = {
        epoch: 1, collaboration: { mode: 'default', revision: 1 },
        currentPlan: plan, activePlanRun: run(), planPresentations: presentations(),
        active_task: cancelled ? null : { task_id: 'presentation-task', status },
        run_status: cancelled ? 'idle' : status,
      }
      const payloads: Record<string, unknown> = {
        'chat.history': chatHistoryPayload(),
        'sessions.messages.subscribe': sessionMessagesSubscribePayload(SESSION, metadata),
        'sessions.messages.hydrate': sessionMessagesHydratePayload(SESSION, metadata),
        'sessions.messages.snapshot': sessionMessagesSnapshotPayload(SESSION),
        'agents.list': { agents: [] },
        'commands.list_for_surface': { commands: [] },
        'config.get': { squilla_router: { enabled: false, tiers: {} }, permissions: {}, skills: {} },
        'models.routing.get': { mode: 'direct' },
        'onboarding.status': { audioConfigured: false },
        'sessions.list': { sessions: [], count: 0, ts: 1_800_000_000, has_more: false },
        'usage.status': { sessions: [] },
      }
      reply(frame.id, payloads[frame.method] ?? {})
    })
  })
  return {
    mutations,
    settleCancellation() {
      if (!settleCancellation) throw new Error('No cancellation request is pending')
      const settle = settleCancellation
      settleCancellation = undefined
      settle()
    },
  }
}

for (const width of [1280, 390]) {
  for (const status of ['queued', 'running'] as const) {
    test(`hidden plan keeps Stop reachable through ${status} cancellation and refresh at ${width}px`, { tag: '@plan-goal-runtime' }, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 })
      const gateway = await installGateway(page, status)
      const { mutations } = gateway
      await page.goto(`/control/chat?session=${encodeURIComponent(SESSION)}`)
      const card = page.locator(`[data-plan-revision-id="${REVISION}"]`)
      const cancel = page.locator('.plan-run__cancel')
      await expect(card).toBeVisible()
      await expect(cancel).toBeVisible()
      await expect(page.locator('.plan-run__popover')).toHaveCount(0)
      await card.getByRole('button', { name: 'Hide plan', exact: true }).click()
      await expect(card.getByRole('button', { name: 'Show plan', exact: true })).toBeVisible()
      await expect(card.locator('.plan-card__body')).toHaveCount(0)
      expect(mutations.map(item => item.method)).toEqual(['plans.setPresentation'])
      await page.reload()
      const restore = card.getByRole('button', { name: 'Show plan', exact: true })
      await expect(restore).toBeVisible()
      // Stop remains outside the hidden proposal, including after reload.
      await expect(cancel).toBeVisible()
      const bounds = await cancel.boundingBox()
      expect(bounds?.height).toBeGreaterThanOrEqual(44)
      expect((bounds?.x ?? -1) + (bounds?.width ?? 0)).toBeLessThanOrEqual(width)
      await cancel.click()
      await expect.poll(() => mutations.filter(item => item.method === 'plans.cancelRun').length).toBe(1)
      await expect(cancel).toBeDisabled()
      await expect(page.locator(`.plan-run--${status}`)).toBeVisible()
      await expect(restore).toBeVisible()
      await expect(page.locator('.plan-run--cancelled')).toHaveCount(0)

      gateway.settleCancellation()
      await expect(page.locator('.plan-run--cancelled')).toBeVisible()
      await expect(cancel).toHaveCount(0)
      await expect(restore).toBeVisible()

      // Stopping execution cannot discard the historical proposal or its
      // presentation preference. Restoration still works after fresh hydration.
      await page.reload()
      await expect(restore).toBeVisible()
      await expect(cancel).toHaveCount(0)
      await restore.focus()
      await page.keyboard.press('Enter')
      await expect(card.getByText('Keep this proposal in history.')).toBeVisible()
      expect(mutations.map(item => item.method)).toEqual([
        'plans.setPresentation', 'plans.cancelRun', 'plans.setPresentation',
      ])
      expect(mutations[1]?.params).toMatchObject({ runId: 'presentation-run', expectedStateRevision: 1 })
      expect(mutations[2]?.params.expectedPresentationRevision).toBe(1)
    })
  }
}
