import { describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'

import { useChatSessionRuntime } from './useChatSessionRuntime'
import type { ChatMessage } from '@/types/chat'

describe('useChatSessionRuntime project drafts', () => {
  it('clears composer state when an explicit new task replaces an empty draft', () => {
    const sessionKey = ref('agent:main:webchat:project-a-draft')
    const resetDraftComposer = vi.fn()
    const runtime = useChatSessionRuntime({
      sessionKey,
      messages: ref<ChatMessage[]>([]),
      pendingSessionIntent: ref<string | null>('new_chat'),
      routerDecisionPending: ref(null),
      currentEpoch: ref(0),
      lastStreamSeq: ref(0),
      activeTaskGroups: ref(new Set<string>()),
      aborted: ref(false),
      lastHeaderRole: ref(''),
      lastHeaderDay: ref(''),
      usageAccum: ref({
        input: 0,
        output: 0,
        cacheRead: 0,
        cacheWrite: 0,
        cost: null,
        routedTurns: 0,
        sessionSaved: 0,
      }),
      usageModel: ref(''),
      createSessionKey: vi.fn(() => 'agent:main:webchat:project-b-draft'),
      persistSession: vi.fn(),
      unsubscribeSession: vi.fn(),
      subscribeSession: vi.fn(),
      loadHistory: vi.fn(),
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
      resetDraftComposer,
    })

    runtime.startDraftSession('main')

    expect(sessionKey.value).toBe('agent:main:webchat:project-b-draft')
    expect(resetDraftComposer).toHaveBeenCalledOnce()
  })
})
