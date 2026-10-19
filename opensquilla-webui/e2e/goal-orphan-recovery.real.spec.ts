import { writeFile } from 'node:fs/promises'
import { expect, test } from './real-gateway.fixture'

const SESSION = 'agent:main:webchat:e2e-goal-orphan-recovery'
const FIRST_OBJECTIVE = 'This accepted Goal must never execute after its activation fails.'
const NEXT_OBJECTIVE = 'Run exactly this new Goal after the old durable admission is released.'

function object(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' ? value as Record<string, unknown> : {}
}

test.use({
  isolatedRealGatewayScenario: 'activation-orphan-clear',
  isolatedRealGatewayInitialSessionKeys: [SESSION],
})

test('Clear preserves an accepted but unstarted task compensation until real SQLite recovery releases admission', async ({
  page, isolatedRealGateway,
}, testInfo) => {
  test.setTimeout(120_000)
  await page.addInitScript(() => localStorage.setItem('opensquilla-locale', 'en'))
  const requests: Array<{ id: string; method: string; sessionKey: unknown }> = []
  const responses: Array<{ method: string; ok: unknown; code: unknown; payload: Record<string, unknown> }> = []
  const rendererErrors: string[] = []
  const observations: Array<{ stage: string; state: Record<string, unknown> }> = []
  let completed = false
  page.on('pageerror', error => rendererErrors.push(error.message))
  page.on('websocket', socket => {
    const methods = new Map<string, string>()
    socket.on('framesent', ({ payload }) => {
      const frame = JSON.parse(String(payload))
      if (frame.type !== 'req' || !String(frame.method).startsWith('goals.')) return
      methods.set(frame.id, frame.method)
      requests.push({ id: frame.id, method: frame.method, sessionKey: frame.params?.sessionKey })
    })
    socket.on('framereceived', ({ payload }) => {
      const frame = JSON.parse(String(payload))
      const method = frame.type === 'res' ? methods.get(frame.id) : undefined
      if (!method) return
      const body = object(frame.payload)
      // Keep only public acceptance coordinates. Do not export continuity tokens.
      responses.push({ method, ok: frame.ok, code: object(frame.error).code,
        payload: { accepted: body.accepted, sessionKey: body.sessionKey, taskId: body.taskId,
          goalId: object(body.goal).goalId } })
    })
  })
  const accepted = (method: string) => responses.filter(response => response.method === method && response.ok === true)
  const snapshot = async (stage: string) => {
    const state = await isolatedRealGateway.readActivationRecovery()
    observations.push({ stage, state })
    return state
  }
  try {
    await page.goto(`${isolatedRealGateway.controlUrl}chat?session=${encodeURIComponent(SESSION)}`)
    const input = page.locator('.chat-textarea')
    const send = page.locator('.chat-send-btn[aria-label="Send"]')
    const ribbon = page.locator('.goal-ribbon')
    const startGoal = async (objective: string) => {
      await page.getByRole('button', { name: 'Add', exact: true }).click()
      await page.getByRole('menuitem', { name: /Goal mode/ }).click()
      await input.fill(objective)
      await expect(send).toBeEnabled()
      await send.click()
    }
    await startGoal(FIRST_OBJECTIVE)
    await expect.poll(() => accepted('goals.set').length).toBe(1)
    await expect(ribbon).toContainText(FIRST_OBJECTIVE)
    const first = accepted('goals.set')[0]!.payload
    expect(first.accepted).toBe(true)
    await expect.poll(async () => {
      const state = await isolatedRealGateway.readActivationRecovery()
      return { task: object(state.failedTask).status, recoveries: state.recoveryRecords,
        reservations: state.runtimeReservations, providerCalls: state.providerCalls }
    }, { timeout: 15_000 }).toEqual({ task: 'queued', recoveries: 1, reservations: 0, providerCalls: 0 })
    const retained = await snapshot('accepted-unstarted-compensation-retained')
    expect(object(retained.failedTask)).toMatchObject({ taskId: first.taskId, startedAt: null,
      admissionOwner: { resident_lease: 'held', settlement: 'required' } })
    expect(retained.activatedTaskIds).toEqual([])

    await ribbon.getByRole('button', { name: 'Goal actions', exact: true }).click()
    await ribbon.getByRole('menuitem', { name: 'Remove goal' }).click()
    await page.getByRole('dialog', { name: 'Remove this goal?' }).getByRole('button', { name: 'Remove goal', exact: true }).click()
    await expect.poll(() => accepted('goals.clear').length).toBe(1)
    await expect(ribbon).toHaveCount(0)
    await expect.poll(async () => (await isolatedRealGateway.readActivationRecovery()).currentGoal).toBeNull()
    const cleared = await snapshot('cleared-while-database-fault-remains')
    expect(cleared.faultReleased).toBe(false)
    expect(object(cleared.failedTask)).toMatchObject({ taskId: first.taskId, status: 'queued', startedAt: null })
    expect(cleared.providerCalls).toBe(0)

    await isolatedRealGateway.releaseActivationFault()
    await expect.poll(async () => {
      const state = await isolatedRealGateway.readActivationRecovery()
      const task = object(state.failedTask)
      return { status: task.status, owner: task.admissionOwner, recoveries: state.recoveryRecords }
    }, { timeout: 15_000 }).toEqual({ status: 'abandoned', owner: {
      resident_lease: 'released', compute_lease: 'not_acquired', settlement: 'settled',
    }, recoveries: 0 })
    const healed = await snapshot('old-task-and-admission-released-with-no-current-goal')
    expect(healed.currentGoal).toBeNull()
    expect(healed.activatedTaskIds).toEqual([])
    expect(healed.providerCalls).toBe(0)

    // No reload, private state mutation, raw mutation RPC, or Goal replacement
    // workaround: the ordinary public UI must admit a new Goal in this session.
    await startGoal(NEXT_OBJECTIVE)
    await expect.poll(() => accepted('goals.set').length).toBe(2)
    const successor = accepted('goals.set')[1]!.payload
    expect(successor.accepted).toBe(true)
    expect(successor.sessionKey).toBe(SESSION)
    expect(successor.taskId).not.toBe(first.taskId)
    expect(successor.goalId).not.toBe(first.goalId)
    await expect(ribbon).toContainText(NEXT_OBJECTIVE)
    await expect.poll(async () => (await isolatedRealGateway.readProviderCalls()).length).toBe(1)
    await expect.poll(async () => (await isolatedRealGateway.readActivationRecovery()).activatedTaskIds).toEqual([successor.taskId])

    // Pause stops future continuation while allowing this one new task to finish.
    await expect(page.getByTestId('goal-lifecycle-action')).toHaveAttribute('data-action', 'pause')
    await page.getByTestId('goal-lifecycle-action').click()
    await expect.poll(() => accepted('goals.pause').length).toBe(1)
    await isolatedRealGateway.releaseFirstTask()
    await expect(page.locator('.msg-ai').filter({ hasText: 'Task one completed after the lifecycle checks.' }))
      .toHaveCount(1, { timeout: 30_000 })
    await expect.poll(async () => object((await isolatedRealGateway.readActivationRecovery()).currentGoal)).toMatchObject({
      goalId: successor.goalId, status: 'paused', activeTaskId: null, turnsStarted: 1, turnsSettled: 1,
    })
    const final = await snapshot('successor-settled-once-and-old-task-stays-abandoned')
    expect(object(final.failedTask)).toMatchObject({ taskId: first.taskId, status: 'abandoned', startedAt: null })
    expect(final.activatedTaskIds).toEqual([successor.taskId])
    expect(final.providerCalls).toBe(1)
    expect(responses.filter(response => response.ok === false)).toEqual([])
    expect(rendererErrors).toEqual([])
    await page.screenshot({ path: testInfo.outputPath('goal-orphan-recovery.png') })
    completed = true
  } finally {
    const body = JSON.stringify({ evidence: 'Chromium, production Gateway and SQLite; deterministic preactivation exception and native SQLite trigger; isolated provider',
      assertionsCompleted: completed, requests, responses, observations, rendererErrors,
      latest: await isolatedRealGateway.readActivationRecovery().catch(() => null),
      providerCalls: await isolatedRealGateway.readProviderCalls() }, null, 2)
    await writeFile(testInfo.outputPath('goal-orphan-recovery.json'), body)
    await testInfo.attach('goal-orphan-recovery', { body, contentType: 'application/json' })
  }
})
