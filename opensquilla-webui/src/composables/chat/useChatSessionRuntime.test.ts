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
})
