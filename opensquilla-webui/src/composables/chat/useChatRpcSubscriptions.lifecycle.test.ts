import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { RpcClient } from '@/lib/rpc'
import { createConversationEventHub } from '@/modules/conversationEventHub'
import { createConversationEventTransport, conversationEventSessionKey } from '@/adapters/gateway/conversationEventTransport'
import { TransportFlowV4 } from '@/adapters/gateway/transportFlowV4'
import type { TransportEventHandler } from '@/adapters/gateway/transportTypes'
import { useChatRpcSubscriptions } from './useChatRpcSubscriptions'

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}

const cleanups: Array<() => void> = []

/** The real consumption/recovery chain, with only wire/control I/O replaced. */
function harness() {
  const client = new RpcClient()
  const listeners = new Map<string, Set<TransportEventHandler>>()
  let generation = 1
  let epoch = 'epoch-1'
  const on = (event: string, handler: TransportEventHandler) => {
    const bucket = listeners.get(event) ?? new Set<TransportEventHandler>()
    bucket.add(handler)
    listeners.set(event, bucket)
    return () => { bucket.delete(handler) }
  }
  const emit = (event: string, ...args: unknown[]) => {
    for (const handler of listeners.get(event) ?? []) handler(...args)
  }
  const transport = createConversationEventTransport({
    subscribe: (event, handler) => ({ close: on(event, handler) }),
    subscribeConsumed: (event, handler) => ({ close: client.onConsumedEvent(event, handler) }),
    subscribeGap: handler => ({ close: client.onGap(handler) }),
  })
  const hub = createConversationEventHub(transport, { sessionKey: conversationEventSessionKey })
  // A second view keeps the physical source present while the child detaches.
  const keeper = hub.open('parent')
  keeper.observe(() => 'applied')
  const recoverGap = vi.spyOn(client, 'recoverGap')
  const control = vi.fn(async (_method: string, params: Record<string, unknown> = {}) => ({
    delivery_epoch: params.delivery_epoch, ack_delivery_id: params.ack_delivery_id,
    dirty_keys: [], global_dirty: false,
  }))
  const flow = new TransportFlowV4({
    get connectionGeneration() { return generation },
    on,
    enableConsumptionFlow: () => client.enableConsumptionFlow(),
    consumeEvent: (event, payload, meta) => client.consumeEvent(event, payload, meta),
    recoverGap: detail => client.recoverGap(detail),
    supportsRecovery: () => true,
    request: async <T>(method: string, params?: Record<string, unknown>) => control(method, params) as Promise<T>,
  })
  const recover = vi.fn(async () => true)
  const consume = vi.fn((): 'applied' | undefined => 'applied')
  let key = 'child'
  const bridge = useChatRpcSubscriptions({ onEvent: consume, onRecoveryRequired: recover }, {
    runtime: { events: hub }, getSessionKey: () => key,
  })
  function hello() {
    emit('_hello', { policy: { transport_flow: {
      delivery_epoch: epoch, window_frames: 128, window_bytes: 4194304,
    } } })
    emit('_state', 'connected')
  }
  function deliver(id: number) {
    emit('*', 'session.event.text_delta', { session_key: 'child', stream_seq: id, text: 'x' }, {
      flow: { delivery_epoch: epoch, delivery_id: id },
    })
  }
  const dispose = () => {
    bridge.unsubscribe()
    keeper.close()
    hub.dispose()
    transport.unsubscribe()
    flow.close()
    client.disconnect()
  }
  cleanups.push(dispose)
  return {
    bridge, hub, flow, recover, consume, control, recoverGap, deliver, dispose,
    start() { hello(); hub.prepareReadRetirement('child') },
    setKey(value: string) { key = value; bridge.setSessionKey(value) },
    reconnect() {
      emit('_state', 'disconnected')
      generation++
      epoch = `epoch-${generation}`
      hello()
      hub.prepareReadRetirement('child')
    },
    recoverExplicitly: () => client.recoverGap({ keys: ['child'], global: false }),
    invalidateRead() {
      emit('*', 'transport.flow.dirty', { delivery_epoch: epoch, dirty_keys: ['child'], global_dirty: false }, {})
    },
    listenerCount: () => [...listeners.values()].reduce((count, bucket) => count + bucket.size, 0),
  }
}

beforeEach(() => { vi.useFakeTimers() })
afterEach(() => {
  for (const cleanup of cleanups.splice(0)) cleanup()
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('chat owner recovery through the hub and consumption flow', () => {
  it('keeps ownerless admission suspended across reattachment until explicit recovery and a current installation', async () => {
    const h = harness()
    h.bridge.subscribe()
    h.start()
    h.bridge.unsubscribe()
    h.deliver(1)
    await vi.advanceTimersByTimeAsync(5_000)
    expect(h.recoverGap).toHaveBeenCalledOnce()
    await expect(h.recoverGap.mock.results[0]!.value).resolves.toEqual({ retryable: false })
    expect(h.recover).not.toHaveBeenCalled()
    expect(h.control).not.toHaveBeenCalled()
    expect(h.flow.diagnostics).toMatchObject({ ackDeliveryId: 0, unownedFrames: 1, recoveryInFlight: false, queuedRecoveryKeys: 0 })

    // Even an installation begun after that pause becomes stale when the
    // server invalidates the read again. Neither ownerless result consumes it.
    const supersededVersion = h.flow.recoveryVersion('child')
    h.invalidateRead()
    await vi.advanceTimersByTimeAsync(0)
    expect(h.recoverGap).toHaveBeenCalledTimes(2)
    expect(h.control).not.toHaveBeenCalled()

    // Reattaching a view does not itself re-drive a suspended recovery.
    h.bridge.subscribe()
    await vi.advanceTimersByTimeAsync(5_000)
    expect(h.recoverGap).toHaveBeenCalledTimes(2)
    expect(h.recover).not.toHaveBeenCalled()
    expect(h.control).not.toHaveBeenCalled()

    const installation = deferred<boolean>()
    h.recover.mockReturnValueOnce(installation.promise)
    const retry = h.recoverExplicitly()
    await vi.advanceTimersByTimeAsync(0)
    expect(h.recover).toHaveBeenCalledOnce()
    const currentVersion = h.flow.recoveryVersion('child')
    expect(currentVersion).not.toBe(supersededVersion)
    h.flow.snapshotInstalled('child', supersededVersion)
    expect(h.flow.diagnostics.ackDeliveryId).toBe(0)
    installation.resolve(true)
    await expect(retry).resolves.toBe(true)
    // A boolean recovery result outside the flow cannot manufacture an ACK.
    expect(h.flow.diagnostics.ackDeliveryId).toBe(0)
    h.flow.snapshotInstalled('child', currentVersion)
    await vi.advanceTimersByTimeAsync(50)
    expect(h.flow.diagnostics).toMatchObject({ ackDeliveryId: 1, unownedFrames: 0 })
    expect(h.control).toHaveBeenCalledExactlyOnceWith('transport.flow.update', {
      delivery_epoch: 'epoch-1', ack_delivery_id: 1,
    })
    h.deliver(2)
    await vi.advanceTimersByTimeAsync(50)
    expect(h.consume).toHaveBeenCalledOnce()
    expect(h.flow.diagnostics.ackDeliveryId).toBe(2)
  })

  it('rejects a detached owner completion even when the same key has already reattached', async () => {
    const h = harness()
    const oldRecovery = deferred<boolean>()
    const currentRecovery = deferred<boolean>()
    h.consume.mockReturnValue(undefined)
    h.recover.mockReturnValueOnce(oldRecovery.promise).mockReturnValueOnce(currentRecovery.promise)
    h.bridge.subscribe()
    h.start()
    h.deliver(1)
    await vi.advanceTimersByTimeAsync(0)
    expect(h.recover).toHaveBeenCalledOnce()
    h.setKey('')
    h.setKey('child')
    oldRecovery.resolve(true)
    await vi.advanceTimersByTimeAsync(0)
    await expect(h.recoverGap.mock.results[0]!.value).resolves.toBe(false)
    expect(h.flow.diagnostics).toMatchObject({ ackDeliveryId: 0, unownedFrames: 1 })
    expect(h.control).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(1_000)
    expect(h.recover).toHaveBeenCalledTimes(2)
    currentRecovery.resolve(true)
    await vi.advanceTimersByTimeAsync(50)
    expect(h.flow.diagnostics).toMatchObject({ ackDeliveryId: 1, unownedFrames: 0 })
  })

  it('fences both recovery completion and installation from the previous connection generation', async () => {
    const h = harness()
    const oldRecovery = deferred<boolean>()
    const currentRecovery = deferred<boolean>()
    h.consume.mockReturnValue(undefined)
    h.recover.mockReturnValueOnce(oldRecovery.promise).mockReturnValueOnce(currentRecovery.promise)
    h.bridge.subscribe()
    h.start()
    h.deliver(1)
    await vi.advanceTimersByTimeAsync(0)
    const oldVersion = h.flow.recoveryVersion('child')
    h.reconnect()
    h.deliver(1)
    await vi.advanceTimersByTimeAsync(0)
    expect(h.recover).toHaveBeenCalledTimes(2)
    oldRecovery.resolve(true)
    h.flow.snapshotInstalled('child', oldVersion)
    await vi.advanceTimersByTimeAsync(50)
    await expect(h.recoverGap.mock.results[0]!.value).resolves.toBe(false)
    expect(h.flow.diagnostics).toMatchObject({ ackDeliveryId: 0, unownedFrames: 1 })
    expect(h.control).not.toHaveBeenCalled()
    currentRecovery.resolve(true)
    await vi.advanceTimersByTimeAsync(50)
    expect(h.flow.diagnostics).toMatchObject({ ackDeliveryId: 1, unownedFrames: 0 })
    expect(h.control).toHaveBeenCalledExactlyOnceWith('transport.flow.update', {
      delivery_epoch: 'epoch-2', ack_delivery_id: 1,
    })
  })

  it('disposes the owner, hub and flow without ACK or retry from an in-flight completion', async () => {
    const h = harness()
    const recovery = deferred<boolean>()
    h.consume.mockReturnValue(undefined)
    h.recover.mockReturnValueOnce(recovery.promise)
    h.bridge.subscribe()
    h.start()
    h.deliver(1)
    await vi.advanceTimersByTimeAsync(0)
    expect(h.recover).toHaveBeenCalledOnce()
    h.dispose()
    expect(h.listenerCount()).toBe(0)
    recovery.resolve(true)
    await vi.advanceTimersByTimeAsync(5_000)
    await expect(h.recoverGap.mock.results[0]!.value).resolves.toBe(false)
    expect(h.recover).toHaveBeenCalledOnce()
    expect(h.control).not.toHaveBeenCalled()
    expect(h.flow.diagnostics).toMatchObject({ enabled: false, ackDeliveryId: 0, pendingFrames: 0, unownedFrames: 0, recoveryInFlight: false })
    expect(vi.getTimerCount()).toBe(0)
  })
})
