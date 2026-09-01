import { ref } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { RpcTimeoutError } from '@/lib/rpc'
import type {
  SessionReadLease,
  SessionReadLifecycle,
} from '@/modules/sessionReadLifecycle'
import type { SessionSubscriptionOutcome } from './useChatSessionSubscription'
import {
  autoSendDraftIsUnchanged,
  shouldRetrySessionPhase,
  type SessionBootstrapPhaseContext,
  type SessionPhaseResult,
} from './sessionBootstrapContract'
import { useChatSessionBootstrap } from './useChatSessionBootstrap'

const LIVE_READY: SessionSubscriptionOutcome = {
  authoritative: true,
  live: false,
  backgroundOnly: false,
}

function createBootstrap(overrides: {
  loadHistory?: (
    context: SessionBootstrapPhaseContext,
    retry: boolean,
  ) => Promise<SessionPhaseResult | void>
  subscribeSession?: (
    context: SessionBootstrapPhaseContext,
  ) => Promise<SessionSubscriptionOutcome>
  criticalRequestsQueued?: () => Promise<void>
} = {}) {
  const loadHistoryImplementation = overrides.loadHistory || (async () => ({ ok: true }))
  const loadHistory = vi.fn(async (
    context: SessionBootstrapPhaseContext,
    retry: boolean,
  ) => loadHistoryImplementation(context, retry))
  const subscribeImplementation = overrides.subscribeSession || (async () => LIVE_READY)
  const subscribeSession = vi.fn(subscribeImplementation)
  const cancelHistory = vi.fn()
  const cancelSubscription = vi.fn()
  const closeLease = vi.fn(async () => undefined)
  let currentLease: SessionReadLease | null = null
  const openSessionRead = vi.fn(() => {
    void currentLease?.close()
    currentLease = ({
      criticalRequestsQueued: overrides.criticalRequestsQueued?.() ?? Promise.resolve(),
      live: Promise.resolve({}),
      metadata: Promise.resolve({}),
      history: {},
      retryMetadata: async () => ({}),
      close: closeLease,
    }) as unknown as SessionReadLease
    return currentLease
  })
  const sessionReadLifecycle = {
    open: openSessionRead,
    current: () => currentLease,
  } as SessionReadLifecycle
  const sessionKey = ref('agent:main:webchat:bootstrap-test')
  const api = useChatSessionBootstrap({
    sessionKey,
    sessionReadLifecycle,
    loadHistory,
    subscribeSession,
    cancelHistory,
    cancelSubscription,
  })
  return {
    api,
    loadHistory,
    subscribeSession,
    cancelHistory,
    cancelSubscription,
    closeLease,
    openSessionRead,
  }
}

afterEach(() => {
  vi.useRealTimers()
})

describe('useChatSessionBootstrap', () => {
  it('releases optional traffic after the lease queues critical frames, not responses', async () => {
    let releaseHistory!: (result: SessionPhaseResult) => void
    let releaseLive!: (result: SessionSubscriptionOutcome) => void
    const history = new Promise<SessionPhaseResult>(resolve => { releaseHistory = resolve })
    const live = new Promise<SessionSubscriptionOutcome>(resolve => { releaseLive = resolve })
    const { api } = createBootstrap({
      loadHistory: async () => history,
      subscribeSession: async () => live,
    })

    const run = api.startSessionBootstrap()
    await run.criticalRequestsQueued

    expect(api.historyPhase.value).toBe('loading')
    expect(api.livePhase.value).toBe('connecting')

    releaseHistory({ ok: true })
    releaseLive(LIVE_READY)
    await Promise.all([run.history, run.live])
  })

  it('retries a history-only timeout without reopening the live lease', async () => {
    vi.useFakeTimers()
    let attempt = 0
    const { api, loadHistory, subscribeSession, openSessionRead } = createBootstrap({
      loadHistory: async () => {
        attempt += 1
        return attempt === 1
          ? { ok: false, error: new RpcTimeoutError('chat.history', 7_000) }
          : { ok: true }
      },
    })

    const run = api.startSessionBootstrap()
    await vi.runAllTimersAsync()
    await Promise.all([run.history, run.live])

    expect(loadHistory).toHaveBeenCalledTimes(2)
    expect(subscribeSession).toHaveBeenCalledOnce()
    expect(openSessionRead).toHaveBeenCalledOnce()
    expect(api.historyPhase.value).toBe('ready')
  })

  it('replaces a disconnected lease with a fenced logical generation', async () => {
    let opened = 0
    let releaseReplacement!: () => void
    const { api, openSessionRead, subscribeSession } = createBootstrap({
      criticalRequestsQueued: () => {
        opened += 1
        return opened === 1
          ? Promise.resolve()
          : new Promise<void>(resolve => { releaseReplacement = resolve })
      },
    })

    const initial = api.startSessionBootstrap()
    await initial.live
    const replacement = api.handleConnectionState('disconnected')!

    expect(replacement.generation).toBeGreaterThan(initial.generation)
    expect(api.isSessionBootstrapCurrent(initial.generation)).toBe(false)
    expect(api.isSessionBootstrapCurrent(replacement.generation)).toBe(true)
    expect(openSessionRead).toHaveBeenCalledTimes(2)
    expect(subscribeSession).toHaveBeenCalledTimes(2)

    let replacementQueued = false
    void replacement.criticalRequestsQueued.then(() => { replacementQueued = true })
    await Promise.resolve()
    expect(replacementQueued).toBe(false)
    releaseReplacement()
    await replacement.criticalRequestsQueued
    expect(replacementQueued).toBe(true)
  })

  it('coalesces connected with the in-flight reconnect replacement', async () => {
    let releaseLive!: (result: SessionSubscriptionOutcome) => void
    let calls = 0
    const { api, openSessionRead } = createBootstrap({
      subscribeSession: async () => {
        calls += 1
        if (calls === 1) return LIVE_READY
        return new Promise(resolve => { releaseLive = resolve })
      },
    })
    await api.startSessionBootstrap().live

    const disconnected = api.handleConnectionState('disconnected')!
    const connected = api.handleConnectionState('connected')!

    expect(connected.generation).toBe(disconnected.generation)
    expect(openSessionRead).toHaveBeenCalledTimes(2)
    releaseLive(LIVE_READY)
    await disconnected.live
  })

  it('keeps a degraded lease bounded until an explicit connected recovery', async () => {
    let available = false
    const { api, openSessionRead } = createBootstrap({
      subscribeSession: async () => available
        ? LIVE_READY
        : { authoritative: false, live: false, backgroundOnly: false },
    })
    const initial = api.startSessionBootstrap()
    await Promise.all([initial.history, initial.live])

    const disconnected = api.handleConnectionState('disconnected')!
    expect(disconnected.generation).toBe(initial.generation)
    expect(openSessionRead).toHaveBeenCalledOnce()

    available = true
    const recovered = api.handleConnectionState('connected')!
    await recovered.live
    expect(recovered.generation).toBeGreaterThan(initial.generation)
    expect(openSessionRead).toHaveBeenCalledTimes(2)
    expect(api.livePhase.value).toBe('ready')
  })

  it('keeps live retry ownership at the lease boundary while history retries once', async () => {
    const historyContexts: SessionBootstrapPhaseContext[] = []
    const liveContexts: SessionBootstrapPhaseContext[] = []
    const { api } = createBootstrap({
      loadHistory: async context => {
        historyContexts.push(context)
        return { ok: false, error: new RpcTimeoutError('chat.history', 7_000) }
      },
      subscribeSession: async context => {
        liveContexts.push(context)
        return {
          authoritative: false,
          live: false,
          backgroundOnly: false,
          error: new RpcTimeoutError('sessions.messages.subscribe', 7_000),
        }
      },
    })

    const run = api.startSessionBootstrap()
    await Promise.all([run.history, run.live])

    expect(historyContexts.map(context => context.attempt)).toEqual([0, 1])
    expect(liveContexts.map(context => context.attempt)).toEqual([0])
    expect(api.historyPhase.value).toBe('error')
    expect(api.livePhase.value).toBe('degraded')
  })

  it('retries STORAGE_BUSY after the server delay without disturbing live', async () => {
    vi.useFakeTimers()
    const busy = Object.assign(new Error('storage busy'), {
      code: 'STORAGE_BUSY',
      retryable: true,
      retry_after_ms: 100,
    })
    let attempt = 0
    const { api, loadHistory, subscribeSession } = createBootstrap({
      loadHistory: async () => {
        attempt += 1
        return attempt === 1 ? { ok: false, error: busy } : { ok: true }
      },
    })

    const run = api.startSessionBootstrap()
    await run.live
    expect(loadHistory).toHaveBeenCalledOnce()
    await vi.advanceTimersByTimeAsync(100)
    await run.history

    expect(loadHistory).toHaveBeenCalledTimes(2)
    expect(subscribeSession).toHaveBeenCalledOnce()
  })

  it('retries failed history manually on the same lease', async () => {
    let recover = false
    const failure = Object.assign(new Error('history unavailable'), {
      code: 'HISTORY_UNAVAILABLE',
      retryable: true,
    })
    const { api, loadHistory, subscribeSession, openSessionRead } = createBootstrap({
      loadHistory: async () => recover ? { ok: true } : { ok: false, error: failure },
    })

    const initial = api.startSessionBootstrap()
    await Promise.all([initial.history, initial.live])
    recover = true
    await api.retryHistory()

    expect(loadHistory).toHaveBeenCalledTimes(3)
    expect(subscribeSession).toHaveBeenCalledOnce()
    expect(openSessionRead).toHaveBeenCalledOnce()
    expect(api.historyPhase.value).toBe('ready')
  })

  it('invalidates consumer projections before closing the owned lease', async () => {
    let releaseHistory!: (result: SessionPhaseResult) => void
    let releaseLive!: (result: SessionSubscriptionOutcome) => void
    const history = new Promise<SessionPhaseResult>(resolve => { releaseHistory = resolve })
    const live = new Promise<SessionSubscriptionOutcome>(resolve => { releaseLive = resolve })
    const { api, cancelHistory, cancelSubscription, closeLease } = createBootstrap({
      loadHistory: async () => history,
      subscribeSession: async () => live,
    })
    const run = api.startSessionBootstrap()
    cancelHistory.mockClear()
    cancelSubscription.mockClear()

    api.cancelSessionBootstrap()

    expect(cancelHistory).toHaveBeenCalledOnce()
    expect(cancelSubscription).toHaveBeenCalledOnce()
    expect(closeLease).toHaveBeenCalledOnce()
    expect(api.historyPhase.value).toBe('idle')
    expect(api.livePhase.value).toBe('idle')

    releaseHistory({ ok: true })
    releaseLive(LIVE_READY)
    await Promise.all([run.history, run.live])
    expect(api.historyPhase.value).toBe('idle')
    expect(api.livePhase.value).toBe('idle')
  })

  it('retries live through a fresh logical run without reloading history', async () => {
    const { api, loadHistory, subscribeSession, openSessionRead } = createBootstrap()
    const initial = api.startSessionBootstrap()
    await Promise.all([initial.history, initial.live])

    await api.retryLive()

    expect(loadHistory).toHaveBeenCalledOnce()
    expect(subscribeSession).toHaveBeenCalledTimes(2)
    expect(openSessionRead).toHaveBeenCalledTimes(2)
    expect(api.historyPhase.value).toBe('ready')
  })

  it('upgrades a same-session live-only run without reopening its lease', async () => {
    const { api, loadHistory, subscribeSession, openSessionRead } = createBootstrap()
    await api.startSessionBootstrap({ includeHistory: false }).live
    await api.startSessionBootstrap({ includeHistory: true }).history

    expect(subscribeSession).toHaveBeenCalledOnce()
    expect(loadHistory).toHaveBeenCalledOnce()
    expect(openSessionRead).toHaveBeenCalledOnce()
  })

  it('defers reconnect recovery during handoff and replays only after rollback', async () => {
    const { api, subscribeSession } = createBootstrap()
    const initial = api.startSessionBootstrap()
    await Promise.all([initial.history, initial.live])

    api.setSessionHandoffTarget('agent:main:webchat:target', 1)
    expect(api.handleConnectionState('disconnected')?.deferred).toBe(true)
    expect(api.handleConnectionState('connected')?.deferred).toBe(true)
    expect(subscribeSession).toHaveBeenCalledOnce()

    const resumed = api.setSessionHandoffTarget(null, 1)
    await resumed?.live
    expect(subscribeSession).toHaveBeenCalledTimes(2)
  })

  it('discards stale reconnect transitions after the target commits', async () => {
    const { api, subscribeSession } = createBootstrap()
    const initial = api.startSessionBootstrap()
    await Promise.all([initial.history, initial.live])

    api.setSessionHandoffTarget('agent:main:webchat:target', 2)
    api.handleConnectionState('disconnected')
    api.handleConnectionState('connected')

    expect(api.setSessionHandoffTarget(null, 2, 'committed')).toBeUndefined()
    expect(subscribeSession).toHaveBeenCalledOnce()
  })

  it('tracks draft mutation and retryable phase contracts independently of transport', () => {
    const attachment = { id: 1 }
    expect(autoSendDraftIsUnchanged(
      'draft', 'draft', [attachment], [attachment], 1, 1,
    )).toBe(true)
    expect(autoSendDraftIsUnchanged(
      'draft', 'draft', [attachment], [attachment], 1, 2,
    )).toBe(false)
    expect(shouldRetrySessionPhase(Object.assign(new Error('temporarily unavailable'), {
      code: 'UNAVAILABLE',
      retryable: true,
    }))).toBe(true)
    expect(shouldRetrySessionPhase(Object.assign(new Error('not authorized'), {
      code: 'FORBIDDEN',
      retryable: false,
    }))).toBe(false)
  })
})
