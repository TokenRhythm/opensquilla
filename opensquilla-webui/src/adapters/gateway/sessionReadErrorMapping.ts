import {
  SessionReadContractError,
  SessionReadFailure,
  SessionReadLeaseClosedError,
  SessionReadSessionMissingError,
} from '@/modules/sessionReadLifecycle'
import { readTransportFailure } from './transportTypes'

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
  const kind = code === 'SNAPSHOT_TOO_LARGE'
    ? 'too-large'
    : code === 'RPC_ABORTED' || (error instanceof Error && error.name === 'AbortError')
    ? 'aborted'
    : code === 'RPC_TIMEOUT'
      ? 'timeout'
      : code === 'STORAGE_BUSY' || code === 'SNAPSHOT_BUSY'
        ? 'busy'
        : 'unavailable'
  return new SessionReadFailure(
    kind,
    failure.message,
    code !== 'SNAPSHOT_TOO_LARGE' && (failure.retryable === true || kind === 'timeout'
      || kind === 'busy' || code === 'SNAPSHOT_EXPIRED' || code === 'SNAPSHOT_STALE' || !code),
    failure.retryAfterMs ?? 0,
    error,
  )
}
