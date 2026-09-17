import { describe, expect, it, vi } from 'vitest'

import {
  createConversationSessionRuntime,
  type ConversationSessionRuntime,
} from './conversationSessionRuntime'

type Event = { key?: string; value: number }
type Outcome = { authoritative: boolean }

function sourceHarness() {
  let subscriptions = 0
  let detachments = 0
  let handlers: ((event: Event) => void) | null = null
  const source = {
    subscribe(next: { onEvent?: (event: Event) => void }) {
      subscriptions += 1
      handlers = next.onEvent ?? null
      return () => {
        detachments += 1
        handlers = null
      }
    },
  }
  return {
    source,
    emit(event: Event) { handlers?.(event) },
    counts: () => ({ subscriptions, detachments }),
  }
}

function runtime(source: ReturnType<typeof sourceHarness>['source']) {
  return createConversationSessionRuntime<Event, Outcome>({
    source,
    events: { sessionKey: event => event.key },
  })
}

describe('ConversationSessionRuntime', () => {
  it('shares one event source and one cursor policy across logical handles', () => {
    const harness = sourceHarness()
    const services = runtime(harness.source)
    const first = services.events.open('a')
    const second = services.events.open('b')
    const firstEvents: Event[] = []
    const secondEvents: Event[] = []
    first.observe(event => firstEvents.push(event))
    second.observe(event => secondEvents.push(event))

    harness.emit({ key: 'a', value: 1 })
    harness.emit({ key: 'b', value: 2 })

    expect(firstEvents.map(event => event.value)).toEqual([1])
    expect(secondEvents.map(event => event.value)).toEqual([2])
    expect(harness.counts()).toEqual({ subscriptions: 1, detachments: 0 })
    expect(services.cursor.createCursor('a').sessionKey).toBe('a')

    first.close()
    expect(harness.counts()).toEqual({ subscriptions: 1, detachments: 0 })
    second.close()
    expect(harness.counts()).toEqual({ subscriptions: 1, detachments: 1 })
  })

  it('owns subscription attempts independently from event handle closure', async () => {
    const harness = sourceHarness()
    const services: ConversationSessionRuntime<Event, Outcome> = runtime(harness.source)
    const first = services.subscriptions.start(
      {
        key: 'a',
        sinceStreamGeneration: null,
        sinceStreamSeq: 0,
        bootstrapGeneration: 1,
        bootstrapAttempt: 0,
      },
      undefined,
      async attempt => {
        await new Promise<void>(resolve => {
          attempt.controller.signal.addEventListener('abort', () => resolve(), { once: true })
        })
        return { authoritative: false }
      },
    )
    const eventHandle = services.events.open('a')
    eventHandle.close()
    services.subscriptions.cancel()
    await expect(first).resolves.toEqual({ authoritative: false })
  })

  it('dispose is idempotent, detaches events, and cancels attempts', async () => {
    const harness = sourceHarness()
    const services = runtime(harness.source)
    const errors = vi.fn()
    services.events.observeDecodeError(errors)
    let aborted = false
    const pending = services.subscriptions.start(
      {
        key: 'a',
        sinceStreamGeneration: null,
        sinceStreamSeq: 0,
        bootstrapGeneration: 1,
        bootstrapAttempt: 0,
      },
      undefined,
      async attempt => {
        await new Promise<void>(resolve => {
          attempt.controller.signal.addEventListener('abort', () => {
            aborted = true
            resolve()
          }, { once: true })
        })
        return { authoritative: false }
      },
    )

    services.dispose()
    services.dispose()
    await pending

    expect(aborted).toBe(true)
    expect(services.subscriptions.leases.size).toBe(0)
    expect(harness.counts()).toEqual({ subscriptions: 1, detachments: 1 })
  })
})
