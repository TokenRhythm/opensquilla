import { describe, expect, it, vi } from 'vitest'
import { effectScope, nextTick, ref } from 'vue'
import { useChatRpcEventHandlers, type ChatRpcStreamApi } from './useChatRpcEventHandlers'
import type { SessionBootstrapRun } from './useChatSessionBootstrap'
import type {
  ChatMessage,
  ChatPendingItem,
  ChatRunStatus,
  ChatRunStatusSource,
} from '@/types/chat'
import {
  FINISHED_STREAM_TASK_ID,
  PENDING_STREAM_TASK_ID,
} from '@/utils/chat/streamEvents'

function createHarness(options: {
  messages?: ChatMessage[]
  endStreaming?: (messages: ChatMessage[]) => void
  sessionRunStatus?: (source: ChatRunStatusSource | null | undefined) => ChatRunStatus
  handleSessionConnectionState?: (state: string) => SessionBootstrapRun | undefined
  loadCurrentSessionUsage?: () => void
  refreshRunModePreference?: () => void | Promise<void>
  pendingQueue?: ChatPendingItem[]
  restoreSteerIntoComposer?: (text: string) => void
  getCompactionPlacement?: (compactionId: string) => 'activity' | 'standalone' | undefined
} = {}) {
  const messages = ref<ChatMessage[]>(options.messages ?? [])
  const sessionKey = ref('agent:main:test')
  const lastStreamSeq = ref(0)
  const activeTaskGroups = ref(new Set<string>())
  const activeStreamTaskId = ref('')
  const pendingQueue = ref<ChatPendingItem[]>(options.pendingQueue ?? [])
  const applySessionRunState = vi.fn()
  const stream: ChatRpcStreamApi = {
    isStreaming: ref(true),
    streamBubble: ref(true),
    streamHasVisibleOutput: ref(false),
    startStreaming: vi.fn(),
    endStreaming: vi.fn(() => options.endStreaming?.(messages.value)),
    appendDelta: vi.fn(),
    scheduleRender: vi.fn(),
    appendToolCall: vi.fn(),
    appendToolDelta: vi.fn(),
    appendToolResult: vi.fn(),
    appendArtifact: vi.fn(),
    reconcileFinalText: vi.fn(),
    resetLiveTurnState: vi.fn(),
    resetStreamIdleTimer: vi.fn(),
    clearStreamIdleTimer: vi.fn(),
    setStreamActivity: vi.fn(),
    recordCompactionActivity: vi.fn(),
    showThinkingIndicator: vi.fn(),
    hideThinkingIndicator: vi.fn(),
    appendFrame: vi.fn(),
    useReducer: ref(false),
  }
  const markEnsembleHandoff = vi.fn()
  const schedulePendingDrainAfterTerminal = vi.fn()
  const scheduleHistorySync = vi.fn()
  const showCompactionToast = vi.fn()
  const showWarningToast = vi.fn()
  const handleSessionConnectionState = vi.fn(
    options.handleSessionConnectionState ?? (() => undefined),
  )
  const loadCurrentSessionUsage = vi.fn(options.loadCurrentSessionUsage ?? (() => {}))
  const refreshRunModePreference = vi.fn(options.refreshRunModePreference ?? (() => {}))
  const restoreSteerIntoComposer = vi.fn(options.restoreSteerIntoComposer ?? (() => {}))
  const scope = effectScope()
  const api = scope.run(() => useChatRpcEventHandlers({
    sessionKey,
    currentEpoch: ref(0),
    lastStreamSeq,
    activeTaskGroups,
    activeStreamTaskId,
    aborted: ref(false),
    messages,
    pendingQueue,
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
    stream,
    normalizeRunStatus: (status: string) => status,
    sessionRunStatus: options.sessionRunStatus || (() => ({ status: 'idle', label: 'Idle', task: null })),
    applySessionRunState,
    queueRouterDecision: vi.fn(),
    appendEnsembleProgress: vi.fn(),
    markEnsembleHandoff,
    flushPendingRouterDecision: vi.fn(),
    clearPendingRouterDecision: vi.fn(),
    handleRouterControlReplay: vi.fn(),
    showCompactionToast,
    getCompactionPlacement: options.getCompactionPlacement,
    showWarningToast,
    scheduleHistorySync,
    schedulePendingDrainAfterTerminal,
    popAllPendingIntoComposer: vi.fn(() => false),
    restoreSteerIntoComposer,
    saveWidgetState: vi.fn(),
    handleSessionConnectionState,
    loadCurrentSessionUsage,
    refreshRunModePreference,
  }))!
  return {
    api,
    messages,
    sessionKey,
    lastStreamSeq,
    stream,
    activeTaskGroups,
    activeStreamTaskId,
    pendingQueue,
    applySessionRunState,
    markEnsembleHandoff,
    schedulePendingDrainAfterTerminal,
    scheduleHistorySync,
    showCompactionToast,
    showWarningToast,
    handleSessionConnectionState,
    loadCurrentSessionUsage,
    refreshRunModePreference,
    restoreSteerIntoComposer,
    stop: () => scope.stop(),
  }
}

describe('useChatRpcEventHandlers live snapshot restoration', () => {
  it('does not replace live task state for a recents-only session change', () => {
    const {
      api,
      activeStreamTaskId,
      applySessionRunState,
      stop,
    } = createHarness()
    try {
      activeStreamTaskId.value = 'task-live'

      api.handlers.onSessionsChanged({
        session_key: 'agent:main:test',
        reason: 'title_changed',
      })

      expect(applySessionRunState).not.toHaveBeenCalled()
    } finally {
      stop()
    }
  })

  it('rebuilds the unfinished turn while advancing to the authoritative cursor', () => {
    const {
      api,
      stream,
      activeStreamTaskId,
      lastStreamSeq,
      stop,
    } = createHarness()
    try {
      lastStreamSeq.value = 900
      api.restoreLiveTurnSnapshot({
        key: 'agent:main:test',
        task_id: 'task-live',
        current_stream_seq: 2400,
        events: [
          {
            event: 'session.event.thinking',
            payload: {
              session_key: 'agent:main:test',
              task_id: 'task-live',
              text: 'Recovered reasoning',
              stream_seq: 10,
            },
          },
          {
            event: 'session.event.tool_use_start',
            payload: {
              session_key: 'agent:main:test',
              task_id: 'task-live',
              id: 'tool-1',
              name: 'exec',
              stream_seq: 11,
            },
          },
          {
            event: 'session.event.text_delta',
            payload: {
              session_key: 'agent:main:test',
              task_id: 'task-live',
              text: 'Recovered answer',
              presentation: 'answer',
              stream_seq: 12,
            },
          },
        ],
      })

      expect(stream.resetLiveTurnState).toHaveBeenCalledOnce()
      expect(api.streamThinkingText.value).toBe('Recovered reasoning')
      expect(stream.appendToolCall).toHaveBeenCalledWith(expect.objectContaining({
        id: 'tool-1',
      }))
      expect(stream.appendDelta).toHaveBeenCalledWith('Recovered answer', 'answer')
      expect(activeStreamTaskId.value).toBe('task-live')
      expect(lastStreamSeq.value).toBe(2400)
    } finally {
      stop()
    }
  })

  it('restores an active compaction from the authoritative live snapshot', () => {
    const {
      api,
      lastStreamSeq,
      stream,
      showCompactionToast,
      stop,
    } = createHarness()
    try {
      stream.isStreaming.value = false
      vi.mocked(stream.startStreaming).mockImplementation(() => {
        stream.isStreaming.value = true
      })
      api.restoreLiveTurnSnapshot({
        key: 'agent:main:test',
        task_id: 'task-live',
        current_stream_seq: 2400,
        events: [
          {
            event: 'session.event.compaction',
            payload: {
              session_key: 'agent:main:test',
              status: 'started',
              phase: 'summarizing',
              compaction_id: 'cmp-live',
              task_id: 'task-live',
              sequence: 1,
              stream_seq: 2399,
            },
          },
        ],
      })

      expect(showCompactionToast).toHaveBeenCalledWith(
        expect.objectContaining({
          status: 'started',
          phase: 'summarizing',
          compaction_id: 'cmp-live',
          sequence: 1,
        }),
        expect.objectContaining({
          authoritativeLive: true,
          placement: 'activity',
          replayed: false,
        }),
      )
      expect(showCompactionToast.mock.calls[0][0]).not.toHaveProperty('stream_seq')
      expect(stream.startStreaming).toHaveBeenCalledOnce()
      expect(stream.recordCompactionActivity).toHaveBeenCalledWith(expect.objectContaining({
        compaction_id: 'cmp-live',
      }))
      expect(lastStreamSeq.value).toBe(2400)
    } finally {
      stop()
    }
  })
})

describe('useChatRpcEventHandlers compaction ownership', () => {
  it('buffers compaction while task identity is pending and replays only for its owner', () => {
    const {
      api,
      activeStreamTaskId,
      lastStreamSeq,
      stream,
      showCompactionToast,
      stop,
    } = createHarness()
    try {
      activeStreamTaskId.value = PENDING_STREAM_TASK_ID
      api.handlers.onCompaction({
        session_key: 'agent:main:test',
        task_id: 'task-owned',
        stream_seq: 1,
        status: 'started',
        source: 'automatic',
        compaction_id: 'cmp-owned',
      }, {})

      expect(showCompactionToast).not.toHaveBeenCalled()
      expect(lastStreamSeq.value).toBe(0)

      api.bindActiveStreamTask('task-owned')

      expect(showCompactionToast).toHaveBeenCalledOnce()
      expect(stream.recordCompactionActivity).toHaveBeenCalledWith(expect.objectContaining({
        compaction_id: 'cmp-owned',
      }))
      expect(lastStreamSeq.value).toBe(1)
    } finally {
      stop()
    }
  })

  it('rejects a compaction tagged for another task before consuming its sequence', () => {
    const {
      api,
      activeStreamTaskId,
      lastStreamSeq,
      stream,
      showCompactionToast,
      stop,
    } = createHarness()
    try {
      activeStreamTaskId.value = 'task-current'
      api.handlers.onCompaction({
        session_key: 'agent:main:test',
        task_id: 'task-other',
        stream_seq: 7,
        status: 'completed',
        source: 'automatic',
        compaction_id: 'cmp-other',
      }, {})

      expect(showCompactionToast).not.toHaveBeenCalled()
      expect(stream.recordCompactionActivity).not.toHaveBeenCalled()
      expect(lastStreamSeq.value).toBe(0)
    } finally {
      stop()
    }
  })

  it('replays done before higher-sequence maintenance without losing the terminal', () => {
    const getCompactionPlacement = vi.fn((id: string) => (
      id === 'cmp-late' ? 'activity' as const : undefined
    ))
    const {
      api,
      activeStreamTaskId,
      lastStreamSeq,
      stream,
      showCompactionToast,
      stop,
    } = createHarness({ getCompactionPlacement })
    try {
      vi.mocked(stream.endStreaming).mockImplementation(() => {
        stream.isStreaming.value = false
      })
      activeStreamTaskId.value = PENDING_STREAM_TASK_ID
      api.handlers.onAny('session.event.done', {
        session_key: 'agent:main:test',
        task_id: 'task-race',
        stream_seq: 10,
        text: 'Finished before late maintenance.',
      })
      api.handlers.onCompaction({
        session_key: 'agent:main:test',
        task_id: 'task-race',
        stream_seq: 11,
        status: 'completed',
        source: 'automatic',
        compaction_id: 'cmp-late',
      }, {})

      api.bindActiveStreamTask('task-race')

      expect(stream.endStreaming).toHaveBeenCalledOnce()
      expect(activeStreamTaskId.value).toBe(FINISHED_STREAM_TASK_ID)
      expect(lastStreamSeq.value).toBe(10)
      expect(showCompactionToast).toHaveBeenCalledOnce()
      expect(showCompactionToast).toHaveBeenCalledWith(
        expect.objectContaining({ compaction_id: 'cmp-late' }),
        expect.objectContaining({ placement: 'activity' }),
      )
      expect(stream.recordCompactionActivity).not.toHaveBeenCalled()
    } finally {
      stop()
    }
  })

  it('lets a terminal own a stream sequence shared with an earlier visible frame', () => {
    const {
      api,
      activeStreamTaskId,
      lastStreamSeq,
      stream,
      showCompactionToast,
      stop,
    } = createHarness()
    try {
      activeStreamTaskId.value = PENDING_STREAM_TASK_ID
      api.handlers.onCompaction({
        session_key: 'agent:main:test',
        task_id: 'task-shared-seq',
        stream_seq: 10,
        status: 'started',
        source: 'automatic',
        compaction_id: 'cmp-shared-seq',
      }, {})
      api.handlers.onAny('session.event.done', {
        session_key: 'agent:main:test',
        task_id: 'task-shared-seq',
        stream_seq: 10,
        text: 'Done on the shared sequence.',
      })

      api.bindActiveStreamTask('task-shared-seq')

      expect(showCompactionToast).toHaveBeenCalledOnce()
      expect(stream.endStreaming).toHaveBeenCalledOnce()
      expect(activeStreamTaskId.value).toBe(FINISHED_STREAM_TASK_ID)
      expect(lastStreamSeq.value).toBe(10)
    } finally {
      stop()
    }
  })

  it('accepts only tracked terminal compaction after its task has finished', () => {
    const getCompactionPlacement = vi.fn((id: string) => (
      id === 'cmp-known' ? 'activity' as const : undefined
    ))
    const {
      api,
      activeStreamTaskId,
      lastStreamSeq,
      stream,
      showCompactionToast,
      stop,
    } = createHarness({ getCompactionPlacement })
    try {
      activeStreamTaskId.value = FINISHED_STREAM_TASK_ID
      stream.isStreaming.value = false

      api.handlers.onCompaction({
        session_key: 'agent:main:test',
        task_id: 'task-finished',
        stream_seq: 20,
        status: 'started',
        source: 'automatic',
        compaction_id: 'cmp-known',
      }, {})
      api.handlers.onCompaction({
        session_key: 'agent:main:test',
        task_id: 'task-finished',
        stream_seq: 21,
        status: 'failed',
        source: 'automatic',
        compaction_id: 'cmp-known',
      }, {})
      api.handlers.onCompaction({
        session_key: 'agent:main:test',
        task_id: 'task-finished',
        stream_seq: 22,
        status: 'failed',
        source: 'automatic',
        compaction_id: 'cmp-unknown',
      }, {})

      expect(showCompactionToast).toHaveBeenCalledOnce()
      expect(showCompactionToast).toHaveBeenCalledWith(
        expect.objectContaining({ compaction_id: 'cmp-known', status: 'failed' }),
        expect.objectContaining({ placement: 'activity' }),
      )
      expect(stream.recordCompactionActivity).not.toHaveBeenCalled()
      expect(stream.startStreaming).not.toHaveBeenCalled()
      expect(lastStreamSeq.value).toBe(21)
      expect(getCompactionPlacement).toHaveBeenCalledWith('cmp-known')
      expect(getCompactionPlacement).toHaveBeenCalledWith('cmp-unknown')
    } finally {
      stop()
    }
  })

  it.each([
    ['completed', 'completed'],
    ['emergency_ephemeral', 'completed'],
    ['skipped', 'skipped'],
    ['stale', 'cancelled'],
    ['cancelled', 'cancelled'],
    ['failed', 'failed'],
    ['error', 'failed'],
    ['timed_out', 'failed'],
  ] as const)(
    'settles the latest committed activity marker for a late %s terminal',
    (status, expectedState) => {
      const initialMessages: ChatMessage[] = [
        {
          role: 'assistant',
          text: 'Earlier turn',
          ts: '2026-08-04T00:00:00.000Z',
          statusHistory: [{
            action: 'context_compaction',
            label: '',
            at: 1_000,
            id: 'cmp-committed',
            category: 'maintenance',
            state: 'running',
          }],
        },
        {
          role: 'assistant',
          text: 'Most recent turn',
          ts: '2026-08-04T00:01:00.000Z',
          statusHistory: [{
            action: 'context_compaction',
            label: '',
            at: 2_000,
            id: 'cmp-committed',
            category: 'maintenance',
            state: 'running',
            detail: 'summarizing',
          }],
        },
      ]
      const {
        api,
        activeStreamTaskId,
        lastStreamSeq,
        messages,
        stream,
        stop,
      } = createHarness({
        messages: initialMessages,
        getCompactionPlacement: id => id === 'cmp-committed' ? 'activity' : undefined,
      })
      try {
        activeStreamTaskId.value = FINISHED_STREAM_TASK_ID
        stream.isStreaming.value = false

        api.handlers.onCompaction({
          session_key: 'agent:main:test',
          task_id: 'task-finished',
          stream_seq: 31,
          status,
          source: 'automatic',
          compaction_id: 'cmp-committed',
        }, { authoritativeLive: true })

        expect(messages.value).toHaveLength(2)
        expect(messages.value[0]?.statusHistory?.[0]).toMatchObject({
          at: 1_000,
          state: 'running',
        })
        expect(messages.value[1]?.statusHistory?.[0]).toMatchObject({
          at: 2_000,
          state: expectedState,
          detail: 'summarizing',
        })
        expect(stream.startStreaming).not.toHaveBeenCalled()
        expect(stream.recordCompactionActivity).not.toHaveBeenCalled()
        expect(lastStreamSeq.value).toBe(31)
      } finally {
        stop()
      }
    },
  )

  it('syncs history after an accepted identified manual completion only', () => {
    const {
      api,
      scheduleHistorySync,
      showCompactionToast,
      stop,
    } = createHarness()
    try {
      showCompactionToast
        .mockReturnValueOnce('standalone')
        .mockReturnValueOnce(false)
      const payload = {
        session_key: 'agent:main:test',
        status: 'completed',
        source: 'manual',
        compaction_id: 'cmp-manual',
      }
      api.handlers.onCompaction({ ...payload, stream_seq: 1 }, {})
      api.handlers.onCompaction({ ...payload, stream_seq: 2 }, {})

      expect(scheduleHistorySync).toHaveBeenCalledOnce()
    } finally {
      stop()
    }
  })
})

describe('useChatRpcEventHandlers durable out-of-band messages', () => {
  it('shows cron results immediately, preserves provenance, and deduplicates replay by id', () => {
    const { api, messages, scheduleHistorySync, applySessionRunState, stop } = createHarness()
    try {
      api.handlers.onCronResult({
        sessionKey: 'agent:other:test',
        stream_seq: 1,
        message: { text: 'foreign', messageId: 'cron-foreign' },
      })
      api.handlers.onCronResult({
        sessionKey: 'agent:main:test',
        epoch: -1,
        stream_seq: 1,
        message: { text: 'stale', messageId: 'cron-stale' },
      })
      const payload = {
        sessionKey: 'agent:main:test',
        stream_seq: 2,
        message: {
          role: 'assistant',
          text: 'scheduled result',
          timestamp: '2026-07-22T10:00:00Z',
          messageId: 'cron-message-1',
          provenanceKind: 'cron',
          provenanceSourceTool: 'cron.run',
        },
      }
      api.handlers.onCronResult(payload)
      api.handlers.onCronResult({ ...payload, stream_seq: 3 })

      expect(messages.value).toEqual([expect.objectContaining({
        role: 'assistant',
        text: 'scheduled result',
        messageId: 'cron-message-1',
        provenanceKind: 'cron',
        provenanceSourceTool: 'cron.run',
      })])
      expect(scheduleHistorySync).toHaveBeenCalledOnce()
      expect(applySessionRunState).not.toHaveBeenCalled()
    } finally {
      stop()
    }
  })

  it('shows subagent completion immediately and rejects foreign, stale, and replayed events', () => {
    const { api, messages, scheduleHistorySync, stop } = createHarness()
    try {
      api.handlers.onSubagentCompletion({
        session_key: 'agent:other:test',
        stream_seq: 1,
        type: 'subagent_completion',
        child_session_key: 'agent:main:subagent:foreign',
        message_id: 'foreign',
      })
      api.handlers.onSubagentCompletion({
        session_key: 'agent:main:test',
        epoch: -1,
        stream_seq: 1,
        type: 'subagent_completion',
        child_session_key: 'agent:main:subagent:stale',
        message_id: 'stale',
      })
      const current = {
        session_key: 'agent:main:test',
        stream_seq: 2,
        type: 'subagent_completion' as const,
        child_session_key: 'agent:main:subagent:child',
        status: 'succeeded',
        message_id: 'subagent-message-1',
        result: { text: 'done' },
      }
      api.handlers.onSubagentCompletion(current)
      api.handlers.onSubagentCompletion(current)

      expect(messages.value).toHaveLength(1)
      expect(messages.value[0]).toEqual(expect.objectContaining({
        role: 'system',
        messageId: 'subagent-message-1',
        provenanceKind: 'internal_system',
        provenanceSourceTool: 'subagent_completion',
        provenanceSourceSessionKey: 'agent:main:subagent:child',
      }))
      const displayed = JSON.parse(messages.value[0].text)
      expect(displayed).toEqual(expect.objectContaining({
        type: 'subagent_completion',
        result: { text: 'done' },
      }))
      expect(displayed).not.toHaveProperty('message_id')
      expect(scheduleHistorySync).toHaveBeenCalledOnce()
    } finally {
      stop()
    }
  })

  it('toasts warnings for five-second host handling while consuming silent warning sequences', () => {
    const { api, showWarningToast, messages, lastStreamSeq, stop } = createHarness()
    try {
      api.handlers.onWarning({
        session_key: 'agent:main:test',
        stream_seq: 1,
        code: 'provider_reasoning_only_retry',
        message: 'retrying',
      })
      api.handlers.onWarning({
        session_key: 'agent:main:test',
        stream_seq: 1,
        message: 'replayed warning',
      })
      api.handlers.onWarning({
        session_key: 'agent:main:test',
        stream_seq: 2,
        code: 'provider_request_message_limit_recovery_success',
        message: 'Older history was summarized for this provider request; retrying once.',
      })
      api.handlers.onWarning({
        session_key: 'agent:main:test',
        stream_seq: 2,
        message: 'replayed compaction warning',
      })
      api.handlers.onWarning({
        session_key: 'agent:main:test',
        stream_seq: 3,
        code: 'context_auto_compaction_start',
        message: 'Provider context limit reached; compacting older context before retrying.',
      })
      api.handlers.onWarning({
        session_key: 'agent:main:test',
        stream_seq: 3,
        message: 'replayed automatic compaction start warning',
      })
      api.handlers.onWarning({
        session_key: 'agent:main:test',
        stream_seq: 4,
        code: 'context_auto_compaction_retry',
        message: 'Stable context compacted; retrying the provider request.',
      })
      api.handlers.onWarning({
        session_key: 'agent:main:test',
        stream_seq: 4,
        message: 'replayed automatic compaction warning',
      })
      api.handlers.onWarning({
        session_key: 'agent:main:test',
        stream_seq: 5,
        message: 'Provider is degraded',
      })

      expect(showWarningToast).toHaveBeenCalledOnce()
      expect(showWarningToast).toHaveBeenCalledWith('Provider is degraded')
      expect(messages.value).toHaveLength(0)
      expect(lastStreamSeq.value).toBe(5)
    } finally {
      stop()
    }
  })
})

describe('useChatRpcEventHandlers steer disposition', () => {
  it('does not paint primary send lifecycle events as same-turn steer status', () => {
    const { api, messages, stop } = createHarness({
      messages: [{
        role: 'user',
        text: 'ordinary queued follow-up',
        ts: 'now',
        clientId: 'client-send',
        turnId: 'turn-send',
      }],
    })

    try {
      api.handlers.onInputDisposition({
        session_key: 'agent:main:test',
        stream_seq: 1,
        client_message_id: 'client-send',
        user_message_id: 'user-send',
        turn_id: 'turn-send',
        intent: 'send',
        disposition: 'applied',
        revision: 1,
      })

      expect(messages.value[0]).not.toHaveProperty('inputDisposition')
      expect(messages.value[0]).not.toHaveProperty('inputDispositionRevision')
    } finally {
      stop()
    }
  })

  it('moves a promoted adjustment to its explicit new turn and clears its retry lease', () => {
    const steer: ChatMessage = {
      role: 'user',
      text: 'use the new constraint',
      ts: 'now',
      turnId: 'turn-old',
      inputDisposition: 'steering',
      steerClientRequestId: 'request-1',
      steerClientMessageId: 'client-1',
    }
    const pending: ChatPendingItem = {
      text: steer.text,
      attachments: [],
      intent: null,
      deliveryState: 'retryable',
      steerClientRequestId: 'request-1',
      steerClientMessageId: 'client-1',
      steerExpectedTurnId: 'turn-old',
    }
    const { api, messages, pendingQueue, scheduleHistorySync, stop } = createHarness({
      messages: [
        {
          role: 'user',
          text: 'original request',
          ts: 'before',
          messageId: 'user-old',
          turnId: 'turn-old',
        },
        steer,
        {
          role: 'assistant',
          text: 'completed old-turn output',
          ts: 'after',
          messageId: 'assistant-old',
          turnId: 'turn-old',
        },
        {
          role: 'router',
          text: '',
          ts: 'new',
          messageId: 'router-new',
          turnId: 'turn-new',
        },
      ],
      pendingQueue: [pending],
    })

    try {
      api.handlers.onInputDisposition({
        session_key: 'agent:main:test',
        stream_seq: 1,
        client_request_id: 'request-1',
        client_message_id: 'client-1',
        user_message_id: 'user-1',
        turn_id: 'turn-old',
        promoted_turn_id: 'turn-new',
        promoted_from_turn_id: 'turn-old',
        disposition: 'promoted',
        revision: 2,
      })
      api.handlers.onInputDisposition({
        session_key: 'agent:main:test',
        stream_seq: 2,
        client_request_id: 'request-1',
        turn_id: 'turn-old',
        disposition: 'steering',
        revision: 1,
      })

      expect(messages.value.map(message => message.messageId)).toEqual([
        'user-old',
        'assistant-old',
        'user-1',
        'router-new',
      ])
      expect(messages.value[2]).toMatchObject({
        messageId: 'user-1',
        turnId: 'turn-new',
        promotedFromTurnId: 'turn-old',
        inputDisposition: 'promoted',
        inputDispositionRevision: 2,
      })
      expect(pendingQueue.value).toEqual([])
      expect(scheduleHistorySync).toHaveBeenCalledOnce()
    } finally {
      stop()
    }
  })

  it.each([
    {
      disposition: 'cancelled' as const,
      retryable: false,
      recovery: 'restore_to_composer',
    },
    {
      disposition: 'rejected' as const,
      retryable: true,
      recovery: 'resend_after_queue_drains',
    },
  ])('restores $disposition steer text once and leaves a muted durable row', ({
    disposition,
    retryable,
    recovery,
  }) => {
    const { api, messages, restoreSteerIntoComposer, stop } = createHarness({
      messages: [{
        role: 'user',
        text: 'preserve this adjustment',
        ts: 'now',
        turnId: 'turn-current',
        inputDisposition: 'steering',
        steerClientRequestId: 'request-restore',
      }],
    })

    try {
      api.handlers.onInputDisposition({
        session_key: 'agent:main:test',
        stream_seq: 1,
        client_request_id: 'request-restore',
        disposition,
        retryable,
        recovery,
        revision: 2,
      })
      api.handlers.onInputDisposition({
        session_key: 'agent:main:test',
        stream_seq: 2,
        client_request_id: 'request-restore',
        disposition,
        retryable,
        recovery,
        revision: 2,
      })

      expect(messages.value[0]).toMatchObject({
        inputDisposition: disposition,
        steerRestored: true,
      })
      expect(restoreSteerIntoComposer).toHaveBeenCalledOnce()
      expect(restoreSteerIntoComposer).toHaveBeenCalledWith('preserve this adjustment')
    } finally {
      stop()
    }
  })

  it('lets an authoritative applied revision win a local Stop race without restoring text', () => {
    const { api, messages, restoreSteerIntoComposer, stop } = createHarness({
      messages: [{
        role: 'user',
        text: 'already reached the model',
        ts: 'now',
        turnId: 'turn-current',
        inputDisposition: 'steering',
        steerStopRequested: true,
        steerClientRequestId: 'request-applied',
      }],
    })

    try {
      api.handlers.onInputDisposition({
        session_key: 'agent:main:test',
        stream_seq: 1,
        client_request_id: 'request-applied',
        disposition: 'applied',
        revision: 2,
        applied_iteration: 2,
        model_call_id: '2.0',
      })

      expect(messages.value[0]).toMatchObject({
        inputDisposition: 'applied',
        inputDispositionRevision: 2,
        steerStopRequested: false,
      })
      expect(restoreSteerIntoComposer).not.toHaveBeenCalled()
    } finally {
      stop()
    }
  })

  it('restores multiple authoritatively cancelled steers in event FIFO order', () => {
    const { api, restoreSteerIntoComposer, stop } = createHarness({
      messages: [
        {
          role: 'user',
          text: 'first adjustment',
          ts: 1,
          turnId: 'turn-current',
          inputDisposition: 'steering',
          steerStopRequested: true,
          steerClientRequestId: 'request-first',
        },
        {
          role: 'user',
          text: 'second adjustment',
          ts: 2,
          turnId: 'turn-current',
          inputDisposition: 'steering',
          steerStopRequested: true,
          steerClientRequestId: 'request-second',
        },
      ],
    })

    try {
      for (const [streamSeq, clientRequestId] of [
        [1, 'request-first'],
        [2, 'request-second'],
      ] as const) {
        api.handlers.onInputDisposition({
          session_key: 'agent:main:test',
          stream_seq: streamSeq,
          client_request_id: clientRequestId,
          disposition: 'cancelled',
          revision: 2,
          recovery: 'restore_to_composer',
        })
      }

      expect(restoreSteerIntoComposer.mock.calls).toEqual([
        ['first adjustment'],
        ['second adjustment'],
      ])
    } finally {
      stop()
    }
  })
})

describe('useChatRpcEventHandlers task group lifecycle', () => {
  it('keeps an active child group when the yielding parent task ends normally', () => {
    const { api, activeTaskGroups, applySessionRunState, stop } = createHarness()

    try {
      api.handlers.onTaskGroupWaiting({
        session_key: 'agent:main:test',
        stream_seq: 1,
        group_id: 'group-live',
      })
      api.handlers.onSessionsChanged({
        session_key: 'agent:main:test',
        reason: 'task_terminal',
        run_status: 'idle',
        last_task: { status: 'succeeded' },
      })

      expect([...activeTaskGroups.value]).toEqual(['group-live'])
      expect(applySessionRunState).toHaveBeenLastCalledWith(expect.objectContaining({
        run_status: 'running',
      }))
    } finally {
      stop()
    }
  })

  it('clears active child groups when the parent session is explicitly cancelled', () => {
    const { api, activeTaskGroups, stream, stop } = createHarness({
      sessionRunStatus: source => ({
        status: source?.run_status === 'cancelled' ? 'cancelled' : 'idle',
        label: '',
        task: null,
      }),
    })

    try {
      api.handlers.onTaskGroupWaiting({
        session_key: 'agent:main:test',
        stream_seq: 1,
        group_id: 'group-live',
      })
      api.handlers.onSessionsChanged({
        session_key: 'agent:main:test',
        reason: 'task_terminal',
        run_status: 'cancelled',
        last_task: { status: 'cancelled' },
      })

      expect(activeTaskGroups.value.size).toBe(0)
      expect(stream.endStreaming).toHaveBeenCalled()
    } finally {
      stop()
    }
  })

  it('releases pending work when the last background-only task group finishes', () => {
    const {
      api,
      activeTaskGroups,
      stream,
      schedulePendingDrainAfterTerminal,
      stop,
    } = createHarness()
    stream.isStreaming.value = false

    try {
      api.handlers.onTaskGroupWaiting({
        session_key: 'agent:main:test',
        stream_seq: 1,
        group_id: 'group-live',
      })
      api.handlers.onTaskGroupDone({
        session_key: 'agent:main:test',
        stream_seq: 2,
        group_id: 'group-live',
      })

      expect(activeTaskGroups.value.size).toBe(0)
      expect(schedulePendingDrainAfterTerminal).toHaveBeenCalledOnce()
    } finally {
      stop()
    }
  })
})

describe('useChatRpcEventHandlers done usage attachment', () => {
  it('distinguishes authoritative snapshots from legacy text fallback', () => {
    const { api, stream, stop } = createHarness()

    try {
      api.handlers.onAny('session.event.done', {
        session_key: 'agent:main:test',
        stream_seq: 1,
        text: 'legacy canonical',
      })
      expect(stream.reconcileFinalText).toHaveBeenLastCalledWith('legacy canonical')

      api.handlers.onAny('session.event.done', {
        session_key: 'agent:main:test',
        stream_seq: 2,
        text: 'legacy canonical with serialized null',
        text_snapshot: null,
      })
      expect(stream.reconcileFinalText).toHaveBeenLastCalledWith('legacy canonical with serialized null')

      api.handlers.onAny('session.event.done', {
        session_key: 'agent:main:test',
        stream_seq: 3,
        text: 'stale legacy aggregate',
        text_snapshot: '',
      })
      expect(stream.reconcileFinalText).toHaveBeenLastCalledWith('')

      api.handlers.onAny('session.event.done', {
        session_key: 'agent:main:test',
        stream_seq: 4,
        text: '',
      })
      expect(stream.reconcileFinalText).toHaveBeenLastCalledWith(null)

      api.handlers.onAny('session.event.done', {
        session_key: 'agent:main:test',
        stream_seq: 5,
        text_snapshot: 'outer canonical',
        usage: { text_snapshot: null },
      })
      expect(stream.reconcileFinalText).toHaveBeenLastCalledWith('outer canonical')

      api.handlers.onAny('session.event.done', {
        session_key: 'agent:main:test',
        stream_seq: 6,
        text: 'outer legacy canonical',
        usage: { text: '' },
      })
      expect(stream.reconcileFinalText).toHaveBeenLastCalledWith('outer legacy canonical')
    } finally {
      stop()
    }
  })

  it('does not attach done usage to the previous assistant when no new bubble was pushed', () => {
    const previous: ChatMessage = { role: 'assistant', text: 'previous', ts: 'before' }
    const { api, messages, stop } = createHarness({ messages: [previous] })

    try {
      api.handlers.onAny('session.event.done', {
        session_key: 'agent:main:test',
        stream_seq: 1,
        text: 'NO_REPLY',
        input_tokens: 10,
        output_tokens: 1,
        model: 'ensemble/default',
        model_usage_breakdown: [{ model: 'z-ai/glm-5.2', role: 'aggregator' }],
        ensemble_trace: { profile: 'default', llm_request_count: 5 },
      })

      expect(messages.value).toHaveLength(1)
      expect(messages.value[0]).toEqual(previous)
      expect(messages.value[0].usage).toBeUndefined()
    } finally {
      stop()
    }
  })

  it('attaches done usage to the assistant message pushed by endStreaming', () => {
    const previous: ChatMessage = { role: 'assistant', text: 'previous', ts: 'before' }
    const { api, messages, stop } = createHarness({
      messages: [previous],
      endStreaming(list) {
        list.push({ role: 'assistant', text: 'current', ts: 'now' })
      },
    })

    try {
      api.handlers.onAny('session.event.done', {
        session_key: 'agent:main:test',
        stream_seq: 1,
        text: 'current',
        input_tokens: 10,
        output_tokens: 1,
        model: 'z-ai/glm-5.2',
        model_usage_breakdown: [{ model: 'z-ai/glm-5.2', role: 'aggregator' }],
        ensemble_trace: { profile: 'default', llm_request_count: 5 },
      })

      expect(messages.value[0].usage).toBeUndefined()
      expect(messages.value[1].usage?.ensemble_trace).toEqual({
        profile: 'default',
        llm_request_count: 5,
      })
      expect(messages.value[1].model).toBe('z-ai/glm-5.2')
      expect(messages.value[1].input_tokens).toBe(10)
      expect(messages.value[1].output_tokens).toBe(1)
    } finally {
      stop()
    }
  })
})

describe('useChatRpcEventHandlers reasoning timer replay', () => {
  it('keeps elapsed time across A to B to A replay without leaking into B', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(105_000)
    const { api, sessionKey, lastStreamSeq, stop } = createHarness()

    try {
      api.handlers.onAny('session.event.thinking', {
        session_key: 'agent:main:test',
        stream_seq: 1,
        text: 'first',
        started_at: 100_000,
      })
      expect(api.streamThinkingElapsedText.value).toBe('5s')

      vi.setSystemTime(108_000)
      sessionKey.value = 'agent:main:other'
      lastStreamSeq.value = 0
      await nextTick()
      expect(api.streamThinkingText.value).toBe('')

      sessionKey.value = 'agent:main:test'
      lastStreamSeq.value = 0
      await nextTick()
      api.handlers.onAny('session.event.thinking', {
        session_key: 'agent:main:test',
        stream_seq: 1,
        text: 'first',
        started_at: 100_000,
      })
      expect(api.streamThinkingElapsedText.value).toBe('8s')

      vi.setSystemTime(110_000)
      api.handlers.onAny('session.event.thinking', {
        session_key: 'agent:main:test',
        stream_seq: 2,
        text: ' second',
        started_at: 109_000,
      })
      expect(api.streamThinkingElapsedText.value).toBe('10s')
    } finally {
      stop()
      vi.useRealTimers()
    }
  })

  it('stops replayed reasoning at the original done emission time', () => {
    vi.useFakeTimers()
    vi.setSystemTime(120_000)
    const { api, messages, stop } = createHarness({
      endStreaming(list) {
        list.push({ role: 'assistant', text: 'answer', ts: 'now' })
      },
    })

    try {
      api.handlers.onAny('session.event.thinking', {
        session_key: 'agent:main:test',
        stream_seq: 1,
        text: 'reasoning',
        started_at: 100_000,
      })
      api.handlers.onAny('session.event.done', {
        session_key: 'agent:main:test',
        stream_seq: 2,
        text: 'answer',
        reasoning_content: 'reasoning',
        emitted_at: 108_000,
      })

      expect(messages.value[0].reasoning).toEqual({
        text: 'reasoning',
        seconds: 8,
      })
    } finally {
      stop()
      vi.useRealTimers()
    }
  })

  it('falls back to the local clock for legacy, skewed, and invalid start times', () => {
    vi.useFakeTimers()
    vi.setSystemTime(5_000_000)

    try {
      for (const startedAt of [
        undefined,
        5_006_000,
        5_000_000 - 60 * 60 * 1_000 - 1,
        Number.NaN,
      ]) {
        const { api, stop } = createHarness()
        try {
          api.handlers.onAny('session.event.thinking', {
            session_key: 'agent:main:test',
            stream_seq: 1,
            text: 'reasoning',
            started_at: startedAt,
          })
          expect(api.streamThinkingElapsedText.value).toBe('0s')
        } finally {
          stop()
        }
      }
    } finally {
      vi.useRealTimers()
    }
  })

  it('falls back to local completion time when emitted_at precedes the start', () => {
    vi.useFakeTimers()
    vi.setSystemTime(108_000)
    const { api, messages, stop } = createHarness({
      endStreaming(list) {
        list.push({ role: 'assistant', text: 'answer', ts: 'now' })
      },
    })

    try {
      api.handlers.onAny('session.event.thinking', {
        session_key: 'agent:main:test',
        stream_seq: 1,
        text: 'reasoning',
        started_at: 100_000,
      })
      api.handlers.onAny('session.event.done', {
        session_key: 'agent:main:test',
        stream_seq: 2,
        text: 'answer',
        reasoning_content: 'reasoning',
        emitted_at: 99_000,
      })

      expect(messages.value[0].reasoning?.seconds).toBe(8)
    } finally {
      stop()
      vi.useRealTimers()
    }
  })
})

describe('useChatRpcEventHandlers ensemble handoff', () => {
  it('marks ensemble handoff when a current tool call starts', () => {
    const { api, stream, markEnsembleHandoff, stop } = createHarness()

    try {
      api.handlers.onToolUseStart({
        session_key: 'agent:main:test',
        stream_seq: 1,
        tool_use_id: 'tool-1',
        tool_name: 'write_file',
      })

      expect(stream.appendToolCall).toHaveBeenCalledTimes(1)
      expect(markEnsembleHandoff).toHaveBeenCalledTimes(1)
    } finally {
      stop()
    }
  })

  it('does not mark handoff for stale tool events', () => {
    const { api, stream, markEnsembleHandoff, stop } = createHarness()

    try {
      api.handlers.onToolUseStart({
        session_key: 'agent:main:test',
        stream_seq: -1,
        tool_use_id: 'tool-1',
        tool_name: 'write_file',
      })

      expect(stream.appendToolCall).not.toHaveBeenCalled()
      expect(markEnsembleHandoff).not.toHaveBeenCalled()
    } finally {
      stop()
    }
  })
})

describe('useChatRpcEventHandlers ensemble activity', () => {
  it('removes the transient connection-loss row after reconnect', () => {
    const { api, messages, stop } = createHarness()

    try {
      api.handlers.onConnectionState('disconnected')
      expect(messages.value).toEqual([
        expect.objectContaining({
          role: 'system',
          text: 'Connection lost — trying to reconnect…',
        }),
      ])

      api.handlers.onConnectionState('connected')
      expect(messages.value).toEqual([])
    } finally {
      stop()
    }
  })

  it('does not duplicate the transient row while disconnected', () => {
    const { api, messages, stop } = createHarness()

    try {
      api.handlers.onConnectionState('disconnected')
      api.handlers.onConnectionState('disconnected')
      expect(messages.value).toHaveLength(1)
    } finally {
      stop()
    }
  })

  it('treats ensemble progress as a hard-idle liveness event', () => {
    const { api, stream, stop } = createHarness()

    try {
      stream.isStreaming.value = false
      api.handlers.onEnsembleProgress({
        stream_seq: 1,
        event_type: 'proposer_start',
        proposer_label: 'anchor',
        proposer_model: 'qwen/qwen3.7-plus',
      })
      expect(stream.startStreaming).toHaveBeenCalledTimes(1)
      expect(stream.resetStreamIdleTimer).toHaveBeenCalledTimes(1)
    } finally {
      stop()
    }
  })

  it('maps ensemble heartbeats without letting channel keepalives replace the phase', () => {
    const { api, stream, stop } = createHarness()

    try {
      api.handlers.onRunHeartbeat({ stream_seq: 1, phase: 'ensemble_proposers_wait' })
      expect(stream.setStreamActivity).toHaveBeenLastCalledWith('Generating candidates')

      api.handlers.onRunHeartbeat({ stream_seq: 2, phase: 'channel' })
      expect(stream.setStreamActivity).toHaveBeenCalledTimes(1)

      api.handlers.onRunHeartbeat({ stream_seq: 3, phase: 'ensemble_aggregator_stream' })
      expect(stream.setStreamActivity).toHaveBeenLastCalledWith('Synthesizing candidates')

      api.handlers.onRunHeartbeat({ stream_seq: 4, phase: 'provider_wait' })
      expect(stream.setStreamActivity).toHaveBeenLastCalledWith('Planning next step')
    } finally {
      stop()
    }
  })

  it('restarts the hard idle timer after reconnect while a turn is streaming', () => {
    const { api, stream, stop } = createHarness()

    try {
      vi.mocked(stream.resetStreamIdleTimer).mockClear()
      api.handlers.onConnectionState('connected')
      expect(stream.resetStreamIdleTimer).toHaveBeenCalledTimes(1)
    } finally {
      stop()
    }
  })

  it('refreshes reconnect metadata once critical requests are queued', async () => {
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
    const run: SessionBootstrapRun = {
      generation: 2,
      criticalRequestsQueued,
      history,
      live,
    }
    const harness = createHarness({
      handleSessionConnectionState: () => run,
    })

    try {
      harness.api.handlers.onConnectionState('connected')
      await Promise.resolve()
      expect(harness.loadCurrentSessionUsage).not.toHaveBeenCalled()
      expect(harness.refreshRunModePreference).not.toHaveBeenCalled()

      resolveCriticalRequestsQueued()
      await vi.waitFor(() => {
        expect(harness.loadCurrentSessionUsage).toHaveBeenCalledOnce()
        expect(harness.refreshRunModePreference).toHaveBeenCalledOnce()
      })

      resolveLive()
      resolveHistory()
      await Promise.all([live, history])
    } finally {
      harness.stop()
    }
  })
})
