import { describe, expect, it, vi } from 'vitest'
import type { RpcEventHandler } from '@/lib/rpc'
import { createConversationEventTransport } from './conversationEventTransport'

type ListenerMap = Map<string, Set<RpcEventHandler>>

function harness() {
  const listeners: ListenerMap = new Map()
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

  it('decodes aliases before dispatch while preserving raw wildcard observation', () => {
    const { rpc, transport } = harness()
    const event = vi.fn()
    const any = vi.fn()
    transport.subscribe({ onEvent: event, onAny: any })

    const payload = { sessionKey: 'agent:main:alpha', streamSeq: 3, text: 'hi' }
    rpc.emit('*', 'text_delta', payload, { replayed: false })

    expect(event).toHaveBeenCalledTimes(1)
    expect(event.mock.calls[0]?.[0]).toMatchObject({
      kind: 'conversation',
      wireName: 'text_delta',
      payload,
      meta: { replayed: false },
      decoded: { kind: 'known', name: 'session.event.text_delta', legacy: true },
    })
    expect(any).toHaveBeenCalledWith('text_delta', payload)
  })

  it('keeps directory changes in the same listener without treating them as conversation frames', () => {
    const { rpc, transport } = harness()
    const event = vi.fn()
    const any = vi.fn()
    transport.subscribe({ onEvent: event, onAny: any })
    const payload = { key: 'agent:main:alpha', reason: 'renamed' }

    rpc.emit('*', 'sessions.changed', payload, {})

    expect(event).toHaveBeenCalledWith({
      kind: 'sessions-changed',
      wireName: 'sessions.changed',
      decoded: null,
      payload,
      meta: {},
    })
    expect(any).toHaveBeenCalledWith('sessions.changed', payload)
  })

  it('quarantines malformed frames but does not break the wildcard stream', () => {
    const { rpc, transport } = harness()
    const event = vi.fn()
    const error = vi.fn()
    const any = vi.fn()
    transport.subscribe({ onEvent: event, onDecodeError: error, onAny: any })

    rpc.emit('*', 'presence', { value: true }, {})

    expect(event).toHaveBeenCalledWith(expect.objectContaining({
      kind: 'invalid',
      wireName: 'presence',
      payload: { value: true },
    }))
    expect(error).toHaveBeenCalledTimes(1)
    expect(any).toHaveBeenCalledWith('presence', { value: true })
  })

  it('forwards connection state through the same lifecycle owner', () => {
    const { rpc, transport } = harness()
    const state = vi.fn()
    transport.subscribe({ onConnectionState: state })

    rpc.emit('_state', 'connected')

    expect(state).toHaveBeenCalledWith('connected')
  })
})
