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

  it('keeps separate allowances for 20 chats within one document', () => {
    const write = vi.fn()
    const logger = new SessionRecoveryDiagnostics(write, () => 0)
    for (let id = 0; id < 20; id++) {
      logger.log({ phase: 'failed', reason: 'metadata_hydration_failed', sessionKey: `chat-${id}` })
      logger.log({ phase: 'recovered', reason: 'recovery_completed', sessionKey: `chat-${id}` })
    }
    expect(records(write)).toHaveLength(40)
    expect(new Set(records(write).map(record => record.session_key_sha256)).size).toBe(20)
    expect(records(write).every(record => record.suppressed_count === 0)).toBe(true)
  })

  it('bounds one chat even when its diagnostic signatures keep changing', () => {
    const write = vi.fn()
    const logger = new SessionRecoveryDiagnostics(write, () => 0)
    for (let generation = 0; generation < 1000; generation++) logger.log({
      phase: 'failed', reason: 'metadata_hydration_failed', sessionKey: 'chat',
      connectionGeneration: generation,
    })
    expect(records(write)).toHaveLength(8)
    logger.log({ phase: 'failed', reason: 'metadata_hydration_failed', sessionKey: 'other' })
    const messages = records(write)
    expect(messages[messages.length - 1]).toMatchObject({ suppressed_count: 992 })
  })

  it('bounds scopes without eviction resets and reports suppression next minute', () => {
    const write = vi.fn()
    let now = 0
    const logger = new SessionRecoveryDiagnostics(write, () => now)
    for (let id = 0; id < 1000; id++) logger.log({
      phase: 'trigger', reason: 'consumer_timeout', sessionKey: `session-${id}`,
    })
    expect(write).toHaveBeenCalledTimes(64)
    // Existing scopes retain their unused allowance when the scope table is full.
    for (let generation = 1; generation < 8; generation++) {
      for (let id = 0; id < 64; id++) logger.log({
        phase: 'trigger', reason: 'consumer_timeout', sessionKey: `session-${id}`,
        connectionGeneration: generation,
      })
    }
    expect(write).toHaveBeenCalledTimes(512)
    logger.log({ phase: 'failed', reason: 'consumer_timeout', sessionKey: 'session-0' })
    logger.log({ phase: 'failed', reason: 'consumer_timeout', sessionKey: 'session-1000' })
    expect(write).toHaveBeenCalledTimes(512)
    now = 60_000
    logger.log({ phase: 'trigger', reason: 'consumer_timeout', sessionKey: 'next' })
    const messages = records(write)
    expect(messages[messages.length - 1]).toMatchObject({ suppressed_count: 2 })
    expect(records(write).reduce((sum, record) => sum + record.suppressed_count, 0)).toBe(938)
  })

  it('shares the global scope for missing or unhashable identities', () => {
    const write = vi.fn()
    const logger = new SessionRecoveryDiagnostics(write, () => 0)
    for (let generation = 0; generation < 20; generation++) logger.log({
      phase: 'trigger', reason: 'server_dirty', connectionGeneration: generation,
      sessionKey: generation % 2 === 0 ? undefined : 'x'.repeat(4097),
    })
    expect(records(write)).toHaveLength(8)
    expect(records(write).every(record => record.session_key_sha256 === undefined)).toBe(true)
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
