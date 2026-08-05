import { describe, expect, it, vi } from 'vitest'
import { nextTick, ref } from 'vue'
import { useChatGoals } from './useChatGoals'

function harness() {
  const handlers = new Map<string, (...args: unknown[]) => void>()
  const rpc = {
    call: vi.fn().mockResolvedValue({ goal: null }),
    on: vi.fn((event: string, handler: (...args: unknown[]) => void) => {
      handlers.set(event, handler)
      return () => handlers.delete(event)
    }),
  }
  const sessionKey = ref('agent:main:webchat:test')
  const notify = vi.fn()
  const ensureSessionKey = vi.fn(async () => sessionKey.value)
  const api = useChatGoals({ rpc, sessionKey, ensureSessionKey, notify })
  return { api, rpc, sessionKey, notify, handlers, ensureSessionKey }
}

function goalPayload(status = 'running', extra: Record<string, unknown> = {}) {
  return {
    goalId: 'g1',
    sessionKey: 'agent:main:webchat:test',
    goalText: 'Refactor the module',
    status,
    turns: 2,
    startedAt: Date.now() - 5000,
    ...extra,
  }
}

describe('useChatGoals', () => {
  it('arms and disarms the composer draft', () => {
    const { api } = harness()
    expect(api.draftArmed.value).toBe(false)
    api.arm()
    expect(api.draftArmed.value).toBe(true)
    api.disarm()
    expect(api.draftArmed.value).toBe(false)
  })

  it('starts a goal, observes it, and surfaces the active snapshot', async () => {
    const { api, rpc } = harness()
    const callOrder: string[] = []
    rpc.call.mockImplementation(async (method: string) => {
      callOrder.push(method)
      if (method === 'goals.status') return { goal: goalPayload() }
      return {}
    })

    const started = await api.startGoal('Refactor the module')

    expect(started).toBe(true)
    expect(rpc.call).toHaveBeenCalledWith('goals.set', {
      sessionKey: 'agent:main:webchat:test',
      message: 'Refactor the module',
    })
    expect(rpc.call).toHaveBeenCalledWith('goals.observe', {
      sessionKey: 'agent:main:webchat:test',
      watch: true,
    })
    expect(callOrder.indexOf('goals.observe')).toBeLessThan(callOrder.indexOf('goals.set'))
    expect(api.activeGoal.value?.status).toBe('running')
    expect(api.activeGoal.value?.goalText).toBe('Refactor the module')
  })

  it('rejects empty goal text', async () => {
    const { api, rpc } = harness()
    const started = await api.startGoal('   ')
    expect(started).toBe(false)
    expect(rpc.call).not.toHaveBeenCalledWith('goals.set', expect.anything())
  })

  it('refreshes from plan_run events emitted by the goal driver', async () => {
    const { api, rpc, handlers } = harness()
    rpc.call.mockImplementation(async (method: string) => {
      if (method === 'goals.status') return { goal: goalPayload() }
      return {}
    })
    await api.startGoal('Refactor the module')
    expect(api.activeGoal.value?.turns).toBe(2)

    rpc.call.mockImplementation(async (method: string) => {
      if (method === 'goals.status') return { goal: goalPayload('running', { turns: 3 }) }
      return {}
    })
    handlers.get('session.event.goal_run')?.({
      sessionKey: 'agent:main:webchat:test',
      plan_run: { driverKind: 'goal', driverId: 'g1', status: 'paused' },
    })
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(api.activeGoal.value?.turns).toBe(3)
  })

  it('ignores plan_run events from non-goal runs', async () => {
    const { api, rpc, handlers } = harness()
    rpc.call.mockImplementation(async (method: string) => {
      if (method === 'goals.status') return { goal: goalPayload() }
      return {}
    })
    await api.startGoal('Refactor the module')

    rpc.call.mockImplementation(async (method: string) => {
      if (method === 'goals.status') return { goal: goalPayload('running', { turns: 9 }) }
      return {}
    })
    handlers.get('session.event.plan_run')?.({
      sessionKey: 'agent:main:webchat:test',
      plan_run: { driverKind: 'manual', status: 'running' },
    })
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(api.activeGoal.value?.turns).toBe(2)
  })

  it('pauses, resumes and clears via RPC then refreshes', async () => {
    const { api, rpc } = harness()
    rpc.call.mockImplementation(async (method: string) => {
      if (method === 'goals.status') return { goal: goalPayload('paused') }
      return {}
    })
    await api.startGoal('Refactor the module')

    await api.pause()
    expect(rpc.call).toHaveBeenCalledWith('goals.pause', { sessionKey: 'agent:main:webchat:test' })
    expect(api.activeGoal.value?.status).toBe('paused')

    rpc.call.mockImplementation(async (method: string) => {
      if (method === 'goals.status') return { goal: goalPayload('running') }
      return {}
    })
    await api.resume()
    expect(rpc.call).toHaveBeenCalledWith('goals.resume', { sessionKey: 'agent:main:webchat:test' })
    expect(api.activeGoal.value?.status).toBe('running')

    rpc.call.mockImplementation(async () => ({ goal: null }))
    await api.clear()
    expect(rpc.call).toHaveBeenCalledWith('goals.clear', { sessionKey: 'agent:main:webchat:test' })
    expect(api.activeGoal.value).toBe(null)
  })

  it('materializes a session on the draft landing before starting a goal', async () => {
    const handlers = new Map<string, (...args: unknown[]) => void>()
    const rpc = {
      call: vi.fn().mockResolvedValue({ goal: null }),
      on: vi.fn((event: string, handler: (...args: unknown[]) => void) => {
        handlers.set(event, handler)
        return () => handlers.delete(event)
      }),
    }
    const sessionKey = ref('')
    const ensureSessionKey = vi.fn(async () => {
      sessionKey.value = 'agent:main:webchat:new1'
      return sessionKey.value
    })
    const api = useChatGoals({ rpc, sessionKey, ensureSessionKey })

    rpc.call.mockImplementation(async (method: string) => {
      if (method === 'goals.status') return { goal: goalPayload('running').sessionKey ? { ...goalPayload('running'), sessionKey: 'agent:main:webchat:new1' } : goalPayload('running') }
      return {}
    })
    const started = await api.startGoal('Do the thing')
    expect(started).toBe(true)
    expect(ensureSessionKey).toHaveBeenCalledTimes(1)
    expect(rpc.call).toHaveBeenCalledWith('goals.set', {
      sessionKey: 'agent:main:webchat:new1',
      message: 'Do the thing',
    })
  })

  it('clears the draft and goal when the session changes', async () => {
    const { api, sessionKey, rpc } = harness()
    rpc.call.mockImplementation(async (method: string) => {
      if (method === 'goals.status') return { goal: goalPayload() }
      return {}
    })
    api.arm()
    await api.startGoal('Refactor the module')
    expect(api.draftArmed.value).toBe(true)

    sessionKey.value = 'agent:main:webchat:other'
    await nextTick()
    expect(api.draftArmed.value).toBe(false)
  })
})
