import { computed, onScopeDispose, ref, watch, type Ref } from 'vue'
import type {
  ChatMessage,
  ChatPendingItem,
  ChatRunStatus,
  ChatRunStatusSource,
  ChatUsagePayload,
} from '@/types/chat'
import type {
  AnswerGenerationResetPayload,
  ArtifactPayload,
  CompactionPayload,
  CronResultPayload,
  EnsembleProgressPayload,
  InputDispositionPayload,
  ProviderActivityPayload,
  RouterDecisionPayload,
  SessionDonePayload,
  SessionEventPayload,
  SessionMessagesSnapshotResponse,
  StreamEventEnvelope,
  SubagentCompletionPayload,
  TextDeltaPayload,
  ToolDeltaPayload,
  ToolEndPayload,
  ToolResultPayload,
  ToolUsePayload,
  WarningPayload,
} from '@/types/rpc'
import type { ChatRpcSubscriptionHandlers } from '@/composables/chat/useChatRpcSubscriptions'
import {
  isAuthoritativeSessionSubscription,
  type SessionSubscriptionOutcome,
} from '@/composables/chat/useChatSessionSubscription'
import type { SessionBootstrapRun } from '@/composables/chat/useChatSessionBootstrap'
import type { FrameInput, ReasoningBlock } from '@/types/turnlog'
import type { StatusPart } from '@/types/parts'
import type { FoldLiveTurnMode } from '@/composables/chat/useChatTurnLog'
import type { ChatTaskOwnershipApi } from '@/composables/chat/useChatTaskOwnership'
import { chatTaskId } from '@/composables/chat/useChatTaskOwnership'
import {
  FINISHED_STREAM_TASK_ID,
  PENDING_STREAM_TASK_ID,
  STOPPED_STREAM_TASK_ID,
  acceptStreamSeq as decideStreamSeq,
  activeTaskGroupRunState as buildActiveTaskGroupRunState,
  isCurrentSessionPayload as payloadIsCurrentSession,
  isCurrentTaskPayload as payloadIsCurrentTask,
  isStaleEpoch as payloadIsStaleEpoch,
  payloadTaskId,
  sessionChangeIsTerminal as payloadSessionChangeIsTerminal,
  sessionErrorMessage as eventSessionErrorMessage,
  taskGroupId as eventTaskGroupId,
  taskTerminalAsSessionEvent as normalizeTaskTerminalEvent,
  taskTerminalStatus as eventTaskTerminalStatus,
} from '@/utils/chat/streamEvents'
import { localizedChatErrorMessage } from '@/utils/chat/errors'
import { normalizeTurnOutcome } from '@/utils/chat/turnOutcome'
import { usageAccountingErrorCode } from '@/utils/chat/usageAccountingFailure'
import {
  useChatSteerDelivery,
  type ChatSteerDeliveryApi,
} from './useChatSteerDelivery'

export interface ChatUsageAccumulator {
  input: number
  output: number
  cacheRead: number
  cacheWrite: number
  cost: number | null
  routedTurns: number
  sessionSaved: number
}

export interface ChatRpcStreamApi {
  isStreaming: Ref<boolean>
  streamBubble: Ref<boolean>
  streamHasVisibleOutput: Ref<boolean>
  startStreaming: () => void
  endStreaming: (opts?: { reason?: string, suppressed?: boolean }) => void
  checkpointForUserMessage?: (turnId: string) => void
  appendDelta: (text: string, presentation?: 'intermediate' | 'answer') => void
  scheduleRender: () => void
  appendToolCall: (payload: ToolUsePayload) => void
  appendToolDelta: (payload: ToolDeltaPayload) => void
  appendToolEnd?: (payload: ToolEndPayload) => void
  appendToolResult: (payload: ToolResultPayload) => void
  appendArtifact: (payload: ArtifactPayload) => void
  reconcileFinalText: (finalText: string | null | undefined) => void
  resetLiveTurnState?: () => void
  resetAnswerGeneration?: (options?: {
    textSnapshot?: string
    preserveCompletedTools?: boolean
  }) => void
  setAssistantMessageId?: (messageId: string) => void
  resetStreamIdleTimer: (opts?: { progress?: boolean }) => void
  clearStreamIdleTimer: () => void
  setStreamActivity: (label: string, key?: string) => void
  restoreStatusHistory?: (entries: readonly StatusPart[]) => void
  recordCompactionActivity?: (payload: CompactionPayload) => void
  showThinkingIndicator: () => void
  hideThinkingIndicator: () => void
  // live-turn shadow log: the thinking ref lives here, so this composable appends
  // its own thinking frames into the stream-owned log after the legacy mutation.
  appendFrame: (frame: FrameInput) => void
  noteReasoningPresentationDelta?: (text: string) => void
  useReducer: Ref<FoldLiveTurnMode>
  getThinkingText?: () => string
}

type ChatCompactionPlacement = 'activity' | 'standalone'
type ChatCompactionPresentationResult = boolean | ChatCompactionPlacement | void

export interface UseChatRpcEventHandlersOptions {
  sessionKey: Ref<string>
  currentEpoch: Ref<number>
  lastStreamSeq: Ref<number>
  observeStreamGeneration?: (payload: unknown) => boolean
  activeTaskGroups: Ref<Set<string>>
  taskOwnership?: ChatTaskOwnershipApi
  // Task id of the turn whose output the live stream is currently rendering.
  // Empty = unknown (no guard). Set when a fresh turn starts (see useChatSend)
  // and on task.running; reset on session switch.
  activeStreamTaskId: Ref<string>
  aborted: Ref<boolean>
  messages: Ref<ChatMessage[]>
  pendingQueue: Ref<ChatPendingItem[]>
  usageAccum: Ref<ChatUsageAccumulator>
  usageModel: Ref<string>
  stream: ChatRpcStreamApi
  normalizeRunStatus: (status: string) => string
  sessionRunStatus: (source: ChatRunStatusSource | null | undefined) => ChatRunStatus
  applySessionRunState: (source: ChatRunStatusSource | null | undefined) => void
  queueRouterDecision: (payload: RouterDecisionPayload) => void
  appendEnsembleProgress: (payload: EnsembleProgressPayload) => void
  markEnsembleHandoff: () => void
  flushPendingRouterDecision: () => void
  clearPendingRouterDecision: () => void
  handleRouterControlReplay: () => void
  showCompactionToast: (
    payload: CompactionPayload,
    meta?: Record<string, unknown>,
  ) => ChatCompactionPresentationResult
  getCompactionPlacement?: (compactionId: string) => ChatCompactionPlacement | undefined
  showWarningToast: (message: string) => void
  scheduleHistorySync: () => void
  schedulePendingDrainAfterTerminal: () => void
  popAllPendingIntoComposer: () => boolean
  restoreSteerIntoComposer?: (text: string) => void
  steerDelivery?: ChatSteerDeliveryApi
  saveWidgetState: () => void
  subscribeSession?: () =>
    | boolean
    | void
    | SessionSubscriptionOutcome
    | Promise<boolean | void | SessionSubscriptionOutcome>
  onSessionSubscribed?: () => void | Promise<void>
  loadHistory?: () => void
  handleSessionConnectionState?: (state: string) => SessionBootstrapRun | undefined
  loadCurrentSessionUsage: () => void
  refreshRunModePreference?: () => void | Promise<void>
}

type ChatDoneUsageFields = {
  input_tokens?: number
  output_tokens?: number
  cached_tokens?: number
  cache_write?: number
  cost_usd?: number
  model?: string
  text?: string
  text_snapshot?: string | null
  textSnapshot?: string | null
  model_usage_breakdown?: unknown
  modelUsageBreakdown?: unknown
  ensemble_trace?: unknown
  ensembleTrace?: unknown
  coverage_status?: string
  coverageStatus?: string
  usage_unknown?: boolean
  usageUnknown?: boolean
  unknown_usage_events?: number
  unknownUsageEvents?: number
  decision_id?: string
}

type ChatDoneUsagePayload = SessionDonePayload & ChatDoneUsageFields & {
  usage?: ChatDoneUsageFields
}

type BufferedTerminalEvent =
  | { kind: 'event'; event: string; payload: SessionEventPayload; priority: number; replayWithoutSeq?: boolean }
  | { kind: 'session-change'; payload: SessionEventPayload; priority: number; replayWithoutSeq?: boolean }

type BufferedTerminalEventInput =
  | { kind: 'event'; event: string; payload: SessionEventPayload; priority?: number }
  | { kind: 'session-change'; payload: SessionEventPayload; priority?: number }

type BufferedPendingStreamEvent = {
  event: string
  payload: SessionEventPayload
  replayWithoutSeq?: boolean
}

type BufferedPendingReplayEntry =
  | {
      kind: 'stream'
      event: string
      payload: SessionEventPayload
      order: number
      replayWithoutSeq?: boolean
    }
  | {
      kind: 'terminal'
      terminal: BufferedTerminalEvent
      payload: SessionEventPayload
      order: number
    }

const MAX_PENDING_TASK_BUCKETS = 8
const MAX_PENDING_STREAM_EVENTS_PER_TASK = 64
const SERVER_CLOCK_TOLERANCE_MS = 5_000
const MAX_TRUSTED_REASONING_AGE_MS = 60 * 60 * 1_000
const PROVIDER_ACTIVITY_PHASES = new Set([
  'requesting',
  'reasoning',
  'retry_wait',
  'retrying',
  'fallback',
])
const PROVIDER_ACTIVITY_REASONS = new Set([
  'initial',
  'rate_limited',
  'provider_overloaded',
  'transport_transient',
  'reasoning_only',
  'empty_response',
  'stream_incomplete',
  'invalid_response',
  'context_overflow',
  'unknown',
])

const COMPACTION_TERMINAL_STATUSES = new Set([
  'completed',
  'skipped',
  'failed',
  'error',
  'cancelled',
  'timed_out',
  'stale',
  'emergency_ephemeral',
])

const TASK_TERMINAL_STATUSES = new Set([
  'succeeded',
  'failed',
  'cancelled',
  'timeout',
  'abandoned',
  'interrupted',
])

type LiveThinking = {
  text: string
  startedAt: number
  serverStartedAt: number | null
}

function trustedReasoningStartedAt(raw: unknown, now: number): number | null {
  if (typeof raw !== 'number' || !Number.isFinite(raw) || raw <= 0) return null
  if (raw > now + SERVER_CLOCK_TOLERANCE_MS) return null
  if (raw < now - MAX_TRUSTED_REASONING_AGE_MS) return null
  return raw
}

function trustedReasoningDoneAt(
  raw: unknown,
  serverStartedAt: number,
  now: number,
): number | null {
  if (typeof raw !== 'number' || !Number.isFinite(raw) || raw <= 0) return null
  if (raw < serverStartedAt) return null
  if (raw > now + SERVER_CLOCK_TOLERANCE_MS) return null
  if (raw - serverStartedAt > MAX_TRUSTED_REASONING_AGE_MS) return null
  return raw
}

function doneTextSnapshot(
  donePayload: ChatDoneUsagePayload,
  usagePayload: ChatDoneUsageFields,
): string | null {
  // A string snapshot is authoritative even when it is empty. Dataclass
  // serialization includes an unset optional field as null, so null must mean
  // "absent" and still permit the legacy nonempty `text` fallback.
  // Prefer any actual string across the outer and nested compatibility shapes.
  for (const source of [donePayload, usagePayload]) {
    for (const key of ['text_snapshot', 'textSnapshot'] as const) {
      if (Object.prototype.hasOwnProperty.call(source, key)) {
        const value = source[key]
        if (typeof value === 'string') return value
      }
    }
  }

  // Older gateways only sent `text`. Preserve their nonempty terminal
  // reconciliation behavior; legacy empty text always meant "fall back".
  for (const source of [usagePayload, donePayload]) {
    if (typeof source.text === 'string' && source.text) return source.text
  }
  return null
}

function doneDeliveryIsSuppressed(donePayload: ChatDoneUsagePayload): boolean {
  // The contract is deliberately strict and outer-payload-owned. A reason by
  // itself is diagnostic, not authority to erase output from a mixed-version
  // gateway.
  return donePayload.delivery === 'suppressed'
}

function doneTurnProvenance(
  donePayload: ChatDoneUsagePayload,
  snakeKey: 'input_mode' | 'run_kind',
  camelKey: 'inputMode' | 'runKind',
): string | undefined {
  // Provenance is outer-payload-owned like delivery. Accept camelCase only as
  // an additive client compatibility spelling; do not infer it from usage.
  for (const value of [donePayload[snakeKey], donePayload[camelKey]]) {
    if (typeof value === 'string' && value.trim()) return value.trim()
  }
  return undefined
}

function doneTurnId(donePayload: ChatDoneUsagePayload): string | undefined {
  // Terminal identity is outer-payload-owned. TaskRuntime stamps the same
  // durable turn id on Done and transcript turn_context; camelCase remains an
  // additive compatibility spelling for alternate gateways.
  for (const value of [
    donePayload.turn_id,
    donePayload.turnId,
    donePayload.task_id,
    donePayload.taskId,
  ]) {
    if (typeof value === 'string' && value.trim()) return value.trim()
  }
  return undefined
}

// A completed turn's measured thinking duration must survive the
// chat.history sync that replaces the messages array ~50ms after done.
// History rows carry the reasoning text but not the duration, so records
// are re-attached strictly by identity (reasoning or answer text) —
// never by timestamp proximity, which mis-bound reasoning to a
// neighbouring turn whenever a turn ran longer than the gap after it.
const REASONING_LOG_LIMIT = 20

interface TurnReasoningRecord {
  sessionKey: string
  text: string
  seconds: number
  messageText: string
  blocks: ReasoningBlock[]
}

interface TurnActivityRecord {
  sessionKey: string
  turnId?: string
  messageText: string
  statusHistory: StatusPart[]
}

export function useChatRpcEventHandlers(options: UseChatRpcEventHandlersOptions) {
  const {
    sessionKey,
    currentEpoch,
    lastStreamSeq,
    activeTaskGroups,
    activeStreamTaskId,
    aborted,
    messages,
    pendingQueue,
    usageAccum,
    usageModel,
    stream,
  } = options
  const steerDelivery = options.steerDelivery || useChatSteerDelivery({
    messages,
    pendingQueue,
    checkpointForUserMessage: stream.checkpointForUserMessage,
    scheduleHistorySync: options.scheduleHistorySync,
    restoreSteerIntoComposer: options.restoreSteerIntoComposer,
  })

  // Live thinking deltas for the current turn (session.event.thinking).
  const streamThinking = ref<LiveThinking | null>(null)
  let currentGenerationEpoch: number | null = null
  let activeAssistantMessageId = ''
  let terminalGenerationClosed = false
  const turnReasoningLog: TurnReasoningRecord[] = []
  const turnActivityLog: TurnActivityRecord[] = []
  const pendingTerminalEvents = new Map<string, BufferedTerminalEvent>()
  const pendingStreamEvents = new Map<string, BufferedPendingStreamEvent[]>()
  const settledTaskIds = new Set<string>()
  let pendingSuccessorRenderTaskId = ''

  function compactionStatus(payload: CompactionPayload): string {
    const status = String(payload.status || '').toLowerCase()
    if (status) return status
    if (Object.prototype.hasOwnProperty.call(payload, 'compacted')) {
      return payload.compacted ? 'completed' : 'skipped'
    }
    return ''
  }

  function payloadCompactionId(payload: CompactionPayload): string {
    return String(payload.compaction_id || payload.compactionId || '').trim()
  }

  function compactionTerminalActivityState(status: string): StatusPart['state'] | undefined {
    if (status === 'completed' || status === 'emergency_ephemeral') return 'completed'
    if (status === 'skipped') return 'skipped'
    if (status === 'stale' || status === 'cancelled') return 'cancelled'
    if (status === 'failed' || status === 'error' || status === 'timed_out') return 'failed'
    return undefined
  }

  function settleCommittedCompactionActivity(payload: CompactionPayload): boolean {
    const id = payloadCompactionId(payload)
    const state = compactionTerminalActivityState(compactionStatus(payload))
    if (!id || !state) return false

    for (let messageIndex = messages.value.length - 1; messageIndex >= 0; messageIndex -= 1) {
      const message = messages.value[messageIndex]
      if (message?.role !== 'assistant' || !message.statusHistory) continue
      for (let statusIndex = message.statusHistory.length - 1; statusIndex >= 0; statusIndex -= 1) {
        const marker = message.statusHistory[statusIndex]
        if (marker?.action !== 'context_compaction' || marker.id !== id) continue
        message.statusHistory[statusIndex] = {
          ...marker,
          state,
          reason: String(payload.reason || payload.skip_reason || marker.reason || ''),
          // The lifecycle stays anchored where its started frame first appeared.
          at: marker.at,
        }
        return true
      }
    }
    return false
  }

  function trackedLateCompactionPlacement(
    payload: CompactionPayload,
  ): ChatCompactionPlacement | undefined {
    if (activeStreamTaskId.value !== FINISHED_STREAM_TASK_ID) return undefined
    if (!isCurrentSessionPayload(payload)) return undefined
    if (!COMPACTION_TERMINAL_STATUSES.has(compactionStatus(payload))) return undefined
    const id = payloadCompactionId(payload)
    if (!id) return undefined
    return options.getCompactionPlacement?.(id)
  }

  function bufferedStreamSeq(payload: SessionEventPayload): number | null {
    const sequence = payload.stream_seq
    return typeof sequence === 'number' && Number.isFinite(sequence) ? sequence : null
  }

  function withoutBufferedStreamSeq(payload: SessionEventPayload): SessionEventPayload {
    const replayPayload = { ...payload }
    delete replayPayload.stream_seq
    return replayPayload
  }

  function comparePendingReplayEntries(
    left: BufferedPendingReplayEntry,
    right: BufferedPendingReplayEntry,
  ): number {
    const leftSequence = bufferedStreamSeq(left.payload)
    const rightSequence = bufferedStreamSeq(right.payload)
    if (leftSequence !== null && rightSequence !== null && leftSequence !== rightSequence) {
      return leftSequence - rightSequence
    }
    // Mixed-version gateways may omit sequence numbers. Preserve their old
    // stream-before-terminal behavior while still sorting every numbered frame.
    if (leftSequence !== null && rightSequence === null) {
      return right.kind === 'terminal' ? -1 : 1
    }
    if (leftSequence === null && rightSequence !== null) {
      return left.kind === 'terminal' ? 1 : -1
    }
    if (leftSequence !== null && leftSequence === rightSequence && left.kind !== right.kind) {
      // A terminal owns the cursor for a shared sequence, after all visible
      // frames at that sequence have been applied.
      return left.kind === 'terminal' ? 1 : -1
    }
    return left.order - right.order
  }

  function terminalEventPriority(event: string): number {
    if (event === 'session.event.done' || event === 'chat.done' || event.endsWith('.error')) return 3
    if (eventTaskTerminalStatus(event)) return 2
    return 0
  }

  function isTerminalEvent(event: string): boolean {
    if (eventTaskTerminalStatus(event)) return true
    if (event === 'session.event.done' || event === 'chat.done') return true
    return event.endsWith('.error') && !event.includes('.task_group.')
  }

  function bufferPendingTerminalEvent(
    entry: BufferedTerminalEventInput,
  ): boolean {
    if (!isCurrentSessionPayload(entry.payload)) return false
    if (isStaleEpoch(entry.payload)) return false
    const terminalTask = entry.kind === 'session-change'
      ? terminalSessionChangeTask(entry.payload)
      : null
    const taskId = chatTaskId(terminalTask) || payloadTaskId(entry.payload)
    if (!taskId) return false
    const activeTaskId = activeStreamTaskId.value
    const buffersPendingAcceptance = activeTaskId === PENDING_STREAM_TASK_ID
    const buffersSuccessor = Boolean(
      stream.isStreaming.value
      && activeTaskId
      && activeTaskId !== taskId
      && (
        options.taskOwnership?.runningTaskId.value === taskId
        || pendingSuccessorRenderTaskId === taskId
      ),
    )
    if (!buffersPendingAcceptance && !buffersSuccessor) return false
    if (buffersSuccessor && !acceptStreamSeq(entry.payload)) return true
    const priority = entry.priority ?? (entry.kind === 'event' ? terminalEventPriority(entry.event) : 1)
    const existing = pendingTerminalEvents.get(taskId)
    if (!existing || priority >= existing.priority) {
      if (!existing && pendingTerminalEvents.size >= MAX_PENDING_TASK_BUCKETS) {
        const oldestTaskId = pendingTerminalEvents.keys().next().value
        if (typeof oldestTaskId === 'string') pendingTerminalEvents.delete(oldestTaskId)
      }
      pendingTerminalEvents.set(taskId, {
        ...entry,
        priority,
        ...(buffersSuccessor ? { replayWithoutSeq: true } : {}),
      } as BufferedTerminalEvent)
    }
    return true
  }

  function markTaskSettled(payload: SessionEventPayload) {
    const terminalTask = terminalSessionChangeTask(payload)
    const taskId = chatTaskId(terminalTask) || payloadTaskId(payload)
    if (taskId) {
      settledTaskIds.add(taskId)
      pendingTerminalEvents.delete(taskId)
      pendingStreamEvents.delete(taskId)
    }
  }

  function bufferPendingStreamEvent(
    event: string,
    payload: SessionEventPayload,
  ): boolean {
    if (!isCurrentSessionPayload(payload)) return false
    if (isStaleEpoch(payload)) return false
    const taskId = payloadTaskId(payload)
    if (!taskId) return false
    const activeTaskId = activeStreamTaskId.value
    const buffersPendingAcceptance = activeTaskId === PENDING_STREAM_TASK_ID
    const buffersSuccessor = Boolean(
      stream.isStreaming.value
      && activeTaskId
      && activeTaskId !== taskId
      && (
        options.taskOwnership?.runningTaskId.value === taskId
        || pendingSuccessorRenderTaskId === taskId
      ),
    )
    if (!buffersPendingAcceptance && !buffersSuccessor) return false
    if (buffersSuccessor && !acceptStreamSeq(payload)) return true

    let buffered = pendingStreamEvents.get(taskId)
    if (!buffered) {
      if (pendingStreamEvents.size >= MAX_PENDING_TASK_BUCKETS) {
        const oldestTaskId = pendingStreamEvents.keys().next().value
        if (typeof oldestTaskId === 'string') pendingStreamEvents.delete(oldestTaskId)
      }
      buffered = []
      pendingStreamEvents.set(taskId, buffered)
    }
    if (buffered.length >= MAX_PENDING_STREAM_EVENTS_PER_TASK) buffered.shift()
    buffered.push({ event, payload, ...(buffersSuccessor ? { replayWithoutSeq: true } : {}) })
    return true
  }

  function handleRpcAnswerGenerationReset(payload: AnswerGenerationResetPayload) {
    if (isStaleEpoch(payload)) return
    if (bufferPendingStreamEvent('session.event.answer_generation_reset', payload)) return
    if (!isCurrentTaskPayload(payload)) return

    const oldGenerationEpoch = numericGenerationEpoch(
      payload.old_generation_epoch ?? payload.oldGenerationEpoch,
    )
    const newGenerationEpoch = numericGenerationEpoch(
      payload.new_generation_epoch ?? payload.newGenerationEpoch,
    )
    if (
      oldGenerationEpoch === null
      || newGenerationEpoch === null
      || newGenerationEpoch <= oldGenerationEpoch
    ) return

    const assistantMessageId = payloadAssistantMessageId(payload)
    if (
      assistantMessageId
      && activeAssistantMessageId
      && assistantMessageId !== activeAssistantMessageId
    ) return

    if (
      currentGenerationEpoch !== null
      && currentGenerationEpoch !== oldGenerationEpoch
      && currentGenerationEpoch !== newGenerationEpoch
    ) return
    if (!acceptStreamSeq(payload)) return

    // Snapshot replay and duplicate delivery can legitimately present the same
    // reset more than once. Consume its cursor above, then leave the live
    // bubble untouched when the requested generation is already active.
    if (currentGenerationEpoch === newGenerationEpoch) return
    currentGenerationEpoch = newGenerationEpoch
    if (assistantMessageId) {
      activeAssistantMessageId = assistantMessageId
      stream.setAssistantMessageId?.(assistantMessageId)
    }

    const authoritativeTextSnapshot = typeof payload.authoritative_text_snapshot === 'string'
      ? payload.authoritative_text_snapshot
      : typeof payload.authoritativeTextSnapshot === 'string'
        ? payload.authoritativeTextSnapshot
        : ''
    const authoritativeReasoningSnapshot = typeof payload.authoritative_reasoning_snapshot === 'string'
      ? payload.authoritative_reasoning_snapshot
      : typeof payload.authoritativeReasoningSnapshot === 'string'
        ? payload.authoritativeReasoningSnapshot
        : ''
    const preserveCompletedTools = payload.preserve_completed_tools
      ?? payload.preserveCompletedTools
      ?? true

    // A reset is a local replacement inside the current answer bubble. It must
    // not call resetLiveTurnState(), which would erase completed tools/artifacts
    // and create the conditions for a second assistant row on the next done.
    if (!stream.isStreaming.value && !stream.resetAnswerGeneration) {
      stream.startStreaming()
    }
    stream.resetAnswerGeneration?.({
      textSnapshot: authoritativeTextSnapshot,
      preserveCompletedTools,
    })

    streamThinking.value = null
    stream.hideThinkingIndicator()
    if (authoritativeReasoningSnapshot) {
      const now = Date.now()
      streamThinking.value = {
        text: authoritativeReasoningSnapshot,
        startedAt: now,
        serverStartedAt: null,
      }
      if (stream.useReducer.value) {
        stream.appendFrame({
          kind: 'thinking',
          text: authoritativeReasoningSnapshot,
          at: now,
        })
      }
    }
    stream.resetStreamIdleTimer()

    if (payload.terminal === true) {
      const terminalTextSnapshot = typeof payload.terminal_text_snapshot === 'string'
        ? payload.terminal_text_snapshot
        : typeof payload.terminalTextSnapshot === 'string'
          ? payload.terminalTextSnapshot
          : authoritativeTextSnapshot
      stream.reconcileFinalText(terminalTextSnapshot)
      stream.endStreaming()
      terminalGenerationClosed = true
      markTaskSettled(payload)
      options.clearPendingRouterDecision()
      if (activeTaskGroups.value.size > 0) {
        options.applySessionRunState(activeTaskGroupRunState(payload))
      } else {
        options.applySessionRunState({
          run_status: 'failed',
          last_task: { ...(payload || {}), status: 'failed' },
        })
      }
      options.scheduleHistorySync()
      activeStreamTaskId.value = FINISHED_STREAM_TASK_ID
      return
    }

    stream.scheduleRender()
  }

  function replayPendingStreamEvent(entry: BufferedPendingStreamEvent) {
    const { event } = entry
    const payload = entry.replayWithoutSeq
      ? withoutBufferedStreamSeq(entry.payload)
      : entry.payload
    if (event === 'session.event.answer_generation_reset') {
      handleRpcAnswerGenerationReset(payload as AnswerGenerationResetPayload)
    } else if (event === 'session.event.text_delta') {
      handleRpcTextDelta(payload as TextDeltaPayload)
    } else if (event === 'session.event.tool_use_start') {
      handleRpcToolUseStart(payload as ToolUsePayload)
    } else if (event === 'session.event.tool_use_delta') {
      handleRpcToolUseDelta(payload as ToolDeltaPayload)
    } else if (event === 'session.event.tool_use_end') {
      handleRpcToolUseEnd(payload as ToolEndPayload)
    } else if (event === 'session.event.tool_result') {
      handleRpcToolResult(payload as ToolResultPayload)
    } else if (event === 'session.event.artifact') {
      handleRpcArtifact(payload as ArtifactPayload)
    } else if (event === 'session.event.state_change') {
      handleRpcStateChange(payload)
    } else if (event === 'session.event.run_heartbeat') {
      handleRpcRunHeartbeat(payload)
    } else if (event === 'session.event.provider_activity') {
      handleRpcProviderActivity(payload as ProviderActivityPayload)
    } else if (event === 'session.event.router_decision') {
      handleRpcRouterDecision(payload as RouterDecisionPayload)
    } else if (event === 'session.event.ensemble_progress') {
      handleRpcEnsembleProgress(payload as EnsembleProgressPayload)
    } else if (event === 'session.event.router_control_replay') {
      handleRpcRouterControlReplay(payload)
    } else if (event === 'session.event.compaction') {
      // A live snapshot is the authoritative base for the active stream, not
      // historical replay. Compaction deliberately ignores replayed
      // non-terminal events, so mark this as live to restore the busy/Stop
      // state before subscribing from snapshot.current_stream_seq.
      handleRpcCompaction(payload as CompactionPayload, {
        authoritativeLive: true,
        replayed: false,
      })
    } else if (
      event === 'session.event.thinking_start'
      || event === 'session.event.thinking'
      || event === 'session.event.thinking_end'
    ) {
      handleRpcAny(event, payload)
    }
  }

  function restoreLiveTurnSnapshot(snapshot: SessionMessagesSnapshotResponse) {
    if (!snapshot || snapshot.key !== sessionKey.value) return

    stream.resetLiveTurnState?.()
    clearLiveThinking()
    clearGenerationTracking()
    pendingTerminalEvents.clear()
    pendingStreamEvents.clear()
    settledTaskIds.clear()
    pendingSuccessorRenderTaskId = ''
    options.clearPendingRouterDecision()
    activeStreamTaskId.value = typeof snapshot.task_id === 'string'
      ? snapshot.task_id
      : ''

    for (const entry of snapshot.events || []) {
      if (!entry || typeof entry.event !== 'string') continue
      const payload = { ...(entry.payload || {}) }
      // Snapshot events retain their original sequence for diagnostics, but
      // they form an authoritative base rather than fresh deltas. Replaying
      // them through the normal render handlers without the old sequence
      // rebuilds the bubble even when this client already had a newer cursor.
      delete payload.stream_seq
      replayPendingStreamEvent({ event: entry.event, payload })
    }

    if (
      typeof snapshot.current_stream_seq === 'number'
      && Number.isFinite(snapshot.current_stream_seq)
    ) {
      lastStreamSeq.value = Math.max(0, snapshot.current_stream_seq)
    }
  }

  function replayPendingTerminalEvent(entry: BufferedTerminalEvent) {
    const payload = entry.replayWithoutSeq
      ? withoutBufferedStreamSeq(entry.payload)
      : entry.payload
    if (entry.kind === 'session-change') {
      handleRpcSessionsChanged(payload)
    } else {
      handleRpcAny(entry.event, payload)
    }
  }

  function bindActiveStreamTask(taskId: string) {
    if (!taskId) return
    const bufferedTerminal = pendingTerminalEvents.get(taskId)
    const bufferedStream = pendingStreamEvents.get(taskId) || []
    // One chat.send response resolves one PENDING window. Drop every other
    // task's early frames so a late prior task can never be consumed by a
    // future send that happens to reuse this session.
    pendingTerminalEvents.clear()
    pendingStreamEvents.clear()
    if (settledTaskIds.has(taskId)) {
      activeStreamTaskId.value = FINISHED_STREAM_TASK_ID
      return
    }
    activeStreamTaskId.value = taskId
    if (pendingSuccessorRenderTaskId === taskId) pendingSuccessorRenderTaskId = ''

    const replayEntries: BufferedPendingReplayEntry[] = bufferedStream.map(
      (entry, order) => ({ kind: 'stream', ...entry, order }),
    )
    if (bufferedTerminal) {
      replayEntries.push({
        kind: 'terminal',
        terminal: bufferedTerminal,
        payload: bufferedTerminal.payload,
        order: bufferedStream.length,
      })
    }
    replayEntries.sort(comparePendingReplayEntries)

    const terminalSequence = bufferedTerminal
      ? bufferedStreamSeq(bufferedTerminal.payload)
      : null
    let taskTerminalReplayed = false
    for (const entry of replayEntries) {
      if (entry.kind === 'terminal') {
        replayPendingTerminalEvent(entry.terminal)
        taskTerminalReplayed = true
        continue
      }

      const sequence = bufferedStreamSeq(entry.payload)
      const sharesTerminalSequence = terminalSequence !== null && sequence === terminalSequence
      const maintenanceAfterTerminal = taskTerminalReplayed
        && entry.event === 'session.event.compaction'
      // Let the terminal own a shared cursor. A tracked compaction terminal may
      // still close its existing UI after task completion, but it must not move
      // the task cursor past the terminal that closed the stream.
      const payload = entry.replayWithoutSeq || sharesTerminalSequence || maintenanceAfterTerminal
        ? withoutBufferedStreamSeq(entry.payload)
        : entry.payload
      replayPendingStreamEvent({ event: entry.event, payload })
    }
  }

  function successorTaskId(predecessorTaskId: string): string {
    const runningTaskId = options.taskOwnership?.runningTaskId.value || ''
    return runningTaskId && runningTaskId !== predecessorTaskId
      ? runningTaskId
      : ''
  }

  function bindSuccessorAfterTerminal(predecessorTaskId: string): boolean {
    const successor = successorTaskId(predecessorTaskId)
    if (!successor) return false
    aborted.value = false
    // Start the successor bubble before replaying its early frames. The
    // successor may already have emitted its terminal while A's terminal was
    // delayed; starting after replay would reopen B after that terminal and
    // incorrectly restore B as authoritative running work.
    if (!stream.isStreaming.value) stream.startStreaming()
    bindActiveStreamTask(successor)
    return true
  }

  // 1s ticker so the live "Thinking · Ns" label advances on a clock while
  // reasoning is open, not only when a new reasoning delta happens to arrive.
  const elapsedTick = ref(0)
  let elapsedTimer: ReturnType<typeof setInterval> | null = null
  watch(
    () => stream.isStreaming.value && !!streamThinking.value,
    (active) => {
      if (active && !elapsedTimer) {
        elapsedTimer = setInterval(() => { elapsedTick.value++ }, 1000)
      } else if (!active && elapsedTimer) {
        clearInterval(elapsedTimer)
        elapsedTimer = null
      }
    },
  )
  onScopeDispose(() => { if (elapsedTimer) { clearInterval(elapsedTimer); elapsedTimer = null } })

  const streamThinkingText = computed(() => streamThinking.value?.text || '')
  // Recomputed per delta AND on the 1s tick so the label keeps pace between
  // deltas; the final "Thought for Ns" uses the measured wall clock at done.
  const streamThinkingElapsedText = computed(() => {
    elapsedTick.value
    const current = streamThinking.value
    if (!current) return ''
    const seconds = Math.max(0, Math.floor((Date.now() - current.startedAt) / 1000))
    return `${seconds}s`
  })

  function appendThinkingDelta(text: string, payload: SessionEventPayload) {
    if (!text) return
    if (!stream.isStreaming.value) stream.startStreaming()
    stream.noteReasoningPresentationDelta?.(text)
    const current = streamThinking.value
    if (current) {
      // Production renders reasoning from the non-reactive accumulator on the
      // shared publish clock. Rebuilding this reactive prefix for every delta
      // invalidated Vue 20,000 times and retained old strings between frames.
      if (stream.useReducer.value !== true) {
        streamThinking.value = { ...current, text: current.text + text }
      }
    } else {
      const now = Date.now()
      const serverStartedAt = trustedReasoningStartedAt(payload.started_at, now)
      streamThinking.value = {
        text: stream.useReducer.value === true ? '' : text,
        startedAt: serverStartedAt ?? now,
        serverStartedAt,
      }
    }
    // The fold concats the same text into its thinkingText. Gating already
    // passed upstream (handleRpcAny), so this frame mirrors an accepted delta.
    if (stream.useReducer.value) {
      const blockId = typeof payload.block_id === 'string'
        ? payload.block_id
        : typeof payload.blockId === 'string'
          ? payload.blockId
          : undefined
      const rawBlockIndex = payload.block_index ?? payload.blockIndex
      const blockIndex = typeof rawBlockIndex === 'number' ? rawBlockIndex : undefined
      stream.appendFrame({
        kind: 'thinking',
        text,
        at: trustedReasoningStartedAt(payload.started_at, Date.now()) ?? Date.now(),
        ...(blockId ? { blockId } : {}),
        ...(blockIndex !== undefined ? { blockIndex } : {}),
      })
    }
    // Reasoning growth must re-pin the thread to the bottom just like answer
    // text and tool deltas. Schedule the same batched render/scroll flush so a
    // long thinking phase keeps following the live turn instead of only
    // snapping down once answer text starts streaming.
    stream.scheduleRender()
  }

  function clearLiveThinking() {
    streamThinking.value = null
  }

  function handleThinkingStart(payload: SessionEventPayload) {
    if (!stream.isStreaming.value) stream.startStreaming()
    if (!stream.useReducer.value) return
    const blockId = typeof payload.block_id === 'string'
      ? payload.block_id
      : typeof payload.blockId === 'string'
        ? payload.blockId
        : ''
    if (!blockId) return
    const rawBlockIndex = payload.block_index ?? payload.blockIndex
    const rawContentKind = payload.content_kind ?? payload.contentKind
    stream.appendFrame({
      kind: 'thinking-start',
      blockId,
      blockIndex: typeof rawBlockIndex === 'number' ? rawBlockIndex : 0,
      at: trustedReasoningStartedAt(payload.started_at, Date.now()) ?? Date.now(),
      contentKind: rawContentKind === 'summary' ? 'summary' : 'reasoning',
    })
    stream.scheduleRender()
  }

  function handleThinkingEnd(payload: SessionEventPayload) {
    if (!stream.useReducer.value) return
    const blockId = typeof payload.block_id === 'string'
      ? payload.block_id
      : typeof payload.blockId === 'string'
        ? payload.blockId
        : ''
    if (!blockId) return
    const rawBlockIndex = payload.block_index ?? payload.blockIndex
    const rawStatus = payload.status
    const status = rawStatus === 'interrupted' || rawStatus === 'error'
      ? rawStatus
      : 'completed'
    const rawEndedAt = payload.ended_at ?? payload.endedAt
    stream.appendFrame({
      kind: 'thinking-end',
      blockId,
      blockIndex: typeof rawBlockIndex === 'number' ? rawBlockIndex : 0,
      status,
      at: typeof rawEndedAt === 'number' && Number.isFinite(rawEndedAt)
        ? rawEndedAt
        : Date.now(),
    })
    stream.scheduleRender()
  }

  // Walk recorded turn reasonings (newest first) and re-bind each to the
  // newest unclaimed assistant message it identifies: a row that already
  // carries this record's reasoning text gets its measured duration
  // restored, and a row without reasoning is claimed via its answer text
  // (covers history rows that predate the reasoning backfill). A record
  // with no identity match attaches nowhere — losing a duration beats
  // showing one turn's reasoning under another turn's answer. Idempotent:
  // safe to run after every history replacement.
  function attachTurnReasoning() {
    const list = messages.value
    if (turnReasoningLog.length > 0) {
      const claimed = new Set<ChatMessage>()
      for (let r = turnReasoningLog.length - 1; r >= 0; r--) {
        const record = turnReasoningLog[r]
        if (record.sessionKey !== sessionKey.value) continue
        for (let i = list.length - 1; i >= 0; i--) {
          const msg = list[i]
          if (msg.role !== 'assistant' || claimed.has(msg)) continue
          const carriesRecordReasoning = msg.reasoning?.text === record.text
          const matchesAnswerText =
            !msg.reasoning && record.messageText !== '' && msg.text.trim() === record.messageText
          if (!carriesRecordReasoning && !matchesAnswerText) continue
          claimed.add(msg)
          msg.reasoning = { text: record.text, seconds: record.seconds }
          if (record.blocks.length) {
            msg.reasoningBlocks = record.blocks.map(block => ({ ...block }))
          }
          break
        }
      }
    }
    attachTurnActivity()
  }

  // Canonical chat history may not yet persist client activity markers. Keep
  // the just-finished turn's safe structured phases attached across the
  // immediate history replacement so its completed disclosure remains a full
  // process record instead of collapsing back to reasoning-only.
  function attachTurnActivity() {
    if (turnActivityLog.length === 0) return
    const claimed = new Set<ChatMessage>()
    for (let r = turnActivityLog.length - 1; r >= 0; r--) {
      const record = turnActivityLog[r]
      if (record.sessionKey !== sessionKey.value) continue
      for (let i = messages.value.length - 1; i >= 0; i--) {
        const msg = messages.value[i]
        if (msg.role !== 'assistant' || claimed.has(msg)) continue
        const matchesTurn = Boolean(record.turnId && msg.turnId === record.turnId)
        const matchesAnswer = (!record.turnId || !msg.turnId)
          && record.messageText !== ''
          && msg.text.trim() === record.messageText
        if (!matchesTurn && !matchesAnswer) continue
        claimed.add(msg)
        const existing = msg.statusHistory ?? []
        const localPhaseKeys = new Set(record.statusHistory.map(step =>
          `${step.action}\u001f${step.at}\u001f${step.id || ''}`,
        ))
        msg.statusHistory = [
          ...record.statusHistory.map(step => ({ ...step })),
          ...existing.filter(step => !localPhaseKeys.has(
            `${step.action}\u001f${step.at}\u001f${step.id || ''}`,
          )),
        ]
        break
      }
    }
  }

  function recordTurnActivity(message: ChatMessage) {
    if (!message.statusHistory?.length) return
    turnActivityLog.push({
      sessionKey: sessionKey.value,
      turnId: message.turnId,
      messageText: message.text.trim(),
      statusHistory: message.statusHistory.map(step => ({ ...step })),
    })
    if (turnActivityLog.length > REASONING_LOG_LIMIT) {
      turnActivityLog.splice(0, turnActivityLog.length - REASONING_LOG_LIMIT)
    }
    attachTurnActivity()
  }

  function recordTurnReasoning(
    text: string,
    seconds: number,
    messageText: string,
    blocks?: ReasoningBlock[],
  ) {
    turnReasoningLog.push({
      sessionKey: sessionKey.value,
      text,
      seconds,
      messageText,
      blocks: (blocks ?? []).map(block => ({ ...block })),
    })
    if (turnReasoningLog.length > REASONING_LOG_LIMIT) {
      turnReasoningLog.splice(0, turnReasoningLog.length - REASONING_LOG_LIMIT)
    }
    attachTurnReasoning()
  }

  function doneUsagePayload(donePayload: ChatDoneUsagePayload): ChatUsagePayload | undefined {
    const raw = (donePayload.usage || donePayload || {}) as Record<string, unknown>
    if (!raw || typeof raw !== 'object') return undefined
    const usage = { ...raw } as ChatUsagePayload
    const direct = donePayload as Record<string, unknown>
    if (direct.model_usage_breakdown != null && usage.model_usage_breakdown == null) {
      usage.model_usage_breakdown = direct.model_usage_breakdown as never
    }
    if (direct.modelUsageBreakdown != null && usage.modelUsageBreakdown == null) {
      usage.modelUsageBreakdown = direct.modelUsageBreakdown as never
    }
    if (direct.ensemble_trace != null && usage.ensemble_trace == null) {
      usage.ensemble_trace = direct.ensemble_trace as never
    }
    if (direct.ensembleTrace != null && usage.ensembleTrace == null) {
      usage.ensembleTrace = direct.ensembleTrace as never
    }
    for (const key of [
      'coverage_status',
      'coverageStatus',
      'usage_unknown',
      'usageUnknown',
      'unknown_usage_events',
      'unknownUsageEvents',
    ] as const) {
      if (direct[key] != null && usage[key] == null) {
        usage[key] = direct[key] as never
      }
    }
    return usage
  }

  watch(sessionKey, () => {
    streamThinking.value = null
    clearGenerationTracking()
    turnReasoningLog.length = 0
    turnActivityLog.length = 0
    pendingTerminalEvents.clear()
    pendingStreamEvents.clear()
    settledTaskIds.clear()
    pendingSuccessorRenderTaskId = ''
    // The newly shown session has its own (possibly running) task; forget the
    // previous session's active task so we stay lenient until it re-asserts.
    activeStreamTaskId.value = ''
  })
  watch(activeStreamTaskId, (taskId, previousTaskId) => {
    if (taskId === PENDING_STREAM_TASK_ID) {
      clearGenerationTracking()
      pendingTerminalEvents.clear()
      pendingStreamEvents.clear()
      settledTaskIds.clear()
      pendingSuccessorRenderTaskId = ''
    } else if (!taskId || taskId === FINISHED_STREAM_TASK_ID || taskId === STOPPED_STREAM_TASK_ID) {
      pendingTerminalEvents.clear()
      pendingStreamEvents.clear()
    } else if (taskId !== previousTaskId) {
      // A concrete task transition starts a fresh generation ledger. The
      // PENDING branch above already clears it before normal chat.send replay;
      // this also covers a task.running/snapshot adoption that skips PENDING.
      clearGenerationTracking()
    }
  }, { flush: 'sync' })

  function isStaleEpoch(payload: StreamEventEnvelope): boolean {
    return payloadIsStaleEpoch(payload, currentEpoch.value)
  }

  function isCurrentSessionPayload(payload: StreamEventEnvelope): boolean {
    return payloadIsCurrentSession(payload, sessionKey.value)
  }

  // Drop late events tagged with a different task than the one rendering now,
  // so a stale turn's tool_use/error/done can't leak into the current turn
  // (issue #344). Lenient: untagged events and unknown active task pass.
  function isCurrentTaskPayload(payload: StreamEventEnvelope): boolean {
    return payloadIsCurrentTask(payload, activeStreamTaskId.value)
  }

  function acceptStreamSeq(payload: StreamEventEnvelope): boolean {
    options.observeStreamGeneration?.(payload)
    const decision = decideStreamSeq(payload, sessionKey.value, lastStreamSeq.value)
    if (decision.accepted) lastStreamSeq.value = decision.nextStreamSeq
    return decision.accepted
  }

  function numericGenerationEpoch(raw: unknown): number | null {
    return typeof raw === 'number' && Number.isInteger(raw) && raw >= 0 ? raw : null
  }

  function payloadGenerationEpoch(payload: StreamEventEnvelope): number | null {
    return numericGenerationEpoch(payload.generation_epoch ?? payload.generationEpoch)
  }

  function payloadAssistantMessageId(payload: StreamEventEnvelope): string {
    const id = payload.assistant_message_id ?? payload.assistantMessageId
    return typeof id === 'string' ? id.trim() : ''
  }

  function clearGenerationTracking() {
    currentGenerationEpoch = null
    activeAssistantMessageId = ''
    terminalGenerationClosed = false
    stream.setAssistantMessageId?.('')
  }

  /**
   * Generation and assistant-message identity are independent of the session
   * epoch/stream cursor. Once a reset advances the generation, late provider
   * frames from the old generation must not reach any live mutator. During the
   * PENDING window we buffer frames first and infer their generation when they
   * are replayed against the accepted task.
   */
  function isCurrentGenerationPayload(payload: StreamEventEnvelope): boolean {
    if (activeStreamTaskId.value === PENDING_STREAM_TASK_ID) return true

    const assistantMessageId = payloadAssistantMessageId(payload)
    if (
      assistantMessageId
      && activeAssistantMessageId
      && assistantMessageId !== activeAssistantMessageId
    ) return false
    if (assistantMessageId && !activeAssistantMessageId) {
      activeAssistantMessageId = assistantMessageId
      stream.setAssistantMessageId?.(assistantMessageId)
    }

    const generationEpoch = payloadGenerationEpoch(payload)
    if (generationEpoch === null) return true
    if (currentGenerationEpoch === null) {
      currentGenerationEpoch = generationEpoch
      return true
    }
    return generationEpoch === currentGenerationEpoch
  }

  function activeTaskGroupRunState(payload: SessionEventPayload = {}) {
    return buildActiveTaskGroupRunState(payload, activeTaskGroups.value.size)
  }

  function noteTaskGroupActive(payload: SessionEventPayload) {
    const gid = eventTaskGroupId(payload)
    if (gid) activeTaskGroups.value.add(gid)
    options.applySessionRunState(activeTaskGroupRunState(payload))
  }

  function noteTaskGroupTerminal(payload: SessionEventPayload, terminalStatus: string) {
    const gid = eventTaskGroupId(payload)
    if (gid) activeTaskGroups.value.delete(gid)
    if (activeTaskGroups.value.size > 0) {
      options.applySessionRunState(activeTaskGroupRunState(payload))
      return
    }
    const runningTaskId = options.taskOwnership?.runningTaskId.value || ''
    if (runningTaskId) {
      options.applySessionRunState({
        run_status: 'running',
        active_task: { task_id: runningTaskId, status: 'running' },
      })
      return
    }
    options.applySessionRunState({
      run_status: terminalStatus === 'failed' ? 'failed' : 'idle',
      last_task: { ...(payload || {}), status: terminalStatus },
    })
    if (!stream.isStreaming.value) {
      options.schedulePendingDrainAfterTerminal()
    }
  }

  function sessionChangeIsTerminal(payload: SessionEventPayload): boolean {
    return payloadSessionChangeIsTerminal(payload, options.normalizeRunStatus)
      || Boolean(terminalSessionChangeTask(payload))
  }

  function terminalSessionChangeTask(
    payload: SessionEventPayload,
  ): ChatRunStatusSource['last_task'] {
    const lastTask = (payload.last_task || payload.lastTask) as ChatRunStatusSource['last_task']
    if (lastTask) return lastTask
    const changedTask = (payload.changed_task || payload.changedTask) as ChatRunStatusSource['last_task']
    return changedTask && TASK_TERMINAL_STATUSES.has(String(changedTask.status || '').toLowerCase())
      ? changedTask
      : null
  }

  function isStoppedCancelledTerminalEvent(terminalStatus: string, payload: SessionEventPayload): boolean {
    const taskId = payloadTaskId(payload)
    return Boolean(
      isCurrentSessionPayload(payload)
      && terminalStatus === 'cancelled'
      && taskId
      && options.taskOwnership?.stopRequestedTaskId.value === taskId,
    )
  }

  function isStoppedTerminalSessionChange(payload: SessionEventPayload): boolean {
    if (!isCurrentSessionPayload(payload)) return false
    if (!sessionChangeIsTerminal(payload)) return false
    const terminalTask = terminalSessionChangeTask(payload)
    const taskId = chatTaskId(terminalTask)
    if (!taskId || options.taskOwnership?.stopRequestedTaskId.value !== taskId) return false
    const terminalStatus = String(terminalTask?.status || '').toLowerCase()
    return terminalStatus === 'cancelled' || terminalStatus === 'abandoned' || terminalStatus === 'interrupted'
  }

  function syncTerminalSessionChange(payload: SessionEventPayload = {}) {
    if (!isCurrentSessionPayload(payload)) return false
    const terminalTask = terminalSessionChangeTask(payload)
    const terminalTaskId = chatTaskId(terminalTask)
    const hasAuthoritativeProjection = Boolean(
      'run_status' in payload
      || 'runStatus' in payload
      || 'active_task' in payload
      || 'activeTask' in payload
      || 'last_task' in payload
      || 'lastTask' in payload
    )
    const snapshotUnavailable = Boolean(terminalTaskId && !hasAuthoritativeProjection)
    const terminalWasAlreadySettled = Boolean(
      terminalTaskId && settledTaskIds.has(terminalTaskId),
    )
    const settled = terminalTaskId
      ? options.taskOwnership?.noteTerminal(terminalTaskId)
      : undefined
    const activeTask = (payload.active_task || payload.activeTask) as ChatRunStatusSource['active_task']
    const activeTaskId = chatTaskId(activeTask)
    const renderTaskId = activeStreamTaskId.value
    const runningTaskId = options.taskOwnership?.runningTaskId.value || ''
    const terminalIsBackground = Boolean(
      terminalTaskId
      && terminalTaskId !== renderTaskId
      && (
        terminalWasAlreadySettled
        || (settled?.wasQueued && !settled.wasRunning && !settled.wasStopTarget)
        || (runningTaskId && runningTaskId !== terminalTaskId && runningTaskId === renderTaskId)
        || (activeTaskId && activeTaskId !== terminalTaskId && activeTaskId === renderTaskId)
      )
    )
    if (terminalIsBackground) {
      markTaskSettled(payload)
      // A snapshot-failure fallback may contain only changed_task. It is
      // authoritative for B's terminal identity, but it must not project an
      // implicit idle state over a still-running A.
      if (activeTaskId) options.applySessionRunState(payload)
      options.scheduleHistorySync()
      return true
    }
    activeTaskGroups.value.clear()
    const terminalStatus = String(terminalTask?.status || '').toLowerCase()
    const interrupted = ['cancelled', 'abandoned', 'interrupted'].includes(terminalStatus)
    if (stream.isStreaming.value) stream.endStreaming(interrupted ? { reason: 'aborted' } : undefined)
    markTaskSettled(payload)
    options.applySessionRunState(payload)
    if (snapshotUnavailable) {
      // changed_task-only is the Gateway's explicit snapshot-failure shape.
      // It proves A ended but says nothing about queued/running continuation
      // B. Fail closed until a fresh authoritative projection arrives instead
      // of briefly declaring idle and draining C out of order.
      options.taskOwnership?.beginHydration()
      void Promise.resolve(options.subscribeSession?.())
        .then((subscribed) => {
          if (isAuthoritativeSessionSubscription(subscribed)) {
            return options.onSessionSubscribed?.()
          }
        })
        .catch(() => {})
    }
    options.scheduleHistorySync()
    if (!options.taskOwnership?.hasAuthoritativeWork.value && !interrupted) {
      options.schedulePendingDrainAfterTerminal()
    }
    if (!bindSuccessorAfterTerminal(terminalTaskId)) {
      activeStreamTaskId.value = FINISHED_STREAM_TASK_ID
    }
    return true
  }

  function handleRpcTextDelta(payload: TextDeltaPayload) {
    if (isStaleEpoch(payload)) return
    if (bufferPendingStreamEvent('session.event.text_delta', payload)) return
    if (!isCurrentGenerationPayload(payload)) return
    if (!isCurrentTaskPayload(payload)) return
    if (!acceptStreamSeq(payload)) return
    stream.resetStreamIdleTimer()
    options.markEnsembleHandoff()
    const presentation = payload.presentation
    if (presentation === 'intermediate' || presentation === 'answer') {
      stream.appendDelta(payload.text || '', presentation)
    } else {
      // Compatibility with older gateways that predate semantic text roles.
      stream.appendDelta(payload.text || '')
    }
  }

  function handleRpcToolUseStart(payload: ToolUsePayload) {
    if (isStaleEpoch(payload)) return
    if (aborted.value) return
    if (bufferPendingStreamEvent('session.event.tool_use_start', payload)) return
    if (!isCurrentGenerationPayload(payload)) return
    if (!isCurrentTaskPayload(payload)) return
    if (!acceptStreamSeq(payload)) return
    stream.resetStreamIdleTimer()
    options.markEnsembleHandoff()
    stream.appendToolCall(payload)
  }

  function handleRpcToolUseDelta(payload: ToolDeltaPayload) {
    if (isStaleEpoch(payload)) return
    if (aborted.value) return
    if (bufferPendingStreamEvent('session.event.tool_use_delta', payload)) return
    if (!isCurrentGenerationPayload(payload)) return
    if (!isCurrentTaskPayload(payload)) return
    if (!acceptStreamSeq(payload)) return
    stream.resetStreamIdleTimer()
    options.markEnsembleHandoff()
    stream.appendToolDelta(payload)
  }

  function handleRpcToolUseEnd(payload: ToolEndPayload) {
    if (isStaleEpoch(payload)) return
    if (aborted.value) return
    if (bufferPendingStreamEvent('session.event.tool_use_end', payload)) return
    if (!isCurrentGenerationPayload(payload)) return
    if (!isCurrentTaskPayload(payload)) return
    if (!acceptStreamSeq(payload)) return
    stream.resetStreamIdleTimer()
    options.markEnsembleHandoff()
    stream.appendToolEnd?.(payload)
  }

  function handleRpcToolResult(payload: ToolResultPayload) {
    if (isStaleEpoch(payload)) return
    if (aborted.value) return
    if (bufferPendingStreamEvent('session.event.tool_result', payload)) return
    if (!isCurrentGenerationPayload(payload)) return
    if (!isCurrentTaskPayload(payload)) return
    if (!acceptStreamSeq(payload)) return
    stream.resetStreamIdleTimer()
    stream.appendToolResult(payload)
  }

  function handleRpcArtifact(payload: ArtifactPayload) {
    if (isStaleEpoch(payload)) return
    if (aborted.value) return
    if (bufferPendingStreamEvent('session.event.artifact', payload)) return
    if (!isCurrentGenerationPayload(payload)) return
    if (!isCurrentTaskPayload(payload)) return
    if (!acceptStreamSeq(payload)) return
    stream.resetStreamIdleTimer()
    stream.appendArtifact(payload)
  }

  function handleRpcStateChange(payload: SessionEventPayload) {
    if (isStaleEpoch(payload)) return
    if (!payload || aborted.value) return
    if (bufferPendingStreamEvent('session.event.state_change', payload)) return
    if (!isCurrentGenerationPayload(payload)) return
    if (!isCurrentTaskPayload(payload)) return
    if (!acceptStreamSeq(payload)) return
    stream.resetStreamIdleTimer()
    const to = payload.to_state || payload.toState || ''
    const activeState = ['thinking', 'streaming', 'tool_calling', 'tool_use', 'running'].includes(String(to))
    if (!stream.isStreaming.value && activeState) stream.startStreaming()
    if (!stream.isStreaming.value) return
    if (activeState) options.markEnsembleHandoff()
    if (to === 'thinking') {
      if (stream.streamBubble.value && !stream.streamHasVisibleOutput.value) {
        stream.setStreamActivity('Planning next step')
      } else if (!stream.streamBubble.value) {
        stream.showThinkingIndicator()
      }
    } else if (to === 'streaming' && stream.streamBubble.value && !stream.streamHasVisibleOutput.value) {
      stream.setStreamActivity('Model is generating')
    } else if ((to === 'tool_calling' || to === 'tool_use') && stream.streamBubble.value && !stream.streamHasVisibleOutput.value) {
      stream.setStreamActivity('Preparing tool call')
    } else if (to && stream.streamBubble.value && !stream.streamHasVisibleOutput.value) {
      stream.setStreamActivity('Still running')
    }
  }

  function handleRpcRunHeartbeat(payload: SessionEventPayload) {
    if (isStaleEpoch(payload)) return
    if (aborted.value) return
    if (bufferPendingStreamEvent('session.event.run_heartbeat', payload)) return
    if (!isCurrentGenerationPayload(payload)) return
    if (!isCurrentTaskPayload(payload)) return
    if (!acceptStreamSeq(payload)) return
    if (!stream.isStreaming.value) stream.startStreaming()
    // Transport heartbeat proves liveness only. It must neither replace the
    // current structured provider phase nor postpone the 20s no-progress UI.
    stream.resetStreamIdleTimer({ progress: false })
    if (!stream.streamBubble.value) {
      stream.showThinkingIndicator()
    }
  }

  function providerActivityCounter(raw: unknown, maximum: number): number {
    const value = Number(raw)
    if (!Number.isFinite(value)) return 0
    return Math.min(maximum, Math.max(0, Math.floor(value)))
  }

  function handleRpcProviderActivity(payload: ProviderActivityPayload) {
    if (isStaleEpoch(payload)) return
    if (aborted.value) return
    if (bufferPendingStreamEvent('session.event.provider_activity', payload)) return
    if (!isCurrentTaskPayload(payload)) return
    if (!acceptStreamSeq(payload)) return

    const phase = String(payload.phase || '')
    const reason = String(payload.reason || 'unknown')
    if (!PROVIDER_ACTIVITY_PHASES.has(phase)) return
    const safeReason = PROVIDER_ACTIVITY_REASONS.has(reason) ? reason : 'unknown'
    const attempt = providerActivityCounter(payload.retry_attempt, 10_000)
    const limit = providerActivityCounter(payload.retry_limit, 10_000)
    const retryAfterMs = providerActivityCounter(payload.retry_after_ms, 900_000)
    const retryAfterSeconds = Math.ceil(retryAfterMs / 1000)

    if (!stream.isStreaming.value) stream.startStreaming()
    stream.resetStreamIdleTimer()
    options.markEnsembleHandoff()

    if (phase === 'requesting') {
      stream.setStreamActivity('Waiting for model', 'provider:requesting')
    } else if (phase === 'reasoning') {
      stream.setStreamActivity('Thinking deeply', 'provider:reasoning')
    } else if (phase === 'retry_wait' && safeReason === 'rate_limited') {
      stream.setStreamActivity(
        `Rate limited · ${retryAfterSeconds}s`,
        `provider:rate_limited:${retryAfterSeconds}`,
      )
    } else if (phase === 'retry_wait') {
      stream.setStreamActivity(
        `Waiting to retry · ${retryAfterSeconds}s`,
        `provider:retry_wait:${retryAfterSeconds}`,
      )
    } else if (phase === 'retrying') {
      stream.setStreamActivity(
        `Retrying ${attempt}/${limit}`,
        `provider:retrying:${attempt}:${limit}`,
      )
    } else if (phase === 'fallback') {
      stream.setStreamActivity('Switching to backup model', 'provider:fallback')
    }
  }

  function handleRpcCompaction(payload: CompactionPayload, meta: unknown) {
    if (isStaleEpoch(payload)) return
    if (bufferPendingStreamEvent('session.event.compaction', payload)) return
    if (!isCurrentGenerationPayload(payload)) return
    const trackedPlacement = trackedLateCompactionPlacement(payload)
    if (!isCurrentTaskPayload(payload) && !trackedPlacement) return
    if (!acceptStreamSeq(payload)) return
    const safeMeta = (meta && typeof meta === 'object' ? meta : {}) as Record<string, unknown>
    const source = String(payload.source || '').toLowerCase()
    const status = compactionStatus(payload)
    const userVisible = payload.user_visible ?? payload.userVisible ?? true
    const taskId = payloadTaskId(payload)
    const ownedByCurrentTask = Boolean(
      taskId
      && activeStreamTaskId.value
      && taskId === activeStreamTaskId.value,
    )
    const canOwnActivity = source !== 'manual'
      && userVisible !== false
      && !['skipped', 'stale'].includes(status)
    const prefersActivity = canOwnActivity
      && (
        stream.isStreaming.value
        || safeMeta.authoritativeLive === true
        || ownedByCurrentTask
      )
    const settleCommittedActivity = trackedPlacement === 'activity'
      && !stream.isStreaming.value
    // An authoritative snapshot can contain compaction before any state/text
    // frame. Open the live reducer first so a later startStreaming() cannot
    // reset and discard the restored maintenance marker.
    if (prefersActivity && !stream.isStreaming.value && !settleCommittedActivity) {
      stream.startStreaming()
    }

    const requestedPlacement: ChatCompactionPlacement = trackedPlacement
      || (prefersActivity ? 'activity' : 'standalone')
    const presentation = options.showCompactionToast(payload || {}, {
      ...safeMeta,
      placement: requestedPlacement,
    })
    if (presentation === false) return
    const placement: ChatCompactionPlacement = presentation === 'activity'
      || presentation === 'standalone'
      ? presentation
      : requestedPlacement
    if (placement === 'activity') {
      if (settleCommittedActivity) {
        settleCommittedCompactionActivity(payload)
      } else {
        if (!stream.isStreaming.value && (safeMeta.authoritativeLive === true || ownedByCurrentTask)) {
          stream.startStreaming()
        }
        stream.recordCompactionActivity?.(payload)
      }
    }
    const compactionId = payloadCompactionId(payload)
    const durable = String(payload.durability || '').toLowerCase() === 'durable'
    if (source === 'manual' && status === 'completed' && (durable || compactionId)) {
      options.scheduleHistorySync()
    }
  }

  function handleRpcWarning(payload: WarningPayload) {
    if (isStaleEpoch(payload)) return
    if (!isCurrentSessionPayload(payload)) return
    // Silent compatibility warnings still own their stream sequence. Consuming
    // it before the display filter prevents a later replay from being accepted.
    if (!acceptStreamSeq(payload)) return
    if (
      payload.code === 'provider_reasoning_only_retry'
      || payload.code === 'provider_request_message_limit_recovery_success'
      || payload.code === 'context_auto_compaction_start'
      || payload.code === 'context_auto_compaction_retry'
    ) return
    // Let the view provide the locale-specific fallback when older gateways
    // omit a warning message.
    options.showWarningToast(String(payload.message || ''))
  }

  function handleRpcInputDisposition(payload: InputDispositionPayload) {
    if (isStaleEpoch(payload)) return
    if (!isCurrentSessionPayload(payload)) return
    if (!acceptStreamSeq(payload)) return
    // Primary sends also publish durable queued/applied disposition events.
    // Those events describe ingress ownership, not same-turn Steer UX. Older
    // gateways omitted intent for steer events, so only an explicit non-steer
    // intent is ignored for compatibility.
    if (payload.intent && payload.intent !== 'steer') return
    const disposition = payload.disposition
    if (!disposition) return
    const clientRequestId = String(payload.client_request_id || '')
    const clientMessageId = String(payload.client_message_id || '')
    const userMessageId = String(payload.user_message_id || '')
    const rawRevision = Number(payload.revision)
    const incomingRevision = Number.isInteger(rawRevision) && rawRevision >= 0
      ? rawRevision
      : undefined
    const promotedTurnId = String(payload.promoted_turn_id || '').trim()
    const recovery = String(payload.recovery || '').trim().toLowerCase()
    steerDelivery.disposition({
      clientRequestId,
      clientMessageId,
      userMessageId,
      disposition,
      revision: incomingRevision,
      turnId: String(payload.turn_id || payload.target_turn_id || payload.task_id || ''),
      promotedTurnId,
      promotedFromTurnId: String(
        payload.promoted_from_turn_id
        || payload.target_turn_id
        || payload.turn_id
        || '',
      ),
      appliedIteration: payload.applied_iteration,
      modelCallId: String(payload.model_call_id || ''),
    }, {
      retryable: payload.retryable === true,
      hint: recovery,
    })
  }

  function messageAlreadyPresent(candidate: ChatMessage): boolean {
    if (candidate.messageId) {
      return messages.value.some(message => message.messageId === candidate.messageId)
    }
    return messages.value.some(message =>
      message.role === candidate.role
      && message.text === candidate.text
      && message.provenanceKind === candidate.provenanceKind
      && message.provenanceSourceSessionKey === candidate.provenanceSourceSessionKey
      && message.provenanceSourceTool === candidate.provenanceSourceTool
      && (candidate.provenanceSourceTool === 'subagent_completion' || message.ts === candidate.ts))
  }

  function appendDurableEventMessage(message: ChatMessage) {
    if (messageAlreadyPresent(message)) return
    messages.value.push(message)
    // The pushed row is deliberately immediate. History remains authoritative
    // for ordering and durable metadata and will replace/dedupe by message id.
    options.scheduleHistorySync()
  }

  function handleRpcCronResult(payload: CronResultPayload) {
    if (isStaleEpoch(payload)) return
    if (!isCurrentSessionPayload(payload)) return
    if (!acceptStreamSeq(payload)) return
    const message = payload.message
    if (!message || typeof message !== 'object') return
    const text = typeof message.text === 'string' ? message.text : ''
    if (!text) return
    appendDurableEventMessage({
      role: typeof message.role === 'string' && message.role ? message.role : 'assistant',
      text,
      ts: message.timestamp ?? new Date().toISOString(),
      messageId: String(message.messageId || message.message_id || '') || undefined,
      provenanceKind: String(message.provenanceKind || 'cron'),
      provenanceSourceTool: String(message.provenanceSourceTool || ''),
      provenanceSourceSessionKey: String(message.provenanceSourceSessionKey || ''),
    })
  }

  function handleRpcSubagentCompletion(payload: SubagentCompletionPayload) {
    if (isStaleEpoch(payload)) return
    if (!isCurrentSessionPayload(payload)) return
    if (!acceptStreamSeq(payload)) return
    const durablePayload = { ...payload }
    delete durablePayload.session_key
    delete durablePayload.sessionKey
    delete durablePayload.stream_seq
    delete durablePayload.epoch
    // message_id correlates this push with the separately persisted transcript
    // row. It is transport metadata, not part of the subagent business payload
    // stored/rendered as JSON.
    delete durablePayload.message_id
    delete durablePayload.messageId
    const sourceSessionKey = String(payload.child_session_key || '')
    appendDurableEventMessage({
      role: 'system',
      text: JSON.stringify(durablePayload),
      ts: new Date().toISOString(),
      messageId: String(payload.message_id || payload.messageId || '') || undefined,
      provenanceKind: 'internal_system',
      provenanceSourceSessionKey: sourceSessionKey,
      provenanceSourceTool: 'subagent_completion',
    })
  }

  function handleRpcEpochChanged(payload: SessionEventPayload) {
    const ep = payload?.epoch
    if (typeof ep === 'number' && Number.isFinite(ep) && ep > currentEpoch.value) {
      activeTaskGroups.value.clear()
      currentEpoch.value = ep
    }
  }

  function handleRpcSessionsChanged(payload: SessionEventPayload) {
    if (isStaleEpoch(payload)) return
    if (!isCurrentSessionPayload(payload)) return
    const changedTask = (payload.changed_task || payload.changedTask) as ChatRunStatusSource['active_task']
    const changedTaskStatus = String(changedTask?.status || '').toLowerCase()
    if (changedTaskStatus === 'queued') options.taskOwnership?.noteQueued(changedTask || '')
    // changed_task describes which lifecycle row changed; it is deliberately
    // non-authoritative when Gateway snapshot generation failed. Only the
    // direct task.running event or an active_task/run_status projection may
    // replace the known running owner.
    // Terminal changed_task is consumed below together with the render
    // terminal. Clearing it here would erase the exact Stop target before the
    // cancellation disposition is classified.
    if (isStoppedTerminalSessionChange(payload)) {
      syncTerminalSessionChange(payload)
      return
    }
    if (
      sessionChangeIsTerminal(payload) &&
      bufferPendingTerminalEvent({ kind: 'session-change', payload })
    ) return
    const payloadTerminalTask = terminalSessionChangeTask(payload)
    const payloadTerminalTaskId = chatTaskId(payloadTerminalTask)
    const activeProjection = (payload.active_task || payload.activeTask) as ChatRunStatusSource['active_task']
    const carriesSettledContinuation = Boolean(
      sessionChangeIsTerminal(payload)
      && payloadTerminalTaskId
      && settledTaskIds.has(payloadTerminalTaskId)
      && chatTaskId(activeProjection),
    )
    const terminalMatchesRenderOwner = Boolean(
      sessionChangeIsTerminal(payload)
      && payloadTerminalTaskId
      && payloadTerminalTaskId === activeStreamTaskId.value,
    )
    if (
      !terminalMatchesRenderOwner
      && !carriesSettledContinuation
      && !isCurrentTaskPayload(payload)
    ) return
    if (sessionChangeIsTerminal(payload)) {
      const terminalStatus = String(payloadTerminalTask?.status || '').toLowerCase()
      const interrupted = ['cancelled', 'abandoned', 'interrupted'].includes(terminalStatus)
      if (activeTaskGroups.value.size > 0 && !interrupted) {
        if (stream.isStreaming.value) stream.endStreaming()
        options.applySessionRunState(activeTaskGroupRunState(payload))
        options.scheduleHistorySync()
        const terminalTaskId = chatTaskId(terminalSessionChangeTask(payload))
        if (terminalTaskId) options.taskOwnership?.noteTerminal(terminalTaskId)
        if (bindSuccessorAfterTerminal(terminalTaskId)) return
        activeStreamTaskId.value = FINISHED_STREAM_TASK_ID
        return
      }
      syncTerminalSessionChange(payload)
      return
    }
    const carriesRunState = (
      'run_status' in payload
      || 'runStatus' in payload
      || 'active_task' in payload
      || 'activeTask' in payload
      || 'last_task' in payload
      || 'lastTask' in payload
    )
    // Recents-only changes (for example an asynchronously generated title)
    // share the sessions.changed event name but carry no task state. They must
    // not collapse a live task snapshot or discard its steer capability.
    if (!carriesRunState) return
    options.applySessionRunState(payload)
  }

  function handleRpcTaskQueued(payload: SessionEventPayload) {
    // Task lifecycle pushes can arrive after a reconnect has advanced the
    // session epoch.  They must be rejected before touching the ownership
    // reducer, otherwise an old queued task can make an authoritative idle or
    // running session look busy again.
    if (isStaleEpoch(payload)) return
    if (!isCurrentSessionPayload(payload)) return
    const taskId = payloadTaskId(payload)
    if (!taskId) return
    const queued = options.taskOwnership?.noteQueued({ ...(payload || {}), status: 'queued' })
    // queued can describe another same-session task. Keep the fresh send on
    // PENDING until its chat.send response supplies the accepted task id.
    if (options.taskOwnership?.runningTaskId.value) return
    if (activeStreamTaskId.value === PENDING_STREAM_TASK_ID && !queued?.foreground) return
    options.applySessionRunState({ run_status: 'queued', active_task: { ...(payload || {}), status: 'queued' } })
  }

  function handleRpcTaskRunning(payload: SessionEventPayload) {
    // Keep lifecycle ownership on the same epoch boundary as stream frames.
    // In particular, a late task.running from the previous subscription must
    // not take Stop/render ownership away from the current task.
    if (isStaleEpoch(payload)) return
    if (!isCurrentSessionPayload(payload)) return
    // task.running is the authoritative "this task now owns the live stream"
    // signal once no chat.send response is pending. While PENDING, another
    // same-session task may start first; only the response can identify which
    // task belongs to this optimistic stream.
    const taskId = payloadTaskId(payload)
    if (!taskId) return
    options.taskOwnership?.noteRunning({ ...(payload || {}), status: 'running' })
    aborted.value = false
    const currentRenderTaskId = activeStreamTaskId.value
    if (
      currentRenderTaskId !== PENDING_STREAM_TASK_ID
      && (
        !stream.isStreaming.value
        || !currentRenderTaskId
        || currentRenderTaskId === FINISHED_STREAM_TASK_ID
        || currentRenderTaskId === STOPPED_STREAM_TASK_ID
        || currentRenderTaskId === taskId
      )
    ) {
      bindActiveStreamTask(taskId)
    } else if (currentRenderTaskId !== taskId && currentRenderTaskId !== PENDING_STREAM_TASK_ID) {
      pendingSuccessorRenderTaskId = taskId
    }
    options.applySessionRunState({ run_status: 'running', active_task: { ...(payload || {}), status: 'running' } })
    if (stream.isStreaming.value && !stream.streamHasVisibleOutput.value) {
      stream.setStreamActivity('Running')
    }
  }

  function handleRpcTaskGroupWaiting(payload: SessionEventPayload) {
    if (isStaleEpoch(payload)) return
    if (!acceptStreamSeq(payload)) return
    noteTaskGroupActive(payload)
  }

  function handleRpcTaskGroupSynthesizing(payload: SessionEventPayload) {
    if (isStaleEpoch(payload)) return
    if (!acceptStreamSeq(payload)) return
    noteTaskGroupActive(payload)
  }

  function handleRpcTaskGroupDone(payload: SessionEventPayload) {
    if (isStaleEpoch(payload)) return
    if (!acceptStreamSeq(payload)) return
    noteTaskGroupTerminal(payload, 'succeeded')
  }

  function handleRpcTaskGroupFailed(payload: SessionEventPayload) {
    if (isStaleEpoch(payload)) return
    if (!acceptStreamSeq(payload)) return
    noteTaskGroupTerminal(payload, 'failed')
  }

  function handleRpcRouterDecision(payload: RouterDecisionPayload) {
    if (isStaleEpoch(payload)) return
    if (bufferPendingStreamEvent('session.event.router_decision', payload)) return
    if (!isCurrentGenerationPayload(payload)) return
    if (!isCurrentTaskPayload(payload)) return
    if (!acceptStreamSeq(payload)) return
    options.queueRouterDecision(payload)
  }

  function handleRpcEnsembleProgress(payload: EnsembleProgressPayload) {
    if (isStaleEpoch(payload)) return
    if (bufferPendingStreamEvent('session.event.ensemble_progress', payload)) return
    if (!isCurrentGenerationPayload(payload)) return
    if (!isCurrentTaskPayload(payload)) return
    if (!acceptStreamSeq(payload)) return
    if (!stream.isStreaming.value) stream.startStreaming()
    // A lifecycle frame is a real gateway event. Keep the hard no-event timer
    // aligned with the wire even when heartbeat cadence is slower than progress.
    stream.resetStreamIdleTimer()
    options.appendEnsembleProgress(payload)
  }

  function handleRpcRouterControlReplay(payload: SessionEventPayload) {
    if (isStaleEpoch(payload)) return
    if (aborted.value) return
    if (bufferPendingStreamEvent('session.event.router_control_replay', payload)) return
    if (!isCurrentGenerationPayload(payload)) return
    if (!isCurrentTaskPayload(payload)) return
    if (!acceptStreamSeq(payload)) return
    options.handleRouterControlReplay()
  }

  function handleRpcAny(rawEvent: string, rawPayload: unknown) {
    const payloadObj = (rawPayload && typeof rawPayload === 'object' ? rawPayload : {}) as SessionEventPayload
    // The wildcard subscription observes approvals and compact task lifecycle
    // events in addition to ordinary stream frames.  Reject an older
    // subscription epoch before *any* of those branches can mutate run state
    // or task ownership; per-event guards below remain as defence in depth for
    // replayed/normalised payloads.
    if (isStaleEpoch(payloadObj)) return
    if (!isCurrentSessionPayload(payloadObj)) return
    if (rawEvent === 'session.event.answer_generation_reset') {
      handleRpcAnswerGenerationReset(payloadObj as AnswerGenerationResetPayload)
      return
    }
    if (
      terminalGenerationClosed
      && (rawEvent.endsWith('.error') || rawEvent.endsWith('.done') || rawEvent === 'chat.done')
    ) return
    if (!isCurrentGenerationPayload(payloadObj)) return
    const taskSucceededFallback = rawEvent === 'task.succeeded'
    const terminalStatus = eventTaskTerminalStatus(rawEvent)
    const terminalEvent = isTerminalEvent(rawEvent)
    // Rich done/error receipts are terminal ownership evidence too, even
    // though only compact task.* events encode a lifecycle status in the event
    // name. Without this, a successor whose done frame was buffered behind A
    // remains marked running after replay and blocks every future drain.
    const terminalTaskId = terminalEvent ? payloadTaskId(payloadObj) : ''
    const rawStatus = payloadObj.run_status || payloadObj.runStatus || payloadObj.status || ''
    const normalizedStatus = options.normalizeRunStatus(String(rawStatus))
    if (
      normalizedStatus === 'approval_pending' ||
      (typeof rawEvent === 'string' && rawEvent.includes('approval') && isCurrentSessionPayload(payloadObj))
    ) {
      if (!isCurrentSessionPayload(payloadObj)) return
      options.applySessionRunState({
        run_status: 'approval_pending',
        active_task: { ...(payloadObj || {}), status: 'approval_pending' },
      })
      return
    }
    if (
      terminalEvent &&
      bufferPendingTerminalEvent({ kind: 'event', event: rawEvent, payload: payloadObj })
    ) return
    const terminalOwnership = terminalTaskId
      ? options.taskOwnership?.noteTerminal(terminalTaskId)
      : undefined
    if (
      terminalOwnership?.wasQueued
      && !terminalOwnership.wasRunning
      && !terminalOwnership.wasStopTarget
      && terminalTaskId !== activeStreamTaskId.value
    ) {
      markTaskSettled(payloadObj)
      options.scheduleHistorySync()
      return
    }
    if (
      (
        rawEvent === 'session.event.thinking_start'
        || rawEvent === 'session.event.thinking'
        || rawEvent === 'session.event.thinking_end'
      ) &&
      bufferPendingStreamEvent(rawEvent, payloadObj)
    ) return
    // A stale task's terminal/done/error must not end the current turn's stream
    // or push its "Turn failed" into the live transcript (issue #344). Approvals
    // above stay ungated; everything below mutates the current turn.
    const terminalMatchesStop = terminalOwnership?.wasStopTarget === true
      || isStoppedCancelledTerminalEvent(terminalStatus, payloadObj)
    const terminalMatchesRenderOwner = Boolean(
      terminalStatus
      && payloadTaskId(payloadObj)
      && payloadTaskId(payloadObj) === activeStreamTaskId.value,
    )
    if (!terminalMatchesStop && !terminalMatchesRenderOwner && !isCurrentTaskPayload(payloadObj)) return
    if (terminalStatus) {
      if (!isCurrentSessionPayload(payloadObj)) return
      const terminalRunStatus = terminalStatus === 'succeeded' ? 'idle' : terminalStatus === 'abandoned' ? 'interrupted' : terminalStatus
      if (activeTaskGroups.value.size > 0) {
        options.applySessionRunState(activeTaskGroupRunState(payloadObj))
      } else {
        const terminalRunState = { run_status: terminalRunStatus, last_task: { ...(payloadObj || {}), status: terminalStatus } }
        options.applySessionRunState(terminalRunState)
      }
    }

    const normalized = normalizeTaskTerminalEvent(rawEvent, payloadObj)
    if (normalized && isStaleEpoch(payloadObj)) return
    if (normalized && !stream.isStreaming.value) {
      markTaskSettled(payloadObj)
      activeStreamTaskId.value = FINISHED_STREAM_TASK_ID
      options.scheduleHistorySync()
      return
    }

    const event = normalized ? normalized.event : rawEvent
    const payload = normalized ? normalized.payload : payloadObj

    if (typeof event !== 'string') return
    if (event.startsWith('session.event.') && isStaleEpoch(payload)) return
    if (!acceptStreamSeq(payload)) return
    if (event.startsWith('session.event.task_group.')) return
    if (event === 'sessions.changed') return

    if (event === 'session.event.thinking_start') {
      if (aborted.value) return
      stream.resetStreamIdleTimer()
      handleThinkingStart(payload)
      return
    }

    if (event === 'session.event.thinking') {
      if (aborted.value) return
      const thinkingText = (payload as SessionEventPayload).text
      if (typeof thinkingText !== 'string' || !thinkingText) return
      stream.resetStreamIdleTimer()
      appendThinkingDelta(thinkingText, payload)
      return
    }

    if (event === 'session.event.thinking_end') {
      if (aborted.value) return
      stream.resetStreamIdleTimer()
      handleThinkingEnd(payload)
      return
    }

    if (event.endsWith('.done') || event === 'chat.done') {
      markTaskSettled(payload)
      const donePayload = payload as ChatDoneUsagePayload
      const u = donePayload.usage || donePayload || {}
      const doneSuppressed = payload?.reason !== 'aborted'
        && doneDeliveryIsSuppressed(donePayload)
      if (u.input_tokens || u.output_tokens) {
        usageAccum.value.input += u.input_tokens || 0
        usageAccum.value.output += u.output_tokens || 0
        usageAccum.value.cacheRead += u.cached_tokens || 0
        usageAccum.value.cacheWrite += u.cache_write || 0
        if (u.cost_usd != null) usageAccum.value.cost = (usageAccum.value.cost || 0) + u.cost_usd
      }
      if (u.model) usageModel.value = u.model
      options.saveWidgetState()

      stream.reconcileFinalText(doneSuppressed ? '' : doneTextSnapshot(donePayload, u))

      if (payload?.reason === 'aborted') {
        options.clearPendingRouterDecision()
      } else {
        options.flushPendingRouterDecision()
      }
      // Done backfills the turn's reasoning: prefer the authoritative
      // reasoning_content, fall back to accumulated live thinking deltas.
      const rawReasoningContent = (payload as SessionEventPayload).reasoning_content
      const doneReasoning = typeof rawReasoningContent === 'string'
        ? rawReasoningContent.trim()
        : ''
      const liveThinking = streamThinking.value
      const foldedReasoning = stream.useReducer.value === true
        ? stream.getThinkingText?.().trim() || ''
        : ''
      const reasoningText = doneReasoning || foldedReasoning || liveThinking?.text.trim() || ''
      const reasoningSeconds = (() => {
        if (!liveThinking) return 0
        const now = Date.now()
        const serverStartedAt = liveThinking.serverStartedAt
        let elapsedMs = now - liveThinking.startedAt
        if (serverStartedAt != null) {
          const serverDoneAt = trustedReasoningDoneAt(
            (payload as SessionEventPayload).emitted_at,
            serverStartedAt,
            now,
          )
          if (serverDoneAt != null) elapsedMs = serverDoneAt - serverStartedAt
        }
        return Math.max(0, Math.floor(elapsedMs / 1000))
      })()
      clearLiveThinking()
      const messageCountBeforeEnd = messages.value.length
      stream.endStreaming(
        payload?.reason === 'aborted'
          ? { reason: 'aborted' }
          : doneSuppressed
            ? { suppressed: true }
            : undefined,
      )
      // endStreaming pushes the assistant message only when the turn kept
      // visible output; sentinel/empty bubbles must not record reasoning.
      // Bind reasoning to that exact bubble, then keep a record so the
      // measured duration survives history replacements.
      const completedMessage = messages.value[messageCountBeforeEnd]
      const completedAssistant = completedMessage?.role === 'assistant'
        ? completedMessage
        : null
      if (completedAssistant) {
        completedAssistant.turnId = doneTurnId(donePayload) ?? completedAssistant.turnId
        recordTurnActivity(completedAssistant)
      }
      if (completedAssistant && payload?.reason !== 'aborted') {
        completedAssistant.turnInputMode = doneTurnProvenance(
          donePayload,
          'input_mode',
          'inputMode',
        )
        completedAssistant.turnRunKind = doneTurnProvenance(
          donePayload,
          'run_kind',
          'runKind',
        )
        // task.succeeded is a lifecycle-only fallback when the richer done
        // receipt went missing; do not mislabel its task metadata as usage.
        if (!taskSucceededFallback) completedAssistant.usage = doneUsagePayload(donePayload)
        if (u.model) completedAssistant.model = u.model
        if (u.input_tokens) completedAssistant.input_tokens = u.input_tokens
        if (u.output_tokens) completedAssistant.output_tokens = u.output_tokens
      }
      if (reasoningText && payload?.reason !== 'aborted' && completedAssistant) {
        completedAssistant.reasoning = { text: reasoningText, seconds: reasoningSeconds }
        recordTurnReasoning(
          reasoningText,
          reasoningSeconds,
          completedAssistant.text.trim(),
          completedAssistant.reasoningBlocks,
        )
      }
      options.scheduleHistorySync()

      if (payload?.reason === 'aborted') {
        const cancelledRunState = { run_status: 'cancelled', last_task: { ...(payload || {}), status: 'cancelled' } }
        options.applySessionRunState(cancelledRunState)
      } else if (activeTaskGroups.value.size > 0) {
        options.applySessionRunState(activeTaskGroupRunState({ reason: 'task_group_active' }))
      } else {
        options.applySessionRunState({ run_status: 'idle', last_task: { status: 'succeeded' } })
      }

      const terminalTaskId = payloadTaskId(payload)
      if (
        pendingQueue.value.length > 0
        && payload?.reason !== 'aborted'
        && !options.taskOwnership?.hasAuthoritativeWork.value
      ) {
        options.schedulePendingDrainAfterTerminal()
      }
      if (!bindSuccessorAfterTerminal(terminalTaskId)) {
        activeStreamTaskId.value = FINISHED_STREAM_TASK_ID
      }
    } else if (event.endsWith('.error')) {
      markTaskSettled(payload)
      options.clearPendingRouterDecision()
      clearLiveThinking()
      const terminalTurnId = payloadTaskId(payload)
      const turnOutcome = normalizeTurnOutcome({
        ...(payload || {}),
        turn_id: terminalTurnId,
        status: 'failed',
      })
      if (turnOutcome?.statusHistory?.length) {
        stream.restoreStatusHistory?.(turnOutcome.statusHistory)
      }
      const messageCountBeforeEnd = messages.value.length
      stream.endStreaming()
      const completedMessage = messages.value[messageCountBeforeEnd]
      if (completedMessage?.role === 'assistant') {
        completedMessage.turnId = terminalTurnId || completedMessage.turnId
        completedMessage.turnOutcome = turnOutcome
      }
      const rawErrorCode = (payload as { code?: unknown })?.code
      const errorCode = usageAccountingErrorCode(payload)
        ?? (typeof rawErrorCode === 'string' ? rawErrorCode : undefined)
      const serverMessage = eventSessionErrorMessage(payload)
      messages.value.push({
        role: 'error',
        text: localizedChatErrorMessage(
          errorCode,
          serverMessage,
          turnOutcome?.replaySafe === true,
        ),
        errorCode,
        turnId: terminalTurnId || undefined,
        turnOutcome,
        terminalNotice: true,
        ts: new Date().toISOString(),
      })
      options.scheduleHistorySync()
      if (activeTaskGroups.value.size > 0) {
        options.applySessionRunState(activeTaskGroupRunState(payload))
      } else {
        options.applySessionRunState({ run_status: 'failed', last_task: { ...(payload || {}), status: 'failed' } })
      }
      const terminalTaskId = payloadTaskId(payload)
      if (
        pendingQueue.value.length > 0
        && activeTaskGroups.value.size === 0
        && !options.taskOwnership?.hasAuthoritativeWork.value
      ) {
        options.schedulePendingDrainAfterTerminal()
      }
      if (!bindSuccessorAfterTerminal(terminalTaskId)) {
        activeStreamTaskId.value = FINISHED_STREAM_TASK_ID
      }
    }
  }

  let connectionLostNoted = false
  let connectionLostNotice: ChatMessage | null = null
  let connectionStateGeneration = 0

  function clearConnectionLostStatus() {
    connectionLostNoted = false
    if (!connectionLostNotice) return
    const noticeIndex = messages.value.indexOf(connectionLostNotice)
    if (noticeIndex >= 0) messages.value.splice(noticeIndex, 1)
    connectionLostNotice = null
  }

  function handleRpcConnectionState(state: string) {
    const stateGeneration = ++connectionStateGeneration
    const recovery = options.handleSessionConnectionState?.(state)
    if (state === 'connected') {
      clearConnectionLostStatus()
      stream.hideThinkingIndicator()
      const subscription = recovery?.live ?? options.subscribeSession?.()
      void Promise.resolve(subscription)
        .then((subscribed) => {
          if (isAuthoritativeSessionSubscription(subscribed)) {
            return options.onSessionSubscribed?.()
          }
        })
        .catch((error: unknown) => {
          console.warn(
            'Session recovery after reconnect failed:',
            error instanceof Error ? error.message : error,
          )
        })
      if (sessionKey.value) {
        const connectedSessionKey = sessionKey.value
        // Preserve critical frame ordering after reconnect without waiting for a
        // potentially slow history response before refreshing independent UI.
        const criticalRequestsQueued = recovery?.criticalRequestsQueued
          ?? Promise.resolve()
        void criticalRequestsQueued.then(() => {
          if (
            connectionStateGeneration === stateGeneration
            && sessionKey.value === connectedSessionKey
          ) {
            options.loadCurrentSessionUsage()
            void options.refreshRunModePreference?.()
          }
        })
      }
      if (!recovery) {
        options.loadCurrentSessionUsage()
        options.loadHistory?.()
      }
      // Reconnect restores transport liveness, not model progress. Keep the
      // 20-second provider-silence clock honest while re-arming the separate
      // hard-idle watchdog.
      if (stream.isStreaming.value) stream.resetStreamIdleTimer({ progress: false })
    }
    if (state === 'disconnected' && stream.isStreaming.value) {
      // Keep the idle watchdog armed so a run whose events never resume still
      // times out honestly. The row is transient and removed after reconnect.
      stream.showThinkingIndicator()
      if (!connectionLostNoted) {
        connectionLostNoted = true
        connectionLostNotice = {
          role: 'system',
          text: 'Connection lost — trying to reconnect…',
          ts: new Date().toISOString(),
        }
        messages.value.push(connectionLostNotice)
      }
    }
  }

  const handlers: ChatRpcSubscriptionHandlers = {
    onAnswerGenerationReset: handleRpcAnswerGenerationReset,
    onTextDelta: handleRpcTextDelta,
    onToolUseStart: handleRpcToolUseStart,
    onToolUseDelta: handleRpcToolUseDelta,
    onToolUseEnd: handleRpcToolUseEnd,
    onToolResult: handleRpcToolResult,
    onArtifact: handleRpcArtifact,
    onStateChange: handleRpcStateChange,
    onRunHeartbeat: handleRpcRunHeartbeat,
    onProviderActivity: handleRpcProviderActivity,
    onCompaction: handleRpcCompaction,
    onWarning: handleRpcWarning,
    onInputDisposition: handleRpcInputDisposition,
    onCronResult: handleRpcCronResult,
    onSubagentCompletion: handleRpcSubagentCompletion,
    onEpochChanged: handleRpcEpochChanged,
    onSessionsChanged: handleRpcSessionsChanged,
    onTaskQueued: handleRpcTaskQueued,
    onTaskRunning: handleRpcTaskRunning,
    onTaskGroupWaiting: handleRpcTaskGroupWaiting,
    onTaskGroupSynthesizing: handleRpcTaskGroupSynthesizing,
    onTaskGroupDone: handleRpcTaskGroupDone,
    onTaskGroupFailed: handleRpcTaskGroupFailed,
    onRouterDecision: handleRpcRouterDecision,
    onEnsembleProgress: handleRpcEnsembleProgress,
    onRouterControlReplay: handleRpcRouterControlReplay,
    onAny: handleRpcAny,
    onConnectionState: handleRpcConnectionState,
  }

  return {
    handlers,
    bindActiveStreamTask,
    restoreLiveTurnSnapshot,
    streamThinkingText,
    streamThinkingElapsedText,
    attachTurnReasoning,
  }
}
