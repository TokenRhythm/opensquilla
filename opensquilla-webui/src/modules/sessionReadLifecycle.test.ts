import { afterEach, describe, expect, it, vi } from 'vitest'
import { InMemorySessionReadPortAdapter } from '@/adapters/inMemory/sessionReadPortInMemory'
import { createConversationRuntime } from './conversationRuntime'
import { createConversationSubscriptionLifecycle } from './conversationSubscriptionLifecycle'
import {
  createSessionReadLifecycle,
  createSessionReadLifecycleFactory,
  SessionReadLeaseClosedError,
  type SessionReadHistoryPage,
  type SessionReadMetadata,
  type SessionReadPort,
  type SessionReadPortLease,
  type SessionReadPortLive,
} from './sessionReadLifecycle'

function metadata(sessionKey: string, routingMode = 'recommended'): SessionReadMetadata {
  return Object.freeze({
    sessionKey,
    workspaceId: 'workspace-1',
    projectWorkspace: Object.freeze({ id: 'workspace-1', displayName: 'Workspace One' }),
    projectWorkspaceDeferred: false,
    activeTaskGroupIds: Object.freeze([]),
    runModeLock: Object.freeze({
      locked: true,
      runMode: 'safe',
      source: 'profile',
      additional: Object.freeze({}),
    }),
    pendingUserInputs: Object.freeze([]),
    collaboration: null,
    routing: Object.freeze({ mode: routingMode }),
    currentPlan: null,
    activePlanRun: null,
    goal: null,
    goalSnapshotStreamSeq: null,
    tasks: Object.freeze([]),
    activeTask: null,
    lastTask: null,
    runStatus: 'idle',
    queuedTaskIds: Object.freeze([]),
    epoch: 3,
    hydrationComplete: true,
    deferredFields: Object.freeze([]),
    additional: Object.freeze({}),
  })
}

function page(label: string, patch: Partial<SessionReadHistoryPage> = {}): SessionReadHistoryPage {
  return Object.freeze({
    messages: Object.freeze([Object.freeze({
      id: `${label}:id`,
      messageId: `${label}:message`,
      transcriptId: `${label}:transcript`,
      role: 'assistant',
      text: label,
      createdAt: 1_725_199_200,
      reasoningContent: 'reasoning',
      routerDecision: Object.freeze({ tier: 'c1' }),
      artifacts: Object.freeze([Object.freeze({ artifactId: 'artifact-1' })]),
      toolCalls: Object.freeze([Object.freeze({ name: 'read' })]),
      timeline: Object.freeze([Object.freeze({ kind: 'thinking' })]),
      attachments: Object.freeze([Object.freeze({ attachmentId: 'attachment-1' })]),
      promptAnnotations: Object.freeze([Object.freeze({ kind: 'cache' })]),
      provenance: Object.freeze({
        kind: 'forwarded',
        sourceSessionKey: 'source',
        sourceTool: 'delegate',
      }),
      turnContext: Object.freeze({
        turnId: null,
        promotedTurnId: null,
        appliedIteration: null,
        activityMarkers: Object.freeze([]),
        additional: Object.freeze({ runMode: 'safe' }),
      }),
      usage: Object.freeze({ totalTokens: 3 }),
      model: 'model-1',
      inputTokens: 1,
      outputTokens: 2,
      additional: Object.freeze({ additiveField: true }),
    })]),
    hasMore: false,
    oldestCursor: `${label}:oldest`,
    newestCursor: `${label}:newest`,
    scope: 'complete',
    loadedCount: 1,
    pageSize: 100,
    canonicalAvailable: true,
    canonicalComplete: true,
    compactionSummaries: Object.freeze([]),
    turnOutcomes: Object.freeze([]),
    additional: Object.freeze({}),
    ...patch,
  })
}

function live(sessionKey: string, patch: Partial<SessionReadPortLive> = {}): SessionReadPortLive {
  return Object.freeze({
    sessionKey,
    activity: 'idle',
    activeTaskId: null,
    initialMetadata: metadata(sessionKey),
    snapshot: null,
    cursor: Object.freeze({
      sessionKey,
      sessionEpoch: 3,
      streamGeneration: 'stream-1',
      currentStreamSeq: 9,
      replayComplete: true,
      replayGapReason: null,
    }),
    snapshotCursor: null,
    ...patch,
  })
}

function fixture(sessionKey: string, patch: Record<string, unknown> = {}) {
  return {
    sessionKey,
    live: live(sessionKey),
    metadata: metadata(sessionKey),
    latestHistory: page('latest'),
    ...patch,
  }
}

function harness(fixtures: ConstructorParameters<typeof InMemorySessionReadPortAdapter>[0]) {
  const adapter = new InMemorySessionReadPortAdapter(fixtures)
  const lifecycle = createSessionReadLifecycle({
    port: adapter,
    runtime: createConversationRuntime(),
    subscriptions: createConversationSubscriptionLifecycle<SessionReadPortLease>(),
  })
  return { adapter, lifecycle }
}

afterEach(() => {
  vi.useRealTimers()
})

describe('SessionReadLifecycle', () => {
  it('exposes only the conversation read lease and delegates runtime ownership', async () => {
    const { adapter, lifecycle } = harness([fixture('alpha')])

    const lease = lifecycle.open({ sessionKey: 'alpha' })
    expect(lifecycle.current()).toBe(lease)

    expect(Object.keys(lease).sort()).toEqual([
      'close',
      'criticalRequestsQueued',
      'history',
      'live',
      'metadata',
      'retryMetadata',
    ])
    expect(Object.keys(lease.history).sort()).toEqual(['after', 'before', 'latest'])
    for (const transportControl of [
      'changes',
      'expectedGeneration',
      'initial',
      'onSent',
      'purpose',
      'ready',
      'readEarlier',
      'supports',
    ]) {
      expect(transportControl in lease).toBe(false)
    }
    await expect(lease.live).resolves.toMatchObject({
      sessionKey: 'alpha',
      reloadRequired: null,
    })
    expect(adapter.openRecords).toEqual([{
      sessionKey: 'alpha',
      includeInitialHistory: true,
      resumeFrom: { streamGeneration: null, streamSeq: 0 },
    }])

    await lease.close()
    expect(lifecycle.current()).toBeNull()
  })

  it('binds the private Port while accepting the existing runtime owner', async () => {
    const adapter = new InMemorySessionReadPortAdapter([fixture('alpha')])
    const factory = createSessionReadLifecycleFactory(adapter)
    const lifecycle = factory.create({
      cursor: createConversationRuntime(),
      subscriptions: createConversationSubscriptionLifecycle<SessionReadPortLease>(),
    })

    const lease = lifecycle.open({ sessionKey: 'alpha' })
    await expect(lease.live).resolves.toMatchObject({ sessionKey: 'alpha' })
    expect(lifecycle.current()).toBe(lease)

    await lease.close()
    expect(lifecycle.current()).toBeNull()
  })

  it('keeps live independent from metadata hydration and history reads', async () => {
    vi.useFakeTimers()
    const { lifecycle } = harness([fixture('alpha', {
      metadataDelayMs: 100,
      historyDelayMs: 100,
    })])
    const lease = lifecycle.open({ sessionKey: 'alpha' })
    let metadataSettled = false
    let historySettled = false
    void lease.metadata.finally(() => { metadataSettled = true })
    void lease.history.latest().finally(() => { historySettled = true })

    await expect(lease.live).resolves.toMatchObject({ sessionKey: 'alpha' })
    expect(metadataSettled).toBe(false)
    expect(historySettled).toBe(false)

    await vi.advanceTimersByTimeAsync(100)
    expect(metadataSettled).toBe(true)
    expect(historySettled).toBe(true)
    await lease.close()
  })

  it('provides rich latest, before and after history without owning cursors', async () => {
    const before = page('before', { hasMore: true, canonicalAvailable: false })
    const after = page('after', { scope: 'compacted' })
    const { adapter, lifecycle } = harness([fixture('alpha', {
      history: [
        { direction: 'before', cursor: 'older', page: before },
        { direction: 'after', cursor: 'newer', page: after },
      ],
    })])
    const lease = lifecycle.open({ sessionKey: 'alpha', includeInitialHistory: false })

    await expect(lease.history.latest()).resolves.toEqual(page('latest'))
    await expect(lease.history.before('older', { limit: 25 })).resolves.toEqual(before)
    await expect(lease.history.after('newer', { limit: 10 })).resolves.toEqual(after)
    expect(adapter.historyRecords.map(({ signal: _signal, ...record }) => record)).toEqual([
      { sessionKey: 'alpha', direction: 'latest', cursor: null, limit: 100 },
      { sessionKey: 'alpha', direction: 'before', cursor: 'older', limit: 25 },
      { sessionKey: 'alpha', direction: 'after', cursor: 'newer', limit: 10 },
    ])
    expect(() => lease.history.before(' ')).toThrow(TypeError)
    await expect(lease.history.latest({ limit: 201 })).rejects.toBeInstanceOf(RangeError)

    await lease.close()
  })

  it('forwards semantic history budgets and links caller cancellation to the lease owner', async () => {
    vi.useFakeTimers()
    const { adapter, lifecycle } = harness([fixture('alpha', { historyDelayMs: 100 })])
    const lease = lifecycle.open({ sessionKey: 'alpha' })
    await lease.live
    const caller = new AbortController()
    const pending = lease.history.latest({
      signal: caller.signal,
      budgetMs: 2_500,
      deadlineAt: 10_000,
    })

    await Promise.resolve()
    await Promise.resolve()
    caller.abort()

    await expect(pending).rejects.toMatchObject({ name: 'AbortError' })
    expect(adapter.historyRecords).toHaveLength(1)
    expect(adapter.historyRecords[0]).toMatchObject({
      sessionKey: 'alpha',
      direction: 'latest',
      cursor: null,
      limit: 100,
      budgetMs: 2_500,
      deadlineAt: 10_000,
    })
    expect(adapter.historyRecords[0]?.signal.aborted).toBe(true)

    await lease.close()
  })

  it('keeps metadata and history errors out of the live failure domain', async () => {
    const { lifecycle } = harness([fixture('alpha', {
      metadataError: new Error('hydrate unavailable'),
      historyError: new Error('history unavailable'),
    })])
    const lease = lifecycle.open({ sessionKey: 'alpha' })

    await expect(lease.live).resolves.toMatchObject({ sessionKey: 'alpha' })
    await expect(lease.metadata).rejects.toThrow('hydrate unavailable')
    await expect(lease.history.latest()).rejects.toThrow('history unavailable')
    await expect(lease.live).resolves.toMatchObject({ sessionKey: 'alpha' })

    await lease.close()
  })

  it('retries metadata without reopening the live lease', async () => {
    const recovered = metadata('alpha', 'manual')
    const { adapter, lifecycle } = harness([fixture('alpha', {
      metadataError: new Error('first hydration failed'),
      retryMetadata: recovered,
    })])
    const lease = lifecycle.open({ sessionKey: 'alpha' })

    await expect(lease.metadata).rejects.toThrow('first hydration failed')
    await expect(lease.retryMetadata()).resolves.toEqual(recovered)
    expect(adapter.metadataRetryCount).toBe(1)
    expect(adapter.openRecords).toHaveLength(1)

    await lease.close()
  })

  it('uses shared cursor policy and resumes a replacement lease for the same session', async () => {
    const { adapter, lifecycle } = harness([fixture('alpha', {
      live: live('alpha', {
        cursor: Object.freeze({
          sessionKey: 'alpha',
          streamGeneration: 'replacement',
          currentStreamSeq: 12,
          replayComplete: false,
          replayGapReason: 'stream_generation_changed',
        }),
      }),
    })])
    const first = lifecycle.open({ sessionKey: 'alpha' })

    await expect(first.live).resolves.toMatchObject({
      reloadRequired: 'generationChanged',
    })
    const second = lifecycle.open({ sessionKey: 'alpha' })
    await second.live
    expect(adapter.openRecords[1]?.resumeFrom).toEqual({
      streamGeneration: 'replacement',
      streamSeq: 12,
    })
    await expect(first.history.latest()).rejects.toMatchObject({
      name: 'SessionReadLeaseClosedError',
      reason: 'superseded',
    })
    expect(adapter.closeCount).toBe(1)

    await second.close()
  })

  it('finishes the prior generation-pinned release before opening a replacement', async () => {
    const order: string[] = []
    let resolveRelease!: () => void
    const releaseGate = new Promise<void>(resolve => { resolveRelease = resolve })
    let openCount = 0
    const port: SessionReadPort = {
      open(request) {
        const ordinal = ++openCount
        order.push(`open:${ordinal}`)
        const result = live(request.sessionKey)
        const hydrated = metadata(request.sessionKey)
        return {
          criticalRequestsQueued: Promise.resolve(),
          live: Promise.resolve(result),
          metadata: Promise.resolve(hydrated),
          readHistory: async () => page('latest'),
          retryMetadata: async () => hydrated,
          close: async () => {
            order.push(`close:${ordinal}:start`)
            if (ordinal === 1) await releaseGate
            order.push(`close:${ordinal}:end`)
          },
        }
      },
    }
    const lifecycle = createSessionReadLifecycle({
      port,
      runtime: createConversationRuntime(),
      subscriptions: createConversationSubscriptionLifecycle<SessionReadPortLease>(),
    })
    const first = lifecycle.open({ sessionKey: 'alpha' })
    await first.live

    const second = lifecycle.open({ sessionKey: 'alpha' })
    await Promise.resolve()
    expect(order).toEqual(['open:1', 'close:1:start'])

    resolveRelease()
    await second.live
    expect(order).toEqual([
      'open:1',
      'close:1:start',
      'close:1:end',
      'open:2',
    ])

    await second.close()
  })

  it('aborts stale phases and closes a lease exactly once', async () => {
    vi.useFakeTimers()
    const { adapter, lifecycle } = harness([fixture('alpha', {
      metadataDelayMs: 100,
      historyDelayMs: 100,
    })])
    const lease = lifecycle.open({ sessionKey: 'alpha' })
    await lease.live
    const pendingMetadata = lease.metadata
    const pendingHistory = lease.history.latest()

    await lease.close()
    await lease.close()

    await expect(pendingMetadata).rejects.toMatchObject({ name: 'AbortError' })
    await expect(pendingHistory).rejects.toMatchObject({ name: 'AbortError' })
    await expect(lease.retryMetadata()).rejects.toBeInstanceOf(SessionReadLeaseClosedError)
    expect(adapter.closeCount).toBe(1)
    expect(adapter.activeLeaseCount).toBe(0)
  })

  it('releases the Port when live acquisition fails', async () => {
    const { adapter, lifecycle } = harness([fixture('alpha', {
      liveError: new Error('subscription rejected'),
    })])
    const lease = lifecycle.open({ sessionKey: 'alpha' })

    await expect(lease.live).rejects.toThrow('subscription rejected')
    await vi.waitFor(() => expect(adapter.closeCount).toBe(1))
    expect(adapter.activeLeaseCount).toBe(0)
    await expect(lease.history.latest()).rejects.toBeInstanceOf(SessionReadLeaseClosedError)
  })
})
