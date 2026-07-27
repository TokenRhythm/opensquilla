import { describe, expect, it, vi } from 'vitest'

import { useSessionListSubscription } from './useSessionListSubscription'

type Handler = (payload: unknown) => void

function makeRpc(initialState: 'connected' | 'disconnected' = 'connected') {
  let state = initialState
  const handlers = new Map<string, Set<Handler>>()
  const calls: string[] = []
  const rpc = {
    call: vi.fn(async (method: string) => {
      calls.push(method)
    }),
    on: vi.fn((event: string, handler: Handler) => {
      const eventHandlers = handlers.get(event) || new Set<Handler>()
      eventHandlers.add(handler)
      handlers.set(event, eventHandlers)
      return () => eventHandlers.delete(handler)
    }),
  }

  return {
    rpc,
    calls,
    isConnected: () => state === 'connected',
    emit(event: string, payload: unknown) {
      if (event === '_state' && typeof payload === 'string') {
        state = payload as 'connected' | 'disconnected'
      }
      handlers.get(event)?.forEach(handler => handler(payload))
    },
    listenerCount(event: string) {
      return handlers.get(event)?.size || 0
    },
  }
}

async function flushAsyncWork() {
  await Promise.resolve()
  await Promise.resolve()
  await Promise.resolve()
}

describe('useSessionListSubscription', () => {
  it('subscribes before loading and refreshes on session changes', async () => {
    const harness = makeRpc()
    const refresh = vi.fn(async () => {
      harness.calls.push('refresh')
    })
    const scheduleRefresh = vi.fn()
    const subscription = useSessionListSubscription({
      rpc: harness.rpc,
      isConnected: harness.isConnected,
      refresh,
      scheduleRefresh,
    })

    subscription.subscribe()
    await flushAsyncWork()
    harness.emit('sessions.changed', { key: 'agent:main:webchat:test' })

    expect(harness.calls).toEqual(['sessions.subscribe', 'refresh'])
    expect(scheduleRefresh).toHaveBeenCalledOnce()
  })

  it('subscribes once per connection and resubscribes after reconnect', async () => {
    const harness = makeRpc()
    const refresh = vi.fn(async () => {})
    const subscription = useSessionListSubscription({
      rpc: harness.rpc,
      isConnected: harness.isConnected,
      refresh,
      scheduleRefresh: vi.fn(),
    })

    subscription.subscribe()
    harness.emit('_state', 'connected')
    await flushAsyncWork()
    expect(harness.rpc.call).toHaveBeenCalledTimes(1)

    harness.emit('_state', 'disconnected')
    harness.emit('_state', 'connected')
    await flushAsyncWork()

    expect(harness.rpc.call).toHaveBeenNthCalledWith(1, 'sessions.subscribe')
    expect(harness.rpc.call).toHaveBeenNthCalledWith(2, 'sessions.subscribe')
    expect(refresh).toHaveBeenCalledTimes(2)
  })

  it('retries a failed subscription on reconnect and cleans up best-effort', async () => {
    const harness = makeRpc()
    harness.rpc.call
      .mockRejectedValueOnce(new Error('connection closed'))
      .mockResolvedValue(undefined)
    const refresh = vi.fn(async () => {})
    const warn = vi.fn()
    const subscription = useSessionListSubscription({
      rpc: harness.rpc,
      isConnected: harness.isConnected,
      refresh,
      scheduleRefresh: vi.fn(),
      warn,
    })

    subscription.subscribe()
    await flushAsyncWork()
    expect(refresh).not.toHaveBeenCalled()
    expect(warn).toHaveBeenCalledOnce()

    harness.emit('_state', 'disconnected')
    harness.emit('_state', 'connected')
    await flushAsyncWork()
    expect(refresh).toHaveBeenCalledOnce()

    subscription.cleanup()
    await flushAsyncWork()

    expect(harness.rpc.call).toHaveBeenLastCalledWith('sessions.unsubscribe')
    expect(harness.listenerCount('_state')).toBe(0)
    expect(harness.listenerCount('sessions.changed')).toBe(0)
  })
})
