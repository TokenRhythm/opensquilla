import { computed, onScopeDispose, ref, watch, type Ref } from 'vue'
import type {
  ChatMessage,
  ChatPendingItem,
  ChatRunStatus,
  ChatRunStatusSource,
  ChatUsagePayload,
} from '@/types/chat'
import type {
  ArtifactPayload,
  CompactionPayload,
  CronResultPayload,
  EnsembleProgressPayload,
  RouterDecisionPayload,
  SessionEventPayload,
  StreamEventEnvelope,
  SubagentCompletionPayload,
  TextDeltaPayload,
  ToolDeltaPayload,
  ToolResultPayload,
  ToolUsePayload,
  WarningPayload,
} from '@/types/rpc'
import type { ChatRpcSubscriptionHandlers } from '@/composables/chat/useChatRpcSubscriptions'
import type { FrameInput } from '@/types/turnlog'
import type { FoldLiveTurnMode } from '@/composables/chat/useChatTurnLog'
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
import { stoppedOutputNoticeMessage } from '@/composables/chat/stoppedOutputNotice'
import { localizedChatErrorMessage } from '@/utils/chat/errors'

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
  endStreaming: (opts?: { reason?: string }) => void
  appendDelta: (text: string) => void
  scheduleRender: () => void
  appendToolCall: (payload: ToolUsePayload) => void
  appendToolDelta: (payload: ToolDeltaPayload) => void
  appendToolResult: (payload: ToolResultPayload) => void
  appendArtifact: (payload: ArtifactPayload) => void
  reconcileFinalText: (finalText: string | null | undefined) => void
  resetStreamIdleTimer: () => void
  clearStreamIdleTimer: () => void
  setStreamActivity: (label: string) => void
  showThinkingIndicator: () => void
  hideThinkingIndicator: () => void
  // live-turn shadow log: the thinking ref lives here, so this composable appends
  // its own thinking frames into the stream-owned log after the legacy mutation.
  appendFrame: (frame: FrameInput) => void
  useReducer: Ref<FoldLiveTurnMode>
}

export interface UseChatRpcEventHandlersOptions {
  sessionKey: Ref<string>
  currentEpoch: Ref<number>
  lastStreamSeq: Ref<number>
  activeTaskGroups: Ref<Set<string>>
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
  showCompactionToast: (payload: CompactionPayload, meta?: Record<string, unknown>) => void
  showWarningToast: (message: string) => void
  scheduleHistorySync: () => void
  schedulePendingDrainAfterTerminal: () => void
  popAllPendingIntoComposer: () => boolean
  saveWidgetState: () => void
  subscribeSession: () => void
  loadHistory: () => void
  loadCurrentSessionUsage: () => void
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
  decision_id?: string
}

type ChatDoneUsagePayload = SessionEventPayload & ChatDoneUsageFields & {
  usage?: ChatDoneUsageFields
}

type BufferedTerminalEvent =
  | { kind: 'event'; event: string; payload: SessionEventPayload; priority: number }
  | { kind: 'session-change'; payload: SessionEventPayload; priority: number }

type BufferedTerminalEventInput =
  | { kind: 'event'; event: string; payload: SessionEventPayload; priority?: number }
  | { kind: 'session-change'; payload: SessionEventPayload; priority?: number }

type BufferedPendingStreamEvent = {
  event: string
  payload: SessionEventPayload
}

const MAX_PENDING_TASK_BUCKETS = 8
const MAX_PENDING_STREAM_EVENTS_PER_TASK = 64

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

  // Live thinking deltas for the current turn (session.event.thinking).
  const streamThinking = ref<{ text: string; startedAt: number } | null>(null)
  const turnReasoningLog: TurnReasoningRecord[] = []
  const pendingTerminalEvents = new Map<string, BufferedTerminalEvent>()
  const pendingStreamEvents = new Map<string, BufferedPendingStreamEvent[]>()
  const settledTaskIds = new Set<string>()

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
    if (activeStreamTaskId.value !== PENDING_STREAM_TASK_ID) return false
    if (!isCurrentSessionPayload(entry.payload)) return false
    if (isStaleEpoch(entry.payload)) return false
    const taskId = payloadTaskId(entry.payload)
    if (!taskId) return false
    const priority = entry.priority ?? (entry.kind === 'event' ? terminalEventPriority(entry.event) : 1)
    const existing = pendingTerminalEvents.get(taskId)
    if (!existing || priority >= existing.priority) {
      if (!existing && pendingTerminalEvents.size >= MAX_PENDING_TASK_BUCKETS) {
        const oldestTaskId = pendingTerminalEvents.keys().next().value
        if (typeof oldestTaskId === 'string') pendingTerminalEvents.delete(oldestTaskId)
      }
      pendingTerminalEvents.set(taskId, { ...entry, priority } as BufferedTerminalEvent)
    }
    return true
  }

  function markTaskSettled(payload: SessionEventPayload) {
    const taskId = payloadTaskId(payload)
    if (taskId) settledTaskIds.add(taskId)
    pendingTerminalEvents.clear()
    pendingStreamEvents.clear()
  }

  function bufferPendingStreamEvent(
    event: string,
    payload: SessionEventPayload,
  ): boolean {
    if (activeStreamTaskId.value !== PENDING_STREAM_TASK_ID) return false
    if (!isCurrentSessionPayload(payload)) return false
    if (isStaleEpoch(payload)) return false
    const taskId = payloadTaskId(payload)
    if (!taskId) return false

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
    buffered.push({ event, payload })
    return true
  }

  function replayPendingStreamEvent(entry: BufferedPendingStreamEvent) {
    const { event, payload } = entry
    if (event === 'session.event.text_delta') {
      handleRpcTextDelta(payload as TextDeltaPayload)
    } else if (event === 'session.event.tool_use_start') {
      handleRpcToolUseStart(payload as ToolUsePayload)
    } else if (event === 'session.event.tool_use_delta') {
      handleRpcToolUseDelta(payload as ToolDeltaPayload)
    } else if (event === 'session.event.tool_result') {
      handleRpcToolResult(payload as ToolResultPayload)
    } else if (event === 'session.event.artifact') {
      handleRpcArtifact(payload as ArtifactPayload)
    } else if (event === 'session.event.state_change') {
      handleRpcStateChange(payload)
    } else if (event === 'session.event.run_heartbeat') {
      handleRpcRunHeartbeat(payload)
    } else if (event === 'session.event.router_decision') {
      handleRpcRouterDecision(payload as RouterDecisionPayload)
    } else if (event === 'session.event.ensemble_progress') {
      handleRpcEnsembleProgress(payload as EnsembleProgressPayload)
    } else if (event === 'session.event.router_control_replay') {
      handleRpcRouterControlReplay(payload)
    } else if (event === 'session.event.thinking') {
      handleRpcAny(event, payload)
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
    for (const entry of bufferedStream) replayPendingStreamEvent(entry)
    if (!bufferedTerminal) return
    if (bufferedTerminal.kind === 'session-change') {
      handleRpcSessionsChanged(bufferedTerminal.payload)
    } else {
      handleRpcAny(bufferedTerminal.event, bufferedTerminal.payload)
    }
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

  function appendThinkingDelta(text: string) {
    if (!text) return
    if (!stream.isStreaming.value) stream.startStreaming()
    const current = streamThinking.value
    streamThinking.value = current
      ? { text: current.text + text, startedAt: current.startedAt }
      : { text, startedAt: Date.now() }
    // The fold concats the same text into its thinkingText. Gating already
    // passed upstream (handleRpcAny), so this frame mirrors an accepted delta.
    if (stream.useReducer.value) stream.appendFrame({ kind: 'thinking', text, at: Date.now() })
    // Reasoning growth must re-pin the thread to the bottom just like answer
    // text and tool deltas. Schedule the same batched render/scroll flush so a
    // long thinking phase keeps following the live turn instead of only
    // snapping down once answer text starts streaming.
    stream.scheduleRender()
  }

  function clearLiveThinking() {
    streamThinking.value = null
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
    if (turnReasoningLog.length === 0) return
    const list = messages.value
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
        break
      }
    }
  }

  function recordTurnReasoning(text: string, seconds: number, messageText: string) {
    turnReasoningLog.push({ sessionKey: sessionKey.value, text, seconds, messageText })
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
    return usage
  }

  watch(sessionKey, () => {
    streamThinking.value = null
    turnReasoningLog.length = 0
    pendingTerminalEvents.clear()
    pendingStreamEvents.clear()
    settledTaskIds.clear()
    // The newly shown session has its own (possibly running) task; forget the
    // previous session's active task so we stay lenient until it re-asserts.
    activeStreamTaskId.value = ''
  })
  watch(activeStreamTaskId, (taskId) => {
    if (taskId === PENDING_STREAM_TASK_ID) {
      pendingTerminalEvents.clear()
      pendingStreamEvents.clear()
      settledTaskIds.clear()
    } else if (!taskId || taskId === FINISHED_STREAM_TASK_ID || taskId === STOPPED_STREAM_TASK_ID) {
      pendingTerminalEvents.clear()
      pendingStreamEvents.clear()
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
    const decision = decideStreamSeq(payload, sessionKey.value, lastStreamSeq.value)
    if (decision.accepted) lastStreamSeq.value = decision.nextStreamSeq
    return decision.accepted
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
  }

  function isStoppedCancelledTerminalEvent(terminalStatus: string, payload: SessionEventPayload): boolean {
    return (
      activeStreamTaskId.value === STOPPED_STREAM_TASK_ID &&
      isCurrentSessionPayload(payload) &&
      terminalStatus === 'cancelled'
    )
  }

  function isStoppedTerminalSessionChange(payload: SessionEventPayload): boolean {
    if (activeStreamTaskId.value !== STOPPED_STREAM_TASK_ID) return false
    if (!isCurrentSessionPayload(payload)) return false
    if (!sessionChangeIsTerminal(payload)) return false
    const state = options.sessionRunStatus(payload)
    return state.status === 'cancelled' || state.status === 'interrupted'
  }

  function syncTerminalSessionChange(payload: SessionEventPayload = {}) {
    if (!isCurrentSessionPayload(payload)) return false
    activeTaskGroups.value.clear()
    const state = options.sessionRunStatus(payload)
    const interrupted = state.status === 'cancelled' || state.status === 'interrupted'
    if (stream.isStreaming.value) stream.endStreaming(interrupted ? { reason: 'aborted' } : undefined)
    options.applySessionRunState(payload)
    materializeStoppedOutputNotice(state)
    options.scheduleHistorySync()
    if (interrupted) {
      options.popAllPendingIntoComposer()
    } else {
      options.schedulePendingDrainAfterTerminal()
    }
    activeStreamTaskId.value = FINISHED_STREAM_TASK_ID
    return true
  }

  function handleRpcTextDelta(payload: TextDeltaPayload) {
    if (isStaleEpoch(payload)) return
    if (bufferPendingStreamEvent('session.event.text_delta', payload)) return
    if (!isCurrentTaskPayload(payload)) return
    if (!acceptStreamSeq(payload)) return
    stream.resetStreamIdleTimer()
    options.markEnsembleHandoff()
    stream.appendDelta(payload.text || '')
  }

  function handleRpcToolUseStart(payload: ToolUsePayload) {
    if (isStaleEpoch(payload)) return
    if (aborted.value) return
    if (bufferPendingStreamEvent('session.event.tool_use_start', payload)) return
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
    if (!isCurrentTaskPayload(payload)) return
    if (!acceptStreamSeq(payload)) return
    stream.resetStreamIdleTimer()
    options.markEnsembleHandoff()
    stream.appendToolDelta(payload)
  }

  function handleRpcToolResult(payload: ToolResultPayload) {
    if (isStaleEpoch(payload)) return
    if (aborted.value) return
    if (bufferPendingStreamEvent('session.event.tool_result', payload)) return
    if (!isCurrentTaskPayload(payload)) return
    if (!acceptStreamSeq(payload)) return
    stream.resetStreamIdleTimer()
    stream.appendToolResult(payload)
  }

  function handleRpcArtifact(payload: ArtifactPayload) {
    if (isStaleEpoch(payload)) return
    if (aborted.value) return
    if (bufferPendingStreamEvent('session.event.artifact', payload)) return
    if (!isCurrentTaskPayload(payload)) return
    if (!acceptStreamSeq(payload)) return
    stream.resetStreamIdleTimer()
    stream.appendArtifact(payload)
  }

  function handleRpcStateChange(payload: SessionEventPayload) {
    if (isStaleEpoch(payload)) return
    if (!payload || aborted.value) return
    if (bufferPendingStreamEvent('session.event.state_change', payload)) return
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
    if (!isCurrentTaskPayload(payload)) return
    if (!acceptStreamSeq(payload)) return
    if (!stream.isStreaming.value) stream.startStreaming()
    stream.resetStreamIdleTimer()
    if (stream.streamBubble.value && !stream.streamHasVisibleOutput.value) {
      const phase = String(payload.phase || '')
      if (phase.startsWith('ensemble_proposers')) {
        stream.setStreamActivity('Generating candidates')
      } else if (phase.startsWith('ensemble_aggregator')) {
        stream.setStreamActivity('Synthesizing candidates')
      } else {
        stream.setStreamActivity('Planning next step')
      }
    } else if (!stream.streamBubble.value) {
      stream.showThinkingIndicator()
    }
  }

  function handleRpcCompaction(payload: CompactionPayload, meta: unknown) {
    if (isStaleEpoch(payload)) return
    if (!acceptStreamSeq(payload)) return
    const safeMeta = (meta && typeof meta === 'object' ? meta : {}) as Record<string, unknown>
    options.showCompactionToast(payload || {}, safeMeta)
  }

  function handleRpcWarning(payload: WarningPayload) {
    if (isStaleEpoch(payload)) return
    if (!isCurrentSessionPayload(payload)) return
    // Silent compatibility warnings still own their stream sequence. Consuming
    // it before the display filter prevents a later replay from being accepted.
    if (!acceptStreamSeq(payload)) return
    if (payload.code === 'provider_reasoning_only_retry') return
    // Let the view provide the locale-specific fallback when older gateways
    // omit a warning message.
    options.showWarningToast(String(payload.message || ''))
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
    if (isStoppedTerminalSessionChange(payload)) {
      syncTerminalSessionChange(payload)
      return
    }
    if (
      sessionChangeIsTerminal(payload) &&
      bufferPendingTerminalEvent({ kind: 'session-change', payload })
    ) return
    if (!isCurrentTaskPayload(payload)) return
    if (sessionChangeIsTerminal(payload)) {
      markTaskSettled(payload)
      const state = options.sessionRunStatus(payload)
      const interrupted = state.status === 'cancelled' || state.status === 'interrupted'
      if (activeTaskGroups.value.size > 0 && !interrupted) {
        if (stream.isStreaming.value) stream.endStreaming()
        options.applySessionRunState(activeTaskGroupRunState(payload))
        options.scheduleHistorySync()
        return
      }
      syncTerminalSessionChange(payload)
      return
    }
    options.applySessionRunState(payload)
  }

  function handleRpcTaskQueued(payload: SessionEventPayload) {
    if (!isCurrentSessionPayload(payload)) return
    // queued can describe another same-session task. Keep the fresh send on
    // PENDING until its chat.send response supplies the accepted task id.
    options.applySessionRunState({ run_status: 'queued', active_task: { ...(payload || {}), status: 'queued' } })
  }

  function handleRpcTaskRunning(payload: SessionEventPayload) {
    if (!isCurrentSessionPayload(payload)) return
    // task.running is the authoritative "this task now owns the live stream"
    // signal once no chat.send response is pending. While PENDING, another
    // same-session task may start first; only the response can identify which
    // task belongs to this optimistic stream.
    const taskId = payloadTaskId(payload)
    if (taskId && activeStreamTaskId.value !== PENDING_STREAM_TASK_ID) {
      activeStreamTaskId.value = taskId
    }
    options.applySessionRunState({ run_status: 'running', active_task: { ...(payload || {}), status: 'running' } })
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
    if (!isCurrentTaskPayload(payload)) return
    if (!acceptStreamSeq(payload)) return
    options.queueRouterDecision(payload)
  }

  function handleRpcEnsembleProgress(payload: EnsembleProgressPayload) {
    if (isStaleEpoch(payload)) return
    if (bufferPendingStreamEvent('session.event.ensemble_progress', payload)) return
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
    if (!isCurrentTaskPayload(payload)) return
    if (!acceptStreamSeq(payload)) return
    options.handleRouterControlReplay()
  }

  function handleRpcAny(rawEvent: string, rawPayload: unknown) {
    const payloadObj = (rawPayload && typeof rawPayload === 'object' ? rawPayload : {}) as SessionEventPayload
    const taskSucceededFallback = rawEvent === 'task.succeeded'
    const terminalStatus = eventTaskTerminalStatus(rawEvent)
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
      isTerminalEvent(rawEvent) &&
      bufferPendingTerminalEvent({ kind: 'event', event: rawEvent, payload: payloadObj })
    ) return
    if (
      rawEvent === 'session.event.thinking' &&
      bufferPendingStreamEvent(rawEvent, payloadObj)
    ) return
    // A stale task's terminal/done/error must not end the current turn's stream
    // or push its "Turn failed" into the live transcript (issue #344). Approvals
    // above stay ungated; everything below mutates the current turn.
    if (!isStoppedCancelledTerminalEvent(terminalStatus, payloadObj) && !isCurrentTaskPayload(payloadObj)) return
    if (terminalStatus) {
      if (!isCurrentSessionPayload(payloadObj)) return
      const terminalRunStatus = terminalStatus === 'succeeded' ? 'idle' : terminalStatus === 'abandoned' ? 'interrupted' : terminalStatus
      if (activeTaskGroups.value.size > 0) {
        options.applySessionRunState(activeTaskGroupRunState(payloadObj))
      } else {
        const terminalRunState = { run_status: terminalRunStatus, last_task: { ...(payloadObj || {}), status: terminalStatus } }
        options.applySessionRunState(terminalRunState)
        if (!stream.isStreaming.value) {
          materializeStoppedOutputNotice(options.sessionRunStatus(terminalRunState))
        }
      }
    }

    const normalized = normalizeTaskTerminalEvent(rawEvent, payloadObj)
    if (normalized && isStaleEpoch(payloadObj)) return
    if (normalized && !stream.isStreaming.value) {
      markTaskSettled(payloadObj)
      activeStreamTaskId.value = FINISHED_STREAM_TASK_ID
      return
    }

    const event = normalized ? normalized.event : rawEvent
    const payload = normalized ? normalized.payload : payloadObj

    if (typeof event !== 'string') return
    if (event.startsWith('session.event.') && isStaleEpoch(payload)) return
    if (!acceptStreamSeq(payload)) return
    if (event.startsWith('session.event.task_group.')) return
    if (event === 'sessions.changed') return

    if (event === 'session.event.thinking') {
      if (aborted.value) return
      const thinkingText = (payload as SessionEventPayload).text
      if (typeof thinkingText !== 'string' || !thinkingText) return
      stream.resetStreamIdleTimer()
      appendThinkingDelta(thinkingText)
      return
    }

    if (event.endsWith('.done') || event === 'chat.done') {
      markTaskSettled(payload)
      const donePayload = payload as ChatDoneUsagePayload
      const u = donePayload.usage || donePayload || {}
      if (u.input_tokens || u.output_tokens) {
        usageAccum.value.input += u.input_tokens || 0
        usageAccum.value.output += u.output_tokens || 0
        usageAccum.value.cacheRead += u.cached_tokens || 0
        usageAccum.value.cacheWrite += u.cache_write || 0
        if (u.cost_usd != null) usageAccum.value.cost = (usageAccum.value.cost || 0) + u.cost_usd
      }
      if (u.model) usageModel.value = u.model
      options.saveWidgetState()

      stream.reconcileFinalText(doneTextSnapshot(donePayload, u))

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
      const reasoningText = doneReasoning || liveThinking?.text.trim() || ''
      const reasoningSeconds = liveThinking
        ? Math.max(0, Math.floor((Date.now() - liveThinking.startedAt) / 1000))
        : 0
      clearLiveThinking()
      const messageCountBeforeEnd = messages.value.length
      stream.endStreaming()
      // endStreaming pushes the assistant message only when the turn kept
      // visible output; sentinel/empty bubbles must not record reasoning.
      // Bind reasoning to that exact bubble, then keep a record so the
      // measured duration survives history replacements.
      const completedMessage = messages.value[messageCountBeforeEnd]
      const completedAssistant = completedMessage?.role === 'assistant'
        ? completedMessage
        : null
      if (completedAssistant && payload?.reason !== 'aborted') {
        // task.succeeded is a lifecycle-only fallback when the richer done
        // receipt went missing; do not mislabel its task metadata as usage.
        if (!taskSucceededFallback) completedAssistant.usage = doneUsagePayload(donePayload)
        if (u.model) completedAssistant.model = u.model
        if (u.input_tokens) completedAssistant.input_tokens = u.input_tokens
        if (u.output_tokens) completedAssistant.output_tokens = u.output_tokens
      }
      if (reasoningText && payload?.reason !== 'aborted' && completedAssistant) {
        completedAssistant.reasoning = { text: reasoningText, seconds: reasoningSeconds }
        recordTurnReasoning(reasoningText, reasoningSeconds, completedAssistant.text.trim())
      }
      options.scheduleHistorySync()

      if (payload?.reason === 'aborted') {
        options.popAllPendingIntoComposer()
        const cancelledRunState = { run_status: 'cancelled', last_task: { ...(payload || {}), status: 'cancelled' } }
        options.applySessionRunState(cancelledRunState)
        materializeStoppedOutputNotice(options.sessionRunStatus(cancelledRunState))
      } else if (activeTaskGroups.value.size > 0) {
        options.applySessionRunState(activeTaskGroupRunState({ reason: 'task_group_active' }))
      } else {
        options.applySessionRunState({ run_status: 'idle', last_task: { status: 'succeeded' } })
      }

      if (pendingQueue.value.length > 0 && payload?.reason !== 'aborted') {
        options.schedulePendingDrainAfterTerminal()
      }
      activeStreamTaskId.value = FINISHED_STREAM_TASK_ID
    } else if (event.endsWith('.error')) {
      markTaskSettled(payload)
      options.clearPendingRouterDecision()
      clearLiveThinking()
      stream.endStreaming()
      const rawErrorCode = (payload as { code?: unknown })?.code
      const errorCode = typeof rawErrorCode === 'string' ? rawErrorCode : undefined
      const serverMessage = eventSessionErrorMessage(payload)
      messages.value.push({
        role: 'error',
        text: localizedChatErrorMessage(errorCode, serverMessage),
        errorCode,
        terminalNotice: true,
        ts: new Date().toISOString(),
      })
      options.scheduleHistorySync()
      if (activeTaskGroups.value.size > 0) {
        options.applySessionRunState(activeTaskGroupRunState(payload))
      } else {
        options.applySessionRunState({ run_status: 'failed', last_task: { ...(payload || {}), status: 'failed' } })
      }
      activeStreamTaskId.value = FINISHED_STREAM_TASK_ID
    }
  }

  function materializeStoppedOutputNotice(state: ChatRunStatus) {
    const notice = stoppedOutputNoticeMessage(messages.value, state)
    if (notice) messages.value.push(notice)
  }

  let connectionLostNoted = false
  function handleRpcConnectionState(state: string) {
    if (state === 'connected' && sessionKey.value) {
      connectionLostNoted = false
      stream.hideThinkingIndicator()
      options.subscribeSession()
      options.loadCurrentSessionUsage()
      options.loadHistory()
      if (stream.isStreaming.value) stream.resetStreamIdleTimer()
    }
    if (state === 'disconnected' && stream.isStreaming.value) {
      // Surface the drop instead of silently freezing the work-card, and keep the
      // idle watchdog ARMED (do not clear it) so a run whose events never resume
      // still times out honestly instead of spinning forever on a dead socket.
      stream.showThinkingIndicator()
      if (!connectionLostNoted) {
        connectionLostNoted = true
        messages.value.push({ role: 'system', text: 'Connection lost — trying to reconnect…', ts: new Date().toISOString() })
      }
    }
  }

  const handlers: ChatRpcSubscriptionHandlers = {
    onTextDelta: handleRpcTextDelta,
    onToolUseStart: handleRpcToolUseStart,
    onToolUseDelta: handleRpcToolUseDelta,
    onToolResult: handleRpcToolResult,
    onArtifact: handleRpcArtifact,
    onStateChange: handleRpcStateChange,
    onRunHeartbeat: handleRpcRunHeartbeat,
    onCompaction: handleRpcCompaction,
    onWarning: handleRpcWarning,
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
    streamThinkingText,
    streamThinkingElapsedText,
    attachTurnReasoning,
  }
}
