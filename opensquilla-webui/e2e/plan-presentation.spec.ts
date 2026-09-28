import { expect, test, type Page, type WebSocketRoute } from '@playwright/test'
import { helloOkResponse } from './support/gateway-fixture'
import {
  chatHistoryPayload, sessionMessagesHydratePayload,
  sessionMessagesSnapshotPayload, sessionMessagesSubscribePayload,
} from './support/session-read-fixtures'

const SESSION = 'agent:main:webchat:presentation-e2e'
const REVISION = 'presentation-revision'
const TASK = 'presentation-task'
const TERMINAL_HISTORY_TEXT = 'The implementation turn has ended.'

type RunStep = { stepId: string; title: string; status: 'pending' | 'in_progress' | 'completed' }

async function installGateway(
  page: Page,
  status: 'queued' | 'running',
  steps: RunStep[] = [{ stepId: 'inspect', title: 'Inspect', status: 'in_progress' }],
) {
  let dismissed = false
  let presentationRevision = 0
  let cancelled = false
  let completed = false
  let socket: WebSocketRoute | undefined
  let connectionCount = 0
  let metadataReadCount = 0
  let settleCancellation: (() => void) | undefined
  const mutations: Array<{ method: string; params: Record<string, unknown> }> = []
  const plan = {
    revisionId: REVISION, planId: 'presentation-plan', generation: 1,
    title: 'A synthetic proposal', markdown: 'Keep this proposal in history.',
    current: true, steps: steps.map(({ stepId, title }) => ({ stepId, title })),
  }
  const run = () => ({
    runId: 'presentation-run', planRevisionId: REVISION,
    status: completed ? 'completed' : cancelled ? 'cancelled' : status,
    stateRevision: cancelled || completed ? 2 : 1,
    activeTaskId: cancelled || completed ? null : TASK,
    steps,
  })
  const emit = (event: string, payload: Record<string, unknown>) => {
    if (!socket) throw new Error('No synthetic gateway connection is open')
    socket.send(JSON.stringify({ type: 'event', event, payload: {
      key: SESSION, session_key: SESSION, epoch: 1, ...payload,
    } }))
  }
  const presentations = () => presentationRevision
    ? [{ revisionId: REVISION, dismissed, stateRevision: presentationRevision }]
    : []
  await page.addInitScript(() => localStorage.setItem('opensquilla-locale', 'en'))
  await page.route('**/api/approvals', route => route.fulfill({ json: { mode: 'prompt', pending: [] } }))
  await page.route('**/api/elevated-mode', route => route.fulfill({ json: { enabled: false } }))
  await page.route('**/api/system/update', route => route.fulfill({ json: {} }))
  await page.routeWebSocket(/\/ws$/, ws => {
    socket = ws
    connectionCount += 1
    const reply = (id: string, payload: unknown) => ws.send(JSON.stringify({ type: 'res', id, ok: true, payload }))
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(message => {
      const frame = JSON.parse(String(message))
      if (frame.type !== 'req') return
      if (frame.method === 'connect') {
        ws.send(helloOkResponse({ features: { methods: [
          'plans.setMode', 'plans.capabilities', 'plans.implement', 'plans.revise',
          'plans.cancelRun', 'plans.setPresentation',
        ], events: ['session.event.plan_run'] }, auth: { principal: { isOwner: true } } }))
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
            key: SESSION, session_key: SESSION, task_id: TASK,
            epoch: 1, reason: 'aborted',
          } }))
          reply(frame.id, { sessionKey: SESSION, planRun: run() })
        }
        return
      }
      if (frame.method === 'sessions.messages.subscribe' || frame.method === 'sessions.messages.hydrate') {
        metadataReadCount += 1
      }
      const terminal = cancelled || completed
      const task = { task_id: TASK, status: completed ? 'succeeded' : cancelled ? 'cancelled' : status }
      const metadata = {
        epoch: 1, collaboration: { mode: 'default', revision: 1 },
        currentPlan: plan, activePlanRun: terminal ? null : run(), planPresentations: presentations(),
        tasks: [task], active_task: terminal ? null : task, last_task: terminal ? task : null,
        run_status: terminal ? 'idle' : status,
      }
      const payloads: Record<string, unknown> = {
        'chat.history': chatHistoryPayload(completed ? [{
          id: 'presentation-terminal-history', role: 'assistant', turn_id: TASK,
          text: TERMINAL_HISTORY_TEXT,
        }] : []),
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
    get connectionCount() { return connectionCount },
    get metadataReadCount() { return metadataReadCount },
    finish(order: 'task-first' | 'run-first') {
      completed = true
      const taskEvent = () => emit('task.succeeded', { task_id: TASK, terminal_reason: 'completed' })
      const runEvent = () => emit('session.event.plan_run', { plan_run: run() })
      for (const event of order === 'task-first' ? [taskEvent, runEvent] : [runEvent, taskEvent]) event()
    },
    emitStaleRunning() {
      emit('session.event.plan_run', { plan_run: {
        ...run(), status: 'running', stateRevision: 1, activeTaskId: TASK,
      } })
    },
    disconnect() {
      if (!socket) throw new Error('No synthetic gateway connection is open')
      socket.close({ code: 1012, reason: 'Synthetic transport restart' })
    },
    settleCancellation() {
      if (!settleCancellation) throw new Error('No cancellation request is pending')
      const settle = settleCancellation
      settleCancellation = undefined
      settle()
    },
  }
}

for (const completedCount of [0, 1, 4]) {
  for (const order of ['task-first', 'run-first'] as const) {
    test(`Plan turn ends neutrally at ${completedCount}/4 and auto-hides after ${order} settlement`, { tag: '@plan-goal-runtime' }, async ({ page }) => {
      const steps: RunStep[] = Array.from({ length: 4 }, (_, index) => ({
        stepId: `work-${index}`, title: `Work ${index + 1}`,
        status: index < completedCount ? 'completed' : 'pending',
      }))
      const gateway = await installGateway(page, 'running', steps)
      await page.goto(`/control/chat?session=${encodeURIComponent(SESSION)}`)
      const dock = page.locator('[data-plan-run-id="presentation-run"]')
      await expect(dock).toBeVisible()
      await dock.locator('.plan-run__summary').click()
      await expect(dock.locator('.plan-run__step--completed')).toHaveCount(completedCount)
      await expect(dock.locator('.plan-run__step--pending')).toHaveCount(4 - completedCount)
      await page.reload()
      await expect(dock).toBeVisible()

      gateway.finish(order)
      await expect(dock.locator('.plan-run__static')).toContainText('Execution ended')
      await expect(dock.locator('.plan-run__static-status')).toHaveText(`${completedCount}/4`)
      await expect(dock.locator('.execution-todo-marker--completed')).toHaveCount(0)
      await expect(dock.locator('.execution-todo-marker--pending')).toHaveCount(1)
      await expect(page.getByRole('button', { name: 'End plan execution', exact: true })).toHaveCount(0)
      gateway.emitStaleRunning()
      await expect(dock.locator('.plan-run__static')).toContainText('Execution ended')
      await expect(dock).toHaveCount(0)

      const priorConnections = gateway.connectionCount
      const priorMetadataReads = gateway.metadataReadCount
      gateway.disconnect()
      await expect.poll(() => gateway.connectionCount).toBeGreaterThan(priorConnections)
      await expect.poll(() => gateway.metadataReadCount).toBeGreaterThan(priorMetadataReads)
      await expect(page.getByText(TERMINAL_HISTORY_TEXT, { exact: true })).toBeVisible()
      await expect(dock).toHaveCount(0)
      await page.reload()
      await expect(page.getByText(TERMINAL_HISTORY_TEXT, { exact: true })).toBeVisible()
      await expect(dock).toHaveCount(0)
    })
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
      const cancel = page.getByRole('button', { name: 'End plan execution', exact: true })
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
      // Visibility precedes the composer's scale-in transition completing.
      await expect.poll(async () => (await cancel.boundingBox())?.height ?? 0)
        .toBeGreaterThanOrEqual(44)
      const bounds = await cancel.boundingBox()
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
