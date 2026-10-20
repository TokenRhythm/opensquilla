import { describe, expect, it, vi } from 'vitest'
import { SESSION_RECOVERY_LOG_PREFIX, SessionRecoveryDiagnostics } from './sessionRecoveryDiagnostics'

function records(write: ReturnType<typeof vi.fn>) {
  return write.mock.calls.map(([message]) => JSON.parse(String(message).slice(SESSION_RECOVERY_LOG_PREFIX.length)))
}

describe('session recovery diagnostics', () => {
  it.each(['RPC_TIMEOUT', 'RPC_TRANSPORT_ERROR', 'SNAPSHOT_EXPIRED', 'READ_STALE'])(
    'retains the recoverable %s code',
    code => {
      const write = vi.fn()
      const logger = new SessionRecoveryDiagnostics(write, () => 0)
      logger.log({ phase: 'failed', reason: 'live_subscription_failed', error: { code } })
      expect(records(write)[0]).toMatchObject({ error_code: code })
    },
  )

  it('correlates a session by SHA-256 and retains typed causes without recording content', () => {
    const write = vi.fn()
    const logger = new SessionRecoveryDiagnostics(write, () => 0)
    logger.log({
      phase: 'failed', reason: 'live_subscription_failed', sessionKey: 'alpha',
      connectionGeneration: 7, deliveryEpoch: 'epoch-1', streamSeq: 91,
      error: { kind: 'busy', retryable: true, message: 'private answer', cause: {
        code: 'SNAPSHOT_STALE', message: 'api_key=secret', payload: 'private payload',
      } },
    })
    expect(records(write)).toEqual([{
      phase: 'failed', reason: 'live_subscription_failed', session_key_sha256: '8ed3f6ad685b959e',
      connection_generation: 7, delivery_epoch: 'epoch-1', stream_seq: 91,
      error_kind: 'busy', retryable: true, error_code: 'SNAPSHOT_STALE', suppressed_count: 0,
    }])
    expect(write.mock.calls[0]![0]).not.toMatch(/private|secret|alpha/)
  })

  it('deduplicates a hot loop and reports its suppressed count on the next transition', () => {
    const write = vi.fn()
    let now = 0
    const logger = new SessionRecoveryDiagnostics(write, () => now)
    for (let id = 1; id <= 1000; id++) logger.log({
      phase: 'trigger', reason: 'server_dirty', sessionKey: 'alpha', deliveryId: id,
    })
    expect(write).toHaveBeenCalledOnce()
    logger.log({ phase: 'recovered', reason: 'recovery_completed', sessionKey: 'alpha' })
    expect(records(write)[1]).toMatchObject({ suppressed_count: 999 })
    now = 60_000
    logger.log({ phase: 'trigger', reason: 'server_dirty', sessionKey: 'alpha' })
    expect(write).toHaveBeenCalledTimes(3)
  })

  it('bounds distinct scopes as well as repeats, and emits a suppression summary next minute', () => {
    const write = vi.fn()
    let now = 0
    const logger = new SessionRecoveryDiagnostics(write, () => now)
    for (let id = 0; id < 1000; id++) logger.log({
      phase: 'trigger', reason: 'consumer_timeout', sessionKey: `session-${id}`,
    })
    expect(write).toHaveBeenCalledTimes(8)
    now = 60_000
    logger.log({ phase: 'trigger', reason: 'consumer_timeout', sessionKey: 'next' })
    expect(records(write)[write.mock.calls.length - 1]).toMatchObject({ suppressed_count: 992 })
  })

  it('does not serialize arbitrary errors or allow a logging failure to affect recovery', () => {
    const write = vi.fn()
    const logger = new SessionRecoveryDiagnostics(write, () => 0)
    logger.log({
      phase: 'failed', reason: 'consumer_rejected',
      deliveryEpoch: 'secret\ninjection', deliveryId: -1,
      error: { code: 'secret-value', message: 'private answer', cause: { kind: 'private kind' } },
    })
    expect(records(write)).toEqual([{
      phase: 'failed', reason: 'consumer_rejected', suppressed_count: 0,
    }])
    expect(() => new SessionRecoveryDiagnostics(() => { throw new Error('sink failed') }).log({
      phase: 'trigger', reason: 'server_dirty',
    })).not.toThrow()
    expect(() => logger.log({
      phase: 'failed', reason: 'consumer_rejected',
      error: { get code() { throw new Error('hostile error property') } },
    })).not.toThrow()
  })
})
