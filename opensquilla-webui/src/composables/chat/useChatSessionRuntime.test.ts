import { ref } from 'vue'
import { describe, expect, it, vi } from 'vitest'

import { useChatSessionRuntime, type ChatUsageAccumulator } from './useChatSessionRuntime'
import type { Attachment, ChatMessage } from '@/types/chat'

function emptyUsage(): ChatUsageAccumulator {
  return {
    input: 0,
    output: 0,
    cacheRead: 0,
    cacheWrite: 0,
    cost: null,
    routedTurns: 0,
    sessionSaved: 0,
  }
}

function runtimeHarness(
  initialSessionKey: string,
  generatedSessionKey = 'agent:main:webchat:generated',
) {
  const sessionKey = ref(initialSessionKey)
  const pendingSessionIntent = ref<string | null>(null)
  const persistSession = vi.fn((key: string) => { sessionKey.value = key })
  const beginSessionResolution = vi.fn()
  const cancelSessionBootstrap = vi.fn()
  const setSessionHandoffTarget = vi.fn()
  const switchPendingQueue = vi.fn()
  const adoptPendingQueue = vi.fn()
  const retireAttachments = vi.fn()
  const resetDraftComposer = vi.fn()
  const bootstrappedSessionKeys: string[] = []
  const liveOutcome = {
    authoritative: true,
    live: false,
    backgroundOnly: false,
  }
  const startSessionBootstrap = vi.fn((
    _options?: { includeHistory?: boolean; force?: boolean },
  ) => {
    bootstrappedSessionKeys.push(sessionKey.value)
    return {
      generation: 1,
      criticalRequestsQueued: Promise.resolve(),
      history: Promise.resolve({ ok: true }),
      live: Promise.resolve(liveOutcome),
    }
  })
  const runtime = useChatSessionRuntime({
    sessionKey,
    messages: ref<ChatMessage[]>([]),
    pendingSessionIntent,
    routerDecisionPending: ref(null),
    currentEpoch: ref(0),
    lastStreamSeq: ref(0),
    activeTaskGroups: ref(new Set<string>()),
    aborted: ref(false),
    lastHeaderRole: ref(''),
    lastHeaderDay: ref(''),
    usageAccum: ref(emptyUsage()),
    usageModel: ref(''),
    createSessionKey: () => generatedSessionKey,
    persistSession,
    beginSessionResolution,
    cancelSessionBootstrap,
    setSessionHandoffTarget,
    startSessionBootstrap,
    loadCurrentSessionUsage: vi.fn(),
    applySessionRunState: vi.fn(),
    setCompactInFlight: vi.fn(),
    hideCompactStatus: vi.fn(),
    clearPendingQueue: vi.fn(),
    switchPendingQueue,
    adoptPendingQueue,
    resetSavingsPopupCooldown: vi.fn(),
    restoreWidgetState: vi.fn(),
    resetStreamLiveTurnState: vi.fn(),
    retireAttachments,
    resetDraftComposer,
  })

  return {
    adoptPendingQueue,
    beginSessionResolution,
    bootstrappedSessionKeys,
    cancelSessionBootstrap,
    liveOutcome,
    pendingSessionIntent,
    persistSession,
    resetDraftComposer,
    retireAttachments,
    runtime,
    sessionKey,
    setSessionHandoffTarget,
    startSessionBootstrap,
    switchPendingQueue,
  }
}

describe('useChatSessionRuntime Meta draft recovery', () => {
  it('rebinds an untouched provisional draft without persisting it', async () => {
    const sessionKey = ref('agent:main:webchat:local-draft')
    const pendingSessionIntent = ref<string | null>('new_chat')
    const switchPendingQueue = vi.fn()
    const persistSession = vi.fn((key: string) => { sessionKey.value = key })
    const cancelSessionBootstrap = vi.fn()
    const retireAttachments = vi.fn()
    const liveOutcome = {
      authoritative: true,
      live: false,
      backgroundOnly: false,
    }
    const startSessionBootstrap = vi.fn(() => ({
      generation: 2,
      criticalRequestsQueued: Promise.resolve(),
      history: Promise.resolve({ ok: true }),
      live: Promise.resolve(liveOutcome),
    }))
    const runtime = useChatSessionRuntime({
      sessionKey,
      messages: ref<ChatMessage[]>([]),
      pendingSessionIntent,
      routerDecisionPending: ref(null),
      currentEpoch: ref(0),
      lastStreamSeq: ref(0),
      activeTaskGroups: ref(new Set<string>()),
      aborted: ref(false),
      lastHeaderRole: ref(''),
      lastHeaderDay: ref(''),
      usageAccum: ref(emptyUsage()),
      usageModel: ref(''),
      createSessionKey: () => 'agent:main:webchat:draft',
      persistSession,
      cancelSessionBootstrap,
      startSessionBootstrap,
      loadCurrentSessionUsage: vi.fn(),
      applySessionRunState: vi.fn(),
      setCompactInFlight: vi.fn(),
      hideCompactStatus: vi.fn(),
      clearPendingQueue: vi.fn(),
      switchPendingQueue,
      adoptPendingQueue: vi.fn(),
      resetSavingsPopupCooldown: vi.fn(),
      restoreWidgetState: vi.fn(),
      resetStreamLiveTurnState: vi.fn(),
      retireAttachments,
    })

    await expect(runtime.rebindDraftSession(
      'agent:main:webchat:server-draft',
      () => true,
    )).resolves.toEqual(liveOutcome)

    expect(cancelSessionBootstrap).toHaveBeenCalledOnce()
    expect(sessionKey.value).toBe('agent:main:webchat:server-draft')
    expect(pendingSessionIntent.value).toBe('new_chat')
    expect(switchPendingQueue).toHaveBeenCalledWith(
      'agent:main:webchat:server-draft',
      expect.any(Function),
      expect.anything(),
    )
    expect(startSessionBootstrap).toHaveBeenCalledWith({ includeHistory: false })
    expect(persistSession).not.toHaveBeenCalled()
    expect(retireAttachments).not.toHaveBeenCalled()
  })

  it('does not rebind after the draft ownership guard fails', async () => {
    const sessionKey = ref('agent:main:webchat:local-draft')
    const runtime = useChatSessionRuntime({
      sessionKey,
      messages: ref<ChatMessage[]>([]),
      pendingSessionIntent: ref('new_chat'),
      routerDecisionPending: ref(null),
      currentEpoch: ref(0),
      lastStreamSeq: ref(0),
      activeTaskGroups: ref(new Set<string>()),
      aborted: ref(false),
      lastHeaderRole: ref(''),
      lastHeaderDay: ref(''),
      usageAccum: ref(emptyUsage()),
      usageModel: ref(''),
      createSessionKey: () => 'agent:main:webchat:draft',
      persistSession: vi.fn(),
      cancelSessionBootstrap: vi.fn(),
      startSessionBootstrap: vi.fn(),
      loadCurrentSessionUsage: vi.fn(),
      applySessionRunState: vi.fn(),
      setCompactInFlight: vi.fn(),
      hideCompactStatus: vi.fn(),
      clearPendingQueue: vi.fn(),
      switchPendingQueue: vi.fn(),
      adoptPendingQueue: vi.fn(),
      resetSavingsPopupCooldown: vi.fn(),
      restoreWidgetState: vi.fn(),
      resetStreamLiveTurnState: vi.fn(),
    })

    await expect(runtime.rebindDraftSession(
      'agent:main:webchat:server-draft',
      () => false,
    )).resolves.toBe(false)
    expect(sessionKey.value).toBe('agent:main:webchat:local-draft')
  })

  it('keeps the source bootstrap until a delayed queue switch can commit', async () => {
    const sessionKey = ref('agent:main:webchat:a')
    let finishQueue!: () => void
    const queue = new Promise<void>(resolve => { finishQueue = resolve })
    const cancelSessionBootstrap = vi.fn()
    const persistSession = vi.fn((key: string) => { sessionKey.value = key })
    const setSessionHandoffTarget = vi.fn()
    const beginSessionResolution = vi.fn()
    const retireAttachments = vi.fn()
    const runtime = useChatSessionRuntime({
      sessionKey,
      messages: ref<ChatMessage[]>([]),
      pendingSessionIntent: ref(null),
      routerDecisionPending: ref(null),
      currentEpoch: ref(0),
      lastStreamSeq: ref(0),
      activeTaskGroups: ref(new Set<string>()),
      aborted: ref(false),
      lastHeaderRole: ref(''),
      lastHeaderDay: ref(''),
      usageAccum: ref(emptyUsage()),
      usageModel: ref(''),
      createSessionKey: () => '',
      persistSession,
      beginSessionResolution,
      cancelSessionBootstrap,
      setSessionHandoffTarget,
      startSessionBootstrap: vi.fn(() => ({
        generation: 1,
        criticalRequestsQueued: Promise.resolve(),
        history: Promise.resolve({ ok: true }),
        live: Promise.resolve({
          authoritative: true,
          live: false,
          backgroundOnly: false,
        }),
      })),
      loadCurrentSessionUsage: vi.fn(),
      applySessionRunState: vi.fn(),
      setCompactInFlight: vi.fn(),
      hideCompactStatus: vi.fn(),
      clearPendingQueue: vi.fn(),
      switchPendingQueue: vi.fn(() => queue),
      adoptPendingQueue: vi.fn(),
      resetSavingsPopupCooldown: vi.fn(),
      restoreWidgetState: vi.fn(),
      resetStreamLiveTurnState: vi.fn(),
      retireAttachments,
    })

    const switching = runtime.switchToSession('agent:main:webchat:b')
    expect(sessionKey.value).toBe('agent:main:webchat:a')
    expect(cancelSessionBootstrap).not.toHaveBeenCalled()
    expect(beginSessionResolution).not.toHaveBeenCalled()
    expect(retireAttachments).not.toHaveBeenCalled()

    finishQueue()
    await switching

    expect(cancelSessionBootstrap).toHaveBeenCalledOnce()
    expect(beginSessionResolution).toHaveBeenCalledOnce()
    expect(beginSessionResolution).toHaveBeenCalledWith('agent:main:webchat:b')
    expect(persistSession).toHaveBeenCalledWith(
      'agent:main:webchat:b',
      { source: 'runtime.switchToSession' },
    )
    expect(setSessionHandoffTarget).toHaveBeenNthCalledWith(
      1,
      'agent:main:webchat:b',
      1,
    )
    expect(setSessionHandoffTarget).toHaveBeenLastCalledWith(null, 1, 'committed')
    expect(retireAttachments).toHaveBeenCalledOnce()
  })

  it('supersedes delayed A to B when navigation returns to A', async () => {
    const sessionKey = ref('agent:main:webchat:a')
    let finishQueue!: () => void
    const queue = new Promise<void>(resolve => { finishQueue = resolve })
    const cancelSessionBootstrap = vi.fn()
    const persistSession = vi.fn((key: string) => { sessionKey.value = key })
    const beginSessionResolution = vi.fn()
    const retireAttachments = vi.fn()
    const switchPendingQueue = vi.fn((
      _key: string,
      shouldCommit?: () => boolean,
      _handoffSignal?: AbortSignal,
    ) => queue.then(() => { shouldCommit?.() }))
    const runtime = useChatSessionRuntime({
      sessionKey,
      messages: ref<ChatMessage[]>([]),
      pendingSessionIntent: ref(null),
      routerDecisionPending: ref(null),
      currentEpoch: ref(0),
      lastStreamSeq: ref(0),
      activeTaskGroups: ref(new Set<string>()),
      aborted: ref(false),
      lastHeaderRole: ref(''),
      lastHeaderDay: ref(''),
      usageAccum: ref(emptyUsage()),
      usageModel: ref(''),
      createSessionKey: () => '',
      persistSession,
      beginSessionResolution,
      cancelSessionBootstrap,
      startSessionBootstrap: vi.fn(),
      loadCurrentSessionUsage: vi.fn(),
      applySessionRunState: vi.fn(),
      setCompactInFlight: vi.fn(),
      hideCompactStatus: vi.fn(),
      clearPendingQueue: vi.fn(),
      switchPendingQueue,
      adoptPendingQueue: vi.fn(),
      resetSavingsPopupCooldown: vi.fn(),
      restoreWidgetState: vi.fn(),
      resetStreamLiveTurnState: vi.fn(),
      retireAttachments,
    })

    const toB = runtime.switchToSession('agent:main:webchat:b')
    const supersededSignal = switchPendingQueue.mock.calls[0]?.[2]
    expect(supersededSignal?.aborted).toBe(false)
    await runtime.switchToSession('agent:main:webchat:a')
    expect(supersededSignal?.aborted).toBe(true)
    finishQueue()
    await toB

    expect(sessionKey.value).toBe('agent:main:webchat:a')
    expect(cancelSessionBootstrap).not.toHaveBeenCalled()
    expect(beginSessionResolution).not.toHaveBeenCalled()
    expect(persistSession).not.toHaveBeenCalled()
    expect(retireAttachments).not.toHaveBeenCalled()
    const commitGuard = switchPendingQueue.mock.calls[0]?.[1]
    expect(commitGuard?.()).toBe(false)
  })

  it('leaves the source bootstrap active when queue adoption fails', async () => {
    const sessionKey = ref('agent:main:webchat:a')
    const cancelSessionBootstrap = vi.fn()
    const beginSessionResolution = vi.fn()
    const retireAttachments = vi.fn()
    const failure = new Error('queue adoption failed')
    const runtime = useChatSessionRuntime({
      sessionKey,
      messages: ref<ChatMessage[]>([]),
      pendingSessionIntent: ref(null),
      routerDecisionPending: ref(null),
      currentEpoch: ref(0),
      lastStreamSeq: ref(0),
      activeTaskGroups: ref(new Set<string>()),
      aborted: ref(false),
      lastHeaderRole: ref(''),
      lastHeaderDay: ref(''),
      usageAccum: ref(emptyUsage()),
      usageModel: ref(''),
      createSessionKey: () => '',
      persistSession: vi.fn(),
      beginSessionResolution,
      cancelSessionBootstrap,
      startSessionBootstrap: vi.fn(),
      loadCurrentSessionUsage: vi.fn(),
      applySessionRunState: vi.fn(),
      setCompactInFlight: vi.fn(),
      hideCompactStatus: vi.fn(),
      clearPendingQueue: vi.fn(),
      switchPendingQueue: vi.fn(async () => { throw failure }),
      adoptPendingQueue: vi.fn(),
      resetSavingsPopupCooldown: vi.fn(),
      restoreWidgetState: vi.fn(),
      resetStreamLiveTurnState: vi.fn(),
      retireAttachments,
    })

    await expect(runtime.switchToSession('agent:main:webchat:b')).rejects.toBe(failure)
    expect(sessionKey.value).toBe('agent:main:webchat:a')
    expect(cancelSessionBootstrap).not.toHaveBeenCalled()
    expect(beginSessionResolution).not.toHaveBeenCalled()
    expect(retireAttachments).not.toHaveBeenCalled()
  })

  it('preserves attachments for same-key navigation and canonical adoption', async () => {
    const sessionKey = ref('agent:main:webchat:a')
    const pendingAttachments = ref<Attachment[]>([{
      kind: 'inline',
      local_id: 1,
      name: 'goal-context.txt',
      mime: 'text/plain',
      data: 'Z29hbCBjb250ZXh0',
    }])
    const retireAttachments = vi.fn(() => { pendingAttachments.value = [] })
    const runtime = useChatSessionRuntime({
      sessionKey,
      messages: ref<ChatMessage[]>([]),
      pendingSessionIntent: ref(null),
      routerDecisionPending: ref(null),
      currentEpoch: ref(0),
      lastStreamSeq: ref(0),
      activeTaskGroups: ref(new Set<string>()),
      aborted: ref(false),
      lastHeaderRole: ref(''),
      lastHeaderDay: ref(''),
      usageAccum: ref(emptyUsage()),
      usageModel: ref(''),
      createSessionKey: () => '',
      persistSession: key => { sessionKey.value = key },
      cancelSessionBootstrap: vi.fn(),
      startSessionBootstrap: vi.fn(() => ({
        generation: 1,
        criticalRequestsQueued: Promise.resolve(),
        history: Promise.resolve({ ok: true }),
        live: Promise.resolve({
          authoritative: true,
          live: false,
          backgroundOnly: false,
        }),
      })),
      loadCurrentSessionUsage: vi.fn(),
      applySessionRunState: vi.fn(),
      setCompactInFlight: vi.fn(),
      hideCompactStatus: vi.fn(),
      clearPendingQueue: vi.fn(),
      switchPendingQueue: vi.fn(),
      adoptPendingQueue: vi.fn(),
      resetSavingsPopupCooldown: vi.fn(),
      restoreWidgetState: vi.fn(),
      resetStreamLiveTurnState: vi.fn(),
      retireAttachments,
    })

    await runtime.switchToSession('agent:main:webchat:a')
    await runtime.adoptMaterializedSession('agent:main:webchat:b')
    await runtime.adoptResponseSession('agent:main:webchat:c', 'request-a')

    expect(sessionKey.value).toBe('agent:main:webchat:c')
    expect(retireAttachments).not.toHaveBeenCalled()
    expect(pendingAttachments.value).toHaveLength(1)

    await runtime.switchToSession('agent:main:webchat:a')
    expect(retireAttachments).toHaveBeenCalledOnce()
    expect(pendingAttachments.value).toEqual([])
  })

  it.each([
    ['short legacy key', 'agent:main:webchat:same', 'sess-same'],
    ['default-agent key', 'agent:main:webchat:same', 'agent:default:webchat:same'],
    ['trimmed key', 'agent:main:webchat:same', '  agent:main:webchat:same  '],
    ['default key', 'agent:main:webchat:default', '  default  '],
    ['webchat default key', 'agent:main:webchat:default', ' webchat:default '],
    ['blank default key', '   ', 'default'],
  ])('treats a %s alias as unchanged navigation', async (
    _label,
    sourceKey,
    targetAlias,
  ) => {
    const harness = runtimeHarness(sourceKey)

    await expect(harness.runtime.switchToSession(targetAlias)).resolves.toBeUndefined()

    expect(harness.setSessionHandoffTarget).toHaveBeenNthCalledWith(
      1,
      sourceKey.trim() || 'agent:main:webchat:default',
      1,
    )
    expect(harness.setSessionHandoffTarget).toHaveBeenLastCalledWith(
      null,
      1,
      'unchanged',
    )
    expect(harness.switchPendingQueue).not.toHaveBeenCalled()
    expect(harness.persistSession).not.toHaveBeenCalled()
    expect(harness.cancelSessionBootstrap).not.toHaveBeenCalled()
    expect(harness.startSessionBootstrap).not.toHaveBeenCalled()
    expect(harness.retireAttachments).not.toHaveBeenCalled()
    expect(harness.sessionKey.value).toBe(sourceKey)
  })

  it('keeps every handoff policy inert for aliases of the current session', async () => {
    const sourceAlias = ' agent:default:webchat:same '
    const harness = runtimeHarness(sourceAlias, 'sess-same')

    await harness.runtime.adoptMaterializedSession('sess-same')
    await harness.runtime.adoptResponseSession(
      ' agent:main:webchat:same ',
      'request-same',
    )
    await expect(harness.runtime.rebindDraftSession(
      'agent:default:webchat:same',
      sourceKey => sourceKey === sourceAlias,
    )).resolves.toBe(false)
    await harness.runtime.startDraftSession('main')

    expect(harness.switchPendingQueue).not.toHaveBeenCalled()
    expect(harness.adoptPendingQueue).not.toHaveBeenCalled()
    expect(harness.persistSession).not.toHaveBeenCalled()
    expect(harness.cancelSessionBootstrap).not.toHaveBeenCalled()
    expect(harness.startSessionBootstrap).not.toHaveBeenCalled()
    expect(harness.retireAttachments).not.toHaveBeenCalled()
    expect(harness.resetDraftComposer).not.toHaveBeenCalled()
    expect(harness.sessionKey.value).toBe(sourceAlias)
    expect(
      harness.setSessionHandoffTarget.mock.calls
        .filter(([target]) => target !== null)
        .map(([target]) => target),
    ).toEqual(Array(4).fill('agent:main:webchat:same'))
    expect(
      harness.setSessionHandoffTarget.mock.calls
        .filter(([target]) => target === null)
        .map(([, , outcome]) => outcome),
    ).toEqual(Array(4).fill('unchanged'))
  })

  it('uses canonical keys throughout real switches while preserving queue and attachment policies', async () => {
    const harness = runtimeHarness(' agent:default:webchat:a ', 'sess-f')

    await expect(harness.runtime.switchToSession(' sess-b ')).resolves.toEqual({
      authoritative: true,
      authoritativeIdle: true,
      backgroundOnly: false,
    })
    await harness.runtime.adoptMaterializedSession('agent:default:webchat:c')
    await harness.runtime.adoptResponseSession(' sess-d ', 'request-d')
    await expect(harness.runtime.rebindDraftSession(
      ' agent:default:webchat:e ',
      sourceKey => sourceKey === 'agent:main:webchat:d',
    )).resolves.toEqual(harness.liveOutcome)
    await harness.runtime.startDraftSession('main')

    expect(harness.switchPendingQueue.mock.calls.map(([target]) => target)).toEqual([
      'agent:main:webchat:b',
      'agent:main:webchat:c',
      'agent:main:webchat:e',
      'agent:main:webchat:f',
    ])
    expect(harness.adoptPendingQueue).toHaveBeenCalledWith(
      'agent:main:webchat:d',
      'request-d',
      expect.any(Function),
      expect.anything(),
    )
    expect(harness.beginSessionResolution.mock.calls.map(([target]) => target)).toEqual([
      'agent:main:webchat:b',
      'agent:main:webchat:c',
      'agent:main:webchat:d',
    ])
    expect(harness.persistSession.mock.calls.map(([target]) => target)).toEqual([
      'agent:main:webchat:b',
      'agent:main:webchat:c',
      'agent:main:webchat:d',
    ])
    expect(harness.bootstrappedSessionKeys).toEqual([
      'agent:main:webchat:b',
      'agent:main:webchat:c',
      'agent:main:webchat:d',
      'agent:main:webchat:e',
      'agent:main:webchat:f',
    ])
    expect(harness.startSessionBootstrap.mock.calls.map(([bootstrapOptions]) => (
      bootstrapOptions
    ))).toEqual([
      { includeHistory: true },
      { includeHistory: true },
      { includeHistory: true },
      { includeHistory: false },
      { includeHistory: false },
    ])
    expect(harness.retireAttachments).toHaveBeenCalledTimes(2)
    expect(harness.resetDraftComposer).toHaveBeenCalledOnce()
    expect(harness.pendingSessionIntent.value).toBe('new_chat')
    expect(harness.sessionKey.value).toBe('agent:main:webchat:f')
    expect(
      harness.setSessionHandoffTarget.mock.calls
        .filter(([target]) => target !== null)
        .map(([target]) => target),
    ).toEqual([
      'agent:main:webchat:b',
      'agent:main:webchat:c',
      'agent:main:webchat:d',
      'agent:main:webchat:e',
      'agent:main:webchat:f',
    ])
  })
})
