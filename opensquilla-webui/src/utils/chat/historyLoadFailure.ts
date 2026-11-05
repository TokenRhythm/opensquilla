import { SessionReadFailure, SessionReadHistoryCursorError } from '@/modules/sessionReadLifecycle'

export interface HistoryLoadFailure {
  readonly kind: 'too-large' | 'timeout' | 'busy' | 'unavailable' | 'cursor' | 'failed'
  readonly action: 'retry' | 'reload-latest' | 'none'
}

/** Display only stable categories; transport messages may contain private data. */
export function classifyHistoryLoadFailure(error: unknown): HistoryLoadFailure {
  if (error instanceof SessionReadHistoryCursorError) return { kind: 'cursor', action: 'reload-latest' }
  if (error instanceof SessionReadFailure) {
    if (error.kind === 'too-large') return { kind: 'too-large', action: 'none' }
    if (!error.retryable) return { kind: 'failed', action: 'none' }
    if (error.kind === 'timeout' || error.kind === 'busy') return { kind: error.kind, action: 'retry' }
  }
  return { kind: 'unavailable', action: 'retry' }
}
