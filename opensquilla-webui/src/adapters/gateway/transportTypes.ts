export type TransportTerminationAction = 'reject' | 'reconnect'

/** Request lifecycle policy shared only between the private transport and its Adapters. */
export interface TransportCallOptions {
  timeoutMs?: number
  signal?: AbortSignal
  timeoutAction?: TransportTerminationAction
  abortAction?: TransportTerminationAction
  expectedGeneration?: number
  onSent?: (socketGeneration: number) => void
}

export interface TransportConnectionWaitOptions {
  timeoutAction?: TransportTerminationAction
  abortAction?: TransportTerminationAction
}

export type TransportEventHandler = {
  bivarianceHack(...args: unknown[]): void
}['bivarianceHack']

export interface TransportFailure {
  readonly message: string
  readonly code?: string
  readonly details?: unknown
  readonly retryable?: boolean
  readonly retryAfterMs?: number
  readonly accepted?: boolean | null
}

/** Read a rejected wire request without allowing its error shape past an Adapter. */
export function readTransportFailure(error: unknown): TransportFailure {
  const source = error && typeof error === 'object'
    ? error as Record<string, unknown>
    : null
  const data = source?.data && typeof source.data === 'object' && !Array.isArray(source.data)
    ? source.data as Record<string, unknown>
    : null
  const code = source?.code ?? data?.code
  const retryAfter = source?.retry_after_ms ?? source?.retryAfterMs
  const accepted = source?.accepted
  return {
    message: error instanceof Error ? error.message : String(error ?? ''),
    ...(typeof code === 'string' && code ? { code } : {}),
    ...(source && Object.prototype.hasOwnProperty.call(source, 'details')
      ? { details: source.details }
      : {}),
    ...(typeof source?.retryable === 'boolean' ? { retryable: source.retryable } : {}),
    ...(typeof retryAfter === 'number' && Number.isFinite(retryAfter)
      ? { retryAfterMs: Math.max(0, retryAfter) }
      : {}),
    ...(accepted === true || accepted === false || accepted === null ? { accepted } : {}),
  }
}
