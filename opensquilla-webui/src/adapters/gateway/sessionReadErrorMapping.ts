import {
  SessionReadContractError,
  SessionReadFailure,
  SessionReadLeaseClosedError,
  SessionReadSessionMissingError,
} from '@/modules/sessionReadLifecycle'
import { readTransportFailure } from './privateTransports'

export function mapSessionReadError(error: unknown): Error {
  if (
    error instanceof SessionReadFailure
    || error instanceof SessionReadContractError
    || error instanceof SessionReadLeaseClosedError
    || error instanceof SessionReadSessionMissingError
  ) return error
  const failure = readTransportFailure(error)
  const code = failure.code?.toUpperCase()
  if (code === 'NOT_FOUND' || code === 'SESSION_NOT_FOUND') {
    return new SessionReadSessionMissingError(failure.message, error)
  }
  const kind = code === 'RPC_ABORTED' || (error instanceof Error && error.name === 'AbortError')
    ? 'aborted'
    : code === 'RPC_TIMEOUT'
      ? 'timeout'
      : code === 'STORAGE_BUSY'
        ? 'busy'
        : 'unavailable'
  return new SessionReadFailure(
    kind,
    failure.message,
    failure.retryable === true || kind === 'timeout' || kind === 'busy' || !code,
    failure.retryAfterMs ?? 0,
    error,
  )
}
