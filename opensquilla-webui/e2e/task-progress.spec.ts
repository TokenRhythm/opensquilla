import { expect, test, type Locator, type Page, type WebSocketRoute } from '@playwright/test'
import { helloOkResponse } from './support/gateway-fixture'
import {
  chatHistoryPayload, sessionMessagesHydratePayload,
  sessionMessagesSnapshotPayload, sessionMessagesSubscribePayload,
} from './support/session-read-fixtures'

const SESSION = 'agent:main:webchat:ordinary-progress'
const TASK = 'ordinary-task'
const TERMINAL_HISTORY_TEXT = 'Synthetic ordinary task history restored.'
type TerminalStatus = 'succeeded' | 'failed' | 'cancelled'
type Progress = {
  revision: number
  explanation: string
  steps: Array<{ step: string; status: 'pending' | 'in_progress' | 'completed' }>
}

async function installGateway(page: Page) {
  const cancellations: Array<{ method: string; params: Record<string, unknown> }> = []
  let progress: Progress = {
    revision: 1,
    explanation: 'Check the ordinary task.',
    steps: [
      { step: 'Inspect files', status: 'in_progress' },
      { step: 'Create the page', status: 'pending' },
      { step: 'Preview the page', status: 'pending' },
    ],
  }
  let taskStatus: 'running' | TerminalStatus = 'running'
  let socket: WebSocketRoute | undefined
  const metadata = () => {
    const task = { task_id: TASK, status: taskStatus, progress }
    return {
      epoch: 1, collaboration: { mode: 'default', revision: 1 }, tasks: [task],
      active_task: taskStatus === 'running' ? task : null,
      last_task: taskStatus === 'running' ? null : task,
      run_status: taskStatus === 'running' ? 'running' : 'idle',
    }
  }
  const emit = (event: string, payload: Record<string, unknown>) => {
    if (!socket) throw new Error('No synthetic gateway connection is open')
    socket.send(JSON.stringify({
      type: 'event', event,
      payload: { key: SESSION, session_key: SESSION, epoch: 1, task_id: TASK, ...payload },
    }))
  }
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
      if (frame.method === 'chat.abort' || frame.method === 'plans.cancelRun') {
        cancellations.push({ method: frame.method, params: frame.params })
        reply(frame.id, { aborted: true, key: SESSION })
        return
      }
      const payloads: Record<string, unknown> = {
        'chat.history': chatHistoryPayload(taskStatus === 'running' ? [] : [{
          id: 'ordinary-task-history', role: 'assistant', turn_id: TASK,
          text: TERMINAL_HISTORY_TEXT,
        }]),
        'sessions.messages.subscribe': sessionMessagesSubscribePayload(SESSION, metadata()),
        'sessions.messages.hydrate': sessionMessagesHydratePayload(SESSION, metadata()),
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
  return {
    cancellations,
    emitProgress(value: Progress, epoch = 1) {
      emit('session.event.progress', { progress: value, epoch })
    },
    updateProgress(value: Progress) {
      progress = value
      emit('session.event.progress', { progress })
    },
    finish(status: TerminalStatus) {
      taskStatus = status
      emit(`task.${status}`, {
        status,
        terminal_reason: status === 'cancelled' ? 'user_abort' : status,
        terminal_message: status === 'failed' ? 'Synthetic provider failure.' : undefined,
      })
    },
  }
}

async function expectInsideViewport(locator: Locator, width: number) {
  const bounds = await locator.boundingBox()
  expect(bounds).not.toBeNull()
  expect(bounds!.x).toBeGreaterThanOrEqual(0)
  expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(width)
}

for (const width of [1280, 390]) {
  test(`Default task uses the Plan execution ribbon and survives active refresh at ${width}px`, { tag: '@plan-goal-runtime' }, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 })
    const gateway = await installGateway(page)
    await page.goto(`/control/chat?session=${encodeURIComponent(SESSION)}`)
    const dock = page.locator(`[data-task-progress-id="${TASK}"]`)
    const summary = dock.locator('button.plan-run__summary')
    const popover = dock.locator('.plan-run__popover')
    await expect(dock).toHaveClass(/plan-run-dock/)
    await expect(summary).toContainText('Step 1/3')
    await expect(dock.getByRole('region')).toHaveAttribute('aria-label', 'Progress 0/3')
    await expect(summary).toHaveAttribute('aria-label', 'Progress 0/3: Inspect files')
    await expect(summary).toHaveAttribute('aria-expanded', 'false')
    await expect(popover).toHaveCount(0)
    await expect(page.locator('.plan-card, [data-plan-run-id], .goal-ribbon')).toHaveCount(0)
    await summary.click()
    await expect(summary).toHaveAttribute('aria-expanded', 'true')
    await expect(popover.locator('.plan-run__step')).toHaveCount(3)
    await expect(popover.getByText('Inspect files', { exact: true })).toBeVisible()
    await expect(popover.getByText('Check the ordinary task.', { exact: true })).toBeVisible()
    await expectInsideViewport(dock, width)
    await expectInsideViewport(popover, width)
    const expandedScreenshot = testInfo.outputPath(`ordinary-progress-expanded-${width}.png`)
    await page.screenshot({ path: expandedScreenshot })
    await testInfo.attach(`ordinary-progress-expanded-${width}`, {
      path: expandedScreenshot, contentType: 'image/png',
    })
    await page.keyboard.press('Escape')
    await expect(popover).toHaveCount(0)

    gateway.updateProgress({
      revision: 2, explanation: 'Files inspected.',
      steps: [
        { step: 'Inspect files', status: 'completed' },
        { step: 'Create the page', status: 'in_progress' },
        { step: 'Preview the page', status: 'pending' },
      ],
    })
    await expect(summary).toContainText('Step 2/3')
    gateway.emitProgress({
      revision: 20, explanation: 'Old generation',
      steps: [{ step: 'Stale', status: 'in_progress' }],
    }, 0)
    await summary.click()
    await expect(popover.getByText('Stale', { exact: true })).toHaveCount(0)
    await expect(summary).toContainText('Step 2/3')

    await page.reload()
    await expect(summary).toContainText('Step 2/3')
    await summary.click()
    await expect(popover.getByText('Files inspected.', { exact: true })).toBeVisible()
    gateway.updateProgress({
      revision: 3, explanation: 'Checks complete; preparing the response.',
      steps: [
        { step: 'Inspect files', status: 'completed' },
        { step: 'Create the page', status: 'completed' },
        { step: 'Preview the page', status: 'completed' },
      ],
    })
    await expect(summary).toContainText('Finishing · 3/3')
    await page.reload()
    await expect(summary).toContainText('Finishing · 3/3')
    gateway.finish('succeeded')
    await expect(dock).toHaveCount(0)
    const settledScreenshot = testInfo.outputPath(`ordinary-progress-settled-${width}.png`)
    await page.screenshot({ path: settledScreenshot })
    await testInfo.attach(`ordinary-progress-settled-${width}`, {
      path: settledScreenshot, contentType: 'image/png',
    })
  })
}

test('Composer Stop stops its ordinary task without a Plan mutation', { tag: '@plan-goal-runtime' }, async ({ page }) => {
  const gateway = await installGateway(page)
  await page.goto(`/control/chat?session=${encodeURIComponent(SESSION)}`)
  const dock = page.locator(`[data-task-progress-id="${TASK}"]`)
  const cancel = page.getByRole('button', { name: 'Stop current response', exact: true })
  await expect(cancel).toBeEnabled()
  await cancel.click()
  await expect.poll(() => gateway.cancellations).toEqual([{
    method: 'chat.abort',
    params: { sessionKey: SESSION, taskId: TASK, source: 'webui_stop', scope: 'task' },
  }])
  gateway.finish('cancelled')
  await expect(dock).toHaveCount(0)
})

for (const status of ['succeeded', 'failed', 'cancelled'] as const) {
  test(`Default progress disappears on ${status} and cannot return from late events or refresh`, { tag: '@plan-goal-runtime' }, async ({ page }) => {
    const gateway = await installGateway(page)
    await page.goto(`/control/chat?session=${encodeURIComponent(SESSION)}`)
    const dock = page.locator(`[data-task-progress-id="${TASK}"]`)
    await expect(dock.locator('.plan-run__summary')).toContainText('Step 1/3')
    gateway.finish(status)
    await expect(dock).toHaveCount(0)

    // An out-of-order progress update must not revive an ended task's dock.
    gateway.updateProgress({
      revision: 50, explanation: 'A delayed update from the finished task.',
      steps: [{ step: 'Late progress', status: 'in_progress' }],
    })
    await expect(dock).toHaveCount(0)
    await page.reload()
    // A connected socket alone can precede session hydration. Require the
    // terminal history response to be rendered before checking for residue.
    await expect(page.getByText(TERMINAL_HISTORY_TEXT, { exact: true })).toBeVisible()
    // Both tasks[] and last_task still carry progress in fresh hydration.
    await expect(dock).toHaveCount(0)
    await expect(page.locator('.plan-run__popover, .plan-card, .goal-ribbon')).toHaveCount(0)
  })
}
