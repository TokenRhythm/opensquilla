import type {
  ConversationEventData,
  ConversationEventIdentity,
} from '@/modules/conversationEventContent'

import type { ConversationSemanticEventKind } from '@/modules/conversationEvents'
import {
  createConversationRuntime,
  type ConversationCursorSignal,
} from '@/modules/conversationRuntime'

export interface StreamSeqDecision {
  accepted: boolean
  nextStreamSeq: number
}

/** The Adapter has already resolved every accepted cursor spelling. */
export function conversationCursorSignal(source: ConversationEventIdentity | null | undefined): ConversationCursorSignal {
  return {
    sessionKey: source?.key,
    sessionEpoch: source?.epoch,
    streamGeneration: source?.stream_generation,
    streamSeq: source?.stream_seq,
    currentStreamSeq: source?.current_stream_seq,
    replayComplete: source?.replay_complete,
    replayGapReason: source?.replay_gap_reason,
  }
}

export type NormalizeRunStatus = (status: string) => string

export const PENDING_STREAM_TASK_ID = '__opensquilla_pending_stream_task__'
export const STOPPED_STREAM_TASK_ID = '__opensquilla_stopped_stream_task__'
// Tombstone left after a terminal event closes the live turn. Unlike an empty
// task id (which deliberately keeps legacy/untagged events lenient), this makes
// late task-tagged heartbeats/state changes fail the identity guard instead of
// reopening an empty work card after the answer has already completed.
export const FINISHED_STREAM_TASK_ID = '__opensquilla_finished_stream_task__'

const cursorRuntime = createConversationRuntime()

export function isCurrentSessionPayload(payload: ConversationEventIdentity | null | undefined, sessionKey: string): boolean {
  const signal = conversationCursorSignal(payload)
  const key = signal.sessionKey || ''
  return !key || !sessionKey || key === sessionKey
}

export function payloadTaskId(payload: ConversationEventIdentity | null | undefined): string {
  return payload?.task_id ?? ''
}

// Identity guard for the live stream: an event belongs to the current turn
// unless it is tagged with a *different* task than the one rendering now.
// Lenient on both sides — a missing activeTaskId (legacy/unknown) or a payload
// with no task_id (non-TaskRuntime events: approvals, task groups, router…)
// always passes, so only positively-mismatched TaskRuntime events are dropped.
export function isCurrentTaskPayload(
  payload: ConversationEventIdentity | null | undefined,
  activeTaskId: string,
): boolean {
  if (!activeTaskId) return true
  const taskId = payloadTaskId(payload)
  if (!taskId) return true
  return taskId === activeTaskId
}

export function isStaleEpoch(payload: ConversationEventIdentity | null | undefined, currentEpoch: number): boolean {
  return cursorRuntime.isStaleEpoch(
    cursorRuntime.createCursor('', { sessionEpoch: currentEpoch }),
    conversationCursorSignal(payload).sessionEpoch,
  )
}

export function acceptStreamSeq(
  payload: ConversationEventIdentity | null | undefined,
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

export function taskGroupId(payload: ConversationEventData | null | undefined): string {
  const id = payload?.group_id
  return typeof id === 'string' && id ? id : ''
}

export function activeTaskGroupRunState(payload: ConversationEventData = {}, activeTaskGroupCount: number) {
  return {
    run_status: 'running',
    active_task: { ...(payload || {}), status: 'running', task_group_count: activeTaskGroupCount },
  }
}

export function sessionChangeIsTerminal(
  payload: ConversationEventData,
  normalizeRunStatus: NormalizeRunStatus,
): boolean {
  const reason = String(payload?.reason || '').toLowerCase()
  if (reason === 'turn_complete' || reason === 'task_terminal') return true
  const lifecycle = String(payload?.status || '').toLowerCase()
  if (['done', 'failed', 'killed', 'timeout'].includes(lifecycle)) return true
  const runStatus = normalizeRunStatus(String(payload?.run_status || ''))
  return ['failed', 'timeout', 'cancelled', 'interrupted'].includes(runStatus)
}

export function taskTerminalStatus(event: ConversationSemanticEventKind): string {
  const statusByKind: Partial<Record<ConversationSemanticEventKind, string>> = {
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
  payload: ConversationEventData | null | undefined,
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
  const rawCode = payload?.code ?? payload?.error_class ?? payload?.terminalOutcome?.errorClass
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

export function taskTerminalMessage(status: string, payload: ConversationEventData | null | undefined): string {
  if (typeof payload?.terminal_message === 'string' && payload.terminal_message.trim()) return payload.terminal_message.trim()
  if (status === 'timeout') return 'The task timed out before it could finish.'
  if (status === 'abandoned') return 'The task stopped before it could finish.'
  if (status === 'cancelled') return 'The task was cancelled before it finished.'
  if (status === 'failed') return 'The task failed before it could finish.'
  return 'The task ended before it could finish.'
}

export function sessionErrorMessage(payload: ConversationEventData | null | undefined): string {
  if (typeof payload?.terminal_message === 'string' && payload.terminal_message.trim()) return payload.terminal_message.trim()
  const message = typeof payload?.message === 'string' ? payload.message : ''
  const code = typeof payload?.code === 'string' ? payload.code.toLowerCase() : ''
  if (code.includes('timeout') || message.toLowerCase().includes('stream idle')) return 'The task timed out before it could finish.'
  if (message) return message
  return 'Agent error'
}
