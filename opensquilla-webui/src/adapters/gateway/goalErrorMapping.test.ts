import { describe, expect, it } from 'vitest'
import { mapGoalError } from './goalErrorMapping'

describe('Goal error recovery contract', () => {
  it.each(['EXECUTION_LEASE_REQUIRED', 'STALE_GOAL', 'UNAVAILABLE', 'STORAGE_BUSY'])('preserves explicit false for %s', code => {
    expect(mapGoalError({ code, retryable: false, details: { recoveryAction: 'retry' } }, { reattach: true }))
      .toMatchObject({ retryable: false, recoveryAction: 'retry' })
  })

  it.each(['await-subscription', 'require-takeover', 'owner-attached', 'refresh-goal', 'stop', 'retry'])('projects typed %s recovery and retry hint', recoveryAction => {
    expect(mapGoalError({ data: { code: 'EXECUTION_LEASE_REQUIRED', retryable: true, retry_after_ms: 5_000,
      details: { recoveryAction } } }, { reattach: true }))
      .toMatchObject({ code: 'conflict', reason: 'connection-required', recoveryAction, retryable: true, retryAfterMs: 5_000 })
  })

  it.each(['UNAVAILABLE', 'STORAGE_BUSY', 'RPC_TIMEOUT', 'RPC_TRANSPORT_ERROR'])('keeps known legacy transient %s recoverable without an explicit flag', code => {
    expect(mapGoalError({ code }).retryable).toBe(true)
  })

  it('does not convert a programming error or unknown rejection into retry permission', () => {
    expect(mapGoalError(new TypeError('cannot read property')).retryable).toBe(false)
    expect(mapGoalError({ code: 'UNKNOWN_FAILURE' }).retryable).toBe(false)
  })

  it('fails closed for legacy ambiguous lease errors without interpreting wording', () => {
    expect(mapGoalError({ code: 'EXECUTION_LEASE_REQUIRED', message: 'connection timeout retry please' }, { reattach: true }))
      .toMatchObject({ retryable: false, recoveryAction: undefined })
    expect(mapGoalError({ code: 'EXECUTION_LEASE_REQUIRED', retryable: false,
      details: { recoveryAction: 'future-action' } }, { reattach: true }).recoveryAction).toBeUndefined()
  })
})
