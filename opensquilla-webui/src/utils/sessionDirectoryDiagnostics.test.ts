import { describe, expect, it, vi } from 'vitest'
import { SessionDirectoryError } from '@/modules/sessionDirectory'
import { sessionDirectoryErrorLogValue, storageBusyDiagnostic } from './sessionDirectoryDiagnostics'

const details = {
  operation: 'session_titles',
  stage: 'deadline',
  resource: 'session_storage_recovery_read_pool',
  waited_ms: 2001,
}
const expected = { code: 'STORAGE_BUSY', ...details, retry_after_ms: 100 }

describe('session directory storage-busy diagnostics', () => {
  it('retains the current RPC code/details shape through a domain error cause', () => {
    const wireError = Object.assign(new Error('Session storage is temporarily busy'), {
      code: 'STORAGE_BUSY', details, retry_after_ms: 100,
    })
    expect(storageBusyDiagnostic(wireError)).toEqual(expected)
    expect(storageBusyDiagnostic(new SessionDirectoryError('unavailable', 'Wrapped', {
      cause: wireError,
    }))).toEqual(expected)
  })

  it.each([
    { data: { code: 'STORAGE_BUSY', ...details, retry_after_ms: 100 } },
    { code: 'STORAGE_BUSY', data: { details, retry_after_ms: 100 } },
    { data: { code: 'STORAGE_BUSY', details, retry_after_ms: 100 } },
  ])('supports RPC data envelopes without dumping them', error => {
    expect(storageBusyDiagnostic(error)).toEqual(expected)
  })

  it('logs only whitelisted identifiers and timings, never request content or identities', () => {
    const secret = 'agent:main:webchat:private-session'
    const error = Object.assign(new Error(`User message: ${secret}`), {
      code: 'STORAGE_BUSY', retry_after_ms: 100,
      details: { ...details, session_id: secret, params: { message: secret }, holder_operation: secret },
      data: { token: secret }, cause: { message: secret },
    })
    expect(sessionDirectoryErrorLogValue(error)).toEqual(expected)
    expect(JSON.stringify(sessionDirectoryErrorLogValue(error))).not.toContain(secret)
    expect(storageBusyDiagnostic({ code: 'STORAGE_BUSY', details: {
      operation: secret, resource: '/private/user/session.db', stage: 'x\nsecret',
      waited_ms: Infinity, retry_after_ms: -1,
    } })).toEqual({ code: 'STORAGE_BUSY' })
  })

  it('rejects objects, oversized values, coercions and invalid timing values', () => {
    const toString = vi.fn(() => 'session_titles')
    expect(storageBusyDiagnostic({ code: 'STORAGE_BUSY', details: {
      operation: { toString }, resource: 'r'.repeat(97), stage: null,
      waited_ms: '2000', retry_after_ms: NaN,
    } })).toEqual({ code: 'STORAGE_BUSY' })
    expect(toString).not.toHaveBeenCalled()
    expect(storageBusyDiagnostic({ code: 'STORAGE_BUSY', waited_ms: 0.5, retry_after_ms: 2 ** 54 }))
      .toEqual({ code: 'STORAGE_BUSY' })
    expect(storageBusyDiagnostic({ code: 'STORAGE_BUSY', operation: 'session_titles:recovery', waited_ms: 0 }))
      .toEqual({ code: 'STORAGE_BUSY', operation: 'session_titles:recovery', waited_ms: 0 })
  })

  it('does not invoke getters, recurse through cycles or throw on hostile proxies', () => {
    const getter = vi.fn(() => { throw new Error('hostile getter') })
    const error = Object.defineProperties(new Error('ignored'), {
      code: { get: getter }, message: { get: getter }, data: { get: getter },
    })
    expect(storageBusyDiagnostic(error)).toBeNull()
    expect(sessionDirectoryErrorLogValue(error)).toBe('Session directory request failed')
    expect(getter).not.toHaveBeenCalled()
    const cycle: { cause?: unknown; data?: unknown } = {}
    cycle.cause = cycle
    cycle.data = { code: 'STORAGE_BUSY', details }
    expect(storageBusyDiagnostic(cycle)).toEqual({ code: 'STORAGE_BUSY', ...details })
    const proxy = new Proxy({}, { getOwnPropertyDescriptor: getter, getPrototypeOf: getter })
    expect(storageBusyDiagnostic(proxy)).toBeNull()
    expect(sessionDirectoryErrorLogValue(proxy)).toBe('Session directory request failed')
    let deep: unknown = { code: 'STORAGE_BUSY' }
    for (let i = 0; i < 100; i++) deep = { cause: deep }
    expect(storageBusyDiagnostic(deep)).toBeNull()
  })

  it('preserves the usual non-busy log values', () => {
    expect(sessionDirectoryErrorLogValue(new Error('existing failure'))).toBe('existing failure')
    expect(sessionDirectoryErrorLogValue(new Error())).toBe('')
    expect(sessionDirectoryErrorLogValue('existing string')).toBe('existing string')
    const object = { code: 'UNAVAILABLE', message: 'existing object' }
    expect(sessionDirectoryErrorLogValue(object)).toBe(object)
  })
})
