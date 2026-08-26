import {
  RpcAbortError,
  RpcTimeoutError,
  type RpcCallOptions,
  type RpcClientError,
  type RpcConnectionWaitOptions,
} from '@/lib/rpc'

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
  /**
   * Marks that this attempt's subscribe frame was synchronously sent. History
   * may then enter the serialized Gateway queue behind it.
   */
  markLiveSubscribeSent?: (socketGeneration: number) => void
  /** Marks that the canonical history frame was synchronously sent. */
  markHistoryRequestSent?: (socketGeneration: number) => void
  /**
   * Optional metadata may start once the critical frames are on the wire. It
   * must not wait for a potentially slow history response.
   */
  waitForCriticalRequestsQueued?: () => Promise<void>
}

export interface SessionPhaseResult<T = void> {
  ok: boolean
  value?: T
  error?: unknown
  cancelled?: boolean
}

export function rpcErrorCode(error: unknown): string {
  if (!error || typeof error !== 'object') return ''
  const code = (error as RpcClientError).code
  return typeof code === 'string' ? code : ''
}

export function isRpcAbort(error: unknown): boolean {
  return error instanceof RpcAbortError || rpcErrorCode(error) === 'RPC_ABORTED'
}

export function isRpcTimeout(error: unknown): boolean {
  return error instanceof RpcTimeoutError || rpcErrorCode(error) === 'RPC_TIMEOUT'
}

export function isStorageBusy(error: unknown): boolean {
  return rpcErrorCode(error) === 'STORAGE_BUSY'
}

export function retryAfterMs(error: unknown): number {
  if (!error || typeof error !== 'object') return 0
  const value = (error as RpcClientError).retry_after_ms
  return typeof value === 'number' && Number.isFinite(value)
    ? Math.max(0, value)
    : 0
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
    throw new RpcTimeoutError(method, 0)
  }
  return Math.max(1, Math.min(maximumMs, remaining))
}

export function phaseCallOptions(
  context: SessionBootstrapPhaseContext,
  method: string,
  maximumMs = SESSION_PHASE_ATTEMPT_BUDGET_MS,
): RpcCallOptions {
  return {
    timeoutMs: phaseTimeoutMs(context, method, maximumMs),
    signal: context.signal,
    // Keep the legacy timeout recovery until the negotiated Gateway policy
    // proves a method runs outside the serialized dispatcher. Callers may
    // safely downgrade a policy-advertised detached read to request-local.
    timeoutAction: 'reconnect',
    // Route/session ownership ends locally. The transport has an independent
    // lifecycle and must never be recycled merely because a consumer moved on.
    abortAction: 'reject',
  }
}

export function phaseConnectionWaitOptions(): RpcConnectionWaitOptions {
  return {
    // The transport-owned challenge/Hello watchdogs now fence and recycle the
    // exact generation. A session budget only owns its waiter, so it must not
    // pre-empt a valid authentication attempt that may legitimately take up to
    // the 45 second transport deadline.
    timeoutAction: 'reject',
    abortAction: 'reject',
  }
}

export function shouldRetrySessionPhase(error: unknown): boolean {
  if (isRpcAbort(error)) return false
  const code = rpcErrorCode(error)
  if (isRpcTimeout(error) || isStorageBusy(error)) return true
  if ((error as RpcClientError | null | undefined)?.retryable === true) return true
  if (code) return false
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
