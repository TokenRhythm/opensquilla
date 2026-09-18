import { describe, expect, it } from 'vitest'
import { createV4GoalCenter } from './goalCenterV4'
import { projectGoalSnapshot } from './goalSnapshotProjection'

function transport(response: unknown, supported = true) {
  const requests: Array<{ method: string; params?: Record<string, unknown> }> = []
  return {
    requests,
    request: async <T>(method: string, params?: Record<string, unknown>) => {
      requests.push({ method, params })
      return response as T
    },
    supports: () => supported,
  }
}

describe('createV4GoalCenter', () => {
  it('preserves explicit no-budget and background options and projects total budget accounting', async () => {
    const source = transport({ accepted: true, goal: { status: 'active', tokenBudget: null, budgetTokensUsed: 321, usageCoverage: 'complete', executionPolicy: 'background' } })
    const center = createV4GoalCenter(source)
    const result = await center.edit({ sessionKey: 'agent:demo', expectedGoalId: 'g1', expectedStateRevision: 2, clientRequestId: 'budget-edit', objective: 'ship', tokenBudget: null, executionPolicy: 'background' })
    expect(source.requests[0]?.params).toMatchObject({ tokenBudget: null, executionPolicy: 'background' })
    expect(result.goal).toMatchObject({ tokenBudget: null, budgetTokensUsed: 321, usageCoverage: 'complete', executionPolicy: 'background' })
    await center.edit({ sessionKey: 'agent:demo', expectedGoalId: 'g1', expectedStateRevision: 3, clientRequestId: 'objective-only', objective: 'ship again' })
    expect(source.requests[1]?.params).not.toHaveProperty('tokenBudget')
    expect(source.requests[1]?.params).not.toHaveProperty('executionPolicy')
  })

  it('keeps goal-mode availability behind the semantic module boundary', () => {
    const source = {
      supports: (method: string) => method === 'goals.set' || method === 'goals.capabilities',
      request: async <T>(_method: string, _params?: Record<string, unknown>) => undefined as T,
    }
    const center = createV4GoalCenter(source)
    expect(center.available('goal-mode')).toBe(true)
  })

  it('validates and projects process-scoped goal capabilities', async () => {
    const source = transport({
      supported: true,
      executionEnabled: false,
      maxTurns: 50,
      runtimeBudgetSeconds: 3600,
      methods: ['goals.status'],
      future: { version: 2 },
    })
    const center = createV4GoalCenter(source)

    await expect(center.capabilities()).resolves.toEqual({
      supported: true,
      executionEnabled: false,
      maxTurns: 50,
      runtimeBudgetSeconds: 3600,
      methods: ['goals.status'],
      tokenBudgetSupported: false,
      backgroundExecutionSupported: false,
    })
    expect(source.requests[0]).toEqual({
      method: 'goals.capabilities',
      params: undefined,
    })
  })

  it('projects explicit current-server budget and background capabilities', async () => {
    const center = createV4GoalCenter(transport({
      supported: true, executionEnabled: true, maxTurns: 50, runtimeBudgetSeconds: 3600,
      methods: ['goals.set', 'goals.edit'],
      tokenBudgetSupported: true, backgroundExecutionSupported: true,
    }))
    await expect(center.capabilities()).resolves.toMatchObject({
      tokenBudgetSupported: true, backgroundExecutionSupported: true,
    })
  })

  it.each([undefined, 'future_coverage'])('does not invent historical accounting for coverage %s', usageCoverage => {
    expect(projectGoalSnapshot({ status: 'active', usageCoverage })?.usageCoverage).toBeUndefined()
    expect(projectGoalSnapshot({ status: 'active', usage_coverage: usageCoverage })?.usageCoverage).toBeUndefined()
  })

  it('rejects explicit unknown coverage in a validated status response', async () => {
    const center = createV4GoalCenter(transport({
      sessionKey: 'agent:demo', sessionId: 's1', epoch: 1,
      goal: { status: 'active', usageCoverage: 'future_coverage' },
    }))
    await expect(center.status('agent:demo')).rejects.toThrow('invalid response')
  })

  it('rejects an incomplete capability response at the adapter boundary', async () => {
    const source = transport({ supported: true, executionEnabled: true })
    await expect(createV4GoalCenter(source).capabilities()).rejects.toThrow('invalid response')
  })

  it('validates and projects goals.status without exposing wire aliases', async () => {
    const source = transport({ session_key: 'agent:demo', session_id: 's1', epoch: 2, goal: { status: 'active', goal_id: 'g1', objective: 'ship', session_key: 'agent:demo', session_id: 's1', epoch: 2, state_revision: 3, objective_revision: 1, progress_revision: 0, active_task_id: null, source_user_message_id: 'm1', turns_started: 2, usage: { total_tokens: 4 } } })
    const center = createV4GoalCenter(source)
    await expect(center.status('agent:demo')).resolves.toMatchObject({ sessionKey: 'agent:demo', goal: { goalId: 'g1', objective: 'ship', stateRevision: 3, activeTaskId: null, sourceMessageId: 'm1', turnsStarted: 2, usage: { total_tokens: 4 } } })
    expect(source.requests[0]).toMatchObject({ method: 'goals.status', params: { sessionKey: 'agent:demo' } })
  })

  it('keeps goals.set idempotency fields and maps the result to domain terms', async () => {
    const source = transport({ session_key: 'agent:demo', accepted: true, replayed: false, goal: { status: 'active', goalId: 'g1' }, task_id: 't1' })
    const center = createV4GoalCenter(source)
    await expect(center.set({ sessionKey: 'agent:demo', objective: 'ship', clientRequestId: '550e8400-e29b-41d4-a716-446655440000', clientMessageId: '550e8400-e29b-41d4-a716-446655440001' })).resolves.toMatchObject({ sessionKey: 'agent:demo', accepted: true, taskId: 't1', goal: { goalId: 'g1' } })
    expect(source.requests[0]).toMatchObject({ method: 'goals.set', params: { clientRequestId: '550e8400-e29b-41d4-a716-446655440000', clientMessageId: '550e8400-e29b-41d4-a716-446655440001' } })
  })

  it('maps legacy RPC errors to stable domain codes', async () => {
    const source = { ...transport(null), request: async () => { throw Object.assign(new Error('stale'), { code: 'GOAL_ACTIVE' }) } }
    await expect(createV4GoalCenter(source).set({ sessionKey: 'agent:demo', objective: 'ship', clientRequestId: '550e8400-e29b-41d4-a716-446655440000', clientMessageId: '550e8400-e29b-41d4-a716-446655440001' })).rejects.toMatchObject({ name: 'GoalCenterError', code: 'conflict', retryable: true })
  })

  it('rejects structurally incomplete query and mutation responses', async () => {
    const statusSource = transport({ sessionKey: 'agent:demo', sessionId: 's1', epoch: 1 })
    await expect(createV4GoalCenter(statusSource).status('agent:demo')).rejects.toThrow('invalid response')
    const setSource = transport({ accepted: true })
    await expect(createV4GoalCenter(setSource).set({ sessionKey: 'agent:demo', objective: 'ship', clientRequestId: '550e8400-e29b-41d4-a716-446655440000', clientMessageId: '550e8400-e29b-41d4-a716-446655440001' })).rejects.toThrow('acceptance outcome')
  })

  it.each(['GOAL_BUSY', 'SESSION_GENERATION_CHANGED', 'PLAN_MODE_ACTIVE', 'PLAN_RUN_ACTIVE', 'IDEMPOTENCY_CONFLICT', 'GOAL_NOT_FOUND', 'GOAL_NOT_RESUMABLE'])('preserves conflict semantics for %s', async code => {
    const source = { ...transport(null), request: async () => { throw Object.assign(new Error(code), { code }) } }
    await expect(createV4GoalCenter(source).set({ sessionKey: 'agent:demo', objective: 'ship', clientRequestId: '550e8400-e29b-41d4-a716-446655440000', clientMessageId: '550e8400-e29b-41d4-a716-446655440001' })).rejects.toMatchObject({ code: 'conflict', retryable: true })
  })

  it('maps disabled goal execution to unsupported without losing details', async () => {
    const source = { ...transport(null), request: async () => { throw Object.assign(new Error('disabled'), { code: 'GOAL_EXECUTION_DISABLED', data: { details: { reason: 'config' } } }) } }
    await expect(createV4GoalCenter(source).set({ sessionKey: 'agent:demo', objective: 'ship', clientRequestId: '550e8400-e29b-41d4-a716-446655440000', clientMessageId: '550e8400-e29b-41d4-a716-446655440001' })).rejects.toMatchObject({ code: 'unsupported', details: { reason: 'config' } })
  })
})
