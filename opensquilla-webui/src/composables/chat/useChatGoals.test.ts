import { describe, expect, it, vi } from 'vitest'
import { effectScope, nextTick, ref, watch, type Ref } from 'vue'
import { createV4SessionReadPort } from '@/adapters/gateway/sessionReadPortV4'
import { createV4GoalContinuity } from '@/adapters/gateway/goalContinuityV4'
import type { TransportCallOptions } from '@/adapters/gateway/transportTypes'
import {
  createGoalSessionReadiness,
  goalHasRenderedTerminalAnchor,
  normalizeGoal,
  useChatGoals,
  type UseChatGoalsOptions,
  type GoalContinuityStorage,
} from './useChatGoals'
import { GoalCenterError, type GoalCapabilities } from '@/modules/goalCenter'
import type { GoalEvent, GoalReattachInput } from '@/modules/goalContinuity'
import { useChatSessionBootstrap } from './useChatSessionBootstrap'
import { useChatSessionSubscription } from './useChatSessionSubscription'
import { createConversationRuntime } from '@/modules/conversationRuntime'
import type { SessionReadLease, SessionReadLive, SessionReadMetadata } from '@/modules/sessionReadLifecycle'

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

function harness(
  continuityStorage?: GoalContinuityStorage,
  streamGeneration?: Ref<string | null>,
  connectionEpoch = ref(0),
  localPathReferences = ref<string[]>([]),
  authority = ref<string | null>('authority-a'),
) {
  const handlers = new Map<string, (...args: unknown[]) => void>()
  const toGoalEvent = (value: unknown): GoalEvent => {
    const source = (value && typeof value === 'object' && !Array.isArray(value))
      ? value as Record<string, unknown>
      : {}
    const nested = Object.prototype.hasOwnProperty.call(source, 'goal')
      ? source.goal
      : source
    const goal = nested && typeof nested === 'object' && !Array.isArray(nested)
      ? nested as Record<string, unknown>
      : null
    const text = (...values: unknown[]) => values.find(item => typeof item === 'string') as string | null ?? null
    const integer = (...values: unknown[]) => values.find(item => typeof item === 'number') as number | null ?? null
    return {
      eventType: text(source.eventType, source.event_type) as GoalEvent['eventType'] ?? 'updated',
      sessionKey: text(source.sessionKey, source.session_key, source.key, goal?.sessionKey, goal?.session_key),
      sessionId: text(source.sessionId, source.session_id, goal?.sessionId, goal?.session_id),
      epoch: integer(source.epoch, source.sessionEpoch, source.session_epoch, goal?.epoch),
      streamSeq: integer(source.streamSeq, source.stream_seq),
      streamGeneration: text(source.streamGeneration, source.stream_generation),
      stateRevision: integer(source.stateRevision, source.state_revision, goal?.stateRevision, goal?.state_revision),
      progressRevision: integer(source.progressRevision, source.progress_revision, goal?.progressRevision, goal?.progress_revision),
      objectiveRevision: integer(source.objectiveRevision, source.objective_revision, goal?.objectiveRevision, goal?.objective_revision),
      previousGoalId: text(source.previousGoalId, source.previous_goal_id),
      goal: goal as GoalEvent['goal'],
    }
  }
  const rpc = {
    call: vi.fn().mockResolvedValue(mutation(goalPayload())),
  }
  const goalCenter = {
    available: () => true,
    capabilities: vi.fn(async (): Promise<GoalCapabilities> => ({
      supported: true,
      executionEnabled: true,
      maxTurns: 50,
      runtimeBudgetSeconds: 3600,
      methods: ['goals.set'],
    })),
    status: async (sessionKey: string) => ({ sessionKey, sessionId: SESSION_ID, epoch: 1, goal: goalPayload() }),
    set: async (input: { sessionKey: string; objective: string; clientRequestId: string; clientMessageId: string }) => rpc.call('goals.set', input),
    edit: async (input: any) => rpc.call('goals.edit', input),
    pause: async (input: any) => rpc.call('goals.pause', input),
    resume: async (input: any) => rpc.call('goals.resume', input),
    clear: async (input: any) => rpc.call('goals.clear', input),
  }
  const goalContinuity = {
    reattach: vi.fn((input: GoalReattachInput, _options?: { signal?: AbortSignal }) => rpc.call('goals.reattach', input)),
    subscribe: vi.fn((listener: (event: GoalEvent) => void) => {
      const handler = (payload: unknown) => listener(toGoalEvent(payload))
      handlers.set('session.event.goal', handler)
      return { close: () => handlers.delete('session.event.goal') }
    }),
    dispose: vi.fn(),
  }
  const sessionKey = ref(SESSION_KEY)
  const currentEpoch = ref(0)
  const notify = vi.fn()
  const onSetAccepted = vi.fn()
  const ensureSessionKey = vi.fn(async () => sessionKey.value)
  const ensureSubscribed = vi.fn<NonNullable<UseChatGoalsOptions['ensureSubscribed']>>(async () => true)
  const api = useChatGoals({
    goalCenter,
    goalContinuity,
    localPathReferences,
    sessionKey,
    currentEpoch,
    streamGeneration,
    connectionEpoch,
    authority,
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
    goalCenter,
    connectionEpoch,
    authority,
    goalContinuity,
    ensureSessionKey,
    ensureSubscribed,
    onSetAccepted,
  }
}

async function flushAsyncWork() {
  await Promise.resolve()
  await Promise.resolve()
}

describe('Goal terminal decision projection and explicit continuity retry', () => {
  const detached = () => goalPayload('active', {
    continuationDeferredReason: 'owner_disconnected', executionState: 'idle', activeTaskId: null,
  })
  async function seeded() {
    const storage = new MemoryContinuityStorage()
    const seed = harness(storage)
    seed.rpc.call.mockResolvedValueOnce(mutation(goalPayload(), { continuityToken: 'saved-token' }))
    await seed.api.startGoal('Seed continuity')
    seed.api.reset()
    const h = harness(storage)
    const hydrate = (snapshot = detached(), watermark = 1) => h.api.applyHydration({
      key: SESSION_KEY, sessionId: SESSION_ID, epoch: 1, goalSnapshotStreamSeq: watermark, goal: snapshot,
    })
    const emit = (snapshot: unknown, streamSeq = 2) => h.handlers.get('session.event.goal')?.({
      eventType: snapshot ? 'updated' : 'cleared', sessionKey: SESSION_KEY,
      sessionId: SESSION_ID, epoch: 1, streamSeq, goal: snapshot,
    })
    return { ...h, storage, hydrate, emit }
  }

  it.each(['require-takeover', 'owner-attached', 'stop'] as const)(
    'preserves %s through a newer detached event without retrying or reclassifying a saved token', async recoveryAction => {
      vi.useFakeTimers()
      const h = await seeded()
      const request = vi.fn(async () => { throw Object.assign(new Error('Server rejected continuity'), {
        code: 'EXECUTION_LEASE_REQUIRED', retryable: false, details: { recoveryAction },
      }) })
      const adapter = createV4GoalContinuity({ request })
      h.goalContinuity.reattach.mockImplementation((input, options) => adapter.reattach(input, options))
      try {
        h.hydrate()
        await vi.advanceTimersByTimeAsync(0)
        expect(h.api.connectionTakeoverAvailable.value).toBe(recoveryAction === 'require-takeover')
        const newer = { ...detached(), stateRevision: 2, objectiveRevision: 2, objective: 'Edited while detached' }
        h.emit(newer)
        await vi.advanceTimersByTimeAsync(125_000)
        expect(h.api.connectionTakeoverAvailable.value).toBe(recoveryAction === 'require-takeover')
        expect(h.api.connectionRetryAvailable.value).toBe(false)
        await h.api.retryConnection()
        h.connectionEpoch.value++
        h.hydrate(newer, 2)
        await vi.advanceTimersByTimeAsync(0)
        expect(h.api.connectionTakeoverAvailable.value).toBe(recoveryAction === 'require-takeover')
        expect(request).toHaveBeenCalledOnce()
      } finally { h.api.reset(); adapter.dispose(); vi.useRealTimers() }
    },
  )

  it.each(['attached', 'paused', 'cleared', 'token', 'authority', 'epoch'] as const)(
    'does not retain a prior takeover action after %s changes', async change => {
      vi.useFakeTimers()
      const h = await seeded()
      h.goalContinuity.reattach.mockRejectedValue(new GoalCenterError('conflict', 'take over', {
        retryable: false, recoveryAction: 'require-takeover',
      }))
      try {
        h.hydrate()
        await vi.advanceTimersByTimeAsync(0)
        expect(h.api.connectionTakeoverAvailable.value).toBe(true)
        if (change === 'attached') h.emit({ ...detached(), continuationDeferredReason: null, stateRevision: 2 })
        if (change === 'paused') h.emit({ ...detached(), status: 'paused', stateRevision: 2 })
        if (change === 'cleared') h.emit(null)
        if (change === 'token') {
          const [key, value] = h.storage.entries()[0]!
          h.storage.setItem(key, JSON.stringify({ ...JSON.parse(value), token: 'replacement-token' }))
          h.emit({ ...detached(), stateRevision: 2 })
        }
        if (change === 'authority') h.authority.value = 'authority-b'
        if (change === 'epoch') h.currentEpoch.value++
        expect(h.api.connectionTakeoverAvailable.value).toBe(false)
        expect(h.api.connectionRetryAvailable.value).toBe(false)
      } finally { h.api.reset(); vi.useRealTimers() }
    },
  )

  it('uses a manual same-token retry only after exhaustion, keeps Retry-After and does not block other Goal controls', async () => {
    vi.useFakeTimers()
    const h = await seeded()
    const request = vi.fn(async (_method: string, _params?: Record<string, unknown>) => mutation(
      goalPayload('active', { stateRevision: 2 }), { continuityToken: 'saved-token' }))
      .mockRejectedValueOnce(Object.assign(new Error('Busy dependency'), {
        code: 'STORAGE_BUSY', retryable: true, retry_after_ms: 180_000, details: { recoveryAction: 'retry' },
      }))
    const adapter = createV4GoalContinuity({
      request: async <T>(method: string, params?: Record<string, unknown>) => await request(method, params) as T,
    })
    h.goalContinuity.reattach.mockImplementation((input, options) => adapter.reattach(input, options))
    try {
      h.hydrate()
      await vi.advanceTimersByTimeAsync(120_001)
      expect(h.api.connectionRetryAvailable.value).toBe(true)
      expect(h.api.connectionTakeoverAvailable.value).toBe(false)
      const newer = { ...detached(), stateRevision: 2 }
      h.emit(newer)
      h.connectionEpoch.value++
      h.hydrate(newer, 2)
      await vi.advanceTimersByTimeAsync(0)
      expect(request).toHaveBeenCalledOnce()
      const retry = h.api.retryConnection()
      expect(h.api.retryConnection()).toBe(retry)
      await vi.advanceTimersByTimeAsync(50_000)
      expect(h.api.busy.value).toBe(false)
      expect(h.api.reattaching.value).toBe(true)
      expect(h.api.connectionRetryAvailable.value).toBe(true)
      expect(request).toHaveBeenCalledOnce()
      await vi.advanceTimersByTimeAsync(10_000)
      await retry
      expect(request).toHaveBeenCalledTimes(2)
      expect(h.ensureSubscribed.mock.calls.map(call => call[1])).toEqual([{ explicit: false }, { explicit: true }])
      expect(request.mock.calls[1]?.[1]).toMatchObject({ continuityToken: 'saved-token', expectedGoalId: 'g1' })
      expect(request.mock.calls[1]?.[1]).not.toHaveProperty('takeover')
      expect(h.rpc.call).not.toHaveBeenCalled()
      expect(h.api.connectionRetryAvailable.value).toBe(false)
      expect(h.api.goal.value?.continuationDeferredReason).toBeNull()
    } finally { h.api.reset(); adapter.dispose(); vi.useRealTimers() }
  })

  it.each(['proof', 'authority', 'goal', 'token', 'pause', 'clear', 'navigation'] as const)(
    'fences a manual retry waiting for subscription after %s changes', async change => {
      vi.useFakeTimers()
      const h = await seeded()
      h.goalContinuity.reattach.mockRejectedValue(new GoalCenterError('unavailable', 'busy', {
        retryable: true, recoveryAction: 'retry', retryAfterMs: 120_000,
      }))
      try {
        h.hydrate()
        await vi.advanceTimersByTimeAsync(120_001)
        let resolve!: (ready: boolean | (() => boolean)) => void
        h.ensureSubscribed.mockImplementation(() => new Promise(done => { resolve = done }))
        const retry = h.api.retryConnection()
        await vi.advanceTimersByTimeAsync(0)
        expect(h.ensureSubscribed).toHaveBeenLastCalledWith(SESSION_KEY, { explicit: true })
        if (change === 'authority') h.authority.value = 'authority-b'
        if (change === 'goal') h.emit({ ...detached(), goalId: 'successor', stateRevision: 2 })
        if (change === 'token') {
          const [key, value] = h.storage.entries()[0]!
          h.storage.setItem(key, JSON.stringify({ ...JSON.parse(value), token: 'new-token' }))
          h.emit({ ...detached(), stateRevision: 2 })
        }
        if (change === 'pause') h.emit({ ...detached(), status: 'paused', stateRevision: 2 })
        if (change === 'clear') h.emit(null)
        if (change === 'navigation') { h.sessionKey.value = 'other'; h.sessionKey.value = SESSION_KEY }
        resolve(change === 'proof' ? () => false : true)
        await vi.advanceTimersByTimeAsync(0)
        if (change === 'proof') h.api.reset()
        await retry
        expect(h.goalContinuity.reattach).toHaveBeenCalledOnce()
      } finally { h.api.reset(); vi.useRealTimers() }
    },
  )
})

describe('Goal operation admission and continuity contracts', () => {
  const actions = ['start', 'takeover', 'resume', 'completed-edit'] as const
  function prepare(action: typeof actions[number]) {
    const h = harness()
    if (action !== 'start') h.api.applyHydration({
      key: SESSION_KEY, sessionId: SESSION_ID, epoch: 1, goalSnapshotStreamSeq: 1,
      goal: goalPayload(action === 'resume' ? 'paused' : action === 'completed-edit' ? 'complete' : 'active', {
        activeTaskId: null, executionState: 'idle',
        continuationDeferredReason: action === 'takeover' ? 'owner_disconnected' : null,
      }),
    })
    const invoke = () => action === 'start' ? h.api.startGoal('Admission goal')
      : action === 'takeover' ? h.api.takeOverConnection()
        : action === 'resume' ? h.api.resume() : h.api.edit('Reactivate completed goal')
    return { ...h, invoke }
  }

  it.each(actions)('waits for current read authority before %s', async action => {
    const h = prepare(action)
    let release!: (value: () => boolean) => void
    h.ensureSubscribed.mockImplementation(() => new Promise(resolve => { release = resolve }))
    const pending = h.invoke()
    await flushAsyncWork()
    expect(h.rpc.call).not.toHaveBeenCalled()
    release(() => true)
    await expect(pending).resolves.toBe(true)
    expect(h.rpc.call).toHaveBeenCalledOnce()
    expect(h.rpc.call.mock.calls[0]![0]).toBe(action === 'start' ? 'goals.set'
      : action === 'takeover' ? 'goals.reattach' : action === 'resume' ? 'goals.resume' : 'goals.edit')
    h.api.reset()
  })

  for (const action of actions) {
    it.each(['revision', 'replacement', 'navigation-return', 'authority-return'] as const)(`fences ${action} after %s during readiness`, async change => {
      const h = prepare(action)
      let release!: (value: () => boolean) => void
      h.ensureSubscribed.mockImplementation(() => new Promise(resolve => { release = resolve }))
      const pending = h.invoke()
      await flushAsyncWork()
      if (change === 'navigation-return') {
        h.sessionKey.value = 'agent:main:webchat:other'
        h.sessionKey.value = SESSION_KEY
      } else if (change === 'authority-return') {
        h.authority.value = 'authority-b'
        h.authority.value = 'authority-a'
      } else {
        h.api.applyHydration({ key: SESSION_KEY, sessionId: SESSION_ID, epoch: 1, goalSnapshotStreamSeq: 2,
          goal: goalPayload(action === 'resume' ? 'paused' : action === 'completed-edit' ? 'complete' : 'active', {
            goalId: change === 'replacement' ? 'replacement-goal' : 'g1', stateRevision: 2,
            activeTaskId: null, executionState: 'idle',
            continuationDeferredReason: action === 'takeover' ? 'owner_disconnected' : null,
          }) })
      }
      release(() => true)
      await expect(pending).resolves.toBe(false)
      expect(h.rpc.call).not.toHaveBeenCalled()
      h.api.reset()
    })
  }

  it('does not project or notify a rejected Start after navigation returns to the same key', async () => {
    const h = prepare('start')
    let reject!: (error: unknown) => void
    h.ensureSubscribed.mockImplementation(() => new Promise((_resolve, fail) => { reject = fail }))
    const pending = h.invoke()
    await flushAsyncWork()
    h.sessionKey.value = 'agent:main:webchat:other'
    h.sessionKey.value = SESSION_KEY
    reject(new GoalCenterError('conflict', 'old response', { details: { current: goalPayload() } }))
    await expect(pending).resolves.toBe(false)
    expect(h.api.goal.value).toBeNull()
    expect(h.notify).not.toHaveBeenCalled()
    expect(h.rpc.call).not.toHaveBeenCalled()
    h.api.reset()
  })

  it.each(['pause', 'clear'] as const)('keeps %s independent of session recovery admission', async action => {
    const h = prepare('takeover')
    h.ensureSubscribed.mockImplementation(() => new Promise(() => {}))
    await expect(h.api[action]()).resolves.toBe(true)
    expect(h.ensureSubscribed).not.toHaveBeenCalled()
    expect(h.rpc.call).toHaveBeenCalledWith(`goals.${action}`, expect.anything())
    h.api.reset()
  })

  it.each([
    ['EXECUTION_LEASE_REQUIRED', 'require-takeover', true],
    ['EXECUTION_LEASE_REQUIRED', 'owner-attached', false],
    ['UNAVAILABLE', 'retry', false],
    ['STALE_GOAL', 'refresh-goal', false],
  ] as const)('composes actual adapter and consumer for terminal %s/%s', async (code, recoveryAction, takeover) => {
    vi.useFakeTimers()
    const storage = new MemoryContinuityStorage()
    const first = harness(storage)
    first.rpc.call.mockResolvedValueOnce(mutation(goalPayload(), { continuityToken: 'continuity-token-1' }))
    await first.api.startGoal('Seed continuity through accepted set')
    const h = harness(storage)
    const request = vi.fn(async () => { throw Object.assign(new Error('Server rejected recovery'), {
      code, retryable: false, accepted: false, details: { recoveryAction },
    }) })
    const adapter = createV4GoalContinuity({ request })
    h.goalContinuity.reattach.mockImplementation((input, options) => adapter.reattach(input, options))
    const hydrate = () => h.api.applyHydration({ key: SESSION_KEY, sessionId: SESSION_ID, epoch: 1,
      goalSnapshotStreamSeq: 1, goal: goalPayload('active', {
        continuationDeferredReason: 'owner_disconnected', executionState: 'idle', activeTaskId: null,
      }) })
    try {
      hydrate()
      await vi.advanceTimersByTimeAsync(0)
      expect(request).toHaveBeenCalledOnce()
      expect(h.ensureSubscribed).toHaveBeenCalledWith(SESSION_KEY, { explicit: false })
      expect(h.api.connectionTakeoverAvailable.value).toBe(takeover)
      h.connectionEpoch.value++
      hydrate()
      await vi.advanceTimersByTimeAsync(121_000)
      expect(request).toHaveBeenCalledOnce()
      expect(h.api.connectionTakeoverAvailable.value).toBe(takeover)
      expect(h.rpc.call).not.toHaveBeenCalled()
    } finally { first.api.reset(); h.api.reset(); adapter.dispose(); vi.useRealTimers() }
  })

  it('resumes hydrated continuity on first authority binding without another hydration or renewed budget', async () => {
    vi.useFakeTimers()
    const storage = new MemoryContinuityStorage()
    const first = harness(storage)
    first.rpc.call.mockResolvedValueOnce(mutation(goalPayload(), { continuityToken: 'continuity-token-1' }))
    await first.api.startGoal('Seed continuity')
    const h = harness(storage, undefined, ref(0), ref([]), ref<string | null>(null))
    const releases: Array<(value: boolean) => void> = []
    h.ensureSubscribed.mockImplementation(() => new Promise(resolve => { releases.push(resolve) }))
    h.api.applyHydration({ key: SESSION_KEY, sessionId: SESSION_ID, epoch: 1, goalSnapshotStreamSeq: 1,
      goal: goalPayload('active', { continuationDeferredReason: 'owner_disconnected', executionState: 'idle', activeTaskId: null }) })
    try {
      await vi.advanceTimersByTimeAsync(1_000)
      h.authority.value = 'authority-a'
      await vi.advanceTimersByTimeAsync(0)
      expect(releases).toHaveLength(2)
      releases[0]!(true)
      await vi.advanceTimersByTimeAsync(0)
      expect(h.rpc.call).not.toHaveBeenCalled()
      h.rpc.call.mockResolvedValueOnce(mutation(goalPayload(), { continuityToken: 'continuity-token-2' }))
      releases[1]!(true)
      await vi.advanceTimersByTimeAsync(0)
      expect(h.rpc.call).toHaveBeenCalledOnce()
      expect(storage.entries()[0]?.[1]).toContain('continuity-token-2')
    } finally { first.api.reset(); h.api.reset(); vi.useRealTimers() }
  })

  it.each(['refresh', 'newer-event', 'navigation-return', 'status-rejected'] as const)('owns one bounded status correction after refresh-goal: %s', async outcome => {
    vi.useFakeTimers()
    const storage = new MemoryContinuityStorage()
    const first = harness(storage)
    first.rpc.call.mockResolvedValueOnce(mutation(goalPayload(), { continuityToken: 'continuity-token-1' }))
    await first.api.startGoal('Seed continuity')
    const h = harness(storage)
    let resolve!: (value: Awaited<ReturnType<typeof h.goalCenter.status>>) => void
    let reject!: (error: unknown) => void
    const status = vi.spyOn(h.goalCenter, 'status').mockImplementation(() => new Promise((done, fail) => { resolve = done; reject = fail }))
    const request = vi.fn(async () => { throw Object.assign(new Error('Session generation changed'), {
      code: 'SESSION_GENERATION_CHANGED', retryable: false, details: { recoveryAction: 'refresh-goal' },
    }) })
    const adapter = createV4GoalContinuity({ request })
    h.goalContinuity.reattach.mockImplementation((input, options) => adapter.reattach(input, options))
    const detached = goalPayload('active', { continuationDeferredReason: 'owner_disconnected', executionState: 'idle', activeTaskId: null })
    const hydrate = () => h.api.applyHydration({ key: SESSION_KEY, sessionId: SESSION_ID, epoch: 1, goalSnapshotStreamSeq: 1, goal: detached })
    try {
      hydrate()
      await vi.advanceTimersByTimeAsync(0)
      expect(status).toHaveBeenCalledOnce()
      if (outcome === 'newer-event') h.handlers.get('session.event.goal')?.({
        sessionKey: SESSION_KEY, sessionId: SESSION_ID, epoch: 1, streamSeq: 2,
        goal: goalPayload('active', { goalId: 'newer-goal', stateRevision: 10 }),
      })
      if (outcome === 'navigation-return') {
        h.sessionKey.value = 'agent:main:webchat:other'
        h.sessionKey.value = SESSION_KEY
      }
      if (outcome === 'status-rejected') reject(new GoalCenterError('unavailable', 'status unavailable', { retryable: true }))
      else resolve({ sessionKey: SESSION_KEY, sessionId: SESSION_ID, epoch: 1, goal: goalPayload('paused', { stateRevision: 2 }) })
      await vi.advanceTimersByTimeAsync(0)
      expect(h.api.goal.value?.goalId ?? null).toBe(outcome === 'navigation-return' ? null : outcome === 'newer-event' ? 'newer-goal' : 'g1')
      if (outcome === 'refresh') expect(h.api.goal.value?.status).toBe('paused')
      if (outcome === 'status-rejected') {
        h.connectionEpoch.value++
        hydrate()
      }
      await vi.advanceTimersByTimeAsync(121_000)
      expect(request).toHaveBeenCalledOnce()
      expect(status).toHaveBeenCalledOnce()
      expect(h.api.connectionTakeoverAvailable.value).toBe(false)
      expect(h.api.reattaching.value).toBe(false)
    } finally { first.api.reset(); h.api.reset(); adapter.dispose(); vi.useRealTimers() }
  })

  it('fences late same-token recovery after authority A-B-A', async () => {
    const storage = new MemoryContinuityStorage()
    const first = harness(storage)
    first.rpc.call.mockResolvedValueOnce(mutation(goalPayload(), { continuityToken: 'continuity-token-1' }))
    await first.api.startGoal('Seed continuity')
    const h = harness(storage)
    let resolve!: (value: ReturnType<typeof mutation>) => void
    h.rpc.call.mockImplementationOnce(() => new Promise(done => { resolve = done }))
    h.api.applyHydration({ key: SESSION_KEY, sessionId: SESSION_ID, epoch: 1, goalSnapshotStreamSeq: 1,
      goal: goalPayload('active', { continuationDeferredReason: 'owner_disconnected', executionState: 'idle', activeTaskId: null }) })
    await vi.waitFor(() => expect(resolve).toBeDefined())
    h.authority.value = 'authority-b'
    h.authority.value = 'authority-a'
    resolve(mutation(goalPayload('active', { continuationDeferredReason: null }), { continuityToken: 'late-token' }))
    await flushAsyncWork()
    expect(h.api.goal.value).toBeNull()
    expect(storage.entries()[0]?.[1]).not.toContain('late-token')
    first.api.reset(); h.api.reset()
  })
})

describe('useChatGoals', () => {
  it('joins bootstrap authority during takeover without replacing its subscription consumer', async () => {
    const scope = effectScope()
    const h = scope.run(() => harness())!
    const metadata: SessionReadMetadata = {
      sessionKey: SESSION_KEY, workspaceId: null, projectWorkspace: null, projectWorkspaceDeferred: false,
      activeTaskGroupIds: [], runModeLock: { locked: false, runMode: null, source: null, additional: {} },
      pendingUserInputs: [], collaboration: null, routing: null, currentPlan: null, activePlanRun: null,
      goal: null, goalSnapshotStreamSeq: null, tasks: [], activeTask: null, lastTask: null,
      runStatus: 'idle', queuedTaskIds: [], epoch: 1, hydrationComplete: true, deferredFields: [], additional: {},
    }
    let completeLive!: (live: SessionReadLive) => void
    const pendingLive = new Promise<SessionReadLive>(resolve => { completeLive = resolve })
    const live: SessionReadLive = { sessionKey: SESSION_KEY, activity: 'idle', activeTaskId: null,
      initialMetadata: metadata, snapshot: null, reloadRequired: null }
    const lease = {
      criticalRequestsQueued: Promise.resolve(), live: pendingLive, metadata: Promise.resolve(metadata),
      history: {}, retryMetadata: async () => metadata, reconcile: async () => live, close: async () => {},
    } as unknown as SessionReadLease
    let current: SessionReadLease | null = null
    const subscription = scope.run(() => useChatSessionSubscription({
      sessionReadLeaseReader: { current: () => current },
      conversationRuntime: createConversationRuntime(), sessionKey: h.sessionKey, lastStreamSeq: ref(0),
      runStatus: ref({ status: 'idle', label: 'Idle', task: null }), isStreaming: ref(false),
      hasActiveInterrupt: ref(false), activeStreamTaskId: ref(''), activeTaskGroups: ref(new Set<string>()),
      sessionRunStatus: () => ({ status: 'idle', label: 'Idle', task: null }),
      startStreaming() {}, loadHistory: async () => ({ ok: true }),
      resetStreamIdleTimer() {}, resetStreamLiveTurnState() {},
    }))!
    const subscribeSession = vi.fn(subscription.subscribeSession)
    const reconcileSession = vi.fn(subscription.reconcileSession)
    const bootstrap = scope.run(() => useChatSessionBootstrap({
      sessionKey: h.sessionKey, connectionState: ref('connected'),
      sessionReadLifecycle: { open: () => { current = lease; return lease }, current: () => current },
      loadHistory: async () => ({ ok: true }), subscribeSession, reconcileSession,
      cancelHistory() {}, cancelSubscription: subscription.cancelActiveSubscription,
    }))!
    h.ensureSubscribed.mockImplementation(createGoalSessionReadiness({
      sessionKey: h.sessionKey, waitForLiveReady: bootstrap.waitForLiveReady,
    }))
    try {
      h.api.applyHydration({ key: SESSION_KEY, sessionId: SESSION_ID, epoch: 1,
        goalSnapshotStreamSeq: 1, goal: goalPayload('active', { continuationDeferredReason: 'owner_disconnected' }) })
      const initial = bootstrap.startSessionBootstrap()
      await nextTick()
      const takeover = h.api.takeOverConnection()
      await flushAsyncWork()
      expect(h.goalContinuity.reattach).not.toHaveBeenCalled()
      expect(subscribeSession).toHaveBeenCalledOnce()

      completeLive(live)
      await expect(takeover).resolves.toBe(true)
      await expect(initial.live).resolves.toMatchObject({ authoritative: true })
      expect(bootstrap.livePhase.value).toBe('ready')
      expect(subscribeSession).toHaveBeenCalledOnce()
      expect(reconcileSession).not.toHaveBeenCalled()
      expect(h.goalContinuity.reattach).toHaveBeenCalledOnce()
      expect(h.goalContinuity.reattach).toHaveBeenCalledWith(expect.objectContaining({ takeover: true }))
    } finally { bootstrap.cancelSessionBootstrap(); scope.stop() }
  })

  it('rejects Goal admission when navigation supersedes a pending coordinator result', async () => {
    const sessionKey = ref(SESSION_KEY)
    let complete!: (value: () => boolean) => void
    const pending = new Promise<() => boolean>(resolve => { complete = resolve })
    const waitForLiveReady = vi.fn(() => pending)
    const ensureSubscribed = createGoalSessionReadiness({ sessionKey, waitForLiveReady })
    const admitted = ensureSubscribed(SESSION_KEY)
    sessionKey.value = 'agent:main:other'
    complete(() => true)
    await expect(admitted).resolves.toBe(false)
    expect(waitForLiveReady).toHaveBeenCalledOnce()
  })

  it.each(['start', 'takeover'] as const)('keeps %s behind a fresh read requested after Goal began waiting', async action => {
    const scope = effectScope()
    const h = scope.run(() => harness())!
    type Outcome = { authoritative: boolean; live: boolean; backgroundOnly: boolean }
    const ready: Outcome = { authoritative: true, live: false, backgroundOnly: false }
    let complete!: (value: Outcome) => void
    let completeFresh!: (value: Outcome) => void
    let current: SessionReadLease | null = null
    const reconcileSession = vi.fn(() => new Promise<Outcome>(resolve => { completeFresh = resolve }))
    const bootstrap = scope.run(() => useChatSessionBootstrap({
      sessionKey: h.sessionKey, connectionState: ref('connected'),
      sessionReadLifecycle: {
        current: () => current,
        open: () => current = { criticalRequestsQueued: Promise.resolve(), close: async () => {} } as SessionReadLease,
      },
      loadHistory: async () => ({ ok: true }),
      subscribeSession: () => new Promise<Outcome>(resolve => { complete = resolve }),
      reconcileSession, cancelHistory() {}, cancelSubscription() {},
    }))!
    h.ensureSubscribed.mockImplementation(createGoalSessionReadiness({
      sessionKey: h.sessionKey, waitForLiveReady: bootstrap.waitForLiveReady,
    }))
    try {
      if (action === 'takeover') h.api.applyHydration({ key: SESSION_KEY, sessionId: SESSION_ID, epoch: 1,
        goalSnapshotStreamSeq: 1, goal: goalPayload('active', { continuationDeferredReason: 'owner_disconnected' }) })
      const initial = bootstrap.startSessionBootstrap()
      const command = action === 'start' ? h.api.startGoal('Wait for current recovery') : h.api.takeOverConnection()
      await flushAsyncWork()
      const recovery = bootstrap.retryLive(false)
      complete(ready)
      await initial.live
      await vi.waitFor(() => expect(reconcileSession).toHaveBeenCalledOnce())
      expect(bootstrap.livePhase.value).not.toBe('ready')
      expect(h.rpc.call).not.toHaveBeenCalled()
      completeFresh({ ...ready })
      await recovery
      await expect(command).resolves.toBe(true)
      expect(h.rpc.call).toHaveBeenCalledOnce()
      expect(h.rpc.call.mock.calls[0]![0]).toBe(action === 'start' ? 'goals.set' : 'goals.reattach')
    } finally { bootstrap.cancelSessionBootstrap(); scope.stop() }
  })

  it.each(['start', 'takeover'] as const)('rechecks a revoked readiness proof at %s dispatch', async action => {
    const h = harness()
    if (action === 'takeover') h.api.applyHydration({ key: SESSION_KEY, sessionId: SESSION_ID, epoch: 1,
      goalSnapshotStreamSeq: 1, goal: goalPayload('active', { continuationDeferredReason: 'owner_disconnected' }) })
    h.ensureSubscribed.mockResolvedValue(() => false)
    const command = action === 'start' ? h.api.startGoal('Revoked read') : h.api.takeOverConnection()
    await expect(command).resolves.toBe(false)
    expect(h.rpc.call).not.toHaveBeenCalled()
  })

  it.each(['start', 'takeover'] as const)('keeps %s behind a gap queued by the ready publication itself', async action => {
    const scope = effectScope()
    const h = scope.run(() => harness())!
    type Outcome = { authoritative: boolean; live: boolean; backgroundOnly: boolean }
    const ready: Outcome = { authoritative: true, live: false, backgroundOnly: false }
    let current: SessionReadLease | null = null
    let completeFresh!: (value: Outcome) => void
    let command: Promise<boolean> | undefined
    let recovery: Promise<Outcome> | undefined
    let invalidated = false
    const reconcileSession = vi.fn(() => new Promise<Outcome>(resolve => { completeFresh = resolve }))
    const bootstrap = scope.run(() => useChatSessionBootstrap({
      sessionKey: h.sessionKey, connectionState: ref('connected'),
      sessionReadLifecycle: {
        current: () => current,
        open: () => current = { criticalRequestsQueued: Promise.resolve(), close: async () => {} } as SessionReadLease,
      },
      loadHistory: async () => ({ ok: true }),
      subscribeSession: async () => ({ ...ready }),
      reconcileSession, cancelHistory() {}, cancelSubscription() {},
    }))!
    h.ensureSubscribed.mockImplementation(createGoalSessionReadiness({
      sessionKey: h.sessionKey, waitForLiveReady: bootstrap.waitForLiveReady,
    }))
    scope.run(() => watch(bootstrap.livePhase, phase => {
      if (phase !== 'ready' || invalidated) return
      invalidated = true
      // The read has published its result, but its promise/finally has not
      // settled. Queue a real successor and join it through Goal admission.
      recovery = bootstrap.retryLive(false)
      command = action === 'start'
        ? h.api.startGoal('Wait for the publication-window gap')
        : h.api.takeOverConnection()
    }, { flush: 'sync' }))
    try {
      if (action === 'takeover') h.api.applyHydration({ key: SESSION_KEY, sessionId: SESSION_ID, epoch: 1,
        goalSnapshotStreamSeq: 1, goal: goalPayload('active', { continuationDeferredReason: 'owner_disconnected' }) })
      await bootstrap.startSessionBootstrap().live
      await vi.waitFor(() => expect(reconcileSession).toHaveBeenCalledOnce())
      expect(bootstrap.livePhase.value).toBe('connecting')
      expect(h.rpc.call).not.toHaveBeenCalled()
      completeFresh({ ...ready })
      await recovery
      await expect(command).resolves.toBe(true)
      expect(h.rpc.call).toHaveBeenCalledOnce()
      expect(h.rpc.call.mock.calls[0]![0]).toBe(action === 'start' ? 'goals.set' : 'goals.reattach')
    } finally { bootstrap.cancelSessionBootstrap(); scope.stop() }
  })

  it('freezes local references before subscription and retains them in the accepted message', async () => {
    const path = 'C:\\Users\\tester\\Downloads\\report.pdf'
    const references = ref([path])
    const { api, rpc, ensureSubscribed, onSetAccepted } = harness(undefined, undefined, ref(0), references)
    let subscribed!: (value: boolean) => void
    ensureSubscribed.mockImplementationOnce(() => new Promise(resolve => { subscribed = resolve }))
    const objective = `Review the report\n${path}`
    const pending = api.startGoal(objective)
    await flushAsyncWork()
    references.value = ['C:\\Users\\tester\\Downloads\\new-draft.pdf']
    subscribed(true)

    expect(await pending).toBe(true)
    expect(rpc.call).toHaveBeenCalledWith('goals.set', expect.objectContaining({
      objective,
      localPathReferences: [path],
    }))
    expect(onSetAccepted).toHaveBeenCalledWith(expect.objectContaining({
      objective,
      localPathReferences: [path],
    }))
  })

  it('does not label unrelated objective text with stale composer references', async () => {
    const { api, rpc } = harness(undefined, undefined, ref(0), ref(['C:\\report.pdf']))
    expect(await api.startGoal('A separate objective')).toBe(true)
    expect(rpc.call.mock.calls[0]![1]).not.toHaveProperty('localPathReferences')
  })

  it('reuses an unknown Goal set receipt and message identity on retry', async () => {
    const { api, rpc, currentEpoch } = harness()
    currentEpoch.value = 1
    rpc.call.mockRejectedValueOnce(new Error('Connection lost after acceptance'))
    expect(await api.startGoal('Synthetic uncertain Goal')).toBe(false)
    const first = rpc.call.mock.calls[0]![1]
    expect(await api.startGoal('Synthetic uncertain Goal')).toBe(true)
    expect(rpc.call.mock.calls[1]![1]).toEqual(first)
    expect(await api.startGoal('Synthetic uncertain Goal')).toBe(true)
    expect(rpc.call.mock.calls[2]![1].clientRequestId).not.toBe(first.clientRequestId)
  })

  it('does not replay a removed Goal when the same objective is intentionally created again', async () => {
    const { api, rpc, currentEpoch } = harness()
    currentEpoch.value = 1
    rpc.call.mockRejectedValueOnce(new Error('Connection lost after acceptance'))
    await api.startGoal('Synthetic clear and recreate')
    const first = rpc.call.mock.calls[0]![1]
    api.applyHydration({ key: SESSION_KEY, epoch: 1, goal: goalPayload('active') })
    rpc.call.mockResolvedValueOnce(mutation(null))
    await api.clear()
    await api.startGoal('Synthetic clear and recreate')
    expect(rpc.call.mock.calls[2]![1].clientRequestId).not.toBe(first.clientRequestId)
  })

  it('adopts same-revision usage events and preserves pause after late receipts become complete', () => {
    const { api, handlers } = harness()
    api.applyHydration({ key: SESSION_KEY, epoch: 1, goalSnapshotStreamSeq: 10,
      goal: goalPayload('paused', { stateRevision: 3, usageCoverage: 'partial_usage', pauseReason: 'user', usage: { totalTokens: 10 } }) })
    const emit = (stream_seq: number, usageCoverage: string, totalTokens: number) => handlers.get('session.event.goal')?.({
      session_key: SESSION_KEY, session_id: SESSION_ID, epoch: 1, stream_seq,
      goal: goalPayload('paused', { stateRevision: 3, usageCoverage, pauseReason: 'user', usage: { totalTokens } }),
    })
    emit(12, 'complete', 45)
    expect(api.goal.value).toMatchObject({ status: 'paused', usageCoverage: 'complete', usage: { totalTokens: 45 } })
    emit(11, 'partial_usage', 10)
    expect(api.goal.value).toMatchObject({ status: 'paused', usageCoverage: 'complete', usage: { totalTokens: 45 } })
  })

  it.each(['subscribe', 'hydrate', 'retry'] as const)(
    'keeps newer Goal events when %s metadata carries an earlier questionnaire cursor',
    async source => {
      const metadata = {
        key: SESSION_KEY,
        epoch: 1,
        workspaceId: null,
        projectWorkspace: null,
        projectWorkspaceDeferred: false,
        active_task_group_ids: [],
        run_mode_lock: { locked: false },
        pendingUserInputs: [],
        collaboration: null,
        routing: null,
        currentPlan: null,
        activePlanRun: null,
        goal: null,
        goalSnapshotStreamSeq: 0,
        tasks: [],
        active_task: null,
        last_task: null,
        run_status: 'idle',
        hydration_complete: true,
        deferred_fields: [],
      }
      const wireResults: Record<string, unknown> = {
        'sessions.messages.subscribe': {
          ...metadata,
          subscribed: true,
          stream_generation: 'stream-1',
          current_stream_seq: 0,
          replay_complete: true,
          replay_gap_reason: null,
          replayed_count: 0,
          ...(source !== 'subscribe' ? {
            hydration_complete: false,
            deferred_fields: ['goal', 'goalSnapshotStreamSeq'],
          } : {}),
        },
        'sessions.messages.snapshot': {
          key: SESSION_KEY,
          task_id: null,
          stream_generation: 'stream-1',
          current_stream_seq: 0,
          events: [],
        },
        'sessions.messages.hydrate': metadata,
        'sessions.messages.unsubscribe': null,
      }
      const transport: Parameters<typeof createV4SessionReadPort>[0] = {
        generation: 1,
        request<T>(
          method: string,
          _params?: Record<string, unknown>,
          options?: TransportCallOptions,
        ): Promise<T> {
          options?.onSent?.(1)
          return Promise.resolve(wireResults[method] as T)
        },
      }
      const lease = createV4SessionReadPort(transport).open({
        sessionKey: SESSION_KEY,
        includeInitialHistory: false,
        resumeFrom: { streamGeneration: null, streamSeq: 0 },
        signal: new AbortController().signal,
      })
      try {
        const live = await lease.live
        const projected = source === 'subscribe'
          ? live.initialMetadata
          : source === 'retry'
            ? await lease.retryMetadata()
            : await lease.metadata
        // A generation-less live event is an authoritative legacy transport
        // signal. A questionnaire read's older cursor must not change it back.
        const h = harness(undefined, ref<string | null>(null))
        h.api.applyHydration(live.initialMetadata)
        const emit = (revision: number, streamSeq: number, complete = false) => {
          h.handlers.get('session.event.goal')!({
            sessionKey: SESSION_KEY,
            sessionId: SESSION_ID,
            epoch: 1,
            streamSeq,
            goal: goalPayload(complete ? 'complete' : 'active', {
              stateRevision: revision,
              progressRevision: revision - 1,
              ...(complete ? {
                activeTaskId: null,
                executionState: 'idle',
                terminalReason: 'complete',
              } : {}),
            }),
          })
        }
        emit(2, 1)
        emit(3, 2)
        expect(h.api.activeGoal.value?.stateRevision).toBe(3)
        expect(h.api.applyHydration(projected)).toBe(false)
        expect(h.api.activeGoal.value?.stateRevision).toBe(3)
        emit(4, 3, true)
        expect(h.api.activeGoal.value).toBeNull()
        expect(h.api.lastGoal.value).toMatchObject({
          goalId: 'g1', status: 'complete', stateRevision: 4,
        })
      } finally {
        await lease.close()
      }
    },
  )

  it('creates and edits objectives without execution settings or capability discovery', async () => {
    const { api, rpc, goalCenter } = harness()
    api.arm()
    expect(await api.startGoal('Refactor the module')).toBe(true)
    expect(await api.edit('Refactor safely')).toBe(true)
    expect(goalCenter.capabilities).not.toHaveBeenCalled()
    for (const [, params] of rpc.call.mock.calls) {
      expect(params).not.toHaveProperty('tokenBudget')
      expect(params).not.toHaveProperty('executionPolicy')
    }
  })

  it('keeps an uncertain request identity when the connection changes', async () => {
    const { api, rpc, goalCenter, connectionEpoch } = harness()
    rpc.call.mockRejectedValueOnce(new Error('Connection lost after acceptance'))
    expect(await api.startGoal('Preserve the uncertain Goal identity')).toBe(false)
    const original = rpc.call.mock.calls[0]![1]
    connectionEpoch.value += 1
    expect(await api.startGoal('Preserve the uncertain Goal identity')).toBe(true)
    expect(rpc.call.mock.calls[1]![1]).toEqual(original)
    expect(goalCenter.capabilities).not.toHaveBeenCalled()
  })

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
      const { api, rpc, goalContinuity, ensureSubscribed, onSetAccepted } = harness()
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
      expect(goalContinuity.subscribe).toHaveBeenCalledWith(expect.any(Function))
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
    await vi.waitFor(() => expect(refreshed.goalContinuity.reattach).toHaveBeenCalledOnce())
    expect(refreshed.goalContinuity.reattach).toHaveBeenCalledWith({
      sessionKey: SESSION_KEY,
      sessionId: SESSION_ID,
      epoch: 1,
      expectedGoalId: 'g1',
      continuityToken: 'continuity-token-1',
      sourceKind: 'web',
    }, expect.objectContaining({ signal: expect.any(AbortSignal) }))
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
    await vi.waitFor(() => expect(resolveReattach).toBeDefined())
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

  it('retries transient reattachment with the same token without takeover or Resume', async () => {
    vi.useFakeTimers()
    const storage = new MemoryContinuityStorage()
    const first = harness(storage)
    first.rpc.call.mockResolvedValueOnce(mutation(goalPayload(), {
      continuityToken: 'continuity-token-1',
    }))
    await first.api.startGoal('Refactor the module')

    const refreshed = harness(storage)
    refreshed.rpc.call.mockRejectedValueOnce(new GoalCenterError('unavailable', 'connection changed', { retryable: true }))
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
    expect(refreshed.api.connectionTakeoverAvailable.value).toBe(false)

    refreshed.rpc.call.mockResolvedValueOnce(mutation(goalPayload('active', {
      continuationDeferredReason: null,
      activeTaskId: null,
      executionState: 'idle',
    }), {
      continuityToken: 'continuity-token-2',
    }))
    await vi.advanceTimersByTimeAsync(500)
    expect(refreshed.goalContinuity.reattach).toHaveBeenLastCalledWith({
      sessionKey: SESSION_KEY,
      sessionId: SESSION_ID,
      epoch: 1,
      expectedGoalId: 'g1',
      continuityToken: 'continuity-token-1',
      sourceKind: 'web',
    }, expect.objectContaining({ signal: expect.any(AbortSignal) }))
    expect(refreshed.rpc.call).not.toHaveBeenCalledWith('goals.resume', expect.anything())
    expect(refreshed.api.activeGoal.value?.continuationDeferredReason).toBeNull()
    expect(storage.entries()[0]?.[1]).toContain('continuity-token-2')
    vi.useRealTimers()
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
    const error = new GoalCenterError(
      'conflict',
      'The Goal still owns an unsettled task',
      { reason: 'busy', retryable: true },
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
