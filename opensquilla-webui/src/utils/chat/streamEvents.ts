import type { SessionEventPayload, StreamEventEnvelope } from '@/types/chat'
import type { ConversationSemanticEventKind } from '@/modules/conversationEvents'
import {
  createConversationRuntime,
  type ConversationCursorSignal,
} from '@/modules/conversationRuntime'

export interface StreamSeqDecision {
  accepted: boolean
  nextStreamSeq: number
}

/**
 * Translate legacy v4 spellings at the transport edge.  ConversationRuntime
 * intentionally accepts only these canonical facts, so aliases do not leak
 * into domain Modules.  Invalid optional cursor values are omitted to retain
 * the v4 client's historical leniency for unversioned events.
 */
export function conversationCursorSignal(source: unknown): ConversationCursorSignal {
  const value = source && typeof source === 'object'
    ? source as Record<string, unknown>
    : {}
  const text = (...keys: string[]): string | undefined => {
    for (const key of keys) {
      const candidate = value[key]
      // Do not trim legacy identifiers at this compatibility edge. The old
      // helpers compared the first truthy wire spelling byte-for-byte; domain
      // Contract validation can tighten this in a later versioned adapter.
      if (typeof candidate === 'string' && candidate) return candidate
    }
    return undefined
  }
  const numberValue = (...keys: string[]): number | undefined => {
    for (const key of keys) {
      const candidate = value[key]
      if (
        typeof candidate === 'number'
        && Number.isFinite(candidate)
      ) return candidate
    }
    return undefined
  }
  const signal: ConversationCursorSignal = {
    sessionKey: text('key', 'session_key', 'sessionKey'),
    sessionEpoch: numberValue('epoch'),
    streamGeneration: text('stream_generation', 'streamGeneration'),
    streamSeq: numberValue('stream_seq', 'streamSeq'),
    currentStreamSeq: numberValue('current_stream_seq', 'currentStreamSeq'),
    replayComplete: typeof value.replay_complete === 'boolean'
      ? value.replay_complete
      : typeof value.replayComplete === 'boolean'
        ? value.replayComplete
        : undefined,
    replayGapReason: text('replay_gap_reason', 'replayGapReason'),
  }
  return signal
}

export type NormalizeRunStatus = (status: string) => string

export type TaskSettlementStatus =
  | 'failed'
  | 'cancelled'
  | 'timeout'
  | 'abandoned'
  | 'interrupted'

export type TaskTerminalStatus = 'succeeded' | TaskSettlementStatus

export const PENDING_STREAM_TASK_ID = '__opensquilla_pending_stream_task__'
export const STOPPED_STREAM_TASK_ID = '__opensquilla_stopped_stream_task__'
// Tombstone left after a terminal event closes the live turn. Unlike an empty
// task id (which deliberately keeps legacy/untagged events lenient), this makes
// late task-tagged heartbeats/state changes fail the identity guard instead of
// reopening an empty work card after the answer has already completed.
export const FINISHED_STREAM_TASK_ID = '__opensquilla_finished_stream_task__'

const cursorRuntime = createConversationRuntime()

export function payloadSessionKey(payload: StreamEventEnvelope | null | undefined): string {
  return payload?.key || payload?.session_key || payload?.sessionKey || ''
}

export function isCurrentSessionPayload(payload: StreamEventEnvelope | null | undefined, sessionKey: string): boolean {
  const signal = conversationCursorSignal(payload)
  const key = signal.sessionKey || ''
  return !key || !sessionKey || key === sessionKey
}

export function payloadTaskId(payload: StreamEventEnvelope | null | undefined): string {
  const record = (payload && typeof payload === 'object' ? payload : {}) as Record<string, unknown>
  const direct = record.task_id ?? record.taskId
  if (typeof direct === 'string') return direct
  for (const key of ['active_task', 'activeTask', 'last_task', 'lastTask']) {
    const nested = record[key]
    if (!nested || typeof nested !== 'object') continue
    const nestedRecord = nested as Record<string, unknown>
    const nestedId = nestedRecord.task_id ?? nestedRecord.taskId
    if (typeof nestedId === 'string') return nestedId
  }
  return ''
}

// Identity guard for the live stream: an event belongs to the current turn
// unless it is tagged with a *different* task than the one rendering now.
// Lenient on both sides — a missing activeTaskId (legacy/unknown) or a payload
// with no task_id (non-TaskRuntime events: approvals, task groups, router…)
// always passes, so only positively-mismatched TaskRuntime events are dropped.
export function isCurrentTaskPayload(
  payload: StreamEventEnvelope | null | undefined,
  activeTaskId: string,
): boolean {
  if (!activeTaskId) return true
  const taskId = payloadTaskId(payload)
  if (!taskId) return true
  return taskId === activeTaskId
}

export function isStaleEpoch(payload: StreamEventEnvelope | null | undefined, currentEpoch: number): boolean {
  return cursorRuntime.isStaleEpoch(
    cursorRuntime.createCursor('', { sessionEpoch: currentEpoch }),
    conversationCursorSignal(payload).sessionEpoch,
  )
}

export function acceptStreamSeq(
  payload: StreamEventEnvelope | null | undefined,
  sessionKey: string,
  lastStreamSeq: number,
): StreamSeqDecision {
  const decision = cursorRuntime.acceptEvent(
    cursorRuntime.createCursor(sessionKey, { streamSeq: lastStreamSeq }),
    conversationCursorSignal(payload),
    { observeGeneration: false },
  )
  return {
    accepted: decision.accepted,
    nextStreamSeq: decision.cursor.streamSeq,
  }
}

export function taskGroupId(payload: SessionEventPayload | null | undefined): string {
  const id = payload?.group_id
  return typeof id === 'string' && id ? id : ''
}

export function activeTaskGroupRunState(payload: SessionEventPayload = {}, activeTaskGroupCount: number) {
  return {
    run_status: 'running',
    active_task: { ...(payload || {}), status: 'running', task_group_count: activeTaskGroupCount },
  }
}

export function sessionChangeIsTerminal(
  payload: SessionEventPayload,
  normalizeRunStatus: NormalizeRunStatus,
): boolean {
  const reason = String(payload?.reason || '').toLowerCase()
  if (reason === 'turn_complete' || reason === 'task_terminal') return true
  const lifecycle = String(payload?.status || '').toLowerCase()
  if (['done', 'failed', 'killed', 'timeout'].includes(lifecycle)) return true
  const runStatus = normalizeRunStatus(String(payload?.run_status || payload?.runStatus || ''))
  return ['failed', 'timeout', 'cancelled', 'interrupted'].includes(runStatus)
}

export function taskTerminalStatus(
  event: ConversationSemanticEventKind,
): TaskTerminalStatus | '' {
  const statusByKind: Partial<Record<ConversationSemanticEventKind, TaskTerminalStatus>> = {
    'task-succeeded': 'succeeded',
    'task-failed': 'failed',
    'task-timed-out': 'timeout',
    'task-abandoned': 'abandoned',
    'task-cancelled': 'cancelled',
  }
  return statusByKind[event] ?? ''
}

export function taskTerminalAsSessionEvent(
  event: ConversationSemanticEventKind,
  payload: SessionEventPayload | null | undefined,
) {
  // The rich completion receipt carries final text + usage, but TaskRuntime
  // also emits a lifecycle success after its handler returns. Treat that
  // lifecycle event as a terminal fallback so a missing done frame cannot leave
  // the client spinning forever on an otherwise completed turn.
  if (event === 'task-succeeded') {
    return { kind: 'turn-completed' as const, payload: { ...(payload || {}), reason: 'completed' } }
  }
  if (event === 'task-cancelled') {
    return { kind: 'turn-completed' as const, payload: { ...(payload || {}), reason: 'aborted' } }
  }
  const status = taskTerminalStatus(event)
  if (!['failed', 'timeout', 'abandoned'].includes(status)) return null
  const outcome = payload?.turn_outcome && typeof payload.turn_outcome === 'object'
    ? payload.turn_outcome as Record<string, unknown>
    : {}
  const rawCode = payload?.code ?? payload?.error_class ?? outcome.error_class
  const payloadCode = typeof rawCode === 'string' ? rawCode.trim().toLowerCase() : ''
  const rawReason = payload?.terminal_reason
  const terminalReason = typeof rawReason === 'string' ? rawReason.trim().toLowerCase() : ''
  const code = /^[a-z][a-z0-9_.-]*$/.test(payloadCode)
    ? payloadCode
    : /^[a-z][a-z0-9_.-]*$/.test(terminalReason) ? terminalReason : status
  return {
    kind: 'turn-failed' as const,
    payload: { ...(payload || {}), message: taskTerminalMessage(status, payload), code },
  }
}

export function taskTerminalMessage(status: string, payload: SessionEventPayload | null | undefined): string {
  if (typeof payload?.terminal_message === 'string' && payload.terminal_message.trim()) return payload.terminal_message.trim()
  if (status === 'timeout') return 'The task timed out before it could finish.'
  if (status === 'abandoned') return 'The task stopped before it could finish.'
  if (status === 'cancelled') return 'The task was cancelled before it finished.'
  if (status === 'failed') return 'The task failed before it could finish.'
  return 'The task ended before it could finish.'
}

export function sessionErrorMessage(payload: SessionEventPayload | null | undefined): string {
  if (typeof payload?.terminal_message === 'string' && payload.terminal_message.trim()) return payload.terminal_message.trim()
  const message = typeof payload?.message === 'string' ? payload.message : ''
  const code = typeof payload?.code === 'string' ? payload.code.toLowerCase() : ''
  if (code.includes('timeout') || message.toLowerCase().includes('stream idle')) return 'The task timed out before it could finish.'
  if (message) return message
  return 'Agent error'
}
