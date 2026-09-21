import { afterEach, describe, expect, it, vi } from 'vitest'
import { reactive } from 'vue'
import { createDurableDelivery } from './durableDelivery'
import { TurnCommandError } from '@/modules/turnCommands'
import type { TurnCommands, TurnSendRequest, TurnReceiptResult, TurnSendResponse } from '@/modules/turnCommands'
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

function request(id = 'synthetic-request'): TurnSendRequest {
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
