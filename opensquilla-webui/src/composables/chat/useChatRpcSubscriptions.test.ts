import { describe, expect, it, vi } from 'vitest'
import { useChatRpcSubscriptions } from './useChatRpcSubscriptions'
import type { TransportEventHandler } from '@/adapters/gateway/transportTypes'
import { createConversationEventHub, type ConversationEventSourceHandlers } from '@/modules/conversationEventHub'
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
  it('does not turn an unmaterialized side into a global consumer while the main chat is active', () => {
    const rpc = rpcHarness()
    const runtime = runtimeHarness(rpc)
    const mainEvent = vi.fn()
    const sideEvent = vi.fn()
    const sideState = vi.fn()
    const main = useChatRpcSubscriptions({ onEvent: mainEvent }, { runtime, getSessionKey: () => 'parent' })
    let key = ''
    const side = useChatRpcSubscriptions({ onEvent: sideEvent, onConnectionState: sideState }, { runtime, getSessionKey: () => key })
    main.subscribe(); side.subscribe()
    rpc.emit('*', 'session.event.text_delta', { session_key: 'parent', stream_seq: 1, text: 'main answer' }, {})
    rpc.emit('_state', 'connected')
    expect(mainEvent).toHaveBeenCalledOnce()
    expect(sideEvent).not.toHaveBeenCalled()
    expect(sideState).not.toHaveBeenCalled()
    expect(rpc.count('*')).toBe(1)

    key = 'child'; side.setSessionKey(key)
    rpc.emit('*', 'session.event.text_delta', { session_key: 'parent', stream_seq: 2, text: 'more main' }, {})
    rpc.emit('*', 'session.event.text_delta', { session_key: 'child', stream_seq: 1, text: 'side answer' }, {})
    rpc.emit('_state', 'connected')
    expect(sideEvent).toHaveBeenCalledOnce()
    expect(sideState).toHaveBeenCalledOnce()
    expect(mainEvent).toHaveBeenCalledTimes(2)
    main.unsubscribe(); side.unsubscribe()
  })

  it('detaches an explicit empty owner immediately and can attach again without subscribing twice', () => {
    const rpc = rpcHarness()
    const event = vi.fn()
    const state = vi.fn()
    let key = 'child'
    const bridge = useChatRpcSubscriptions({ onEvent: event, onConnectionState: state }, { runtime: runtimeHarness(rpc), getSessionKey: () => key })
    bridge.subscribe()
    key = ''; bridge.setSessionKey(key)
    expect(rpc.count('*')).toBe(0)
    expect(rpc.count('_state')).toBe(0)
    rpc.emit('*', 'session.event.text_delta', { session_key: 'child', stream_seq: 1, text: 'late' }, {})
    rpc.emit('_state', 'connected')
    expect(event).not.toHaveBeenCalled()
    expect(state).not.toHaveBeenCalled()
    key = 'next-child'; bridge.setSessionKey(key)
    expect(rpc.count('*')).toBe(1)
    rpc.emit('*', 'session.event.text_delta', { session_key: key, stream_seq: 1, text: 'new' }, {})
    expect(event).toHaveBeenCalledOnce()
    bridge.unsubscribe()
    key = 'third-child'; bridge.setSessionKey(key)
    expect(rpc.count('*')).toBe(0)
  })

  it('fences decode/connection/recovery callbacks for empty owners and late recovery completion', async () => {
    let sourceHandlers!: ConversationEventSourceHandlers<ConversationEventTransportMessage>
    const hub = createConversationEventHub<ConversationEventTransportMessage>({ subscribe(handlers) {
      sourceHandlers = handlers
      return () => {}
    } }, { sessionKey: conversationEventSessionKey })
    // Another actual read keeps the transport connected while this draft has
    // no child. All callbacks still pass through the real hub and bridge.
    const keeper = hub.open('parent')
    keeper.observe(() => 'applied')
    hub.prepareReadRetirement('child')
    hub.prepareReadRetirement('other')
    const error = vi.fn()
    const connection = vi.fn()
    const recover = vi.fn(async () => true)
    let key = ''
    const bridge = useChatRpcSubscriptions({ onEvent: vi.fn(), onDecodeError: error, onConnectionState: connection, onRecoveryRequired: recover },
      { runtime: { events: hub }, getSessionKey: () => key })
    bridge.subscribe()
    sourceHandlers.onDecodeError?.(new Error('decode'))
    sourceHandlers.onConnectionState?.('connected')
    expect(await sourceHandlers.onRecoveryRequired?.({ keys: ['child'], global: false })).toEqual({ retryable: false })
    expect(error).not.toHaveBeenCalled()
    expect(connection).not.toHaveBeenCalled()
    expect(recover).not.toHaveBeenCalled()

    key = 'child'; bridge.setSessionKey(key)
    expect(await sourceHandlers.onRecoveryRequired?.({ keys: ['other'], global: false })).toBe(false)
    expect(recover).not.toHaveBeenCalled()
    sourceHandlers.onDecodeError?.(new Error('owned decode'))
    expect(error).toHaveBeenCalledOnce()
    let finish!: (value: boolean) => void
    recover.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    const pending = sourceHandlers.onRecoveryRequired!({ keys: ['child'], global: false })
    await vi.waitFor(() => expect(recover).toHaveBeenCalledOnce())
    key = ''; bridge.setSessionKey(key)
    finish(true)
    expect(await pending).toBe(false)
    sourceHandlers.onDecodeError?.(new Error('retired'))
    expect(error).toHaveBeenCalledOnce()
    bridge.unsubscribe(); keeper.close()
  })

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
    })
    expect(event).toHaveBeenCalledOnce()
    bridge.unsubscribe()
  })
})
