import { ref } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { RpcTimeoutError } from '@/lib/rpc'
import { useChatSessionBootstrap } from './useChatSessionBootstrap'
import type {
  SessionBootstrapPhaseContext,
  SessionPhaseResult,
} from './sessionBootstrapContract'
import {
  autoSendDraftIsUnchanged,
  shouldRetrySessionPhase,
} from './sessionBootstrapContract'
import type { SessionSubscriptionOutcome } from './useChatSessionSubscription'

const LIVE_READY: SessionSubscriptionOutcome = {
  authoritative: true,
  live: false,
  backgroundOnly: false,
}
const UNAVAILABLE_FOR_TEST: SessionSubscriptionOutcome = {
  authoritative: false,
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
  markCriticalRequestsSent?: boolean
} = {}) {
  const loadHistoryImplementation = overrides.loadHistory || (async () => ({ ok: true }))
  const loadHistory = vi.fn(async (
    context: SessionBootstrapPhaseContext,
    retry: boolean,
  ) => {
    // Production marks immediately after RpcClient synchronously sends the
    // history frame.
    if (overrides.markCriticalRequestsSent !== false) {
      context.markHistoryRequestSent?.(context.attempt + 1)
    }
    return loadHistoryImplementation(context, retry)
  })
  const subscribeImplementation = overrides.subscribeSession || (async () => LIVE_READY)
  const subscribeSession = vi.fn(async (context: SessionBootstrapPhaseContext) => {
    // The production subscription marks immediately after its subscribe frame
    // is synchronously sent. Test doubles must model the same wire boundary.
    if (overrides.markCriticalRequestsSent !== false) {
      context.markLiveSubscribeSent?.(context.attempt + 1)
    }
    return subscribeImplementation(context)
  })
  const cancelHistory = vi.fn()
  const cancelSubscription = vi.fn()
  const unsubscribeSession = vi.fn()
  const sessionKey = ref('agent:main:webchat:bootstrap-test')
  const api = useChatSessionBootstrap({
    sessionKey,
    loadHistory,
    subscribeSession,
    cancelHistory,
    cancelSubscription,
    unsubscribeSession,
  })
  return {
    api,
    sessionKey,
    loadHistory,
    subscribeSession,
    cancelHistory,
    cancelSubscription,
    unsubscribeSession,
  }
}

afterEach(() => {
  vi.useRealTimers()
})

describe('useChatSessionBootstrap', () => {
  it('releases optional traffic after critical frames are queued, not responses', async () => {
    let resolveHistory!: (result: SessionPhaseResult) => void
    let resolveLive!: (result: SessionSubscriptionOutcome) => void
    const history = new Promise<SessionPhaseResult>(resolve => {
      resolveHistory = resolve
    })
    const live = new Promise<SessionSubscriptionOutcome>(resolve => {
      resolveLive = resolve
    })
    const { api } = createBootstrap({
      loadHistory: async () => history,
      subscribeSession: async () => live,
    })

    const run = api.startSessionBootstrap()
    await run.criticalRequestsQueued

    expect(api.historyPhase.value).toBe('loading')
    expect(api.livePhase.value).toBe('connecting')

    resolveHistory({ ok: true })
    resolveLive(LIVE_READY)
    await Promise.all([run.history, run.live])
  })

  it('retries a history-only timeout without waiting for a new live registration', async () => {
    vi.useFakeTimers()
    let historyAttempt = 0
    const { api, loadHistory, subscribeSession } = createBootstrap({
      loadHistory: async () => {
        historyAttempt += 1
        return historyAttempt === 1
          ? {
              ok: false,
              error: new RpcTimeoutError('chat.history', 7_000),
            }
          : { ok: true }
      },
    })

    const run = api.startSessionBootstrap()
    await vi.runAllTimersAsync()
    await Promise.all([run.history, run.live])

    expect(loadHistory).toHaveBeenCalledTimes(2)
    expect(subscribeSession).toHaveBeenCalledOnce()
    expect(api.historyPhase.value).toBe('ready')
    expect(api.livePhase.value).toBe('ready')
  })

  it('queues replacement live registration before retrying legacy history', async () => {
    const order: string[] = []
    let resolveFirstHistory!: (result: SessionPhaseResult) => void
    const { api } = createBootstrap({
      subscribeSession: async context => {
        order.push(`subscribe:${context.attempt}`)
        return LIVE_READY
      },
      loadHistory: async context => {
        order.push(`history:${context.attempt}`)
        if (context.attempt === 0) {
          return new Promise(resolve => {
            resolveFirstHistory = resolve
          })
        }
        return { ok: true }
      },
    })

    const run = api.startSessionBootstrap()
    await run.live
    api.handleConnectionState('disconnected')
    resolveFirstHistory({
      ok: false,
      error: new RpcTimeoutError('chat.history', 7_000),
    })
    await Promise.all([run.history, api.handleConnectionState('connected')!.live])

    expect(order).toEqual([
      'subscribe:0',
      'history:0',
      'subscribe:1',
      'history:1',
    ])
    expect(api.livePhase.value).toBe('ready')
    expect(api.historyPhase.value).toBe('ready')
  })

  it('rearms the critical queue for a replacement socket', async () => {
    let resolveFirstHistory!: (result: SessionPhaseResult) => void
    let historyAttempt = 0
    const connectionFailure = new Error('connection closed')
    const { api, loadHistory, subscribeSession } = createBootstrap({
      loadHistory: async () => {
        historyAttempt += 1
        if (historyAttempt === 1) {
          return new Promise(resolve => {
            resolveFirstHistory = resolve
          })
        }
        return { ok: true }
      },
    })

    const initial = api.startSessionBootstrap()
    await initial.criticalRequestsQueued
    await initial.live

    const recovery = api.handleConnectionState('disconnected')!
    let replacementQueued = false
    void recovery.criticalRequestsQueued.then(() => {
      replacementQueued = true
    })
    await Promise.resolve()
    expect(replacementQueued).toBe(false)

    resolveFirstHistory({ ok: false, error: connectionFailure })
    await recovery.criticalRequestsQueued
    await Promise.all([recovery.history, recovery.live])

    expect(loadHistory).toHaveBeenCalledTimes(2)
    expect(subscribeSession).toHaveBeenCalledTimes(2)
    expect(replacementQueued).toBe(true)
  })

  it('keeps existing queue waiters blocked across a same-run reconnect', async () => {
    let resolveFirstLive!: (result: SessionSubscriptionOutcome) => void
    let liveAttempt = 0
    const { api } = createBootstrap({
      markCriticalRequestsSent: false,
      subscribeSession: async context => {
        liveAttempt += 1
        if (liveAttempt === 1) {
          return new Promise(resolve => {
            resolveFirstLive = resolve
          })
        }
        context.markLiveSubscribeSent?.(2)
        return LIVE_READY
      },
      loadHistory: async context => {
        context.markHistoryRequestSent?.(2)
        return { ok: true }
      },
    })

    const initial = api.startSessionBootstrap()
    let initialQueued = false
    void initial.criticalRequestsQueued.then(() => {
      initialQueued = true
    })

    const firstRecovery = api.handleConnectionState('disconnected')!
    let firstReplacementQueued = false
    void firstRecovery.criticalRequestsQueued.then(() => {
      firstReplacementQueued = true
    })
    const latestRecovery = api.handleConnectionState('disconnected')!
    let latestReplacementQueued = false
    void latestRecovery.criticalRequestsQueued.then(() => {
      latestReplacementQueued = true
    })
    await Promise.resolve()
    expect(initialQueued).toBe(false)
    expect(firstReplacementQueued).toBe(false)
    expect(latestReplacementQueued).toBe(false)

    resolveFirstLive({
      ...UNAVAILABLE_FOR_TEST,
      error: new Error('connection closed'),
    })
    await latestRecovery.criticalRequestsQueued
    await firstRecovery.criticalRequestsQueued
    await Promise.all([latestRecovery.history, latestRecovery.live])

    expect(initialQueued).toBe(true)
    expect(firstReplacementQueued).toBe(true)
    expect(latestReplacementQueued).toBe(true)
    expect(liveAttempt).toBe(2)
  })

  it('cancels delayed auto-send when text or attachments changed', () => {
    const attachment = { id: 1 }
    expect(autoSendDraftIsUnchanged(
      'draft', 'draft', [attachment], [attachment], 1, 1,
    )).toBe(true)
    expect(autoSendDraftIsUnchanged(
      'draft', 'edited', [attachment], [attachment], 1, 2,
    )).toBe(false)
    expect(autoSendDraftIsUnchanged(
      'draft', '', [attachment], [attachment], 1, 2,
    )).toBe(false)
    expect(autoSendDraftIsUnchanged(
      'draft', 'draft', [], [attachment], 1, 2,
    )).toBe(false)
    expect(autoSendDraftIsUnchanged(
      'draft', 'draft', [attachment], [{ id: 1 }], 1, 2,
    )).toBe(false)
    // Editing and then restoring the same visible draft still means the user
    // took control while bootstrap was pending.
    expect(autoSendDraftIsUnchanged(
      'draft', 'draft', [attachment], [attachment], 1, 3,
    )).toBe(false)
  })

  it('honors a retryable server contract for idempotent bootstrap phases', () => {
    expect(shouldRetrySessionPhase(Object.assign(new Error('temporarily unavailable'), {
      code: 'UNAVAILABLE',
      retryable: true,
    }))).toBe(true)
    expect(shouldRetrySessionPhase(Object.assign(new Error('not authorized'), {
      code: 'FORBIDDEN',
      retryable: false,
    }))).toBe(false)
  })

  it('terminates both phases after one bounded automatic retry', async () => {
    const historyContexts: SessionBootstrapPhaseContext[] = []
    const liveContexts: SessionBootstrapPhaseContext[] = []
    const { api } = createBootstrap({
      loadHistory: async context => {
        historyContexts.push(context)
        return {
          ok: false,
          error: new RpcTimeoutError('chat.history', 7_000),
        }
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
    expect(liveContexts.map(context => context.attempt)).toEqual([0, 1])
    expect(liveContexts.map(context => context.skipSnapshot)).toEqual([false, false])
    expect(historyContexts.every(context =>
      context.attemptDeadlineAt <= context.deadlineAt
      && context.attemptDeadlineAt - Date.now() <= 7_000,
    )).toBe(true)
    expect(api.historyPhase.value).toBe('error')
    expect(api.livePhase.value).toBe('degraded')
  })

  it('puts live subscribe ahead of history on every replacement socket', async () => {
    const order: string[] = []
    const connectionFailure = new Error('connection closed')
    const { api } = createBootstrap({
      subscribeSession: async context => {
        order.push(`subscribe:${context.attempt}`)
        return context.attempt === 0
          ? { ...UNAVAILABLE_FOR_TEST, error: connectionFailure }
          : LIVE_READY
      },
      loadHistory: async context => {
        order.push(`history:${context.attempt}`)
        return context.attempt === 0
          ? { ok: false, error: connectionFailure }
          : { ok: true }
      },
    })

    const run = api.startSessionBootstrap()
    await Promise.all([run.history, run.live])

    expect(order).toEqual([
      'subscribe:0',
      'history:0',
      'subscribe:1',
      'history:1',
    ])
    expect(api.livePhase.value).toBe('ready')
    expect(api.historyPhase.value).toBe('ready')
  })

  it('retries STORAGE_BUSY once after the server-provided delay without degrading live', async () => {
    vi.useFakeTimers()
    const busy = Object.assign(new Error('storage busy'), {
      code: 'STORAGE_BUSY',
      retryable: true,
      retry_after_ms: 100,
    })
    let historyAttempt = 0
    const { api, loadHistory, subscribeSession } = createBootstrap({
      loadHistory: async () => {
        historyAttempt += 1
        return historyAttempt === 1
          ? { ok: false, error: busy }
          : { ok: true }
      },
    })

    const run = api.startSessionBootstrap()
    await run.live
    expect(loadHistory).toHaveBeenCalledOnce()
    expect(api.livePhase.value).toBe('ready')

    await vi.advanceTimersByTimeAsync(100)
    await run.history

    expect(loadHistory).toHaveBeenCalledTimes(2)
    expect(subscribeSession).toHaveBeenCalledOnce()
    expect(api.historyPhase.value).toBe('ready')
    expect(api.livePhase.value).toBe('ready')
  })

  it('retries one failed phase manually without restarting the healthy phase', async () => {
    let recoverHistory = false
    const terminal = Object.assign(new Error('history unavailable'), {
      code: 'HISTORY_UNAVAILABLE',
      retryable: true,
    })
    const { api, loadHistory, subscribeSession } = createBootstrap({
      loadHistory: async () => (
        recoverHistory
          ? { ok: true }
          : { ok: false, error: terminal }
      ),
    })

    const first = api.startSessionBootstrap()
    await Promise.all([first.history, first.live])
    expect(api.historyPhase.value).toBe('error')
    expect(api.livePhase.value).toBe('ready')

    recoverHistory = true
    await api.retryHistory()

    expect(loadHistory).toHaveBeenCalledTimes(3)
    expect(subscribeSession).toHaveBeenCalledOnce()
    expect(api.historyPhase.value).toBe('ready')
    expect(api.livePhase.value).toBe('ready')
  })

  it('invalidates and aborts both phases before best-effort unsubscribe', async () => {
    let resolveHistory!: (result: SessionPhaseResult) => void
    let resolveLive!: (result: SessionSubscriptionOutcome) => void
    const history = new Promise<SessionPhaseResult>(resolve => {
      resolveHistory = resolve
    })
    const live = new Promise<SessionSubscriptionOutcome>(resolve => {
      resolveLive = resolve
    })
    const { api, cancelHistory, cancelSubscription, unsubscribeSession } = createBootstrap({
      loadHistory: async () => history,
      subscribeSession: async () => live,
    })

    const run = api.startSessionBootstrap()
    expect(api.livePhase.value).toBe('connecting')

    api.cancelSessionBootstrap()

    expect(cancelHistory).toHaveBeenCalledOnce()
    expect(cancelSubscription).toHaveBeenCalledOnce()
    expect(unsubscribeSession).toHaveBeenCalledWith(
      'agent:main:webchat:bootstrap-test',
    )
    expect(api.historyPhase.value).toBe('idle')
    expect(api.livePhase.value).toBe('idle')

    resolveHistory({ ok: true })
    resolveLive(LIVE_READY)
    await Promise.all([run.history, run.live])

    expect(api.historyPhase.value).toBe('idle')
    expect(api.livePhase.value).toBe('idle')
  })

  it('routes a completed connection recovery through one new coordinated run', async () => {
    const { api, loadHistory, subscribeSession } = createBootstrap()
    const first = api.startSessionBootstrap()
    await Promise.all([first.history, first.live])

    const resumed = api.handleConnectionState('disconnected')
    expect(api.livePhase.value).toBe('connecting')
    await Promise.all([resumed!.history, resumed!.live])
    api.handleConnectionState('connected')

    expect(loadHistory).toHaveBeenCalledTimes(2)
    expect(subscribeSession).toHaveBeenCalledTimes(2)
    expect(api.historyPhase.value).toBe('ready')
    expect(api.livePhase.value).toBe('ready')
  })

  it('does not restart a terminal bootstrap when its final timeout reconnects', async () => {
    let resolveHistory!: (result: SessionPhaseResult) => void
    let resolveLive!: (result: SessionSubscriptionOutcome) => void
    let historyCalls = 0
    let liveCalls = 0
    const timeout = new RpcTimeoutError('session bootstrap', 7_000)
    const { api, loadHistory, subscribeSession } = createBootstrap({
      loadHistory: async () => {
        historyCalls += 1
        if (historyCalls === 1) {
          return new Promise(resolve => {
            resolveHistory = resolve
          })
        }
        return { ok: false, error: timeout }
      },
      subscribeSession: async () => {
        liveCalls += 1
        if (liveCalls === 1) {
          return new Promise(resolve => {
            resolveLive = resolve
          })
        }
        return { ...UNAVAILABLE_FOR_TEST, error: timeout }
      },
    })

    const run = api.startSessionBootstrap()
    await vi.waitFor(() => {
      expect(resolveHistory).toBeTypeOf('function')
      expect(resolveLive).toBeTypeOf('function')
    })
    api.handleConnectionState('disconnected')
    resolveHistory({ ok: false, error: timeout })
    resolveLive({ ...UNAVAILABLE_FOR_TEST, error: timeout })
    await Promise.all([run.history, run.live])

    api.handleConnectionState('disconnected')
    expect(api.livePhase.value).toBe('degraded')
    api.handleConnectionState('connected')
    await Promise.resolve()

    expect(loadHistory).toHaveBeenCalledTimes(2)
    expect(subscribeSession).toHaveBeenCalledTimes(2)
    expect(api.historyPhase.value).toBe('error')
    expect(api.livePhase.value).toBe('degraded')
  })

  it('does not grant a degraded live phase more attempts when history recycles the socket', async () => {
    let resolveHistory!: (result: SessionPhaseResult) => void
    const history = new Promise<SessionPhaseResult>(resolve => {
      resolveHistory = resolve
    })
    const timeout = new RpcTimeoutError('sessions.messages.subscribe', 7_000)
    const { api, subscribeSession } = createBootstrap({
      loadHistory: async () => history,
      subscribeSession: async () => ({
        ...UNAVAILABLE_FOR_TEST,
        error: timeout,
      }),
    })

    const run = api.startSessionBootstrap()
    await run.live
    expect(subscribeSession).toHaveBeenCalledTimes(2)
    expect(api.livePhase.value).toBe('degraded')

    api.handleConnectionState('disconnected')
    await Promise.resolve()

    expect(subscribeSession).toHaveBeenCalledTimes(2)
    expect(api.livePhase.value).toBe('degraded')
    resolveHistory({ ok: true })
    await run.history
  })

  it('preserves the live attempt count across repeated disconnects in one bootstrap run', async () => {
    let resolveHistory!: (result: SessionPhaseResult) => void
    const history = new Promise<SessionPhaseResult>(resolve => {
      resolveHistory = resolve
    })
    const { api, subscribeSession } = createBootstrap({
      loadHistory: async () => history,
      subscribeSession: async () => LIVE_READY,
    })

    const run = api.startSessionBootstrap()
    await run.live
    expect(subscribeSession).toHaveBeenCalledOnce()

    await api.handleConnectionState('disconnected')!.live
    expect(subscribeSession).toHaveBeenCalledTimes(2)
    expect(api.livePhase.value).toBe('ready')

    await api.handleConnectionState('disconnected')!.live
    expect(subscribeSession).toHaveBeenCalledTimes(2)
    expect(api.livePhase.value).toBe('degraded')

    resolveHistory({ ok: true })
    await run.history
  })

  it('gives live a fresh outage budget when a manual history retry recycles its socket', async () => {
    let resolveManualHistory!: (result: SessionPhaseResult) => void
    let historyCalls = 0
    let liveCalls = 0
    const firstLiveFailure = new Error('connection closed')
    const terminalHistory = Object.assign(new Error('history unavailable'), {
      code: 'HISTORY_UNAVAILABLE',
    })
    const { api, subscribeSession } = createBootstrap({
      loadHistory: async () => {
        historyCalls += 1
        if (historyCalls === 1) {
          return { ok: false, error: terminalHistory }
        }
        return new Promise(resolve => {
          resolveManualHistory = resolve
        })
      },
      subscribeSession: async () => {
        liveCalls += 1
        return liveCalls === 1
          ? { ...UNAVAILABLE_FOR_TEST, error: firstLiveFailure }
          : LIVE_READY
      },
    })

    const initial = api.startSessionBootstrap()
    await Promise.all([initial.history, initial.live])
    expect(subscribeSession).toHaveBeenCalledTimes(2)
    expect(api.livePhase.value).toBe('ready')
    expect(api.historyPhase.value).toBe('error')

    const historyRetry = api.retryHistory()
    await vi.waitFor(() => expect(resolveManualHistory).toBeTypeOf('function'))
    const outage = api.handleConnectionState('disconnected')
    await outage!.live

    expect(subscribeSession).toHaveBeenCalledTimes(3)
    expect(api.livePhase.value).toBe('ready')
    resolveManualHistory({ ok: true })
    await historyRetry
  })

  it('skips the second snapshot only after the first attempt reached a terminal snapshot stage', async () => {
    const contexts: SessionBootstrapPhaseContext[] = []
    let call = 0
    const timeout = new RpcTimeoutError('sessions.messages.snapshot', 3_000)
    const { api } = createBootstrap({
      subscribeSession: async context => {
        contexts.push(context)
        call += 1
        return call === 1
          ? {
              ...UNAVAILABLE_FOR_TEST,
              error: timeout,
              skipSnapshotOnRetry: true,
            }
          : LIVE_READY
      },
    })

    await api.startSessionBootstrap().live

    expect(contexts.map(context => context.skipSnapshot)).toEqual([false, true])
    expect(api.livePhase.value).toBe('ready')
  })

  it('does not reset an outage budget while the socket repeatedly flaps', async () => {
    let resolveRecovery!: (result: SessionSubscriptionOutcome) => void
    let liveCalls = 0
    const { api, subscribeSession } = createBootstrap({
      subscribeSession: async () => {
        liveCalls += 1
        if (liveCalls === 1) return LIVE_READY
        return new Promise(resolve => {
          resolveRecovery = resolve
        })
      },
    })
    const initial = api.startSessionBootstrap()
    await Promise.all([initial.history, initial.live])

    const recovery = api.handleConnectionState('disconnected')
    api.handleConnectionState('connected')
    api.handleConnectionState('disconnected')
    api.handleConnectionState('connected')

    expect(subscribeSession).toHaveBeenCalledTimes(2)
    resolveRecovery(LIVE_READY)
    await recovery!.live
    expect(api.livePhase.value).toBe('ready')
  })

  it('upgrades a same-session live-only bootstrap to include history', async () => {
    const { api, loadHistory, subscribeSession } = createBootstrap()
    const liveOnly = api.startSessionBootstrap({ includeHistory: false })
    await liveOnly.live

    const full = api.startSessionBootstrap({ includeHistory: true })
    await full.history

    expect(subscribeSession).toHaveBeenCalledOnce()
    expect(loadHistory).toHaveBeenCalledOnce()
    expect(api.historyPhase.value).toBe('ready')
  })
})
