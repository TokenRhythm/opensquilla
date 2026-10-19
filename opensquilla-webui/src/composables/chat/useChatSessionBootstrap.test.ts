import { effectScope, ref, shallowRef, watch, type Ref } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type {
  SessionReadLease,
  SessionReadLifecycle,
  SessionReadOpenRequest,
} from '@/modules/sessionReadLifecycle'
import { SessionReadFailure } from '@/modules/sessionReadLifecycle'
import type { SessionSubscriptionOutcome, SessionMetadataState, SessionMetadataOutcome } from './useChatSessionSubscription'
import {
  autoSendDraftIsUnchanged,
  shouldRetrySessionPhase,
  sessionRecoverySucceeded,
  type SessionBootstrapPhaseContext,
  type SessionPhaseResult,
} from './sessionBootstrapContract'
import { useChatSessionBootstrap, type UseChatSessionBootstrapOptions } from './useChatSessionBootstrap'
import { createConversationEventTransport } from '@/adapters/gateway/conversationEventTransport'
import type { TransportEventHandler } from '@/adapters/gateway/transportTypes'
import { TransportFlowV4 } from '@/adapters/gateway/transportFlowV4'

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
  reconcileSession?: (context: SessionBootstrapPhaseContext) => Promise<SessionSubscriptionOutcome>
  connectionState?: Ref<string>
  metadata?: UseChatSessionBootstrapOptions['metadata']
  isProvisionalDraft?: () => boolean
  incidentScope?: () => string
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
  const openSessionRead = vi.fn((_request: SessionReadOpenRequest) => {
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
  const retireCurrentLease = () => {
    currentLease = null
  }
  const sessionKey = ref('agent:main:webchat:bootstrap-test')
  const api = useChatSessionBootstrap({
    sessionKey,
    sessionReadLifecycle,
    loadHistory,
    subscribeSession,
    reconcileSession: overrides.reconcileSession,
    connectionState: overrides.connectionState,
    metadata: overrides.metadata,
    isProvisionalDraft: overrides.isProvisionalDraft,
    incidentScope: overrides.incidentScope,
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
    retireCurrentLease,
    currentLease: () => currentLease,
    sessionKey,
  }
}

afterEach(() => {
  vi.useRealTimers()
})

function createManualMetadataRecovery(overrides: Parameters<typeof createBootstrap>[0] = {}) {
  const state = shallowRef<SessionMetadataState>({ phase: 'idle', key: '', lease: null, attempt: 0, outcome: null })
  let active: { finish: (outcome: SessionMetadataOutcome) => void } | null = null
  let ordinal = 0
  const attempts: Array<{ finish: (outcome: SessionMetadataOutcome) => void }> = []
  function beginAttempt(record: boolean) {
    const snapshot = { key: subject.sessionKey.value, lease: subject.currentLease(), attempt: ++ordinal }
    let resolve!: (outcome: SessionMetadataOutcome) => void
    const promise = new Promise<SessionMetadataOutcome>(finish => { resolve = finish })
    const operation = {
      finish(outcome: SessionMetadataOutcome) {
        if (active === operation) {
          active = null
          state.value = { ...snapshot, phase: outcome.ok ? 'ready' : 'error', outcome }
        }
        resolve(outcome)
      },
    }
    active = operation
    if (record) attempts.push(operation)
    state.value = { ...snapshot, phase: 'loading', outcome: null }
    return promise
  }
  const runAttempt = vi.fn(() => beginAttempt(true))
  const cancelAttempt = vi.fn(() => active?.finish({ ok: false, cancelled: true }))
  const subject = createBootstrap({ ...overrides,
    metadata: { state, runAttempt, cancelAttempt },
  })
  return { ...subject, state, runAttempt, attempts,
    beginMetadata: () => beginAttempt(false),
    finishMetadata: (outcome: SessionMetadataOutcome) => active?.finish(outcome),
  }
}

describe('explicit complete-session recovery', () => {
  it('joins duplicate and synchronous busy observers without rereading healthy live state', async () => {
    vi.useFakeTimers()
    const h = createManualMetadataRecovery()
    let reentrant: Promise<boolean> | undefined
    const stop = watch(h.api.sessionRetryBusy, busy => {
      if (busy) reentrant = h.api.retrySession()
    }, { flush: 'sync' })
    try {
      await h.api.startSessionBootstrap().live
      h.beginMetadata()
      await vi.advanceTimersByTimeAsync(120_000)
      expect(h.api.metadataRecovery?.explicitRetryAvailable.value).toBe(true)
      const manual = h.api.retrySession()
      expect(h.api.retrySession()).toBe(manual)
      expect(reentrant).toBe(manual)
      expect(h.api.sessionRetryBusy.value).toBe(true)
      await vi.advanceTimersByTimeAsync(0)
      expect(h.runAttempt).toHaveBeenCalledOnce()
      expect(h.subscribeSession).toHaveBeenCalledOnce()
      expect(h.openSessionRead).toHaveBeenCalledOnce()
      expect(h.closeLease).not.toHaveBeenCalled()
      h.attempts[0]!.finish({ ok: true })
      await expect(manual).resolves.toBe(true)
      expect(h.api.metadataRecovery?.phase.value).toBe('ready')
      expect(h.api.sessionRetryBusy.value).toBe(false)
      expect(h.api.livePhase.value).toBe('ready')
    } finally { stop(); h.api.cancelSessionBootstrap() }
  })

  it('repairs exhausted live and metadata together and waits for both authorities', async () => {
    vi.useFakeTimers()
    let finishLive!: (result: SessionSubscriptionOutcome) => void
    let liveAttempts = 0
    const h = createManualMetadataRecovery({
      subscribeSession: () => ++liveAttempts === 1 ? new Promise(() => {})
        : new Promise<SessionSubscriptionOutcome>(resolve => { finishLive = resolve }),
    })
    try {
      const initial = h.api.startSessionBootstrap()
      h.beginMetadata()
      await vi.advanceTimersByTimeAsync(120_000)
      await initial.live
      expect(h.api.liveRecoveryTerminal.value).toBe(true)
      expect(h.api.metadataRecovery?.explicitRetryAvailable.value).toBe(true)
      const manual = h.api.retrySession()
      await vi.advanceTimersByTimeAsync(0)
      expect(h.subscribeSession).toHaveBeenCalledTimes(2)
      expect(h.runAttempt).not.toHaveBeenCalled()
      finishLive({ ...LIVE_READY })
      await vi.advanceTimersByTimeAsync(0)
      expect(h.api.livePhase.value).toBe('ready')
      expect(h.runAttempt).toHaveBeenCalledOnce()
      expect(h.api.sessionRetryBusy.value).toBe(true)
      h.attempts[0]!.finish({ ok: true })
      await expect(manual).resolves.toBe(true)
      expect(h.api.sessionRetryBusy.value).toBe(false)
      expect(h.openSessionRead).toHaveBeenCalledTimes(2)
    } finally { h.api.cancelSessionBootstrap() }
  })

  it('reports metadata failure even when live remains healthy and does not auto-renew it', async () => {
    vi.useFakeTimers()
    const h = createManualMetadataRecovery()
    try {
      await h.api.startSessionBootstrap().live
      h.beginMetadata()
      await vi.advanceTimersByTimeAsync(120_000)
      const manual = h.api.retrySession()
      await vi.advanceTimersByTimeAsync(0)
      const failure = new SessionReadFailure('unavailable', 'Rejected metadata read', false)
      h.attempts[0]!.finish({ ok: false, error: failure })
      await expect(manual).resolves.toBe(false)
      expect(h.api.sessionRetryBusy.value).toBe(false)
      expect(h.api.livePhase.value).toBe('ready')
      expect(h.api.metadataRecovery?.error.value).toBe(failure)
      await expect(h.api.retryMetadata()).resolves.toBe(false)
      await expect(h.api.retrySession()).resolves.toBe(false)
      expect(h.runAttempt).toHaveBeenCalledOnce()
      expect(h.subscribeSession).toHaveBeenCalledOnce()
    } finally { h.api.cancelSessionBootstrap() }
  })

  it.each(['route', 'authority'] as const)('retires an awaited %s owner without clearing successor busy or applying stale success', async changed => {
    vi.useFakeTimers()
    const authority = ref('owner-a')
    const h = createManualMetadataRecovery({ incidentScope: () => authority.value })
    try {
      await h.api.startSessionBootstrap().live
      h.beginMetadata()
      await vi.advanceTimersByTimeAsync(120_000)
      const obsolete = h.api.retrySession()
      await vi.advanceTimersByTimeAsync(0)
      expect(h.runAttempt).toHaveBeenCalledOnce()
      if (changed === 'route') h.sessionKey.value = 'agent:main:webchat:successor'
      else authority.value = 'owner-b'
      expect(h.api.sessionRetryBusy.value).toBe(false)
      // Start a successor operation before allowing the obsolete continuation
      // to finish; its finally must not clear the successor's busy ownership.
      const successorBootstrap = h.api.startSessionBootstrap({ force: true })
      h.beginMetadata()
      const successor = h.api.retrySession()
      expect(h.api.sessionRetryBusy.value).toBe(true)
      await expect(obsolete).resolves.toBe(false)
      expect(h.api.sessionRetryBusy.value).toBe(true)
      h.attempts[0]!.finish({ ok: true })
      expect(h.api.metadataRecovery?.phase.value).not.toBe('ready')
      await successorBootstrap.live
      // The successor joins its initial metadata operation instead of adding
      // a second physical read while that operation is still loading.
      h.finishMetadata({ ok: true })
      await expect(successor).resolves.toBe(true)
      expect(h.api.sessionRetryBusy.value).toBe(false)
      expect(h.api.metadataRecovery?.phase.value).toBe('ready')
      expect(h.runAttempt).toHaveBeenCalledOnce()
    } finally { h.api.cancelSessionBootstrap() }
  })

  it('does not admit recovery after owner disposal or bootstrap cancellation', async () => {
    vi.useFakeTimers()
    const scope = effectScope()
    const h = scope.run(() => createManualMetadataRecovery())!
    await h.api.startSessionBootstrap().live
    h.beginMetadata()
    await vi.advanceTimersByTimeAsync(120_000)
    const manual = h.api.retrySession()
    h.api.cancelSessionBootstrap()
    await expect(manual).resolves.toBe(false)
    await expect(h.api.retrySession()).resolves.toBe(false)
    scope.stop()
    await expect(h.api.retrySession()).resolves.toBe(false)
    expect(h.runAttempt).not.toHaveBeenCalled()
    expect(h.api.sessionRetryBusy.value).toBe(false)
  })
})

describe('useChatSessionBootstrap', () => {
  it('joins automatic readiness without adding a read or renewing an exhausted incident', async () => {
    vi.useFakeTimers()
    const h = createBootstrap({ subscribeSession: () => new Promise(() => {}) })
    try {
      const initial = h.api.startSessionBootstrap()
      const waiting = h.api.waitForLiveReady({ explicit: false })
      expect(h.subscribeSession).toHaveBeenCalledOnce()
      await vi.advanceTimersByTimeAsync(120_001)
      await initial.live
      await expect(waiting).resolves.toBe(false)
      await expect(h.api.waitForLiveReady({ explicit: false })).resolves.toBe(false)
      expect(h.subscribeSession).toHaveBeenCalledOnce()
      expect(h.api.liveRecoveryTerminal.value).toBe(true)
      h.subscribeSession.mockResolvedValueOnce({ ...LIVE_READY })
      const explicit = await h.api.waitForLiveReady()
      expect(explicit && explicit()).toBe(true)
      expect(h.subscribeSession).toHaveBeenCalledTimes(2)
    } finally { h.api.cancelSessionBootstrap() }
  })

  it.each(['predecessor', 'fresh-again'] as const)('does not let a stale %s request suppress a fast replacement', async mode => {
    vi.useFakeTimers()
    let reads = 0
    const pendingUntilAbort = (context: SessionBootstrapPhaseContext) => new Promise<SessionSubscriptionOutcome>(resolve => {
      context.signal.addEventListener('abort', () => resolve({ ...LIVE_READY, authoritative: false, cancelled: true }), { once: true })
    })
    const h = createBootstrap({ connectionState: ref('connected'),
      subscribeSession: context => ++reads === 1 && mode === 'predecessor'
        ? pendingUntilAbort(context) : Promise.resolve({ ...LIVE_READY }),
      reconcileSession: pendingUntilAbort,
    })
    try {
      const initial = h.api.startSessionBootstrap()
      if (mode === 'fresh-again') await initial.live
      const previous = h.api.retryLive(false)
      if (mode === 'fresh-again') void h.api.retryLive(false)
      const replacement = h.api.handleConnectionState('connected')!
      await expect(replacement.live).resolves.toMatchObject({ authoritative: true })
      await previous
      expect(h.api.livePhase.value).toBe('ready')
      await vi.advanceTimersByTimeAsync(121_000)
      expect(h.api.livePhase.value).toBe('ready')
      expect(h.api.liveRecoveryTerminal.value).toBe(false)
      expect(h.openSessionRead).toHaveBeenCalledTimes(2)
    } finally { h.api.cancelSessionBootstrap() }
  })

  it('keeps a new successor gap pending when the old request finishes cancelling', async () => {
    vi.useFakeTimers()
    let completeReplacement!: (value: SessionSubscriptionOutcome) => void
    let completeFresh!: (value: SessionSubscriptionOutcome) => void
    let reads = 0
    const h = createBootstrap({ connectionState: ref('connected'),
      subscribeSession: () => ++reads === 1 ? new Promise(() => {})
        : new Promise(resolve => { completeReplacement = resolve }),
      reconcileSession: () => new Promise(resolve => { completeFresh = resolve }),
    })
    try {
      h.api.startSessionBootstrap()
      const previous = h.api.retryLive(false)
      const replacement = h.api.handleConnectionState('connected')!
      const currentRecovery = h.api.retryLive(false)
      completeReplacement({ ...LIVE_READY })
      await replacement.live
      await previous
      await vi.advanceTimersByTimeAsync(0)
      expect(h.api.livePhase.value).toBe('connecting')
      completeFresh({ ...LIVE_READY })
      await currentRecovery
      expect(h.api.livePhase.value).toBe('ready')
      expect(h.openSessionRead).toHaveBeenCalledTimes(2)
    } finally { h.api.cancelSessionBootstrap() }
  })

  it('rejects command readiness when the fresh required read fails', async () => {
    let initialReady!: (value: SessionSubscriptionOutcome) => void
    const h = createBootstrap({
      subscribeSession: () => new Promise(resolve => { initialReady = resolve }),
      reconcileSession: async () => ({ ...LIVE_READY, authoritative: false,
        error: new SessionReadFailure('unavailable', 'Unavailable', false) }),
    })
    try {
      const initial = h.api.startSessionBootstrap()
      const ready = h.api.waitForLiveReady()
      const recovery = h.api.retryLive(false)
      initialReady(LIVE_READY)
      await initial.live
      await recovery
      await expect(ready).resolves.toBe(false)
    } finally { h.api.cancelSessionBootstrap() }
  })

  it('returns a live readiness proof that rejects later gaps and authority changes', async () => {
    const authority = ref('owner-a')
    const h = createBootstrap({ incidentScope: () => authority.value,
      reconcileSession: () => new Promise(() => {}) })
    try {
      await h.api.startSessionBootstrap().live
      const ready = await h.api.waitForLiveReady()
      expect(ready && ready()).toBe(true)
      const recovery = h.api.retryLive(false)
      expect(ready && ready()).toBe(false)
      authority.value = 'owner-b'
      authority.value = 'owner-a'
      expect(ready && ready()).toBe(false)
      h.api.cancelSessionBootstrap()
      await recovery
    } finally { h.api.cancelSessionBootstrap() }
  })

  it.each(['initial', 'reconcile'])('replaces an obsolete read base after %s failure while retaining history', async source => {
    vi.useFakeTimers()
    const rebase = { ...LIVE_READY, authoritative: false,
      error: new SessionReadFailure('rebase-required' as SessionReadFailure['kind'], 'Reload state', true) }
    let attempts = 0
    const h = createBootstrap({
      connectionState: ref('connected'),
      subscribeSession: async () => source === 'initial' && attempts++ === 0 ? rebase : LIVE_READY,
      reconcileSession: async () => rebase,
    })
    try {
      const initial = h.api.startSessionBootstrap()
      await Promise.all([initial.live, initial.history])
      if (source === 'reconcile') await h.api.retryLive()
      await vi.advanceTimersByTimeAsync(500)
      expect(h.openSessionRead).toHaveBeenCalledTimes(2)
      expect(h.openSessionRead).toHaveBeenLastCalledWith({ sessionKey: 'agent:main:webchat:bootstrap-test', includeInitialHistory: false })
      expect(h.closeLease).toHaveBeenCalledOnce()
      expect(h.loadHistory).toHaveBeenCalledOnce()
      expect(h.api.historyPhase.value).toBe('ready')
      expect(h.api.livePhase.value).toBe('ready')
    } finally { h.api.cancelSessionBootstrap() }
  })

  it('allows repeated rebases within the original deadline until the third base succeeds', async () => {
    vi.useFakeTimers()
    const deadlines: number[] = []
    const h = createBootstrap({
      connectionState: ref('connected'),
      subscribeSession: async context => {
        deadlines.push(context.deadlineAt)
        return deadlines.length < 3 ? { ...LIVE_READY, authoritative: false,
          error: new SessionReadFailure('rebase-required', 'Reload state', true) } : LIVE_READY
      },
    })
    try {
      await h.api.startSessionBootstrap().live
      await vi.advanceTimersByTimeAsync(500)
      expect(h.openSessionRead).toHaveBeenCalledTimes(2)
      await vi.advanceTimersByTimeAsync(1000)
      expect(h.openSessionRead).toHaveBeenCalledTimes(3)
      expect(new Set(deadlines).size).toBe(1)
      expect(h.api.livePhase.value).toBe('ready')
    } finally { h.api.cancelSessionBootstrap() }
  })

  it('does not renew an exhausted rebase deadline and permits the existing explicit retry', async () => {
    vi.useFakeTimers()
    let failing = true
    const h = createBootstrap({
      connectionState: ref('connected'),
      subscribeSession: async () => failing ? { ...LIVE_READY, authoritative: false,
        error: new SessionReadFailure('rebase-required', 'Reload state', true) } : LIVE_READY,
    })
    try {
      await h.api.startSessionBootstrap().live
      await vi.advanceTimersByTimeAsync(136_000)
      const attempts = h.openSessionRead.mock.calls.length
      expect(attempts).toBeGreaterThan(2)
      await vi.advanceTimersByTimeAsync(45_000)
      expect(h.openSessionRead).toHaveBeenCalledTimes(attempts)
      expect(h.api.livePhase.value).toBe('degraded')
      failing = false
      await expect(h.api.retryLive(true)).resolves.toMatchObject({ authoritative: true })
      expect(h.openSessionRead).toHaveBeenCalledTimes(attempts + 1)
    } finally { h.api.cancelSessionBootstrap() }
  })

  it('does not reopen a rebase failure after its owner has been cancelled', async () => {
    vi.useFakeTimers()
    const h = createBootstrap({ connectionState: ref('connected'), subscribeSession: async () => ({
      ...LIVE_READY, authoritative: false,
      error: new SessionReadFailure('rebase-required', 'Reload state', true),
    }) })
    await h.api.startSessionBootstrap().live
    h.api.cancelSessionBootstrap()
    await vi.advanceTimersByTimeAsync(15_000)
    expect(h.openSessionRead).toHaveBeenCalledOnce()
  })

  it('restarts failed history when explicitly retrying an exhausted rebase budget', async () => {
    vi.useFakeTimers()
    let failing = true
    const h = createBootstrap({
      connectionState: ref('connected'),
      loadHistory: async () => failing
        ? { ok: false, error: new SessionReadFailure('unavailable', 'history failed', false) }
        : { ok: true },
      subscribeSession: async () => failing ? { ...LIVE_READY, authoritative: false,
        error: new SessionReadFailure('rebase-required', 'Reload state', true) } : LIVE_READY,
    })
    try {
      const initial = h.api.startSessionBootstrap()
      await Promise.all([initial.live, initial.history])
      await vi.advanceTimersByTimeAsync(136_000)
      expect(h.api.historyPhase.value).toBe('error')
      expect(h.api.livePhase.value).toBe('degraded')
      failing = false
      await expect(h.api.retryLive(true)).resolves.toMatchObject({ authoritative: true })
      await vi.waitFor(() => expect(h.api.historyPhase.value).toBe('ready'))
      expect(h.openSessionRead).toHaveBeenLastCalledWith({ sessionKey: 'agent:main:webchat:bootstrap-test', includeInitialHistory: true })
    } finally { h.api.cancelSessionBootstrap() }
  })

  it('grants an explicit rebase retry a new deadline before the automatic timer observes expiry', async () => {
    vi.useFakeTimers()
    let calls = 0
    const h = createBootstrap({ subscribeSession: async () => calls++ === 0 ? {
      ...LIVE_READY, authoritative: false,
      error: new SessionReadFailure('rebase-required', 'Reload state', true),
    } : LIVE_READY })
    try {
      const run = h.api.startSessionBootstrap()
      await Promise.all([run.live, run.history])
      vi.setSystemTime(Date.now() + 120_001)
      const retried = h.api.retryLive(true)
      await vi.advanceTimersByTimeAsync(500)
      await expect(retried).resolves.toMatchObject({ authoritative: true })
      expect(h.openSessionRead).toHaveBeenCalledTimes(2)
      expect(h.api.livePhase.value).toBe('ready')
    } finally { h.api.cancelSessionBootstrap() }
  })

  it.each(['loading', 'error'])('restarts the %s history phase when replacing its obsolete lease', async phase => {
    vi.useFakeTimers()
    let read = 0
    let release!: (value: SessionPhaseResult) => void
    const pending = new Promise<SessionPhaseResult>(resolve => { release = resolve })
    const h = createBootstrap({
      loadHistory: async () => ++read === 1
        ? phase === 'loading' ? pending : { ok: false, error: new SessionReadFailure('unavailable', 'history failed', false) }
        : { ok: true },
      subscribeSession: vi.fn().mockResolvedValueOnce({ ...LIVE_READY, authoritative: false,
        error: new SessionReadFailure('rebase-required', 'Reload state', true) }).mockResolvedValue(LIVE_READY),
    })
    try {
      const initial = h.api.startSessionBootstrap()
      await initial.live
      if (phase === 'error') await initial.history
      expect(h.api.historyPhase.value).toBe(phase)
      const retried = h.api.retryLive()
      await vi.advanceTimersByTimeAsync(500)
      await retried
      await vi.waitFor(() => expect(h.api.historyPhase.value).toBe('ready'))
      expect(h.openSessionRead).toHaveBeenLastCalledWith({ sessionKey: 'agent:main:webchat:bootstrap-test', includeInitialHistory: true })
      expect(h.loadHistory).toHaveBeenCalledTimes(2)
    } finally { release({ ok: true }); h.api.cancelSessionBootstrap() }
  })

  it('keeps draft identity on reconnect and retires it after first-send acceptance', async () => {
    let draft = true
    const h = createBootstrap({ isProvisionalDraft: () => draft })
    await h.api.startSessionBootstrap({ includeHistory: false }).live
    h.api.handleConnectionState('disconnected', false)
    await h.api.handleConnectionState('connected', false)?.live
    expect(h.openSessionRead.mock.calls.length).toBeGreaterThanOrEqual(2)
    expect(h.openSessionRead.mock.calls.every(args => args[0]?.provisionalDraft === true)).toBe(true)
    expect(h.loadHistory).not.toHaveBeenCalled()
    // ChatView clears the accepted intent before the existing forced bootstrap.
    draft = false
    await h.api.startSessionBootstrap({ includeHistory: false, force: true }).live
    expect(h.openSessionRead).toHaveBeenLastCalledWith({
      sessionKey: 'agent:main:webchat:bootstrap-test', includeInitialHistory: false,
    })
    h.api.cancelSessionBootstrap()
  })

  it.each(['too-large', 'budget-exhausted'] as const)(
    'preserves %s from the bootstrap result into the Conversation recovery callback', async kind => {
      const terminal = new SessionReadFailure(kind, 'Explicit retry required', false)
      const h = createBootstrap({
        reconcileSession: async () => ({ ...LIVE_READY, authoritative: false, error: terminal }),
      })
      try {
        await h.api.startSessionBootstrap().live
        await expect(h.api.retryLive(false).then(sessionRecoverySucceeded)).rejects.toBe(terminal)
        expect(h.api.livePhase.value).toBe('degraded')
        expect(h.openSessionRead).toHaveBeenCalledOnce()
        await expect(h.api.retryLive(true).then(sessionRecoverySucceeded)).resolves.toBe(true)
        expect(h.openSessionRead).toHaveBeenCalledTimes(2)
      } finally { h.api.cancelSessionBootstrap() }
    },
  )

  it('does not classify raw wire retryability or a cancelled read as a terminal domain failure', () => {
    const raw = Object.assign(new Error('stale'), { code: 'SNAPSHOT_STALE', retryable: false })
    expect(sessionRecoverySucceeded({ authoritative: false, error: raw })).toBe(false)
    expect(sessionRecoverySucceeded({ authoritative: false, error: new SessionReadFailure('unavailable', 'retry', true) })).toBe(false)
    expect(sessionRecoverySucceeded({ authoritative: false, cancelled: true,
      error: new SessionReadFailure('too-large', 'stale owner', false) })).toBe(false)
  })

  it('does not queue another snapshot when an ordinary heartbeat arrives during a slow live transfer', async () => {
    vi.useFakeTimers()
    let complete!: (result: SessionSubscriptionOutcome) => void
    const pending = new Promise<SessionSubscriptionOutcome>(resolve => { complete = resolve })
    const reconcileSession = vi.fn(async () => LIVE_READY)
    const h = createBootstrap({
      subscribeSession: () => pending, reconcileSession, connectionState: ref('connected'),
    })
    let wildcard!: TransportEventHandler
    const transport = createConversationEventTransport({
      subscribe(event, listener) {
        if (event === '*') wildcard = listener
        return { close() {} }
      },
    })
    transport.subscribe({ onEvent: event => {
      // The real Conversation consumer requests recovery on invalid frames.
      if (event.kind === 'invalid') void h.api.retryLive()
    } })
    try {
      const run = h.api.startSessionBootstrap()
      await vi.advanceTimersByTimeAsync(30_000)
      expect(h.api.livePhase.value).toBe('degraded')
      wildcard('tick', { time_ms: Date.now() }, {})
      await vi.advanceTimersByTimeAsync(15_000)
      complete(LIVE_READY)
      await run.live
      await vi.advanceTimersByTimeAsync(0)
      expect(h.api.livePhase.value).toBe('ready')
      expect(reconcileSession).not.toHaveBeenCalled()
      expect(h.openSessionRead).toHaveBeenCalledOnce()
    } finally {
      transport.unsubscribe()
      h.api.cancelSessionBootstrap()
    }
  })

  it('merges gaps during an initial live read into a fresh reconciliation on the same lease', async () => {
    let release!: (value: SessionSubscriptionOutcome) => void
    const initial = new Promise<SessionSubscriptionOutcome>(resolve => { release = resolve })
    const reconcileSession = vi.fn(async () => LIVE_READY)
    const { api, openSessionRead, closeLease } = createBootstrap({ subscribeSession: () => initial, reconcileSession })
    const run = api.startSessionBootstrap()
    const recovery = api.retryLive()
    expect(api.retryLive()).toBe(recovery)
    expect(reconcileSession).not.toHaveBeenCalled()
    release(LIVE_READY)
    await run.live
    await recovery
    expect(reconcileSession).toHaveBeenCalledOnce()
    expect(openSessionRead).toHaveBeenCalledOnce()
    expect(closeLease).not.toHaveBeenCalled()
    api.cancelSessionBootstrap()
  })

  it('automatically retries a failed live read on the original lease without a retry click', async () => {
    vi.useFakeTimers()
    const reconcileSession = vi.fn(async () => LIVE_READY)
    const { api, openSessionRead, closeLease } = createBootstrap({
      connectionState: ref('connected'),
      subscribeSession: async () => ({ ...LIVE_READY, authoritative: false,
        error: new SessionReadFailure('timeout', 'held subscribe', true) }),
      reconcileSession,
    })
    const run = api.startSessionBootstrap()
    await Promise.all([run.history, run.live])
    await vi.advanceTimersByTimeAsync(500)
    expect(reconcileSession).toHaveBeenCalledOnce()
    expect(openSessionRead).toHaveBeenCalledOnce()
    expect(closeLease).not.toHaveBeenCalled()
    expect(api.livePhase.value).toBe('ready')
    api.cancelSessionBootstrap()
  })

  it('retries only deferred metadata while preserving an authoritative live subscription', async () => {
    vi.useFakeTimers()
    const state = shallowRef<SessionMetadataState>({ phase: 'idle', key: '', lease: null, attempt: 0, outcome: null })
    const retryMetadata = vi.fn(async () => {
      state.value = { ...state.value, phase: 'loading', attempt: state.value.attempt + 1, outcome: null }
      state.value = { ...state.value, phase: 'ready', outcome: { ok: true } }
      return { ok: true }
    })
    const { api, openSessionRead, closeLease, currentLease, sessionKey } = createBootstrap({
      connectionState: ref('connected'), metadata: { state, runAttempt: retryMetadata, cancelAttempt: vi.fn() },
    })
    const run = api.startSessionBootstrap()
    await Promise.all([run.history, run.live])
    expect(api.livePhase.value).toBe('ready')
    state.value = { phase: 'loading', key: sessionKey.value, lease: currentLease(), attempt: 1, outcome: null }
    state.value = { ...state.value, phase: 'error', outcome: {
      ok: false, error: new SessionReadFailure('busy', 'storage busy', true, 100),
    } }
    await vi.advanceTimersByTimeAsync(500)
    expect(retryMetadata).toHaveBeenCalledOnce()
    expect(api.livePhase.value).toBe('ready')
    expect(openSessionRead).toHaveBeenCalledOnce()
    expect(closeLease).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(30_000)
    expect(retryMetadata).toHaveBeenCalledOnce()
    api.cancelSessionBootstrap()
  })

  it('rearms automatic live retry after repeated failure without leaving stable degradation', async () => {
    vi.useFakeTimers()
    const failure = { ...LIVE_READY, authoritative: false,
      error: new SessionReadFailure('timeout', 'held subscribe', true) }
    const reconcileSession = vi.fn().mockResolvedValueOnce(failure).mockResolvedValueOnce(LIVE_READY)
    const h = createBootstrap({
      connectionState: ref('connected'), subscribeSession: async () => failure, reconcileSession,
    })
    await h.api.startSessionBootstrap().live
    h.api.dismissRecoveryNotice()
    await vi.advanceTimersByTimeAsync(500)
    expect(reconcileSession).toHaveBeenCalledTimes(1)
    expect(h.api.livePhase.value).toBe('degraded')
    expect(h.api.noticeDismissed.value).toBe(true)
    await vi.advanceTimersByTimeAsync(1_000)
    expect(reconcileSession).toHaveBeenCalledTimes(2)
    expect(h.api.livePhase.value).toBe('ready')
    expect(h.openSessionRead).toHaveBeenCalledTimes(1)
    h.api.cancelSessionBootstrap()
  })

  it('publishes a history-only failure before notifying the automatic recovery watcher', async () => {
    vi.useFakeTimers()
    let available = false
    const { api, loadHistory, subscribeSession, openSessionRead, closeLease } = createBootstrap({
      connectionState: ref('connected'),
      loadHistory: async () => available
        ? { ok: true }
        : { ok: false, error: new SessionReadFailure('unavailable', 'history offline', true) },
    })
    try {
      const run = api.startSessionBootstrap()
      await run.live
      await vi.advanceTimersByTimeAsync(100)
      await run.history
      expect(api.historyPhase.value).toBe('error')
      expect(api.livePhase.value).toBe('ready')
      const failedAttempts = loadHistory.mock.calls.length

      available = true
      // No connection transition or unrelated live update wakes the watcher.
      await vi.advanceTimersByTimeAsync(500)
      expect(loadHistory).toHaveBeenCalledTimes(failedAttempts + 1)
      expect(api.historyPhase.value).toBe('ready')
      expect(subscribeSession).toHaveBeenCalledOnce()
      expect(openSessionRead).toHaveBeenCalledOnce()
      expect(closeLease).not.toHaveBeenCalled()
    } finally {
      api.cancelSessionBootstrap()
    }
  })

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
          ? { ok: false, error: new SessionReadFailure('timeout', 'chat.history timed out', true) }
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

  it('replaces an in-flight connected run with one coherent generation', async () => {
    let releaseInitialLive!: (result: SessionSubscriptionOutcome) => void
    let liveCall = 0
    let historyCall = 0
    const criticalRequests: Promise<void>[] = []
    const initialHistory: SessionPhaseResult = { ok: true }
    const replacementHistory: SessionPhaseResult = { ok: true }
    const replacementLive: SessionSubscriptionOutcome = {
      ...LIVE_READY,
      error: new Error('replacement-live'),
    }
    const { api, openSessionRead } = createBootstrap({
      criticalRequestsQueued: () => {
        const queued = Promise.resolve()
        criticalRequests.push(queued)
        return queued
      },
      loadHistory: async () => {
        historyCall += 1
        return historyCall === 1 ? initialHistory : replacementHistory
      },
      subscribeSession: async () => {
        liveCall += 1
        if (liveCall === 1) {
          return new Promise(resolve => { releaseInitialLive = resolve })
        }
        return replacementLive
      },
    })

    const initial = api.startSessionBootstrap()
    const replacement = api.handleConnectionState('connected')!

    expect(replacement.generation).toBeGreaterThan(initial.generation)
    expect(replacement.criticalRequestsQueued).toBe(criticalRequests[1])
    await expect(replacement.history).resolves.toBe(replacementHistory)
    await expect(replacement.live).resolves.toBe(replacementLive)
    expect(openSessionRead).toHaveBeenCalledTimes(2)

    releaseInitialLive(LIVE_READY)
    await expect(initial.live).resolves.toEqual({
      ...LIVE_READY,
      authoritative: false,
      cancelled: true,
    })
    expect(api.historyPhase.value).toBe('ready')
    expect(api.livePhase.value).toBe('ready')
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
        return {
          ok: false,
          error: new SessionReadFailure('timeout', 'chat.history timed out', true),
        }
      },
      subscribeSession: async context => {
        liveContexts.push(context)
        return {
          authoritative: false,
          live: false,
          backgroundOnly: false,
          error: new SessionReadFailure(
            'timeout',
            'sessions.messages.subscribe timed out',
            true,
          ),
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
    const busy = new SessionReadFailure('busy', 'storage busy', true, 100)
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
    const failure = new SessionReadFailure('unavailable', 'history unavailable', true)
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

  it('reopens a dead lease before retrying history', async () => {
    let recover = false
    const failure = new SessionReadFailure('unavailable', 'history unavailable', false)
    const {
      api,
      loadHistory,
      subscribeSession,
      openSessionRead,
      retireCurrentLease,
    } = createBootstrap({
      loadHistory: async () => recover ? { ok: true } : { ok: false, error: failure },
    })

    const initial = api.startSessionBootstrap()
    await Promise.all([initial.history, initial.live])
    retireCurrentLease()
    recover = true

    await expect(api.retryHistory()).resolves.toEqual({ ok: true })
    expect(loadHistory).toHaveBeenCalledTimes(2)
    expect(subscribeSession).toHaveBeenCalledTimes(2)
    expect(openSessionRead).toHaveBeenCalledTimes(2)
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

  it('defers an in-flight connected recovery until handoff rollback', async () => {
    let releaseInitial!: (result: SessionSubscriptionOutcome) => void
    let liveCall = 0
    const unavailable: SessionSubscriptionOutcome = {
      authoritative: false,
      live: false,
      backgroundOnly: false,
      cancelled: true,
    }
    const { api, subscribeSession, openSessionRead } = createBootstrap({
      subscribeSession: async () => {
        liveCall += 1
        if (liveCall === 1) {
          return new Promise(resolve => { releaseInitial = resolve })
        }
        return LIVE_READY
      },
    })
    const initial = api.startSessionBootstrap({ includeHistory: false })

    api.setSessionHandoffTarget('agent:main:webchat:target', 1)
    expect(api.handleConnectionState('connected', false)?.deferred).toBe(true)
    expect(openSessionRead).toHaveBeenCalledOnce()

    releaseInitial(unavailable)
    await expect(initial.live).resolves.toEqual(unavailable)
    expect(api.livePhase.value).toBe('degraded')
    expect(openSessionRead).toHaveBeenCalledOnce()

    const resumed = api.setSessionHandoffTarget(null, 1)
    expect(resumed?.generation).toBeGreaterThan(initial.generation)
    await expect(resumed?.live).resolves.toBe(LIVE_READY)
    expect(subscribeSession).toHaveBeenCalledTimes(2)
    expect(openSessionRead).toHaveBeenCalledTimes(2)
  })

  it('discards a deferred in-flight recovery after the target commits', async () => {
    let releaseInitial!: (result: SessionSubscriptionOutcome) => void
    const unavailable: SessionSubscriptionOutcome = {
      authoritative: false,
      live: false,
      backgroundOnly: false,
      cancelled: true,
    }
    const { api, subscribeSession, openSessionRead } = createBootstrap({
      subscribeSession: async () => new Promise(resolve => { releaseInitial = resolve }),
    })
    const initial = api.startSessionBootstrap({ includeHistory: false })

    api.setSessionHandoffTarget('agent:main:webchat:target', 2)
    expect(api.handleConnectionState('connected', false)?.deferred).toBe(true)
    releaseInitial(unavailable)
    await expect(initial.live).resolves.toEqual(unavailable)

    expect(api.setSessionHandoffTarget(null, 2, 'committed')).toBeUndefined()
    expect(subscribeSession).toHaveBeenCalledOnce()
    expect(openSessionRead).toHaveBeenCalledOnce()
    expect(api.livePhase.value).toBe('degraded')
  })

  it('tracks draft mutation and retryable phase contracts independently of transport', () => {
    const attachment = { id: 1 }
    expect(autoSendDraftIsUnchanged(
      'draft', 'draft', [attachment], [attachment], 1, 1,
    )).toBe(true)
    expect(autoSendDraftIsUnchanged(
      'draft', 'draft', [attachment], [attachment], 1, 2,
    )).toBe(false)
    expect(shouldRetrySessionPhase(
      new SessionReadFailure('unavailable', 'temporarily unavailable', true),
    )).toBe(true)
    expect(shouldRetrySessionPhase(
      new SessionReadFailure('unavailable', 'not authorized', false),
    )).toBe(false)
  })
})


describe('shared live recovery admission', () => {
  it('publishes first authority binding as fresh proof without another ready edge', async () => {
    vi.useFakeTimers()
    const authority = ref('')
    const h = createBootstrap({ incidentScope: () => authority.value, connectionState: ref('connected') })
    const checkpoint = h.api.recoveryProof.capture()
    const proofAvailable = vi.fn()
    const stop = watch(h.api.recoveryProof.revision, () => { if (checkpoint()) proofAvailable() })
    try {
      await h.api.startSessionBootstrap().live
      await vi.advanceTimersByTimeAsync(0)
      expect(h.api.livePhase.value).toBe('ready')
      expect(checkpoint()).toBe(false)
      expect(proofAvailable).not.toHaveBeenCalled()
      authority.value = 'first-principal'
      await vi.advanceTimersByTimeAsync(0)
      expect(checkpoint()).toBe(true)
      expect(proofAvailable).toHaveBeenCalledOnce()
      expect(h.subscribeSession).toHaveBeenCalledOnce()
      expect(h.api.livePhase.value).toBe('ready')
    } finally { stop(); h.api.cancelSessionBootstrap() }
  })

  it.each([false, true])('rejects an unknown checkpoint after first principal changes before readiness (via unknown=%s)', async viaUnknown => {
    vi.useFakeTimers()
    const authority = ref('')
    let finishOld!: (value: SessionSubscriptionOutcome) => void
    const subscribe = vi.fn().mockImplementationOnce(() => new Promise<SessionSubscriptionOutcome>(resolve => { finishOld = resolve }))
      .mockResolvedValue({ ...LIVE_READY })
    const h = createBootstrap({ incidentScope: () => authority.value, connectionState: ref('connected'), subscribeSession: subscribe })
    const checkpoint = h.api.recoveryProof.capture()
    try {
      const initial = h.api.startSessionBootstrap()
      authority.value = 'principal-a'
      await vi.advanceTimersByTimeAsync(0)
      expect(h.api.livePhase.value).toBe('connecting')
      // Observe even transitions coalesced before the asynchronous bootstrap watcher.
      if (viaUnknown) authority.value = ''
      authority.value = 'principal-b'
      await vi.advanceTimersByTimeAsync(0)
      expect(h.api.livePhase.value).toBe('ready')
      expect(checkpoint()).toBe(false)
      finishOld(LIVE_READY)
      await initial.live
      expect(checkpoint()).toBe(false)
    } finally { h.api.cancelSessionBootstrap() }
  })

  it('does not revive proof after principal A is lost and returns to A', async () => {
    vi.useFakeTimers()
    const authority = ref('principal-a')
    const h = createBootstrap({ incidentScope: () => authority.value, connectionState: ref('connected') })
    const checkpoint = h.api.recoveryProof.capture()
    try {
      await h.api.startSessionBootstrap().live
      expect(checkpoint()).toBe(true)
      authority.value = ''
      authority.value = 'principal-a'
      await h.api.startSessionBootstrap({ force: true }).live
      expect(checkpoint()).toBe(false)
    } finally { h.api.cancelSessionBootstrap() }
  })

  it('requires a read newer than capture and permits replacement leases in the same authority', async () => {
    vi.useFakeTimers()
    const h = createBootstrap({ incidentScope: () => 'principal-a' })
    try {
      await h.api.startSessionBootstrap().live
      const checkpoint = h.api.recoveryProof.capture()
      expect(checkpoint()).toBe(false)
      h.retireCurrentLease()
      await h.api.retryLive()
      expect(checkpoint()).toBe(true)
      expect(h.openSessionRead).toHaveBeenCalledTimes(2)
    } finally { h.api.cancelSessionBootstrap() }
  })

  it('does not count a read waiting for admission as newer than a captured checkpoint', async () => {
    vi.useFakeTimers()
    let attempts = 0
    const h = createBootstrap({ incidentScope: () => 'principal-a', subscribeSession: async () => ++attempts === 1
      ? { ...LIVE_READY, authoritative: false, error: new SessionReadFailure('busy', 'Wait', true, 5_000) }
      : LIVE_READY })
    try {
      await h.api.startSessionBootstrap().live
      // With no active owner, the replacement phase starts before its retained
      // retry hint permits opening the lease. An idle owner's queued retry would
      // instead start a genuinely new phase after this checkpoint.
      h.api.cancelSessionBootstrap()
      const replacement = h.api.startSessionBootstrap({ force: true })
      const checkpoint = h.api.recoveryProof.capture()
      expect(attempts).toBe(1)
      await vi.advanceTimersByTimeAsync(5_000)
      await replacement.live
      expect(h.api.livePhase.value).toBe('ready')
      expect(checkpoint()).toBe(false)
      await h.api.retryLive()
      expect(checkpoint()).toBe(true)
    } finally { h.api.cancelSessionBootstrap() }
  })

  it.each(['session-return', 'cancel', 'dispose'] as const)('invalidates captured proof on %s', async reason => {
    vi.useFakeTimers()
    const scope = effectScope()
    const h = scope.run(() => createBootstrap({ incidentScope: () => 'principal-a' }))!
    const checkpoint = h.api.recoveryProof.capture()
    try {
      await h.api.startSessionBootstrap().live
      expect(checkpoint()).toBe(true)
      if (reason === 'session-return') {
        const key = h.sessionKey.value
        h.sessionKey.value = 'different-session'
        h.sessionKey.value = key
        await h.api.startSessionBootstrap({ force: true }).live
      } else if (reason === 'cancel') {
        h.api.cancelSessionBootstrap()
        await h.api.startSessionBootstrap().live
      } else scope.stop()
      expect(checkpoint()).toBe(false)
    } finally { h.api.cancelSessionBootstrap(); scope.stop() }
  })

  it.each(['before', 'after'] as const)('binds first Hello authority %s connected without repeating its admitted read', async order => {
    vi.useFakeTimers()
    const scope = ref('')
    const connection = ref('connecting')
    let finish!: (result: SessionSubscriptionOutcome) => void
    const subscribe = vi.fn().mockImplementationOnce(() => new Promise<SessionSubscriptionOutcome>(resolve => { finish = resolve }))
      .mockResolvedValue({ ...LIVE_READY })
    const h = createBootstrap({ incidentScope: () => scope.value,
      connectionState: connection, subscribeSession: subscribe })
    try {
      const initial = h.api.startSessionBootstrap()
      if (order === 'before') scope.value = 'authenticated-target'
      connection.value = 'connected'
      if (order === 'before') expect(h.api.handleConnectionState('connected')!.generation).toBe(initial.generation)
      await vi.advanceTimersByTimeAsync(0)
      if (order === 'after') scope.value = 'authenticated-target'
      finish(LIVE_READY)
      await vi.advanceTimersByTimeAsync(0)
      await expect(initial.live).resolves.toMatchObject({ authoritative: true })
      expect(h.openSessionRead).toHaveBeenCalledOnce()
      expect(h.api.livePhase.value).toBe('ready')
      expect(h.api.currentSessionBootstrap()).toBeDefined()
    } finally { h.api.cancelSessionBootstrap() }
  })

  it('retains retry hints and the deadline when binding the first Hello authority', async () => {
    vi.useFakeTimers()
    const scope = ref('')
    const connection = ref('connecting')
    const contexts: SessionBootstrapPhaseContext[] = []
    const failure = { ...LIVE_READY, authoritative: false,
      error: new SessionReadFailure('busy', 'storage busy', true, 5_000) }
    const h = createBootstrap({ incidentScope: () => scope.value, connectionState: connection,
      subscribeSession: async context => { contexts.push(context); return failure },
      reconcileSession: async context => { contexts.push(context); return LIVE_READY },
    })
    try {
      await h.api.startSessionBootstrap().live
      connection.value = 'connected'
      await vi.advanceTimersByTimeAsync(1_000)
      scope.value = 'authenticated-target'
      await vi.advanceTimersByTimeAsync(3_999)
      expect(contexts).toHaveLength(1)
      expect(h.openSessionRead).toHaveBeenCalledOnce()
      await vi.advanceTimersByTimeAsync(1)
      expect(contexts).toHaveLength(2)
      expect(contexts[1]!.deadlineAt).toBe(contexts[0]!.deadlineAt)
      expect(h.api.livePhase.value).toBe('ready')
      expect(h.openSessionRead).toHaveBeenCalledOnce()
    } finally { h.api.cancelSessionBootstrap() }
  })

  it('does not resurrect a completed incident when the first target proof arrives after readiness', async () => {
    vi.useFakeTimers()
    const scope = ref('')
    const h = createBootstrap({ incidentScope: () => scope.value, connectionState: ref('connected') })
    try {
      await h.api.startSessionBootstrap().live
      await vi.advanceTimersByTimeAsync(120_001)
      scope.value = 'authenticated-target'
      await vi.advanceTimersByTimeAsync(0)
      expect(h.openSessionRead).toHaveBeenCalledOnce()
      await expect(h.api.retryLive()).resolves.toMatchObject({ authoritative: true })
      expect(h.openSessionRead).toHaveBeenCalledTimes(2)
      expect(h.api.liveRecoveryTerminal.value).toBe(false)
    } finally { h.api.cancelSessionBootstrap() }
  })

  it.each([false, true])('replaces a proven principal rather than rebinding it (via unknown=%s)', async unknown => {
    vi.useFakeTimers()
    const scope = ref('principal-a')
    const h = createBootstrap({ incidentScope: () => scope.value, connectionState: ref('connected'),
      subscribeSession: async () => ({ ...LIVE_READY }) })
    try {
      const old = await h.api.startSessionBootstrap().live
      if (unknown) {
        scope.value = ''
        await vi.advanceTimersByTimeAsync(0)
      }
      scope.value = 'principal-b'
      expect(h.api.isCurrentLiveResult(old)).toBe(false)
      await vi.advanceTimersByTimeAsync(0)
      expect(h.openSessionRead).toHaveBeenCalledTimes(unknown ? 3 : 2)
      expect(h.api.isCurrentLiveResult(old)).toBe(false)
      expect(h.api.livePhase.value).toBe('ready')
    } finally { h.api.cancelSessionBootstrap() }
  })

  it('proves a replacement result against the current run, lease and authority scope', async () => {
    vi.useFakeTimers()
    let scope = 'authority-a'
    const h = createBootstrap({
      incidentScope: () => scope,
      subscribeSession: async () => ({ ...LIVE_READY }),
    })
    try {
      const first = await h.api.startSessionBootstrap().live
      expect(h.api.isCurrentLiveResult(first)).toBe(true)
      h.retireCurrentLease()
      const second = await h.api.retryLive()
      expect(h.api.isCurrentLiveResult(second)).toBe(true)
      expect(h.api.isCurrentLiveResult(first)).toBe(false)
      scope = 'authority-b'
      expect(h.api.isCurrentLiveResult(second)).toBe(false)
      const third = await h.api.startSessionBootstrap().live
      expect(h.api.isCurrentLiveResult(third)).toBe(true)
    } finally { h.api.cancelSessionBootstrap() }
  })

  it.each([false, true])('gates lease.open after cancelling the owner (explicit=%s)', async explicit => {
    vi.useFakeTimers()
    let available = false
    const contexts: SessionBootstrapPhaseContext[] = []
    const h = createBootstrap({ subscribeSession: async context => {
      contexts.push(context)
      return available ? LIVE_READY : { ...LIVE_READY, authoritative: false,
        error: new SessionReadFailure('busy', 'wait for storage', true, 5_000) }
    } })
    try {
      await h.api.startSessionBootstrap().live
      h.api.cancelSessionBootstrap()
      await vi.advanceTimersByTimeAsync(1_000)
      available = true
      const resumed = explicit ? h.api.retryLive(true) : h.api.startSessionBootstrap().live
      await vi.advanceTimersByTimeAsync(3_999)
      expect(h.openSessionRead).toHaveBeenCalledOnce()
      await vi.advanceTimersByTimeAsync(1)
      await expect(resumed).resolves.toMatchObject({ authoritative: true })
      expect(h.openSessionRead).toHaveBeenCalledTimes(2)
      expect(contexts[1]!.attemptDeadlineAt).toBeGreaterThan(Date.now())
      if (!explicit) expect(contexts[1]!.deadlineAt).toBe(contexts[0]!.deadlineAt)
    } finally { h.api.cancelSessionBootstrap() }
  })

  it('preserves the history attempt budget after a long admission wait', async () => {
    vi.useFakeTimers()
    let available = false
    const h = createBootstrap({ subscribeSession: async () => available ? LIVE_READY : {
      ...LIVE_READY, authoritative: false,
      error: new SessionReadFailure('busy', 'wait for storage', true, 20_000),
    } })
    try {
      const initial = h.api.startSessionBootstrap()
      await Promise.all([initial.live, initial.history])
      h.api.cancelSessionBootstrap()
      available = true
      const resumed = h.api.startSessionBootstrap()
      await vi.advanceTimersByTimeAsync(20_000)
      await expect(resumed.history).resolves.toMatchObject({ ok: true })
      await expect(resumed.live).resolves.toMatchObject({ authoritative: true })
      expect(h.api.historyPhase.value).toBe('ready')
      expect(h.loadHistory).toHaveBeenCalledTimes(2)
      expect(h.loadHistory.mock.calls[1]![0].deadlineAt).toBeGreaterThan(Date.now())
    } finally { h.api.cancelSessionBootstrap() }
  })

  it('joins a queued post-gap read when an explicit caller arrives during its predecessor', async () => {
    vi.useFakeTimers()
    let complete!: (result: SessionSubscriptionOutcome) => void
    let reconcile!: (result: SessionSubscriptionOutcome) => void
    const h = createBootstrap({
      subscribeSession: () => new Promise(resolve => { complete = resolve }),
      reconcileSession: () => new Promise(resolve => { reconcile = resolve }),
    })
    try {
      const run = h.api.startSessionBootstrap()
      const recovery = h.api.retryLive()
      const explicit = h.api.retryLive(true)
      expect(explicit).toBe(recovery)
      const delivered = vi.fn()
      void explicit.then(delivered)
      complete(LIVE_READY)
      await run.live
      await vi.advanceTimersByTimeAsync(0)
      expect(delivered).not.toHaveBeenCalled()
      expect(h.api.livePhase.value).not.toBe('ready')
      reconcile(LIVE_READY)
      await expect(explicit).resolves.toMatchObject({ authoritative: true })
      expect(h.api.livePhase.value).toBe('ready')
    } finally { h.api.cancelSessionBootstrap() }
  })

  it('coalesces gaps during reconciliation into one fresh proof before releasing its callers', async () => {
    vi.useFakeTimers()
    const completions: Array<(result: SessionSubscriptionOutcome) => void> = []
    const reconcile = vi.fn(() => new Promise<SessionSubscriptionOutcome>(resolve => { completions.push(resolve) }))
    const h = createBootstrap({ reconcileSession: reconcile })
    try {
      await h.api.startSessionBootstrap().live
      const recovery = h.api.retryLive()
      expect(reconcile).toHaveBeenCalledOnce()
      expect(h.api.retryLive()).toBe(recovery)
      expect(h.api.retryLive()).toBe(recovery)
      const delivered = vi.fn()
      void recovery.then(delivered)
      completions[0]!(LIVE_READY)
      await vi.advanceTimersByTimeAsync(0)
      expect(reconcile).toHaveBeenCalledTimes(2)
      expect(h.api.livePhase.value).not.toBe('ready')
      expect(delivered).not.toHaveBeenCalled()
      const explicit = h.api.retryLive(true)
      expect(explicit).toBe(recovery)
      completions[1]!({ ...LIVE_READY })
      const result = await recovery
      expect(h.api.isCurrentLiveResult(result)).toBe(true)
      expect(reconcile).toHaveBeenCalledTimes(2)
      expect(h.openSessionRead).toHaveBeenCalledOnce()
    } finally { h.api.cancelSessionBootstrap() }
  })

  it('does not renew the deadline when repeated explicit clicks join an admission wait', async () => {
    vi.useFakeTimers()
    const h = createBootstrap({ connectionState: ref('connected'), subscribeSession: async () => ({
      ...LIVE_READY, authoritative: false,
      error: new SessionReadFailure('busy', 'wait for storage', true, 300_000),
    }) })
    try {
      await h.api.startSessionBootstrap().live
      await vi.advanceTimersByTimeAsync(119_000)
      const joined = h.api.retryLive(true)
      await vi.advanceTimersByTimeAsync(999)
      expect(h.api.retryLive(true)).toBe(joined)
      expect(h.api.liveRecoveryTerminal.value).toBe(false)
      await vi.advanceTimersByTimeAsync(1)
      await expect(joined).resolves.toMatchObject({ authoritative: false,
        error: { kind: 'budget-exhausted' } })
      expect(h.api.liveRecoveryTerminal.value).toBe(true)
      expect(h.openSessionRead).toHaveBeenCalledOnce()
    } finally { h.api.cancelSessionBootstrap() }
  })

  it('coalesces dirty triggers and preserves retry hints and deadlines across lease replacement', async () => {
    vi.useFakeTimers()
    const contexts: SessionBootstrapPhaseContext[] = []
    const failure = { ...LIVE_READY, authoritative: false,
      error: new SessionReadFailure('busy', 'storage busy', true, 5_000) }
    const h = createBootstrap({
      connectionState: ref('connected'),
      subscribeSession: async context => { contexts.push(context); return failure },
      reconcileSession: async context => { contexts.push(context); return failure },
    })
    try {
      await h.api.startSessionBootstrap().live
      const first = h.api.retryLive()
      for (let index = 0; index < 4; index++) {
        await vi.advanceTimersByTimeAsync(1_000)
        expect(h.api.retryLive()).toBe(first)
        h.retireCurrentLease()
        expect(h.api.handleConnectionState('connected')!.live).toBe(first)
        expect(h.openSessionRead).toHaveBeenCalledOnce()
      }
      await vi.advanceTimersByTimeAsync(1_000)
      await first
      expect(h.openSessionRead).toHaveBeenCalledTimes(2)
      expect(contexts).toHaveLength(2)
      expect(contexts[1]!.deadlineAt).toBe(contexts[0]!.deadlineAt)

      for (let index = 0; index < 115; index++) {
        h.retireCurrentLease()
        h.api.handleConnectionState('connected')
        void h.api.retryLive()
        await vi.advanceTimersByTimeAsync(1_000)
      }
      expect(h.api.liveRecoveryTerminal.value).toBe(true)
      expect(new Set(contexts.map(context => context.deadlineAt)).size).toBe(1)
      const attempts = contexts.length
      for (let index = 0; index < 5; index++) {
        h.retireCurrentLease()
        h.api.handleConnectionState('connected')
        await h.api.retryLive()
        await vi.advanceTimersByTimeAsync(30_000)
      }
      expect(contexts).toHaveLength(attempts)
      expect(h.api.livePhase.value).toBe('degraded')
    } finally { h.api.cancelSessionBootstrap() }
  })

  it('allows explicit renewal after expiry without bypassing an outstanding server hint', async () => {
    vi.useFakeTimers()
    let available = false
    const h = createBootstrap({
      connectionState: ref('connected'),
      subscribeSession: async () => available ? LIVE_READY : { ...LIVE_READY, authoritative: false,
        error: new SessionReadFailure('busy', 'wait for storage', true, 180_000) },
    })
    try {
      await h.api.startSessionBootstrap().live
      await vi.advanceTimersByTimeAsync(120_000)
      expect(h.api.liveRecoveryTerminal.value).toBe(true)
      available = true
      const explicit = h.api.retryLive(true)
      await vi.advanceTimersByTimeAsync(59_999)
      expect(h.openSessionRead).toHaveBeenCalledOnce()
      expect(h.api.livePhase.value).toBe('degraded')
      await vi.advanceTimersByTimeAsync(1)
      await expect(explicit).resolves.toMatchObject({ authoritative: true })
      expect(h.openSessionRead).toHaveBeenCalledTimes(2)
      expect(h.api.liveRecoveryTerminal.value).toBe(false)
      expect(h.api.livePhase.value).toBe('ready')
    } finally { h.api.cancelSessionBootstrap() }
  })

  it('bounds a stalled attempt and fences a late success without treating 15 seconds as failure', async () => {
    vi.useFakeTimers()
    let release!: (result: SessionSubscriptionOutcome) => void
    let signal!: AbortSignal
    const h = createBootstrap({
      subscribeSession: context => {
        signal = context.signal
        return new Promise(resolve => { release = resolve })
      },
    })
    try {
      const run = h.api.startSessionBootstrap()
      await vi.advanceTimersByTimeAsync(15_000)
      expect(h.api.liveRecoveryTerminal.value).toBe(false)
      expect(signal.aborted).toBe(false)
      await vi.advanceTimersByTimeAsync(105_000)
      await expect(run.live).resolves.toMatchObject({ authoritative: false,
        error: { kind: 'budget-exhausted', retryable: false } })
      expect(signal.aborted).toBe(true)
      expect(h.api.liveRecoveryTerminal.value).toBe(true)
      release(LIVE_READY)
      await vi.advanceTimersByTimeAsync(0)
      expect(h.api.livePhase.value).toBe('degraded')
      expect(h.subscribeSession).toHaveBeenCalledOnce()
    } finally { h.api.cancelSessionBootstrap() }
  })

  it('retains the incident deadline when a gap requires a fresh read after an older success', async () => {
    vi.useFakeTimers()
    let release!: (result: SessionSubscriptionOutcome) => void
    const contexts: SessionBootstrapPhaseContext[] = []
    const h = createBootstrap({
      subscribeSession: context => {
        contexts.push(context)
        return new Promise(resolve => { release = resolve })
      },
      reconcileSession: async context => {
        contexts.push(context)
        return { ...LIVE_READY, authoritative: false,
          error: new SessionReadFailure('busy', 'retry', true, 5_000) }
      },
    })
    try {
      const initial = h.api.startSessionBootstrap()
      await vi.advanceTimersByTimeAsync(30_000)
      const recovery = h.api.retryLive()
      release(LIVE_READY)
      await initial.live
      expect(h.api.livePhase.value).not.toBe('ready')
      await recovery
      expect(contexts[1]!.deadlineAt).toBe(contexts[0]!.deadlineAt)
      await vi.advanceTimersByTimeAsync(90_000)
      expect(h.api.liveRecoveryTerminal.value).toBe(true)
    } finally { h.api.cancelSessionBootstrap() }
  })

  it.each([false, true])('keeps flow retries and version churn behind coordinator admission (v2=%s)', async v2 => {
    vi.useFakeTimers()
    const failure = { ...LIVE_READY, authoritative: false,
      error: new SessionReadFailure('busy', 'storage busy', true, 5_000) }
    const reconcile = vi.fn(async () => failure)
    const h = createBootstrap({ connectionState: ref('connected'),
      subscribeSession: async () => failure, reconcileSession: reconcile })
    const listeners = new Map<string, TransportEventHandler>()
    const recover = vi.fn(async () => {
      try { return sessionRecoverySucceeded(await h.api.retryLive()) }
      catch { return { retryable: false } as const }
    })
    const request = vi.fn(async (_method: string, params: Record<string, unknown> = {}) => ({
      delivery_epoch: 'epoch', ack_delivery_id: params.ack_delivery_id ?? 0,
      dirty_keys: [], global_dirty: false,
      connection_epoch: 'epoch', consumed: params.consumed ?? [],
      staged_recovery: params.staged_recovery ?? [], discarded_lanes: params.discarded_lanes ?? [],
    }))
    const flow = new TransportFlowV4({
      connectionGeneration: 1, supportsRecovery: () => true,
      enableConsumptionFlow() {},
      request: <T>(method: string, params?: Record<string, unknown>) => request(method, params) as Promise<T>,
      consumeEvent: async () => { throw new Error('No consumption owner') },
      recoverGap: recover,
      on(event, handler) { listeners.set(event, handler); return () => { listeners.delete(event) } },
    })
    try {
      await h.api.startSessionBootstrap().live
      listeners.get('_hello')!({ policy: { transport_flow: {
        delivery_epoch: 'epoch', window_frames: 128, window_bytes: 4 * 1024 * 1024,
        ...(v2 ? { capability: 'transport.session-flow.v2' } : {}),
      } } })
      const key = 'agent:main:webchat:bootstrap-test'
      listeners.get('*')!('session.event.text_delta', { session_key: key }, {
        flow: { delivery_epoch: 'epoch', delivery_id: 1 },
        ...(v2 ? { session_flow_v2: { connection_epoch: 'epoch', subscription_epoch: 'lane', delivery_id: 1 } } : {}),
      })
      for (let index = 0; index < 19; index++) {
        await vi.advanceTimersByTimeAsync(250)
        listeners.get('*')!('transport.flow.dirty', {
          delivery_epoch: 'epoch', dirty_keys: [key], global_dirty: false,
        }, {})
      }
      expect(reconcile).not.toHaveBeenCalled()
      await vi.advanceTimersByTimeAsync(250)
      expect(reconcile).toHaveBeenCalledOnce()
      await vi.advanceTimersByTimeAsync(4_999)
      expect(reconcile).toHaveBeenCalledOnce()
      await vi.advanceTimersByTimeAsync(1)
      expect(reconcile).toHaveBeenCalledTimes(2)
      await vi.advanceTimersByTimeAsync(112_000)
      expect(h.api.liveRecoveryTerminal.value).toBe(true)
      const calls = reconcile.mock.calls.length
      const flowCalls = recover.mock.calls.length
      await vi.advanceTimersByTimeAsync(30_000)
      expect(reconcile).toHaveBeenCalledTimes(calls)
      expect(recover).toHaveBeenCalledTimes(flowCalls)
      expect(flow.diagnostics.unownedFrames).toBe(1)
      expect(flow.diagnostics.acknowledgedDeliveryId).toBe(0)
      expect(h.openSessionRead).toHaveBeenCalledOnce()
    } finally { flow.close(); h.api.cancelSessionBootstrap() }
  })
})


describe('incident presentation budget', () => {
  it('degrades at 15 seconds while a 45 second progressing live transfer continues', async () => {
    vi.useFakeTimers()
    let complete!: (result: SessionSubscriptionOutcome) => void
    const pending = new Promise<SessionSubscriptionOutcome>(resolve => { complete = resolve })
    const h = createBootstrap({ subscribeSession: () => pending, connectionState: ref('connected') })
    const run = h.api.startSessionBootstrap()
    await vi.advanceTimersByTimeAsync(15_000)
    expect(h.api.livePhase.value).toBe('degraded')
    expect(h.closeLease).not.toHaveBeenCalled()
    const manual = h.api.retryLive(true)
    expect(h.api.retryBusy.value).toBe(true)
    expect(h.openSessionRead).toHaveBeenCalledOnce()
    h.api.dismissRecoveryNotice()
    await vi.advanceTimersByTimeAsync(30_000)
    expect(h.api.noticeDismissed.value).toBe(true)
    expect(h.api.livePhase.value).toBe('degraded')
    complete(LIVE_READY)
    await Promise.all([run.live, manual])
    expect(h.api.livePhase.value).toBe('ready')
    expect(h.api.retryBusy.value).toBe(false)
    h.api.cancelSessionBootstrap()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('keeps degradation and dismissal through automatic retries and socket replacement', async () => {
    vi.useFakeTimers()
    const failure = { ...LIVE_READY, authoritative: false, error: new SessionReadFailure('timeout', 'slow storage', true) }
    const h = createBootstrap({
      subscribeSession: async () => failure, reconcileSession: async () => failure,
      connectionState: ref('connected'),
    })
    await h.api.startSessionBootstrap().live
    h.api.dismissRecoveryNotice()
    await vi.advanceTimersByTimeAsync(15_000)
    const replacement = h.api.handleConnectionState('connected')
    await vi.advanceTimersByTimeAsync(15_000)
    await replacement?.live
    expect(h.api.livePhase.value).toBe('degraded')
    expect(h.api.noticeDismissed.value).toBe(true)
    await vi.advanceTimersByTimeAsync(60_000)
    expect(h.api.livePhase.value).toBe('degraded')
    expect(h.api.noticeDismissed.value).toBe(true)
    h.api.cancelSessionBootstrap()
  })
})
