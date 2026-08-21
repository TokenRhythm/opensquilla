// @vitest-environment happy-dom
import { describe, expect, it, vi } from 'vitest'
import { nextTick, ref, type Ref } from 'vue'

import { useChatHistory } from './useChatHistory'
import type { ChatMessage } from '@/types/chat'
import type { ChatHistoryResponse } from '@/types/rpc'
import { RpcTimeoutError } from '@/lib/rpc'

function makeHistory(autoScroll = true, overrides: {
  response?: ChatHistoryResponse
  messages?: ChatMessage[]
  preserveLiveTail?: boolean
  sessionKey?: Ref<string>
  threadRef?: Ref<HTMLElement | null>
  concurrentHistoryReads?: boolean
} = {}) {
  const response: ChatHistoryResponse = overrides.response || {
    messages: [
      {
        id: 'm1',
        message_id: 'm1',
        role: 'assistant',
        text: 'hello',
        timestamp: '2026-07-06T00:00:00Z',
      },
    ],
    has_more: false,
    oldest_cursor: null,
    newest_cursor: null,
    history_scope: 'session',
  }
  const messages = ref<ChatMessage[]>(overrides.messages || [])
  const rpc = {
    policy: {
      concurrent_history_reads: overrides.concurrentHistoryReads ?? true,
    },
    waitForConnection: vi.fn().mockResolvedValue(undefined),
    call: vi.fn().mockResolvedValue(response),
  }
  const scrollToBottom = vi.fn()
  const api = useChatHistory({
    rpc,
    sessionKey: overrides.sessionKey || ref('agent:main:webchat:test'),
    messages,
    threadRef: overrides.threadRef,
    lastHeaderRole: ref(''),
    lastHeaderDay: ref(''),
    preserveLiveTail: ref(overrides.preserveLiveTail ?? false),
    autoScroll: ref(autoScroll),
    stripTimePrefix: text => text,
    scrollToBottom,
  })
  return { api, rpc, scrollToBottom, messages }
}

function historyMessage(id: string): NonNullable<ChatHistoryResponse['messages']>[number] {
  return {
    id,
    message_id: id,
    role: 'assistant',
    text: id,
    timestamp: `2026-07-06T00:00:${id.replace(/\D/g, '').padStart(2, '0')}Z`,
  }
}

describe('useChatHistory canonical pagination', () => {
  it('restores nested prompt annotation snapshots on an annotation-only user row', async () => {
    const { api, messages } = makeHistory(false, {
      response: {
        messages: [{
          id: 'user-annotation-1',
          message_id: 'user-annotation-1',
          role: 'user',
          text: '',
          timestamp: '2026-07-06T00:00:00Z',
          promptAnnotations: [{
            version: 1,
            annotationId: 'annotation-history-1',
            order: 2,
            body: 'Make the primary action red.',
            document: { id: 'document-1', name: 'page.html', kind: 'html' },
            revision: { id: 'revision-3', generation: 3, sha256: 'a'.repeat(64) },
            anchor: {
              id: 'anchor-2',
              kind: 'dom_source',
              tagName: 'BUTTON',
              locator: { start_offset: 7 },
              quote: '<button>',
            },
          }],
        }],
        has_more: false,
      },
    })

    await api.loadHistory()

    expect(messages.value).toHaveLength(1)
    expect(messages.value[0]).toMatchObject({
      role: 'user',
      text: '',
      restoredFromHistory: true,
      promptAnnotations: [{
        annotationId: 'annotation-history-1',
        documentId: 'document-1',
        documentName: 'page.html',
        revisionId: 'revision-3',
        generation: 3,
        anchorId: 'anchor-2',
        body: 'Make the primary action red.',
        tagName: 'button',
        sentOrder: 2,
      }],
    })
  })

  it('preserves semantic text presentation from canonical history', async () => {
    const { api, messages } = makeHistory(false, {
      response: {
        messages: [{
          id: 'assistant-1',
          message_id: 'assistant-1',
          role: 'assistant',
          text: 'Working note.Final answer.',
          timestamp: '2026-07-06T00:00:00Z',
          timeline: [
            { type: 'text', raw: 'Working note.', presentation: 'intermediate' },
            { type: 'text', raw: 'Final answer.', presentation: 'answer' },
          ],
        }],
        has_more: false,
      },
    })

    await api.loadHistory()

    expect(messages.value[0]?.timeline).toEqual([
      { type: 'text', raw: 'Working note.', presentation: 'intermediate' },
      { type: 'text', raw: 'Final answer.', presentation: 'answer' },
    ])
  })

  it('does not expose an ordinary send disposition as same-turn steer status', async () => {
    const { api, messages } = makeHistory(false, {
      response: {
        messages: [{
          id: 'user-send',
          message_id: 'user-send',
          role: 'user',
          text: 'ordinary queued follow-up',
          timestamp: '2026-07-06T00:00:00Z',
          turn_context: {
            turn_id: 'turn-send',
            target_turn_id: 'turn-send',
            client_request_id: 'request-send',
            client_message_id: 'client-send',
            intent: 'send',
            disposition: 'applied',
            revision: 1,
          },
        }],
        has_more: false,
      },
    })

    await api.loadHistory()

    expect(messages.value[0]).toMatchObject({
      role: 'user',
      text: 'ordinary queued follow-up',
      turnId: 'turn-send',
    })
    expect(messages.value[0]?.inputDisposition).toBeUndefined()
    expect(messages.value[0]?.inputDispositionRevision).toBeUndefined()
    expect(messages.value[0]?.steerClientRequestId).toBeUndefined()
    expect(messages.value[0]?.steerClientMessageId).toBeUndefined()
  })

  it('restores an explicit Steer intent without relying on shared transport IDs', async () => {
    const { api, messages } = makeHistory(false, {
      response: {
        messages: [{
          id: 'current-steer',
          message_id: 'current-steer',
          role: 'user',
          text: 'current same-turn correction',
          timestamp: '2026-07-06T00:00:00Z',
          turn_context: {
            turn_id: 'turn-steer',
            client_request_id: 'request-steer',
            client_message_id: 'client-steer',
            intent: 'steer',
            disposition: 'applied',
            revision: 2,
          },
        }],
        has_more: false,
      },
    })

    await api.loadHistory()

    expect(messages.value[0]).toMatchObject({
      inputDisposition: 'applied',
      inputDispositionRevision: 2,
      steerClientRequestId: 'request-steer',
      steerClientMessageId: 'client-steer',
    })
  })

  it('does not infer legacy Steer UX from shared primary-input fields', async () => {
    const { api, messages } = makeHistory(false, {
      response: {
        messages: (['applied', 'cancelled', 'rejected'] as const).map((disposition, index) => ({
          id: `legacy-send-${disposition}`,
          message_id: `legacy-send-${disposition}`,
          role: 'user' as const,
          text: `legacy primary ${disposition}`,
          timestamp: `2026-07-06T00:00:0${index}Z`,
          turn_context: {
            turn_id: 'turn-send',
            target_turn_id: 'turn-send',
            client_request_id: `request-${disposition}`,
            client_message_id: `client-${disposition}`,
            disposition,
            revision: 2,
            applied_iteration: null,
          },
        })),
        has_more: false,
      },
    })

    await api.loadHistory()

    for (const message of messages.value) {
      expect(message.inputDisposition).toBeUndefined()
      expect(message.inputDispositionRevision).toBeUndefined()
      expect(message.steerClientRequestId).toBeUndefined()
      expect(message.steerClientMessageId).toBeUndefined()
    }
  })

  it('restores an applied legacy steer from model-call evidence when intent is absent', async () => {
    const { api, messages } = makeHistory(false, {
      response: {
        messages: [{
          id: 'legacy-steer',
          message_id: 'legacy-steer',
          role: 'user',
          text: 'legacy same-turn correction',
          timestamp: '2026-07-06T00:00:00Z',
          turn_context: {
            turn_id: 'turn-steer',
            client_request_id: 'request-steer',
            client_message_id: 'client-steer',
            disposition: 'applied',
            revision: 2,
            model_call_id: '2.0',
            applied_iteration: 2,
          },
        }],
        has_more: false,
      },
    })

    await api.loadHistory()

    expect(messages.value[0]).toMatchObject({
      role: 'user',
      text: 'legacy same-turn correction',
      turnId: 'turn-steer',
      inputDisposition: 'applied',
      inputDispositionRevision: 2,
      steerClientRequestId: 'request-steer',
      steerClientMessageId: 'client-steer',
      steerModelCallId: '2.0',
      steerAppliedIteration: 2,
    })
  })

  it('projects durable internal turn provenance without mutating history context', async () => {
    const turnContext = {
      turn_id: 'turn-goal',
      input_mode: 'system_event',
      run_kind: 'goal',
    }
    const { api, messages } = makeHistory(false, {
      response: {
        messages: [{
          id: 'assistant-goal',
          message_id: 'assistant-goal',
          role: 'assistant',
          text: 'NO_REPLY\nGoal progress',
          timestamp: '2026-07-06T00:00:00Z',
          turn_context: turnContext,
        }],
        has_more: false,
      },
    })

    await api.loadHistory()

    expect(messages.value[0]).toMatchObject({
      turnId: 'turn-goal',
      turnInputMode: 'system_event',
      turnRunKind: 'goal',
    })
    expect(turnContext).toEqual({
      turn_id: 'turn-goal',
      input_mode: 'system_event',
      run_kind: 'goal',
    })
  })

  it('derives internal goal provenance from a legacy goal_continuation intent', async () => {
    const turnContext = {
      turn_id: 'turn-legacy-goal',
      intent: 'goal_continuation',
    }
    const { api, messages } = makeHistory(false, {
      response: {
        messages: [{
          id: 'assistant-legacy-goal',
          message_id: 'assistant-legacy-goal',
          role: 'assistant',
          text: 'NO_REPLY\nGoal progress',
          timestamp: '2026-07-06T00:00:00Z',
          turn_context: turnContext,
        }],
        has_more: false,
      },
    })

    await api.loadHistory()

    expect(messages.value[0]).toMatchObject({
      turnId: 'turn-legacy-goal',
      turnInputMode: 'system_event',
      turnRunKind: 'goal',
    })
    expect(turnContext).toEqual({
      turn_id: 'turn-legacy-goal',
      intent: 'goal_continuation',
    })
  })

  it('preserves additive cancellation usage coverage from canonical history', async () => {
    const usage = {
      input_tokens: 1,
      output_tokens: 1,
      cost_usd: 0,
      coverage_status: 'usage_unknown',
      usage_unknown: true,
      unknown_usage_events: 1,
    }
    const { api, messages } = makeHistory(false, {
      response: {
        messages: [{
          id: 'assistant-cancelled',
          message_id: 'assistant-cancelled',
          role: 'assistant',
          text: 'Partial answer',
          timestamp: '2026-07-06T00:00:00Z',
          turn_context: { turn_id: 'turn-cancelled' },
          usage,
        }],
        has_more: false,
      },
    })

    await api.loadHistory()

    expect(messages.value[0]?.usage).toEqual(usage)
    expect(usage).toEqual({
      input_tokens: 1,
      output_tokens: 1,
      cost_usd: 0,
      coverage_status: 'usage_unknown',
      usage_unknown: true,
      unknown_usage_events: 1,
    })
  })

  it('requests canonical messages with durable compaction summaries', async () => {
    const { api, rpc } = makeHistory()

    expect(api.historyState.value.initialLoadStatus).toBe('pending')
    await api.loadHistory()

    expect(rpc.call).toHaveBeenCalledWith('chat.history', expect.objectContaining({
      includeCanonical: true,
      includeSummaries: true,
    }), expect.objectContaining({ timeoutAction: 'reject' }))
    expect(api.historyState.value).toMatchObject({
      initialLoadStatus: 'ready',
    })
  })

  it('keeps one legacy activity row across prepend and canonical refresh', async () => {
    const activity = {
      id: 'legacy-tools',
      message_id: 'legacy-tools',
      role: 'assistant',
      text: '',
      timestamp: '2026-07-06T00:00:01Z',
      tool_calls: [
        { type: 'text', text: 'Inspect the source.' },
        { type: 'tool_use', tool_use_id: 'call-read', name: 'read_file', input: {} },
        { type: 'text', text: 'Compare the directory.' },
        { type: 'tool_use', tool_use_id: 'call-list', name: 'list_dir', input: {} },
        { type: 'tool_result', tool_use_id: 'call-read', name: 'read_file', result: 'source' },
        { type: 'tool_result', tool_use_id: 'call-list', name: 'list_dir', result: 'directory' },
      ],
    }
    const { api, rpc, messages } = makeHistory(false, {
      response: {
        messages: [activity],
        canonical_complete: false,
        has_more: true,
        oldest_cursor: 'cursor-tools',
        newest_cursor: 'cursor-tools',
      },
    })
    rpc.call
      .mockResolvedValueOnce({
        messages: [activity],
        canonical_complete: false,
        has_more: true,
        oldest_cursor: 'cursor-tools',
        newest_cursor: 'cursor-tools',
      })
      .mockResolvedValueOnce({
        messages: [{
          id: 'older-user',
          message_id: 'older-user',
          role: 'user',
          text: 'Earlier request',
          timestamp: '2026-07-06T00:00:00Z',
        }],
        canonical_complete: false,
        has_more: false,
        oldest_cursor: 'cursor-older',
        newest_cursor: 'cursor-older',
      })
      .mockResolvedValueOnce({
        messages: [
          activity,
          {
            id: 'later-user',
            message_id: 'later-user',
            role: 'user',
            text: 'Continue',
            timestamp: '2026-07-06T00:00:02Z',
          },
        ],
        canonical_complete: true,
        has_more: false,
        oldest_cursor: 'cursor-tools',
        newest_cursor: 'cursor-later',
      })

    await api.loadHistory()
    await api.loadEarlierHistory()
    await api.loadHistory()

    expect(messages.value.map(message => message.messageId)).toEqual([
      'older-user',
      'legacy-tools',
      'later-user',
    ])
    expect(messages.value.filter(message => message.messageId === 'legacy-tools')).toHaveLength(1)
    expect(messages.value[1]?.tool_calls?.map(segment => segment.tool_use_id)).toEqual([
      undefined,
      'call-read',
      undefined,
      'call-list',
      'call-read',
      'call-list',
    ])
    expect(api.historyState.value.canonicalComplete).toBe(true)
  })

  it('restores manual compaction summaries in stable transcript chronology', async () => {
    const baseTime = 1_720_000_000_000
    const response: ChatHistoryResponse = {
      messages: [
        {
          id: 'user-1',
          message_id: 'user-1',
          role: 'user',
          text: 'Earlier request',
          timestamp: baseTime,
        },
        {
          id: 'assistant-1',
          message_id: 'assistant-1',
          role: 'assistant',
          text: 'Earlier answer',
          timestamp: baseTime + 1_000,
        },
        {
          id: 'user-2',
          message_id: 'user-2',
          role: 'user',
          text: 'Continue',
          timestamp: baseTime + 3_000,
        },
      ],
      canonical_complete: true,
      compaction_summaries: [
        {
          id: 9,
          compaction_id: 'cmp-9',
          compaction_index: 2,
          trigger_reason: 'manual',
          removed_count: 8,
          kept_count: 2,
          created_at: 1_720_000_001,
        },
        {
          id: 7,
          compaction_id: 'cmp-7',
          compaction_index: 1,
          trigger_reason: 'manual',
          removed_count: 5,
          kept_count: 1,
          created_at: 1_720_000_001,
        },
        {
          id: 8,
          compaction_id: 'cmp-auto',
          trigger_reason: 'auto_threshold',
          created_at: 1_720_000_002,
        },
      ],
      has_more: false,
    }
    const { api, messages } = makeHistory(false, {
      response,
      messages: [{
        role: 'maintenance',
        text: '',
        ts: baseTime + 500,
        messageId: 'maintenance:optimistic:cmp-7',
        maintenance: {
          kind: 'context_compaction',
          compactionId: 'cmp-7',
          source: 'manual',
          state: 'completed',
          durability: 'durable',
        },
      }],
    })

    await api.loadHistory()

    const expectedIds = [
      'user-1',
      'assistant-1',
      'maintenance:context-compaction:summary:7',
      'maintenance:context-compaction:summary:9',
      'maintenance:context-compaction:summary:8',
      'user-2',
    ]
    expect(messages.value.map(message => message.messageId)).toEqual(expectedIds)
    expect(messages.value[2]).toMatchObject({
      role: 'maintenance',
      text: '',
      ts: baseTime + 1_000,
      restoredFromHistory: true,
      maintenance: {
        kind: 'context_compaction',
        compactionId: 'cmp-7',
        source: 'manual',
        state: 'completed',
        durability: 'durable',
        removedCount: 5,
        keptCount: 1,
        historyArchived: true,
        canonicalComplete: true,
      },
    })
    expect(messages.value[4]).toMatchObject({
      maintenance: {
        compactionId: 'cmp-auto',
        source: 'automatic',
        historyArchived: true,
        canonicalComplete: true,
      },
    })
    expect(messages.value.filter(message =>
      message.maintenance?.compactionId === 'cmp-7',
    )).toHaveLength(1)

    // A background refresh receives the same metadata. Stable ids and the
    // timestamp tie-breaker keep both membership and order unchanged.
    await api.loadHistory()
    expect(messages.value.map(message => message.messageId)).toEqual(expectedIds)
  })

  it('inserts maintenance without reordering promoted canonical rows', async () => {
    const baseTime = 1_720_000_000_000
    const { api, messages } = makeHistory(false, {
      response: {
        messages: [
          {
            id: 'user-old',
            message_id: 'user-old',
            role: 'user',
            text: 'Original request',
            timestamp: baseTime,
            turn_context: { turn_id: 'turn-old' },
          },
          {
            id: 'steer-1',
            message_id: 'steer-1',
            role: 'user',
            text: 'Use the new constraint',
            timestamp: baseTime + 1_000,
            turn_context: {
              turn_id: 'turn-new',
              promoted_from_turn_id: 'turn-old',
              disposition: 'promoted',
              revision: 2,
            },
          },
          {
            id: 'assistant-old',
            message_id: 'assistant-old',
            role: 'assistant',
            text: 'Completed old turn',
            timestamp: baseTime + 2_000,
            turn_context: { turn_id: 'turn-old' },
          },
          {
            id: 'assistant-new',
            message_id: 'assistant-new',
            role: 'assistant',
            text: 'Completed promoted turn',
            timestamp: baseTime + 3_000,
            turn_context: { turn_id: 'turn-new' },
          },
        ],
        compaction_summaries: [{
          id: 11,
          compaction_id: 'cmp-promoted',
          trigger_reason: 'manual',
          created_at: baseTime + 1_500,
        }],
        has_more: false,
      },
    })

    await api.loadHistory()

    expect(messages.value
      .filter(message => message.role !== 'maintenance')
      .map(message => message.messageId)).toEqual([
      'user-old',
      'assistant-old',
      'steer-1',
      'assistant-new',
    ])
    expect(messages.value.map(message => message.messageId)).toEqual([
      'user-old',
      'maintenance:context-compaction:summary:11',
      'assistant-old',
      'steer-1',
      'assistant-new',
    ])
  })

  it('applies the shared bootstrap deadline without recycling on history timeout', async () => {
    const { api, rpc } = makeHistory()
    const now = Date.now()
    const controller = new AbortController()

    await api.loadHistory({}, {
      generation: 1,
      key: 'agent:main:webchat:test',
      attempt: 0,
      deadlineAt: now + 15_000,
      attemptDeadlineAt: now + 7_000,
      signal: controller.signal,
      skipSnapshot: false,
    })

    expect(rpc.waitForConnection).toHaveBeenCalledWith(
      expect.any(Number),
      expect.any(AbortSignal),
      {
        timeoutAction: 'reject',
        abortAction: 'reject',
      },
    )
    expect(rpc.call).toHaveBeenCalledWith(
      'chat.history',
      expect.objectContaining({ includeCanonical: true }),
      expect.objectContaining({
        timeoutMs: expect.any(Number),
        signal: expect.any(AbortSignal),
        timeoutAction: 'reject',
        abortAction: 'reject',
        onSent: expect.any(Function),
      }),
    )
  })

  it('recycles a legacy serial Gateway when history is abandoned', async () => {
    const { api, rpc } = makeHistory(true, {
      concurrentHistoryReads: false,
    })
    const now = Date.now()

    await api.loadHistory({}, {
      generation: 1,
      key: 'agent:main:webchat:test',
      attempt: 0,
      deadlineAt: now + 15_000,
      attemptDeadlineAt: now + 7_000,
      signal: new AbortController().signal,
      skipSnapshot: false,
    })

    expect(rpc.call).toHaveBeenCalledWith(
      'chat.history',
      expect.any(Object),
      expect.objectContaining({
        timeoutAction: 'reconnect',
        abortAction: 'reconnect',
      }),
    )
  })

  it('enters the initial loading state before the first RPC settles', async () => {
    let resolveHistory!: (value: ChatHistoryResponse) => void
    const pendingHistory = new Promise<ChatHistoryResponse>(resolve => {
      resolveHistory = resolve
    })
    const { api, rpc } = makeHistory()
    rpc.call.mockReturnValueOnce(pendingHistory)

    const load = api.loadHistory()
    expect(api.historyState.value.initialLoadStatus).toBe('loading')

    resolveHistory({
      messages: [],
      has_more: false,
      oldest_cursor: null,
    })
    await load
    expect(api.historyState.value.initialLoadStatus).toBe('ready')
  })

  it('does not restore the full-screen loader for a settled empty-session refresh', async () => {
    let resolveRefresh!: (value: ChatHistoryResponse) => void
    const refreshResponse = new Promise<ChatHistoryResponse>(resolve => {
      resolveRefresh = resolve
    })
    const { api, rpc } = makeHistory(false, {
      response: {
        messages: [],
        has_more: false,
        oldest_cursor: null,
      },
    })

    await api.loadHistory()
    rpc.call.mockReturnValueOnce(refreshResponse)
    const refresh = api.loadHistory()

    expect(api.historyState.value.loading).toBe(true)
    expect(api.historyState.value.initialLoadStatus).toBe('ready')

    resolveRefresh({
      messages: [],
      has_more: false,
      oldest_cursor: null,
    })
    await refresh
    expect(api.historyState.value.initialLoadStatus).toBe('ready')
  })

  it('keeps a settled empty-session refresh failure retryable without restoring the loader', async () => {
    const { api, rpc } = makeHistory(false, {
      response: {
        messages: [],
        has_more: false,
        oldest_cursor: null,
      },
    })

    await api.loadHistory()
    rpc.call.mockRejectedValueOnce(new Error('offline'))
    await api.loadHistory()

    expect(api.historyState.value).toMatchObject({
      initialLoadStatus: 'ready',
      loadEarlierError: false,
      recoveryError: true,
    })

    let resolveRetry!: (value: ChatHistoryResponse) => void
    rpc.call.mockReturnValueOnce(new Promise<ChatHistoryResponse>(resolve => {
      resolveRetry = resolve
    }))
    const retry = api.retryHistory()
    expect(api.historyState.value).toMatchObject({
      initialLoadStatus: 'ready',
      loading: true,
      retrying: true,
      loadEarlierError: false,
    })
    resolveRetry({
      messages: [],
      has_more: false,
      oldest_cursor: null,
    })
    await retry
    expect(rpc.call).toHaveBeenCalledTimes(3)
    expect(api.historyState.value).toMatchObject({
      initialLoadStatus: 'ready',
      retrying: false,
      loadEarlierError: false,
      recoveryError: false,
    })
  })

  it('keeps loaded messages visible and exposes an inline recovery error after refresh fails', async () => {
    const { api, rpc, messages } = makeHistory()
    await api.loadHistory()
    expect(messages.value.map(message => message.text)).toEqual(['hello'])

    rpc.call.mockRejectedValueOnce(new Error('refresh disconnected'))
    await api.loadHistory()

    expect(messages.value.map(message => message.text)).toEqual(['hello'])
    expect(api.historyState.value).toMatchObject({
      initialLoadStatus: 'ready',
      loading: false,
      recoveryError: true,
    })
  })

  it('keeps an unavailable canonical reader retryable after an empty session has settled', async () => {
    const { api, rpc } = makeHistory(false, {
      response: {
        messages: [],
        has_more: false,
        oldest_cursor: null,
      },
    })

    await api.loadHistory()
    rpc.call
      .mockResolvedValueOnce({
        messages: [],
        has_more: false,
        oldest_cursor: null,
        canonical_available: false,
        canonical_complete: false,
      })
      .mockResolvedValueOnce({
        messages: [],
        has_more: false,
        oldest_cursor: null,
        canonical_available: true,
        canonical_complete: true,
      })
    await api.loadHistory()

    expect(api.historyState.value).toMatchObject({
      initialLoadStatus: 'ready',
      canonicalAvailable: false,
      canonicalComplete: false,
      loadEarlierError: false,
    })

    await api.retryHistory()
    expect(rpc.call).toHaveBeenCalledTimes(3)
    expect(api.historyState.value).toMatchObject({
      initialLoadStatus: 'ready',
      canonicalAvailable: true,
      canonicalComplete: true,
    })
  })

  it('restores the durable causal turn identity from canonical history', async () => {
    const { api, messages } = makeHistory(false, {
      response: {
        messages: [{
          id: 'assistant-1',
          message_id: 'assistant-1',
          role: 'assistant',
          text: 'partial answer',
          timestamp: '2026-07-06T00:00:00Z',
          turn_context: {
            turn_id: 'turn-1',
            intent: 'send',
          },
        }],
        has_more: false,
      },
    })

    await api.loadHistory()

    expect(messages.value[0]?.turnId).toBe('turn-1')
  })

  it('prefers a durable summary boundary over duplicate activity metadata', async () => {
    const { api, messages } = makeHistory(false, {
      response: {
        messages: [{
          id: 'assistant-1',
          message_id: 'assistant-1',
          role: 'assistant',
          text: 'answer after compaction',
          timestamp: '2026-07-06T00:00:00Z',
          turn_context: {
            turn_id: 'turn-1',
            activity_markers: [{
              kind: 'context_compaction',
              id: 'cmp-history',
              status: 'completed',
              at: 1_720_000_000_000,
            }],
          },
        }],
        canonical_complete: true,
        compaction_summaries: [{
          id: 12,
          compaction_id: 'cmp-history',
          trigger_reason: 'manual',
          created_at: 1_720_000_000_000,
        }],
        has_more: false,
      },
    })

    await api.loadHistory()

    const assistant = messages.value.find(message => message.role === 'assistant')
    expect(assistant).toMatchObject({
      restoredFromHistory: true,
      statusHistory: [],
    })
    expect(messages.value).toHaveLength(2)
    expect(messages.value.find(message => message.role === 'maintenance')).toMatchObject({
      maintenance: {
        compactionId: 'cmp-history',
        historyArchived: true,
        canonicalComplete: true,
      },
    })
  })

  it('interleaves cold same-turn output when the steer crosses a page boundary', async () => {
    const { api, rpc, messages } = makeHistory(false)
    rpc.call
      .mockResolvedValueOnce({
        messages: [{
          id: 'assistant-1',
          message_id: 'assistant-1',
          role: 'assistant',
          text: '前😀后续',
          timestamp: '2026-07-06T00:00:02Z',
          turn_context: { turn_id: 'turn-1' },
          usage: {
            model_call_segments: [{
              model_call_id: '2.0',
              iteration: 2,
              start_codepoint: 2,
              end_codepoint: 4,
            }],
          },
        }],
        has_more: true,
        oldest_cursor: 'cursor-assistant',
        newest_cursor: 'cursor-assistant',
      })
      .mockResolvedValueOnce({
        messages: [
          {
            id: 'user-1',
            message_id: 'user-1',
            role: 'user',
            text: '原始问题',
            timestamp: '2026-07-06T00:00:00Z',
            turn_context: { turn_id: 'turn-1' },
          },
          {
            id: 'steer-1',
            message_id: 'steer-1',
            role: 'user',
            text: '请补充细节',
            timestamp: '2026-07-06T00:00:01Z',
            turn_context: {
              turn_id: 'turn-1',
              disposition: 'applied',
              revision: 2,
              model_call_id: '2.0',
              applied_iteration: 2,
            },
          },
        ],
        has_more: false,
        oldest_cursor: 'cursor-user',
        newest_cursor: 'cursor-steer',
      })

    await api.loadHistory()
    await api.loadEarlierHistory()

    expect(messages.value.map(message => [message.role, message.text])).toEqual([
      ['user', '原始问题'],
      ['assistant', '前😀'],
      ['user', '请补充细节'],
      ['assistant', '后续'],
    ])
    expect(messages.value[2]).toMatchObject({
      messageId: 'steer-1',
      inputDisposition: 'applied',
      steerModelCallId: '2.0',
      steerAppliedIteration: 2,
    })
    expect(messages.value[3]?.messageId).toBe('assistant-1')
  })

  it('restores a promoted steer under its new turn instead of the completed target turn', async () => {
    const { api, messages } = makeHistory(false, {
      response: {
        // Persistence keeps the steer row's original receive sequence. The
        // history projection must move it behind the completed old turn and
        // ahead of output belonging to its promoted follow-up.
        messages: [
          {
            id: 'user-old',
            message_id: 'user-old',
            role: 'user',
            text: 'original request',
            timestamp: '2026-07-06T00:00:00Z',
            turn_context: { turn_id: 'turn-old' },
          },
          {
            id: 'steer-1',
            message_id: 'steer-1',
            role: 'user',
            text: 'use the new constraint',
            timestamp: '2026-07-06T00:00:01Z',
            turn_context: {
              turn_id: 'turn-new',
              target_turn_id: 'turn-old',
              promoted_turn_id: 'turn-new',
              promoted_from_turn_id: 'turn-old',
              disposition: 'promoted',
              revision: 2,
            },
          },
          {
            id: 'assistant-old',
            message_id: 'assistant-old',
            role: 'assistant',
            text: 'completed old-turn output',
            timestamp: '2026-07-06T00:00:02Z',
            turn_context: { turn_id: 'turn-old' },
          },
          {
            id: 'assistant-new',
            message_id: 'assistant-new',
            role: 'assistant',
            text: 'promoted follow-up output',
            timestamp: '2026-07-06T00:00:03Z',
            turn_context: { turn_id: 'turn-new' },
          },
        ],
        has_more: false,
      },
    })

    await api.loadHistory()

    expect(messages.value.map(message => message.messageId)).toEqual([
      'user-old',
      'assistant-old',
      'steer-1',
      'assistant-new',
    ])
    expect(messages.value[2]).toMatchObject({
      turnId: 'turn-new',
      promotedFromTurnId: 'turn-old',
      inputDisposition: 'promoted',
      inputDispositionRevision: 2,
    })
  })

  it('re-homes a promoted steer when its completed turn crosses a page boundary', async () => {
    const { api, rpc, messages } = makeHistory(false)
    rpc.call
      .mockResolvedValueOnce({
        messages: [
          {
            id: 'assistant-old',
            message_id: 'assistant-old',
            role: 'assistant',
            text: 'completed old-turn output',
            timestamp: '2026-07-06T00:00:02Z',
            turn_context: { turn_id: 'turn-old' },
          },
          {
            id: 'assistant-new',
            message_id: 'assistant-new',
            role: 'assistant',
            text: 'promoted follow-up output',
            timestamp: '2026-07-06T00:00:03Z',
            turn_context: { turn_id: 'turn-new' },
          },
        ],
        has_more: true,
        oldest_cursor: 'cursor-assistant-old',
        newest_cursor: 'cursor-assistant-new',
      })
      .mockResolvedValueOnce({
        messages: [
          {
            id: 'user-old',
            message_id: 'user-old',
            role: 'user',
            text: 'original request',
            timestamp: '2026-07-06T00:00:00Z',
            turn_context: { turn_id: 'turn-old' },
          },
          {
            id: 'steer-1',
            message_id: 'steer-1',
            role: 'user',
            text: 'use the new constraint',
            timestamp: '2026-07-06T00:00:01Z',
            turn_context: {
              turn_id: 'turn-new',
              promoted_from_turn_id: 'turn-old',
              disposition: 'promoted',
              revision: 2,
            },
          },
        ],
        has_more: false,
        oldest_cursor: 'cursor-user-old',
        newest_cursor: 'cursor-steer',
      })

    await api.loadHistory()
    await api.loadEarlierHistory()

    expect(messages.value.map(message => message.messageId)).toEqual([
      'user-old',
      'assistant-old',
      'steer-1',
      'assistant-new',
    ])
  })

  it('restores immutable plan revisions from typed transcript segments', async () => {
    const { api, messages } = makeHistory(false, {
      response: {
        messages: [{
          id: 'assistant-plan',
          message_id: 'assistant-plan',
          role: 'assistant',
          text: 'Legacy Markdown fallback',
          timestamp: '2026-07-06T00:00:00Z',
          tool_calls: [{
            type: 'plan',
            snapshot: {
              revisionId: 'revision-2',
              planId: 'plan-1',
              title: 'Ship plan mode',
              markdown: 'A complete plan.',
              steps: [{ stepId: 'inspect', title: 'Inspect' }],
              current: true,
            },
          }],
        }],
        has_more: false,
      },
    })

    await api.loadHistory()

    expect(messages.value[0]?.planRevisions).toEqual([
      expect.objectContaining({
        revisionId: 'revision-2',
        planId: 'plan-1',
        title: 'Ship plan mode',
      }),
    ])
  })

  it('prepends one page per cursor and preserves the reader scroll anchor', async () => {
    const thread = document.createElement('div')
    let height = 400
    const earlySummary = {
      id: 21,
      compaction_id: 'cmp-early',
      trigger_reason: 'manual',
      created_at: Date.parse('2026-07-06T00:00:01.500Z'),
    }
    const lateSummary = {
      id: 22,
      compaction_id: 'cmp-late',
      trigger_reason: 'manual',
      created_at: Date.parse('2026-07-06T00:00:03.500Z'),
    }
    Object.defineProperties(thread, {
      scrollHeight: { configurable: true, get: () => height },
      scrollTop: { configurable: true, value: 120, writable: true },
    })
    const threadRef = ref<HTMLElement | null>(thread)
    const { api, rpc, messages } = makeHistory(false, {
      threadRef,
      response: {
        messages: [historyMessage('m3'), historyMessage('m4')],
        compaction_summaries: [lateSummary],
        has_more: true,
        oldest_cursor: 'cursor-3',
        newest_cursor: 'cursor-4',
        canonical_complete: true,
      },
    })
    const anchor = document.createElement('article')
    anchor.dataset.messageId = 'm3'
    thread.append(anchor)
    document.body.append(thread)
    thread.getBoundingClientRect = () => ({ top: 0, bottom: 500 } as DOMRect)
    anchor.getBoundingClientRect = () => {
      const canonicalCount = messages.value.filter(message => message.role !== 'maintenance').length
      const top = canonicalCount > 2 ? 300 : 100
      return { top, bottom: top + 60 } as DOMRect
    }
    rpc.call.mockImplementationOnce(async () => ({
      messages: [historyMessage('m3'), historyMessage('m4')],
      compaction_summaries: [lateSummary],
      has_more: true,
      oldest_cursor: 'cursor-3',
      newest_cursor: 'cursor-4',
      canonical_complete: true,
    })).mockImplementationOnce(async () => {
      // Simulate unrelated live-tail growth while the page request is in
      // flight. The visible durable message still moves by exactly 200px.
      height = 900
      return {
        messages: [historyMessage('m1'), historyMessage('m2')],
        compaction_summaries: [earlySummary, lateSummary],
        has_more: false,
        oldest_cursor: 'cursor-1',
        newest_cursor: 'cursor-2',
        canonical_complete: true,
      }
    })

    await api.loadHistory()
    await api.loadEarlierHistory()
    await nextTick()
    await api.loadEarlierHistory()

    expect(messages.value.map(message => message.messageId)).toEqual([
      'm1',
      'maintenance:context-compaction:summary:21',
      'm2',
      'm3',
      'maintenance:context-compaction:summary:22',
      'm4',
    ])
    expect(messages.value
      .filter(message => message.role !== 'maintenance')
      .map(message => message.messageId)).toEqual(['m1', 'm2', 'm3', 'm4'])
    expect(thread.scrollTop).toBe(320)
    expect(rpc.call).toHaveBeenCalledTimes(2)
    expect(api.historyState.value.canonicalComplete).toBe(true)
    expect(api.historyState.value.newestCursor).toBe('cursor-4')
    thread.remove()
  })

  it('queues a threshold crossing during latest-window refresh without consuming its cursor', async () => {
    let resolveRefresh!: (value: ChatHistoryResponse) => void
    const refresh = new Promise<ChatHistoryResponse>(resolve => { resolveRefresh = resolve })
    const { api, rpc, messages } = makeHistory(false)
    rpc.call
      .mockResolvedValueOnce({
        messages: [historyMessage('m4')],
        has_more: true,
        oldest_cursor: 'cursor-4',
        newest_cursor: 'cursor-4',
        canonical_available: true,
      })
      .mockResolvedValueOnce({
        messages: [historyMessage('m3')],
        has_more: true,
        oldest_cursor: 'cursor-3',
        newest_cursor: 'cursor-3',
        canonical_available: true,
      })
      .mockImplementationOnce(() => refresh)
      .mockResolvedValueOnce({
        messages: [historyMessage('m2')],
        has_more: false,
        oldest_cursor: 'cursor-2',
        newest_cursor: 'cursor-2',
        canonical_available: true,
      })

    await api.loadHistory()
    await api.loadEarlierHistory()
    const refreshing = api.loadHistory()
    await vi.waitFor(() => expect(api.historyState.value.loading).toBe(true))

    api.loadEarlierHistory()
    resolveRefresh({
      messages: [historyMessage('m4'), historyMessage('m5')],
      has_more: true,
      oldest_cursor: 'cursor-4',
      newest_cursor: 'cursor-5',
      canonical_available: true,
    })
    await refreshing
    await vi.waitFor(() => expect(rpc.call).toHaveBeenCalledTimes(4))

    expect(rpc.call).toHaveBeenNthCalledWith(4, 'chat.history', expect.objectContaining({
      before: 'cursor-3',
    }), expect.objectContaining({ timeoutAction: 'reject' }))
    await vi.waitFor(() => {
      expect(messages.value.map(message => message.messageId)).toEqual(['m2', 'm3', 'm4', 'm5'])
    })
  })

  it('does not apply an unavailable fallback page and retries the exact prepend boundary', async () => {
    const { api, rpc, messages } = makeHistory(false)
    rpc.call
      .mockResolvedValueOnce({
        messages: [historyMessage('m4')],
        has_more: true,
        oldest_cursor: 'cursor-4',
        newest_cursor: 'cursor-4',
        canonical_available: true,
        canonical_complete: true,
      })
      .mockResolvedValueOnce({
        messages: [historyMessage('fallback')],
        has_more: false,
        oldest_cursor: 'fallback-cursor',
        newest_cursor: 'fallback-cursor',
        canonical_available: false,
        canonical_complete: false,
      })
      .mockResolvedValueOnce({
        messages: [historyMessage('m2'), historyMessage('m3')],
        has_more: false,
        oldest_cursor: 'cursor-2',
        newest_cursor: 'cursor-3',
        canonical_available: true,
        canonical_complete: true,
      })

    await api.loadHistory()
    await api.loadEarlierHistory()

    expect(messages.value.map(message => message.messageId)).toEqual(['m4'])
    expect(api.historyState.value).toMatchObject({
      hasMore: true,
      oldestCursor: 'cursor-4',
      newestCursor: 'cursor-4',
      canonicalAvailable: false,
    })

    await api.retryHistory()

    expect(messages.value.map(message => message.messageId)).toEqual(['m2', 'm3', 'm4'])
    expect(rpc.call).toHaveBeenNthCalledWith(3, 'chat.history', expect.objectContaining({
      before: 'cursor-4',
    }), expect.objectContaining({ timeoutAction: 'reject' }))
  })

  it('keeps more than 200 loaded canonical messages during a latest-window refresh', async () => {
    const loaded = Array.from({ length: 250 }, (_, index): ChatMessage => ({
      role: 'assistant',
      text: `old ${index}`,
      ts: `old-${index}`,
      messageId: `m-${index}`,
      restoredFromHistory: true,
    }))
    const latest = Array.from({ length: 200 }, (_, index) => historyMessage(`m-${index + 50}`))
    const { api, messages } = makeHistory(false, {
      messages: loaded,
      response: {
        messages: latest,
        has_more: true,
        oldest_cursor: 'cursor-50',
        newest_cursor: 'cursor-249',
      },
    })

    await api.loadHistory()

    expect(messages.value).toHaveLength(250)
    expect(messages.value.slice(0, 51).map(message => message.messageId)).toEqual([
      ...Array.from({ length: 50 }, (_, index) => `m-${index}`),
      'm-50',
    ])
  })

  it('bridges forward without dropping loaded pages when a refresh has no message-id overlap', async () => {
    const initial = Array.from({ length: 50 }, (_, index) => historyMessage(`m-${index + 250}`))
    const earlier = Array.from({ length: 50 }, (_, index) => historyMessage(`m-${index + 200}`))
    const latest = Array.from({ length: 199 }, (_, index) => historyMessage(`m-${index + 500}`))
    latest.push({
      id: 'live-user-server',
      message_id: 'live-user-server',
      role: 'user',
      text: 'still running',
      timestamp: '2026-07-06T01:00:00Z',
    })
    const { api, rpc, messages } = makeHistory(false, { preserveLiveTail: true })
    rpc.call
      .mockResolvedValueOnce({
        messages: initial,
        has_more: true,
        oldest_cursor: 'cursor-250',
        newest_cursor: 'cursor-299',
        canonical_available: true,
      })
      .mockResolvedValueOnce({
        messages: earlier,
        has_more: true,
        oldest_cursor: 'cursor-200',
        newest_cursor: 'cursor-249',
        canonical_available: true,
      })
      .mockResolvedValueOnce({
        messages: latest,
        has_more: true,
        oldest_cursor: 'cursor-500',
        newest_cursor: 'cursor-live',
        canonical_available: true,
      })
      .mockResolvedValueOnce({
        messages: Array.from({ length: 200 }, (_, index) => historyMessage(`m-${index + 300}`)),
        has_more: true,
        oldest_cursor: 'cursor-300',
        newest_cursor: 'cursor-499',
        canonical_available: true,
      })
      .mockResolvedValueOnce({
        messages: latest,
        has_more: false,
        oldest_cursor: 'cursor-500',
        newest_cursor: 'cursor-live',
        canonical_available: true,
      })
      .mockResolvedValueOnce({
        messages: [historyMessage('m-199')],
        has_more: true,
        oldest_cursor: 'cursor-199',
        newest_cursor: 'cursor-199',
        canonical_available: true,
      })

    await api.loadHistory()
    await api.loadEarlierHistory()
    messages.value.push(
      {
        role: 'user',
        text: 'still running',
        ts: '2026-07-06T01:00:00Z',
        messageId: 'live-user-server',
        turnId: 'turn-live',
      },
      {
        role: 'user',
        text: 'adjust while running',
        ts: '2026-07-06T01:00:01Z',
        clientId: 'local-steer',
        turnId: 'turn-live',
        inputDisposition: 'steering',
      },
    )

    await api.loadHistory()

    expect(messages.value[0].messageId).toBe('m-200')
    expect(messages.value.some(message => message.messageId === 'm-300')).toBe(true)
    expect(messages.value.some(message => message.messageId === 'm-500')).toBe(true)
    expect(messages.value[messages.value.length - 1]).toMatchObject({
      clientId: 'local-steer',
      inputDisposition: 'steering',
    })
    expect(api.historyState.value).toMatchObject({
      hasMore: true,
      oldestCursor: 'cursor-200',
      newestCursor: 'cursor-live',
    })
    expect(rpc.call).toHaveBeenNthCalledWith(4, 'chat.history', expect.objectContaining({
      after: 'cursor-299',
      limit: 200,
    }), expect.objectContaining({ timeoutAction: 'reject' }))
    expect(rpc.call).toHaveBeenNthCalledWith(5, 'chat.history', expect.objectContaining({
      after: 'cursor-499',
      limit: 200,
    }), expect.objectContaining({ timeoutAction: 'reject' }))

    await api.loadEarlierHistory()
    expect(rpc.call).toHaveBeenNthCalledWith(6, 'chat.history', expect.objectContaining({
      before: 'cursor-200',
    }), expect.objectContaining({ timeoutAction: 'reject' }))
  })

  it('bounds each disconnected forward bridge and resumes from the saved cursor', async () => {
    const { api, rpc, messages } = makeHistory(false)
    rpc.call
      .mockResolvedValueOnce({
        messages: [historyMessage('m8')],
        has_more: true,
        oldest_cursor: 'cursor-8',
        newest_cursor: 'cursor-8',
        canonical_available: true,
      })
      .mockResolvedValueOnce({
        messages: [historyMessage('m7')],
        has_more: true,
        oldest_cursor: 'cursor-7',
        newest_cursor: 'cursor-7',
        canonical_available: true,
      })
      .mockResolvedValueOnce({
        messages: [historyMessage('m20')],
        has_more: true,
        oldest_cursor: 'cursor-20',
        newest_cursor: 'cursor-20',
        canonical_available: true,
      })
      .mockResolvedValueOnce({
        messages: [historyMessage('m9'), historyMessage('m10')],
        has_more: true,
        oldest_cursor: 'cursor-9',
        newest_cursor: 'cursor-10',
        canonical_available: true,
      })
      .mockResolvedValueOnce({
        messages: [historyMessage('m11'), historyMessage('m12')],
        has_more: true,
        oldest_cursor: 'cursor-11',
        newest_cursor: 'cursor-12',
        canonical_available: true,
      })
      .mockResolvedValueOnce({
        messages: [historyMessage('m20')],
        has_more: true,
        oldest_cursor: 'cursor-20',
        newest_cursor: 'cursor-20',
        canonical_available: true,
      })
      .mockResolvedValueOnce({
        messages: Array.from({ length: 8 }, (_, index) => historyMessage(`m${index + 13}`)),
        has_more: false,
        oldest_cursor: 'cursor-13',
        newest_cursor: 'cursor-20',
        canonical_available: true,
      })

    await api.loadHistory()
    await api.loadEarlierHistory()
    await api.loadHistory()

    expect(rpc.call).toHaveBeenCalledTimes(5)
    expect(messages.value.map(message => message.messageId)).toEqual([
      'm7', 'm8', 'm9', 'm10', 'm11', 'm12',
    ])
    expect(api.historyState.value.newestCursor).toBe('cursor-12')

    await vi.waitFor(() => expect(rpc.call).toHaveBeenCalledTimes(7))
    expect(rpc.call).toHaveBeenNthCalledWith(7, 'chat.history', expect.objectContaining({
      after: 'cursor-12',
    }), expect.objectContaining({ timeoutAction: 'reject' }))
    expect(messages.value.map(message => message.messageId)).toEqual([
      'm7', 'm8', 'm9', 'm10', 'm11', 'm12', 'm13', 'm14', 'm15', 'm16',
      'm17', 'm18', 'm19', 'm20',
    ])
  })

  it('keeps expanded history untouched when a forward bridge is unavailable', async () => {
    const { api, rpc, messages } = makeHistory(false)
    rpc.call
      .mockResolvedValueOnce({
        messages: [historyMessage('m4')],
        has_more: true,
        oldest_cursor: 'cursor-4',
        newest_cursor: 'cursor-4',
        canonical_available: true,
      })
      .mockResolvedValueOnce({
        messages: [historyMessage('m3')],
        has_more: true,
        oldest_cursor: 'cursor-3',
        newest_cursor: 'cursor-3',
        canonical_available: true,
      })
      .mockResolvedValueOnce({
        messages: [historyMessage('m9')],
        has_more: true,
        oldest_cursor: 'cursor-9',
        newest_cursor: 'cursor-9',
        canonical_available: true,
      })
      .mockResolvedValueOnce({
        messages: [historyMessage('fallback')],
        has_more: false,
        oldest_cursor: 'fallback-cursor',
        newest_cursor: 'fallback-cursor',
        canonical_available: false,
        canonical_complete: false,
      })
      .mockResolvedValueOnce({
        messages: [historyMessage('m9')],
        has_more: true,
        oldest_cursor: 'cursor-9',
        newest_cursor: 'cursor-9',
        canonical_available: true,
      })
      .mockResolvedValueOnce({
        messages: Array.from({ length: 5 }, (_, index) => historyMessage(`m${index + 5}`)),
        has_more: false,
        oldest_cursor: 'cursor-5',
        newest_cursor: 'cursor-9',
        canonical_available: true,
      })

    await api.loadHistory()
    await api.loadEarlierHistory()
    await api.loadHistory()

    expect(messages.value.map(message => message.messageId)).toEqual(['m3', 'm4'])
    expect(api.historyState.value).toMatchObject({
      hasMore: true,
      oldestCursor: 'cursor-3',
      newestCursor: 'cursor-4',
      canonicalAvailable: false,
    })

    await api.retryHistory()

    expect(messages.value.map(message => message.messageId)).toEqual([
      'm3',
      'm4',
      'm5',
      'm6',
      'm7',
      'm8',
      'm9',
    ])
    expect(rpc.call).toHaveBeenNthCalledWith(6, 'chat.history', expect.objectContaining({
      after: 'cursor-4',
    }), expect.objectContaining({ timeoutAction: 'reject' }))
  })

  it('stops a forward bridge when its cursor does not advance', async () => {
    const { api, rpc, messages } = makeHistory(false)
    rpc.call
      .mockResolvedValueOnce({
        messages: [historyMessage('m4')],
        has_more: true,
        oldest_cursor: 'cursor-4',
        newest_cursor: 'cursor-4',
        canonical_available: true,
      })
      .mockResolvedValueOnce({
        messages: [historyMessage('m3')],
        has_more: true,
        oldest_cursor: 'cursor-3',
        newest_cursor: 'cursor-3',
        canonical_available: true,
      })
      .mockResolvedValueOnce({
        messages: [historyMessage('m9')],
        has_more: true,
        oldest_cursor: 'cursor-9',
        newest_cursor: 'cursor-9',
        canonical_available: true,
      })
      .mockResolvedValueOnce({
        messages: [historyMessage('m5')],
        has_more: true,
        oldest_cursor: 'cursor-5',
        newest_cursor: 'cursor-4',
        canonical_available: true,
      })

    await api.loadHistory()
    await api.loadEarlierHistory()
    await api.loadHistory()

    expect(rpc.call).toHaveBeenCalledTimes(4)
    expect(messages.value.map(message => message.messageId)).toEqual(['m3', 'm4'])
    expect(api.historyState.value).toMatchObject({
      oldestCursor: 'cursor-3',
      newestCursor: 'cursor-4',
      loadingEarlier: false,
      loadEarlierError: false,
      recoveryError: true,
    })
  })

  it('allows the same cursor to be retried after a failed earlier-page request', async () => {
    const { api, rpc } = makeHistory(false, {
      response: {
        messages: [historyMessage('m2')],
        has_more: true,
        oldest_cursor: 'cursor-2',
      },
    })
    rpc.call
      .mockResolvedValueOnce({
        messages: [historyMessage('m2')],
        has_more: true,
        oldest_cursor: 'cursor-2',
      })
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce({
        messages: [historyMessage('m1')],
        has_more: false,
        oldest_cursor: 'cursor-1',
      })

    await api.loadHistory()
    await api.loadEarlierHistory()
    expect(api.historyState.value.loadEarlierError).toBe(true)

    await api.loadEarlierHistory()
    expect(api.historyState.value.loadEarlierError).toBe(false)
    expect(rpc.call).toHaveBeenCalledTimes(3)
  })

  it('surfaces and retries an initial history request failure', async () => {
    const { api, rpc, messages } = makeHistory(false)
    rpc.call
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce({
        messages: [historyMessage('m1')],
        has_more: false,
        oldest_cursor: 'cursor-1',
        canonical_available: true,
      })

    await api.loadHistory()
    expect(api.historyState.value).toMatchObject({
      initialLoadStatus: 'error',
      loadEarlierError: false,
    })

    const retry = api.retryHistory()
    expect(api.historyState.value).toMatchObject({
      initialLoadStatus: 'loading',
    })
    await retry
    expect(api.historyState.value).toMatchObject({
      initialLoadStatus: 'ready',
      loadEarlierError: false,
    })
    expect(messages.value.map(message => message.messageId)).toEqual(['m1'])
  })

  it('keeps an initial failure retryable when a live row arrives first', async () => {
    const { api, rpc, messages } = makeHistory(false)
    let rejectHistory!: (reason: Error) => void
    rpc.call.mockReturnValueOnce(new Promise<ChatHistoryResponse>((_resolve, reject) => {
      rejectHistory = reject
    }))

    const load = api.loadHistory()
    messages.value.push({
      role: 'assistant',
      text: 'live row',
      ts: 'live',
      messageId: 'live-row',
    })
    rejectHistory(new Error('offline'))
    await load

    expect(api.historyState.value).toMatchObject({
      initialLoadStatus: 'error',
      loadEarlierError: false,
    })
    expect(messages.value.map(message => message.messageId)).toEqual(['live-row'])
  })

  it('retries the current canonical window when the canonical reader was unavailable', async () => {
    const { api, rpc, messages } = makeHistory(false)
    rpc.call
      .mockResolvedValueOnce({
        messages: [historyMessage('fallback')],
        has_more: false,
        oldest_cursor: null,
        canonical_available: false,
        canonical_complete: false,
      })
      .mockResolvedValueOnce({
        messages: [historyMessage('canonical')],
        has_more: false,
        oldest_cursor: null,
        canonical_available: true,
        canonical_complete: true,
      })

    await api.loadHistory()
    expect(api.historyState.value.canonicalAvailable).toBe(false)
    expect(api.historyState.value.loadingEarlier).toBe(false)
    expect(api.historyState.value.initialLoadStatus).toBe('ready')
    expect(messages.value.map(message => message.messageId)).toEqual(['fallback'])

    await api.retryHistory()
    expect(api.historyState.value.canonicalAvailable).toBe(true)
    expect(rpc.call).toHaveBeenCalledTimes(2)
  })

  it('marks an empty unavailable canonical reader as an initial retriable failure', async () => {
    const { api } = makeHistory(false, {
      response: {
        messages: [],
        has_more: false,
        oldest_cursor: null,
        canonical_available: false,
        canonical_complete: false,
      },
    })

    await api.loadHistory()

    expect(api.historyState.value).toMatchObject({
      initialLoadStatus: 'error',
      canonicalAvailable: false,
      canonicalComplete: false,
      loadEarlierError: false,
    })
  })

  it('settles a confirmed empty session without reporting an initial failure', async () => {
    const { api } = makeHistory(false, {
      response: {
        messages: [],
        has_more: false,
        oldest_cursor: null,
        canonical_available: false,
        canonical_complete: true,
      },
    })

    await api.loadHistory()

    expect(api.historyState.value).toMatchObject({
      initialLoadStatus: 'ready',
      canonicalAvailable: false,
      canonicalComplete: true,
    })
  })

  it('keeps an old-gateway empty success without canonical fields compatible', async () => {
    const { api } = makeHistory(false, {
      response: {
        messages: [],
        has_more: false,
        oldest_cursor: null,
      },
    })

    await api.loadHistory()

    expect(api.historyState.value).toMatchObject({
      initialLoadStatus: 'ready',
      canonicalAvailable: null,
      canonicalComplete: null,
      loadEarlierError: false,
    })
  })

  it('keeps a pre-canonical-complete unavailable empty response compatible', async () => {
    const { api } = makeHistory(false, {
      response: {
        messages: [],
        has_more: false,
        oldest_cursor: null,
        canonical_available: false,
      },
    })

    await api.loadHistory()

    expect(api.historyState.value).toMatchObject({
      initialLoadStatus: 'ready',
      canonicalAvailable: false,
      canonicalComplete: null,
      loadEarlierError: false,
    })
  })

  it('discards a stale response after switching sessions', async () => {
    const sessionKey = ref('agent:main:webchat:old')
    let resolveOld!: (value: ChatHistoryResponse) => void
    const oldResponse = new Promise<ChatHistoryResponse>(resolve => { resolveOld = resolve })
    const { api, rpc, messages } = makeHistory(false, {
      sessionKey,
      messages: [{
        role: 'assistant',
        text: 'old loaded row',
        ts: 'old',
        messageId: 'old-loaded',
        restoredFromHistory: true,
      }],
    })
    rpc.call
      .mockImplementationOnce(() => oldResponse)
      .mockResolvedValueOnce({
        messages: [historyMessage('new-message')],
        has_more: false,
        oldest_cursor: null,
      })

    const oldLoad = api.loadHistory()
    await vi.waitFor(() => expect(rpc.call).toHaveBeenCalledTimes(1))
    sessionKey.value = 'agent:main:webchat:new'
    const newLoad = api.loadHistory()
    await newLoad
    resolveOld({
      messages: [historyMessage('old-message')],
      has_more: false,
      oldest_cursor: null,
    })
    await oldLoad

    expect(messages.value.map(message => message.messageId)).toEqual(['new-message'])
    expect(api.historyState.value.loading).toBe(false)
    expect(api.historyState.value.initialLoadStatus).toBe('ready')
  })

  it('cancels a scheduled history sync before switching to another session or draft', async () => {
    vi.useFakeTimers()
    try {
      const sessionKey = ref('agent:main:webchat:old')
      const { api, rpc } = makeHistory(false, { sessionKey })

      api.scheduleHistorySync()
      api.cancelActiveHistory()
      sessionKey.value = 'agent:main:webchat:new-draft'
      await vi.advanceTimersByTimeAsync(50)

      expect(rpc.waitForConnection).not.toHaveBeenCalled()
      expect(rpc.call).not.toHaveBeenCalled()
    } finally {
      vi.useRealTimers()
    }
  })

  it('keeps the new session loading when a stale request fails first', async () => {
    const sessionKey = ref('agent:main:webchat:old')
    let rejectOld!: (reason: Error) => void
    let resolveNew!: (value: ChatHistoryResponse) => void
    const oldResponse = new Promise<ChatHistoryResponse>((_resolve, reject) => {
      rejectOld = reject
    })
    const newResponse = new Promise<ChatHistoryResponse>(resolve => {
      resolveNew = resolve
    })
    const { api, rpc, messages } = makeHistory(false, { sessionKey })
    rpc.call
      .mockImplementationOnce(() => oldResponse)
      .mockImplementationOnce(() => newResponse)

    const oldLoad = api.loadHistory()
    await vi.waitFor(() => expect(rpc.call).toHaveBeenCalledTimes(1))
    sessionKey.value = 'agent:main:webchat:new'
    const newLoad = api.loadHistory()
    await vi.waitFor(() => expect(rpc.call).toHaveBeenCalledTimes(2))

    rejectOld(new Error('stale offline response'))
    await oldLoad
    expect(messages.value).toEqual([])
    expect(api.historyState.value).toMatchObject({
      initialLoadStatus: 'loading',
      loading: true,
      loadEarlierError: false,
    })

    resolveNew({
      messages: [historyMessage('new-message')],
      has_more: false,
      oldest_cursor: null,
    })
    await newLoad
    expect(messages.value.map(message => message.messageId)).toEqual(['new-message'])
    expect(api.historyState.value).toMatchObject({
      initialLoadStatus: 'ready',
      loading: false,
    })
  })
})

describe('useChatHistory scroll anchoring', () => {
  it('does not force the thread to the latest message when the reader has scrolled up', async () => {
    const { api, scrollToBottom } = makeHistory(false)

    await api.loadHistory()
    await nextTick()

    expect(scrollToBottom).not.toHaveBeenCalled()
  })

  it('keeps the initial pinned load behavior when the thread is still at the bottom', async () => {
    const { api, scrollToBottom } = makeHistory(true)

    await api.loadHistory()
    await nextTick()

    expect(scrollToBottom).toHaveBeenCalledTimes(1)
  })

  it('keeps protocol-shaped assistant documentation canonical', async () => {
    const text = [
      'Document `<tool_calls>` inline.',
      '```xml',
      '<tool_calls><invoke name="demo"></invoke></tool_calls>',
      '```',
      'Keep `<｜DSML｜tool_calls>` too.',
      '<details><summary>View areas around line 10</summary>Visible note.</details>',
      'Final suffix.',
    ].join('\n')
    const { api, messages } = makeHistory(true, {
      response: {
        messages: [{
          id: 'literal-1',
          message_id: 'literal-1',
          role: 'assistant',
          text,
          timestamp: '2026-07-06T00:00:00Z',
        }],
        has_more: false,
        oldest_cursor: null,
        newest_cursor: null,
        history_scope: 'session',
      },
    })

    await api.loadHistory()

    expect(messages.value[0]?.text).toBe(text)
  })
})

describe('useChatHistory optimistic local rows', () => {
  it('does not erase local user text when an immediate history sync is still empty', async () => {
    const localMessages: ChatMessage[] = [
      { role: 'user', text: '上下文相关SOTA论文', ts: '2026-07-07T10:00:00Z' },
    ]
    const { api, messages } = makeHistory(true, {
      messages: localMessages,
      response: {
        messages: [],
        has_more: false,
        oldest_cursor: null,
        newest_cursor: null,
        history_scope: 'session',
      },
    })

    await api.loadHistory()

    expect(messages.value).toEqual(localMessages)
  })

  it('drops a legacy synthetic stop bubble and uses the typed turn outcome', async () => {
    const { api, messages } = makeHistory(true, {
      messages: [
        { role: 'user', text: 'stop immediately', ts: '2026-07-07T10:00:00Z', messageId: 'user-1' },
        {
          role: 'assistant',
          text: 'Stopped after 1s',
          ts: '2026-07-07T10:00:01Z',
          messageId: 'client-stop-notice:task-1',
          stopNotice: true,
          interrupted: true,
        },
      ],
      response: {
        messages: [
          {
            id: 'user-1',
            message_id: 'user-1',
            role: 'user',
            text: 'stop immediately',
            timestamp: '2026-07-07T10:00:00Z',
            turn_context: { turn_id: 'turn-1' },
          },
        ],
        turn_outcomes: [{
          turn_id: 'turn-1',
          task_id: 'task-1',
          status: 'cancelled',
          started_at: 1_000,
          finished_at: 2_000,
          accepted_routing_mode: 'ensemble',
          outcome: {
            kind: 'cancelled',
            cancellation_source: 'webui_stop',
          },
        }],
        has_more: false,
        oldest_cursor: null,
        newest_cursor: null,
        history_scope: 'session',
      },
    })

    await api.loadHistory()

    expect(messages.value.map(message => [message.role, message.text])).toEqual([
      ['user', 'stop immediately'],
    ])
    expect(messages.value[0]?.turnOutcome).toMatchObject({
      turnId: 'turn-1',
      status: 'cancelled',
      cancellationSource: 'webui_stop',
      acceptedRoutingMode: 'ensemble',
    })
  })

  it('restores usage barrier activity and its retryable error from terminal history', async () => {
    const { api, messages } = makeHistory(true, {
      response: {
        messages: [
          {
            id: 'user-usage',
            message_id: 'user-usage',
            role: 'user',
            text: 'retry this turn',
            timestamp: '2026-07-07T10:00:00Z',
            turn_context: { turn_id: 'turn-usage' },
          },
          {
            id: 'system-usage',
            message_id: 'system-usage',
            role: 'system',
            text: 'Error: usage ledger unavailable',
            timestamp: '2026-07-07T10:00:01Z',
            turn_context: { turn_id: 'turn-usage' },
          },
        ],
        turn_outcomes: [{
          turn_id: 'turn-usage',
          task_id: 'turn-usage',
          status: 'failed',
          error_class: 'usage_accounting_busy',
          retryable: true,
          usage_call_index: 1,
          no_prior_provider_dispatch: true,
          replay_safe: true,
          user_message_id: 'user-usage',
          terminal_message: 'server fallback',
          activity_snapshot: {
            version: 1,
            task_id: 'turn-usage',
            turn_id: 'turn-usage',
            phases: [
              { kind: 'router', phase: 'decided', at: 1_000 },
              { kind: 'state', phase: 'thinking', at: 1_100 },
            ],
          },
          outcome: {
            kind: 'blocked',
            reason: 'usage_accounting_busy',
            error_class: 'usage_accounting_busy',
            retryable: true,
            usage_call_index: 1,
            no_prior_provider_dispatch: true,
            replay_safe: true,
            user_message_id: 'user-usage',
          },
        }],
        has_more: false,
        oldest_cursor: null,
        newest_cursor: null,
        history_scope: 'session',
      },
    })

    await api.loadHistory()

    expect(messages.value.map(message => message.role)).toEqual(['user', 'assistant', 'error'])
    expect(messages.value[1]).toMatchObject({
      turnId: 'turn-usage',
      text: '',
      statusHistory: [
        expect.objectContaining({ action: 'router:decided', at: 1_000 }),
        expect.objectContaining({ action: 'Planning next step', at: 1_100 }),
      ],
    })
    expect(messages.value[2]).toMatchObject({
      role: 'error',
      errorCode: 'usage_accounting_busy',
      terminalNotice: true,
      text: 'The provider request was not sent and no usage was billed. You can safely retry this turn.',
      turnOutcome: expect.objectContaining({ userMessageId: 'user-usage' }),
    })
  })

  it('accepts a complete v2 atomically and rejects transcript drift without partial mixing', async () => {
    const assistantMessage = {
      id: 'assistant-activity-v2',
      message_id: 'assistant-activity-v2',
      role: 'assistant' as const,
      text: 'Final answer.',
      reasoning_content: ' A😀 ',
      timestamp: '2026-07-07T10:00:01Z',
      turn_context: { turn_id: 'turn-activity-v2' },
      tool_calls: [
        { type: 'text', text: 'Inspect.' },
        { type: 'tool_use', tool_use_id: 'tool-1', name: 'skill_view', input: {} },
        { type: 'tool_result', tool_use_id: 'tool-1', name: 'skill_view', result: 'ok' },
        { type: 'text', text: 'Final answer.' },
      ],
    }
    const entries = [
      {
        type: 'phase', id: 'provider:requesting:4', order: 4,
        kind: 'provider', phase: 'requesting', at: 4_000, ended_at: 5_000,
      },
      {
        type: 'reasoning', id: 'reasoning-1', order: 6, block_index: 0,
        started_at: 6_000, ended_at: 8_000, status: 'completed',
        content_kind: 'reasoning', text_start_utf16: 0, text_end_utf16: 5,
      },
      {
        type: 'segment', id: 'text:0', order: 31, segment_type: 'text',
        text_index: 0, text_utf16_length: 8, at: 31_000, ended_at: 32_000,
      },
      {
        type: 'segment', id: 'tool:tool-1', order: 41, segment_type: 'tool',
        tool_use_id: 'tool-1', name: 'skill_view', started_at: 41_000,
        ended_at: 42_000, is_error: false,
      },
      {
        type: 'segment', id: 'text:1', order: 50, segment_type: 'text',
        text_index: 1, text_utf16_length: 13, at: 50_000, ended_at: 51_000,
      },
    ]
    const outcome = {
      turn_id: 'turn-activity-v2',
      task_id: 'turn-activity-v2',
      status: 'succeeded',
      activity_snapshot: {
        version: 2,
        task_id: 'turn-activity-v2',
        turn_id: 'turn-activity-v2',
        complete: true,
        reasoning_utf16_length: 5,
        entries,
      },
    }
    const complete = makeHistory(false, {
      response: {
        messages: [assistantMessage],
        turn_outcomes: [outcome],
        has_more: false,
      },
    })

    await complete.api.loadHistory()

    expect(complete.messages.value[0]).toMatchObject({
      activitySnapshot: { version: 2, complete: true },
      activitySnapshotIncomplete: false,
      statusHistory: [{ action: 'provider:requesting', activityOrder: 4 }],
      reasoningBlocks: [{ id: 'reasoning-1', text: ' A😀 ', activityOrder: 6 }],
    })

    const corrupted = makeHistory(false, {
      response: {
        messages: [assistantMessage],
        turn_outcomes: [{
          ...outcome,
          activity_snapshot: {
            ...outcome.activity_snapshot,
            entries: entries.map(entry => entry.id === 'text:1'
              ? { ...entry, text_utf16_length: 12 }
              : entry),
          },
        }],
        has_more: false,
      },
    })

    await corrupted.api.loadHistory()

    expect(corrupted.messages.value[0]).toMatchObject({
      activitySnapshot: { version: 2, complete: false },
      activitySnapshotIncomplete: true,
    })
    expect(corrupted.messages.value[0]?.statusHistory).toEqual([])
    expect(corrupted.messages.value[0]?.reasoningBlocks).toBeUndefined()
    expect(corrupted.messages.value[0]?.tool_calls).toHaveLength(4)
  })

  it.each([
    [
      'image_input_unsupported',
      'The selected model cannot process image input. Choose an image-capable model or remove the image.',
    ],
    [
      'ensemble_multimodal_unsupported',
      "Ensemble doesn't support image input yet. Under Model routing, choose AI-powered single-model router with an image-capable tier configured, or turn routing Off and select an image-capable model.",
    ],
  ])('restores %s as a localized error card', async (errorClass, expectedText) => {
    const { api, messages } = makeHistory(true, {
      response: {
        messages: [
          {
            id: 'user-image',
            message_id: 'user-image',
            role: 'user',
            text: 'inspect this image',
            timestamp: '2026-07-07T10:00:00Z',
            turn_context: { turn_id: 'turn-image' },
          },
          {
            id: 'system-image',
            message_id: 'system-image',
            role: 'system',
            text: 'Error: server fallback [synthetic ref]',
            timestamp: '2026-07-07T10:00:01Z',
            turn_context: { turn_id: 'turn-image' },
          },
        ],
        turn_outcomes: [{
          turn_id: 'turn-image',
          task_id: 'turn-image',
          status: 'failed',
          error_class: errorClass,
          retryable: false,
          terminal_message: 'server fallback',
        }],
        has_more: false,
        oldest_cursor: null,
        newest_cursor: null,
        history_scope: 'session',
      },
    })

    await api.loadHistory()

    expect(messages.value.map(message => message.role)).toEqual(['user', 'error'])
    expect(messages.value[1]).toMatchObject({
      errorCode: errorClass,
      terminalNotice: true,
      text: expectedText,
    })
  })

  it('restores a usage barrier retry card when the transcript error row is absent', async () => {
    const { api, messages } = makeHistory(true, {
      response: {
        messages: [{
          id: 'user-usage',
          message_id: 'user-usage',
          role: 'user',
          text: 'retry this turn',
          timestamp: '2026-07-07T10:00:00Z',
          turn_context: { turn_id: 'turn-usage' },
        }],
        turn_outcomes: [{
          turn_id: 'turn-usage',
          task_id: 'task-usage',
          status: 'failed',
          finished_at: 2_000,
          error_class: 'usage_accounting_unavailable',
          retryable: true,
          usage_call_index: 1,
          no_prior_provider_dispatch: true,
          replay_safe: true,
          user_message_id: 'user-usage',
          terminal_message: 'server fallback',
          activity_snapshot: {
            version: 1,
            task_id: 'turn-usage',
            turn_id: 'turn-usage',
            phases: [{ kind: 'router', phase: 'decided', at: 1_000 }],
          },
        }],
        has_more: false,
        oldest_cursor: null,
        newest_cursor: null,
        history_scope: 'session',
      },
    })

    await api.loadHistory()

    expect(messages.value.map(message => message.role)).toEqual(['user', 'assistant', 'error'])
    expect(messages.value[2]).toMatchObject({
      messageId: 'terminal-error:task-usage',
      turnId: 'turn-usage',
      errorCode: 'usage_accounting_unavailable',
      terminalNotice: true,
      restoredFromHistory: true,
      text: 'The provider request was not sent and no usage was billed. You can safely retry this turn.',
      turnOutcome: expect.objectContaining({
        turnId: 'turn-usage',
        userMessageId: 'user-usage',
      }),
    })
  })

  it('restores a usage barrier retry card without an activity snapshot', async () => {
    const { api, messages } = makeHistory(true, {
      response: {
        messages: [{
          id: 'user-usage',
          message_id: 'user-usage',
          role: 'user',
          text: 'retry this turn',
          timestamp: '2026-07-07T10:00:00Z',
          turn_context: { turn_id: 'turn-usage' },
        }],
        turn_outcomes: [{
          turn_id: 'turn-usage',
          task_id: 'task-usage',
          status: 'failed',
          error_class: 'usage_accounting_busy',
          retryable: true,
          usage_call_index: 1,
          no_prior_provider_dispatch: true,
          replay_safe: true,
        }],
        has_more: false,
        oldest_cursor: null,
        newest_cursor: null,
        history_scope: 'session',
      },
    })

    await api.loadHistory()

    expect(messages.value.map(message => message.role)).toEqual(['user', 'error'])
    expect(messages.value[1]).toMatchObject({
      messageId: 'terminal-error:task-usage',
      turnId: 'turn-usage',
      errorCode: 'usage_accounting_busy',
      terminalNotice: true,
      restoredFromHistory: true,
    })
  })

  it('restores a later-call usage barrier without claiming replay is safe', async () => {
    const { api, messages } = makeHistory(true, {
      response: {
        messages: [{
          id: 'user-usage',
          message_id: 'user-usage',
          role: 'user',
          text: 'continue after tools',
          timestamp: '2026-07-07T10:00:00Z',
          turn_context: { turn_id: 'turn-usage' },
        }],
        turn_outcomes: [{
          turn_id: 'turn-usage',
          task_id: 'task-usage',
          status: 'failed',
          error_class: 'usage_accounting_busy',
          retryable: true,
          usage_call_index: 2,
          no_prior_provider_dispatch: false,
          replay_safe: false,
          outcome: {
            kind: 'blocked',
            reason: 'usage_accounting_busy',
            error_class: 'usage_accounting_busy',
            retryable: true,
            usage_call_index: 2,
            no_prior_provider_dispatch: false,
            replay_safe: false,
          },
        }],
        has_more: false,
        oldest_cursor: null,
        newest_cursor: null,
        history_scope: 'session',
      },
    })

    await api.loadHistory()

    expect(messages.value.map(message => message.role)).toEqual(['user', 'error'])
    expect(messages.value[1]).toMatchObject({
      errorCode: 'usage_accounting_busy',
      text: 'This provider request was not sent. Earlier work in this turn may already have run or been billed, so review it before trying again.',
      turnOutcome: {
        usageCallIndex: 2,
        noPriorProviderDispatch: false,
        replaySafe: false,
        retryable: true,
      },
    })
  })

  it('prefers a durable usage barrier row when the turn crosses a page boundary', async () => {
    const { api, rpc, messages } = makeHistory(true)
    const outcome = {
      turn_id: 'turn-usage',
      task_id: 'task-usage',
      status: 'failed',
      error_class: 'usage_accounting_busy',
      retryable: true,
      usage_call_index: 1,
      no_prior_provider_dispatch: true,
      replay_safe: true,
    }
    rpc.call
      .mockResolvedValueOnce({
        messages: [{
          id: 'system-usage',
          message_id: 'system-usage',
          role: 'system',
          text: 'Error: usage ledger busy',
          timestamp: '2026-07-07T10:00:01Z',
          turn_context: { turn_id: 'turn-usage' },
        }],
        turn_outcomes: [outcome],
        has_more: true,
        oldest_cursor: 'cursor-system',
        newest_cursor: 'cursor-system',
        history_scope: 'session',
      })
      .mockResolvedValueOnce({
        messages: [{
          id: 'user-usage',
          message_id: 'user-usage',
          role: 'user',
          text: 'retry this turn',
          timestamp: '2026-07-07T10:00:00Z',
          turn_context: { turn_id: 'turn-usage' },
        }],
        turn_outcomes: [outcome],
        has_more: false,
        oldest_cursor: 'cursor-user',
        newest_cursor: 'cursor-user',
        history_scope: 'session',
      })

    await api.loadHistory()
    await api.loadEarlierHistory()

    expect(messages.value.map(message => message.messageId)).toEqual([
      'user-usage',
      'system-usage',
    ])
    expect(messages.value.filter(message => message.role === 'error')).toHaveLength(1)
  })

  it('keeps exact-turn optimistic usage activity through repeated history catch-up', async () => {
    const pendingResponse: ChatHistoryResponse = {
      messages: [{
        id: 'user-usage',
        message_id: 'user-usage',
        role: 'user',
        text: 'retry this turn',
        timestamp: '2026-07-07T10:00:00Z',
        turn_context: { turn_id: 'turn-usage' },
      }],
      has_more: false,
      oldest_cursor: null,
      newest_cursor: null,
      history_scope: 'session',
    }
    const { api, rpc, messages } = makeHistory(true, {
      messages: [
        {
          role: 'user',
          text: 'retry this turn',
          ts: 'local-user',
          messageId: 'user-usage',
          turnId: 'turn-usage',
        },
        {
          role: 'assistant',
          text: '',
          ts: 'local-activity',
          turnId: 'turn-usage',
          statusHistory: [{ action: 'router:decided', label: 'Route selected', at: 1_000 }],
        },
        {
          role: 'error',
          text: 'The provider request was not sent.',
          ts: 'local-error',
          turnId: 'turn-usage',
          errorCode: 'usage_accounting_busy',
          terminalNotice: true,
        },
      ],
      response: pendingResponse,
    })

    await api.loadHistory()
    await api.loadHistory()

    expect(messages.value.map(message => message.role)).toEqual(['user', 'assistant', 'error'])
    expect(messages.value[1]).toMatchObject({
      turnId: 'turn-usage',
      statusHistory: [expect.objectContaining({ action: 'router:decided', at: 1_000 })],
    })
    expect(messages.value[1]?.messageId).toBeUndefined()
    expect(messages.value[2]).toMatchObject({
      turnId: 'turn-usage',
      errorCode: 'usage_accounting_busy',
      terminalNotice: true,
    })

    rpc.call.mockResolvedValueOnce({
      ...pendingResponse,
      messages: [
        ...(pendingResponse.messages || []),
        {
          id: 'system-usage',
          message_id: 'system-usage',
          role: 'system',
          text: 'Error: usage ledger unavailable',
          timestamp: '2026-07-07T10:00:01Z',
          turn_context: { turn_id: 'turn-usage' },
        },
      ],
      turn_outcomes: [{
        turn_id: 'turn-usage',
        task_id: 'turn-usage',
        status: 'failed',
        error_class: 'usage_accounting_busy',
        retryable: true,
        usage_call_index: 1,
        no_prior_provider_dispatch: true,
        replay_safe: true,
        activity_snapshot: {
          version: 1,
          task_id: 'turn-usage',
          turn_id: 'turn-usage',
          phases: [{ kind: 'router', phase: 'decided', at: 1_000 }],
        },
      }],
    })
    await api.loadHistory()

    expect(messages.value.map(message => message.role)).toEqual(['user', 'assistant', 'error'])
    expect(messages.value[1]?.messageId).toBe('terminal-activity:turn-usage')
    expect(messages.value.filter(message => message.role === 'assistant')).toHaveLength(1)
  })

  it('keeps a terminal replay error until server history contains a durable error row', async () => {
    const { api, messages } = makeHistory(true, {
      messages: [
        { role: 'user', text: 'retry this turn', ts: 'local-user' },
        {
          role: 'error',
          text: 'Activation failed; retry this message.',
          ts: 'local-error',
          errorCode: 'failed',
          terminalNotice: true,
        },
      ],
      response: {
        messages: [
          {
            id: 'server-user',
            message_id: 'server-user',
            role: 'user',
            text: 'retry this turn',
            timestamp: 'server-user',
          },
        ],
        has_more: false,
        oldest_cursor: null,
        newest_cursor: null,
        history_scope: 'session',
      },
    })

    await api.loadHistory()

    expect(messages.value.map(message => [message.role, message.text])).toEqual([
      ['user', 'retry this turn'],
      ['error', 'Activation failed; retry this message.'],
    ])
    expect(messages.value[1]).toMatchObject({
      errorCode: 'failed',
      terminalNotice: true,
    })
  })

  it('does not infer interruption bubbles from adjacent repeated user messages', async () => {
    const prompt = '调研一下上下文相关的sota论文'
    const { api, messages } = makeHistory(true, {
      messages: [
        { role: 'user', text: prompt, ts: 'local-1' },
        {
          role: 'assistant',
          text: '输出被中断',
          ts: 'local-stop-1',
          messageId: 'client-stop-notice:task-1',
          stopNotice: true,
          interrupted: true,
        },
        { role: 'user', text: prompt, ts: 'local-2' },
        {
          role: 'assistant',
          text: '输出被中断',
          ts: 'local-stop-2',
          messageId: 'client-stop-notice:task-2',
          stopNotice: true,
          interrupted: true,
        },
        { role: 'user', text: prompt, ts: 'local-3' },
        {
          role: 'assistant',
          text: '输出被中断',
          ts: 'local-stop-3',
          messageId: 'client-stop-notice:task-3',
          stopNotice: true,
          interrupted: true,
        },
      ],
      response: {
        messages: [
          {
            id: 'server-user-1',
            message_id: 'server-user-1',
            role: 'user',
            text: prompt,
            timestamp: 'server-1',
          },
          {
            id: 'server-user-2',
            message_id: 'server-user-2',
            role: 'user',
            text: prompt,
            timestamp: 'server-2',
          },
          {
            id: 'server-user-3',
            message_id: 'server-user-3',
            role: 'user',
            text: prompt,
            timestamp: 'server-3',
          },
        ],
        has_more: false,
        oldest_cursor: null,
        newest_cursor: null,
        history_scope: 'session',
      },
    })

    await api.loadHistory()

    expect(messages.value.map(message => [message.role, message.text])).toEqual([
      ['user', prompt],
      ['user', prompt],
      ['user', prompt],
    ])
    expect(messages.value.some(message => message.stopNotice)).toBe(false)
  })
})

describe('useChatHistory accepted ensemble reconciliation', () => {
  function acceptedRouter(turnId: string | undefined): ChatMessage {
    return {
      role: 'router',
      text: '',
      ts: '2026-07-07T10:00:00.500Z',
      ...(turnId ? { turnId } : {}),
      messageId: 'router-live',
      provenanceKind: 'router_decision',
      routerDecision: {
        tier: 'c1',
        model: 'anthropic/claude-sonnet-4.6',
        source: 'squilla_router',
        accepted_routing_mode: 'ensemble',
      },
      ensemble: {
        profile: 'llm_ensemble',
        modelCount: 1,
        totalCandidates: 1,
        requestCount: 1,
        fallbackUsed: false,
        fallbackReason: '',
        costUsd: 0,
        savedUsd: 0,
        savedPct: 0,
        models: [{
          role: 'proposer_1',
          label: 'proposer_1',
          provider: 'anthropic',
          model: 'claude-sonnet-4.6',
          modelShort: 'claude-sonnet-4.6',
          input: 10,
          output: 20,
          costUsd: 0,
          status: 'done',
        }],
      },
    }
  }

  const canonicalTurn = (turnId = 'turn-current'): ChatHistoryResponse => ({
    messages: [
      {
        id: `user-${turnId}`,
        message_id: `user-${turnId}`,
        role: 'user',
        text: `question ${turnId}`,
        timestamp: '2026-07-07T10:00:00Z',
        turn_context: { turn_id: turnId },
      },
      {
        id: `assistant-${turnId}`,
        message_id: `assistant-${turnId}`,
        role: 'assistant',
        text: `answer ${turnId}`,
        timestamp: '2026-07-07T10:00:01Z',
        turn_context: { turn_id: turnId },
      },
    ],
    has_more: false,
    canonical_available: true,
    canonical_complete: true,
  })

  it('keeps the live accepted ensemble strip through done and canonical replacement', async () => {
    const response = canonicalTurn()
    const { api, messages } = makeHistory(false, {
      messages: [
        {
          role: 'user',
          text: 'question turn-current',
          ts: '2026-07-07T10:00:00Z',
          messageId: 'user-turn-current',
          turnId: 'turn-current',
        },
        acceptedRouter('turn-current'),
        {
          role: 'assistant',
          text: 'answer turn-current',
          ts: '2026-07-07T10:00:01Z',
          turnId: 'turn-current',
        },
      ],
      response,
      preserveLiveTail: false,
    })

    await api.loadHistory()
    await api.loadHistory()

    expect(messages.value.map(message => message.role)).toEqual(['user', 'router', 'assistant'])
    const routers = messages.value.filter(message => message.role === 'router')
    expect(routers).toHaveLength(1)
    expect(routers[0]).toMatchObject({
      turnId: 'turn-current',
      routerSettled: true,
      restoredFromHistory: true,
      routerDecision: { accepted_routing_mode: 'ensemble' },
      ensemble: {
        models: [expect.objectContaining({ model: 'claude-sonnet-4.6' })],
      },
    })
  })

  it('merges the marker and live members into an existing same-turn canonical router', async () => {
    const response = canonicalTurn()
    response.messages?.splice(1, 0, {
      id: 'router-canonical',
      message_id: 'router-canonical',
      role: 'router',
      text: '',
      timestamp: '2026-07-07T10:00:00.750Z',
      turn_context: { turn_id: 'turn-current' },
      router_decision: {
        tier: 'c1',
        model: 'anthropic/claude-sonnet-4.6',
        source: 'squilla_router',
      },
    })
    const { api, messages } = makeHistory(false, {
      messages: [
        {
          role: 'user',
          text: 'question turn-current',
          ts: 0,
          messageId: 'user-turn-current',
          turnId: 'turn-current',
        },
        acceptedRouter('turn-current'),
      ],
      response,
    })

    await api.loadHistory()

    const routers = messages.value.filter(message => message.role === 'router')
    expect(routers).toHaveLength(1)
    expect(routers[0]).toMatchObject({
      messageId: 'router-canonical',
      turnId: 'turn-current',
      routerDecision: { accepted_routing_mode: 'ensemble' },
      ensemble: { modelCount: 1 },
    })
  })

  it('never copies an accepted marker to an adjacent turn or past compaction', async () => {
    const current = canonicalTurn('turn-current')
    const adjacent = canonicalTurn('turn-adjacent')
    const replacement = canonicalTurn('turn-after-compaction')
    adjacent.messages?.splice(1, 0, {
      id: 'router-adjacent',
      message_id: 'router-adjacent',
      role: 'router',
      text: '',
      timestamp: '2026-07-07T10:01:00.500Z',
      turn_context: { turn_id: 'turn-adjacent' },
      router_decision: {
        tier: 'c1',
        model: 'openai/gpt-5.4-mini',
        source: 'squilla_router',
      },
    })
    const { api, rpc, messages } = makeHistory(false, {
      messages: [acceptedRouter('turn-current'), acceptedRouter(undefined)],
    })
    rpc.call
      .mockResolvedValueOnce({
        ...current,
        messages: [...(current.messages || []), ...(adjacent.messages || [])],
      })
      .mockResolvedValueOnce(replacement)

    await api.loadHistory()

    const adjacentRouter = messages.value.find(message => message.messageId === 'router-adjacent')
    expect(adjacentRouter?.routerDecision?.accepted_routing_mode).toBeUndefined()
    expect(messages.value.filter(message =>
      message.role === 'router' && message.turnId === 'turn-current',
    )).toHaveLength(1)

    await api.loadHistory()

    expect(messages.value.some(message =>
      message.role === 'router' && message.turnId === 'turn-current',
    )).toBe(false)
    expect(messages.value.some(message => ['ensemble', 'llm_ensemble'].includes(
      String(message.routerDecision?.accepted_routing_mode || '').toLowerCase(),
    ))).toBe(false)
  })
})

describe('useChatHistory safe local-tail synchronization', () => {
  it('protects a successor from an older response until a post-generation load succeeds', async () => {
    vi.useFakeTimers()
    try {
      let resolveOld!: (value: ChatHistoryResponse) => void
      let resolveSafe!: (value: ChatHistoryResponse) => void
      const oldResponse = new Promise<ChatHistoryResponse>(resolve => { resolveOld = resolve })
      const safeResponse = new Promise<ChatHistoryResponse>(resolve => { resolveSafe = resolve })
      const durableA: NonNullable<ChatHistoryResponse['messages']> = [
        {
          id: 'user-a',
          message_id: 'user-a',
          role: 'user',
          text: 'prompt A',
          timestamp: '2026-07-06T00:00:00Z',
          turn_context: { turn_id: 'turn-a' },
        },
        {
          id: 'assistant-a',
          message_id: 'assistant-a',
          role: 'assistant',
          text: 'answer A',
          timestamp: '2026-07-06T00:00:01Z',
          turn_context: { turn_id: 'turn-a' },
        },
      ]
      const durableAB: NonNullable<ChatHistoryResponse['messages']> = [
        ...durableA,
        {
          id: 'user-b',
          message_id: 'user-b',
          role: 'user',
          text: 'prompt B',
          timestamp: '2026-07-06T00:00:02Z',
          turn_context: { turn_id: 'turn-b' },
        },
        {
          id: 'assistant-b',
          message_id: 'assistant-b',
          role: 'assistant',
          text: 'answer B',
          timestamp: '2026-07-06T00:00:03Z',
          turn_context: { turn_id: 'turn-b' },
        },
      ]
      const { api, rpc, messages } = makeHistory(false, {
        concurrentHistoryReads: false,
        messages: [
          {
            role: 'user',
            text: 'prompt A',
            ts: '2026-07-06T00:00:00Z',
            messageId: 'user-a',
            turnId: 'turn-a',
            restoredFromHistory: true,
          },
          {
            role: 'assistant',
            text: 'answer A',
            ts: '2026-07-06T00:00:01Z',
            messageId: 'assistant-a',
            turnId: 'turn-a',
            restoredFromHistory: true,
          },
          {
            role: 'user',
            text: 'prompt B',
            ts: 'local-b',
            messageId: 'user-b',
            turnId: 'turn-b',
          },
          {
            role: 'assistant',
            text: 'answer B in progress',
            ts: 'local-b-answer',
            turnId: 'turn-b',
          },
        ],
      })
      rpc.call
        .mockImplementationOnce(() => oldResponse)
        .mockImplementationOnce(() => safeResponse)
        .mockResolvedValueOnce({ messages: durableA, has_more: false })

      const oldLoad = api.loadHistory()
      await Promise.resolve()
      expect(rpc.call).toHaveBeenCalledTimes(1)

      api.scheduleHistorySync(true)
      await vi.advanceTimersByTimeAsync(50)
      expect(rpc.call).toHaveBeenCalledTimes(1)

      resolveOld({ messages: durableA, has_more: false })
      await oldLoad
      expect(messages.value.map(message => message.text)).toEqual([
        'prompt A',
        'answer A',
        'prompt B',
        'answer B in progress',
      ])

      await vi.advanceTimersByTimeAsync(50)
      expect(rpc.call).toHaveBeenCalledTimes(2)
      expect(rpc.call.mock.calls[1]?.[2]).toMatchObject({
        timeoutAction: 'reject',
        abortAction: 'reject',
      })
      resolveSafe({ messages: durableAB, has_more: false })
      await Promise.resolve()
      await Promise.resolve()
      await Promise.resolve()
      expect(api.historyState.value.loading).toBe(false)
      expect(messages.value.map(message => message.messageId)).toEqual([
        'user-a',
        'assistant-a',
        'user-b',
        'assistant-b',
      ])

      await api.loadHistory()
      expect(rpc.call).toHaveBeenCalledTimes(3)
      expect(rpc.call.mock.calls[2]?.[2]).toMatchObject({
        timeoutAction: 'reconnect',
        abortAction: 'reconnect',
      })
      expect(messages.value.map(message => message.messageId)).toEqual([
        'user-a',
        'assistant-a',
      ])
      api.cleanup()
    } finally {
      vi.useRealTimers()
    }
  })

  it('keeps a ready session unchanged when a safe background sync times out', async () => {
    vi.useFakeTimers()
    try {
      const { api, rpc, messages } = makeHistory(false, {
        concurrentHistoryReads: false,
      })
      await api.loadHistory()
      const readyMessages = messages.value
      expect(api.historyState.value).toMatchObject({
        initialLoadStatus: 'ready',
        loading: false,
        retrying: false,
        recoveryError: false,
      })

      rpc.call.mockRejectedValueOnce(new RpcTimeoutError('chat.history', 1_000))
      api.scheduleHistorySync(true)
      await vi.advanceTimersByTimeAsync(50)
      await Promise.resolve()
      await Promise.resolve()

      expect(rpc.call).toHaveBeenCalledTimes(2)
      expect(rpc.call.mock.calls[1]?.[2]).toMatchObject({
        timeoutAction: 'reject',
        abortAction: 'reject',
      })
      expect(messages.value).toBe(readyMessages)
      expect(api.historyState.value).toMatchObject({
        initialLoadStatus: 'ready',
        loading: false,
        loadingEarlier: false,
        retrying: false,
        loadEarlierError: false,
        recoveryError: false,
      })
      expect(api.retryHistory()).toBeUndefined()
      expect(rpc.call).toHaveBeenCalledTimes(2)

      messages.value.push(
        {
          role: 'user',
          text: 'successor prompt',
          ts: 'local-successor',
          messageId: 'successor-user',
          turnId: 'successor-turn',
        },
        {
          role: 'assistant',
          text: 'successor answer',
          ts: 'local-successor-answer',
          turnId: 'successor-turn',
        },
      )
      rpc.call.mockResolvedValueOnce({
        messages: [historyMessage('m1')],
        has_more: false,
      })

      await api.loadHistory()

      expect(rpc.call.mock.calls[2]?.[2]).toMatchObject({
        timeoutAction: 'reconnect',
        abortAction: 'reconnect',
      })
      expect(messages.value.map(message => message.text)).toEqual([
        'm1',
        'successor prompt',
        'successor answer',
      ])
      api.cleanup()
    } finally {
      vi.useRealTimers()
    }
  })
})
