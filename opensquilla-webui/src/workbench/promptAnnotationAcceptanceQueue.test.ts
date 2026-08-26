import { describe, expect, it } from 'vitest'

import { PromptAnnotationAcceptanceQueue } from './promptAnnotationAcceptanceQueue'

describe('PromptAnnotationAcceptanceQueue', () => {
  it('retains an acceptance until a matching artifact item is mounted', async () => {
    const queue = new PromptAnnotationAcceptanceQueue()
    queue.enqueue({
      acceptedIds: ['annotation-1'],
      sessionKey: 'session-canonical',
      requestSessionKey: 'session-draft',
    }, 0)

    const delivered: string[][] = []
    await queue.flush(
      [{ sessionKey: 'unrelated' }],
      item => item.sessionKey,
      (_item, ids) => { delivered.push([...ids]) },
      1,
    )
    expect(delivered).toEqual([])
    expect(queue.size).toBe(1)

    await queue.flush(
      [{ sessionKey: 'session-draft' }],
      item => item.sessionKey,
      (_item, ids) => { delivered.push([...ids]) },
      2,
    )
    expect(delivered).toEqual([['annotation-1']])
    expect(queue.size).toBe(0)
  })

  it('merges replayed events that share provisional or canonical identity', async () => {
    const queue = new PromptAnnotationAcceptanceQueue()
    queue.enqueue({ acceptedIds: ['annotation-1'], sessionKey: 'canonical' }, 0)
    queue.enqueue({
      acceptedIds: ['annotation-2'],
      sessionKey: 'canonical',
      requestSessionKey: 'draft',
    }, 1)
    expect(queue.size).toBe(1)

    const delivered: string[][] = []
    await queue.flush(
      [{ sessionKey: 'draft' }],
      item => item.sessionKey,
      (_item, ids) => { delivered.push([...ids]) },
      2,
    )
    expect(delivered).toEqual([['annotation-1', 'annotation-2']])
    expect(queue.size).toBe(0)
  })

  it('keeps a failed delivery for the next lifecycle pass', async () => {
    const queue = new PromptAnnotationAcceptanceQueue()
    queue.enqueue({ acceptedIds: ['annotation-1'], sessionKey: 'session' }, 0)
    let fail = true
    const item = { sessionKey: 'session' }
    const deliver = () => !fail

    await queue.flush([item], value => value.sessionKey, deliver, 1)
    expect(queue.size).toBe(1)
    fail = false
    await queue.flush([item], value => value.sessionKey, deliver, 2)
    expect(queue.size).toBe(0)
  })

  it('does not delete a replay enqueued while delivery is awaiting the runtime', async () => {
    const queue = new PromptAnnotationAcceptanceQueue()
    queue.enqueue({ acceptedIds: ['annotation-1'], sessionKey: 'session' }, 0)
    let releaseDelivery: () => void = () => undefined
    const deliveryStarted = new Promise<void>(resolve => {
      releaseDelivery = resolve
    })
    const firstFlush = queue.flush(
      [{ sessionKey: 'session' }],
      item => item.sessionKey,
      async () => {
        await deliveryStarted
      },
      1,
    )
    // Let the callback enter its await before replaying the response.
    await Promise.resolve()
    queue.enqueue({ acceptedIds: ['annotation-2'], sessionKey: 'session' }, 2)
    releaseDelivery()
    await firstFlush
    expect(queue.size).toBe(1)

    const delivered: string[][] = []
    await queue.flush(
      [{ sessionKey: 'session' }],
      item => item.sessionKey,
      (_item, ids) => { delivered.push([...ids]) },
      3,
    )
    expect(delivered).toEqual([['annotation-1', 'annotation-2']])
    expect(queue.size).toBe(0)
  })

  it('expires disconnected handoffs and bounds retained entries', async () => {
    const queue = new PromptAnnotationAcceptanceQueue({ maxEntries: 2, ttlMs: 10 })
    queue.enqueue({ acceptedIds: ['a'], sessionKey: 'a' }, 0)
    queue.enqueue({ acceptedIds: ['b'], sessionKey: 'b' }, 1)
    queue.enqueue({ acceptedIds: ['c'], sessionKey: 'c' }, 2)
    expect(queue.size).toBe(2)

    await queue.flush(
      [{ sessionKey: 'b' }, { sessionKey: 'c' }],
      item => item.sessionKey,
      () => undefined,
      20,
    )
    expect(queue.size).toBe(0)
  })
})
