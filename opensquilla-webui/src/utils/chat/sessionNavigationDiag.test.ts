import { afterEach, describe, expect, it } from 'vitest'
import {
  clearSessionNavigationDiag,
  beginSessionHandoffDiag,
  finishSessionHandoffDiag,
  readSessionNavigationDiag,
  recordRpcTransportDiag,
  recordSessionNavigationDiag,
  SESSION_NAVIGATION_DIAG_LIMIT,
  SESSION_NAVIGATION_DIAG_STORAGE_KEY,
  setSessionNavigationDiagStorageForTest,
  type SessionNavigationDiagStorage,
} from './sessionNavigationDiag'

class MemoryStorage implements SessionNavigationDiagStorage {
  private values = new Map<string, string>()

  getItem(key: string): string | null {
    return this.values.get(key) ?? null
  }

  setItem(key: string, value: string): void {
    this.values.set(key, value)
  }

  removeItem(key: string): void {
    this.values.delete(key)
  }
}

describe('sessionNavigationDiag', () => {
  afterEach(() => {
    setSessionNavigationDiagStorageForTest(null)
  })

  it('persists finite non-negative recovery metrics while filtering URLs, credentials and bodies', () => {
    const memory = new MemoryStorage()
    setSessionNavigationDiagStorageForTest(memory)
    recordRpcTransportDiag({
      phase: 'hello', generation: 5, recoveryMs: 0, loopLagMs: 12.5, maxLoopLagMs: 9000,
      url: 'ws://private.example/ws', token: 'PRIVATE_TOKEN', payload: { text: 'PRIVATE_BODY' },
      message: 'PRIVATE_MESSAGE',
    })
    expect(readSessionNavigationDiag()[0]).toMatchObject({
      source: 'rpc.transport', recoveryMs: 0, loopLagMs: 12.5, maxLoopLagMs: 9000,
    })
    const serialized = memory.getItem(SESSION_NAVIGATION_DIAG_STORAGE_KEY) ?? ''
    for (const secret of ['private.example', 'PRIVATE_TOKEN', 'PRIVATE_BODY', 'PRIVATE_MESSAGE']) {
      expect(serialized).not.toContain(secret)
    }
  })

  it.each([-1, Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY, '100', null, undefined, {}])(
    'does not persist an invalid recovery metric value %s', value => {
      setSessionNavigationDiagStorageForTest(new MemoryStorage())
      recordRpcTransportDiag({ phase: 'hello', generation: 5, recoveryMs: value, loopLagMs: value, maxLoopLagMs: value })
      const entry = readSessionNavigationDiag()[0]
      expect(entry).not.toHaveProperty('recoveryMs')
      expect(entry).not.toHaveProperty('loopLagMs')
      expect(entry).not.toHaveProperty('maxLoopLagMs')
    },
  )

  it('keeps recovery metrics in the existing bounded 200-entry diagnostic ring', () => {
    setSessionNavigationDiagStorageForTest(new MemoryStorage())
    for (let index = 0; index < SESSION_NAVIGATION_DIAG_LIMIT + 5; index++) {
      recordRpcTransportDiag({ phase: 'hello', generation: index, recoveryMs: index, loopLagMs: 0, maxLoopLagMs: 0 })
    }
    const entries = readSessionNavigationDiag()
    expect(entries).toHaveLength(200)
    expect(entries[0].recoveryMs).toBe(204)
    expect(entries[entries.length - 1].recoveryMs).toBe(5)
  })

  it('records newest entries first with opaque session correlation', () => {
    setSessionNavigationDiagStorageForTest(new MemoryStorage())

    recordSessionNavigationDiag('send.start', { requestSession: 'A', current: 'A' })
    recordSessionNavigationDiag('send.response.stale', {
      requestSession: 'A',
      responseSession: 'A',
      current: 'B',
      reason: 'current_session_changed',
    })

    const entries = readSessionNavigationDiag()
    expect(entries.map(entry => entry.source)).toEqual([
      'send.response.stale',
      'send.start',
    ])
    expect(entries[0]).toMatchObject({ reason: 'current_session_changed' })
    expect(entries[0]?.requestSession).toMatch(/^target-[0-9a-f]{8}$/)
    expect(entries[0]?.requestSession).toBe(entries[0]?.responseSession)
    expect(entries[0]?.requestSession).toBe(entries[1]?.requestSession)
    expect(entries[1]?.requestSession).toBe(entries[1]?.current)
    expect(entries[0]?.current).not.toBe(entries[0]?.requestSession)
    expect(JSON.stringify(entries)).not.toContain('"A"')
    expect(JSON.stringify(entries)).not.toContain('"B"')
  })

  it('clears stored diagnostics', () => {
    setSessionNavigationDiagStorageForTest(new MemoryStorage())

    recordSessionNavigationDiag('persistSession', { from: 'A', to: 'B' })
    clearSessionNavigationDiag()

    expect(readSessionNavigationDiag()).toEqual([])
  })

  it('redacts legacy diagnostics on read and migrates the stored copy', () => {
    const memory = new MemoryStorage()
    memory.setItem(SESSION_NAVIGATION_DIAG_STORAGE_KEY, JSON.stringify([{
      t: 1,
      iso: '1970-01-01T00:00:00.001Z',
      source: 'legacy.navigation',
      from: '/private/workspaces/customer-a',
      to: 'agent:main:webchat:customer-b',
      targetKeyHash: 'legacy-raw-target',
      reason: 'Failed while reading /private/workspaces/customer-a/secret.txt',
    }]))
    setSessionNavigationDiagStorageForTest(memory)

    const entries = readSessionNavigationDiag()

    expect(entries[0]?.from).toMatch(/^target-[0-9a-f]{8}$/)
    expect(entries[0]?.to).toMatch(/^target-[0-9a-f]{8}$/)
    expect(entries[0]?.targetKeyHash).toMatch(/^target-[0-9a-f]{8}$/)
    expect(entries[0]?.reason).toBe('reason_redacted')
    const migrated = memory.getItem(SESSION_NAVIGATION_DIAG_STORAGE_KEY) ?? ''
    expect(migrated).not.toContain('/private/workspaces')
    expect(migrated).not.toContain('customer-b')
    expect(migrated).not.toContain('legacy-raw-target')
    expect(migrated).not.toContain('secret.txt')
  })

  it('does not persist arbitrary request errors as diagnostic reasons', () => {
    setSessionNavigationDiagStorageForTest(new MemoryStorage())

    recordSessionNavigationDiag('send.error.stale', {
      requestSession: 'session-a',
      current: 'session-b',
      reason: 'Provider exposed /private/customer/prompt.txt',
    })

    expect(readSessionNavigationDiag()[0]?.reason).toBe('reason_redacted')
    expect(JSON.stringify(readSessionNavigationDiag())).not.toContain('/private/customer')
  })

  it('records transport and handoff diagnostics without raw session or peer text', () => {
    setSessionNavigationDiagStorageForTest(new MemoryStorage())

    beginSessionHandoffDiag(7, '/private/workspace/customer/session-A')
    recordRpcTransportDiag({
      phase: 'close',
      generation: 12,
      connId: 'conn-12',
      code: 1011,
      reason: '/private/workspace/customer/session-A failed',
      wasClean: false,
      sessionKey: 'session-A',
      url: 'ws://secret.example/ws',
    })
    finishSessionHandoffDiag(7, 'committed')

    const entries = readSessionNavigationDiag()
    const transport = entries.find(entry => entry.source === 'rpc.transport')
    expect(transport).toMatchObject({
      generation: 12,
      connId: 'conn-12',
      closeCode: 1011,
      reason: 'peer_close_reason_redacted',
      wasClean: false,
      handoffEpoch: 7,
    })
    expect(transport?.targetKeyHash).toMatch(/^target-[0-9a-f]{8}$/)
    const serialized = JSON.stringify(entries)
    expect(serialized).not.toContain('/private/workspace')
    expect(serialized).not.toContain('session-A')
    expect(serialized).not.toContain('secret.example')
  })

  it('preserves fixed internal recovery reasons for support diagnosis', () => {
    setSessionNavigationDiagStorageForTest(new MemoryStorage())

    recordRpcTransportDiag({
      phase: 'retire',
      generation: 4,
      reason: 'generation_consistency_recovery',
      reconnectAttempt: 2,
    })

    expect(readSessionNavigationDiag()[0]).toMatchObject({
      source: 'rpc.transport',
      phase: 'retire',
      generation: 4,
      reason: 'generation_consistency_recovery',
      reconnectAttempt: 2,
    })
  })
})
