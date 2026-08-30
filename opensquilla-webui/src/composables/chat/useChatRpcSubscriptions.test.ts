import { describe, expect, it, vi } from 'vitest'
import { useChatRpcSubscriptions } from './useChatRpcSubscriptions'
import type { RpcEventHandler } from '@/lib/rpc'

function rpcHarness() {
  const listeners = new Map<string, Set<RpcEventHandler>>()
  const rpc = {
    on(event: string, handler: RpcEventHandler) {
      const bucket = listeners.get(event) ?? new Set<RpcEventHandler>()
      bucket.add(handler)
      listeners.set(event, bucket)
      return () => bucket.delete(handler)
    },
    emit(event: string, ...args: unknown[]) {
      for (const handler of listeners.get(event) ?? []) handler(...args)
    },
    count(event: string) {
      return listeners.get(event)?.size ?? 0
    },
  }
  return rpc
}

describe('useChatRpcSubscriptions', () => {
  it('bridges one logical subscription through the event hub and detaches idempotently', () => {
    const rpc = rpcHarness()
    const event = vi.fn()
    const any = vi.fn()
    const state = vi.fn()
    const bridge = useChatRpcSubscriptions(rpc, {
      onEvent: event,
      onAny: any,
      onConnectionState: state,
    })

    const detach = bridge.subscribe()
    expect(rpc.count('*')).toBe(1)
    expect(rpc.count('_state')).toBe(1)
    rpc.emit('*', 'session.event.text_delta', {
      session_key: 'agent:main:test',
      stream_seq: 1,
      text: 'hello',
    }, {})
    rpc.emit('_state', 'connected')
    expect(event).toHaveBeenCalledTimes(1)
    expect(any).toHaveBeenCalledWith('session.event.text_delta', expect.any(Object))
    expect(state).toHaveBeenCalledWith('connected')

    detach()
    detach()
    expect(rpc.count('*')).toBe(0)
    expect(rpc.count('_state')).toBe(0)
  })

  it('allows a second logical handle without a second WebSocket listener', () => {
    const rpc = rpcHarness()
    const primary = vi.fn()
    const secondary = vi.fn()
    const bridge = useChatRpcSubscriptions(rpc, { onEvent: primary })
    const primaryDetach = bridge.subscribe()
    const secondaryStream = bridge.open('agent:main:secondary', secondary)

    expect(rpc.count('*')).toBe(1)
    rpc.emit('*', 'session.event.text_delta', {
      session_key: 'agent:main:secondary',
      stream_seq: 1,
      text: 'secondary',
    }, {})
    expect(primary).toHaveBeenCalledTimes(1)
    expect(secondary).toHaveBeenCalledTimes(1)

    secondaryStream.unsubscribe()
    primaryDetach()
    expect(rpc.count('*')).toBe(0)
  })

  it('changes only the logical session fence when the visible route changes', () => {
    const rpc = rpcHarness()
    const event = vi.fn()
    let currentKey = 'agent:main:alpha'
    const bridge = useChatRpcSubscriptions(
      rpc,
      { onEvent: event },
      { getSessionKey: () => currentKey },
    )
    bridge.subscribe()

    rpc.emit('*', 'session.event.text_delta', {
      session_key: 'agent:main:beta', stream_seq: 1, text: 'hidden',
    }, {})
    expect(event).not.toHaveBeenCalled()

    currentKey = 'agent:main:beta'
    bridge.setSessionKey(currentKey)
    rpc.emit('*', 'session.event.text_delta', {
      session_key: 'agent:main:beta', stream_seq: 2, text: 'visible',
    }, {})
    expect(event).toHaveBeenCalledTimes(1)
    expect(rpc.count('*')).toBe(1)
    bridge.unsubscribe()
  })
})
