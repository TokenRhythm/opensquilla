import { ref } from 'vue'
import { describe, expect, it, vi } from 'vitest'

import { useChatSessionRuntime, type ChatUsageAccumulator } from './useChatSessionRuntime'
import type { ChatMessage } from '@/types/chat'

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
    })

    await expect(runtime.rebindDraftSession(
      'agent:main:webchat:server-draft',
      () => true,
    )).resolves.toEqual(liveOutcome)

    expect(cancelSessionBootstrap).toHaveBeenCalledOnce()
    expect(sessionKey.value).toBe('agent:main:webchat:server-draft')
    expect(pendingSessionIntent.value).toBe('new_chat')
    expect(switchPendingQueue).toHaveBeenCalledWith('agent:main:webchat:server-draft')
    expect(startSessionBootstrap).toHaveBeenCalledWith({ includeHistory: false })
    expect(persistSession).not.toHaveBeenCalled()
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
})
