import { describe, expect, it, vi } from 'vitest'
import type { TransportEventHandler } from './transportTypes'
import { createConversationEventTransport } from './conversationEventTransport'

type ListenerMap = Map<string, Set<TransportEventHandler>>

function harness() {
  const listeners: ListenerMap = new Map()
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
    registered(event: string) {
      return listeners.get(event)?.size ?? 0
    },
  }
  return { rpc, transport: createConversationEventTransport(rpc) }
}

describe('conversation event transport adapter', () => {
  it('uses one wildcard listener and one connection-state listener', () => {
    const { rpc, transport } = harness()
    const detach = transport.subscribe({})

    expect(rpc.registered('*')).toBe(1)
    expect(rpc.registered('_state')).toBe(1)
    detach()
    expect(rpc.registered('*')).toBe(0)
    expect(rpc.registered('_state')).toBe(0)
  })

  it('decodes aliases into a semantic event while preserving the opaque payload', () => {
    const { rpc, transport } = harness()
    const event = vi.fn()
    transport.subscribe({ onEvent: event })

    const payload = { sessionKey: 'agent:main:alpha', streamSeq: 3, text: 'hi' }
    rpc.emit('*', 'text_delta', payload, { replayed: false })

    expect(event).toHaveBeenCalledTimes(1)
    expect(event.mock.calls[0]?.[0]).toMatchObject({
      kind: 'conversation',
      payload,
      meta: { replayed: false },
      event: { kind: 'known', semanticKind: 'text-delta', legacy: true },
    })
  })

  it('keeps directory changes in the same listener without treating them as conversation frames', () => {
    const { rpc, transport } = harness()
    const event = vi.fn()
    transport.subscribe({ onEvent: event })
    const payload = { key: 'agent:main:alpha', reason: 'renamed' }

    rpc.emit('*', 'sessions.changed', payload, {})

    expect(event).toHaveBeenCalledWith({
      kind: 'sessions-changed',
      payload,
      meta: {},
    })
  })

  it('quarantines malformed frames but does not break the wildcard stream', () => {
    const { rpc, transport } = harness()
    const event = vi.fn()
    const error = vi.fn()
    transport.subscribe({ onEvent: event, onDecodeError: error })

    rpc.emit('*', 'presence', { value: true }, {})

    expect(event).toHaveBeenCalledWith(expect.objectContaining({
      kind: 'invalid',
    }))
    expect(error).toHaveBeenCalledTimes(1)
  })

  it('projects approval aliases before they reach business consumers', () => {
    const { rpc, transport } = harness()
    const event = vi.fn()
    transport.subscribe({ onEvent: event })
    const payload = { approval_id: 'approval-1' }

    rpc.emit('*', 'exec.approval.requested', payload, {})
    rpc.emit('*', 'plugin.approval.resolved', payload, {})

    expect(event.mock.calls.map(call => call[0])).toEqual([
      { kind: 'approval', action: 'requested', sessionKey: null, payload, meta: {} },
      { kind: 'approval', action: 'resolved', sessionKey: null, payload, meta: {} },
    ])
  })

  it('leaves duplicate fencing to ConversationRuntime', () => {
    const { rpc, transport } = harness()
    const event = vi.fn()
    transport.subscribe({ onEvent: event })
    const payload = { session_key: 'agent:main:alpha', stream_seq: 7, text: 'same' }

    rpc.emit('*', 'session.event.text_delta', payload, {})
    rpc.emit('*', 'text_delta', payload, {})

    expect(event).toHaveBeenCalledTimes(2)
    expect(event.mock.calls.map(call => call[0].event.semanticKind))
      .toEqual(['text-delta', 'text-delta'])
  })

  it('forwards connection state through the same lifecycle owner', () => {
    const { rpc, transport } = harness()
    const state = vi.fn()
    transport.subscribe({ onConnectionState: state })

    rpc.emit('_state', 'connected')

    expect(state).toHaveBeenCalledWith('connected')
  })
})
