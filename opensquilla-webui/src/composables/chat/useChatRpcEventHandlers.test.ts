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
  subscribeSession?: () =>
    | boolean
    | void
    | { authoritative: boolean, live: boolean, backgroundOnly: boolean }
    | Promise<boolean | void | { authoritative: boolean, live: boolean, backgroundOnly: boolean }>
  onSessionSubscribed?: () => void | Promise<void>
  handleSessionConnectionState?: (state: string) => SessionBootstrapRun | undefined
  loadCurrentSessionUsage?: () => void
  refreshRunModePreference?: () => void | Promise<void>
  pendingQueue?: ChatPendingItem[]
  stream?: ChatRpcStreamApi
  restoreSteerIntoComposer?: (text: string) => void
  getCompactionPlacement?: (compactionId: string) => 'activity' | 'standalone' | undefined
  observeStreamGeneration?: (payload: unknown) => boolean
  supportsTurnCommitted?: boolean
} = {}) {
  const messages = ref<ChatMessage[]>(options.messages ?? [])
  const sessionKey = ref('agent:main:test')
  const lastStreamSeq = ref(0)
  const activeTaskGroups = ref(new Set<string>())
  const activeStreamTaskId = ref('')
  const pendingQueue = ref<ChatPendingItem[]>(options.pendingQueue ?? [])
  const applySessionRunState = vi.fn()
  const stream: ChatRpcStreamApi = options.stream || {
    isStreaming: ref(true),
    streamBubble: ref(true),
    streamHasVisibleOutput: ref(false),
    startStreaming: vi.fn(),
    endStreaming: vi.fn(() => options.endStreaming?.(messages.value)),
    checkpointForUserMessage: vi.fn(),
    acknowledgeSteerBoundary: vi.fn(),
    appendDelta: vi.fn(),
    scheduleRender: vi.fn(),
    appendToolCall: vi.fn(),
    appendToolDelta: vi.fn(),
    appendToolEnd: vi.fn(),
    appendToolResult: vi.fn(),
    appendArtifact: vi.fn(),
    reconcileFinalText: vi.fn(),
    resetLiveTurnState: vi.fn(),
    resetAnswerGeneration: vi.fn(),
    setAssistantMessageId: vi.fn(),
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
  const bindRouterDecisionToModelCall = vi.fn()
  const schedulePendingDrainAfterTerminal = vi.fn()
  const scheduleHistorySync = vi.fn()
  const showCompactionToast = vi.fn()
  const showWarningToast = vi.fn()
  const subscribeSession = vi.fn(options.subscribeSession || (() => undefined))
  const onSessionSubscribed = vi.fn(options.onSessionSubscribed || (() => undefined))
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
    observeStreamGeneration: options.observeStreamGeneration,
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
    bindRouterDecisionToModelCall,
    appendEnsembleProgress: vi.fn(),
    markEnsembleHandoff,
    flushPendingRouterDecision: vi.fn(),
    clearPendingRouterDecision: vi.fn(),
    handleRouterControlReplay: vi.fn(),
    showCompactionToast,
    getCompactionPlacement: options.getCompactionPlacement,
    showWarningToast,
    supportsTurnCommitted: () => options.supportsTurnCommitted === true,
    scheduleHistorySync,
    schedulePendingDrainAfterTerminal,
    popAllPendingIntoComposer: vi.fn(() => false),
    restoreSteerIntoComposer,
    saveWidgetState: vi.fn(),
    subscribeSession,
    onSessionSubscribed,
    loadHistory: vi.fn(),
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
    bindRouterDecisionToModelCall,
    schedulePendingDrainAfterTerminal,
    scheduleHistorySync,
    showCompactionToast,
    showWarningToast,
    subscribeSession,
    onSessionSubscribed,
    handleSessionConnectionState,
    loadCurrentSessionUsage,
    refreshRunModePreference,
    restoreSteerIntoComposer,
    stop: () => scope.stop(),
  }
}

describe('useChatRpcEventHandlers route-card ownership', () => {
  it('binds text and thinking events to their physical provider calls', () => {
    const { api, bindRouterDecisionToModelCall, stop } = createHarness()
    try {
      api.handlers.onTextDelta({
        session_key: 'agent:main:test',
        turn_id: 'turn-1',
        stream_seq: 1,
        generation_epoch: 0,
        text: 'answer',
        model_call_id: '1.0',
        iteration: 1,
      })
      api.handlers.onAnswerGenerationReset({
        session_key: 'agent:main:test',
        turn_id: 'turn-1',
        stream_seq: 2,
        old_generation_epoch: 0,
        new_generation_epoch: 1,
      })
      api.handlers.onTextDelta({
        session_key: 'agent:main:test',
        turn_id: 'turn-1',
        stream_seq: 3,
        generation_epoch: 0,
        text: 'stale answer',
        model_call_id: 'stale-call',
        iteration: 99,
      })
      api.handlers.onAny('session.event.thinking', {
        session_key: 'agent:main:test',
        turn_id: 'turn-1',
        stream_seq: 4,
        generation_epoch: 1,
        text: 'reasoning',
        model_call_id: '2.0',
        iteration: 2,
      })

      expect(bindRouterDecisionToModelCall.mock.calls).toEqual([
        ['1.0', 1, 'turn-1'],
        ['2.0', 2, 'turn-1'],
      ])
    } finally {
      stop()
    }
  })
})

describe('useChatRpcEventHandlers live snapshot restoration', () => {
  it('replays a committed tool timeline including its authoritative end', () => {
    const { api, stream, stop } = createHarness()
    try {
      api.restoreLiveTurnSnapshot({
        key: 'agent:main:test',
        task_id: 'task-tool-timeline',
        current_stream_seq: 3,
        events: [
          {
            event: 'session.event.tool_use_start',
            payload: {
              session_key: 'agent:main:test',
              task_id: 'task-tool-timeline',
              generation_epoch: 0,
              tool_use_id: 'tool-1',
              tool_name: 'lookup',
              stream_seq: 1,
            },
          },
          {
            event: 'session.event.tool_use_delta',
            payload: {
              session_key: 'agent:main:test',
              task_id: 'task-tool-timeline',
              generation_epoch: 0,
              tool_use_id: 'tool-1',
              json_fragment: '{"query":"answer"}',
              stream_seq: 2,
            },
          },
          {
            event: 'session.event.tool_use_end',
            payload: {
              session_key: 'agent:main:test',
              task_id: 'task-tool-timeline',
              generation_epoch: 0,
              tool_use_id: 'tool-1',
              tool_name: 'lookup',
              arguments: { query: 'answer' },
              stream_seq: 3,
            },
          },
        ],
      })

      expect(stream.appendToolCall).toHaveBeenCalledTimes(1)
      expect(stream.appendToolDelta).toHaveBeenCalledTimes(1)
      expect(stream.appendToolEnd).toHaveBeenCalledWith(expect.objectContaining({
        tool_use_id: 'tool-1',
        arguments: { query: 'answer' },
      }))
    } finally {
      stop()
    }
  })

  it('replays a generation reset and drops late old-generation text', () => {
    const {
      api,
      stream,
      activeStreamTaskId,
      stop,
    } = createHarness()
    try {
      api.restoreLiveTurnSnapshot({
        key: 'agent:main:test',
        task_id: 'task-live',
        current_stream_seq: 5,
        events: [
          {
            event: 'session.event.text_delta',
            payload: {
              session_key: 'agent:main:test',
              task_id: 'task-live',
              generation_epoch: 0,
              text: 'partial old',
              stream_seq: 1,
            },
          },
          {
            event: 'session.event.answer_generation_reset',
            payload: {
              session_key: 'agent:main:test',
              task_id: 'task-live',
              assistant_message_id: 'assistant-1',
              old_generation_epoch: 0,
              new_generation_epoch: 1,
              authoritative_text_snapshot: '',
              authoritative_reasoning_snapshot: '',
              preserve_completed_tools: true,
              stream_seq: 2,
            },
          },
          {
            event: 'session.event.text_delta',
            payload: {
              session_key: 'agent:main:test',
              task_id: 'task-live',
              generation_epoch: 0,
              text: 'late old',
              stream_seq: 3,
            },
          },
          {
            event: 'session.event.text_delta',
            payload: {
              session_key: 'agent:main:test',
              task_id: 'task-live',
              generation_epoch: 1,
              text: 'fixed',
              stream_seq: 4,
            },
          },
        ],
      })

      expect(activeStreamTaskId.value).toBe('task-live')
      expect(stream.resetAnswerGeneration).toHaveBeenCalledWith({
        textSnapshot: '',
        preserveCompletedTools: true,
      })
      expect(stream.appendDelta).toHaveBeenCalledTimes(2)
      expect(stream.appendDelta).toHaveBeenNthCalledWith(1, 'partial old')
      expect(stream.appendDelta).toHaveBeenNthCalledWith(2, 'fixed')
      expect(stream.setAssistantMessageId).toHaveBeenCalledWith('assistant-1')
    } finally {
      stop()
    }
  })

  it('rejects old-generation delta and done after an in-flight reset', () => {
    const {
      api,
      stream,
      activeStreamTaskId,
      stop,
    } = createHarness()
    try {
      activeStreamTaskId.value = 'task-live'
      api.handlers.onTextDelta({
        session_key: 'agent:main:test',
        task_id: 'task-live',
        generation_epoch: 0,
        text: 'partial old',
        stream_seq: 1,
      })
      api.handlers.onAnswerGenerationReset({
        session_key: 'agent:main:test',
        task_id: 'task-live',
        assistant_message_id: 'assistant-1',
        old_generation_epoch: 0,
        new_generation_epoch: 1,
        authoritative_text_snapshot: '',
        authoritative_reasoning_snapshot: '',
        preserve_completed_tools: true,
        stream_seq: 2,
      })

      api.handlers.onTextDelta({
        session_key: 'agent:main:test',
        task_id: 'task-live',
        generation_epoch: 0,
        text: 'late old',
        stream_seq: 3,
      })
      api.handlers.onAny('session.event.done', {
        session_key: 'agent:main:test',
        task_id: 'task-live',
        generation_epoch: 0,
        stream_seq: 4,
        text_snapshot: 'old final',
      })

      expect(stream.appendDelta).toHaveBeenCalledTimes(1)
      expect(stream.reconcileFinalText).not.toHaveBeenCalled()
      expect(stream.endStreaming).not.toHaveBeenCalled()

      api.handlers.onTextDelta({
        session_key: 'agent:main:test',
        task_id: 'task-live',
        generation_epoch: 1,
        text: 'fixed',
        stream_seq: 5,
      })
      api.handlers.onAny('session.event.done', {
        session_key: 'agent:main:test',
        task_id: 'task-live',
        generation_epoch: 1,
        stream_seq: 6,
        text_snapshot: 'fixed',
      })

      expect(stream.appendDelta).toHaveBeenCalledTimes(2)
      expect(stream.endStreaming).toHaveBeenCalledOnce()
    } finally {
      stop()
    }
  })

  it('finishes a terminal reset in the same assistant message and suppresses a late error', () => {
    const {
      api,
      stream,
      activeStreamTaskId,
      messages,
      stop,
    } = createHarness()
    try {
      activeStreamTaskId.value = 'task-live'
      api.handlers.onAnswerGenerationReset({
        session_key: 'agent:main:test',
        task_id: 'task-live',
        assistant_message_id: 'assistant-1',
        old_generation_epoch: 0,
        new_generation_epoch: 1,
        authoritative_text_snapshot: '',
        authoritative_reasoning_snapshot: '',
        terminal: true,
        terminal_text_snapshot: 'The fixed model could not complete this answer.',
        stream_seq: 1,
      })

      expect(stream.reconcileFinalText).toHaveBeenCalledWith(
        'The fixed model could not complete this answer.',
      )
      expect(stream.endStreaming).toHaveBeenCalledOnce()
      expect(stream.setAssistantMessageId).toHaveBeenCalledWith('assistant-1')
      expect(activeStreamTaskId.value).toBe(FINISHED_STREAM_TASK_ID)

      api.handlers.onAny('session.event.error', {
        session_key: 'agent:main:test',
        generation_epoch: 1,
        stream_seq: 2,
        code: 'fixed_model_failed',
        message: 'late duplicate error',
      })

      expect(messages.value.some(message => message.role === 'error')).toBe(false)
    } finally {
      stop()
    }
  })

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

  it('rebuilds an applied steer boundary in snapshot stream order', () => {
    const { api, stream, stop } = createHarness({
      messages: [{
        role: 'user',
        text: 'Use English',
        ts: 2,
        messageId: 'steer-message-1',
        turnId: 'turn-live',
        inputDisposition: 'steering',
      }],
    })
    try {
      api.restoreLiveTurnSnapshot({
        key: 'agent:main:test',
        task_id: 'task-live',
        current_stream_seq: 12,
        events: [
          {
            event: 'session.event.text_delta',
            payload: {
              session_key: 'agent:main:test',
              task_id: 'task-live',
              text: 'Second answer',
              stream_seq: 12,
            },
          },
          {
            event: 'session.event.input_disposition',
            payload: {
              session_key: 'agent:main:test',
              task_id: 'task-live',
              turn_id: 'task-live',
              user_message_id: 'steer-message-1',
              intent: 'steer',
              disposition: 'applied',
              stream_seq: 11,
            },
          },
          {
            event: 'session.event.text_delta',
            payload: {
              session_key: 'agent:main:test',
              task_id: 'task-live',
              text: 'First answer',
              stream_seq: 10,
            },
          },
        ],
      })

      expect(stream.checkpointForUserMessage).toHaveBeenCalledWith(
        'task-live',
        'steer-message-1',
      )
      expect(stream.acknowledgeSteerBoundary).toHaveBeenCalledWith(
        'steer-message-1',
        '',
        0,
      )
      expect(vi.mocked(stream.checkpointForUserMessage!).mock.invocationCallOrder[0])
        .toBeLessThan(
          vi.mocked(stream.acknowledgeSteerBoundary!).mock.invocationCallOrder[0]!,
        )
      expect(vi.mocked(stream.appendDelta).mock.calls.map(call => call[0])).toEqual([
        'First answer',
        'Second answer',
      ])
    } finally {
      stop()
    }
  })

  it('checkpoints an applied snapshot boundary before its history row exists', () => {
    const { api, messages, stream, stop } = createHarness()
    try {
      api.restoreLiveTurnSnapshot({
        key: 'agent:main:test',
        task_id: 'task-live',
        current_stream_seq: 12,
        events: [
          {
            event: 'session.event.text_delta',
            payload: {
              session_key: 'agent:main:test',
              task_id: 'task-live',
              text: 'First answer',
              stream_seq: 10,
            },
          },
          {
            event: 'session.event.input_disposition',
            payload: {
              session_key: 'agent:main:test',
              task_id: 'task-live',
              turn_id: 'task-live',
              user_message_id: 'steer-message-orphan',
              intent: 'steer',
              disposition: 'applied',
              stream_seq: 11,
            },
          },
          {
            event: 'session.event.text_delta',
            payload: {
              session_key: 'agent:main:test',
              task_id: 'task-live',
              text: 'Second answer',
              stream_seq: 12,
            },
          },
        ],
      })

      expect(messages.value).toEqual([])
      expect(stream.checkpointForUserMessage).toHaveBeenCalledWith(
        'task-live',
        'steer-message-orphan',
      )
      expect(stream.acknowledgeSteerBoundary).toHaveBeenCalledWith(
        'steer-message-orphan',
        '',
        0,
      )
      const textOrders = vi.mocked(stream.appendDelta).mock.invocationCallOrder
      const checkpointOrders = vi.mocked(stream.checkpointForUserMessage!).mock.invocationCallOrder
      const acknowledgeOrders = vi.mocked(stream.acknowledgeSteerBoundary!).mock.invocationCallOrder
      expect(textOrders[0]).toBeLessThan(checkpointOrders[0]!)
      expect(checkpointOrders[0]).toBeLessThan(acknowledgeOrders[0]!)
      expect(acknowledgeOrders[0]).toBeLessThan(textOrders[1]!)

      messages.value = [{
        role: 'user',
        text: 'Use English',
        ts: 2,
        messageId: 'steer-message-orphan',
        turnId: 'task-live',
        inputDisposition: 'applied',
      }]
      expect(stream.checkpointForUserMessage).toHaveBeenCalledTimes(2)
      expect(stream.acknowledgeSteerBoundary).toHaveBeenCalledTimes(2)
    } finally {
      stop()
    }
  })

  it('rebuilds the boundary when a legacy applied snapshot omits revision and call id', () => {
    const { api, messages, stream, stop } = createHarness({
      messages: [{
        role: 'user',
        text: 'Use English',
        ts: 2,
        messageId: 'steer-message-1',
        turnId: 'turn-live',
        inputDisposition: 'applied',
        inputDispositionRevision: 3,
      }],
    })
    try {
      api.restoreLiveTurnSnapshot({
        key: 'agent:main:test',
        task_id: 'task-live',
        current_stream_seq: 2,
        events: [
          {
            event: 'session.event.text_delta',
            payload: {
              session_key: 'agent:main:test',
              task_id: 'task-live',
              text: 'First answer',
              stream_seq: 1,
            },
          },
          {
            event: 'session.event.input_disposition',
            payload: {
              session_key: 'agent:main:test',
              task_id: 'task-live',
              turn_id: 'turn-live',
              user_message_id: 'steer-message-1',
              intent: 'steer',
              disposition: 'applied',
              stream_seq: 2,
            },
          },
        ],
      })

      expect(stream.checkpointForUserMessage).toHaveBeenCalledWith(
        'turn-live',
        'steer-message-1',
      )
      expect(stream.acknowledgeSteerBoundary).toHaveBeenCalledWith(
        'steer-message-1',
        '',
        0,
      )
      expect(messages.value[0]?.inputDispositionRevision).toBe(3)
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

describe('useChatRpcEventHandlers stream generation', () => {
  it('observes a restarted generation before rejecting its lower sequence', () => {
    let lastStreamSeqRef = ref(500)
    const observeStreamGeneration = vi.fn((payload: unknown) => {
      if ((payload as { stream_generation?: string }).stream_generation === 'new-generation') {
        lastStreamSeqRef.value = 0
        return true
      }
      return false
    })
    const harness = createHarness({ observeStreamGeneration })
    lastStreamSeqRef = harness.lastStreamSeq
    harness.lastStreamSeq.value = 500
    try {
      harness.api.handlers.onTextDelta({
        session_key: 'agent:main:test',
        task_id: 'task-new',
        stream_generation: 'new-generation',
        stream_seq: 1,
        text: 'first token after restart',
      })

      expect(observeStreamGeneration).toHaveBeenCalledOnce()
      expect(harness.stream.appendDelta).toHaveBeenCalledWith('first token after restart')
      expect(harness.lastStreamSeq.value).toBe(1)
    } finally {
      harness.stop()
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

  it.each(['task.failed', 'task.timeout'])(
    'schedules queued follow-up delivery after %s settles the active task',
    (event) => {
      const {
        api,
        activeStreamTaskId,
        schedulePendingDrainAfterTerminal,
        stop,
      } = createHarness({
        pendingQueue: [{
          pendingUiId: 'pending-terminal-follow-up',
          text: 'Follow up',
          attachments: [],
          intent: null,
        }],
      })
      try {
        activeStreamTaskId.value = 'task-failed'
        api.handlers.onAny(event, {
          session_key: 'agent:main:test',
          task_id: 'task-failed',
          message: 'Provider failed',
        })

        expect(schedulePendingDrainAfterTerminal).toHaveBeenCalledOnce()
      } finally {
        stop()
      }
    },
  )

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
      pendingUiId: 'pending-ui-promoted-adjustment',
      text: steer.text,
      attachments: [],
      intent: null,
      steerAttempt: {
        phase: 'acceptance_unknown',
        request: {
          key: 'agent:main:test',
          message: steer.text,
          expected_turn_id: 'turn-old',
          client_request_id: 'request-1',
          client_message_id: 'client-1',
          surface_id: 'webui',
        },
      },
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

      const segments = [{
        model_call_id: '2.0',
        iteration: 2,
        start_codepoint: 3,
        end_codepoint: 6,
      }]
      api.handlers.onAny('session.event.done', {
        session_key: 'agent:main:test',
        stream_seq: 7,
        text_snapshot: '前半段后半段',
        model_call_segments: segments,
      })
      expect(stream.reconcileFinalText).toHaveBeenLastCalledWith('前半段后半段', segments)
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

  it('honors only the outer suppressed delivery contract and clears stale text', () => {
    const previous: ChatMessage = { role: 'assistant', text: 'previous', ts: 'before' }
    const { api, messages, stream, stop } = createHarness({ messages: [previous] })

    try {
      api.handlers.onAny('session.event.done', {
        session_key: 'agent:main:test',
        stream_seq: 1,
        text_snapshot: 'stale streamed answer',
        delivery: 'suppressed',
        suppression_reason: 'no_reply',
        input_tokens: 10,
        output_tokens: 1,
        model: 'z-ai/glm-5.2',
      })

      expect(stream.reconcileFinalText).toHaveBeenLastCalledWith('')
      expect(stream.endStreaming).toHaveBeenLastCalledWith({ suppressed: true })
      expect(messages.value).toEqual([previous])
      expect(previous.usage).toBeUndefined()

      api.handlers.onAny('session.event.done', {
        session_key: 'agent:main:test',
        stream_seq: 2,
        text_snapshot: 'visible despite diagnostic reason',
        suppression_reason: 'heartbeat_ack',
      })

      expect(stream.reconcileFinalText).toHaveBeenLastCalledWith(
        'visible despite diagnostic reason',
      )
      expect(stream.endStreaming).toHaveBeenLastCalledWith(undefined)
    } finally {
      stop()
    }
  })

  it('attaches suppressed-turn usage only to the preserved tool and artifact row', () => {
    const previous: ChatMessage = { role: 'assistant', text: 'previous', ts: 'before' }
    const { api, messages, stream, stop } = createHarness({
      messages: [previous],
      endStreaming(list) {
        list.push({
          role: 'assistant',
          text: '',
          ts: 'now',
          tool_calls: [{ type: 'tool_use', name: 'web_search', tool_use_id: 'tool-1' }],
          artifacts: [{ id: 'artifact-1', name: 'result.txt' }],
        })
      },
    })

    try {
      api.handlers.onAny('session.event.done', {
        session_key: 'agent:main:test',
        stream_seq: 1,
        text_snapshot: '',
        delivery: 'suppressed',
        suppression_reason: 'heartbeat_ack',
        input_tokens: 10,
        output_tokens: 1,
        model: 'z-ai/glm-5.2',
      })

      expect(stream.endStreaming).toHaveBeenLastCalledWith({ suppressed: true })
      expect(messages.value).toHaveLength(2)
      expect(messages.value[0]?.usage).toBeUndefined()
      expect(messages.value[1]).toMatchObject({
        text: '',
        model: 'z-ai/glm-5.2',
        input_tokens: 10,
        output_tokens: 1,
        artifacts: [{ id: 'artifact-1', name: 'result.txt' }],
      })
      expect(messages.value[1]?.usage).toBeDefined()
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
        turn_id: 'goal-turn-1',
        text: 'current',
        input_tokens: 10,
        output_tokens: 1,
        model: 'z-ai/glm-5.2',
        input_mode: 'system_event',
        run_kind: 'goal',
        model_usage_breakdown: [{ model: 'z-ai/glm-5.2', role: 'aggregator' }],
        ensemble_trace: { profile: 'default', llm_request_count: 5 },
        coverage_status: 'usage_unknown',
        usage_unknown: true,
        unknown_usage_events: 1,
      })

      expect(messages.value[0].usage).toBeUndefined()
      expect(messages.value[1].usage?.ensemble_trace).toEqual({
        profile: 'default',
        llm_request_count: 5,
      })
      expect(messages.value[1].usage).toMatchObject({
        coverage_status: 'usage_unknown',
        usage_unknown: true,
        unknown_usage_events: 1,
      })
      expect(messages.value[1].model).toBe('z-ai/glm-5.2')
      expect(messages.value[1].input_tokens).toBe(10)
      expect(messages.value[1].output_tokens).toBe(1)
      expect(messages.value[1].turnId).toBe('goal-turn-1')
      expect(messages.value[1].turnInputMode).toBe('system_event')
      expect(messages.value[1].turnRunKind).toBe('goal')
    } finally {
      stop()
    }
  })

  it('binds an aborted partial assistant to the terminal task identity', () => {
    const { api, messages, stream, stop } = createHarness({
      endStreaming(list) {
        list.push({ role: 'assistant', text: 'partial answer', ts: 'now' })
      },
    })

    try {
      api.handlers.onAny('session.event.done', {
        session_key: 'agent:main:test',
        stream_seq: 1,
        task_id: 'stopped-turn-1',
        reason: 'aborted',
        text_snapshot: 'partial answer',
      })

      expect(stream.endStreaming).toHaveBeenLastCalledWith({ reason: 'aborted' })
      expect(messages.value[0]).toMatchObject({
        role: 'assistant',
        text: 'partial answer',
        turnId: 'stopped-turn-1',
      })
    } finally {
      stop()
    }
  })
})

describe('useChatRpcEventHandlers reasoning timer replay', () => {
  it('keeps production reasoning text on the shared accumulator publish clock', () => {
    const { api, stream, messages, stop } = createHarness({
      endStreaming(list) {
        list.push({ role: 'assistant', text: 'answer', ts: 'now' })
      },
    })
    stream.useReducer.value = true
    stream.getThinkingText = vi.fn(() => 'folded reasoning')
    try {
      api.handlers.onAny('session.event.thinking', {
        session_key: 'agent:main:test',
        stream_seq: 1,
        text: 'folded ',
      })
      api.handlers.onAny('session.event.thinking', {
        session_key: 'agent:main:test',
        stream_seq: 2,
        text: 'reasoning',
      })

      expect(api.streamThinkingText.value).toBe('')
      expect(stream.appendFrame).toHaveBeenCalledTimes(2)
      api.handlers.onAny('session.event.done', {
        session_key: 'agent:main:test',
        stream_seq: 3,
        text: 'answer',
      })
      expect(messages.value[0]?.reasoning?.text).toBe('folded reasoning')
    } finally {
      stop()
    }
  })

  it('records structured start, delta, and end frames without losing legacy text', () => {
    const { api, stream, stop } = createHarness()
    stream.useReducer.value = 'shadow'

    try {
      api.handlers.onAny('session.event.thinking_start', {
        session_key: 'agent:main:test',
        stream_seq: 1,
        block_id: 'reasoning-1',
        block_index: 0,
        started_at: Date.now(),
      })
      api.handlers.onAny('session.event.thinking', {
        session_key: 'agent:main:test',
        stream_seq: 2,
        block_id: 'reasoning-1',
        block_index: 0,
        text: 'inspect',
        started_at: Date.now(),
      })
      api.handlers.onAny('session.event.thinking_end', {
        session_key: 'agent:main:test',
        stream_seq: 3,
        block_id: 'reasoning-1',
        block_index: 0,
        status: 'completed',
        ended_at: Date.now(),
      })

      expect(api.streamThinkingText.value).toBe('inspect')
      expect(stream.appendFrame).toHaveBeenNthCalledWith(1, expect.objectContaining({
        kind: 'thinking-start',
        blockId: 'reasoning-1',
        blockIndex: 0,
      }))
      expect(stream.appendFrame).toHaveBeenNthCalledWith(2, expect.objectContaining({
        kind: 'thinking',
        blockId: 'reasoning-1',
        text: 'inspect',
      }))
      expect(stream.appendFrame).toHaveBeenNthCalledWith(3, expect.objectContaining({
        kind: 'thinking-end',
        blockId: 'reasoning-1',
        status: 'completed',
      }))
    } finally {
      stop()
    }
  })

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

describe('useChatRpcEventHandlers terminal activity retention', () => {
  it('reattaches structured reasoning blocks after canonical history replacement', () => {
    const reasoningBlocks = [{
      id: 'reasoning-1',
      index: 0,
      text: 'inspect',
      status: 'completed' as const,
      startedAt: 1_000,
      endedAt: 3_000,
      contentKind: 'reasoning' as const,
    }]
    const { api, messages, stop } = createHarness({
      endStreaming(list) {
        list.push({
          role: 'assistant',
          text: 'answer',
          ts: 'now',
          reasoningBlocks,
        })
      },
    })

    try {
      api.handlers.onAny('session.event.done', {
        session_key: 'agent:main:test',
        stream_seq: 1,
        turn_id: 'turn-reasoning-record',
        text: 'answer',
        reasoning_content: 'inspect',
      })

      messages.value = [{
        role: 'assistant',
        text: 'answer',
        ts: 'now',
        turnId: 'turn-reasoning-record',
        reasoning: { text: 'inspect', seconds: 0 },
        restoredFromHistory: true,
      }]
      api.attachTurnReasoning()

      expect(messages.value[0]?.reasoningBlocks).toEqual(reasoningBlocks)
    } finally {
      stop()
    }
  })

  it('reattaches safe phase history after canonical history replaces the local row', () => {
    const phaseHistory = [
      { action: 'Sending', label: 'Sending', at: 1_000 },
      { action: 'provider:requesting', label: 'Waiting', at: 2_000 },
      { action: 'provider:reasoning', label: 'Reasoning', at: 3_000 },
      { action: 'write:1', label: 'Writing', at: 4_000 },
    ]
    const { api, messages, stop } = createHarness({
      endStreaming(list) {
        list.push({
          role: 'assistant',
          text: 'answer',
          ts: '2026-01-01T00:00:07.000Z',
          statusHistory: phaseHistory,
        })
      },
    })

    try {
      api.handlers.onAny('session.event.done', {
        session_key: 'agent:main:test',
        stream_seq: 1,
        turn_id: 'turn-phase-record',
        text: 'answer',
      })
      expect(messages.value[0]?.statusHistory).toEqual(phaseHistory)

      messages.value = [{
        role: 'assistant',
        text: 'answer',
        ts: '2026-01-01T00:00:07.000Z',
        turnId: 'turn-phase-record',
        restoredFromHistory: true,
      }]
      api.attachTurnReasoning()

      expect(messages.value[0]?.statusHistory).toEqual(phaseHistory)
    } finally {
      stop()
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

  it('treats every run heartbeat as transport liveness without replacing the phase', () => {
    const { api, stream, stop } = createHarness()

    try {
      api.handlers.onRunHeartbeat({ stream_seq: 1, phase: 'ensemble_proposers_wait' })
      api.handlers.onRunHeartbeat({ stream_seq: 2, phase: 'channel' })
      api.handlers.onRunHeartbeat({ stream_seq: 3, phase: 'ensemble_aggregator_stream' })
      api.handlers.onRunHeartbeat({ stream_seq: 4, phase: 'provider_wait' })

      expect(stream.setStreamActivity).not.toHaveBeenCalled()
      expect(stream.resetStreamIdleTimer).toHaveBeenCalledTimes(4)
      expect(stream.resetStreamIdleTimer).toHaveBeenCalledWith({ progress: false })
    } finally {
      stop()
    }
  })

  it('maps structured provider activity without rendering provider error text', () => {
    const { api, stream, stop } = createHarness()

    try {
      api.handlers.onProviderActivity({
        stream_seq: 1,
        schema_version: 1,
        phase: 'requesting',
        reason: 'initial',
        activity_id: 'activity-safe',
      })
      api.handlers.onProviderActivity({
        stream_seq: 2,
        schema_version: 1,
        phase: 'reasoning',
        reason: 'reasoning_only',
        activity_id: 'activity-safe',
      })
      api.handlers.onProviderActivity({
        stream_seq: 3,
        schema_version: 1,
        phase: 'retry_wait',
        reason: 'rate_limited',
        retry_after_ms: 8_000,
        activity_id: 'activity-safe',
        message: 'secret provider body',
      } as never)
      api.handlers.onProviderActivity({
        stream_seq: 4,
        schema_version: 1,
        phase: 'retrying',
        reason: 'rate_limited',
        retry_attempt: 2,
        retry_limit: 3,
        activity_id: 'activity-safe',
      })
      api.handlers.onProviderActivity({
        stream_seq: 5,
        schema_version: 1,
        phase: 'fallback',
        reason: 'provider_overloaded',
        activity_id: 'activity-safe',
      })

      expect(stream.setStreamActivity).toHaveBeenNthCalledWith(
        1,
        'Waiting for model',
        'provider:requesting',
      )
      expect(stream.setStreamActivity).toHaveBeenNthCalledWith(
        2,
        'Thinking deeply',
        'provider:reasoning',
      )
      expect(stream.setStreamActivity).toHaveBeenNthCalledWith(
        3,
        'Rate limited · 8s',
        'provider:rate_limited:8',
      )
      expect(stream.setStreamActivity).toHaveBeenNthCalledWith(
        4,
        'Retrying 2/3',
        'provider:retrying:2:3',
      )
      expect(stream.setStreamActivity).toHaveBeenNthCalledWith(
        5,
        'Switching to backup model',
        'provider:fallback',
      )
      expect(JSON.stringify(vi.mocked(stream.setStreamActivity).mock.calls))
        .not.toContain('secret provider body')
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
      expect(stream.resetStreamIdleTimer).toHaveBeenCalledWith({ progress: false })
    } finally {
      stop()
    }
  })

  it('restores durable setup work only after reconnect subscription succeeds', async () => {
    let resolveSubscription: ((subscribed: boolean) => void) | undefined
    const subscription = new Promise<boolean>((resolve) => { resolveSubscription = resolve })
    const { api, subscribeSession, onSessionSubscribed, stop } = createHarness({
      subscribeSession: () => subscription,
    })

    try {
      api.handlers.onConnectionState('connected')
      expect(subscribeSession).toHaveBeenCalledOnce()
      expect(onSessionSubscribed).not.toHaveBeenCalled()

      resolveSubscription?.(true)
      await subscription
      await Promise.resolve()

      expect(onSessionSubscribed).toHaveBeenCalledOnce()
    } finally {
      stop()
    }
  })

  it('does not restore durable setup work when reconnect subscription fails', async () => {
    const { api, onSessionSubscribed, stop } = createHarness({
      subscribeSession: async () => false,
    })

    try {
      api.handlers.onConnectionState('connected')
      await Promise.resolve()
      await Promise.resolve()

      expect(onSessionSubscribed).not.toHaveBeenCalled()
    } finally {
      stop()
    }
  })

  it('does not restore durable setup work from a non-authoritative outcome object', async () => {
    const { api, onSessionSubscribed, stop } = createHarness({
      subscribeSession: async () => ({
        authoritative: false,
        live: false,
        backgroundOnly: false,
      }),
    })

    try {
      api.handlers.onConnectionState('connected')
      await Promise.resolve()
      await Promise.resolve()

      expect(onSessionSubscribed).not.toHaveBeenCalled()
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

describe('useChatRpcEventHandlers durable turn receipts', () => {
  it('keeps legacy done behavior when the Gateway does not advertise receipts', () => {
    vi.useFakeTimers()
    const harness = createHarness()
    try {
      harness.api.handlers.onAny('session.event.done', {
        session_key: harness.sessionKey.value,
        task_id: 'task-legacy',
        stream_seq: 1,
        reason: 'completed',
        text: 'legacy answer',
      })

      expect(harness.stream.endStreaming).toHaveBeenCalledOnce()
      expect(harness.scheduleHistorySync).toHaveBeenCalledOnce()
      expect(harness.scheduleHistorySync).toHaveBeenCalledWith()
      expect(harness.api.awaitingCommitTaskIds.value).toEqual(new Set())
      harness.api.handlers.onAny('session.event.turn_committed', {
        schema_version: 1,
        session_key: harness.sessionKey.value,
        task_id: 'task-legacy',
        turn_id: 'turn-legacy',
        stream_seq: 2,
        status: 'succeeded',
        terminal_reason: 'completed',
        finished_at: 123,
      })
      expect(harness.lastStreamSeq.value).toBe(1)
      vi.advanceTimersByTime(5_000)
      expect(harness.scheduleHistorySync).toHaveBeenCalledOnce()
    } finally {
      harness.stop()
      vi.useRealTimers()
    }
  })

  it('validates a receipt before sequence consumption and handles replay idempotently', () => {
    vi.useFakeTimers()
    const harness = createHarness({ supportsTurnCommitted: true })
    try {
      harness.api.handlers.onAny('session.event.done', {
        session_key: harness.sessionKey.value,
        task_id: 'task-durable',
        turn_id: 'turn-durable',
        stream_seq: 1,
        reason: 'completed',
        text: 'durable answer',
      })

      expect(harness.stream.endStreaming).toHaveBeenCalledOnce()
      expect(harness.scheduleHistorySync).not.toHaveBeenCalled()
      expect(harness.api.awaitingCommitTaskIds.value).toEqual(new Set(['task-durable']))

      harness.api.handlers.onAny('session.event.turn_committed', {
        schema_version: 1,
        session_key: harness.sessionKey.value,
        task_id: 'task-durable',
        stream_seq: 99,
        status: 'succeeded',
        terminal_reason: 'completed',
        finished_at: 123,
      })
      expect(harness.lastStreamSeq.value).toBe(1)
      expect(harness.scheduleHistorySync).not.toHaveBeenCalled()

      harness.api.handlers.onAny('task.succeeded', {
        session_key: harness.sessionKey.value,
        task_id: 'task-durable',
        stream_seq: 2,
        status: 'succeeded',
      })
      expect(harness.scheduleHistorySync).toHaveBeenCalledOnce()
      expect(harness.scheduleHistorySync).toHaveBeenLastCalledWith(true)

      harness.api.handlers.onAny('session.event.turn_committed', {
        schema_version: 1,
        session_key: harness.sessionKey.value,
        task_id: 'task-durable',
        turn_id: 'turn-durable',
        stream_seq: 3,
        status: 'succeeded',
        terminal_reason: 'completed',
        finished_at: 123,
      })
      expect(harness.api.awaitingCommitTaskIds.value).toEqual(new Set())
      expect(harness.scheduleHistorySync).toHaveBeenCalledTimes(2)
      expect(harness.scheduleHistorySync).toHaveBeenLastCalledWith(true)

      harness.api.handlers.onAny('session.event.turn_committed', {
        schema_version: 1,
        session_key: harness.sessionKey.value,
        task_id: 'task-durable',
        turn_id: 'turn-durable',
        stream_seq: 4,
        status: 'succeeded',
        terminal_reason: 'completed',
        finished_at: 123,
      })
      vi.advanceTimersByTime(5_000)
      expect(harness.scheduleHistorySync).toHaveBeenCalledTimes(2)
      expect(harness.showWarningToast).not.toHaveBeenCalled()
      expect(harness.handleSessionConnectionState).not.toHaveBeenCalled()
    } finally {
      harness.stop()
      vi.useRealTimers()
    }
  })

  it('performs one silent safe sync after five seconds without blocking done', () => {
    vi.useFakeTimers()
    const harness = createHarness({ supportsTurnCommitted: true })
    try {
      harness.api.handlers.onAny('session.event.done', {
        session_key: harness.sessionKey.value,
        task_id: 'task-delayed',
        turn_id: 'turn-delayed',
        stream_seq: 1,
        reason: 'completed',
        text: 'visible answer',
      })

      expect(harness.stream.endStreaming).toHaveBeenCalledOnce()
      expect(harness.scheduleHistorySync).not.toHaveBeenCalled()
      vi.advanceTimersByTime(4_999)
      expect(harness.scheduleHistorySync).not.toHaveBeenCalled()
      vi.advanceTimersByTime(1)
      expect(harness.scheduleHistorySync).toHaveBeenCalledOnce()
      expect(harness.scheduleHistorySync).toHaveBeenLastCalledWith(true)
      vi.advanceTimersByTime(30_000)
      expect(harness.scheduleHistorySync).toHaveBeenCalledOnce()
      expect(harness.showWarningToast).not.toHaveBeenCalled()
      expect(harness.handleSessionConnectionState).not.toHaveBeenCalled()
    } finally {
      harness.stop()
      vi.useRealTimers()
    }
  })

  it('cancels commit waiting when finalization transitions the task to failed', () => {
    vi.useFakeTimers()
    const harness = createHarness({ supportsTurnCommitted: true })
    try {
      harness.api.handlers.onAny('session.event.done', {
        session_key: harness.sessionKey.value,
        task_id: 'task-finalizer-failed',
        turn_id: 'turn-finalizer-failed',
        stream_seq: 1,
        reason: 'completed',
        text: 'answer before finalizer failure',
      })
      expect(harness.api.awaitingCommitTaskIds.value).toEqual(
        new Set(['task-finalizer-failed']),
      )

      harness.api.handlers.onAny('task.failed', {
        session_key: harness.sessionKey.value,
        task_id: 'task-finalizer-failed',
        stream_seq: 2,
        status: 'failed',
        terminal_message: 'transcript finalizer failed',
      })

      expect(harness.api.awaitingCommitTaskIds.value).toEqual(new Set())
      const historyCallsAfterFailure = harness.scheduleHistorySync.mock.calls.length
      vi.advanceTimersByTime(5_000)
      expect(harness.scheduleHistorySync).toHaveBeenCalledTimes(historyCallsAfterFailure)
      expect(harness.scheduleHistorySync.mock.calls).not.toContainEqual([true])
    } finally {
      harness.stop()
      vi.useRealTimers()
    }
  })
})
