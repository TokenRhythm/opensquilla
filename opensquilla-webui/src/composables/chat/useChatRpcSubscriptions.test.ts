import { describe, expect, it, vi } from 'vitest'
import { useChatRpcSubscriptions } from './useChatRpcSubscriptions'
import type { TransportEventHandler } from '@/adapters/gateway/transportTypes'
import { createConversationEventHub } from '@/modules/conversationEventHub'
import {
  createConversationEventTransport,
  conversationEventSessionKey,
  type ConversationEventTransportMessage,
} from '@/adapters/gateway/conversationEventTransport'

function rpcHarness() {
  const listeners = new Map<string, Set<TransportEventHandler>>()
  const rpc = {
    subscribe(event: string, handler: TransportEventHandler) {
      const bucket = listeners.get(event) ?? new Set<TransportEventHandler>()
      bucket.add(handler)
      listeners.set(event, bucket)
      return { close: () => { bucket.delete(handler) } }
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

function runtimeHarness(rpc: ReturnType<typeof rpcHarness>) {
  return {
    events: createConversationEventHub(createConversationEventTransport(rpc), {
      sessionKey: conversationEventSessionKey,
    }),
  }
}

describe('useChatRpcSubscriptions', () => {
  it('bridges one logical subscription through the event hub and detaches idempotently', () => {
    const rpc = rpcHarness()
    const event = vi.fn()
    const state = vi.fn()
    const bridge = useChatRpcSubscriptions({
      onEvent: event,
      onConnectionState: state,
    }, {
      runtime: runtimeHarness(rpc),
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
    expect(state).toHaveBeenCalledWith('connected')
    rpc.emit('_state', 'disconnected')
    rpc.emit('_state', 'connected')
    expect(state.mock.calls.map(call => call[0]))
      .toEqual(['connected', 'disconnected', 'connected'])
    expect(rpc.count('*')).toBe(1)

    detach()
    detach()
    expect(rpc.count('*')).toBe(0)
    expect(rpc.count('_state')).toBe(0)
  })

  it('allows a second logical handle without a second WebSocket listener', () => {
    const rpc = rpcHarness()
    const primary = vi.fn()
    const secondary = vi.fn()
    const bridge = useChatRpcSubscriptions(
      { onEvent: primary },
      { runtime: runtimeHarness(rpc) },
    )
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
      { onEvent: event },
      { runtime: runtimeHarness(rpc), getSessionKey: () => currentKey },
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

  it('uses the composition runtime source instead of creating another RPC listener', () => {
    const rpc = rpcHarness()
    const sourceState: {
      emit: ((message: ConversationEventTransportMessage) => void) | null
    } = { emit: null }
    const hub = createConversationEventHub<ConversationEventTransportMessage>({
      subscribe(handlers) {
        sourceState.emit = handlers.onEvent ?? null
        return () => { sourceState.emit = null }
      },
    }, {
      sessionKey: message => (
        message.kind === 'conversation' ? message.event.sessionKey : null
      ),
    })
    const event = vi.fn()
    const bridge = useChatRpcSubscriptions(
      { onEvent: event },
      { runtime: { events: hub }, getSessionKey: () => 'agent:main:a' },
    )
    bridge.subscribe()

    expect(rpc.count('*')).toBe(0)
    sourceState.emit?.({
      kind: 'sessions-changed',
      payload: {},
      meta: {},
    })
    expect(event).toHaveBeenCalledOnce()
    bridge.unsubscribe()
  })
})
