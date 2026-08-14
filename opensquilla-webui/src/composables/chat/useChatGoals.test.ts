import { describe, expect, it, vi } from 'vitest'
import { nextTick, ref } from 'vue'
import {
  goalHasRenderedTerminalAnchor,
  normalizeGoal,
  useChatGoals,
  type GoalContinuityStorage,
} from './useChatGoals'

const SESSION_KEY = 'agent:main:webchat:test'
const SESSION_ID = 'session-1'

class MemoryContinuityStorage implements GoalContinuityStorage {
  private readonly values = new Map<string, string>()

  get length() {
    return this.values.size
  }

  key(index: number) {
    return [...this.values.keys()][index] ?? null
  }

  getItem(key: string) {
    return this.values.get(key) ?? null
  }

  setItem(key: string, value: string) {
    this.values.set(key, value)
  }

  removeItem(key: string) {
    this.values.delete(key)
  }

  entries() {
    return [...this.values.entries()]
  }
}

function goalPayload(status = 'active', extra: Record<string, unknown> = {}) {
  return {
    goalId: 'g1',
    sessionKey: SESSION_KEY,
    sessionId: SESSION_ID,
    epoch: 1,
    objective: 'Refactor the module',
    status,
    stateRevision: 1,
    objectiveRevision: 1,
    progressRevision: 0,
    progress: null,
    continuationSeq: 0,
    activeTaskId: 'task-1',
    sourceMessageId: 'message-1',
    terminalTurnId: null,
    executionState: 'working',
    continuationDeferredReason: null,
    turnsStarted: 1,
    turnsSettled: 0,
    windowTurnsStarted: 1,
    activeTimeMs: 5000,
    windowActiveTimeMs: 5000,
    usage: {
      inputTokens: 10,
      outputTokens: 5,
      reasoningTokens: 2,
      cacheReadTokens: 0,
      cacheWriteTokens: 0,
      totalTokens: 17,
    },
    pauseReason: null,
    blockedReason: null,
    terminalReason: null,
    createdAt: 100,
    updatedAt: 200,
    finishedAt: null,
    ...extra,
  }
}

function mutation(goal: unknown, extra: Record<string, unknown> = {}) {
  return {
    accepted: true,
    clientRequestId: crypto.randomUUID(),
    sessionKey: SESSION_KEY,
    sessionId: SESSION_ID,
    epoch: 1,
    taskId: 'task-1',
    userMessageId: 'message-1',
    previousGoalId: null,
    goal,
    ...extra,
  }
}

function harness(continuityStorage?: GoalContinuityStorage) {
  const handlers = new Map<string, (...args: unknown[]) => void>()
  const rpc = {
    call: vi.fn().mockResolvedValue(mutation(goalPayload())),
    on: vi.fn((event: string, handler: (...args: unknown[]) => void) => {
      handlers.set(event, handler)
      return () => handlers.delete(event)
    }),
  }
  const sessionKey = ref(SESSION_KEY)
  const currentEpoch = ref(0)
  const notify = vi.fn()
  const onSetAccepted = vi.fn()
  const ensureSessionKey = vi.fn(async () => sessionKey.value)
  const ensureSubscribed = vi.fn(async () => true)
  const api = useChatGoals({
    rpc,
    sessionKey,
    currentEpoch,
    ensureSessionKey,
    ensureSubscribed,
    onSetAccepted,
    notify,
    continuityStorage,
  })
  return {
    api,
    rpc,
    sessionKey,
    currentEpoch,
    notify,
    handlers,
    ensureSessionKey,
    ensureSubscribed,
    onSetAccepted,
  }
}

async function flushAsyncWork() {
  await Promise.resolve()
  await Promise.resolve()
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

  it('starts from the mutation response after subscription without watchers or polling', async () => {
    vi.useFakeTimers()
    try {
      const { api, rpc, ensureSubscribed, onSetAccepted } = harness()
      const started = await api.startGoal('  Refactor the module  ')

      expect(started).toBe(true)
      expect(ensureSubscribed).toHaveBeenCalledWith(SESSION_KEY)
      expect(rpc.call).toHaveBeenCalledTimes(1)
      const [method, params] = rpc.call.mock.calls[0]
      expect(method).toBe('goals.set')
      expect(params).toMatchObject({
        sessionKey: SESSION_KEY,
        objective: 'Refactor the module',
      })
      expect(params.clientRequestId).toMatch(/^[0-9a-f-]{36}$/)
      expect(params.clientMessageId).toMatch(/^[0-9a-f-]{36}$/)
      expect(params.clientMessageId).not.toBe(params.clientRequestId)
      expect(onSetAccepted).toHaveBeenCalledOnce()
      expect(onSetAccepted).toHaveBeenCalledWith({
        objective: 'Refactor the module',
        clientMessageId: params.clientMessageId,
        response: expect.objectContaining({
          accepted: true,
          taskId: 'task-1',
          userMessageId: 'message-1',
        }),
      })
      expect(api.activeGoal.value?.status).toBe('active')
      expect(api.activeGoal.value?.objective).toBe('Refactor the module')
      expect(api.activeGoal.value?.sourceMessageId).toBe('message-1')

      await vi.advanceTimersByTimeAsync(15_000)
      expect(rpc.call).toHaveBeenCalledTimes(1)
      expect(rpc.on).toHaveBeenCalledWith('session.event.goal', expect.any(Function))
      expect(rpc.on).not.toHaveBeenCalledWith('session.event.goal_run', expect.anything())
      expect(rpc.on).not.toHaveBeenCalledWith('session.event.plan_run', expect.anything())
    } finally {
      vi.useRealTimers()
    }
  })

  it('materializes, subscribes, then registers a Goal in contract order', async () => {
    const { api, rpc, ensureSessionKey, ensureSubscribed } = harness()
    const order: string[] = []
    ensureSessionKey.mockImplementationOnce(async () => {
      order.push('materialize')
      return SESSION_KEY
    })
    ensureSubscribed.mockImplementationOnce(async () => {
      order.push('subscribe')
      return true
    })
    rpc.call.mockImplementationOnce(async method => {
      order.push(method)
      return mutation(goalPayload())
    })

    expect(await api.startGoal('Refactor the module')).toBe(true)
    expect(ensureSubscribed).toHaveBeenCalledWith(SESSION_KEY)
    expect(order).toEqual([
      'materialize',
      'subscribe',
      'goals.set',
    ])
  })

  it('admits only one Goal start while session materialization is pending', async () => {
    const { api, rpc, ensureSessionKey } = harness()
    let releaseMaterialization!: (key: string) => void
    ensureSessionKey.mockImplementationOnce(() => new Promise(resolve => {
      releaseMaterialization = resolve
    }))

    const first = api.startGoal('Refactor the module')
    await Promise.resolve()

    expect(api.busy.value).toBe(true)
    expect(await api.startGoal('Duplicate click')).toBe(false)
    expect(ensureSessionKey).toHaveBeenCalledOnce()
    expect(rpc.call).not.toHaveBeenCalled()

    releaseMaterialization(SESSION_KEY)
    expect(await first).toBe(true)
    expect(rpc.call).toHaveBeenCalledOnce()
    expect(api.busy.value).toBe(false)
  })

  it('keeps Goal admission across its expected provisional session switch', async () => {
    const { api, rpc, ensureSessionKey, sessionKey } = harness()
    const durableKey = 'agent:main:webchat:durable'
    ensureSessionKey.mockImplementationOnce(async () => {
      sessionKey.value = durableKey
      return durableKey
    })
    rpc.call.mockResolvedValueOnce(mutation(goalPayload('active', {
      sessionKey: durableKey,
    }), {
      sessionKey: durableKey,
    }))

    expect(await api.startGoal('Refactor the module')).toBe(true)
    expect(rpc.call).toHaveBeenCalledWith('goals.set', expect.objectContaining({
      sessionKey: durableKey,
    }))
    expect(api.busy.value).toBe(false)
  })

  it('does not register a Goal when navigation wins materialization', async () => {
    const { api, rpc, ensureSessionKey, sessionKey, onSetAccepted } = harness()
    let releaseMaterialization!: (key: string) => void
    ensureSessionKey.mockImplementationOnce(() => new Promise(resolve => {
      releaseMaterialization = resolve
    }))

    const pending = api.startGoal('Refactor the module')
    await Promise.resolve()
    sessionKey.value = 'agent:main:webchat:operator-choice'
    releaseMaterialization(SESSION_KEY)

    expect(await pending).toBe(false)
    expect(rpc.call).not.toHaveBeenCalled()
    expect(onSetAccepted).not.toHaveBeenCalled()
    expect(api.busy.value).toBe(false)
  })

  it('does not project an accepted response after navigation wins the RPC race', async () => {
    const { api, rpc, sessionKey, onSetAccepted } = harness()
    let releaseResponse!: (value: ReturnType<typeof mutation>) => void
    rpc.call.mockImplementationOnce(() => new Promise(resolve => {
      releaseResponse = resolve
    }))

    const pending = api.startGoal('Refactor the module')
    await Promise.resolve()
    await Promise.resolve()
    expect(rpc.call).toHaveBeenCalledOnce()

    sessionKey.value = 'agent:main:webchat:operator-choice'
    releaseResponse(mutation(goalPayload()))

    expect(await pending).toBe(false)
    expect(onSetAccepted).not.toHaveBeenCalled()
  })

  it('does not adopt or report success from an older epoch on the same session key', async () => {
    const { api, rpc, currentEpoch, onSetAccepted } = harness()
    let releaseResponse!: (value: ReturnType<typeof mutation>) => void
    rpc.call.mockImplementationOnce(() => new Promise(resolve => {
      releaseResponse = resolve
    }))

    const pending = api.startGoal('Refactor the module')
    await Promise.resolve()
    await Promise.resolve()
    expect(rpc.call).toHaveBeenCalledOnce()

    currentEpoch.value = 2
    releaseResponse(mutation(goalPayload()))

    expect(await pending).toBe(false)
    expect(api.goal.value).toBeNull()
    expect(onSetAccepted).not.toHaveBeenCalled()
  })

  it('projects an accepted response already superseded by a newer event for the same Goal', async () => {
    const { api, rpc, handlers, onSetAccepted } = harness()
    let releaseResponse!: (value: ReturnType<typeof mutation>) => void
    rpc.call.mockImplementationOnce(() => new Promise(resolve => {
      releaseResponse = resolve
    }))

    const pending = api.startGoal('Refactor the module')
    await Promise.resolve()
    await Promise.resolve()
    const params = rpc.call.mock.calls[0]?.[1] as Record<string, unknown>

    handlers.get('session.event.goal')?.({
      session_key: SESSION_KEY,
      session_id: SESSION_ID,
      epoch: 1,
      stream_seq: 2,
      event_type: 'updated',
      goal: goalPayload('active', {
        stateRevision: 2,
        progressRevision: 1,
        executionState: 'working',
      }),
    })
    releaseResponse(mutation(goalPayload()))

    expect(await pending).toBe(true)
    expect(api.goal.value).toMatchObject({
      goalId: 'g1',
      stateRevision: 2,
      progressRevision: 1,
      executionState: 'working',
    })
    expect(onSetAccepted).toHaveBeenCalledOnce()
    expect(onSetAccepted).toHaveBeenCalledWith(expect.objectContaining({
      objective: 'Refactor the module',
      clientMessageId: params.clientMessageId,
    }))
  })

  it('keeps a durably accepted Goal successful when local projection fails', async () => {
    const { api, rpc, onSetAccepted, notify } = harness()
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    onSetAccepted.mockRejectedValueOnce(new Error('local render failed'))

    try {
      expect(await api.startGoal('Refactor the module')).toBe(true)
      expect(rpc.call).toHaveBeenCalledOnce()
      expect(onSetAccepted).toHaveBeenCalledOnce()
      expect(api.activeGoal.value?.goalId).toBe('g1')
      expect(notify).not.toHaveBeenCalled()
      expect(warn).toHaveBeenCalledWith(
        'Failed to project the accepted Goal message:',
        expect.any(Error),
      )
    } finally {
      warn.mockRestore()
    }
  })

  it('reattaches a matching active Goal only after authoritative hydration', async () => {
    const storage = new MemoryContinuityStorage()
    const first = harness(storage)
    first.rpc.call.mockResolvedValueOnce(mutation(goalPayload(), {
      continuityToken: 'continuity-token-1',
    }))

    expect(await first.api.startGoal('Refactor the module')).toBe(true)
    expect(storage.entries()).toHaveLength(1)
    expect(storage.entries()[0]?.[0]).toContain(encodeURIComponent(SESSION_KEY))
    expect(storage.entries()[0]?.[0]).toContain(encodeURIComponent(SESSION_ID))
    expect(storage.entries()[0]?.[0]).toContain('g1')
    expect(storage.entries()[0]?.[1]).toContain('continuity-token-1')

    const refreshed = harness(storage)
    refreshed.rpc.call.mockResolvedValueOnce(mutation(goalPayload('active', {
      continuationDeferredReason: null,
      executionState: 'queued',
    }), {
      continuityToken: 'continuity-token-1',
    }))
    const detached = goalPayload('active', {
      continuationDeferredReason: 'owner_disconnected',
      executionState: 'idle',
      activeTaskId: null,
      turnsStarted: 3,
      turnsSettled: 3,
      windowTurnsStarted: 3,
    })

    expect(refreshed.api.applyHydration({
      key: SESSION_KEY,
      sessionId: SESSION_ID,
      epoch: 1,
      goalSnapshotStreamSeq: 4,
      goal: detached,
    })).toBe(true)
    expect(refreshed.rpc.call).toHaveBeenCalledWith('goals.reattach', {
      sessionKey: SESSION_KEY,
      sessionId: SESSION_ID,
      epoch: 1,
      expectedGoalId: 'g1',
      continuityToken: 'continuity-token-1',
      sourceKind: 'web',
    })
    expect(refreshed.rpc.call).not.toHaveBeenCalledWith('goals.resume', expect.anything())

    await flushAsyncWork()
    expect(refreshed.api.activeGoal.value?.continuationDeferredReason).toBeNull()
    expect(refreshed.api.activeGoal.value?.turnsStarted).toBe(3)
    expect(refreshed.api.activeGoal.value?.windowTurnsStarted).toBe(3)
  })

  it('clears the equal-revision detached overlay without rolling back newer live execution', async () => {
    const storage = new MemoryContinuityStorage()
    const first = harness(storage)
    first.rpc.call.mockResolvedValueOnce(mutation(goalPayload(), {
      continuityToken: 'continuity-token-1',
    }))
    await first.api.startGoal('Refactor the module')

    const refreshed = harness(storage)
    let resolveReattach: ((value: ReturnType<typeof mutation>) => void) | undefined
    refreshed.rpc.call.mockImplementationOnce(() => new Promise((resolve) => {
      resolveReattach = resolve
    }))
    refreshed.api.applyHydration({
      key: SESSION_KEY,
      sessionId: SESSION_ID,
      epoch: 1,
      goalSnapshotStreamSeq: 4,
      goal: goalPayload('active', {
        continuationDeferredReason: 'owner_disconnected',
        executionState: 'idle',
        activeTaskId: null,
      }),
    })
    refreshed.handlers.get('session.event.goal')?.({
      session_key: SESSION_KEY,
      session_id: SESSION_ID,
      epoch: 1,
      stream_seq: 5,
      event_type: 'updated',
      goal: goalPayload('active', {
        continuationDeferredReason: 'owner_disconnected',
        executionState: 'working',
      }),
    })
    resolveReattach?.(mutation(goalPayload('active', {
      continuationDeferredReason: null,
      executionState: 'queued',
    }), {
      continuityToken: 'continuity-token-1',
    }))
    await flushAsyncWork()

    expect(refreshed.api.activeGoal.value?.continuationDeferredReason).toBeNull()
    expect(refreshed.api.activeGoal.value?.executionState).toBe('working')
  })

  it('does not consume or use continuity on a deferred fast ACK or a Goal event', async () => {
    const storage = new MemoryContinuityStorage()
    const first = harness(storage)
    first.rpc.call.mockResolvedValueOnce(mutation(goalPayload(), {
      continuityToken: 'continuity-token-1',
    }))
    await first.api.startGoal('Refactor the module')

    const refreshed = harness(storage)
    expect(refreshed.api.applyHydration({
      key: SESSION_KEY,
      epoch: 1,
      goal: null,
      goalSnapshotStreamSeq: null,
      deferred_fields: ['goal', 'goalSnapshotStreamSeq'],
    })).toBe(false)
    expect(storage.entries()).toHaveLength(1)
    expect(refreshed.rpc.call).not.toHaveBeenCalled()

    refreshed.handlers.get('session.event.goal')?.({
      session_key: SESSION_KEY,
      session_id: SESSION_ID,
      epoch: 1,
      stream_seq: 2,
      event_type: 'updated',
      goal: goalPayload('active', {
        continuationDeferredReason: 'owner_disconnected',
        activeTaskId: null,
        executionState: 'idle',
      }),
    })
    expect(refreshed.rpc.call).not.toHaveBeenCalled()
    expect(refreshed.api.activeGoal.value?.status).toBe('active')
  })

  it('never presents continuity from an old Goal to its replacement', async () => {
    const storage = new MemoryContinuityStorage()
    const first = harness(storage)
    first.rpc.call.mockResolvedValueOnce(mutation(goalPayload(), {
      continuityToken: 'continuity-token-1',
    }))
    await first.api.startGoal('Refactor the module')

    const refreshed = harness(storage)
    refreshed.api.applyHydration({
      key: SESSION_KEY,
      sessionId: SESSION_ID,
      epoch: 1,
      goalSnapshotStreamSeq: 4,
      goal: goalPayload('active', {
        goalId: 'g2',
        objective: 'Ship the replacement',
        continuationDeferredReason: 'owner_disconnected',
        activeTaskId: null,
        executionState: 'idle',
      }),
    })

    expect(refreshed.rpc.call).not.toHaveBeenCalled()
    expect(refreshed.api.connectionTakeoverAvailable.value).toBe(true)
    expect(storage.entries()).toHaveLength(0)
  })

  it('offers explicit takeover after automatic reattach fails without using Resume', async () => {
    const storage = new MemoryContinuityStorage()
    const first = harness(storage)
    first.rpc.call.mockResolvedValueOnce(mutation(goalPayload(), {
      continuityToken: 'continuity-token-1',
    }))
    await first.api.startGoal('Refactor the module')

    const refreshed = harness(storage)
    refreshed.rpc.call.mockRejectedValueOnce(new Error('connection changed'))
    refreshed.api.applyHydration({
      key: SESSION_KEY,
      sessionId: SESSION_ID,
      epoch: 1,
      goalSnapshotStreamSeq: 4,
      goal: goalPayload('active', {
        continuationDeferredReason: 'owner_disconnected',
        activeTaskId: null,
        executionState: 'idle',
      }),
    })
    await flushAsyncWork()
    expect(refreshed.api.connectionTakeoverAvailable.value).toBe(true)

    refreshed.rpc.call.mockResolvedValueOnce(mutation(goalPayload('active', {
      continuationDeferredReason: null,
      activeTaskId: null,
      executionState: 'idle',
    }), {
      continuityToken: 'continuity-token-2',
    }))
    expect(await refreshed.api.takeOverConnection()).toBe(true)
    expect(refreshed.rpc.call).toHaveBeenLastCalledWith('goals.reattach', {
      sessionKey: SESSION_KEY,
      sessionId: SESSION_ID,
      epoch: 1,
      expectedGoalId: 'g1',
      takeover: true,
      sourceKind: 'web',
    })
    expect(refreshed.rpc.call).not.toHaveBeenCalledWith('goals.resume', expect.anything())
    expect(refreshed.api.activeGoal.value?.continuationDeferredReason).toBeNull()
    expect(storage.entries()[0]?.[1]).toContain('continuity-token-2')
  })

  it('does not let a cursorless mutation response roll back a newer live execution state', async () => {
    const { api, rpc, handlers } = harness()
    rpc.call.mockImplementationOnce(async () => {
      handlers.get('session.event.goal')?.({
        session_key: SESSION_KEY,
        session_id: SESSION_ID,
        epoch: 1,
        stream_seq: 4,
        event_type: 'updated',
        state_revision: 1,
        progress_revision: 0,
        goal: goalPayload('active', { executionState: 'working' }),
      })
      return mutation(goalPayload('active', { executionState: 'queued' }))
    })

    expect(await api.startGoal('Refactor the module')).toBe(true)
    expect(api.activeGoal.value?.executionState).toBe('working')
  })

  it('rejects empty and oversized goal objectives before RPC', async () => {
    const { api, rpc } = harness()
    expect(await api.startGoal('   ')).toBe(false)
    expect(await api.startGoal('x'.repeat(4001))).toBe(false)
    expect(rpc.call).not.toHaveBeenCalled()
  })

  it('validates set objective length in Unicode code points', async () => {
    const { api, rpc } = harness()
    const validAstralObjective = '🦑'.repeat(3000)

    expect(validAstralObjective.length).toBe(6000)
    expect(await api.startGoal(validAstralObjective)).toBe(true)
    expect(rpc.call).toHaveBeenLastCalledWith('goals.set', expect.objectContaining({
      objective: validAstralObjective,
    }))

    const callsAfterValidSet = rpc.call.mock.calls.length
    expect(await api.startGoal('🦑'.repeat(4001))).toBe(false)
    expect(rpc.call).toHaveBeenCalledTimes(callsAfterValidSet)
  })

  it('applies the single Goal event and rejects stale state/progress revisions', () => {
    const { api, handlers } = harness()
    api.applyHydration({
      key: SESSION_KEY,
      epoch: 1,
      goalSnapshotStreamSeq: 10,
      goal: goalPayload('active', {
        progressRevision: 1,
        progress: {
          explanation: 'Initial pass',
          steps: [{ text: 'Inspect', status: 'in_progress' }],
        },
      }),
    })

    handlers.get('session.event.goal')?.({
      session_key: SESSION_KEY,
      session_id: SESSION_ID,
      epoch: 1,
      stream_seq: 12,
      event_type: 'updated',
      goal: goalPayload('paused', {
        stateRevision: 3,
        progressRevision: 2,
        progress: {
          explanation: 'Tests added',
          steps: [{ text: 'Inspect', status: 'completed' }],
        },
      }),
    })
    expect(api.activeGoal.value?.status).toBe('paused')
    expect(api.activeGoal.value?.progress?.explanation).toBe('Tests added')

    handlers.get('session.event.goal')?.({
      session_key: SESSION_KEY,
      session_id: SESSION_ID,
      epoch: 1,
      stream_seq: 13,
      event_type: 'updated',
      goal: goalPayload('active', { stateRevision: 2, progressRevision: 1 }),
    })
    expect(api.activeGoal.value?.status).toBe('paused')
  })

  it('does not let a late hydrate overwrite an event newer than its watermark', () => {
    const { api, handlers } = harness()
    api.applyHydration({
      key: SESSION_KEY,
      epoch: 1,
      goalSnapshotStreamSeq: 5,
      goal: goalPayload('active'),
    })
    handlers.get('session.event.goal')?.({
      session_key: SESSION_KEY,
      session_id: SESSION_ID,
      epoch: 1,
      stream_seq: 8,
      event_type: 'updated',
      goal: goalPayload('paused', { stateRevision: 2 }),
    })

    expect(api.applyHydration({
      key: SESSION_KEY,
      epoch: 1,
      goalSnapshotStreamSeq: 6,
      goal: goalPayload('active', { stateRevision: 9 }),
    })).toBe(false)
    expect(api.activeGoal.value?.status).toBe('paused')
  })

  it('does not resurrect a Goal replaced by an authoritative hydrate', () => {
    const { api } = harness()
    expect(api.applyHydration({
      key: SESSION_KEY,
      sessionId: SESSION_ID,
      epoch: 1,
      goalSnapshotStreamSeq: 1,
      goal: goalPayload('complete', {
        stateRevision: 4,
        activeTaskId: null,
        executionState: 'idle',
      }),
    })).toBe(true)
    expect(api.applyHydration({
      key: SESSION_KEY,
      sessionId: SESSION_ID,
      epoch: 1,
      goalSnapshotStreamSeq: 2,
      goal: goalPayload('active', {
        goalId: 'g2',
        objective: 'Authoritative replacement',
        stateRevision: 1,
      }),
    })).toBe(true)
    expect(api.goal.value?.goalId).toBe('g2')

    expect(api.applyMutationResponse(mutation(goalPayload('complete', {
      stateRevision: 99,
      activeTaskId: null,
      executionState: 'idle',
    })))).toBe(false)
    expect(api.goal.value?.goalId).toBe('g2')
    expect(api.goal.value?.objective).toBe('Authoritative replacement')
  })

  it('accepts a newer authoritative row at the already-consumed watermark', () => {
    const { api, handlers } = harness()
    handlers.get('session.event.goal')?.({
      session_key: SESSION_KEY,
      session_id: SESSION_ID,
      epoch: 1,
      stream_seq: 5,
      event_type: 'updated',
      goal: goalPayload('active', { stateRevision: 2 }),
    })

    expect(api.applyHydration({
      key: SESSION_KEY,
      sessionId: SESSION_ID,
      epoch: 1,
      goalSnapshotStreamSeq: 5,
      goal: goalPayload('paused', { stateRevision: 3 }),
    })).toBe(true)
    expect(api.activeGoal.value?.status).toBe('paused')
    expect(api.activeGoal.value?.stateRevision).toBe(3)
  })

  it('ignores the deferred fast ACK and applies the later hydrated snapshot', () => {
    const { api } = harness()
    expect(api.applyHydration({
      key: SESSION_KEY,
      epoch: 1,
      goal: null,
      goalSnapshotStreamSeq: null,
      deferred_fields: ['goal', 'goalSnapshotStreamSeq'],
    })).toBe(false)

    expect(api.applyHydration({
      key: SESSION_KEY,
      epoch: 1,
      goalSnapshotStreamSeq: 0,
      goal: goalPayload(),
    })).toBe(true)
    expect(api.activeGoal.value?.goalId).toBe('g1')
  })

  it('resets only the Goal transport watermark when a new stream generation starts', () => {
    const { api, handlers } = harness()
    expect(api.applyHydration({
      key: SESSION_KEY,
      epoch: 1,
      goalSnapshotStreamSeq: 100,
      goal: goalPayload('active'),
    })).toBe(true)
    const authoritativeGoal = api.goal.value

    expect(api.applyHydration({
      key: SESSION_KEY,
      epoch: 1,
      stream_generation: 'gateway-generation-2',
      goal: null,
      goalSnapshotStreamSeq: null,
      deferred_fields: ['goal', 'goalSnapshotStreamSeq'],
    })).toBe(false)
    // A transport restart must not blank durable Goal state while metadata is
    // still hydrating.
    expect(api.goal.value).toBe(authoritativeGoal)

    handlers.get('session.event.goal')?.({
      session_key: SESSION_KEY,
      session_id: SESSION_ID,
      epoch: 1,
      stream_generation: 'gateway-generation-2',
      stream_seq: 1,
      event_type: 'updated',
      goal: goalPayload('paused', {
        stateRevision: 2,
        activeTaskId: null,
        executionState: 'idle',
      }),
    })

    expect(api.goal.value).toMatchObject({
      status: 'paused',
      stateRevision: 2,
    })
  })

  it('applies clear tombstones and does not resurrect the cleared Goal', () => {
    const { api, handlers } = harness()
    api.applyHydration({
      key: SESSION_KEY,
      epoch: 1,
      goalSnapshotStreamSeq: 2,
      goal: goalPayload('active', { stateRevision: 4 }),
    })
    handlers.get('session.event.goal')?.({
      session_key: SESSION_KEY,
      session_id: SESSION_ID,
      epoch: 1,
      stream_seq: 3,
      event_type: 'cleared',
      state_revision: 5,
      previous_goal_id: 'g1',
      goal: null,
    })
    expect(api.goal.value).toBeNull()

    handlers.get('session.event.goal')?.({
      session_key: SESSION_KEY,
      session_id: SESSION_ID,
      epoch: 1,
      stream_seq: 4,
      event_type: 'updated',
      goal: goalPayload('active', { stateRevision: 4 }),
    })
    expect(api.goal.value).toBeNull()
  })

  it('does not resurrect a replaced Goal from a delayed cursorless mutation', () => {
    const { api, handlers } = harness()
    api.applyHydration({
      key: SESSION_KEY,
      epoch: 1,
      goalSnapshotStreamSeq: 2,
      goal: goalPayload('complete', {
        stateRevision: 4,
        activeTaskId: null,
        executionState: 'idle',
      }),
    })
    handlers.get('session.event.goal')?.({
      session_key: SESSION_KEY,
      session_id: SESSION_ID,
      epoch: 1,
      stream_seq: 3,
      event_type: 'created',
      state_revision: 1,
      progress_revision: 0,
      previous_goal_id: 'g1',
      goal: goalPayload('active', {
        goalId: 'g2',
        objective: 'Ship the replacement',
        stateRevision: 1,
      }),
    })
    expect(api.goal.value?.goalId).toBe('g2')

    expect(api.applyMutationResponse(mutation(goalPayload('complete', {
      stateRevision: 99,
      activeTaskId: null,
      executionState: 'idle',
    })))).toBe(false)
    expect(api.goal.value?.goalId).toBe('g2')
    expect(api.goal.value?.objective).toBe('Ship the replacement')
  })

  it('sends edit and pause mutations with the current Goal CAS fence', async () => {
    const { api, rpc } = harness()
    api.applyHydration({
      key: SESSION_KEY,
      epoch: 1,
      goalSnapshotStreamSeq: 2,
      goal: goalPayload('active', { stateRevision: 4 }),
    })
    rpc.call.mockResolvedValueOnce(mutation(goalPayload('active', {
      stateRevision: 5,
      objectiveRevision: 2,
      objective: 'Ship the refactor',
      progress: null,
    })))
    expect(await api.edit('Ship the refactor')).toBe(true)
    expect(rpc.call).toHaveBeenLastCalledWith('goals.edit', expect.objectContaining({
      sessionKey: SESSION_KEY,
      objective: 'Ship the refactor',
      expectedGoalId: 'g1',
      expectedStateRevision: 4,
      clientRequestId: expect.stringMatching(/^[0-9a-f-]{36}$/),
    }))

    rpc.call.mockResolvedValueOnce(mutation(goalPayload('paused', {
      stateRevision: 6,
      objectiveRevision: 2,
      objective: 'Ship the refactor',
      activeTaskId: null,
      executionState: 'idle',
      pauseReason: 'user_paused',
    })))
    expect(await api.pause()).toBe(true)
    expect(rpc.call).toHaveBeenLastCalledWith('goals.pause', expect.objectContaining({
      expectedGoalId: 'g1',
      expectedStateRevision: 5,
    }))
  })

  it('remembers continuity returned by editing a completed Goal back to active', async () => {
    const storage = new MemoryContinuityStorage()
    const { api, rpc } = harness(storage)
    api.applyHydration({
      key: SESSION_KEY,
      epoch: 1,
      goalSnapshotStreamSeq: 2,
      goal: goalPayload('complete', {
        stateRevision: 4,
        activeTaskId: null,
        executionState: 'idle',
        finishedAt: 300,
        terminalReason: 'model_complete',
      }),
    })
    rpc.call.mockResolvedValueOnce(mutation(goalPayload('active', {
      stateRevision: 5,
      objectiveRevision: 2,
      objective: 'Continue the completed Goal',
      activeTaskId: null,
      executionState: 'queued',
      finishedAt: null,
      terminalReason: null,
    }), {
      continuityToken: 'continuity-token-after-edit',
    }))

    expect(await api.edit('Continue the completed Goal')).toBe(true)
    expect(storage.entries()).toHaveLength(1)
    expect(storage.entries()[0]?.[1]).toContain('continuity-token-after-edit')
  })

  it('notifies stable Goal conflicts without leaking raw backend English', async () => {
    const { api, rpc, notify } = harness()
    api.applyHydration({
      key: SESSION_KEY,
      epoch: 1,
      goalSnapshotStreamSeq: 2,
      goal: goalPayload('paused', {
        stateRevision: 4,
        activeTaskId: 'task-1',
        executionState: 'working',
      }),
    })
    const error = Object.assign(
      new Error('The Goal still owns an unsettled task'),
      { code: 'GOAL_BUSY' },
    )
    rpc.call.mockRejectedValueOnce(error)

    expect(await api.resume()).toBe(false)
    expect(notify).toHaveBeenCalledWith(
      'The goal state is still settling. Its latest status is shown above.',
    )
    expect(notify).not.toHaveBeenCalledWith(expect.stringContaining('unsettled task'))
  })

  it('validates edit objective length in Unicode code points', async () => {
    const { api, rpc, notify } = harness()
    api.applyHydration({
      key: SESSION_KEY,
      epoch: 1,
      goalSnapshotStreamSeq: 2,
      goal: goalPayload('active', { stateRevision: 4 }),
    })
    const validAstralObjective = '🦑'.repeat(3000)
    rpc.call.mockResolvedValueOnce(mutation(goalPayload('active', {
      stateRevision: 5,
      objectiveRevision: 2,
      objective: validAstralObjective,
    })))

    expect(await api.edit(validAstralObjective)).toBe(true)
    expect(rpc.call).toHaveBeenLastCalledWith('goals.edit', expect.objectContaining({
      objective: validAstralObjective,
    }))

    const callsAfterValidEdit = rpc.call.mock.calls.length
    expect(await api.edit('🦑'.repeat(4001))).toBe(false)
    expect(rpc.call).toHaveBeenCalledTimes(callsAfterValidEdit)
    expect(notify).toHaveBeenCalledWith('Enter a valid goal before saving.')
  })

  it('exposes a complete outcome only after its owning task settles', () => {
    const { api } = harness()
    api.applyHydration({
      key: SESSION_KEY,
      epoch: 1,
      goalSnapshotStreamSeq: 1,
      goal: goalPayload('blocked', { activeTaskId: null, executionState: 'idle' }),
    })
    expect(api.activeGoal.value?.status).toBe('blocked')
    expect(api.lastGoal.value).toBeNull()

    api.applyHydration({
      key: SESSION_KEY,
      epoch: 1,
      goalSnapshotStreamSeq: 2,
      goal: goalPayload('complete', {
        stateRevision: 2,
        activeTaskId: 'task-1',
        executionState: 'working',
        terminalTurnId: 'turn-1',
      }),
    })
    expect(api.activeGoal.value?.status).toBe('complete')
    expect(api.activeGoal.value?.executionState).toBe('working')
    expect(api.lastGoal.value).toBeNull()

    api.applyHydration({
      key: SESSION_KEY,
      epoch: 1,
      goalSnapshotStreamSeq: 3,
      goal: goalPayload('complete', {
        stateRevision: 3,
        activeTaskId: null,
        executionState: 'idle',
        terminalTurnId: 'turn-1',
      }),
    })
    expect(api.lastGoal.value?.status).toBe('complete')
  })

  it('keeps older complete snapshots displayable after normalization', () => {
    const { api } = harness()
    const legacy: Record<string, unknown> = goalPayload('complete', {
      stateRevision: 2,
      terminalTurnId: undefined,
    })
    delete legacy.activeTaskId
    delete legacy.executionState

    api.applyHydration({
      key: SESSION_KEY,
      epoch: 1,
      goalSnapshotStreamSeq: 1,
      goal: legacy,
    })

    expect(api.lastGoal.value?.status).toBe('complete')
    expect(api.lastGoal.value?.activeTaskId).toBeNull()
    expect(api.lastGoal.value?.executionState).toBe('idle')
  })

  it('uses a rendered terminal assistant as the outcome anchor only after settlement', () => {
    const unsettled = normalizeGoal(goalPayload('complete', {
      stateRevision: 2,
      terminalTurnId: 'turn-terminal',
    }))
    const settled = normalizeGoal(goalPayload('complete', {
      stateRevision: 3,
      activeTaskId: null,
      executionState: 'idle',
      terminalTurnId: 'turn-terminal',
    }))
    const terminalAssistant = [{
      displayRole: 'assistant',
      turnId: 'turn-terminal',
    }]

    expect(goalHasRenderedTerminalAnchor(unsettled, terminalAssistant)).toBe(false)
    expect(goalHasRenderedTerminalAnchor(settled, [])).toBe(false)
    expect(goalHasRenderedTerminalAnchor(settled, [{
      displayRole: 'assistant',
      turnId: 'turn-other',
    }])).toBe(false)
    expect(goalHasRenderedTerminalAnchor(settled, terminalAssistant)).toBe(true)
    expect(goalHasRenderedTerminalAnchor(settled, [{
      ...terminalAssistant[0],
      stopNotice: true,
    }])).toBe(false)
  })

  it('clears generation state synchronously on session and epoch changes', async () => {
    const { api, sessionKey, currentEpoch } = harness()
    api.applyHydration({
      key: SESSION_KEY,
      epoch: 1,
      goalSnapshotStreamSeq: 1,
      goal: goalPayload(),
    })
    expect(api.goal.value).not.toBeNull()

    currentEpoch.value = 2
    expect(api.goal.value).toBeNull()

    api.applyHydration({
      key: SESSION_KEY,
      epoch: 2,
      goalSnapshotStreamSeq: 0,
      goal: goalPayload('active', { epoch: 2 }),
    })
    expect(api.goal.value).not.toBeNull()
    currentEpoch.value = 0
    expect(api.goal.value).toBeNull()

    api.arm()
    sessionKey.value = 'agent:main:webchat:other'
    await nextTick()
    expect(api.draftArmed.value).toBe(false)
    expect(api.goal.value).toBeNull()
  })

  it('keeps per-session continuity across ordinary chat navigation', async () => {
    const storage = new MemoryContinuityStorage()
    const { api, rpc, sessionKey } = harness(storage)
    rpc.call.mockResolvedValueOnce(mutation(goalPayload(), {
      continuityToken: 'continuity-token-1',
    }))
    await api.startGoal('Refactor the module')
    expect(storage.entries()).toHaveLength(1)

    sessionKey.value = 'agent:main:webchat:other'
    await nextTick()

    expect(storage.entries()).toHaveLength(1)
    expect(api.goal.value).toBeNull()
  })
})
