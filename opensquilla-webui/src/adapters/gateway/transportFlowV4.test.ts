import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { TransportFlowV4 } from './transportFlowV4'
import type { TransportCallOptions, TransportEventHandler } from './transportTypes'

const EPOCH = 'delivery-epoch-1'
const METHOD = 'transport.flow.update'

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

function reply(params: Record<string, unknown>) {
  return {
    delivery_epoch: params.delivery_epoch, ack_delivery_id: params.ack_delivery_id,
    dirty_keys: [], global_dirty: false,
  }
}

const owners: TransportFlowV4[] = []

function harness() {
  let generation = 7
  const listeners = new Map<string, Set<TransportEventHandler>>()
  const enable = vi.fn()
  const consume = vi.fn(async (_event: string, _payload: unknown, _meta: Record<string, unknown>): Promise<'applied' | 'dirty'> => 'applied')
  const recover = vi.fn(async (_detail: unknown): Promise<boolean> => true)
  const request = vi.fn(async (_method: string, params: Record<string, unknown> = {}, _options?: TransportCallOptions): Promise<unknown> => reply(params))
  const controller = new TransportFlowV4({
    get connectionGeneration() { return generation },
    request: <T = unknown>(method: string, params?: Record<string, unknown>, options?: TransportCallOptions) => request(method, params, options) as Promise<T>,
    enableConsumptionFlow: enable,
    consumeEvent: consume,
    recoverGap: recover,
    on(event, handler) {
      let handlers = listeners.get(event)
      if (!handlers) { handlers = new Set(); listeners.set(event, handlers) }
      handlers.add(handler)
      return () => { handlers!.delete(handler) }
    },
  })
  owners.push(controller)
  function emit(event: string, ...args: unknown[]) {
    for (const handler of listeners.get(event) ?? []) handler(...args)
  }
  function hello(epoch = EPOCH) {
    emit('_hello', { policy: { transport_flow: { delivery_epoch: epoch, window_frames: 128, window_bytes: 4 * 1024 * 1024 } } })
    emit('_state', 'connected')
  }
  function deliver(id: number, key = 'alpha', epoch = EPOCH) {
    emit('*', 'session.event.text_delta', { session_key: key, text_delta: 'x' }, {
      flow: { delivery_epoch: epoch, delivery_id: id },
    })
  }
  function dirty(keys = ['alpha']) {
    emit('*', 'transport.flow.dirty', { delivery_epoch: EPOCH, dirty_keys: keys, global_dirty: false }, {})
  }
  return {
    controller, enable, consume, recover, request, emit, hello, deliver, dirty,
    replaceGeneration() { generation++ },
    receipt(id: number, epoch = EPOCH) { return { delivery_epoch: epoch, delivery_id: id } },
  }
}

beforeEach(() => { vi.useFakeTimers() })
afterEach(() => {
  for (const owner of owners.splice(0)) owner.close()
  vi.clearAllTimers()
  vi.useRealTimers()
})

describe('connection-local consumption flow', () => {
  it('exposes immutable on-demand queue counts without payloads or session identities', async () => {
    const h = harness()
    const recovery = deferred<boolean>()
    h.consume.mockRejectedValueOnce(new Error('No owner.'))
    h.recover.mockReturnValueOnce(recovery.promise)
    h.hello()
    h.deliver(1, 'PRIVATE_SESSION_KEY')
    await vi.advanceTimersByTimeAsync(0)
    const snapshot = h.controller.diagnostics
    expect(Object.isFrozen(snapshot)).toBe(true)
    expect(snapshot).toMatchObject({
      enabled: true, pendingFrames: 1, unownedFrames: 1, ackDeliveryId: 0,
      acknowledgedDeliveryId: 0, stagedFrames: 0, pendingInstallations: 0,
      recoveryInFlight: true, controlInFlight: false,
    })
    expect(Object.values(snapshot).every(value => typeof value === 'boolean' || typeof value === 'number')).toBe(true)
    expect(JSON.stringify(snapshot)).not.toContain('PRIVATE_SESSION_KEY')
    expect(JSON.stringify(snapshot)).not.toContain(EPOCH)
    h.controller.reset()
    expect(h.controller.diagnostics).toMatchObject({ enabled: false, pendingFrames: 0, unownedFrames: 0, recoveryInFlight: false })
    expect(snapshot.pendingFrames).toBe(1)
  })

  it('advertises capability but waits for a validated negotiated policy', async () => {
    const h = harness()
    expect(h.enable).toHaveBeenCalledOnce()
    h.deliver(1)
    h.emit('_hello', { policy: { transport_flow: { delivery_epoch: EPOCH, window_frames: 999, window_bytes: 4194304 } } })
    h.deliver(1)
    await vi.advanceTimersByTimeAsync(50)
    expect(h.controller.enabled).toBe(false)
    expect(h.consume).not.toHaveBeenCalled()
    expect(h.request).not.toHaveBeenCalled()
    h.hello()
    expect(h.controller.enabled).toBe(true)
  })

  it('ACKs only after the real consumer resolves applied', async () => {
    const h = harness()
    const consumed = deferred<'applied'>()
    h.consume.mockReturnValueOnce(consumed.promise)
    h.hello()
    h.deliver(1)
    await vi.advanceTimersByTimeAsync(100)
    expect(h.request).not.toHaveBeenCalled()
    consumed.resolve('applied')
    await vi.advanceTimersByTimeAsync(50)
    expect(h.request).toHaveBeenCalledExactlyOnceWith(METHOD, {
      delivery_epoch: EPOCH, ack_delivery_id: 1,
    }, { expectedGeneration: 7, timeoutMs: 10000, timeoutAction: 'reject', abortAction: 'reject' })
  })

  it('releases explicit dirty ownership without waiting for snapshot installation', async () => {
    const h = harness()
    const recovery = deferred<boolean>()
    h.consume.mockResolvedValueOnce('dirty')
    h.recover.mockReturnValueOnce(recovery.promise)
    h.hello()
    h.deliver(1)
    await vi.advanceTimersByTimeAsync(50)
    expect(h.recover).toHaveBeenCalledWith({ reason: 'transport_flow_dirty', keys: ['alpha'], global: false })
    expect(h.request).toHaveBeenCalledWith(METHOD, {
      delivery_epoch: EPOCH, ack_delivery_id: 1, dirty_keys: ['alpha'],
    }, expect.anything())
  })

  it('observes a stalled consumer without ACK until recovery explicitly owns it', async () => {
    const h = harness()
    const late = deferred<'applied'>()
    h.consume.mockReturnValueOnce(late.promise)
    h.recover.mockResolvedValue(false)
    h.hello()
    h.deliver(1)
    await vi.advanceTimersByTimeAsync(99)
    expect(h.recover).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(1)
    expect(h.recover).toHaveBeenCalledOnce()
    expect(h.request).not.toHaveBeenCalled()
    // Once recovery has invalidated the consumer, its late completion cannot
    // claim correctness for the replacement synchronization revision.
    late.resolve('applied')
    await vi.advanceTimersByTimeAsync(50)
    expect(h.request).not.toHaveBeenCalled()
    h.recover.mockResolvedValue(true)
    await vi.advanceTimersByTimeAsync(1000)
    expect(h.request).toHaveBeenCalledWith(METHOD, {
      delivery_epoch: EPOCH, ack_delivery_id: 1,
    }, expect.anything())
  })

  it('does not ACK absent or failed consumers while recovery has not succeeded', async () => {
    const h = harness()
    h.consume.mockRejectedValue(new Error('No event consumption owner.'))
    h.recover.mockResolvedValue(false)
    h.hello()
    h.deliver(1)
    await vi.advanceTimersByTimeAsync(2050)
    expect(h.recover).toHaveBeenCalledTimes(3)
    expect(h.request).not.toHaveBeenCalled()
  })

  it('retires an unowned delivery after a later successful automatic recovery', async () => {
    const h = harness()
    h.consume.mockRejectedValueOnce(new Error('Owner has not mounted yet.'))
    h.recover.mockResolvedValueOnce(false).mockResolvedValueOnce(true)
    h.hello()
    h.deliver(1)
    await vi.advanceTimersByTimeAsync(50)
    expect(h.request).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(1050)
    expect(h.recover).toHaveBeenCalledTimes(2)
    expect(h.request).toHaveBeenCalledWith(METHOD, {
      delivery_epoch: EPOCH, ack_delivery_id: 1,
    }, expect.anything())
  })

  it('never treats recovery of alpha as ownership of a later beta delivery', async () => {
    const h = harness()
    const alpha = deferred<boolean>()
    const beta = deferred<boolean>()
    h.consume.mockRejectedValue(new Error('Missing domain owner.'))
    h.recover.mockReturnValueOnce(alpha.promise).mockReturnValueOnce(beta.promise)
    h.hello()
    h.deliver(1, 'alpha')
    await vi.advanceTimersByTimeAsync(0)
    h.deliver(2, 'beta')
    await vi.advanceTimersByTimeAsync(0)
    expect(h.recover).toHaveBeenCalledOnce()
    alpha.resolve(true)
    await vi.advanceTimersByTimeAsync(50)
    expect(h.request.mock.calls.every(([, params]) => Number(params?.ack_delivery_id) <= 1)).toBe(true)
    await vi.advanceTimersByTimeAsync(1000)
    expect(h.recover).toHaveBeenCalledTimes(2)
    beta.resolve(true)
    await vi.advanceTimersByTimeAsync(50)
    expect(h.request).toHaveBeenLastCalledWith(METHOD, {
      delivery_epoch: EPOCH, ack_delivery_id: 2,
    }, expect.anything())
  })

  it('does not include a previously failed alpha receipt in an unrelated beta-only recovery', async () => {
    const h = harness()
    const coveringRecovery = deferred<boolean>()
    h.consume.mockRejectedValue(new Error('Missing domain owner.'))
    h.recover.mockResolvedValueOnce(false).mockImplementation(detail => {
      return (detail as { keys: string[] }).keys.includes('alpha') ? coveringRecovery.promise : Promise.resolve(true)
    })
    h.hello()
    h.deliver(1, 'alpha')
    await vi.advanceTimersByTimeAsync(0)
    h.deliver(2, 'beta')
    await vi.advanceTimersByTimeAsync(50)
    expect(h.request.mock.calls.every(([, params]) => Number(params?.ack_delivery_id) === 0)).toBe(true)
    await vi.advanceTimersByTimeAsync(1000)
    expect(h.recover).toHaveBeenCalledWith({ reason: 'transport_flow_dirty', keys: ['alpha', 'beta'], global: false })
    coveringRecovery.resolve(true)
    await vi.advanceTimersByTimeAsync(50)
    expect(h.request).toHaveBeenLastCalledWith(METHOD, {
      delivery_epoch: EPOCH, ack_delivery_id: 2,
    }, expect.anything())
  })

  it('keeps the cumulative ACK behind an unfinished predecessor', async () => {
    const h = harness()
    const first = deferred<'applied'>()
    h.consume.mockReturnValueOnce(first.promise)
    h.hello()
    h.deliver(1)
    h.deliver(2)
    await vi.advanceTimersByTimeAsync(50)
    expect(h.request).not.toHaveBeenCalled()
    first.resolve('applied')
    await vi.advanceTimersByTimeAsync(50)
    expect(h.request).toHaveBeenCalledWith(METHOD, {
      delivery_epoch: EPOCH, ack_delivery_id: 2,
    }, expect.anything())
  })

  it('can confirm bounded snapshot staging beyond one ordinary window without resuming the domain', async () => {
    const h = harness()
    h.hello()
    for (let id = 1; id <= 134; id++) {
      await h.controller.acknowledgeDelivery(h.receipt(id))
      // The adapter has validated and copied this segment into bounded staging.
      // Installation of the complete snapshot is a separate operation.
      await vi.advanceTimersByTimeAsync(50)
    }
    expect(h.request.mock.calls.every(([, params]) => !params?.resume)).toBe(true)
    expect(h.request).toHaveBeenLastCalledWith(METHOD, {
      delivery_epoch: EPOCH, ack_delivery_id: 134, staged_delivery_ids: [134],
    }, expect.anything())
    await h.controller.resumeFlow({ key: 'alpha', snapshot_id: 'snapshot-1', sync_revision: 'sync-1', stream_generation: 'stream-1', stream_seq: 42 })
    expect(h.request).toHaveBeenLastCalledWith(METHOD, {
      delivery_epoch: EPOCH, ack_delivery_id: 134,
      resume: [{ key: 'alpha', snapshot_id: 'snapshot-1', sync_revision: 'sync-1', stream_generation: 'stream-1', stream_seq: 42 }],
    }, expect.anything())
  })

  it('returns recovery-piece credit across 134 segments without ACKing an unowned predecessor', async () => {
    const h = harness()
    const recovery = deferred<boolean>()
    h.consume.mockRejectedValueOnce(new Error('No domain owner.'))
    h.recover.mockReturnValueOnce(recovery.promise)
    h.hello()
    h.deliver(1)
    await vi.advanceTimersByTimeAsync(0)
    for (let id = 2; id <= 135; id++) {
      // A recovery RPC segment is already validated and copied into staging.
      // It must return its separate recovery credit despite the ordinary gap.
      await h.controller.acknowledgeDelivery(h.receipt(id))
      await vi.advanceTimersByTimeAsync(50)
      expect(h.request).toHaveBeenLastCalledWith(METHOD, {
        delivery_epoch: EPOCH, ack_delivery_id: 0, staged_delivery_ids: [id],
      }, expect.anything())
    }
    expect(h.request).toHaveBeenCalledTimes(134)
    expect(h.request.mock.calls.every(([, params]) => !params?.resume)).toBe(true)
    recovery.resolve(true)
    await vi.advanceTimersByTimeAsync(50)
    expect(h.request).toHaveBeenLastCalledWith(METHOD, {
      delivery_epoch: EPOCH, ack_delivery_id: 135,
    }, expect.anything())
  })

  it('waits for the exact resume reply even behind an earlier control request', async () => {
    const h = harness()
    const first = deferred<unknown>()
    const install = deferred<unknown>()
    h.request.mockReturnValueOnce(first.promise).mockReturnValueOnce(install.promise)
    h.hello()
    h.deliver(1)
    await vi.advanceTimersByTimeAsync(50)
    let installed = false
    const receipt = { key: 'alpha', snapshot_id: 'snapshot-1', sync_revision: 'sync-1', stream_generation: 'stream-1', stream_seq: 42 }
    const done = h.controller.resumeFlow(receipt).then(() => { installed = true })
    await vi.advanceTimersByTimeAsync(50)
    expect(installed).toBe(false)
    first.resolve(reply({ delivery_epoch: EPOCH, ack_delivery_id: 1 }))
    await vi.advanceTimersByTimeAsync(50)
    expect(h.request).toHaveBeenCalledTimes(2)
    expect(installed).toBe(false)
    install.resolve(reply({ delivery_epoch: EPOCH, ack_delivery_id: 1 }))
    await done
    expect(installed).toBe(true)
  })

  it('confirms multiple queued snapshot installations one key and one server reply at a time', async () => {
    const h = harness()
    const responses = [deferred<unknown>(), deferred<unknown>(), deferred<unknown>()]
    for (const response of responses) h.request.mockReturnValueOnce(response.promise)
    h.hello()
    const settled: string[] = []
    const receipts = ['alpha', 'beta', 'gamma'].map(key => ({
      key, snapshot_id: `snapshot-${key}`, sync_revision: `sync-${key}`, stream_generation: 'stream-1', stream_seq: 42,
    }))
    const confirmations = receipts.map(receipt => h.controller.resumeFlow(receipt).then(() => { settled.push(receipt.key) }))
    expect(h.request).toHaveBeenCalledOnce()
    expect(settled).toEqual([])
    for (let index = 0; index < receipts.length; index++) {
      expect(h.request).toHaveBeenCalledTimes(index + 1)
      const params = h.request.mock.calls[index][1]!
      expect(params.resume).toEqual([receipts[index]])
      responses[index].resolve(reply(params))
      await confirmations[index]
      expect(settled).toEqual(receipts.slice(0, index + 1).map(receipt => receipt.key))
      await vi.advanceTimersByTimeAsync(50)
    }
    expect(h.request).toHaveBeenCalledTimes(3)
  })

  it('does not report installation during a lost resume reply or a dirty reply', async () => {
    const h = harness()
    h.request.mockRejectedValueOnce(new Error('Reply lost'))
    h.hello()
    const receipt = { key: 'alpha', snapshot_id: 'snapshot-1', sync_revision: 'sync-1', stream_generation: 'stream-1', stream_seq: 42 }
    let installed = false
    const done = h.controller.resumeFlow(receipt).then(() => { installed = true })
    await vi.advanceTimersByTimeAsync(999)
    expect(installed).toBe(false)
    await vi.advanceTimersByTimeAsync(1)
    await done
    expect(installed).toBe(true)
    expect(h.request.mock.calls[1]).toEqual(h.request.mock.calls[0])
    h.request.mockImplementationOnce(async (_method, params) => ({ ...reply(params!), dirty_keys: ['alpha'] }))
    const dirty = h.controller.resumeFlow({ ...receipt, sync_revision: 'sync-2' })
    await expect(dirty).rejects.toThrow('requires reconciliation')
  })

  it('rejects a pending installation when the connection is replaced', async () => {
    const h = harness()
    const late = deferred<unknown>()
    h.request.mockReturnValueOnce(late.promise)
    h.hello()
    const done = h.controller.resumeFlow({ key: 'alpha', snapshot_id: 'snapshot-1', sync_revision: 'sync-1', stream_generation: 'stream-1', stream_seq: 42 })
    const error = done.catch(value => value)
    h.controller.reset()
    expect(await error).toBeInstanceOf(Error)
    late.resolve(reply({ delivery_epoch: EPOCH, ack_delivery_id: 0 }))
    await vi.advanceTimersByTimeAsync(1000)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('awaits the server credit update before allowing the snapshot reader to continue', async () => {
    const h = harness()
    const result = deferred<unknown>()
    h.request.mockReturnValueOnce(result.promise)
    h.hello()
    let confirmed = false
    const confirmation = h.controller.acknowledgeDelivery(h.receipt(1)).then(() => { confirmed = true })
    await vi.advanceTimersByTimeAsync(100)
    expect(confirmed).toBe(false)
    expect(h.request).toHaveBeenCalledOnce()
    result.resolve(reply({ delivery_epoch: EPOCH, ack_delivery_id: 1 }))
    await confirmation
    expect(confirmed).toBe(true)
  })

  it('retries the same staged credit update without replaying the snapshot read', async () => {
    const h = harness()
    h.request.mockRejectedValueOnce(new Error('Control reply lost.'))
    h.hello()
    const confirmation = h.controller.acknowledgeDelivery(h.receipt(1))
    await vi.advanceTimersByTimeAsync(1050)
    await confirmation
    expect(h.request).toHaveBeenCalledTimes(2)
    expect(h.request.mock.calls[1]).toEqual(h.request.mock.calls[0])
    expect(h.request.mock.calls[0]?.[1]).toEqual({
      delivery_epoch: EPOCH, ack_delivery_id: 1, staged_delivery_ids: [1],
    })
    expect(h.request.mock.calls.every(([method]) => method === METHOD)).toBe(true)
  })

  it('queues a late discarded segment behind a pending staging reply', async () => {
    const h = harness()
    const first = deferred<unknown>()
    h.request.mockReturnValueOnce(first.promise)
    h.hello()
    const one = h.controller.acknowledgeDelivery(h.receipt(1))
    expect(h.controller.acknowledgeDelivery(h.receipt(1))).toBe(one)
    const two = h.controller.acknowledgeDelivery(h.receipt(2))
    await vi.advanceTimersByTimeAsync(50)
    expect(h.request).toHaveBeenCalledOnce()
    first.resolve(reply({ delivery_epoch: EPOCH, ack_delivery_id: 1 }))
    await vi.advanceTimersByTimeAsync(50)
    await Promise.all([one, two])
    expect(h.request).toHaveBeenLastCalledWith(METHOD, {
      delivery_epoch: EPOCH, ack_delivery_id: 2, staged_delivery_ids: [2],
    }, expect.anything())
  })

  it('rejects an old staging waiter on reset and ignores its late control reply', async () => {
    const h = harness()
    const result = deferred<unknown>()
    h.request.mockReturnValueOnce(result.promise)
    h.hello()
    const outcome = h.controller.acknowledgeDelivery(h.receipt(1)).then(() => null, error => error)
    h.controller.reset()
    expect(await outcome).toBeInstanceOf(Error)
    result.resolve(reply({ delivery_epoch: EPOCH, ack_delivery_id: 1 }))
    await vi.advanceTimersByTimeAsync(10000)
    expect(h.request).toHaveBeenCalledOnce()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('ignores duplicate and obsolete epoch receipts', async () => {
    const h = harness()
    h.hello()
    h.deliver(1, 'alpha', 'old-epoch')
    h.deliver(1)
    h.deliver(1)
    await h.controller.acknowledgeDelivery(h.receipt(2, 'old-epoch'))
    await vi.advanceTimersByTimeAsync(50)
    expect(h.consume).toHaveBeenCalledOnce()
    expect(h.request).toHaveBeenCalledOnce()
    h.deliver(1)
    await vi.advanceTimersByTimeAsync(50)
    expect(h.request).toHaveBeenCalledOnce()
  })

  it('fences late consumer completion after a new Hello', async () => {
    const h = harness()
    const oldConsumer = deferred<'applied'>()
    h.consume.mockReturnValueOnce(oldConsumer.promise)
    h.hello()
    h.deliver(1)
    h.replaceGeneration()
    h.hello('new-epoch')
    oldConsumer.resolve('applied')
    await vi.advanceTimersByTimeAsync(50)
    expect(h.request).not.toHaveBeenCalled()
    h.deliver(1, 'alpha', 'new-epoch')
    await vi.advanceTimersByTimeAsync(50)
    expect(h.request).toHaveBeenCalledWith(METHOD, {
      delivery_epoch: 'new-epoch', ack_delivery_id: 1,
    }, expect.objectContaining({ expectedGeneration: 8 }))
  })

  it('fences late consumers even before replacement state has been emitted', async () => {
    const h = harness()
    const oldConsumer = deferred<'applied'>()
    h.consume.mockReturnValueOnce(oldConsumer.promise)
    h.hello()
    h.deliver(1)
    h.replaceGeneration()
    oldConsumer.resolve('applied')
    await vi.advanceTimersByTimeAsync(100)
    expect(h.request).not.toHaveBeenCalled()
  })

  it('retries only an idempotent credit update after a request-local failure', async () => {
    const h = harness()
    h.request.mockRejectedValueOnce(new Error('RPC timeout'))
    h.hello()
    h.deliver(1)
    await vi.advanceTimersByTimeAsync(50)
    expect(h.request).toHaveBeenCalledOnce()
    await vi.advanceTimersByTimeAsync(999)
    expect(h.request).toHaveBeenCalledOnce()
    await vi.advanceTimersByTimeAsync(1)
    expect(h.request).toHaveBeenCalledTimes(2)
    expect(h.request.mock.calls[1]).toEqual(h.request.mock.calls[0])
    expect(h.request.mock.calls.every(([method]) => method === METHOD)).toBe(true)
    expect(h.consume).toHaveBeenCalledOnce()
    expect(h.controller.enabled).toBe(true)
  })

  it('does not accept a successful-looking reply for another delivery epoch', async () => {
    const h = harness()
    h.request.mockResolvedValueOnce({
      delivery_epoch: 'stale-epoch', ack_delivery_id: 1, dirty_keys: [], global_dirty: false,
    })
    h.hello()
    h.deliver(1)
    await vi.advanceTimersByTimeAsync(1050)
    expect(h.request).toHaveBeenCalledTimes(2)
    expect(h.request.mock.calls[1]).toEqual(h.request.mock.calls[0])
  })

  it('keeps only one control request active while later consumers complete', async () => {
    const h = harness()
    const result = deferred<unknown>()
    h.request.mockReturnValueOnce(result.promise)
    h.hello()
    h.deliver(1)
    await vi.advanceTimersByTimeAsync(50)
    h.deliver(2)
    await vi.advanceTimersByTimeAsync(5000)
    expect(h.request).toHaveBeenCalledOnce()
    expect(vi.getTimerCount()).toBe(0)
    result.resolve(reply({ delivery_epoch: EPOCH, ack_delivery_id: 1 }))
    await vi.advanceTimersByTimeAsync(50)
    expect(h.request).toHaveBeenCalledTimes(2)
    expect(h.request).toHaveBeenLastCalledWith(METHOD, {
      delivery_epoch: EPOCH, ack_delivery_id: 2,
    }, expect.anything())
  })

  it('does not let a late old-epoch control reply clear the replacement ACK', async () => {
    const h = harness()
    const oldReply = deferred<unknown>()
    h.request.mockReturnValueOnce(oldReply.promise)
    h.hello()
    h.deliver(1)
    await vi.advanceTimersByTimeAsync(50)
    h.replaceGeneration()
    h.hello('new-epoch')
    h.deliver(1, 'alpha', 'new-epoch')
    await vi.advanceTimersByTimeAsync(50)
    oldReply.resolve(reply({ delivery_epoch: EPOCH, ack_delivery_id: 1 }))
    await vi.advanceTimersByTimeAsync(50)
    h.deliver(2, 'alpha', 'new-epoch')
    await vi.advanceTimersByTimeAsync(50)
    expect(h.request).toHaveBeenCalledTimes(3)
    expect(h.request).toHaveBeenLastCalledWith(METHOD, {
      delivery_epoch: 'new-epoch', ack_delivery_id: 2,
    }, expect.objectContaining({ expectedGeneration: 8 }))
  })

  it('coalesces concurrent dirty recovery into one active read and one delayed rerun', async () => {
    const h = harness()
    const recovery = deferred<boolean>()
    h.recover.mockReturnValueOnce(recovery.promise)
    h.hello()
    h.dirty(['alpha'])
    h.dirty(['beta'])
    h.dirty(['gamma'])
    await vi.advanceTimersByTimeAsync(5000)
    expect(h.recover).toHaveBeenCalledOnce()
    recovery.resolve(true)
    await vi.advanceTimersByTimeAsync(999)
    expect(h.recover).toHaveBeenCalledOnce()
    await vi.advanceTimersByTimeAsync(1)
    expect(h.recover).toHaveBeenCalledTimes(2)
    expect(h.recover).toHaveBeenLastCalledWith({ reason: 'transport_flow_dirty', keys: ['beta', 'gamma'], global: false })
    expect(h.request).not.toHaveBeenCalled()
  })

  it('names every claimed unowned key in a global recovery and independently releases a proven key', async () => {
    const h = harness()
    const first = deferred<boolean>()
    h.consume.mockRejectedValue(new Error('Missing consumer.'))
    h.recover.mockReturnValueOnce(first.promise).mockImplementation(async detail => {
      const scope = detail as { keys: string[], global: boolean }
      // A real hub can prove alpha, but unknown beta has no read admission.
      return !scope.keys.includes('beta')
    })
    h.hello()
    h.deliver(1, 'alpha')
    await vi.advanceTimersByTimeAsync(0)
    h.deliver(2, 'beta')
    h.emit('*', 'transport.flow.dirty', { delivery_epoch: EPOCH, dirty_keys: [], global_dirty: true }, {})
    await vi.advanceTimersByTimeAsync(0)
    first.resolve(false)
    await vi.advanceTimersByTimeAsync(1050)
    const globalScopes = h.recover.mock.calls.map(([scope]) => scope as { keys: string[], global: boolean }).filter(scope => scope.global)
    expect(globalScopes).toHaveLength(1)
    expect(globalScopes[0].keys).toEqual(expect.arrayContaining(['alpha', 'beta']))
    expect(h.recover).toHaveBeenCalledWith({ reason: 'transport_flow_dirty', keys: ['alpha'], global: false })
    expect(h.request).toHaveBeenLastCalledWith(METHOD, { delivery_epoch: EPOCH, ack_delivery_id: 1 }, expect.anything())
    expect(h.request.mock.calls.every(([, params]) => Number(params?.ack_delivery_id) <= 1)).toBe(true)
  })

  it('does not ACK an untagged unknown event because a generic global recovery returned true', async () => {
    const h = harness()
    h.consume.mockRejectedValueOnce(new Error('No owner for this event domain.'))
    h.hello()
    h.emit('*', 'unknown.event', { unrelated: 'body' }, { flow: h.receipt(1) })
    await vi.advanceTimersByTimeAsync(1050)
    expect(h.recover).toHaveBeenCalledWith({ reason: 'transport_flow_dirty', keys: [], global: true })
    expect(h.request).not.toHaveBeenCalled()
  })

  it('retries an unknown queued beta key without repeatedly reconciling healthy alpha', async () => {
    const h = harness()
    const alpha = deferred<boolean>()
    h.consume.mockRejectedValue(new Error('No consumer.'))
    h.recover.mockReturnValueOnce(alpha.promise).mockResolvedValue(false)
    h.hello()
    h.deliver(1, 'alpha')
    await vi.advanceTimersByTimeAsync(0)
    h.deliver(2, 'beta')
    await vi.advanceTimersByTimeAsync(0)
    alpha.resolve(true)
    await vi.advanceTimersByTimeAsync(3050)
    expect(h.recover).toHaveBeenCalledTimes(4)
    for (const [scope] of h.recover.mock.calls.slice(1)) {
      expect(scope).toEqual({ reason: 'transport_flow_dirty', keys: ['beta'], global: false })
    }
    expect(h.request).toHaveBeenCalledExactlyOnceWith(METHOD, { delivery_epoch: EPOCH, ack_delivery_id: 1 }, expect.anything())
  })

  it('reset clears ACK and failed-recovery timers without late retries', async () => {
    const h = harness()
    h.recover.mockResolvedValue(false)
    h.hello()
    h.deliver(1)
    h.dirty()
    await vi.advanceTimersByTimeAsync(0)
    expect(vi.getTimerCount()).toBeGreaterThan(0)
    h.controller.reset()
    expect(vi.getTimerCount()).toBe(0)
    await vi.advanceTimersByTimeAsync(10000)
    expect(h.request).not.toHaveBeenCalled()
    expect(h.recover).toHaveBeenCalledOnce()
  })

  it('reset fences both late recovery failure and late request failure', async () => {
    const h = harness()
    const result = deferred<unknown>()
    const recovery = deferred<boolean>()
    h.request.mockReturnValueOnce(result.promise)
    h.recover.mockReturnValueOnce(recovery.promise)
    h.hello()
    h.deliver(1)
    h.dirty()
    await vi.advanceTimersByTimeAsync(50)
    h.controller.reset()
    result.reject(new Error('Old socket timed out.'))
    recovery.resolve(false)
    await vi.advanceTimersByTimeAsync(10000)
    expect(h.request).toHaveBeenCalledOnce()
    expect(h.recover).toHaveBeenCalledOnce()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('close removes subscriptions and prevents subsequent Hello from re-enabling flow', () => {
    const h = harness()
    h.hello()
    h.controller.close()
    h.hello()
    h.deliver(1)
    expect(h.controller.enabled).toBe(false)
    expect(h.consume).not.toHaveBeenCalled()
  })
})
