import { afterEach, describe, expect, it, vi } from 'vitest'
import { effectScope, nextTick, ref } from 'vue'

import { useChatSessionRuntime } from './useChatSessionRuntime'
import { useChatTaskOwnership } from './useChatTaskOwnership'
import { useChatComposerShortcuts } from './useChatComposerShortcuts'
import { createMemoryChatDraftStorage, useChatDraftPersistence } from './useChatDraftPersistence'
import { useLocalPathDraft } from './useLocalPathDraft'
import type { ChatMessage } from '@/types/chat'

describe('useChatSessionRuntime project drafts', () => {
  it('clears composer state when an explicit new task replaces an empty draft', () => {
    const sessionKey = ref('agent:main:webchat:project-a-draft')
    const resetDraftComposer = vi.fn()
    const resetInputHistory = vi.fn()
    const taskOwnership = useChatTaskOwnership()
    taskOwnership.noteRunning('task-project-a')
    const activeStreamTaskId = ref('task-project-a')
    const activeStreamSessionKey = ref(sessionKey.value)
    const acceptanceStopPending = ref(true)
    const runtime = useChatSessionRuntime({
      sessionKey,
      messages: ref<ChatMessage[]>([]),
      pendingSessionIntent: ref<string | null>('new_chat'),
      routerDecisionPending: ref(null),
      currentEpoch: ref(0),
      lastStreamSeq: ref(0),
      activeTaskGroups: ref(new Set<string>()),
      taskOwnership,
      activeStreamTaskId,
      activeStreamSessionKey,
      acceptanceStopPending,
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
      resetInputHistory,
      resetDraftComposer,
    })

    runtime.startDraftSession('main')

    expect(sessionKey.value).toBe('agent:main:webchat:project-b-draft')
    expect(resetDraftComposer).toHaveBeenCalledOnce()
    expect(resetInputHistory).toHaveBeenCalledOnce()
    expect(taskOwnership.runningTaskId.value).toBe('')
    expect(taskOwnership.queuedTaskIds.value.size).toBe(0)
    expect(taskOwnership.hydrationResolved.value).toBe(true)
    expect(activeStreamTaskId.value).toBe('')
    expect(activeStreamSessionKey.value).toBe('')
    expect(acceptanceStopPending.value).toBe(false)
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

describe('session navigation with composer history and persisted drafts', () => {
  afterEach(() => { vi.unstubAllGlobals() })

  it.each([
    [0, 'ArrowUp'], [0, 'ArrowDown'], [1, 'ArrowUp'], [1, 'ArrowDown'],
    [5, 'ArrowUp'], [5, 'ArrowDown'],
  ] as const)('preserves B draft and paths with %i history entries after %s', async (count, key) => {
    vi.stubGlobal('HTMLTextAreaElement', class {})
    const scope = effectScope()
    const sessionA = 'agent:main:webchat:history-a'
    const sessionB = 'agent:main:webchat:history-b'
    const sessionKey = ref(sessionA)
    const messages = ref<ChatMessage[]>(Array.from({ length: 5 }, (_, index) => ({
      role: 'user', text: `A request ${index + 1}`, ts: null,
    })))
    const draft = useLocalPathDraft()
    const storage = createMemoryChatDraftStorage()
    const savedB = JSON.stringify({
      version: 1, text: 'B saved draft', selectedSkills: [], localPaths: ['C:\\B-draft.pdf'],
    })
    storage.setItem(`opensquilla.chat.draft:${sessionB}`, savedB)
    scope.run(() => useChatDraftPersistence({
      sessionKey, inputText: draft.composerText, localPaths: draft.localPaths, storage,
    }))
    const shortcuts = useChatComposerShortcuts({
      inputText: draft.inputText,
      textareaText: draft.composerText,
      localPathReferences: draft.localPaths,
      restoreInput: draft.restoreInput,
      composing: ref(false),
      messages,
      pendingQueue: ref([]),
      canQueueMore: ref(true),
      slashOpen: ref(false),
      slashIdx: ref(0),
      filteredSlashCmds: ref([]),
      isStreaming: ref(false),
      isSafariWebKit: () => false,
      autoResizeTextarea: vi.fn(),
      handleSlashInput: vi.fn(),
      closeSlashMenu: vi.fn(),
      completeSlashCmd: vi.fn(),
      activateSlashCmd: vi.fn(),
      popPendingTail: () => false,
      enqueuePendingInput: () => false,
      sendCurrentInput: vi.fn(),
    })
    const resetDraftComposer = vi.fn(() => { draft.inputText.value = '' })
    const runtime = useChatSessionRuntime({
      sessionKey, messages,
      pendingSessionIntent: ref(null),
      routerDecisionPending: ref(null),
      currentEpoch: ref(0),
      lastStreamSeq: ref(0),
      activeTaskGroups: ref(new Set<string>()),
      aborted: ref(false),
      lastHeaderRole: ref(''),
      lastHeaderDay: ref(''),
      usageAccum: ref({
        input: 0, output: 0, cacheRead: 0, cacheWrite: 0, cost: null,
        routedTurns: 0, sessionSaved: 0,
      }),
      usageModel: ref(''),
      createSessionKey: () => '',
      persistSession: target => { sessionKey.value = target },
      cancelSessionBootstrap: vi.fn(),
      startSessionBootstrap: () => {
        messages.value = Array.from({ length: count }, () => ({
          role: 'user', text: 'B previous request', ts: null,
        }))
        return {
          generation: 1,
          criticalRequestsQueued: Promise.resolve(),
          history: Promise.resolve({ ok: true }),
          live: Promise.resolve({ authoritative: true, live: false, backgroundOnly: false }),
        }
      },
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
      resetInputHistory: shortcuts.resetInputHistory,
      resetDraftComposer,
    })
    const press = (key: string) => {
      const event = { key, target: null, preventDefault: vi.fn() } as unknown as KeyboardEvent
      shortcuts.onTextareaKeydown(event)
      return event
    }

    try {
      press('ArrowUp')
      expect(draft.composerText.value).toBe('A request 5')
      // Switch before the input-save watcher flushes, as a real sidebar click can.
      await runtime.switchToSession(sessionB)
      expect(draft.composerText.value).toBe('B saved draft')
      expect(draft.localPaths.value).toEqual(['C:\\B-draft.pdf'])
      expect(resetDraftComposer).not.toHaveBeenCalled()
      const event = press(key)
      await nextTick()

      expect(event.preventDefault).not.toHaveBeenCalled()
      expect(draft.inputText.value).toBe('B saved draft\nC:\\B-draft.pdf')
      expect(storage.getItem(`opensquilla.chat.draft:${sessionB}`)).toBe(savedB)
      expect(storage.getItem(`opensquilla.chat.draft:${sessionA}`)).toBe('A request 5')
    } finally {
      scope.stop()
    }
  })
})
