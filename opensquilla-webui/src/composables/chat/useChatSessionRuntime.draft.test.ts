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
      resetDraftComposer,
    })

    runtime.startDraftSession('main')

    expect(sessionKey.value).toBe('agent:main:webchat:project-b-draft')
    expect(resetDraftComposer).toHaveBeenCalledOnce()
  })

  it('loads optional usage once critical requests are queued without waiting for history', async () => {
    const sessionKey = ref('agent:main:webchat:first')
    let resolveCriticalRequestsQueued!: () => void
    let resolveHistory!: () => void
    let resolveLive!: () => void
    const criticalRequestsQueued = new Promise<void>(resolve => {
      resolveCriticalRequestsQueued = resolve
    })
    const history = new Promise<{ ok: boolean }>(resolve => {
      resolveHistory = () => resolve({ ok: true })
    })
    const live = new Promise<{
      authoritative: boolean
      live: boolean
      backgroundOnly: boolean
    }>(resolve => {
      resolveLive = () => resolve({
        authoritative: true,
        live: false,
        backgroundOnly: false,
      })
    })
    const order: string[] = []
    const startSessionBootstrap = vi.fn(() => {
      order.push('bootstrap')
      return {
        generation: 1,
        criticalRequestsQueued,
        history,
        live,
      }
    })
    const loadCurrentSessionUsage = vi.fn(() => {
      order.push('usage')
    })
    const runtime = useChatSessionRuntime({
      sessionKey,
      messages: ref<ChatMessage[]>([]),
      pendingSessionIntent: ref<string | null>(null),
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
      createSessionKey: vi.fn(),
      persistSession: vi.fn((key: string) => {
        sessionKey.value = key
      }),
      cancelSessionBootstrap: vi.fn(),
      startSessionBootstrap,
      loadCurrentSessionUsage,
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

    const switching = runtime.switchToSession('agent:main:webchat:second')
    expect(order).toEqual(['bootstrap'])
    expect(loadCurrentSessionUsage).not.toHaveBeenCalled()

    resolveCriticalRequestsQueued()
    await vi.waitFor(() => expect(loadCurrentSessionUsage).toHaveBeenCalledOnce())
    expect(order).toEqual(['bootstrap', 'usage'])

    resolveLive()
    await switching
    expect(loadCurrentSessionUsage).toHaveBeenCalledOnce()

    resolveHistory()
    await history
  })
})
