import { describe, expect, it, vi } from 'vitest'
import { effectScope, ref } from 'vue'
import { useChatRpcEventHandlers, type ChatRpcStreamApi } from './useChatRpcEventHandlers'
import type { ChatMessage, ChatRunStatus, ChatRunStatusSource } from '@/types/chat'

function createHarness(options: {
  messages?: ChatMessage[]
  endStreaming?: (messages: ChatMessage[]) => void
  sessionRunStatus?: (source: ChatRunStatusSource | null | undefined) => ChatRunStatus
} = {}) {
  const messages = ref<ChatMessage[]>(options.messages ?? [])
  const activeTaskGroups = ref(new Set<string>())
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
    resetStreamIdleTimer: vi.fn(),
    clearStreamIdleTimer: vi.fn(),
    setStreamActivity: vi.fn(),
    showThinkingIndicator: vi.fn(),
    hideThinkingIndicator: vi.fn(),
    appendFrame: vi.fn(),
    useReducer: ref(false),
  }
  const markEnsembleHandoff = vi.fn()
  const schedulePendingDrainAfterTerminal = vi.fn()
  const scheduleHistorySync = vi.fn()
  const showWarningToast = vi.fn()
  const scope = effectScope()
  const api = scope.run(() => useChatRpcEventHandlers({
    sessionKey: ref('agent:main:test'),
    currentEpoch: ref(0),
    lastStreamSeq: ref(0),
    activeTaskGroups,
    activeStreamTaskId: ref(''),
    aborted: ref(false),
    messages,
    pendingQueue: ref([]),
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
    showCompactionToast: vi.fn(),
    showWarningToast,
    scheduleHistorySync,
    schedulePendingDrainAfterTerminal,
    popAllPendingIntoComposer: vi.fn(() => false),
    saveWidgetState: vi.fn(),
    subscribeSession: vi.fn(),
    loadHistory: vi.fn(),
    loadCurrentSessionUsage: vi.fn(),
  }))!
  return {
    api,
    messages,
    stream,
    activeTaskGroups,
    applySessionRunState,
    markEnsembleHandoff,
    schedulePendingDrainAfterTerminal,
    scheduleHistorySync,
    showWarningToast,
    stop: () => scope.stop(),
  }
}

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
    const { api, showWarningToast, messages, stop } = createHarness()
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
        message: 'Provider is degraded',
      })

      expect(showWarningToast).toHaveBeenCalledOnce()
      expect(showWarningToast).toHaveBeenCalledWith('Provider is degraded')
      expect(messages.value).toHaveLength(0)
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

  it('maps ensemble heartbeats to neutral proposer and aggregator phase copy', () => {
    const { api, stream, stop } = createHarness()

    try {
      api.handlers.onRunHeartbeat({ stream_seq: 1, phase: 'ensemble_proposers_wait' })
      expect(stream.setStreamActivity).toHaveBeenLastCalledWith('Generating candidates')

      api.handlers.onRunHeartbeat({ stream_seq: 2, phase: 'ensemble_aggregator_stream' })
      expect(stream.setStreamActivity).toHaveBeenLastCalledWith('Synthesizing candidates')

      api.handlers.onRunHeartbeat({ stream_seq: 3, phase: 'provider_wait' })
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
})
