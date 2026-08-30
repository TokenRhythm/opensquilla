import { describe, expect, it, vi } from 'vitest'
import {
  createConversationEventHub,
  type ConversationEventSourceHandlers,
} from './conversationEventHub'

type Message = { key?: string, value: number }

function sourceHarness() {
  let active: ConversationEventSourceHandlers<Message> | null = null
  let subscriptions = 0
  let detachments = 0
  const source = {
    subscribe(handlers: ConversationEventSourceHandlers<Message>) {
      subscriptions += 1
      active = handlers
      return () => {
        detachments += 1
        active = null
      }
    },
    emit(message: Message) {
      active?.onEvent?.(message)
    },
    state(value: string) {
      active?.onConnectionState?.(value)
    },
    any(event: string, payload: unknown) {
      active?.onAny?.(event, payload)
    },
    counts() {
      return { subscriptions, detachments }
    },
  }
  return source
}

describe('conversation event hub', () => {
  it('multiplexes logical handles over one source and fences keyed events', () => {
    const source = sourceHarness()
    const hub = createConversationEventHub(source, {
      sessionKey: message => message.key,
    })
    const alpha = hub.open('alpha')
    const beta = hub.open('beta')
    const alphaEvents: Message[] = []
    const betaEvents: Message[] = []
    alpha.observe(message => alphaEvents.push(message))
    beta.observe(message => betaEvents.push(message))

    source.emit({ key: 'alpha', value: 1 })
    source.emit({ key: 'beta', value: 2 })
    source.emit({ value: 3 })

    expect(alphaEvents.map(item => item.value)).toEqual([1, 3])
    expect(betaEvents.map(item => item.value)).toEqual([2, 3])
    expect(source.counts()).toEqual({ subscriptions: 1, detachments: 0 })
  })

  it('keeps the physical source alive until the last logical owner closes', () => {
    const source = sourceHarness()
    const hub = createConversationEventHub(source)
    const first = hub.open('')
    const second = hub.open('')
    const firstListener = vi.fn()
    const secondListener = vi.fn()
    first.observe(firstListener)
    second.observe(secondListener)

    first.close()
    source.emit({ value: 1 })
    expect(firstListener).not.toHaveBeenCalled()
    expect(secondListener).toHaveBeenCalledWith({ value: 1 })
    expect(source.counts()).toEqual({ subscriptions: 1, detachments: 0 })

    second.close()
    expect(source.counts()).toEqual({ subscriptions: 1, detachments: 1 })
    second.close()
    expect(source.counts()).toEqual({ subscriptions: 1, detachments: 1 })
  })

  it('forwards diagnostics and supports idempotent observer removal', () => {
    const source = sourceHarness()
    const hub = createConversationEventHub(source)
    const state = vi.fn()
    const any = vi.fn()
    const offState = hub.observeConnectionState(state)
    const offAny = hub.observeAny(any)

    source.state('connected')
    expect(state).toHaveBeenCalledWith('connected')
    source.any('presence', { value: true })
    expect(any).toHaveBeenCalledWith('presence', { value: true })
    offState()
    offState()
    offAny()
    offAny()
    expect(source.counts()).toEqual({ subscriptions: 1, detachments: 1 })
  })

  it('dispose closes handles and prevents reconnection', () => {
    const source = sourceHarness()
    const hub = createConversationEventHub(source)
    const handle = hub.open('')
    const listener = vi.fn()
    handle.observe(listener)
    hub.dispose()
    source.emit({ value: 4 })
    expect(listener).not.toHaveBeenCalled()
    expect(source.counts()).toEqual({ subscriptions: 1, detachments: 1 })
    expect(handle.observe(listener)).not.toThrow()
  })
})
