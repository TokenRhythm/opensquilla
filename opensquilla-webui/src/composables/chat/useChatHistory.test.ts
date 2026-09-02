// @vitest-environment happy-dom
import { describe, expect, it, vi } from 'vitest'
import { nextTick, ref, watch, type Ref } from 'vue'

import { useChatHistory } from './useChatHistory'
import type { ChatMessage, ChatTurnOutcome } from '@/types/chat'
import { RpcTimeoutError } from '@/lib/rpc'
import {
  SessionReadSessionMissingError,
  type SessionReadCompactionSummary,
  type SessionReadHistoryPage,
  type SessionReadHistoryOptions,
  type SessionReadLease,
  type SessionReadMessage,
  type SessionReadTurnContext,
  type SessionReadTurnOutcome,
} from '@/modules/sessionReadLifecycle'

type SessionReadTurnContextFixture = SessionReadTurnContext | (
  Partial<SessionReadTurnContext> & Readonly<Record<string, unknown>>
)

type SessionReadMessageFixture = SessionReadMessage | (
  Partial<Omit<SessionReadMessage, 'turnContext'>> & {
    readonly turnContext?: SessionReadTurnContextFixture | null
  }
)

type SessionReadCompactionSummaryFixture = Partial<Omit<SessionReadCompactionSummary, 'id'>> & {
  readonly id?: string | number | null
}

type SessionReadTurnOutcomeFixture = Partial<Omit<SessionReadTurnOutcome, 'replayProof'>> & {
  readonly replayProof?: Partial<SessionReadTurnOutcome['replayProof']>
  readonly usageCallIndex?: number | null
  readonly noPriorProviderDispatch?: boolean | null
  readonly replaySafe?: boolean | null
  readonly retryAfterMs?: number | null
  readonly userMessageId?: string | null
  readonly terminalMessage?: string | null
} & Readonly<Record<string, unknown>>

type SessionReadHistoryPageFixture = Partial<Omit<
  SessionReadHistoryPage,
  'messages' | 'compactionSummaries' | 'turnOutcomes' | 'scope'
>> & {
  readonly messages?: SessionReadMessageFixture[]
  readonly compactionSummaries?: SessionReadCompactionSummaryFixture[]
  readonly turnOutcomes?: SessionReadTurnOutcomeFixture[]
  readonly scope?: SessionReadHistoryPage['scope'] | 'session'
}

function sessionReadMessage(
  value: SessionReadMessageFixture,
  index: number,
): SessionReadMessage {
  const context = value.turnContext ?? null
  const {
    additional: contextAdditional = {},
    turnId = null,
    promotedTurnId = null,
    appliedIteration = null,
    activityMarkers = [],
    ...contextFields
  } = context ?? {}
  return {
    id: String(value.id ?? value.messageId ?? `history:${index}`),
    messageId: value.messageId ?? null,
    transcriptId: value.transcriptId ?? null,
    role: value.role ?? 'unknown',
    text: value.text ?? '',
    createdAt: value.createdAt ?? null,
    reasoningContent: value.reasoningContent ?? null,
    routerDecision: value.routerDecision ?? null,
    artifacts: value.artifacts ?? [],
    toolCalls: value.toolCalls ?? [],
    timeline: value.timeline ?? [],
    attachments: value.attachments ?? [],
    promptAnnotations: value.promptAnnotations ?? [],
    provenance: value.provenance ?? {
      kind: null,
      sourceSessionKey: null,
      sourceTool: null,
    },
    turnContext: context
      ? {
          turnId,
          promotedTurnId,
          appliedIteration,
          activityMarkers,
          additional: { ...contextFields, ...contextAdditional },
        }
      : null,
    usage: value.usage ?? null,
    model: value.model ?? null,
    inputTokens: value.inputTokens ?? null,
    outputTokens: value.outputTokens ?? null,
    additional: value.additional ?? {},
  }
}

function sessionReadSummary(
  value: SessionReadCompactionSummaryFixture,
): SessionReadCompactionSummary {
  return {
    id: value.id == null ? null : String(value.id),
    compactionId: value.compactionId ?? null,
    compactionIndex: value.compactionIndex ?? null,
    triggerReason: value.triggerReason ?? null,
    summaryText: value.summaryText ?? '',
    summaryFormat: value.summaryFormat ?? '',
    coverageStatus: value.coverageStatus ?? '',
    removedCount: value.removedCount ?? null,
    keptCount: value.keptCount ?? null,
    coveredThroughId: value.coveredThroughId == null
      ? null
      : String(value.coveredThroughId),
    createdAt: typeof value.createdAt === 'number' ? value.createdAt : null,
    additional: value.additional ?? {},
  }
}

function sessionReadOutcome(
  value: SessionReadTurnOutcomeFixture,
): SessionReadTurnOutcome {
  const replay = value.replayProof ?? value
  const {
    replayProof: _replayProof,
    usageCallIndex: _usageCallIndex,
    noPriorProviderDispatch: _noPriorProviderDispatch,
    replaySafe: _replaySafe,
    retryAfterMs: _retryAfterMs,
    userMessageId: _userMessageId,
    terminalMessage: _terminalMessage,
    turnId: _turnId,
    taskId: _taskId,
    status: _status,
    startedAt: _startedAt,
    finishedAt: _finishedAt,
    outcome: _outcome,
    errorClass: _errorClass,
    retryable: _retryable,
    activitySnapshot: _activitySnapshot,
    usage: _usage,
    additional = {},
    ...outcomeAdditional
  } = value
  const activitySnapshot = value.activitySnapshot
    ? (() => {
        const {
          taskId,
          turnId,
          entries,
          ...snapshotFields
        } = value.activitySnapshot
        return {
          ...snapshotFields,
          task_id: taskId ?? value.activitySnapshot.task_id,
          turn_id: turnId ?? value.activitySnapshot.turn_id,
          entries: Array.isArray(entries)
            ? entries.map((entry) => {
                if (!entry || typeof entry !== 'object' || Array.isArray(entry)) return entry
                const {
                  startedAt,
                  retryAfterMs,
                  ...entryFields
                } = entry as Record<string, unknown>
                return {
                  ...entryFields,
                  ...(startedAt !== undefined ? { started_at: startedAt } : {}),
                  ...(retryAfterMs !== undefined ? { retry_after_ms: retryAfterMs } : {}),
                }
              })
            : entries,
        }
      })()
    : null
  return {
    turnId: value.turnId ?? '',
    taskId: value.taskId ?? null,
    status: value.status ?? '',
    startedAt: value.startedAt ?? null,
    finishedAt: value.finishedAt ?? null,
    outcome: value.outcome ?? {},
    errorClass: value.errorClass ?? null,
    retryable: value.retryable ?? null,
    activitySnapshot,
    usage: value.usage ?? null,
    replayProof: {
      usageCallIndex: replay.usageCallIndex ?? null,
      noPriorProviderDispatch: replay.noPriorProviderDispatch ?? null,
      replaySafe: replay.replaySafe ?? null,
      retryAfterMs: replay.retryAfterMs ?? null,
      userMessageId: replay.userMessageId ?? null,
      terminalMessage: replay.terminalMessage ?? null,
    },
    additional: { ...outcomeAdditional, ...additional },
  }
}

function sessionReadPage(value: SessionReadHistoryPageFixture): SessionReadHistoryPage {
  return {
    messages: (value.messages ?? []).map(sessionReadMessage),
    hasMore: value.hasMore ?? false,
    oldestCursor: value.oldestCursor == null ? null : String(value.oldestCursor),
    newestCursor: value.newestCursor == null ? null : String(value.newestCursor),
    scope: value.scope === 'latestWindow' || value.scope === 'compacted'
      ? value.scope
      : 'complete',
    loadedCount: value.messages?.length ?? 0,
    pageSize: value.pageSize ?? 50,
    canonicalAvailable: value.canonicalAvailable ?? null,
    canonicalComplete: value.canonicalComplete ?? null,
    compactionSummaries: (value.compactionSummaries ?? []).map(sessionReadSummary),
    turnOutcomes: (value.turnOutcomes ?? []).map(sessionReadOutcome),
    additional: value.additional ?? {},
  }
}

function makeHistory(autoScroll = true, overrides: {
  response?: SessionReadHistoryPageFixture
  messages?: ChatMessage[]
  preserveLiveTail?: boolean
  autoScroll?: Ref<boolean>
  sessionKey?: Ref<string>
  scrollEpoch?: Ref<number>
  canApplyViewportCorrection?: () => boolean
  threadRef?: Ref<HTMLElement | null>
  concurrentHistoryReads?: boolean
  onTerminalTask?: (outcome: ChatTurnOutcome) => void
} = {}) {
  const response: SessionReadHistoryPageFixture = overrides.response || {
    messages: [
      {
        id: 'm1',
        messageId: 'm1',
        role: 'assistant',
        text: 'hello',
        createdAt: '2026-07-06T00:00:00Z',
      },
    ],
    hasMore: false,
    oldestCursor: null,
    newestCursor: null,
    scope: 'session',
  }
  const messages = ref<ChatMessage[]>(overrides.messages || [])
  const sessionKey = overrides.sessionKey || ref('agent:main:webchat:test')
  const historyFixture = vi.fn(async (
    _direction: 'latest' | 'before' | 'after',
    _cursor: string | null,
    _readOptions: SessionReadHistoryOptions = {},
  ): Promise<SessionReadHistoryPageFixture> => response)
  const readHistory = vi.fn(async (
    direction: 'latest' | 'before' | 'after',
    cursor: string | null,
    readOptions: SessionReadHistoryOptions = {},
  ) => sessionReadPage(await historyFixture(direction, cursor, readOptions)))
  const lease = {
    criticalRequestsQueued: Promise.resolve(),
    history: {
      latest: (readOptions?: SessionReadHistoryOptions) => readHistory('latest', null, readOptions),
      before: (cursor: string, readOptions?: SessionReadHistoryOptions) => readHistory('before', cursor, readOptions),
      after: (cursor: string, readOptions?: SessionReadHistoryOptions) => readHistory('after', cursor, readOptions),
    },
  } as SessionReadLease
  const scrollToBottom = vi.fn()
  const api = useChatHistory({
    sessionReadLeaseReader: { current: () => lease },
    sessionKey,
    messages,
    threadRef: overrides.threadRef,
    lastHeaderRole: ref(''),
    lastHeaderDay: ref(''),
    preserveLiveTail: ref(overrides.preserveLiveTail ?? false),
    autoScroll: overrides.autoScroll ?? ref(autoScroll),
    scrollEpoch: overrides.scrollEpoch,
    canApplyViewportCorrection: overrides.canApplyViewportCorrection,
    stripTimePrefix: text => text,
    scrollToBottom,
    onTerminalTask: overrides.onTerminalTask,
  })
  return { api, readHistory, historyFixture, scrollToBottom, messages }
}

function historyMessage(id: string): SessionReadMessage {
  return sessionReadMessage({
    id,
    messageId: id,
    role: 'assistant',
    text: id,
    createdAt: `2026-07-06T00:00:${id.replace(/\D/g, '').padStart(2, '0')}Z`,
  }, 0)
}

function makeLiveEdgeRecovery(overrides: {
  scrollEpoch?: Ref<number>
  canApplyViewportCorrection?: () => boolean
  onCommit: (context: {
    autoScroll: Ref<boolean>
    thread: HTMLElement
  }) => void
}) {
  const autoScroll = ref(true)
  const thread = document.createElement('div')
  Object.defineProperties(thread, {
    clientHeight: { configurable: true, value: 300 },
    scrollHeight: { configurable: true, value: 12_000 },
    scrollTop: { configurable: true, value: 11_700, writable: true },
  })
  document.body.append(thread)
  const { api, messages } = makeHistory(true, {
    autoScroll,
    scrollEpoch: overrides.scrollEpoch,
    canApplyViewportCorrection: overrides.canApplyViewportCorrection,
    threadRef: ref<HTMLElement | null>(thread),
    messages: [{
      role: 'assistant',
      text: 'message 0320',
      ts: '2026-07-06T00:05:20Z',
      messageId: 'm-320',
      restoredFromHistory: true,
    }],
    response: {
      messages: [historyMessage('m-320')],
      hasMore: true,
      oldestCursor: 'cursor-320',
      newestCursor: 'cursor-320',
      canonicalAvailable: true,
    },
  })
  const stopRender = watch(
    messages,
    () => overrides.onCommit({ autoScroll, thread }),
    { flush: 'sync' },
  )
  return {
    api,
    autoScroll,
    thread,
    cleanup: () => {
      stopRender()
      thread.remove()
    },
  }
}

function makeReaderAnchorRecovery(overrides: {
  canApplyViewportCorrection?: () => boolean
  pendingImage?: boolean
  onCommit?: () => void
} = {}) {
  const thread = document.createElement('div')
  let messageContentTop = 1_040
  let emitPendingImageLoad = () => {}
  Object.defineProperties(thread, {
    clientHeight: { configurable: true, value: 300 },
    scrollHeight: { configurable: true, value: 1_400 },
    scrollTop: { configurable: true, value: 1_000, writable: true },
  })
  thread.getBoundingClientRect = () => ({
    top: 0,
    bottom: 300,
    left: 0,
    right: 600,
    width: 600,
    height: 300,
    x: 0,
    y: 0,
    toJSON: () => ({}),
  })
  const renderAnchor = () => {
    const anchor = document.createElement('article')
    anchor.dataset.messageId = 'm-320'
    if (overrides.pendingImage) {
      const image = document.createElement('img')
      Object.defineProperty(image, 'complete', { configurable: true, value: false })
      emitPendingImageLoad = () => image.dispatchEvent(new Event('load'))
      anchor.append(image)
    }
    anchor.getBoundingClientRect = () => {
      const top = messageContentTop - thread.scrollTop
      return {
        top,
        bottom: top + 40,
        left: 0,
        right: 600,
        width: 600,
        height: 40,
        x: 0,
        y: top,
        toJSON: () => ({}),
      }
    }
    thread.replaceChildren(anchor)
  }
  renderAnchor()
  document.body.append(thread)

  const { api, messages } = makeHistory(false, {
    canApplyViewportCorrection: overrides.canApplyViewportCorrection,
    threadRef: ref<HTMLElement | null>(thread),
    messages: [{
      role: 'assistant',
      text: 'message 0320',
      ts: '2026-07-06T00:05:20Z',
      messageId: 'm-320',
      restoredFromHistory: true,
    }],
    response: {
      messages: [historyMessage('m-320')],
      hasMore: true,
      oldestCursor: 'cursor-320',
      newestCursor: 'cursor-320',
      canonicalAvailable: true,
    },
  })
  const stopRender = watch(messages, () => {
    // Model Electron replacing the long-history window before the browser
    // has a chance to preserve its native scroll anchor.
    thread.scrollTop = 0
    renderAnchor()
    overrides.onCommit?.()
  }, { flush: 'sync' })

  return {
    api,
    thread,
    emitPendingImageLoad: () => emitPendingImageLoad(),
    setMessageContentTop: (top: number) => { messageContentTop = top },
    cleanup: () => {
      stopRender()
      thread.remove()
    },
  }
}

describe('useChatHistory canonical pagination', () => {
  it('restores nested prompt annotation snapshots on an annotation-only user row', async () => {
    const { api, messages } = makeHistory(false, {
      response: {
        messages: [{
          id: 'user-annotation-1',
          messageId: 'user-annotation-1',
          role: 'user',
          text: '',
          createdAt: '2026-07-06T00:00:00Z',
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
        hasMore: false,
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
          messageId: 'assistant-1',
          role: 'assistant',
          text: 'Working note.Final answer.',
          createdAt: '2026-07-06T00:00:00Z',
          timeline: [
            { type: 'text', raw: 'Working note.', presentation: 'intermediate' },
            { type: 'text', raw: 'Final answer.', presentation: 'answer' },
          ],
        }],
        hasMore: false,
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
          messageId: 'user-send',
          role: 'user',
          text: 'ordinary queued follow-up',
          createdAt: '2026-07-06T00:00:00Z',
          turnContext: {
            turnId: 'turn-send',
            targetTurnId: 'turn-send',
            clientRequestId: 'request-send',
            clientMessageId: 'client-send',
            intent: 'send',
            disposition: 'applied',
            revision: 1,
          },
        }],
        hasMore: false,
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
          messageId: 'current-steer',
          role: 'user',
          text: 'current same-turn correction',
          createdAt: '2026-07-06T00:00:00Z',
          turnContext: {
            turnId: 'turn-steer',
            clientRequestId: 'request-steer',
            clientMessageId: 'client-steer',
            intent: 'steer',
            disposition: 'applied',
            revision: 2,
          },
        }],
        hasMore: false,
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
          messageId: `legacy-send-${disposition}`,
          role: 'user' as const,
          text: `legacy primary ${disposition}`,
          createdAt: `2026-07-06T00:00:0${index}Z`,
          turnContext: {
            turnId: 'turn-send',
            targetTurnId: 'turn-send',
            clientRequestId: `request-${disposition}`,
            clientMessageId: `client-${disposition}`,
            disposition,
            revision: 2,
            appliedIteration: null,
          },
        })),
        hasMore: false,
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
          messageId: 'legacy-steer',
          role: 'user',
          text: 'legacy same-turn correction',
          createdAt: '2026-07-06T00:00:00Z',
          turnContext: {
            turnId: 'turn-steer',
            clientRequestId: 'request-steer',
            clientMessageId: 'client-steer',
            disposition: 'applied',
            revision: 2,
            modelCallId: '2.0',
            appliedIteration: 2,
          },
        }],
        hasMore: false,
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
      turnId: 'turn-goal',
      inputMode: 'system_event',
      runKind: 'goal',
    }
    const { api, messages } = makeHistory(false, {
      response: {
        messages: [{
          id: 'assistant-goal',
          messageId: 'assistant-goal',
          role: 'assistant',
          text: 'NO_REPLY\nGoal progress',
          createdAt: '2026-07-06T00:00:00Z',
          turnContext: turnContext,
        }],
        hasMore: false,
      },
    })

    await api.loadHistory()

    expect(messages.value[0]).toMatchObject({
      turnId: 'turn-goal',
      turnInputMode: 'system_event',
      turnRunKind: 'goal',
    })
    expect(turnContext).toEqual({
      turnId: 'turn-goal',
      inputMode: 'system_event',
      runKind: 'goal',
    })
  })

  it('derives internal goal provenance from a legacy goal_continuation intent', async () => {
    const turnContext = {
      turnId: 'turn-legacy-goal',
      intent: 'goal_continuation',
    }
    const { api, messages } = makeHistory(false, {
      response: {
        messages: [{
          id: 'assistant-legacy-goal',
          messageId: 'assistant-legacy-goal',
          role: 'assistant',
          text: 'NO_REPLY\nGoal progress',
          createdAt: '2026-07-06T00:00:00Z',
          turnContext: turnContext,
        }],
        hasMore: false,
      },
    })

    await api.loadHistory()

    expect(messages.value[0]).toMatchObject({
      turnId: 'turn-legacy-goal',
      turnInputMode: 'system_event',
      turnRunKind: 'goal',
    })
    expect(turnContext).toEqual({
      turnId: 'turn-legacy-goal',
      intent: 'goal_continuation',
    })
  })

  it('preserves additive cancellation usage coverage from canonical history', async () => {
    const usage = {
      input_tokens: 1,
      output_tokens: 1,
      cost_usd: 0,
      coverageStatus: 'usage_unknown',
      usage_unknown: true,
      unknown_usage_events: 1,
    }
    const { api, messages } = makeHistory(false, {
      response: {
        messages: [{
          id: 'assistant-cancelled',
          messageId: 'assistant-cancelled',
          role: 'assistant',
          text: 'Partial answer',
          createdAt: '2026-07-06T00:00:00Z',
          turnContext: { turnId: 'turn-cancelled' },
          usage,
        }],
        hasMore: false,
      },
    })

    await api.loadHistory()

    expect(messages.value[0]?.usage).toEqual(usage)
    expect(usage).toEqual({
      input_tokens: 1,
      output_tokens: 1,
      cost_usd: 0,
      coverageStatus: 'usage_unknown',
      usage_unknown: true,
      unknown_usage_events: 1,
    })
  })

  it('requests canonical messages with durable compaction summaries', async () => {
    const { api, readHistory } = makeHistory()

    expect(api.historyState.value.initialLoadStatus).toBe('pending')
    await api.loadHistory()

    expect(readHistory).toHaveBeenCalledWith('latest', null, expect.objectContaining({
      signal: expect.any(AbortSignal),
      budgetMs: expect.any(Number),
      deadlineAt: expect.any(Number),
    }))
    expect(api.historyState.value).toMatchObject({
      initialLoadStatus: 'ready',
    })
  })

  it('keeps one legacy activity row across prepend and canonical refresh', async () => {
    const activity = {
      id: 'legacy-tools',
      messageId: 'legacy-tools',
      role: 'assistant',
      text: '',
      createdAt: '2026-07-06T00:00:01Z',
      toolCalls: [
        { type: 'text', text: 'Inspect the source.' },
        { type: 'tool_use', tool_use_id: 'call-read', name: 'read_file', input: {} },
        { type: 'text', text: 'Compare the directory.' },
        { type: 'tool_use', tool_use_id: 'call-list', name: 'list_dir', input: {} },
        { type: 'tool_result', tool_use_id: 'call-read', name: 'read_file', result: 'source' },
        { type: 'tool_result', tool_use_id: 'call-list', name: 'list_dir', result: 'directory' },
      ],
    }
    const { api, historyFixture, messages } = makeHistory(false, {
      response: {
        messages: [activity],
        canonicalComplete: false,
        hasMore: true,
        oldestCursor: 'cursor-tools',
        newestCursor: 'cursor-tools',
      },
    })
    historyFixture
      .mockResolvedValueOnce({
        messages: [activity],
        canonicalComplete: false,
        hasMore: true,
        oldestCursor: 'cursor-tools',
        newestCursor: 'cursor-tools',
      })
      .mockResolvedValueOnce({
        messages: [{
          id: 'older-user',
          messageId: 'older-user',
          role: 'user',
          text: 'Earlier request',
          createdAt: '2026-07-06T00:00:00Z',
        }],
        canonicalComplete: false,
        hasMore: false,
        oldestCursor: 'cursor-older',
        newestCursor: 'cursor-older',
      })
      .mockResolvedValueOnce({
        messages: [
          activity,
          {
            id: 'later-user',
            messageId: 'later-user',
            role: 'user',
            text: 'Continue',
            createdAt: '2026-07-06T00:00:02Z',
          },
        ],
        canonicalComplete: true,
        hasMore: false,
        oldestCursor: 'cursor-tools',
        newestCursor: 'cursor-later',
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
    const response: SessionReadHistoryPageFixture = {
      messages: [
        {
          id: 'user-1',
          messageId: 'user-1',
          role: 'user',
          text: 'Earlier request',
          createdAt: baseTime,
        },
        {
          id: 'assistant-1',
          messageId: 'assistant-1',
          role: 'assistant',
          text: 'Earlier answer',
          createdAt: baseTime + 1_000,
        },
        {
          id: 'user-2',
          messageId: 'user-2',
          role: 'user',
          text: 'Continue',
          createdAt: baseTime + 3_000,
        },
      ],
      canonicalComplete: true,
      compactionSummaries: [
        {
          id: 9,
          compactionId: 'cmp-9',
          compactionIndex: 2,
          triggerReason: 'manual',
          removedCount: 8,
          keptCount: 2,
          createdAt: 1_720_000_001,
        },
        {
          id: 7,
          compactionId: 'cmp-7',
          compactionIndex: 1,
          triggerReason: 'manual',
          removedCount: 5,
          keptCount: 1,
          createdAt: 1_720_000_001,
        },
        {
          id: 8,
          compactionId: 'cmp-auto',
          triggerReason: 'auto_threshold',
          createdAt: 1_720_000_002,
        },
      ],
      hasMore: false,
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
            messageId: 'user-old',
            role: 'user',
            text: 'Original request',
            createdAt: baseTime,
            turnContext: { turnId: 'turn-old' },
          },
          {
            id: 'steer-1',
            messageId: 'steer-1',
            role: 'user',
            text: 'Use the new constraint',
            createdAt: baseTime + 1_000,
            turnContext: {
              turnId: 'turn-new',
              promotedFromTurnId: 'turn-old',
              disposition: 'promoted',
              revision: 2,
            },
          },
          {
            id: 'assistant-old',
            messageId: 'assistant-old',
            role: 'assistant',
            text: 'Completed old turn',
            createdAt: baseTime + 2_000,
            turnContext: { turnId: 'turn-old' },
          },
          {
            id: 'assistant-new',
            messageId: 'assistant-new',
            role: 'assistant',
            text: 'Completed promoted turn',
            createdAt: baseTime + 3_000,
            turnContext: { turnId: 'turn-new' },
          },
        ],
        compactionSummaries: [{
          id: 11,
          compactionId: 'cmp-promoted',
          triggerReason: 'manual',
          createdAt: baseTime + 1_500,
        }],
        hasMore: false,
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
    const { api, readHistory } = makeHistory()
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

    expect(readHistory).toHaveBeenCalledWith(
      'latest',
      null,
      expect.objectContaining({
        signal: expect.any(AbortSignal),
        budgetMs: expect.any(Number),
        deadlineAt: now + 7_000,
      }),
    )
  })

  it('keeps transport recovery policy behind the lease while making cancellation local', async () => {
    const { api, readHistory } = makeHistory(true, {
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

    expect(readHistory).toHaveBeenCalledWith(
      'latest',
      null,
      expect.objectContaining({
        signal: expect.any(AbortSignal),
        budgetMs: expect.any(Number),
        deadlineAt: now + 7_000,
      }),
    )
  })

  it('enters the initial loading state before the first RPC settles', async () => {
    let resolveHistory!: (value: SessionReadHistoryPageFixture) => void
    const pendingHistory = new Promise<SessionReadHistoryPageFixture>(resolve => {
      resolveHistory = resolve
    })
    const { api, historyFixture } = makeHistory()
    historyFixture.mockReturnValueOnce(pendingHistory)

    const load = api.loadHistory()
    expect(api.historyState.value.initialLoadStatus).toBe('loading')

    resolveHistory({
      messages: [],
      hasMore: false,
      oldestCursor: null,
    })
    await load
    expect(api.historyState.value.initialLoadStatus).toBe('ready')
  })

  it('does not restore the full-screen loader for a settled empty-session refresh', async () => {
    let resolveRefresh!: (value: SessionReadHistoryPageFixture) => void
    const refreshResponse = new Promise<SessionReadHistoryPageFixture>(resolve => {
      resolveRefresh = resolve
    })
    const { api, historyFixture } = makeHistory(false, {
      response: {
        messages: [],
        hasMore: false,
        oldestCursor: null,
      },
    })

    await api.loadHistory()
    historyFixture.mockReturnValueOnce(refreshResponse)
    const refresh = api.loadHistory()

    expect(api.historyState.value.loading).toBe(true)
    expect(api.historyState.value.initialLoadStatus).toBe('ready')

    resolveRefresh({
      messages: [],
      hasMore: false,
      oldestCursor: null,
    })
    await refresh
    expect(api.historyState.value.initialLoadStatus).toBe('ready')
  })

  it('keeps a settled empty-session refresh failure retryable without restoring the loader', async () => {
    const { api, readHistory, historyFixture } = makeHistory(false, {
      response: {
        messages: [],
        hasMore: false,
        oldestCursor: null,
      },
    })

    await api.loadHistory()
    historyFixture.mockRejectedValueOnce(new Error('offline'))
    await api.loadHistory()

    expect(api.historyState.value).toMatchObject({
      initialLoadStatus: 'ready',
      loadEarlierError: false,
      recoveryError: true,
    })

    let resolveRetry!: (value: SessionReadHistoryPageFixture) => void
    historyFixture.mockReturnValueOnce(new Promise<SessionReadHistoryPageFixture>(resolve => {
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
      hasMore: false,
      oldestCursor: null,
    })
    await retry
    expect(readHistory).toHaveBeenCalledTimes(3)
    expect(api.historyState.value).toMatchObject({
      initialLoadStatus: 'ready',
      retrying: false,
      loadEarlierError: false,
      recoveryError: false,
    })
  })

  it('classifies a missing session separately from a retryable history failure', async () => {
    const { api, historyFixture } = makeHistory()
    historyFixture.mockRejectedValueOnce(
      new SessionReadSessionMissingError('missing'),
    )

    await api.loadHistory()

    expect(api.historyState.value).toMatchObject({
      initialLoadStatus: 'error',
      recoveryError: true,
      sessionMissing: true,
    })
  })

  it('commits live-proven missing state once and fences a successor session', async () => {
    const firstKey = 'agent:main:webchat:first'
    const sessionKey = ref(firstKey)
    let resolveFirst!: (value: SessionReadHistoryPageFixture) => void
    const firstPage = new Promise<SessionReadHistoryPageFixture>(resolve => {
      resolveFirst = resolve
    })
    const { api, historyFixture } = makeHistory(false, { sessionKey })
    historyFixture.mockReturnValueOnce(firstPage)

    const firstLoad = api.loadHistory()
    expect(api.historyState.value.loading).toBe(true)
    expect(api.markSessionMissing(firstKey)).toBe(true)
    expect(api.historyState.value).toMatchObject({
      initialLoadStatus: 'ready',
      loading: false,
      loadingEarlier: false,
      retrying: false,
      loadEarlierError: false,
      recoveryError: false,
      sessionMissing: true,
    })
    resolveFirst({ messages: [], hasMore: false })
    await firstLoad
    expect(api.historyState.value.sessionMissing).toBe(true)

    const successorKey = 'agent:main:webchat:successor'
    sessionKey.value = successorKey
    let resolveSuccessor!: (value: SessionReadHistoryPageFixture) => void
    historyFixture.mockReturnValueOnce(new Promise<SessionReadHistoryPageFixture>(resolve => {
      resolveSuccessor = resolve
    }))
    const successorLoad = api.loadHistory()
    expect(api.historyState.value).toMatchObject({
      loading: true,
      sessionMissing: false,
    })
    expect(api.markSessionMissing(firstKey)).toBe(false)
    expect(api.historyState.value).toMatchObject({
      loading: true,
      sessionMissing: false,
    })
    resolveSuccessor({ messages: [], hasMore: false })
    await successorLoad
    expect(api.historyState.value).toMatchObject({
      initialLoadStatus: 'ready',
      sessionMissing: false,
    })
  })

  it('keeps loaded messages visible and exposes an inline recovery error after refresh fails', async () => {
    const { api, historyFixture, messages } = makeHistory()
    await api.loadHistory()
    expect(messages.value.map(message => message.text)).toEqual(['hello'])

    historyFixture.mockRejectedValueOnce(new Error('refresh disconnected'))
    await api.loadHistory()

    expect(messages.value.map(message => message.text)).toEqual(['hello'])
    expect(api.historyState.value).toMatchObject({
      initialLoadStatus: 'ready',
      loading: false,
      recoveryError: true,
    })
  })

  it('keeps an unavailable canonical reader retryable after an empty session has settled', async () => {
    const { api, readHistory, historyFixture } = makeHistory(false, {
      response: {
        messages: [],
        hasMore: false,
        oldestCursor: null,
      },
    })

    await api.loadHistory()
    historyFixture
      .mockResolvedValueOnce({
        messages: [],
        hasMore: false,
        oldestCursor: null,
        canonicalAvailable: false,
        canonicalComplete: false,
      })
      .mockResolvedValueOnce({
        messages: [],
        hasMore: false,
        oldestCursor: null,
        canonicalAvailable: true,
        canonicalComplete: true,
      })
    await api.loadHistory()

    expect(api.historyState.value).toMatchObject({
      initialLoadStatus: 'ready',
      canonicalAvailable: false,
      canonicalComplete: false,
      loadEarlierError: false,
    })

    await api.retryHistory()
    expect(readHistory).toHaveBeenCalledTimes(3)
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
          messageId: 'assistant-1',
          role: 'assistant',
          text: 'partial answer',
          createdAt: '2026-07-06T00:00:00Z',
          turnContext: {
            turnId: 'turn-1',
            intent: 'send',
          },
        }],
        hasMore: false,
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
          messageId: 'assistant-1',
          role: 'assistant',
          text: 'answer after compaction',
          createdAt: '2026-07-06T00:00:00Z',
          turnContext: {
            turnId: 'turn-1',
            activityMarkers: [{
              kind: 'context_compaction',
              id: 'cmp-history',
              status: 'completed',
              at: 1_720_000_000_000,
            }],
          },
        }],
        canonicalComplete: true,
        compactionSummaries: [{
          id: 12,
          compactionId: 'cmp-history',
          triggerReason: 'manual',
          createdAt: 1_720_000_000_000,
        }],
        hasMore: false,
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
    const { api, historyFixture, messages } = makeHistory(false)
    historyFixture
      .mockResolvedValueOnce({
        messages: [{
          id: 'assistant-1',
          messageId: 'assistant-1',
          role: 'assistant',
          text: '前😀后续',
          createdAt: '2026-07-06T00:00:02Z',
          turnContext: { turnId: 'turn-1' },
          usage: {
            model_call_segments: [{
              modelCallId: '2.0',
              iteration: 2,
              start_codepoint: 2,
              end_codepoint: 4,
            }],
          },
        }],
        hasMore: true,
        oldestCursor: 'cursor-assistant',
        newestCursor: 'cursor-assistant',
      })
      .mockResolvedValueOnce({
        messages: [
          {
            id: 'user-1',
            messageId: 'user-1',
            role: 'user',
            text: '原始问题',
            createdAt: '2026-07-06T00:00:00Z',
            turnContext: { turnId: 'turn-1' },
          },
          {
            id: 'steer-1',
            messageId: 'steer-1',
            role: 'user',
            text: '请补充细节',
            createdAt: '2026-07-06T00:00:01Z',
            turnContext: {
              turnId: 'turn-1',
              disposition: 'applied',
              revision: 2,
              modelCallId: '2.0',
              appliedIteration: 2,
            },
          },
        ],
        hasMore: false,
        oldestCursor: 'cursor-user',
        newestCursor: 'cursor-steer',
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
            messageId: 'user-old',
            role: 'user',
            text: 'original request',
            createdAt: '2026-07-06T00:00:00Z',
            turnContext: { turnId: 'turn-old' },
          },
          {
            id: 'steer-1',
            messageId: 'steer-1',
            role: 'user',
            text: 'use the new constraint',
            createdAt: '2026-07-06T00:00:01Z',
            turnContext: {
              turnId: 'turn-new',
              targetTurnId: 'turn-old',
              promotedTurnId: 'turn-new',
              promotedFromTurnId: 'turn-old',
              disposition: 'promoted',
              revision: 2,
            },
          },
          {
            id: 'assistant-old',
            messageId: 'assistant-old',
            role: 'assistant',
            text: 'completed old-turn output',
            createdAt: '2026-07-06T00:00:02Z',
            turnContext: { turnId: 'turn-old' },
          },
          {
            id: 'assistant-new',
            messageId: 'assistant-new',
            role: 'assistant',
            text: 'promoted follow-up output',
            createdAt: '2026-07-06T00:00:03Z',
            turnContext: { turnId: 'turn-new' },
          },
        ],
        hasMore: false,
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
    const { api, historyFixture, messages } = makeHistory(false)
    historyFixture
      .mockResolvedValueOnce({
        messages: [
          {
            id: 'assistant-old',
            messageId: 'assistant-old',
            role: 'assistant',
            text: 'completed old-turn output',
            createdAt: '2026-07-06T00:00:02Z',
            turnContext: { turnId: 'turn-old' },
          },
          {
            id: 'assistant-new',
            messageId: 'assistant-new',
            role: 'assistant',
            text: 'promoted follow-up output',
            createdAt: '2026-07-06T00:00:03Z',
            turnContext: { turnId: 'turn-new' },
          },
        ],
        hasMore: true,
        oldestCursor: 'cursor-assistant-old',
        newestCursor: 'cursor-assistant-new',
      })
      .mockResolvedValueOnce({
        messages: [
          {
            id: 'user-old',
            messageId: 'user-old',
            role: 'user',
            text: 'original request',
            createdAt: '2026-07-06T00:00:00Z',
            turnContext: { turnId: 'turn-old' },
          },
          {
            id: 'steer-1',
            messageId: 'steer-1',
            role: 'user',
            text: 'use the new constraint',
            createdAt: '2026-07-06T00:00:01Z',
            turnContext: {
              turnId: 'turn-new',
              promotedFromTurnId: 'turn-old',
              disposition: 'promoted',
              revision: 2,
            },
          },
        ],
        hasMore: false,
        oldestCursor: 'cursor-user-old',
        newestCursor: 'cursor-steer',
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
          messageId: 'assistant-plan',
          role: 'assistant',
          text: 'Legacy Markdown fallback',
          createdAt: '2026-07-06T00:00:00Z',
          toolCalls: [{
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
        hasMore: false,
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
      compactionId: 'cmp-early',
      triggerReason: 'manual',
      createdAt: Date.parse('2026-07-06T00:00:01.500Z'),
    }
    const lateSummary = {
      id: 22,
      compactionId: 'cmp-late',
      triggerReason: 'manual',
      createdAt: Date.parse('2026-07-06T00:00:03.500Z'),
    }
    Object.defineProperties(thread, {
      scrollHeight: { configurable: true, get: () => height },
      scrollTop: { configurable: true, value: 120, writable: true },
    })
    const threadRef = ref<HTMLElement | null>(thread)
    const { api, readHistory, historyFixture, messages } = makeHistory(false, {
      threadRef,
      response: {
        messages: [historyMessage('m3'), historyMessage('m4')],
        compactionSummaries: [lateSummary],
        hasMore: true,
        oldestCursor: 'cursor-3',
        newestCursor: 'cursor-4',
        canonicalComplete: true,
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
    historyFixture.mockImplementationOnce(async () => ({
      messages: [historyMessage('m3'), historyMessage('m4')],
      compactionSummaries: [lateSummary],
      hasMore: true,
      oldestCursor: 'cursor-3',
      newestCursor: 'cursor-4',
      canonicalComplete: true,
    })).mockImplementationOnce(async () => {
      // Simulate unrelated live-tail growth while the page request is in
      // flight. The visible durable message still moves by exactly 200px.
      height = 900
      return {
        messages: [historyMessage('m1'), historyMessage('m2')],
        compactionSummaries: [earlySummary, lateSummary],
        hasMore: false,
        oldestCursor: 'cursor-1',
        newestCursor: 'cursor-2',
        canonicalComplete: true,
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
    expect(readHistory).toHaveBeenCalledTimes(2)
    expect(api.historyState.value.canonicalComplete).toBe(true)
    expect(api.historyState.value.newestCursor).toBe('cursor-4')
    thread.remove()
  })

  it('queues a threshold crossing during latest-window refresh without consuming its cursor', async () => {
    let resolveRefresh!: (value: SessionReadHistoryPageFixture) => void
    const refresh = new Promise<SessionReadHistoryPageFixture>(resolve => { resolveRefresh = resolve })
    const { api, readHistory, historyFixture, messages } = makeHistory(false)
    historyFixture
      .mockResolvedValueOnce({
        messages: [historyMessage('m4')],
        hasMore: true,
        oldestCursor: 'cursor-4',
        newestCursor: 'cursor-4',
        canonicalAvailable: true,
      })
      .mockResolvedValueOnce({
        messages: [historyMessage('m3')],
        hasMore: true,
        oldestCursor: 'cursor-3',
        newestCursor: 'cursor-3',
        canonicalAvailable: true,
      })
      .mockImplementationOnce(() => refresh)
      .mockResolvedValueOnce({
        messages: [historyMessage('m2')],
        hasMore: false,
        oldestCursor: 'cursor-2',
        newestCursor: 'cursor-2',
        canonicalAvailable: true,
      })

    await api.loadHistory()
    await api.loadEarlierHistory()
    const refreshing = api.loadHistory()
    await vi.waitFor(() => expect(api.historyState.value.loading).toBe(true))

    api.loadEarlierHistory()
    resolveRefresh({
      messages: [historyMessage('m4'), historyMessage('m5')],
      hasMore: true,
      oldestCursor: 'cursor-4',
      newestCursor: 'cursor-5',
      canonicalAvailable: true,
    })
    await refreshing
    await vi.waitFor(() => expect(readHistory).toHaveBeenCalledTimes(4))

    expect(readHistory).toHaveBeenNthCalledWith(
      4,
      'before',
      'cursor-3',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    )
    await vi.waitFor(() => {
      expect(messages.value.map(message => message.messageId)).toEqual(['m2', 'm3', 'm4', 'm5'])
    })
  })

  it('does not apply an unavailable fallback page and retries the exact prepend boundary', async () => {
    const { api, readHistory, historyFixture, messages } = makeHistory(false)
    historyFixture
      .mockResolvedValueOnce({
        messages: [historyMessage('m4')],
        hasMore: true,
        oldestCursor: 'cursor-4',
        newestCursor: 'cursor-4',
        canonicalAvailable: true,
        canonicalComplete: true,
      })
      .mockResolvedValueOnce({
        messages: [historyMessage('fallback')],
        hasMore: false,
        oldestCursor: 'fallback-cursor',
        newestCursor: 'fallback-cursor',
        canonicalAvailable: false,
        canonicalComplete: false,
      })
      .mockResolvedValueOnce({
        messages: [historyMessage('m2'), historyMessage('m3')],
        hasMore: false,
        oldestCursor: 'cursor-2',
        newestCursor: 'cursor-3',
        canonicalAvailable: true,
        canonicalComplete: true,
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
    expect(readHistory).toHaveBeenNthCalledWith(
      3,
      'before',
      'cursor-4',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    )
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
        hasMore: true,
        oldestCursor: 'cursor-50',
        newestCursor: 'cursor-249',
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
    const latest: SessionReadMessageFixture[] = Array.from(
      { length: 199 },
      (_, index) => historyMessage(`m-${index + 500}`),
    )
    latest.push({
      id: 'live-user-server',
      messageId: 'live-user-server',
      role: 'user',
      text: 'still running',
      createdAt: '2026-07-06T01:00:00Z',
    })
    const { api, readHistory, historyFixture, messages } = makeHistory(false, { preserveLiveTail: true })
    historyFixture
      .mockResolvedValueOnce({
        messages: initial,
        hasMore: true,
        oldestCursor: 'cursor-250',
        newestCursor: 'cursor-299',
        canonicalAvailable: true,
      })
      .mockResolvedValueOnce({
        messages: earlier,
        hasMore: true,
        oldestCursor: 'cursor-200',
        newestCursor: 'cursor-249',
        canonicalAvailable: true,
      })
      .mockResolvedValueOnce({
        messages: latest,
        hasMore: true,
        oldestCursor: 'cursor-500',
        newestCursor: 'cursor-live',
        canonicalAvailable: true,
      })
      .mockResolvedValueOnce({
        messages: Array.from({ length: 200 }, (_, index) => historyMessage(`m-${index + 300}`)),
        hasMore: true,
        oldestCursor: 'cursor-300',
        newestCursor: 'cursor-499',
        canonicalAvailable: true,
      })
      .mockResolvedValueOnce({
        messages: latest,
        hasMore: false,
        oldestCursor: 'cursor-500',
        newestCursor: 'cursor-live',
        canonicalAvailable: true,
      })
      .mockResolvedValueOnce({
        messages: [historyMessage('m-199')],
        hasMore: true,
        oldestCursor: 'cursor-199',
        newestCursor: 'cursor-199',
        canonicalAvailable: true,
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
    expect(readHistory).toHaveBeenNthCalledWith(4, 'after', 'cursor-299', expect.objectContaining({
      limit: 200,
      signal: expect.any(AbortSignal),
    }))
    expect(readHistory).toHaveBeenNthCalledWith(5, 'after', 'cursor-499', expect.objectContaining({
      limit: 200,
      signal: expect.any(AbortSignal),
    }))

    await api.loadEarlierHistory()
    expect(readHistory).toHaveBeenNthCalledWith(
      6,
      'before',
      'cursor-200',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    )
  })

  it('bounds each disconnected forward bridge and resumes from the saved cursor', async () => {
    const { api, readHistory, historyFixture, messages } = makeHistory(false)
    historyFixture
      .mockResolvedValueOnce({
        messages: [historyMessage('m8')],
        hasMore: true,
        oldestCursor: 'cursor-8',
        newestCursor: 'cursor-8',
        canonicalAvailable: true,
      })
      .mockResolvedValueOnce({
        messages: [historyMessage('m7')],
        hasMore: true,
        oldestCursor: 'cursor-7',
        newestCursor: 'cursor-7',
        canonicalAvailable: true,
      })
      .mockResolvedValueOnce({
        messages: [historyMessage('m20')],
        hasMore: true,
        oldestCursor: 'cursor-20',
        newestCursor: 'cursor-20',
        canonicalAvailable: true,
      })
      .mockResolvedValueOnce({
        messages: [historyMessage('m9'), historyMessage('m10')],
        hasMore: true,
        oldestCursor: 'cursor-9',
        newestCursor: 'cursor-10',
        canonicalAvailable: true,
      })
      .mockResolvedValueOnce({
        messages: [historyMessage('m11'), historyMessage('m12')],
        hasMore: true,
        oldestCursor: 'cursor-11',
        newestCursor: 'cursor-12',
        canonicalAvailable: true,
      })
      .mockResolvedValueOnce({
        messages: [historyMessage('m20')],
        hasMore: true,
        oldestCursor: 'cursor-20',
        newestCursor: 'cursor-20',
        canonicalAvailable: true,
      })
      .mockResolvedValueOnce({
        messages: Array.from({ length: 8 }, (_, index) => historyMessage(`m${index + 13}`)),
        hasMore: false,
        oldestCursor: 'cursor-13',
        newestCursor: 'cursor-20',
        canonicalAvailable: true,
      })

    await api.loadHistory()
    await api.loadEarlierHistory()
    await api.loadHistory()

    expect(readHistory).toHaveBeenCalledTimes(5)
    expect(messages.value.map(message => message.messageId)).toEqual([
      'm7', 'm8', 'm9', 'm10', 'm11', 'm12',
    ])
    expect(api.historyState.value.newestCursor).toBe('cursor-12')

    await vi.waitFor(() => expect(readHistory).toHaveBeenCalledTimes(7))
    expect(readHistory).toHaveBeenNthCalledWith(
      7,
      'after',
      'cursor-12',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    )
    expect(messages.value.map(message => message.messageId)).toEqual([
      'm7', 'm8', 'm9', 'm10', 'm11', 'm12', 'm13', 'm14', 'm15', 'm16',
      'm17', 'm18', 'm19', 'm20',
    ])
  })

  it('keeps expanded history untouched when a forward bridge is unavailable', async () => {
    const { api, readHistory, historyFixture, messages } = makeHistory(false)
    historyFixture
      .mockResolvedValueOnce({
        messages: [historyMessage('m4')],
        hasMore: true,
        oldestCursor: 'cursor-4',
        newestCursor: 'cursor-4',
        canonicalAvailable: true,
      })
      .mockResolvedValueOnce({
        messages: [historyMessage('m3')],
        hasMore: true,
        oldestCursor: 'cursor-3',
        newestCursor: 'cursor-3',
        canonicalAvailable: true,
      })
      .mockResolvedValueOnce({
        messages: [historyMessage('m9')],
        hasMore: true,
        oldestCursor: 'cursor-9',
        newestCursor: 'cursor-9',
        canonicalAvailable: true,
      })
      .mockResolvedValueOnce({
        messages: [historyMessage('fallback')],
        hasMore: false,
        oldestCursor: 'fallback-cursor',
        newestCursor: 'fallback-cursor',
        canonicalAvailable: false,
        canonicalComplete: false,
      })
      .mockResolvedValueOnce({
        messages: [historyMessage('m9')],
        hasMore: true,
        oldestCursor: 'cursor-9',
        newestCursor: 'cursor-9',
        canonicalAvailable: true,
      })
      .mockResolvedValueOnce({
        messages: Array.from({ length: 5 }, (_, index) => historyMessage(`m${index + 5}`)),
        hasMore: false,
        oldestCursor: 'cursor-5',
        newestCursor: 'cursor-9',
        canonicalAvailable: true,
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
    expect(readHistory).toHaveBeenNthCalledWith(
      6,
      'after',
      'cursor-4',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    )
  })

  it('stops a forward bridge when its cursor does not advance', async () => {
    const { api, readHistory, historyFixture, messages } = makeHistory(false)
    historyFixture
      .mockResolvedValueOnce({
        messages: [historyMessage('m4')],
        hasMore: true,
        oldestCursor: 'cursor-4',
        newestCursor: 'cursor-4',
        canonicalAvailable: true,
      })
      .mockResolvedValueOnce({
        messages: [historyMessage('m3')],
        hasMore: true,
        oldestCursor: 'cursor-3',
        newestCursor: 'cursor-3',
        canonicalAvailable: true,
      })
      .mockResolvedValueOnce({
        messages: [historyMessage('m9')],
        hasMore: true,
        oldestCursor: 'cursor-9',
        newestCursor: 'cursor-9',
        canonicalAvailable: true,
      })
      .mockResolvedValueOnce({
        messages: [historyMessage('m5')],
        hasMore: true,
        oldestCursor: 'cursor-5',
        newestCursor: 'cursor-4',
        canonicalAvailable: true,
      })

    await api.loadHistory()
    await api.loadEarlierHistory()
    await api.loadHistory()

    expect(readHistory).toHaveBeenCalledTimes(4)
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
    const { api, readHistory, historyFixture } = makeHistory(false, {
      response: {
        messages: [historyMessage('m2')],
        hasMore: true,
        oldestCursor: 'cursor-2',
      },
    })
    historyFixture
      .mockResolvedValueOnce({
        messages: [historyMessage('m2')],
        hasMore: true,
        oldestCursor: 'cursor-2',
      })
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce({
        messages: [historyMessage('m1')],
        hasMore: false,
        oldestCursor: 'cursor-1',
      })

    await api.loadHistory()
    await api.loadEarlierHistory()
    expect(api.historyState.value.loadEarlierError).toBe(true)

    await api.loadEarlierHistory()
    expect(api.historyState.value.loadEarlierError).toBe(false)
    expect(readHistory).toHaveBeenCalledTimes(3)
  })

  it('surfaces and retries an initial history request failure', async () => {
    const { api, historyFixture, messages } = makeHistory(false)
    historyFixture
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce({
        messages: [historyMessage('m1')],
        hasMore: false,
        oldestCursor: 'cursor-1',
        canonicalAvailable: true,
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
    const { api, historyFixture, messages } = makeHistory(false)
    let rejectHistory!: (reason: Error) => void
    historyFixture.mockReturnValueOnce(new Promise<SessionReadHistoryPageFixture>((_resolve, reject) => {
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
    const { api, readHistory, historyFixture, messages } = makeHistory(false)
    historyFixture
      .mockResolvedValueOnce({
        messages: [historyMessage('fallback')],
        hasMore: false,
        oldestCursor: null,
        canonicalAvailable: false,
        canonicalComplete: false,
      })
      .mockResolvedValueOnce({
        messages: [historyMessage('canonical')],
        hasMore: false,
        oldestCursor: null,
        canonicalAvailable: true,
        canonicalComplete: true,
      })

    await api.loadHistory()
    expect(api.historyState.value.canonicalAvailable).toBe(false)
    expect(api.historyState.value.loadingEarlier).toBe(false)
    expect(api.historyState.value.initialLoadStatus).toBe('ready')
    expect(messages.value.map(message => message.messageId)).toEqual(['fallback'])

    await api.retryHistory()
    expect(api.historyState.value.canonicalAvailable).toBe(true)
    expect(readHistory).toHaveBeenCalledTimes(2)
  })

  it('marks an empty unavailable canonical reader as an initial retriable failure', async () => {
    const { api } = makeHistory(false, {
      response: {
        messages: [],
        hasMore: false,
        oldestCursor: null,
        canonicalAvailable: false,
        canonicalComplete: false,
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
        hasMore: false,
        oldestCursor: null,
        canonicalAvailable: false,
        canonicalComplete: true,
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
        hasMore: false,
        oldestCursor: null,
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
        hasMore: false,
        oldestCursor: null,
        canonicalAvailable: false,
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
    let resolveOld!: (value: SessionReadHistoryPageFixture) => void
    const oldResponse = new Promise<SessionReadHistoryPageFixture>(resolve => { resolveOld = resolve })
    const { api, readHistory, historyFixture, messages } = makeHistory(false, {
      sessionKey,
      messages: [{
        role: 'assistant',
        text: 'old loaded row',
        ts: 'old',
        messageId: 'old-loaded',
        restoredFromHistory: true,
      }],
    })
    historyFixture
      .mockImplementationOnce(() => oldResponse)
      .mockResolvedValueOnce({
        messages: [historyMessage('new-message')],
        hasMore: false,
        oldestCursor: null,
      })

    const oldLoad = api.loadHistory()
    await vi.waitFor(() => expect(readHistory).toHaveBeenCalledTimes(1))
    sessionKey.value = 'agent:main:webchat:new'
    const newLoad = api.loadHistory()
    await newLoad
    resolveOld({
      messages: [historyMessage('old-message')],
      hasMore: false,
      oldestCursor: null,
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
      const { api, readHistory } = makeHistory(false, { sessionKey })

      api.scheduleHistorySync()
      api.cancelActiveHistory()
      sessionKey.value = 'agent:main:webchat:new-draft'
      await vi.advanceTimersByTimeAsync(50)

      expect(readHistory).not.toHaveBeenCalled()
      expect(readHistory).not.toHaveBeenCalled()
    } finally {
      vi.useRealTimers()
    }
  })

  it('keeps the new session loading when a stale request fails first', async () => {
    const sessionKey = ref('agent:main:webchat:old')
    let rejectOld!: (reason: Error) => void
    let resolveNew!: (value: SessionReadHistoryPageFixture) => void
    const oldResponse = new Promise<SessionReadHistoryPageFixture>((_resolve, reject) => {
      rejectOld = reject
    })
    const newResponse = new Promise<SessionReadHistoryPageFixture>(resolve => {
      resolveNew = resolve
    })
    const { api, readHistory, historyFixture, messages } = makeHistory(false, { sessionKey })
    historyFixture
      .mockImplementationOnce(() => oldResponse)
      .mockImplementationOnce(() => newResponse)

    const oldLoad = api.loadHistory()
    await vi.waitFor(() => expect(readHistory).toHaveBeenCalledTimes(1))
    sessionKey.value = 'agent:main:webchat:new'
    const newLoad = api.loadHistory()
    await vi.waitFor(() => expect(readHistory).toHaveBeenCalledTimes(2))

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
      hasMore: false,
      oldestCursor: null,
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

  it('keeps the visible durable message anchored across a recovery refresh', async () => {
    const harness = makeReaderAnchorRecovery()

    try {
      await harness.api.loadHistory()
      await nextTick()

      expect(harness.thread.scrollTop).toBe(1_000)
    } finally {
      harness.cleanup()
    }
  })

  it('does not restore a reader anchor during application-owned history navigation', async () => {
    const navigationActive = ref(false)
    const harness = makeReaderAnchorRecovery({
      canApplyViewportCorrection: () => !navigationActive.value,
      onCommit: () => { navigationActive.value = true },
    })

    try {
      await harness.api.loadHistory()
      await nextTick()

      expect(harness.thread.scrollTop).toBe(0)
    } finally {
      harness.cleanup()
    }
  })

  it('stops late anchor stabilization when application-owned navigation starts', async () => {
    const navigationActive = ref(false)
    const harness = makeReaderAnchorRecovery({
      canApplyViewportCorrection: () => !navigationActive.value,
      pendingImage: true,
    })

    try {
      await harness.api.loadHistory()
      await nextTick()
      expect(harness.thread.scrollTop).toBe(1_000)

      navigationActive.value = true
      harness.setMessageContentTop(1_140)
      harness.emitPendingImageLoad()
      await Promise.resolve()

      expect(harness.thread.scrollTop).toBe(1_000)
    } finally {
      harness.cleanup()
    }
  })

  it('keeps live-edge ownership when a recovery refresh resets layout scroll', async () => {
    const harness = makeLiveEdgeRecovery({
      onCommit: ({ autoScroll, thread }) => {
        // Model the long-history virtualizer clamping the old viewport before
        // its replacement rows are measured.
        thread.scrollTop = 0
        autoScroll.value = false
      },
    })

    try {
      await harness.api.loadHistory()
      await nextTick()
      await nextTick()

      expect(harness.autoScroll.value).toBe(true)
      expect(harness.thread.scrollTop).toBe(12_000)
    } finally {
      harness.cleanup()
    }
  })

  it('cancels a stale live-edge correction when the viewport epoch changes', async () => {
    const scrollEpoch = ref(1)
    const harness = makeLiveEdgeRecovery({
      scrollEpoch,
      onCommit: ({ autoScroll, thread }) => {
        thread.scrollTop = 0
        autoScroll.value = false
        scrollEpoch.value += 1
      },
    })

    try {
      const outcome = await harness.api.loadHistory()
      await nextTick()

      expect(outcome).toMatchObject({ ok: false, cancelled: true })
      expect(harness.autoScroll.value).toBe(false)
      expect(harness.thread.scrollTop).toBe(0)
    } finally {
      harness.cleanup()
    }
  })

  it('does not reclaim the live edge after fresh reader input', async () => {
    const harness = makeLiveEdgeRecovery({
      onCommit: ({ autoScroll, thread }) => {
        thread.scrollTop = 6_000
        autoScroll.value = false
        // A real input event cannot interleave the synchronous message commit.
        // Queue it immediately afterwards, while the layout handoff is waiting
        // for Vue's next DOM flush.
        queueMicrotask(() => thread.dispatchEvent(new Event('wheel')))
      },
    })

    try {
      const outcome = await harness.api.loadHistory()
      await nextTick()

      expect(outcome).toMatchObject({ ok: true })
      expect(harness.autoScroll.value).toBe(false)
      expect(harness.thread.scrollTop).toBe(6_000)
    } finally {
      harness.cleanup()
    }
  })

  it('does not reclaim the live edge during application-owned history navigation', async () => {
    const navigationActive = ref(false)
    const harness = makeLiveEdgeRecovery({
      canApplyViewportCorrection: () => !navigationActive.value,
      onCommit: ({ autoScroll, thread }) => {
        navigationActive.value = true
        thread.scrollTop = 6_000
        autoScroll.value = false
      },
    })

    try {
      const outcome = await harness.api.loadHistory()
      await nextTick()

      expect(outcome).toMatchObject({ ok: true })
      expect(harness.autoScroll.value).toBe(false)
      expect(harness.thread.scrollTop).toBe(6_000)
    } finally {
      harness.cleanup()
    }
  })

  it('drops a delayed prepend when the reused chat viewport enters a new epoch', async () => {
    let resolveEarlier!: (value: SessionReadHistoryPageFixture) => void
    const earlier = new Promise<SessionReadHistoryPageFixture>(resolve => { resolveEarlier = resolve })
    const epoch = ref(1)
    const thread = document.createElement('div')
    Object.defineProperties(thread, {
      clientHeight: { configurable: true, value: 300 },
      scrollHeight: { configurable: true, value: 900 },
      scrollTop: { configurable: true, value: 120, writable: true },
    })
    const threadRef = ref<HTMLElement | null>(thread)
    const { api, readHistory, historyFixture } = makeHistory(false, {
      scrollEpoch: epoch,
      threadRef,
      response: {
        messages: [historyMessage('m2')],
        hasMore: true,
        oldestCursor: 'cursor-2',
        newestCursor: 'cursor-2',
      },
    })
    historyFixture
      .mockResolvedValueOnce({
        messages: [historyMessage('m2')],
        hasMore: true,
        oldestCursor: 'cursor-2',
        newestCursor: 'cursor-2',
      })
      .mockImplementationOnce(() => earlier)

    await api.loadHistory()
    const pending = api.loadEarlierHistory()
    await vi.waitFor(() => expect(readHistory).toHaveBeenCalledTimes(2))
    epoch.value = 2
    resolveEarlier({
      messages: [historyMessage('m1')],
      hasMore: false,
      oldestCursor: 'cursor-1',
      newestCursor: 'cursor-2',
    })
    await pending

    expect(thread.scrollTop).toBe(120)
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
          messageId: 'literal-1',
          role: 'assistant',
          text,
          createdAt: '2026-07-06T00:00:00Z',
        }],
        hasMore: false,
        oldestCursor: null,
        newestCursor: null,
        scope: 'session',
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
        hasMore: false,
        oldestCursor: null,
        newestCursor: null,
        scope: 'session',
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
            messageId: 'user-1',
            role: 'user',
            text: 'stop immediately',
            createdAt: '2026-07-07T10:00:00Z',
            turnContext: { turnId: 'turn-1' },
          },
        ],
        turnOutcomes: [{
          turnId: 'turn-1',
          taskId: 'task-1',
          status: 'cancelled',
          startedAt: 1_000,
          finishedAt: 2_000,
          accepted_routing_mode: 'ensemble',
          outcome: {
            kind: 'cancelled',
            cancellation_source: 'webui_stop',
          },
        }],
        hasMore: false,
        oldestCursor: null,
        newestCursor: null,
        scope: 'session',
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

  it('restores terminal activity without an assistant transcript row', async () => {
    const onTerminalTask = vi.fn()
    const { api, messages } = makeHistory(true, {
      onTerminalTask,
      response: {
        messages: [{
          id: 'user-cancelled',
          messageId: 'user-cancelled',
          role: 'user',
          text: 'stop after the retry',
          createdAt: '2026-07-07T10:00:00Z',
          turnContext: { turnId: 'turn-cancelled' },
        }],
        turnOutcomes: [{
          turnId: 'turn-cancelled',
          taskId: 'task-cancelled',
          status: 'cancelled',
          finishedAt: 2_000,
          activitySnapshot: {
            version: 2,
            taskId: 'task-cancelled',
            turnId: 'turn-cancelled',
            complete: true,
            reasoning_utf16_length: 0,
            entries: [
              {
                type: 'phase', id: 'provider:requesting:1', order: 1,
                kind: 'provider', phase: 'requesting', at: 1_000, ended_at: 1_200,
              },
              {
                type: 'phase', id: 'provider:retry_wait:2', order: 2,
                kind: 'provider', phase: 'retry_wait', reason: 'rate_limited',
                retryAfterMs: 500, at: 1_200, ended_at: 2_000,
              },
            ],
          },
          outcome: { kind: 'cancelled', cancellation_source: 'webui_stop' },
        }],
        hasMore: false,
        canonicalComplete: true,
      },
    })

    await api.loadHistory()

    expect(messages.value.map(message => message.role)).toEqual(['user', 'assistant'])
    expect(messages.value[1]).toMatchObject({
      text: '',
      turnId: 'turn-cancelled',
      messageId: 'terminal-activity:task-cancelled',
      activitySnapshot: { version: 2, complete: true },
      activitySnapshotIncomplete: false,
      statusHistory: [
        expect.objectContaining({ action: 'provider:requesting', activityOrder: 1 }),
        expect.objectContaining({ action: 'provider:rate_limited:1', activityOrder: 2 }),
      ],
    })
    expect(onTerminalTask).toHaveBeenCalledWith(expect.objectContaining({
      taskId: 'task-cancelled',
      status: 'cancelled',
    }))
  })

  it('restores usage barrier activity and its retryable error from terminal history', async () => {
    const { api, messages } = makeHistory(true, {
      response: {
        messages: [
          {
            id: 'user-usage',
            messageId: 'user-usage',
            role: 'user',
            text: 'retry this turn',
            createdAt: '2026-07-07T10:00:00Z',
            turnContext: { turnId: 'turn-usage' },
          },
          {
            id: 'system-usage',
            messageId: 'system-usage',
            role: 'system',
            text: 'Error: usage ledger unavailable',
            createdAt: '2026-07-07T10:00:01Z',
            turnContext: { turnId: 'turn-usage' },
          },
        ],
        turnOutcomes: [{
          turnId: 'turn-usage',
          taskId: 'turn-usage',
          status: 'failed',
          errorClass: 'usage_accounting_busy',
          retryable: true,
          usageCallIndex: 1,
          noPriorProviderDispatch: true,
          replaySafe: true,
          userMessageId: 'user-usage',
          terminalMessage: 'server fallback',
          activitySnapshot: {
            version: 1,
            taskId: 'turn-usage',
            turnId: 'turn-usage',
            phases: [
              { kind: 'router', phase: 'decided', at: 1_000 },
              { kind: 'state', phase: 'thinking', at: 1_100 },
            ],
          },
          outcome: {
            kind: 'blocked',
            reason: 'usage_accounting_busy',
            errorClass: 'usage_accounting_busy',
            retryable: true,
            usageCallIndex: 1,
            noPriorProviderDispatch: true,
            replaySafe: true,
            userMessageId: 'user-usage',
          },
        }],
        hasMore: false,
        oldestCursor: null,
        newestCursor: null,
        scope: 'session',
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
      messageId: 'assistant-activity-v2',
      role: 'assistant' as const,
      text: 'Final answer.',
      reasoningContent: ' A😀 ',
      createdAt: '2026-07-07T10:00:01Z',
      turnContext: { turnId: 'turn-activity-v2' },
      toolCalls: [
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
        startedAt: 6_000, ended_at: 8_000, status: 'completed',
        content_kind: 'reasoning', text_start_utf16: 0, text_end_utf16: 5,
      },
      {
        type: 'segment', id: 'text:0', order: 31, segment_type: 'text',
        text_index: 0, text_utf16_length: 8, at: 31_000, ended_at: 32_000,
      },
      {
        type: 'segment', id: 'tool:tool-1', order: 41, segment_type: 'tool',
        tool_use_id: 'tool-1', name: 'skill_view', startedAt: 41_000,
        ended_at: 42_000, is_error: false,
      },
      {
        type: 'segment', id: 'text:1', order: 50, segment_type: 'text',
        text_index: 1, text_utf16_length: 13, at: 50_000, ended_at: 51_000,
      },
    ]
    const outcome = {
      turnId: 'turn-activity-v2',
      taskId: 'turn-activity-v2',
      status: 'succeeded',
      activitySnapshot: {
        version: 2,
        taskId: 'turn-activity-v2',
        turnId: 'turn-activity-v2',
        complete: true,
        reasoning_utf16_length: 5,
        entries,
      },
    }
    const complete = makeHistory(false, {
      response: {
        messages: [assistantMessage],
        turnOutcomes: [outcome],
        hasMore: false,
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
        turnOutcomes: [{
          ...outcome,
          activitySnapshot: {
            ...outcome.activitySnapshot,
            entries: entries.map(entry => entry.id === 'text:1'
              ? { ...entry, text_utf16_length: 12 }
              : entry),
          },
        }],
        hasMore: false,
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
            messageId: 'user-image',
            role: 'user',
            text: 'inspect this image',
            createdAt: '2026-07-07T10:00:00Z',
            turnContext: { turnId: 'turn-image' },
          },
          {
            id: 'system-image',
            messageId: 'system-image',
            role: 'system',
            text: 'Error: server fallback [synthetic ref]',
            createdAt: '2026-07-07T10:00:01Z',
            turnContext: { turnId: 'turn-image' },
          },
        ],
        turnOutcomes: [{
          turnId: 'turn-image',
          taskId: 'turn-image',
          status: 'failed',
          errorClass: errorClass,
          retryable: false,
          terminalMessage: 'server fallback',
        }],
        hasMore: false,
        oldestCursor: null,
        newestCursor: null,
        scope: 'session',
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
          messageId: 'user-usage',
          role: 'user',
          text: 'retry this turn',
          createdAt: '2026-07-07T10:00:00Z',
          turnContext: { turnId: 'turn-usage' },
        }],
        turnOutcomes: [{
          turnId: 'turn-usage',
          taskId: 'task-usage',
          status: 'failed',
          finishedAt: 2_000,
          errorClass: 'usage_accounting_unavailable',
          retryable: true,
          usageCallIndex: 1,
          noPriorProviderDispatch: true,
          replaySafe: true,
          userMessageId: 'user-usage',
          terminalMessage: 'server fallback',
          activitySnapshot: {
            version: 1,
            taskId: 'turn-usage',
            turnId: 'turn-usage',
            phases: [{ kind: 'router', phase: 'decided', at: 1_000 }],
          },
        }],
        hasMore: false,
        oldestCursor: null,
        newestCursor: null,
        scope: 'session',
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
          messageId: 'user-usage',
          role: 'user',
          text: 'retry this turn',
          createdAt: '2026-07-07T10:00:00Z',
          turnContext: { turnId: 'turn-usage' },
        }],
        turnOutcomes: [{
          turnId: 'turn-usage',
          taskId: 'task-usage',
          status: 'failed',
          errorClass: 'usage_accounting_busy',
          retryable: true,
          usageCallIndex: 1,
          noPriorProviderDispatch: true,
          replaySafe: true,
        }],
        hasMore: false,
        oldestCursor: null,
        newestCursor: null,
        scope: 'session',
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
          messageId: 'user-usage',
          role: 'user',
          text: 'continue after tools',
          createdAt: '2026-07-07T10:00:00Z',
          turnContext: { turnId: 'turn-usage' },
        }],
        turnOutcomes: [{
          turnId: 'turn-usage',
          taskId: 'task-usage',
          status: 'failed',
          errorClass: 'usage_accounting_busy',
          retryable: true,
          usageCallIndex: 2,
          noPriorProviderDispatch: false,
          replaySafe: false,
          outcome: {
            kind: 'blocked',
            reason: 'usage_accounting_busy',
            errorClass: 'usage_accounting_busy',
            retryable: true,
            usageCallIndex: 2,
            noPriorProviderDispatch: false,
            replaySafe: false,
          },
        }],
        hasMore: false,
        oldestCursor: null,
        newestCursor: null,
        scope: 'session',
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
    const { api, historyFixture, messages } = makeHistory(true)
    const outcome = {
      turnId: 'turn-usage',
      taskId: 'task-usage',
      status: 'failed',
      errorClass: 'usage_accounting_busy',
      retryable: true,
      usageCallIndex: 1,
      noPriorProviderDispatch: true,
      replaySafe: true,
    }
    historyFixture
      .mockResolvedValueOnce({
        messages: [{
          id: 'system-usage',
          messageId: 'system-usage',
          role: 'system',
          text: 'Error: usage ledger busy',
          createdAt: '2026-07-07T10:00:01Z',
          turnContext: { turnId: 'turn-usage' },
        }],
        turnOutcomes: [outcome],
        hasMore: true,
        oldestCursor: 'cursor-system',
        newestCursor: 'cursor-system',
        scope: 'session',
      })
      .mockResolvedValueOnce({
        messages: [{
          id: 'user-usage',
          messageId: 'user-usage',
          role: 'user',
          text: 'retry this turn',
          createdAt: '2026-07-07T10:00:00Z',
          turnContext: { turnId: 'turn-usage' },
        }],
        turnOutcomes: [outcome],
        hasMore: false,
        oldestCursor: 'cursor-user',
        newestCursor: 'cursor-user',
        scope: 'session',
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
    const pendingResponse: SessionReadHistoryPageFixture = {
      messages: [{
        id: 'user-usage',
        messageId: 'user-usage',
        role: 'user',
        text: 'retry this turn',
        createdAt: '2026-07-07T10:00:00Z',
        turnContext: { turnId: 'turn-usage' },
      }],
      hasMore: false,
      oldestCursor: null,
      newestCursor: null,
      scope: 'session',
    }
    const { api, historyFixture, messages } = makeHistory(true, {
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

    historyFixture.mockResolvedValueOnce({
      ...pendingResponse,
      messages: [
        ...(pendingResponse.messages || []),
        {
          id: 'system-usage',
          messageId: 'system-usage',
          role: 'system',
          text: 'Error: usage ledger unavailable',
          createdAt: '2026-07-07T10:00:01Z',
          turnContext: { turnId: 'turn-usage' },
        },
      ],
      turnOutcomes: [{
        turnId: 'turn-usage',
        taskId: 'turn-usage',
        status: 'failed',
        errorClass: 'usage_accounting_busy',
        retryable: true,
        usageCallIndex: 1,
        noPriorProviderDispatch: true,
        replaySafe: true,
        activitySnapshot: {
          version: 1,
          taskId: 'turn-usage',
          turnId: 'turn-usage',
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
            messageId: 'server-user',
            role: 'user',
            text: 'retry this turn',
            createdAt: 'server-user',
          },
        ],
        hasMore: false,
        oldestCursor: null,
        newestCursor: null,
        scope: 'session',
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
            messageId: 'server-user-1',
            role: 'user',
            text: prompt,
            createdAt: 'server-1',
          },
          {
            id: 'server-user-2',
            messageId: 'server-user-2',
            role: 'user',
            text: prompt,
            createdAt: 'server-2',
          },
          {
            id: 'server-user-3',
            messageId: 'server-user-3',
            role: 'user',
            text: prompt,
            createdAt: 'server-3',
          },
        ],
        hasMore: false,
        oldestCursor: null,
        newestCursor: null,
        scope: 'session',
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

  const canonicalTurn = (turnId = 'turn-current'): SessionReadHistoryPageFixture => ({
    messages: [
      {
        id: `user-${turnId}`,
        messageId: `user-${turnId}`,
        role: 'user',
        text: `question ${turnId}`,
        createdAt: '2026-07-07T10:00:00Z',
        turnContext: { turnId: turnId },
      },
      {
        id: `assistant-${turnId}`,
        messageId: `assistant-${turnId}`,
        role: 'assistant',
        text: `answer ${turnId}`,
        createdAt: '2026-07-07T10:00:01Z',
        turnContext: { turnId: turnId },
      },
    ],
    hasMore: false,
    canonicalAvailable: true,
    canonicalComplete: true,
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
      messageId: 'router-canonical',
      role: 'router',
      text: '',
      createdAt: '2026-07-07T10:00:00.750Z',
      turnContext: { turnId: 'turn-current' },
      routerDecision: {
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
      messageId: 'router-adjacent',
      role: 'router',
      text: '',
      createdAt: '2026-07-07T10:01:00.500Z',
      turnContext: { turnId: 'turn-adjacent' },
      routerDecision: {
        tier: 'c1',
        model: 'openai/gpt-5.4-mini',
        source: 'squilla_router',
      },
    })
    const { api, historyFixture, messages } = makeHistory(false, {
      messages: [acceptedRouter('turn-current'), acceptedRouter(undefined)],
    })
    historyFixture
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
      let resolveOld!: (value: SessionReadHistoryPageFixture) => void
      let resolveSafe!: (value: SessionReadHistoryPageFixture) => void
      const oldResponse = new Promise<SessionReadHistoryPageFixture>(resolve => { resolveOld = resolve })
      const safeResponse = new Promise<SessionReadHistoryPageFixture>(resolve => { resolveSafe = resolve })
      const durableA: SessionReadMessageFixture[] = [
        {
          id: 'user-a',
          messageId: 'user-a',
          role: 'user',
          text: 'prompt A',
          createdAt: '2026-07-06T00:00:00Z',
          turnContext: { turnId: 'turn-a' },
        },
        {
          id: 'assistant-a',
          messageId: 'assistant-a',
          role: 'assistant',
          text: 'answer A',
          createdAt: '2026-07-06T00:00:01Z',
          turnContext: { turnId: 'turn-a' },
        },
      ]
      const durableAB: SessionReadMessageFixture[] = [
        ...durableA,
        {
          id: 'user-b',
          messageId: 'user-b',
          role: 'user',
          text: 'prompt B',
          createdAt: '2026-07-06T00:00:02Z',
          turnContext: { turnId: 'turn-b' },
        },
        {
          id: 'assistant-b',
          messageId: 'assistant-b',
          role: 'assistant',
          text: 'answer B',
          createdAt: '2026-07-06T00:00:03Z',
          turnContext: { turnId: 'turn-b' },
        },
      ]
      const { api, readHistory, historyFixture, messages } = makeHistory(false, {
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
      historyFixture
        .mockImplementationOnce(() => oldResponse)
        .mockImplementationOnce(() => safeResponse)
        .mockResolvedValueOnce({ messages: durableA, hasMore: false })

      const oldLoad = api.loadHistory()
      await Promise.resolve()
      expect(readHistory).toHaveBeenCalledTimes(1)

      api.scheduleHistorySync(true)
      await vi.advanceTimersByTimeAsync(50)
      expect(readHistory).toHaveBeenCalledTimes(1)

      resolveOld({ messages: durableA, hasMore: false })
      await oldLoad
      expect(messages.value.map(message => message.text)).toEqual([
        'prompt A',
        'answer A',
        'prompt B',
        'answer B in progress',
      ])

      await vi.advanceTimersByTimeAsync(50)
      expect(readHistory).toHaveBeenCalledTimes(2)
      expect(readHistory.mock.calls[1]?.[2]).toMatchObject({
        signal: expect.any(AbortSignal),
        budgetMs: expect.any(Number),
        deadlineAt: expect.any(Number),
      })
      resolveSafe({ messages: durableAB, hasMore: false })
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
      expect(readHistory).toHaveBeenCalledTimes(3)
      expect(readHistory.mock.calls[2]?.[2]).toMatchObject({
        signal: expect.any(AbortSignal),
        budgetMs: expect.any(Number),
        deadlineAt: expect.any(Number),
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
      const { api, readHistory, historyFixture, messages } = makeHistory(false, {
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

      historyFixture.mockRejectedValueOnce(new RpcTimeoutError('chat.history', 1_000))
      api.scheduleHistorySync(true)
      await vi.advanceTimersByTimeAsync(50)
      await Promise.resolve()
      await Promise.resolve()

      expect(readHistory).toHaveBeenCalledTimes(2)
      expect(readHistory.mock.calls[1]?.[2]).toMatchObject({
        signal: expect.any(AbortSignal),
        budgetMs: expect.any(Number),
        deadlineAt: expect.any(Number),
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
      expect(readHistory).toHaveBeenCalledTimes(2)

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
      historyFixture.mockResolvedValueOnce({
        messages: [historyMessage('m1')],
        hasMore: false,
      })

      await api.loadHistory()

      expect(readHistory.mock.calls[2]?.[2]).toMatchObject({
        signal: expect.any(AbortSignal),
        budgetMs: expect.any(Number),
        deadlineAt: expect.any(Number),
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
