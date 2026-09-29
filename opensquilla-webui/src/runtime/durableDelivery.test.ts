import { afterEach, describe, expect, it, vi } from 'vitest'
import { reactive } from 'vue'
import { createDurableDelivery } from './durableDelivery'
import { TurnCommandError } from '@/modules/turnCommands'
import type { TurnCommands, TurnSendRequest, TurnReceiptResult, TurnSendResponse, TurnSteerResponse, TurnCancelResponse } from '@/modules/turnCommands'
import type { DeliveryWalRecord, PendingInputWal } from '@/utils/chat/pendingInputWal'

function memoryWal() {
  const records = new Map<string, DeliveryWalRecord>()
  const wal: PendingInputWal = {
    put: async () => {}, list: async () => [], delete: async () => {}, close: vi.fn(),
    getDelivery: async id => records.has(id) ? structuredClone(records.get(id)!) : null,
    listDeliveries: async () => [...records.values()].map(record => structuredClone(record)),
    prepareDelivery: async record => {
      const existing = records.get(record.ownerRequestId)
      if (existing) return { applied: false, record: structuredClone(existing) }
      records.set(record.ownerRequestId, structuredClone(record))
      return { applied: true, record: structuredClone(record) }
    },
    compareAndSwapDelivery: async (id, revision, record) => {
      const current = records.get(id)
      if (!current || current.revision !== revision) return { applied: false, record: current ? structuredClone(current) : null }
      if (record) records.set(id, structuredClone(record))
      else records.delete(id)
      return { applied: true, record: record ? structuredClone(record) : null }
    },
  }
  return { records, wal }
}

function request(id = 'synthetic-request'): Extract<TurnSendRequest, { kind: 'new-turn' }> {
  return { kind: 'new-turn', params: { sessionKey: 'synthetic-session', message: 'synthetic message',
    clientRequestId: id, clientMessageId: `message-${id}` } }
}

function harness(overrides: Partial<TurnCommands> = {}, storage = memoryWal()) {
  let identity = 'synthetic-identity'
  let available = true
  let generation = 1
  const commands: TurnCommands = {
    send: vi.fn(async () => ({ taskId: 'synthetic-task', sessionKey: 'synthetic-session' })),
    steer: vi.fn(async () => ({ accepted: true })),
    cancel: vi.fn(async () => ({ aborted: true })),
    lookupReceipt: vi.fn(async () => ({ status: 'not-found' as const })),
    supportsReceiptLookup: () => true, supports: () => true, ...overrides,
  }
  const owner = createDurableDelivery({ commands, wal: storage.wal, ownerId: crypto.randomUUID(),
    access: { identity: () => identity, available: () => available, generation: () => generation } })
  return { owner, commands, ...storage,
    identity: (value: string) => { identity = value; generation += 1 },
    available: (value: boolean) => { available = value },
  }
}

async function flush() { for (let index = 0; index < 60; index += 1) await Promise.resolve() }

afterEach(() => { vi.clearAllTimers(); vi.useRealTimers() })

describe('application-owned durable delivery', () => {
  it.each(['prepared', 'unknown'] as const)('drops a retired workflow %s record without sending or replaying', async phase => {
    const storage = memoryWal()
    const oldRequest = request('retired-workflow')
    oldRequest.params.clientMessageId = 'hidden-control:retired-workflow'
    storage.records.set('retired-workflow', {
      schemaVersion: 2, ownerRequestId: 'retired-workflow', deliveryIdentity: 'synthetic-identity',
      requestSessionKey: 'synthetic-session', request: { kind: 'send', request: oldRequest },
      phase, revision: 1, createdAt: 1, updatedAt: 1,
    })
    const test = harness({}, storage)
    try {
      await test.owner.wake()
      expect(storage.records.size).toBe(0)
      expect(test.commands.send).not.toHaveBeenCalled()
      expect(test.commands.lookupReceipt).not.toHaveBeenCalled()
    } finally { test.owner.dispose() }
  })

  it.each([
    { aborted: true },
    { aborted: false, reason: 'task_not_active' },
    { aborted: false, reason: 'task_mismatch' },
  ])('settles a known exact Stop despite failed initial WAL persistence: %j', async response => {
    const storage = memoryWal()
    const prepare = storage.wal.prepareDelivery!
    storage.wal.prepareDelivery = async () => { throw new Error('Synthetic quota failure') }
    const test = harness({ cancel: vi.fn(async () => response) }, storage)
    try {
      await expect(test.owner.commands.cancel({ sessionKey: 'synthetic-session', taskId: 'known-task', scope: 'task' })).resolves.toEqual(response)
      const summary = test.owner.snapshots()[0]!
      expect(summary).toMatchObject({ stopPending: false, waitReason: 'storage' })
      test.identity('other-identity')
      await test.owner.wake()
      expect(test.owner.snapshots()[0]).toMatchObject({ stopPending: false, waitReason: 'identity', stopAvailable: false })
      await test.owner.retry(summary.id)
      expect(test.commands.cancel).toHaveBeenCalledTimes(1)
      expect(storage.records.size).toBe(0)
      test.identity('synthetic-identity')
      storage.wal.prepareDelivery = prepare
      await test.owner.wake()
      expect(storage.records.get(summary.id)?.stop).toMatchObject({ completed: true, request: { taskId: 'known-task' } })
      expect(test.owner.snapshots()[0]).toMatchObject({ stopPending: false })
      expect(test.owner.snapshots()[0]?.waitReason).toBeUndefined()
      expect(test.commands.cancel).toHaveBeenCalledTimes(1)
    } finally { test.owner.dispose() }
  })

  it('retries an unknown exact Stop with the same task after storage recovers', async () => {
    const storage = memoryWal()
    const prepare = storage.wal.prepareDelivery!
    storage.wal.prepareDelivery = async () => { throw new Error('Synthetic quota failure') }
    const cancel = vi.fn().mockResolvedValueOnce({ aborted: false, reason: 'task_cancel_unknown' }).mockResolvedValue({ aborted: true })
    const test = harness({ cancel }, storage)
    try {
      await test.owner.commands.cancel({ sessionKey: 'synthetic-session', taskId: 'known-task', scope: 'task' })
      const summary = test.owner.snapshots()[0]!
      await test.owner.wake()
      expect(cancel).toHaveBeenCalledTimes(1)
      expect(test.owner.snapshots()[0]).toMatchObject({ stopPending: true, waitReason: 'storage' })
      storage.wal.prepareDelivery = prepare
      await test.owner.retry(summary.id)
      expect(cancel).toHaveBeenCalledTimes(2)
      expect(cancel).toHaveBeenLastCalledWith(expect.objectContaining({ sessionKey: 'synthetic-session', taskId: 'known-task', scope: 'task' }), expect.any(Object))
      expect(storage.records.get(summary.id)?.stop?.completed).toBe(true)
      expect(test.owner.snapshots()[0]).toMatchObject({ stopPending: false })
      expect(test.owner.snapshots()[0]?.waitReason).toBeUndefined()
      expect(test.commands.send).not.toHaveBeenCalled()
      expect(test.commands.lookupReceipt).not.toHaveBeenCalled()
    } finally { test.owner.dispose() }
  })

  it('shares the in-flight exact Stop with wake when an existing delivery CAS fails', async () => {
    const storage = memoryWal()
    storage.records.set('synthetic-request', { schemaVersion: 2, ownerRequestId: 'synthetic-request',
      deliveryIdentity: 'synthetic-identity', requestSessionKey: 'synthetic-session', phase: 'accepted',
      request: { kind: 'send', request: request() }, response: { taskId: 'known-task' },
      revision: 1, createdAt: 1, updatedAt: 1 })
    const compare = storage.wal.compareAndSwapDelivery!
    storage.wal.compareAndSwapDelivery = async () => { throw new Error('Synthetic quota failure') }
    let finish!: (response: TurnCancelResponse) => void
    const test = harness({ cancel: vi.fn(() => new Promise<TurnCancelResponse>(resolve => { finish = resolve })) }, storage)
    try {
      const stopping = test.owner.commands.cancel({ sessionKey: 'synthetic-session', taskId: 'known-task', scope: 'task' })
      await flush()
      const waking = test.owner.wake()
      await flush()
      expect(test.commands.cancel).toHaveBeenCalledTimes(1)
      finish({ aborted: true })
      await Promise.all([stopping, waking])
      expect(test.owner.snapshots()).toEqual([expect.objectContaining({ id: 'synthetic-request', stopPending: false, waitReason: 'storage' })])
      storage.wal.compareAndSwapDelivery = compare
      await test.owner.wake()
      expect(storage.records.size).toBe(1)
      expect(storage.records.get('synthetic-request')?.stop?.completed).toBe(true)
      expect(test.owner.snapshots()[0]?.waitReason).toBeUndefined()
      expect(test.commands.cancel).toHaveBeenCalledTimes(1)
    } finally { test.owner.dispose() }
  })

  it('recovers a volatile exact Stop on a new connection even while WAL is unavailable', async () => {
    const storage = memoryWal()
    storage.wal.prepareDelivery = undefined
    const cancel = vi.fn().mockResolvedValueOnce({ aborted: false, reason: 'task_cancel_unknown' }).mockResolvedValue({ aborted: true })
    const test = harness({ cancel }, storage)
    try {
      await test.owner.commands.cancel({ sessionKey: 'synthetic-session', taskId: 'known-task', scope: 'task' })
      await test.owner.wake()
      expect(cancel).toHaveBeenCalledTimes(1)
      test.identity('synthetic-identity')
      await test.owner.wake()
      expect(cancel).toHaveBeenCalledTimes(2)
      expect(test.owner.snapshots()[0]).toMatchObject({ stopPending: false, waitReason: 'storage' })
      expect(storage.records.size).toBe(0)
    } finally { test.owner.dispose() }
  })

  it.each([true, false])('does not overlap a volatile exact Stop when WAL recovers before its %s response', async aborted => {
    const storage = memoryWal()
    const prepare = storage.wal.prepareDelivery!
    storage.wal.prepareDelivery = async () => { throw new Error('Synthetic quota failure') }
    let finish!: (response: TurnCancelResponse) => void
    const cancel = vi.fn().mockImplementationOnce(() => new Promise<TurnCancelResponse>(resolve => { finish = resolve }))
      .mockResolvedValue({ aborted: true })
    const test = harness({ cancel }, storage)
    try {
      const stopping = test.owner.commands.cancel({ sessionKey: 'synthetic-session', taskId: 'known-task', scope: 'task' })
      await flush()
      const id = test.owner.snapshots()[0]!.id
      storage.wal.prepareDelivery = prepare
      const waking = test.owner.wake()
      await flush()
      expect(storage.records.get(id)?.stop).toMatchObject({ requested: true, completed: false })
      expect(cancel).toHaveBeenCalledTimes(1)
      finish({ aborted, ...(aborted ? {} : { reason: 'task_cancel_unknown' }) })
      await Promise.all([stopping, waking])
      expect(cancel).toHaveBeenCalledTimes(1)
      expect(test.owner.snapshots()[0]?.stopPending).toBe(!aborted)
      if (!aborted) {
        await test.owner.retry(id)
        expect(cancel).toHaveBeenCalledTimes(2)
      } else await test.owner.wake()
      expect(storage.records.get(id)?.stop?.completed).toBe(true)
      expect(test.owner.snapshots()[0]).toMatchObject({ stopPending: false })
      expect(test.owner.snapshots()[0]?.waitReason).toBeUndefined()
      expect(cancel).toHaveBeenCalledTimes(aborted ? 1 : 2)
    } finally { test.owner.dispose() }
  })

  it('keeps late exact Stop results separate when identities share session and task coordinates', async () => {
    const storage = memoryWal()
    storage.wal.prepareDelivery = async () => { throw new Error('Synthetic quota failure') }
    let resolveFirst!: (response: TurnCancelResponse) => void
    const cancel = vi.fn().mockImplementationOnce(() => new Promise<TurnCancelResponse>(resolve => { resolveFirst = resolve }))
      .mockResolvedValue({ aborted: true })
    const test = harness({ cancel }, storage)
    const exact = { sessionKey: 'synthetic-session', taskId: 'known-task', scope: 'task' as const }
    try {
      const first = test.owner.commands.cancel(exact)
      await flush()
      const firstId = test.owner.snapshots()[0]!.id
      test.identity('other-identity')
      await test.owner.wake()
      const second = test.owner.commands.cancel(exact)
      await flush()
      resolveFirst({ aborted: false, reason: 'task_cancel_unknown' })
      await Promise.all([first, second])
      const snapshots = test.owner.snapshots()
      expect(snapshots).toHaveLength(2)
      expect(snapshots.find(item => item.id === firstId)).toMatchObject({ stopPending: true, waitReason: 'identity' })
      expect(snapshots.find(item => item.id !== firstId)).toMatchObject({ stopPending: false, waitReason: 'storage' })
      await test.owner.retry(firstId)
      expect(cancel).toHaveBeenCalledTimes(2)
      test.identity('synthetic-identity')
      await test.owner.retry(firstId)
      expect(cancel).toHaveBeenCalledTimes(3)
      expect(cancel).toHaveBeenLastCalledWith(expect.objectContaining(exact), expect.objectContaining({ expectedGeneration: 3 }))
      expect(test.owner.snapshots().find(item => item.id === firstId)).toMatchObject({ stopPending: false, waitReason: 'storage' })
    } finally { test.owner.dispose() }
  })

  it('bounds completed not-sent notifications without evicting an unresolved delivery', async () => {
    const storage = memoryWal()
    const seed = (id: string, phase: DeliveryWalRecord['phase']): DeliveryWalRecord => ({
      schemaVersion: 2, ownerRequestId: id, deliveryIdentity: 'synthetic-identity',
      requestSessionKey: 'synthetic-session', request: { kind: 'send', request: request(id) },
      phase, ...(phase === 'not-sent' ? { stop: { requested: true, completed: true } } : {}),
      revision: 1, createdAt: 1, updatedAt: 1,
    })
    storage.records.set('unresolved', seed('unresolved', 'unknown'))
    const test = harness({}, storage)
    test.available(false)
    try {
      await test.owner.wake()
      for (let index = 0; index < 130; index += 1) {
        const id = `stopped-${index}`
        storage.records.set(id, seed(id, 'not-sent'))
        await expect(test.owner.commands.send(request(id))).rejects.toMatchObject({ failureCode: 'DELIVERY_STOPPED' })
      }
      expect(test.owner.snapshots()).toHaveLength(129)
      expect(test.owner.snapshots().find(item => item.id === 'unresolved')).toMatchObject({ phase: 'unknown' })
      expect(storage.records).toHaveLength(131)
      expect(test.commands.send).not.toHaveBeenCalled()
      expect(test.commands.lookupReceipt).not.toHaveBeenCalled()
    } finally { test.owner.dispose() }
  })

  it('offers exact Stop while sending or unknown, including offline, and revokes it after identity changes', async () => {
    let rejectSend!: (reason: unknown) => void
    const test = harness({ send: vi.fn(() => new Promise<TurnSendResponse>((_, reject) => { rejectSend = reject })) })
    const sending = test.owner.commands.send(request())
    const failed = expect(sending).rejects.toThrow('synthetic lost ACK')
    await flush()
    expect(test.owner.snapshots()[0]).toMatchObject({ phase: 'submitting', stopAvailable: true })
    test.available(false)
    rejectSend(new TurnCommandError('transport', 'synthetic lost ACK', undefined, null))
    await failed
    await test.owner.wake()
    expect(test.owner.snapshots()[0]).toMatchObject({ phase: 'unknown', stopAvailable: true })
    expect(test.owner.snapshots()[0]?.preview).toBe('synthetic message')
    const identityUpdate = vi.fn()
    test.owner.subscribe(() => identityUpdate(test.owner.snapshots()[0]))
    test.identity('another-identity')
    const changed = test.owner.wake()
    // Subscribers clear their rendered preview before asynchronous WAL paging.
    expect(identityUpdate).toHaveBeenCalledWith(expect.objectContaining({ stopAvailable: false, waitReason: 'identity', preview: undefined }))
    await changed
    expect(test.owner.snapshots()[0]).toMatchObject({ stopAvailable: false, waitReason: 'identity' })
    expect(test.owner.snapshots()[0]?.preview).toBeUndefined()
    await test.owner.requestStop('synthetic-request')
    expect(test.records.get('synthetic-request')?.stop).toBeUndefined()
    test.identity('synthetic-identity')
    await test.owner.wake()
    expect(test.owner.snapshots()[0]?.stopAvailable).toBe(true)
    await test.owner.requestStop('synthetic-request')
    expect(test.records.get('synthetic-request')?.stop?.requested).toBe(true)
    expect(test.owner.snapshots()[0]?.stopAvailable).toBe(false)
    expect(test.commands.cancel).not.toHaveBeenCalled()
    expect(test.commands.lookupReceipt).not.toHaveBeenCalled()
    test.owner.dispose()
  })

  it('does not offer Stop for an unsent or authority-paused delivery', async () => {
    const test = harness()
    test.available(false)
    await expect(test.owner.commands.send(request('not-sent'))).rejects.toMatchObject({ accepted: false })
    test.records.set('paused', { schemaVersion: 2, ownerRequestId: 'paused', requestSessionKey: 'synthetic-session',
      deliveryIdentity: 'synthetic-identity', phase: 'unknown', request: { kind: 'send', request: request('paused') },
      paused: 'authority', revision: 1, createdAt: 1, updatedAt: 1 })
    await test.owner.wake()
    expect(test.owner.snapshots().every(item => !item.stopAvailable)).toBe(true)
    test.owner.dispose()
  })

  it('revokes Stop actions and previews synchronously when storage is invalidated', async () => {
    const storage = memoryWal()
    let invalidate!: () => void
    storage.wal.onInvalidated = listener => { invalidate = listener; return () => {} }
    storage.records.set('synthetic-request', { schemaVersion: 2, ownerRequestId: 'synthetic-request', requestSessionKey: 'synthetic-session',
      deliveryIdentity: 'synthetic-identity', phase: 'unknown', request: { kind: 'send', request: request() },
      revision: 1, createdAt: 1, updatedAt: 1 })
    const test = harness({}, storage)
    test.available(false)
    await test.owner.wake()
    expect(test.owner.snapshots()[0]).toMatchObject({ stopAvailable: true, preview: 'synthetic message' })
    const changed = vi.fn()
    test.owner.subscribe(() => changed(test.owner.snapshots()[0]))
    invalidate()
    expect(changed).toHaveBeenCalledWith(expect.objectContaining({ stopAvailable: false, preview: undefined }))
    test.identity('another-identity')
    await test.owner.requestStop('synthetic-request')
    expect(storage.records.get('synthetic-request')?.stop).toBeUndefined()
    expect(test.commands.cancel).not.toHaveBeenCalled()
    test.owner.dispose()
  })

  it('fences Stop after an asynchronous WAL read changes identity without losing the original intent', async () => {
    const test = harness({ send: vi.fn(async () => { throw new TurnCommandError('transport', 'lost ACK', undefined, null) }) })
    test.available(false)
    // Seed a real-shaped unknown record to isolate the Stop read/CAS boundary.
    test.records.set('synthetic-request', { schemaVersion: 2, ownerRequestId: 'synthetic-request', requestSessionKey: 'synthetic-session',
      deliveryIdentity: 'synthetic-identity', phase: 'unknown', request: { kind: 'send', request: request() },
      revision: 1, createdAt: 1, updatedAt: 1 })
    await test.owner.wake()
    const originalGet = test.wal.getDelivery!
    let release!: () => void
    test.wal.getDelivery = async id => {
      const record = await originalGet(id)
      await new Promise<void>(resolve => { release = resolve })
      return record
    }
    const stop = test.owner.requestStop('synthetic-request')
    await flush()
    expect(test.owner.snapshots()[0]).toMatchObject({ stopPending: true, stopAvailable: false })
    test.identity('another-identity')
    test.wal.getDelivery = originalGet
    release()
    await stop
    await test.owner.wake()
    expect(test.records.get('synthetic-request')?.stop).toBeUndefined()
    await test.owner.requestStop('synthetic-request')
    expect(test.records.get('synthetic-request')?.stop).toBeUndefined()
    test.identity('synthetic-identity')
    await test.owner.wake()
    expect(test.records.get('synthetic-request')?.stop?.requested).toBe(true)
    expect(test.commands.cancel).not.toHaveBeenCalled()
    test.owner.dispose()
  })

  it('snapshots nested Vue proxies before storing an immutable request', async () => {
    const test = harness()
    const sending = reactive(request())
    if (sending.kind !== 'new-turn') throw new Error('synthetic fixture')
    sending.params.source = reactive({ policy: reactive({ mode: 'synthetic-safe' }) })
    sending.params.selectedSkills = reactive([{ instanceId: 'synthetic-skill', name: 'synthetic skill', digest: 'a'.repeat(64) }])
    expect(() => structuredClone(sending)).toThrow()
    await test.owner.commands.send(sending)
    expect(test.commands.send).toHaveBeenCalledTimes(1)
    const stored = test.records.get('synthetic-request')!.request
    sending.params.source.policy = { mode: 'edited' }
    expect(stored).toMatchObject({ request: { params: { source: { policy: { mode: 'synthetic-safe' } } } } })
    test.owner.dispose()
  })
  it('commits every ordinary send before admission and fails closed if WAL is unavailable', async () => {
    const storage = memoryWal()
    const send = vi.fn(async () => {
      expect(storage.records.get('synthetic-request')?.phase).toBe('submitting')
      return { taskId: 'synthetic-task' }
    })
    const test = harness({ send }, storage)
    await test.owner.commands.send(request())
    expect(send).toHaveBeenCalledTimes(1)
    expect(test.records.get('synthetic-request')?.phase).toBe('accepted')
    test.owner.dispose()
    const broken = memoryWal()
    broken.wal.prepareDelivery = async () => { throw new Error('synthetic quota exceeded') }
    const denied = harness({}, broken)
    await expect(denied.owner.commands.send(request())).rejects.toThrow('quota')
    expect(denied.commands.send).not.toHaveBeenCalled()
    denied.owner.dispose()
  })

  it('uses only receipt reads after a lost response and parks after four calls', async () => {
    vi.useFakeTimers()
    const test = harness({ send: vi.fn(async () => { throw new TurnCommandError('transport', 'synthetic lost response', undefined, null) }) })
    await expect(test.owner.commands.send(request())).rejects.toThrow('lost response')
    await vi.advanceTimersByTimeAsync(6_000)
    expect(test.commands.send).toHaveBeenCalledTimes(1)
    expect(test.commands.lookupReceipt).toHaveBeenCalledTimes(4)
    await vi.advanceTimersByTimeAsync(120_000)
    expect(test.commands.lookupReceipt).toHaveBeenCalledTimes(4)
    expect(test.owner.snapshots()[0]?.waitReason).toBe('budget')
    test.owner.dispose()
  })

  it('classifies an initial local connection gate as not sent and preserves its frozen payload', async () => {
    const test = harness()
    test.available(false)
    await expect(test.owner.commands.send(request())).rejects.toMatchObject({ accepted: false })
    expect(test.commands.send).not.toHaveBeenCalled()
    expect(test.records.get('synthetic-request')).toMatchObject({ phase: 'not-sent',
      request: { request: { params: { message: 'synthetic message' } } } })
    test.owner.dispose()
  })

  it('settles Stop latched during an authoritative initial rejection without receipt reads', async () => {
    vi.useFakeTimers()
    let rejectSend!: (reason: unknown) => void
    const test = harness({ send: vi.fn(() => new Promise<TurnSendResponse>((_, reject) => { rejectSend = reject })) })
    try {
      const sending = test.owner.commands.send(request())
      const rejected = expect(sending).rejects.toMatchObject({ accepted: false })
      await flush()
      await test.owner.requestStop('synthetic-request')
      rejectSend(new TurnCommandError('unavailable', 'Synthetic ingress queue full', 'UNAVAILABLE', false, true))
      await rejected
      await vi.advanceTimersByTimeAsync(6_000)
      await test.owner.wake()
      expect(test.records.get('synthetic-request')).toMatchObject({ phase: 'not-sent', stop: { requested: true, completed: true } })
      expect(test.owner.snapshots()[0]?.stopPending).toBe(false)
      expect(test.commands.send).toHaveBeenCalledTimes(1)
      expect(test.commands.lookupReceipt).not.toHaveBeenCalled()
      expect(test.commands.cancel).not.toHaveBeenCalled()
    } finally { test.owner.dispose() }
  })

  it('settles Stop after an initial rejected Steer without a terminal disposition', async () => {
    vi.useFakeTimers()
    let resolveSteer!: (response: TurnSteerResponse) => void
    const test = harness({ steer: vi.fn(() => new Promise<TurnSteerResponse>(resolve => { resolveSteer = resolve })) })
    try {
      const steering = test.owner.commands.steer({ key: 'synthetic-session', clientRequestId: 'synthetic-steer',
        clientMessageId: 'synthetic-message', expectedTurnId: 'old-task', message: 'Synthetic steer' })
      await flush()
      await test.owner.requestStop('synthetic-steer')
      resolveSteer({ status: 'not_accepted', accepted: false, fallbackSafe: true, failureCode: 'ACTIVE_TURN_NOT_STEERABLE' })
      await steering
      await vi.advanceTimersByTimeAsync(6_000)
      await test.owner.wake()
      expect(test.records.get('synthetic-steer')).toMatchObject({ phase: 'not-sent', stop: { requested: true, completed: true } })
      expect(test.owner.snapshots()[0]?.stopPending).toBe(false)
      expect(test.commands.steer).toHaveBeenCalledTimes(1)
      expect(test.commands.lookupReceipt).not.toHaveBeenCalled()
      expect(test.commands.cancel).not.toHaveBeenCalled()
    } finally { test.owner.dispose() }
  })

  it.each([true, false])('settles a persisted not-sent Stop after reopening with connection available=%s', async available => {
    vi.useFakeTimers()
    const storage = memoryWal()
    storage.records.set('synthetic-request', { schemaVersion: 2, ownerRequestId: 'synthetic-request',
      deliveryIdentity: 'synthetic-identity', requestSessionKey: 'synthetic-session',
      request: { kind: 'send', request: request() }, phase: 'not-sent', stop: { requested: true },
      revision: 1, createdAt: 1, updatedAt: 1 })
    const test = harness({}, storage)
    test.available(available)
    try {
      const recovery = test.owner.wake()
      await vi.advanceTimersByTimeAsync(6_000)
      await recovery
      expect(storage.records.get('synthetic-request')?.stop?.completed).toBe(true)
      expect(test.owner.snapshots()[0]?.stopPending).toBe(false)
      expect(test.commands.lookupReceipt).not.toHaveBeenCalled()
      expect(test.commands.send).not.toHaveBeenCalled()
      expect(test.commands.cancel).not.toHaveBeenCalled()
    } finally { test.owner.dispose() }
  })

  it('does not settle an unknown delivery after losing a not-sent Stop CAS to another tab', async () => {
    const storage = memoryWal()
    storage.records.set('synthetic-request', { schemaVersion: 2, ownerRequestId: 'synthetic-request',
      deliveryIdentity: 'synthetic-identity', requestSessionKey: 'synthetic-session',
      request: { kind: 'send', request: request() }, phase: 'not-sent', stop: { requested: true },
      revision: 1, createdAt: 1, updatedAt: 1 })
    const compare = storage.wal.compareAndSwapDelivery!
    let raced = false
    storage.wal.compareAndSwapDelivery = async (id, revision, record) => {
      if (!raced && record?.stop?.completed) {
        raced = true
        const newer: DeliveryWalRecord = { ...storage.records.get(id)!, revision: revision + 1,
          phase: 'unknown', lease: { owner: 'other-tab', epoch: 2, expiresAt: Date.now() + 60_000 } }
        storage.records.set(id, newer)
        return { applied: false, record: structuredClone(newer) }
      }
      return compare(id, revision, record)
    }
    const test = harness({}, storage)
    test.available(false)
    try {
      await test.owner.wake()
      expect(raced).toBe(true)
      expect(storage.records.get('synthetic-request')).toMatchObject({ phase: 'unknown',
        stop: { requested: true }, lease: { owner: 'other-tab', epoch: 2 } })
      expect(storage.records.get('synthetic-request')?.stop?.completed).not.toBe(true)
      expect(test.owner.snapshots()[0]?.stopPending).toBe(true)
      expect(test.commands.lookupReceipt).not.toHaveBeenCalled()
      expect(test.commands.send).not.toHaveBeenCalled()
      expect(test.commands.cancel).not.toHaveBeenCalled()
    } finally { test.owner.dispose() }
  })

  it('persists a failed Stop intent as completed after the initial call is authoritatively rejected', async () => {
    vi.useFakeTimers()
    let rejectSend!: (reason: unknown) => void
    const storage = memoryWal()
    const compare = storage.wal.compareAndSwapDelivery!
    storage.wal.compareAndSwapDelivery = async (id, revision, record) => {
      if (record?.phase === 'submitting' && record.stop) throw new Error('Synthetic Stop quota failure')
      return compare(id, revision, record)
    }
    const test = harness({ send: vi.fn(() => new Promise<TurnSendResponse>((_, reject) => { rejectSend = reject })) }, storage)
    try {
      const sending = test.owner.commands.send(request())
      const rejected = expect(sending).rejects.toMatchObject({ accepted: false })
      await flush()
      await test.owner.requestStop('synthetic-request')
      expect(test.owner.snapshots()[0]).toMatchObject({ stopPending: true, waitReason: 'storage' })
      rejectSend(new TurnCommandError('unavailable', 'Synthetic ingress queue full', 'UNAVAILABLE', false, true))
      await rejected
      await vi.advanceTimersByTimeAsync(6_000)
      await test.owner.wake()
      expect(storage.records.get('synthetic-request')).toMatchObject({ phase: 'not-sent', stop: { requested: true, completed: true } })
      expect(test.owner.snapshots()[0]?.stopPending).toBe(false)
      expect(test.commands.lookupReceipt).not.toHaveBeenCalled()
      expect(test.commands.cancel).not.toHaveBeenCalled()
    } finally { test.owner.dispose() }
  })

  it('does not reopen a completed not-sent Stop when a failed Stop intent can be persisted again', async () => {
    vi.useFakeTimers()
    const storage = memoryWal()
    storage.records.set('synthetic-request', { schemaVersion: 2, ownerRequestId: 'synthetic-request',
      deliveryIdentity: 'synthetic-identity', requestSessionKey: 'synthetic-session',
      request: { kind: 'send', request: request() }, phase: 'not-sent', stop: { requested: true, completed: true },
      revision: 1, createdAt: 1, updatedAt: 1 })
    const compare = storage.wal.compareAndSwapDelivery!
    storage.wal.compareAndSwapDelivery = async () => { throw new Error('Synthetic Stop quota failure') }
    const test = harness({}, storage)
    try {
      await test.owner.requestStop('synthetic-request')
      await test.owner.wake()
      storage.wal.compareAndSwapDelivery = compare
      const recovery = test.owner.wake()
      await vi.advanceTimersByTimeAsync(6_000)
      await recovery
      expect(storage.records.get('synthetic-request')?.stop?.completed).toBe(true)
      expect(test.owner.snapshots()[0]?.stopPending).toBe(false)
      expect(test.commands.lookupReceipt).not.toHaveBeenCalled()
      expect(test.commands.cancel).not.toHaveBeenCalled()
      expect(test.commands.send).not.toHaveBeenCalled()
    } finally { test.owner.dispose() }
  })

  it.each(['send', 'steer'] as const)('retains an initial %s rejection proof when both Stop and rejection-state writes fail', async kind => {
    vi.useFakeTimers()
    let rejectSend!: (reason: unknown) => void
    let resolveSteer!: (response: TurnSteerResponse) => void
    const test = harness({
      send: vi.fn(() => new Promise<TurnSendResponse>((_, reject) => { rejectSend = reject })),
      steer: vi.fn(() => new Promise<TurnSteerResponse>(resolve => { resolveSteer = resolve })),
    })
    const compare = test.wal.compareAndSwapDelivery!
    try {
      const operation = kind === 'send' ? test.owner.commands.send(request())
        : test.owner.commands.steer({ key: 'synthetic-session', clientRequestId: 'synthetic-request',
          clientMessageId: 'synthetic-message', expectedTurnId: 'old-task', message: 'Synthetic steer' })
      const failed = expect(operation).rejects.toThrow()
      await flush()
      test.wal.compareAndSwapDelivery = async () => { throw new Error('Synthetic quota failure') }
      await test.owner.requestStop('synthetic-request')
      if (kind === 'send') rejectSend(new TurnCommandError('unavailable', 'Synthetic initial refusal', 'UNAVAILABLE', false))
      else resolveSteer({ status: 'not_accepted', accepted: false, fallbackSafe: true })
      await failed
      await vi.advanceTimersByTimeAsync(6_000)
      await test.owner.wake()
      expect(test.owner.snapshots()[0]?.waitReason).toBe('storage')
      expect(test.commands.lookupReceipt).not.toHaveBeenCalled()
      expect(test.commands.cancel).not.toHaveBeenCalled()
      test.wal.compareAndSwapDelivery = compare
      const recovery = test.owner.wake()
      await vi.advanceTimersByTimeAsync(6_000)
      await recovery
      expect(test.records.get('synthetic-request')).toMatchObject({ phase: 'not-sent', stop: { requested: true, completed: true } })
      expect(test.owner.snapshots()[0]?.stopPending).toBe(false)
      expect(test.commands.lookupReceipt).not.toHaveBeenCalled()
      expect(test.commands.cancel).not.toHaveBeenCalled()
      expect(kind === 'send' ? test.commands.send : test.commands.steer).toHaveBeenCalledTimes(1)
    } finally { test.owner.dispose() }
  })

  it('discards a cached initial rejection when another lease epoch owns an unknown delivery', async () => {
    vi.useFakeTimers()
    let rejectSend!: (reason: unknown) => void
    const test = harness({ send: vi.fn(() => new Promise<TurnSendResponse>((_, reject) => { rejectSend = reject })) })
    const compare = test.wal.compareAndSwapDelivery!
    try {
      const sending = test.owner.commands.send(request())
      const failed = expect(sending).rejects.toMatchObject({ accepted: false })
      await flush()
      test.wal.compareAndSwapDelivery = async () => { throw new Error('Synthetic quota failure') }
      await test.owner.requestStop('synthetic-request')
      rejectSend(new TurnCommandError('unavailable', 'Synthetic initial refusal', 'UNAVAILABLE', false))
      await failed
      await flush()
      const previous = test.records.get('synthetic-request')!
      test.records.set('synthetic-request', { ...previous, phase: 'unknown', revision: previous.revision + 1,
        lease: { owner: 'other-tab', epoch: 9, expiresAt: Date.now() + 60_000 } })
      test.wal.compareAndSwapDelivery = compare
      await test.owner.wake()
      expect(test.records.get('synthetic-request')).toMatchObject({ phase: 'unknown',
        stop: { requested: true, completed: false }, lease: { owner: 'other-tab', epoch: 9 } })
      expect(test.owner.snapshots()[0]?.stopPending).toBe(true)
      expect(test.commands.lookupReceipt).not.toHaveBeenCalled()
      expect(test.commands.cancel).not.toHaveBeenCalled()
    } finally { test.owner.dispose() }
  })

  it('supersedes a cached initial rejection with an accepted receipt in the same lease epoch', async () => {
    vi.useFakeTimers()
    let rejectSend!: (reason: unknown) => void
    const test = harness({ send: vi.fn(() => new Promise<TurnSendResponse>((_, reject) => { rejectSend = reject })) })
    const compare = test.wal.compareAndSwapDelivery!
    try {
      const sending = test.owner.commands.send(request())
      const failed = expect(sending).rejects.toMatchObject({ accepted: false })
      await flush()
      test.wal.compareAndSwapDelivery = async () => { throw new Error('Synthetic quota failure') }
      await test.owner.requestStop('synthetic-request')
      rejectSend(new TurnCommandError('unavailable', 'Synthetic initial refusal', 'UNAVAILABLE', false))
      await failed
      await flush()
      const previous = test.records.get('synthetic-request')!
      test.records.set('synthetic-request', { ...previous, phase: 'accepted', revision: previous.revision + 1,
        response: { taskId: 'new-task', sessionKey: 'synthetic-session' },
        lease: { ...previous.lease!, expiresAt: 0 } })
      test.wal.compareAndSwapDelivery = compare
      await test.owner.wake()
      await vi.advanceTimersByTimeAsync(6_000)
      await test.owner.wake()
      expect(test.records.get('synthetic-request')).toMatchObject({ phase: 'accepted', stop: { completed: true } })
      expect(test.commands.cancel).toHaveBeenCalledWith(expect.objectContaining({ taskId: 'new-task' }), expect.anything())
      expect(test.commands.lookupReceipt).not.toHaveBeenCalled()
    } finally { test.owner.dispose() }
  })

  it('does not submit a prepared write-after-throw record on wake or explicit receipt recheck', async () => {
    const storage = memoryWal()
    const prepare = storage.wal.prepareDelivery!
    storage.wal.prepareDelivery = async record => { await prepare(record); throw new Error('synthetic write-after-throw') }
    const test = harness({}, storage)
    await expect(test.owner.commands.send(request())).rejects.toThrow('write-after-throw')
    await test.owner.wake()
    await test.owner.retry('synthetic-request')
    expect(test.commands.send).not.toHaveBeenCalled()
    expect(test.commands.lookupReceipt).not.toHaveBeenCalled()
    expect(test.owner.snapshots()[0]?.waitReason).toBe('not-sent')
    test.owner.dispose()
  })

  it('settles Stop for a prepared write-after-throw record without receipt lookup', async () => {
    const storage = memoryWal()
    const prepare = storage.wal.prepareDelivery!
    storage.wal.prepareDelivery = async record => { await prepare(record); throw new Error('synthetic write-after-throw') }
    const test = harness({}, storage)
    try {
      await expect(test.owner.commands.send(request())).rejects.toThrow('write-after-throw')
      await test.owner.requestStop('synthetic-request')
      await test.owner.wake()
      expect(storage.records.get('synthetic-request')).toMatchObject({ phase: 'not-sent', stop: { completed: true } })
      expect(test.owner.snapshots()[0]?.stopPending).toBe(false)
      expect(test.commands.send).not.toHaveBeenCalled()
      expect(test.commands.lookupReceipt).not.toHaveBeenCalled()
      expect(test.commands.cancel).not.toHaveBeenCalled()
    } finally { test.owner.dispose() }
  })

  it('settles a Stop latched before initial prepare finishes and never arms that dispatch', async () => {
    const storage = memoryWal()
    const prepare = storage.wal.prepareDelivery!
    let release!: () => void
    storage.wal.prepareDelivery = async record => {
      await new Promise<void>(resolve => { release = resolve })
      return prepare(record)
    }
    const test = harness({}, storage)
    try {
      const sending = test.owner.commands.send(request())
      const failed = expect(sending).rejects.toMatchObject({ accepted: false })
      await flush()
      await test.owner.requestStop('synthetic-request')
      release()
      await failed
      await test.owner.wake()
      expect(storage.records.get('synthetic-request')).toMatchObject({ phase: 'not-sent', stop: { completed: true } })
      expect(test.commands.send).not.toHaveBeenCalled()
      expect(test.commands.lookupReceipt).not.toHaveBeenCalled()
      expect(test.commands.cancel).not.toHaveBeenCalled()
    } finally { test.owner.dispose() }
  })

  it('fences a queued original dispatch after another tab stops it', async () => {
    const storage = memoryWal()
    const releases: Array<() => void> = []
    const first = harness({ send: vi.fn(() => new Promise<TurnSendResponse>(resolve => {
      releases.push(() => resolve({ taskId: `occupied-${releases.length}`, sessionKey: 'synthetic-session' }))
    })) }, storage)
    const second = harness({}, storage)
    try {
      const occupied = [first.owner.commands.send(request('slot-1')), first.owner.commands.send(request('slot-2'))]
      await flush()
      const queued = first.owner.commands.send(request())
      const settled = expect(queued).rejects.toMatchObject({ accepted: false })
      await flush()
      expect(storage.records.get('synthetic-request')?.phase).toBe('prepared')
      await second.owner.requestStop('synthetic-request')
      expect(storage.records.get('synthetic-request')).toMatchObject({ phase: 'not-sent', stop: { completed: true } })
      for (const release of releases) release()
      await Promise.all(occupied)
      await settled
      await flush()
      expect(first.commands.send).toHaveBeenCalledTimes(2)
      expect(second.commands.send).not.toHaveBeenCalled()
      expect(first.commands.cancel).not.toHaveBeenCalled()
      expect(second.commands.cancel).not.toHaveBeenCalled()
    } finally { first.owner.dispose(); second.owner.dispose() }
  })

  it('lets another tab Stop win the CAS immediately before admission is armed', async () => {
    const storage = memoryWal()
    const first = harness({}, storage)
    const second = harness({}, storage)
    const compare = storage.wal.compareAndSwapDelivery!
    let stopped = false
    storage.wal.compareAndSwapDelivery = async (id, revision, record) => {
      if (!stopped && id === 'synthetic-request' && record?.phase === 'submitting') {
        stopped = true
        await second.owner.requestStop(id)
      }
      return compare(id, revision, record)
    }
    try {
      await expect(first.owner.commands.send(request())).rejects.toMatchObject({ accepted: false, failureCode: 'DELIVERY_STOPPED' })
      expect(storage.records.get('synthetic-request')).toMatchObject({ phase: 'not-sent', stop: { completed: true } })
      expect(first.commands.send).not.toHaveBeenCalled()
      expect(second.commands.lookupReceipt).not.toHaveBeenCalled()
      expect(second.commands.cancel).not.toHaveBeenCalled()
    } finally { first.owner.dispose(); second.owner.dispose() }
  })

  it('blocks queued admission with an in-memory Stop when all Stop writes fail', async () => {
    const storage = memoryWal()
    const releases: Array<() => void> = []
    const test = harness({ send: vi.fn(() => new Promise<TurnSendResponse>(resolve => {
      releases.push(() => resolve({ taskId: 'occupied-task', sessionKey: 'synthetic-session' }))
    })) }, storage)
    const compare = storage.wal.compareAndSwapDelivery!
    try {
      const occupied = [test.owner.commands.send(request('slot-1')), test.owner.commands.send(request('slot-2'))]
      await flush()
      const queued = test.owner.commands.send(request())
      const rejected = expect(queued).rejects.toMatchObject({ accepted: false })
      await flush()
      storage.wal.compareAndSwapDelivery = async (id, revision, record) => {
        if (id === 'synthetic-request' && record?.stop) throw new Error('Synthetic Stop quota failure')
        return compare(id, revision, record)
      }
      await test.owner.requestStop('synthetic-request')
      for (const release of releases) release()
      await Promise.all(occupied)
      await rejected
      await test.owner.wake()
      expect(test.owner.snapshots().find(item => item.id === 'synthetic-request')).toMatchObject({ waitReason: 'storage' })
      expect(test.commands.send).toHaveBeenCalledTimes(2)
      expect(test.commands.lookupReceipt).not.toHaveBeenCalled()
      expect(test.commands.cancel).not.toHaveBeenCalled()
      storage.wal.compareAndSwapDelivery = compare
      await test.owner.wake()
      expect(storage.records.get('synthetic-request')).toMatchObject({ phase: 'not-sent', stop: { completed: true } })
    } finally { test.owner.dispose() }
  })

  it('retains the storage warning after a prepared Stop commits and then throws', async () => {
    const storage = memoryWal()
    storage.records.set('synthetic-request', { schemaVersion: 2, ownerRequestId: 'synthetic-request',
      deliveryIdentity: 'synthetic-identity', requestSessionKey: 'synthetic-session',
      request: { kind: 'send', request: request() }, phase: 'prepared', revision: 1, createdAt: 1, updatedAt: 1 })
    const compare = storage.wal.compareAndSwapDelivery!
    storage.wal.compareAndSwapDelivery = async (...args) => { await compare(...args); throw new Error('Synthetic post-commit failure') }
    const test = harness({}, storage)
    try {
      await test.owner.requestStop('synthetic-request')
      await test.owner.wake()
      expect(storage.records.get('synthetic-request')).toMatchObject({ phase: 'not-sent', stop: { completed: true } })
      expect(test.owner.snapshots()[0]?.waitReason).toBe('storage')
      expect(test.commands.lookupReceipt).not.toHaveBeenCalled()
      storage.wal.compareAndSwapDelivery = compare
      await test.owner.wake()
      expect(test.owner.snapshots()[0]).toMatchObject({ stopPending: false })
      expect(test.owner.snapshots()[0]?.waitReason).not.toBe('storage')
      expect(test.commands.send).not.toHaveBeenCalled()
    } finally { test.owner.dispose() }
  })

  it('requires a fresh request ID after a completed prepared Stop and never cancels the fresh task', async () => {
    const storage = memoryWal()
    const prepare = storage.wal.prepareDelivery!
    storage.wal.prepareDelivery = async record => { await prepare(record); throw new Error('synthetic write-after-throw') }
    const test = harness({}, storage)
    try {
      await expect(test.owner.commands.send(request())).rejects.toThrow('write-after-throw')
      await test.owner.requestStop('synthetic-request')
      storage.wal.prepareDelivery = prepare
      await expect(test.owner.commands.send(request())).rejects.toMatchObject({ accepted: false, failureCode: 'DELIVERY_STOPPED' })
      await expect(test.owner.commands.send(request('fresh-request'))).resolves.toMatchObject({ taskId: 'synthetic-task' })
      await test.owner.wake()
      expect(storage.records.get('synthetic-request')).toMatchObject({ phase: 'not-sent', stop: { completed: true } })
      expect(storage.records.get('fresh-request')).toMatchObject({ phase: 'accepted' })
      expect(storage.records.get('fresh-request')?.stop).toBeUndefined()
      expect(test.commands.send).toHaveBeenCalledTimes(1)
      expect(test.commands.lookupReceipt).not.toHaveBeenCalled()
      expect(test.commands.cancel).not.toHaveBeenCalled()
    } finally { test.owner.dispose() }
  })

  it('settles a reopened persisted prepared Stop offline without replaying admission', async () => {
    const storage = memoryWal()
    storage.records.set('synthetic-request', { schemaVersion: 2, ownerRequestId: 'synthetic-request',
      deliveryIdentity: 'synthetic-identity', requestSessionKey: 'synthetic-session',
      request: { kind: 'send', request: request() }, phase: 'prepared', stop: { requested: true },
      revision: 1, createdAt: 1, updatedAt: 1 })
    const test = harness({}, storage)
    test.available(false)
    try {
      await test.owner.wake()
      expect(storage.records.get('synthetic-request')).toMatchObject({ phase: 'not-sent', stop: { completed: true } })
      expect(test.commands.send).not.toHaveBeenCalled()
      expect(test.commands.lookupReceipt).not.toHaveBeenCalled()
      expect(test.commands.cancel).not.toHaveBeenCalled()
    } finally { test.owner.dispose() }
  })

  it('persists unknown Stop across owner disposal and resumes only for the same identity', async () => {
    vi.useFakeTimers()
    const storage = memoryWal()
    const first = harness({ send: vi.fn(async () => { throw new TurnCommandError('transport', 'synthetic response lost', undefined, null) }) }, storage)
    first.available(false)
    await expect(first.owner.commands.send(request())).rejects.toThrow()
    // Model a frame which crossed ingress just before shutdown.
    const saved = storage.records.get('synthetic-request')!
    storage.records.set(saved.ownerRequestId, { ...saved, phase: 'unknown' })
    await first.owner.requestStop(saved.ownerRequestId)
    first.owner.dispose()
    await flush()
    const second = harness({ lookupReceipt: vi.fn(async () => ({ status: 'found' as const, response: { taskId: 'recovered-task', sessionKey: 'forked-session' } })) }, storage)
    second.identity('other-synthetic-identity')
    await second.owner.wake()
    expect(second.commands.lookupReceipt).not.toHaveBeenCalled()
    expect(second.commands.cancel).not.toHaveBeenCalled()
    second.identity('synthetic-identity')
    const recovery = second.owner.wake()
    await vi.advanceTimersByTimeAsync(6_000)
    await recovery
    expect(second.commands.send).not.toHaveBeenCalled()
    expect(second.commands.cancel).toHaveBeenCalledWith({ sessionKey: 'forked-session', taskId: 'recovered-task', source: 'webui_stop', scope: 'task' }, expect.objectContaining({ expectedGeneration: 3 }))
    expect(storage.records.get(saved.ownerRequestId)?.stop?.completed).toBe(true)
    second.owner.dispose()
  })

  it('keeps unknown acceptance and Stop after a local lookup rejection or unsupported lookup', async () => {
    vi.useFakeTimers()
    const storage = memoryWal()
    storage.records.set('synthetic-request', {
      schemaVersion: 2, ownerRequestId: 'synthetic-request', deliveryIdentity: 'synthetic-identity', requestSessionKey: 'synthetic-session',
      request: { kind: 'send', request: request() }, phase: 'unknown', stop: { requested: true }, revision: 1, createdAt: 1, updatedAt: 1,
    })
    const test = harness({ lookupReceipt: vi.fn(async () => { throw new TurnCommandError('transport', 'checking', undefined, false) }) }, storage)
    const round = test.owner.wake()
    await vi.advanceTimersByTimeAsync(6_000)
    await round
    expect(storage.records.get('synthetic-request')?.phase).toBe('unknown')
    expect(storage.records.get('synthetic-request')?.stop?.completed).not.toBe(true)
    test.owner.dispose()
    const olderGateway = harness({ supportsReceiptLookup: () => false }, storage)
    await olderGateway.owner.wake()
    expect(olderGateway.commands.send).not.toHaveBeenCalled()
    expect(olderGateway.commands.lookupReceipt).not.toHaveBeenCalled()
    expect(olderGateway.owner.snapshots()[0]?.waitReason).toBe('receipt-unsupported')
    olderGateway.owner.dispose()
  })

  it('allows at most two admissions while an exact Stop has an independent slot', async () => {
    const resolves: Array<(response: { taskId: string }) => void> = []
    const test = harness({ send: vi.fn(() => new Promise<TurnSendResponse>(resolve => resolves.push(resolve))) })
    const sends = [test.owner.commands.send(request('a')), test.owner.commands.send(request('b')), test.owner.commands.send(request('c'))]
    await flush()
    expect(test.commands.send).toHaveBeenCalledTimes(2)
    await test.owner.commands.cancel({ sessionKey: 'synthetic-session', taskId: 'known-task', scope: 'task' })
    expect(test.commands.cancel).toHaveBeenCalledTimes(1)
    resolves.shift()!({ taskId: 'task-a' })
    await flush()
    expect(test.commands.send).toHaveBeenCalledTimes(3)
    for (const resolve of resolves) resolve({ taskId: 'other-task' })
    await Promise.all(sends)
    test.owner.dispose()
  })

  it('does not let a late former lease holder overwrite a newer owner or its Stop', async () => {
    vi.useFakeTimers()
    const storage = memoryWal()
    storage.records.set('synthetic-request', {
      schemaVersion: 2, ownerRequestId: 'synthetic-request', deliveryIdentity: 'synthetic-identity', requestSessionKey: 'synthetic-session',
      request: { kind: 'send', request: request() }, phase: 'unknown', revision: 1, createdAt: 1, updatedAt: 1,
    })
    let resolveLookup!: (value: TurnReceiptResult) => void
    const test = harness({ lookupReceipt: vi.fn(() => new Promise<TurnReceiptResult>(resolve => { resolveLookup = resolve })) }, storage)
    void test.owner.wake()
    await flush()
    const previous = storage.records.get('synthetic-request')!
    storage.records.set(previous.ownerRequestId, { ...previous, revision: previous.revision + 1,
      lease: { owner: 'new-owner', epoch: 9, expiresAt: Date.now() + 60_000 }, stop: { requested: true },
    })
    resolveLookup({ status: 'found', response: { taskId: 'late-task' } })
    await flush()
    expect(storage.records.get('synthetic-request')?.phase).toBe('unknown')
    expect(storage.records.get('synthetic-request')?.stop?.requested).toBe(true)
    expect(storage.records.get('synthetic-request')?.lease?.owner).toBe('new-owner')
    test.owner.dispose()
  })

  it('keeps a known exact Stop best effort when durable storage fails', async () => {
    const storage = memoryWal()
    storage.wal.listDeliveries = async () => { throw new Error('synthetic storage denial') }
    const test = harness({}, storage)
    const stop = { sessionKey: 'synthetic-session', taskId: 'known-task', scope: 'task' }
    await expect(test.owner.commands.cancel(stop)).resolves.toEqual({ aborted: true })
    expect(test.commands.cancel).toHaveBeenCalledWith(stop, expect.objectContaining({ expectedGeneration: 1 }))
    test.owner.dispose()
  })

  it('keeps an unknown Stop visible and cancels its exact receipt task while WAL writes fail', async () => {
    const storage = memoryWal()
    storage.records.set('synthetic-request', { schemaVersion: 2, ownerRequestId: 'synthetic-request', deliveryIdentity: 'synthetic-identity',
      requestSessionKey: 'synthetic-session', request: { kind: 'send', request: request() }, phase: 'unknown', revision: 1, createdAt: 1, updatedAt: 1 })
    const compare = storage.wal.compareAndSwapDelivery!
    storage.wal.compareAndSwapDelivery = async () => { throw new DOMException('Synthetic quota failure', 'QuotaExceededError') }
    const test = harness({ lookupReceipt: vi.fn(async () => ({ status: 'found' as const, response: { taskId: 'exact-recovered-task', sessionKey: 'forked-session' } })) }, storage)
    await expect(test.owner.requestStop('synthetic-request')).resolves.toBeUndefined()
    await test.owner.wake()
    expect(test.owner.snapshots()).toContainEqual(expect.objectContaining({ id: 'synthetic-request', waitReason: 'storage' }))
    expect(test.commands.send).not.toHaveBeenCalled()
    expect(test.commands.cancel).toHaveBeenCalledWith(expect.objectContaining({ taskId: 'exact-recovered-task', sessionKey: 'forked-session', scope: 'task' }), expect.anything())
    expect(storage.records.get('synthetic-request')?.stop).toBeUndefined()
    await test.owner.wake()
    expect(test.commands.lookupReceipt).toHaveBeenCalledTimes(1)
    expect(test.commands.cancel).toHaveBeenCalledTimes(1)
    storage.wal.compareAndSwapDelivery = compare
    await test.owner.wake()
    expect(storage.records.get('synthetic-request')?.stop).toMatchObject({ requested: true, completed: true })
    expect(test.owner.snapshots().find(item => item.id === 'synthetic-request')?.waitReason).not.toBe('storage')
    test.owner.dispose()
  })

  it('CAS-persists an unknown Stop after storage recovers before reopening its original identity', async () => {
    const storage = memoryWal()
    storage.records.set('synthetic-request', { schemaVersion: 2, ownerRequestId: 'synthetic-request', deliveryIdentity: 'synthetic-identity',
      requestSessionKey: 'synthetic-session', request: { kind: 'send', request: request() }, phase: 'unknown', revision: 1, createdAt: 1, updatedAt: 1 })
    const compare = storage.wal.compareAndSwapDelivery!
    storage.wal.compareAndSwapDelivery = async () => { throw new Error('Synthetic storage failure') }
    const first = harness({}, storage)
    first.available(false)
    await expect(first.owner.requestStop('synthetic-request')).resolves.toBeUndefined()
    expect(first.owner.snapshots()).toContainEqual(expect.objectContaining({ stopPending: true, waitReason: 'storage' }))
    storage.wal.compareAndSwapDelivery = compare
    await first.owner.wake()
    expect(storage.records.get('synthetic-request')?.stop).toMatchObject({ requested: true, completed: false })
    first.owner.dispose()
    const second = harness({ lookupReceipt: vi.fn(async () => ({ status: 'found' as const, response: { taskId: 'reopened-task' } })) }, storage)
    await second.owner.wake()
    expect(second.commands.cancel).toHaveBeenCalledWith(expect.objectContaining({ taskId: 'reopened-task', scope: 'task' }), expect.anything())
    expect(storage.records.get('synthetic-request')?.stop?.completed).toBe(true)
    second.owner.dispose()
  })

  it('fences a volatile Stop to its original identity and keeps unsupported receipt recovery read-only', async () => {
    const storage = memoryWal()
    storage.records.set('synthetic-request', { schemaVersion: 2, ownerRequestId: 'synthetic-request', deliveryIdentity: 'synthetic-identity',
      requestSessionKey: 'synthetic-session', request: { kind: 'send', request: request() }, phase: 'unknown', revision: 1, createdAt: 1, updatedAt: 1 })
    storage.wal.compareAndSwapDelivery = async () => { throw new Error('Synthetic storage failure') }
    const test = harness({ supportsReceiptLookup: () => false }, storage)
    test.available(false)
    await test.owner.requestStop('synthetic-request')
    test.available(true)
    test.identity('another-synthetic-identity')
    await test.owner.wake()
    expect(test.commands.lookupReceipt).not.toHaveBeenCalled()
    expect(test.commands.cancel).not.toHaveBeenCalled()
    test.identity('synthetic-identity')
    await test.owner.retry('synthetic-request')
    expect(test.commands.lookupReceipt).not.toHaveBeenCalled()
    expect(test.commands.send).not.toHaveBeenCalled()
    expect(test.commands.cancel).not.toHaveBeenCalled()
    expect(test.owner.snapshots()).toContainEqual(expect.objectContaining({ stopPending: true, waitReason: 'storage' }))
    test.owner.dispose()
  })

  it('contains a manual receipt recheck storage failure and keeps a visible recovery state', async () => {
    const storage = memoryWal()
    storage.records.set('synthetic-request', { schemaVersion: 2, ownerRequestId: 'synthetic-request', deliveryIdentity: 'synthetic-identity',
      requestSessionKey: 'synthetic-session', request: { kind: 'send', request: request() }, phase: 'unknown', revision: 1, createdAt: 1, updatedAt: 1 })
    storage.wal.compareAndSwapDelivery = async () => { throw new Error('Synthetic storage failure') }
    const test = harness({}, storage)
    await expect(test.owner.retry('synthetic-request')).resolves.toBeUndefined()
    expect(test.owner.snapshots()).toContainEqual(expect.objectContaining({ id: 'synthetic-request', waitReason: 'storage-check' }))
    expect(test.commands.send).not.toHaveBeenCalled()
    test.owner.dispose()
  })

  it('uses the independent Stop slot for an exact volatile task while both admissions are occupied', async () => {
    const resolves: Array<(response: TurnSendResponse) => void> = []
    const storage = memoryWal()
    const compare = storage.wal.compareAndSwapDelivery!
    storage.wal.compareAndSwapDelivery = async (id, revision, record) => {
      if (id === 'volatile-task') throw new Error('Synthetic quota failure')
      return compare(id, revision, record)
    }
    const test = harness({ send: vi.fn(() => new Promise<TurnSendResponse>(resolve => resolves.push(resolve))) }, storage)
    const admissions = [test.owner.commands.send(request('ordinary-a')), test.owner.commands.send(request('ordinary-b'))]
    await flush()
    expect(test.commands.send).toHaveBeenCalledTimes(2)
    storage.records.set('volatile-task', { schemaVersion: 2, ownerRequestId: 'volatile-task', deliveryIdentity: 'synthetic-identity',
      requestSessionKey: 'synthetic-session', phase: 'accepted', response: { taskId: 'exact-task' }, revision: 1, createdAt: 1, updatedAt: 1 })
    await test.owner.requestStop('volatile-task')
    await flush()
    expect(test.commands.cancel).toHaveBeenCalledWith(expect.objectContaining({ taskId: 'exact-task' }), expect.anything())
    expect(test.commands.lookupReceipt).not.toHaveBeenCalled()
    for (const resolve of resolves) resolve({ taskId: 'ordinary-task' })
    await Promise.all(admissions)
    test.owner.dispose()
  })

  it('allows an explicit volatile Stop to read and cancel exactly while a foreign lease survives a quota failure', async () => {
    const storage = memoryWal()
    storage.records.set('synthetic-request', { schemaVersion: 2, ownerRequestId: 'synthetic-request', deliveryIdentity: 'synthetic-identity',
      requestSessionKey: 'synthetic-session', request: { kind: 'send', request: request() }, phase: 'unknown', revision: 1, createdAt: 1, updatedAt: 1,
      lease: { owner: 'foreign-owner', epoch: 7, expiresAt: Date.now() + 60_000 } })
    storage.wal.compareAndSwapDelivery = async () => { throw new Error('Synthetic quota failure') }
    const test = harness({ lookupReceipt: vi.fn(async () => ({ status: 'found' as const, response: { taskId: 'exact-task' } })) }, storage)
    await test.owner.requestStop('synthetic-request')
    await test.owner.wake()
    expect(test.commands.send).not.toHaveBeenCalled()
    expect(test.commands.lookupReceipt).toHaveBeenCalledTimes(1)
    expect(test.commands.cancel).toHaveBeenCalledWith(expect.objectContaining({ taskId: 'exact-task', scope: 'task' }), expect.anything())
    expect(storage.records.get('synthetic-request')?.lease).toMatchObject({ owner: 'foreign-owner', epoch: 7 })
    expect(storage.records.get('synthetic-request')?.stop).toBeUndefined()
    test.owner.dispose()
  })

  it('never caches a receipt that lost its CAS before a volatile Stop sees a newer promoted task', async () => {
    vi.useFakeTimers()
    const storage = memoryWal()
    storage.records.set('synthetic-request', { schemaVersion: 2, ownerRequestId: 'synthetic-request', deliveryIdentity: 'synthetic-identity',
      requestSessionKey: 'synthetic-session', request: { kind: 'steer', request: { key: 'synthetic-session', clientRequestId: 'synthetic-request',
        clientMessageId: 'synthetic-message', expectedTurnId: 'old-task', message: 'Synthetic steer' } }, phase: 'unknown', revision: 1, createdAt: 1, updatedAt: 1 })
    let resolveLookup!: (response: TurnReceiptResult) => void
    const test = harness({ lookupReceipt: vi.fn(() => new Promise<TurnReceiptResult>(resolve => { resolveLookup = resolve })) }, storage)
    const recovering = test.owner.wake()
    await flush()
    let receiptRace = false
    storage.wal.compareAndSwapDelivery = async (id, revision, record) => {
      if (!receiptRace && record?.response?.taskId === 'old-task') {
        receiptRace = true
        const newer = { ...storage.records.get(id)!, revision: revision + 1, phase: 'accepted' as const,
          response: { accepted: true, disposition: 'promoted' as const, taskId: 'old-task', promotedTurnId: 'promoted-task' },
          lease: { owner: 'foreign-owner', epoch: 9, expiresAt: Date.now() + 60_000 } }
        storage.records.set(id, newer)
        return { applied: false, record: structuredClone(newer) }
      }
      throw new Error('Synthetic quota failure')
    }
    await test.owner.requestStop('synthetic-request')
    resolveLookup({ status: 'found', response: { accepted: true, disposition: 'steering', taskId: 'old-task' } })
    await vi.advanceTimersByTimeAsync(1_000)
    await recovering
    await test.owner.wake()
    expect(test.commands.cancel).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ taskId: 'promoted-task' }), expect.anything())
    expect(storage.records.get('synthetic-request')?.lease?.epoch).toBe(9)
    test.owner.dispose()
  })

  it.each(['cancelled', 'rejected'] as const)('does not cancel the historical task from a terminal %s Steer receipt when Stop storage fails', async disposition => {
    const storage = memoryWal()
    storage.records.set('synthetic-request', { schemaVersion: 2, ownerRequestId: 'synthetic-request', deliveryIdentity: 'synthetic-identity',
      requestSessionKey: 'synthetic-session', request: { kind: 'steer', request: { key: 'synthetic-session', clientRequestId: 'synthetic-request',
        clientMessageId: 'synthetic-message', expectedTurnId: 'old-task', message: 'Synthetic steer' } }, phase: 'unknown', revision: 1, createdAt: 1, updatedAt: 1 })
    const compare = storage.wal.compareAndSwapDelivery!
    storage.wal.compareAndSwapDelivery = async () => { throw new Error('Synthetic quota failure') }
    const test = harness({ lookupReceipt: vi.fn(async () => ({ status: 'found' as const, response: { accepted: true, disposition, taskId: 'old-task' } })) }, storage)
    await test.owner.requestStop('synthetic-request')
    await test.owner.wake()
    expect(test.commands.cancel).not.toHaveBeenCalled()
    expect(test.owner.snapshots()).toContainEqual(expect.objectContaining({ stopPending: false, waitReason: 'storage' }))
    storage.wal.compareAndSwapDelivery = compare
    await test.owner.wake()
    expect(storage.records.get('synthetic-request')?.stop?.completed).toBe(true)
    expect(test.commands.cancel).not.toHaveBeenCalled()
    test.owner.dispose()
  })

  it.each(['request', 'exact'] as const)('refreshes an inactive volatile Steer target after %s Stop instead of reusing the old persisted steering receipt', async method => {
    const storage = memoryWal()
    storage.records.set('synthetic-request', { schemaVersion: 2, ownerRequestId: 'synthetic-request', deliveryIdentity: 'synthetic-identity',
      requestSessionKey: 'synthetic-session', request: { kind: 'steer', request: { key: 'synthetic-session', clientRequestId: 'synthetic-request',
        clientMessageId: 'synthetic-message', expectedTurnId: 'old-task', message: 'Synthetic steer' } }, phase: 'accepted', revision: 1, createdAt: 1, updatedAt: 1,
      response: { accepted: true, disposition: 'steering', taskId: 'old-task' } })
    storage.wal.compareAndSwapDelivery = async () => { throw new Error('Synthetic quota failure') }
    const test = harness({ lookupReceipt: vi.fn(async () => ({ status: 'found' as const, response: { accepted: true, disposition: 'promoted' as const, taskId: 'old-task', promotedTurnId: 'promoted-task' } })),
      cancel: vi.fn().mockResolvedValueOnce({ aborted: false, reason: 'task_not_active' }).mockResolvedValue({ aborted: true }) }, storage)
    if (method === 'request') await test.owner.requestStop('synthetic-request')
    else await test.owner.commands.cancel({ sessionKey: 'synthetic-session', taskId: 'old-task', scope: 'task' })
    await test.owner.wake()
    expect(test.commands.lookupReceipt).not.toHaveBeenCalled()
    expect(test.commands.cancel).toHaveBeenCalledTimes(1)
    await test.owner.retry('synthetic-request')
    expect(test.commands.lookupReceipt).toHaveBeenCalledTimes(1)
    expect(vi.mocked(test.commands.cancel).mock.calls.map(call => call[0].taskId)).toEqual(['old-task', 'promoted-task'])
    expect(test.owner.snapshots()).toContainEqual(expect.objectContaining({ stopPending: false, waitReason: 'storage' }))
    test.owner.dispose()
  })

  it('rereads a queued admission after another tab has made its acceptance unknown', async () => {
    const resolves: Array<(response: TurnSendResponse) => void> = []
    const test = harness({
      send: vi.fn(() => new Promise<TurnSendResponse>(resolve => resolves.push(resolve))),
      lookupReceipt: vi.fn(async () => ({ status: 'found' as const, response: { taskId: 'other-tab-task' } })),
    })
    const a = test.owner.commands.send(request('a'))
    const b = test.owner.commands.send(request('b'))
    const c = test.owner.commands.send(request('c'))
    await flush()
    const queued = test.records.get('c')!
    test.records.set('c', { ...queued, phase: 'unknown', revision: queued.revision + 1,
      lease: { owner: 'other-tab', epoch: 3, expiresAt: 0 } })
    resolves.shift()!({ taskId: 'a-task' })
    await flush()
    expect(test.commands.send).toHaveBeenCalledTimes(2)
    await expect(c).resolves.toMatchObject({ taskId: 'other-tab-task' })
    expect(test.commands.lookupReceipt).toHaveBeenCalledTimes(1)
    resolves.shift()!({ taskId: 'b-task' })
    await Promise.all([a, b])
    test.owner.dispose()
  })

  it('waits for a crashed tab lease without consuming the new owner recovery round', async () => {
    vi.useFakeTimers()
    const storage = memoryWal()
    storage.records.set('synthetic-request', { schemaVersion: 2, ownerRequestId: 'synthetic-request', deliveryIdentity: 'synthetic-identity',
      requestSessionKey: 'synthetic-session', request: { kind: 'send', request: request() }, phase: 'unknown', revision: 1, createdAt: 1, updatedAt: 1,
      lease: { owner: 'crashed-tab', epoch: 1, expiresAt: Date.now() + 60_000 } })
    const test = harness({ lookupReceipt: vi.fn(async () => ({ status: 'found' as const, response: { taskId: 'recovered-task' } })) }, storage)
    await test.owner.wake()
    expect(test.commands.lookupReceipt).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(60_025)
    expect(test.commands.lookupReceipt).toHaveBeenCalledTimes(1)
    expect(storage.records.get('synthetic-request')?.phase).toBe('accepted')
    test.owner.dispose()
  })

  it('fences a late receipt against a newer epoch held by this same owner', async () => {
    vi.useFakeTimers()
    const storage = memoryWal()
    storage.records.set('synthetic-request', { schemaVersion: 2, ownerRequestId: 'synthetic-request', deliveryIdentity: 'synthetic-identity',
      requestSessionKey: 'synthetic-session', request: { kind: 'send', request: request() }, phase: 'unknown', revision: 1, createdAt: 1, updatedAt: 1 })
    let first!: (result: TurnReceiptResult) => void
    const lookupReceipt = vi.fn().mockImplementationOnce(() => new Promise<TurnReceiptResult>(resolve => { first = resolve }))
      .mockResolvedValue({ status: 'found', response: { taskId: 'new-epoch-task' } })
    const test = harness({ lookupReceipt }, storage)
    const firstRound = test.owner.wake()
    await flush()
    const old = storage.records.get('synthetic-request')!
    storage.records.set(old.ownerRequestId, { ...old, revision: old.revision + 1, lease: { ...old.lease!, expiresAt: 0 } })
    await test.owner.retry(old.ownerRequestId)
    first({ status: 'found', response: { taskId: 'stale-task' } })
    await vi.advanceTimersByTimeAsync(1000)
    await firstRound
    expect(storage.records.get(old.ownerRequestId)?.response?.taskId).toBe('new-epoch-task')
    test.owner.dispose()
  })

  it('resumes a persisted Steer Stop against its promoted task', async () => {
    vi.useFakeTimers()
    const storage = memoryWal()
    storage.records.set('synthetic-steer', { schemaVersion: 2, ownerRequestId: 'synthetic-steer', deliveryIdentity: 'synthetic-identity', requestSessionKey: 'synthetic-session',
      request: { kind: 'steer', request: { key: 'synthetic-session', clientRequestId: 'synthetic-steer', clientMessageId: 'synthetic-message', expectedTurnId: 'old-task', message: 'synthetic steer' } },
      phase: 'unknown', stop: { requested: true }, revision: 1, createdAt: 1, updatedAt: 1 })
    const test = harness({ lookupReceipt: vi.fn(async () => ({ status: 'found' as const, response: {
      accepted: true, disposition: 'promoted' as const, taskId: 'old-task', turnId: 'old-task', promotedTurnId: 'promoted-task',
    } })) }, storage)
    const recovering = test.owner.wake()
    await vi.advanceTimersByTimeAsync(6000)
    await recovering
    expect(test.commands.cancel).toHaveBeenCalledWith(expect.objectContaining({ taskId: 'promoted-task', scope: 'task' }), expect.anything())
    expect(storage.records.get('synthetic-steer')?.stop?.completed).toBe(true)
    test.owner.dispose()
  })

  it('pauses authority failures until a request-specific explicit retry', async () => {
    const storage = memoryWal()
    storage.records.set('synthetic-request', { schemaVersion: 2, ownerRequestId: 'synthetic-request', deliveryIdentity: 'synthetic-identity', requestSessionKey: 'synthetic-session',
      request: { kind: 'send', request: request() }, phase: 'unknown', revision: 1, createdAt: 1, updatedAt: 1 })
    const test = harness({ lookupReceipt: vi.fn(async () => { throw new TurnCommandError('rejected', 'synthetic denied', 'FORBIDDEN', false) }) }, storage)
    await test.owner.wake()
    await test.owner.wake()
    test.identity('synthetic-identity')
    await test.owner.wake()
    expect(test.commands.lookupReceipt).toHaveBeenCalledTimes(1)
    expect(test.owner.snapshots()[0]?.waitReason).toBe('permission')
    await test.owner.retry('synthetic-request')
    expect(test.commands.lookupReceipt).toHaveBeenCalledTimes(2)
    test.owner.dispose()
  })

  it('rechecks a Steer receipt after the old task ends and stops its newly promoted task within four calls', async () => {
    vi.useFakeTimers()
    const storage = memoryWal()
    storage.records.set('synthetic-steer', { schemaVersion: 2, ownerRequestId: 'synthetic-steer', deliveryIdentity: 'synthetic-identity', requestSessionKey: 'synthetic-session',
      request: { kind: 'steer', request: { key: 'synthetic-session', clientRequestId: 'synthetic-steer', clientMessageId: 'synthetic-message', expectedTurnId: 'old-task', message: 'synthetic steer' } },
      phase: 'unknown', stop: { requested: true }, revision: 1, createdAt: 1, updatedAt: 1 })
    const test = harness({
      lookupReceipt: vi.fn().mockResolvedValueOnce({ status: 'found', response: { accepted: true, disposition: 'steering', taskId: 'old-task' } })
        .mockResolvedValue({ status: 'found', response: { accepted: true, disposition: 'promoted', taskId: 'old-task', promotedTurnId: 'promoted-task' } }),
      cancel: vi.fn().mockResolvedValueOnce({ aborted: false, reason: 'task_not_active' }).mockResolvedValue({ aborted: true }),
    }, storage)
    const recovering = test.owner.wake()
    await vi.advanceTimersByTimeAsync(6_000)
    await recovering
    expect(test.commands.lookupReceipt).toHaveBeenCalledTimes(2)
    expect(test.commands.cancel).toHaveBeenCalledTimes(2)
    expect(vi.mocked(test.commands.cancel).mock.calls.map(call => call[0].taskId)).toEqual(['old-task', 'promoted-task'])
    expect(storage.records.get('synthetic-steer')?.stop?.completed).toBe(true)
    test.owner.dispose()
  })

  it('keeps a pending Steer Stop until a new authoritative disposition wakes a deduplicated receipt round', async () => {
    vi.useFakeTimers()
    const storage = memoryWal()
    storage.records.set('synthetic-steer', { schemaVersion: 2, ownerRequestId: 'synthetic-steer', deliveryIdentity: 'synthetic-identity', requestSessionKey: 'synthetic-session',
      request: { kind: 'steer', request: { key: 'synthetic-session', clientRequestId: 'synthetic-steer', clientMessageId: 'synthetic-message', expectedTurnId: 'old-task', message: 'synthetic steer' } },
      phase: 'unknown', stop: { requested: true }, revision: 1, createdAt: 1, updatedAt: 1 })
    const lookupReceipt = vi.fn().mockResolvedValue({ status: 'found', response: { accepted: true, disposition: 'steering', taskId: 'old-task' } })
    const cancel = vi.fn().mockResolvedValue({ aborted: false, reason: 'task_not_active' })
    const test = harness({ lookupReceipt, cancel }, storage)
    const recovering = test.owner.wake()
    await vi.advanceTimersByTimeAsync(6_000)
    await recovering
    expect(lookupReceipt).toHaveBeenCalledTimes(2)
    expect(cancel).toHaveBeenCalledTimes(2)
    expect(storage.records.get('synthetic-steer')?.stop?.completed).not.toBe(true)
    await test.owner.wake()
    expect(lookupReceipt).toHaveBeenCalledTimes(2)
    lookupReceipt.mockResolvedValue({ status: 'found', response: { accepted: true, disposition: 'promoted', taskId: 'old-task', promotedTurnId: 'promoted-task' } })
    cancel.mockResolvedValue({ aborted: true })
    const hint = test.owner.noteReceiptChanged('synthetic-steer', '2:promoted-task:promoted')
    await vi.advanceTimersByTimeAsync(6_000)
    await hint
    await test.owner.noteReceiptChanged('synthetic-steer', '2:promoted-task:promoted')
    expect(lookupReceipt).toHaveBeenCalledTimes(3)
    expect(cancel).toHaveBeenLastCalledWith(expect.objectContaining({ taskId: 'promoted-task' }), expect.anything())
    expect(storage.records.get('synthetic-steer')?.stop?.completed).toBe(true)
    test.owner.dispose()
  })

  it('does not clone attachment payloads or notify observers on lease renewal', async () => {
    vi.useFakeTimers()
    const test = harness({ send: vi.fn(() => new Promise<TurnSendResponse>(() => {})) })
    const observed = vi.fn()
    test.owner.observe(observed)
    void test.owner.commands.send(request())
    await flush()
    const notifications = observed.mock.calls.length
    await vi.advanceTimersByTimeAsync(20_000)
    expect(observed).toHaveBeenCalledTimes(notifications)
    expect(observed.mock.calls[0]?.[0]).not.toHaveProperty('request')
    test.owner.dispose()
  })
})


describe('settled delivery notifications', () => {
  it('clears the offline wait reason when the saved exact Stop succeeds after reconnect', async () => {
    const test = harness()
    try {
      await test.owner.commands.send(request())
      test.available(false)
      await test.owner.requestStop('synthetic-request')
      await test.owner.wake()
      expect(test.owner.snapshots()[0]).toMatchObject({ stopPending: true, waitReason: 'offline' })
      test.available(true)
      await test.owner.wake()
      await vi.waitFor(() => expect(test.records.get('synthetic-request')?.stop?.completed).toBe(true))
      expect(test.owner.snapshots()[0]).toMatchObject({ stopPending: false })
      expect(test.owner.snapshots()[0]?.waitReason).toBeUndefined()
    } finally { test.owner.dispose() }
  })
  it('observes another window settling a leased delivery on its next wake', async () => {
    const storage = memoryWal()
    storage.records.set('synthetic-request', { schemaVersion: 2, ownerRequestId: 'synthetic-request',
      deliveryIdentity: 'synthetic-identity', requestSessionKey: 'synthetic-session', phase: 'unknown',
      request: { kind: 'send', request: request() }, revision: 1, createdAt: 1, updatedAt: 1 })
    let settle!: (value: TurnReceiptResult) => void
    const a = harness({ lookupReceipt: vi.fn(() => new Promise<TurnReceiptResult>(resolve => { settle = resolve })) }, storage)
    const b = harness({}, storage)
    const observer = vi.fn()
    b.owner.observe(observer)
    try {
      const recovering = a.owner.wake()
      await flush()
      await b.owner.wake()
      expect(b.owner.snapshots()[0]).toMatchObject({ phase: 'unknown', waitReason: 'lease' })
      settle({ status: 'found', response: { taskId: 'synthetic-task', sessionKey: 'synthetic-session' } })
      await recovering
      expect((await b.owner.get('synthetic-request'))?.phase).toBe('accepted')
      await b.owner.wake()
      expect(b.owner.snapshots()[0]?.phase).toBe('accepted')
      expect(b.owner.snapshots()[0]?.waitReason).toBeUndefined()
      expect(observer).toHaveBeenLastCalledWith(expect.objectContaining({ phase: 'accepted' }))
      expect(b.commands.lookupReceipt).not.toHaveBeenCalled()
      expect(b.commands.send).not.toHaveBeenCalled()
      expect(b.commands.cancel).not.toHaveBeenCalled()
      const reads = vi.spyOn(storage.wal, 'getDelivery')
      await b.owner.wake()
      expect(reads).not.toHaveBeenCalled()
    } finally { a.owner.dispose(); b.owner.dispose() }
  })
})
