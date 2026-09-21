import { afterEach, describe, expect, it } from 'vitest'
import {
  clearSessionNavigationDiag,
  beginSessionHandoffDiag,
  finishSessionHandoffDiag,
  readSessionNavigationDiag,
  recordRpcResumeDiag,
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

  it('preserves the wake incident timeline and first successful RPC without payloads', () => {
    const memory = new MemoryStorage()
    setSessionNavigationDiagStorageForTest(memory)
    const timeline = {
      at: 1_020_000, topology: 'proxy/vpn', visibility: 'hidden', health: 'suspect',
      transportPhase: 'checking', wakeIncidentSource: 'desktop-resume', wakeIncidentProbeTimeoutMs: 2_000,
      suspectAt: 1_015_000, lastRxAt: 999_000, wakeIncidentId: 3,
      wakeIncidentStartedAt: 1_000_000, wakeIncidentDeadlineAt: 1_020_000,
      wakeIncidentStatus: 'reconnecting', wakeSignalCount: 7,
    }
    recordRpcTransportDiag({
      ...timeline, phase: 'wake_incident_timeout', generation: 8,
      reason: 'wake_incident_timeout',
      url: 'ws://PRIVATE_HOST/ws', payload: { secret: 'PRIVATE_PAYLOAD' },
      token: 'PRIVATE_TOKEN', nonce: 'PRIVATE_NONCE',
    })
    recordRpcTransportDiag({
      phase: 'first_successful_rpc', generation: 10, at: 1_020_700,
      topology: 'loopback', visibility: 'visible', health: 'healthy', roundTripMs: 25.5,
    })
    const entries = readSessionNavigationDiag()
    expect(entries[1]).toMatchObject({
      ...timeline, phase: 'wake_incident_timeout', reason: 'wake_incident_timeout',
    })
    expect(entries[0]).toMatchObject({
      phase: 'first_successful_rpc', generation: 10, at: 1_020_700,
      topology: 'loopback', visibility: 'visible', health: 'healthy', roundTripMs: 25.5,
    })
    expect(memory.getItem(SESSION_NAVIGATION_DIAG_STORAGE_KEY)).not.toContain('PRIVATE_')
  })

  it.each([-1, Number.NaN, Number.POSITIVE_INFINITY, 'PRIVATE_VALUE', null, {}])(
    'rejects invalid incident timing and counter values %s', value => {
      setSessionNavigationDiagStorageForTest(new MemoryStorage())
      const fields = [
        'at', 'suspectAt', 'lastRxAt', 'wakeIncidentId', 'wakeIncidentStartedAt',
        'wakeIncidentDeadlineAt', 'wakeSignalCount', 'roundTripMs',
      ]
      recordRpcTransportDiag({
        phase: 'wake_incident_start', generation: 1,
        ...Object.fromEntries(fields.map(field => [field, value])),
      })
      for (const field of fields) expect(readSessionNavigationDiag()[0]).not.toHaveProperty(field)
    },
  )

  it('allows only fixed incident enums, integer counters and transport phase names', () => {
    const memory = new MemoryStorage()
    setSessionNavigationDiagStorageForTest(memory)
    recordRpcTransportDiag({
      phase: 'wake_incident_start', generation: 1,
      topology: 'ws://PRIVATE_HOST', visibility: 'PRIVATE_VISIBILITY', health: 'PRIVATE_HEALTH',
      wakeIncidentStatus: 'PRIVATE_STATUS', wakeIncidentId: 1.5, wakeSignalCount: 0.5,
    })
    expect(recordRpcTransportDiag({ phase: 'PRIVATE_PHASE', generation: 2 })).toBeNull()
    const entry = readSessionNavigationDiag()[0]
    for (const field of ['topology', 'visibility', 'health', 'wakeIncidentStatus', 'wakeIncidentId', 'wakeSignalCount']) {
      expect(entry).not.toHaveProperty(field)
    }
    expect(memory.getItem(SESSION_NAVIGATION_DIAG_STORAGE_KEY)).not.toContain('PRIVATE_')
  })

  it.each([
    'wake_incident_timeout', 'socket_not_open', 'probe_socket_unavailable', 'probe_failed',
    'probe_send_failure', 'control_unconfirmed', 'scheduler_lag', 'wake_grace',
    'native_resume_socket_unavailable',
    'round_trip', 'hello', 'direct_send_timeout', 'recovery_credit_timeout',
    'writer_send_failed', 'writer_serialize_failed', 'writer_capacity', 'transport_resource_limit',
  ])('preserves the fixed transport reason %s', reason => {
    setSessionNavigationDiagStorageForTest(new MemoryStorage())
    recordRpcTransportDiag({ phase: 'retire', generation: 1, reason })
    expect(readSessionNavigationDiag()[0]?.reason).toBe(reason)
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

  it('records the bounded Desktop resume source without accepting arbitrary payloads', () => {
    setSessionNavigationDiagStorageForTest(new MemoryStorage())

    recordRpcResumeDiag({ generation: 9, resumeSource: 'desktop-resume' })

    expect(readSessionNavigationDiag()[0]).toMatchObject({
      source: 'rpc.transport',
      phase: 'desktop_resume',
      generation: 9,
      reason: 'desktop_resume',
      resumeSource: 'desktop-resume',
    })
    const result = recordRpcResumeDiag({
      generation: Number.NaN,
      resumeSource: 'desktop-resume',
    })
    expect(result).toBeNull()
    expect(JSON.stringify(readSessionNavigationDiag())).not.toContain('PRIVATE')
  })
})
