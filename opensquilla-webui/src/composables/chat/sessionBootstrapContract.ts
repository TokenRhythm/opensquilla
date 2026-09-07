import { SessionReadFailure } from '@/modules/sessionReadLifecycle'

export const SESSION_BOOTSTRAP_BUDGET_MS = 15_000
export const SESSION_PHASE_ATTEMPT_BUDGET_MS = 7_000
export const SESSION_SNAPSHOT_BUDGET_MS = 3_000

export type SessionHistoryPhase = 'idle' | 'loading' | 'ready' | 'error'
export type SessionLivePhase = 'idle' | 'connecting' | 'ready' | 'degraded'

export interface SessionBootstrapPhaseContext {
  generation: number
  key: string
  attempt: 0 | 1
  deadlineAt: number
  attemptDeadlineAt: number
  signal: AbortSignal
  skipSnapshot: boolean
}

export interface SessionPhaseResult<T = void> {
  ok: boolean
  value?: T
  error?: unknown
  cancelled?: boolean
}

export function isRpcAbort(error: unknown): boolean {
  return error instanceof SessionReadFailure && error.kind === 'aborted'
}

export function isRpcTimeout(error: unknown): boolean {
  return error instanceof SessionReadFailure && error.kind === 'timeout'
}

export function isStorageBusy(error: unknown): boolean {
  return error instanceof SessionReadFailure && error.kind === 'busy'
}

export function retryAfterMs(error: unknown): number {
  return error instanceof SessionReadFailure ? error.retryAfterMs : 0
}

export function phaseRemainingMs(
  context: SessionBootstrapPhaseContext,
  now = Date.now(),
): number {
  return Math.max(0, Math.min(context.deadlineAt, context.attemptDeadlineAt) - now)
}

export function phaseTimeoutMs(
  context: SessionBootstrapPhaseContext,
  method: string,
  maximumMs = SESSION_PHASE_ATTEMPT_BUDGET_MS,
): number {
  const remaining = phaseRemainingMs(context)
  if (remaining <= 0) {
    throw new SessionReadFailure(
      'timeout',
      `${method} exhausted the session bootstrap budget`,
      true,
    )
  }
  return Math.max(1, Math.min(maximumMs, remaining))
}

export function shouldRetrySessionPhase(error: unknown): boolean {
  if (isRpcAbort(error)) return false
  if (isRpcTimeout(error) || isStorageBusy(error)) return true
  if (error instanceof SessionReadFailure) return error.retryable
  // A socket recycle rejects sibling requests with a generic connection error.
  // One bounded retry is safe and lets both orthogonal phases join the new
  // generation without teaching every caller about transport wording.
  const message = error instanceof Error ? error.message.toLowerCase() : ''
  return (
    message.includes('connection')
    || message.includes('socket')
    || message.includes('not connected')
    || message.includes('network')
  )
}

export function autoSendDraftIsUnchanged(
  expectedText: string,
  currentText: string,
  expectedAttachments: readonly unknown[],
  currentAttachments: readonly unknown[],
  expectedRevision: number,
  currentRevision: number,
): boolean {
  return (
    currentRevision === expectedRevision
    && currentText === expectedText
    && currentAttachments.length === expectedAttachments.length
    && currentAttachments.every(
      (attachment, index) => attachment === expectedAttachments[index],
    )
  )
}
