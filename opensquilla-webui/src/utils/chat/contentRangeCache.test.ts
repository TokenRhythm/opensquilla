import { describe, expect, it, vi } from 'vitest'
import { ContentRangeCache } from './contentRangeCache'

const ref = {
  version: 1 as const,
  sessionKey: 'agent:main:webchat:test',
  sessionId: 'session-1',
  messageId: 'message-1',
}

describe('ContentRangeCache', () => {
  it('reads semantic details with the same revision fence and bounded stream as display bodies', async () => {
    const value = { message_id: ref.messageId, role: 'assistant', reasoning_content: '思考'.repeat(15_000), tool_calls: [] }
    const fetcher = vi.fn(async () => new Response(JSON.stringify(value)))
    const cache = new ContentRangeCache({ fetcher })
    await expect(cache.readDetails({ ...ref, revision: 'r1' })).resolves.toMatchObject({ reasoning: value.reasoning_content })
    expect(fetcher).toHaveBeenCalledWith(expect.stringContaining('&revision=r1&view=details&export=1'), expect.anything())
    await expect(new ContentRangeCache({ fetcher, maxBytes: 1_024 }).readDetails(ref)).rejects.toThrow('too large')
  })

  it.each([
    { message_id: 'other', role: 'assistant', tool_calls: [] },
    { message_id: ref.messageId, role: 'user', tool_calls: [] },
    { message_id: ref.messageId, role: 'assistant', tool_calls: [null] },
  ])('rejects unrelated or malformed detail projections', async value => {
    const cache = new ContentRangeCache({ fetcher: async () => new Response(JSON.stringify(value)) })
    await expect(cache.readDetails(ref)).rejects.toThrow('invalid details')
  })
  it('coalesces identical in-flight reads and bounds the range', async () => {
    let release!: () => void
    const fetcher = vi.fn(() => new Promise<Response>(resolve => {
      release = () => resolve(new Response(new Uint8Array([1, 2, 3]), { status: 206 }))
    })) as unknown as typeof fetch
    const cache = new ContentRangeCache({ fetcher, chunkBytes: 4 })
    const first = cache.read(ref, { offset: 4, limit: 99 })
    const second = cache.read(ref, { offset: 4, limit: 99 })
    expect(fetcher).toHaveBeenCalledTimes(1)
    release()
    await expect(Promise.all([first, second])).resolves.toEqual([
      new Uint8Array([1, 2, 3]),
      new Uint8Array([1, 2, 3]),
    ])
  })

  it('evicts old chunks and rejects oversized responses', async () => {
    const fetcher = vi.fn(async () => new Response(new Uint8Array([1, 2, 3]), { status: 206 })) as unknown as typeof fetch
    const cache = new ContentRangeCache({ fetcher, chunkBytes: 4, maxEntries: 1 })
    await cache.read(ref, { offset: 0, limit: 3 })
    await cache.read(ref, { offset: 3, limit: 3 })
    await cache.read(ref, { offset: 0, limit: 3 })
    expect(fetcher).toHaveBeenCalledTimes(3)
  })

  it('enforces the aggregate byte cap even when entry count is within bounds', async () => {
    const fetcher = vi.fn(async () => new Response(new Uint8Array([1, 2, 3, 4]), { status: 206 })) as unknown as typeof fetch
    const cache = new ContentRangeCache({ fetcher, chunkBytes: 4, maxEntries: 10, maxBytes: 5 })
    await cache.read(ref, { offset: 0, limit: 4 })
    await cache.read(ref, { offset: 4, limit: 4 })
    // The first 4-byte entry was evicted to keep the total at <= 5 bytes.
    await cache.read(ref, { offset: 0, limit: 4 })
    expect(fetcher).toHaveBeenCalledTimes(3)
  })

  it('uses the semantic display view instead of raw ranges', async () => {
    const fetcher = vi.fn(async () => new Response('shown text', { status: 200 })) as unknown as typeof fetch
    const cache = new ContentRangeCache({ fetcher })
    await expect(cache.readDisplay({ ...ref, view: 'display' })).resolves.toBe('shown text')
    expect(fetcher).toHaveBeenCalledWith(
      expect.stringContaining('&view=display&export=1'),
      expect.objectContaining({ method: 'GET' }),
    )
  })

  it('invokes the default browser fetch with its global owner', async () => {
    const originalFetch = globalThis.fetch
    const owner = globalThis
    const fetchSpy = vi.fn(function (this: typeof globalThis) {
      if (this !== owner) throw new TypeError('Illegal invocation')
      return Promise.resolve(new Response('shown text', { status: 200 }))
    }) as unknown as typeof fetch
    globalThis.fetch = fetchSpy
    try {
      const cache = new ContentRangeCache()
      await expect(cache.readDisplay({ ...ref, view: 'display' })).resolves.toBe('shown text')
      expect(fetchSpy).toHaveBeenCalledTimes(1)
    } finally {
      globalThis.fetch = originalFetch
    }
  })

  it('bounds a streamed display response before retaining the transformed row', async () => {
    const fetcher = vi.fn(async () => new Response('123456', { status: 200 })) as unknown as typeof fetch
    const cache = new ContentRangeCache({ fetcher, maxBytes: 5 })
    await expect(cache.readDisplay({ ...ref, view: 'display' }))
      .rejects.toThrow('display is too large')
  })

  it('releases the display stream reader after a successful read', async () => {
    let response!: Response
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new TextEncoder().encode('shown text'))
        controller.close()
      },
    })
    const fetcher = vi.fn(async () => {
      response = new Response(stream, { status: 200 })
      return response
    }) as unknown as typeof fetch
    const cache = new ContentRangeCache({ fetcher })
    await expect(cache.readDisplay({ ...ref, view: 'display' })).resolves.toBe('shown text')
    // A locked body would throw here.  Releasing the reader is part of the
    // successful read contract so the renderer can reclaim external buffers.
    expect(() => response.body?.getReader()).not.toThrow()
  })

  it('cancels an unread display stream after the renderer byte cap is exceeded', async () => {
    let cancelCalls = 0
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new TextEncoder().encode('123456'))
      },
      cancel() {
        cancelCalls += 1
      },
    })
    const fetcher = vi.fn(async () => new Response(stream, { status: 200 })) as unknown as typeof fetch
    const cache = new ContentRangeCache({ fetcher, maxBytes: 5 })
    await expect(cache.readDisplay({ ...ref, view: 'display' }))
      .rejects.toThrow('display is too large')
    expect(cancelCalls).toBe(1)
  })

  it('cancels a declared oversized display body before reading it', async () => {
    let cancelCalls = 0
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new TextEncoder().encode('123456'))
      },
      cancel() {
        cancelCalls += 1
      },
    })
    const fetcher = vi.fn(async () => new Response(stream, {
      status: 200,
      headers: { 'content-length': '6' },
    })) as unknown as typeof fetch
    const cache = new ContentRangeCache({ fetcher, maxBytes: 5 })
    await expect(cache.readDisplay({ ...ref, view: 'display' }))
      .rejects.toThrow('display is too large')
    expect(cancelCalls).toBe(1)
  })

  it('preserves a short range response for the next read instead of skipping bytes', async () => {
    const chunks = [new Uint8Array([0, 1]), new Uint8Array([2, 3]), new Uint8Array([4])]
    const fetcher = vi.fn(async (url: string) => {
      const offset = Number(new URL(url, 'http://localhost').searchParams.get('offset'))
      const value = chunks[offset / 2] ?? new Uint8Array()
      return new Response(value, { status: 206 })
    }) as unknown as typeof fetch
    const cache = new ContentRangeCache({ fetcher, chunkBytes: 4 })
    await expect(cache.readText({ ...ref, byteLength: 5 }, { })).resolves.toBe('\u0000\u0001\u0002\u0003\u0004')
    expect(fetcher).toHaveBeenCalledTimes(3)
  })

  it('releases a raw range reader after a successful bounded read', async () => {
    let response!: Response
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new Uint8Array([1, 2]))
        controller.close()
      },
    })
    const fetcher = vi.fn(async () => {
      response = new Response(stream, { status: 206 })
      return response
    }) as unknown as typeof fetch
    const cache = new ContentRangeCache({ fetcher, chunkBytes: 4 })
    await expect(cache.read(ref, { limit: 2 })).resolves.toEqual(new Uint8Array([1, 2]))
    expect(() => response.body?.getReader()).not.toThrow()
  })

  it('cancels and releases an oversized raw range stream', async () => {
    let cancelCalls = 0
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new Uint8Array([1, 2, 3]))
      },
      cancel() {
        cancelCalls += 1
      },
    })
    const fetcher = vi.fn(async () => new Response(stream, { status: 206 })) as unknown as typeof fetch
    const cache = new ContentRangeCache({ fetcher, chunkBytes: 2 })
    await expect(cache.read(ref, { limit: 2 })).rejects.toThrow('oversized range')
    expect(cancelCalls).toBe(1)
  })

  it('reports the existing whole-text capacity without fetching or changing its budget', async () => {
    const fetcher = vi.fn() as unknown as typeof fetch
    const cache = new ContentRangeCache({ fetcher })
    expect(cache.canReadText({ ...ref, byteLength: 8 * 1024 * 1024 })).toBe(true)
    expect(cache.canReadText({ ...ref, byteLength: 8 * 1024 * 1024 + 1 })).toBe(false)
    await expect(cache.readText({ ...ref, byteLength: 128 * 1024 * 1024 })).rejects.toThrow('too large')
    expect(fetcher).not.toHaveBeenCalled()
  })

  it('does not let the first aborted waiter poison a shared range request', async () => {
    let release!: () => void
    const fetcher = vi.fn(() => new Promise<Response>(resolve => {
      release = () => resolve(new Response(new Uint8Array([1, 2]), { status: 206 }))
    })) as unknown as typeof fetch
    const cache = new ContentRangeCache({ fetcher, chunkBytes: 2 })
    const firstController = new AbortController()
    const first = cache.read(ref, { offset: 0, limit: 2, signal: firstController.signal })
    const second = cache.read(ref, { offset: 0, limit: 2 })
    firstController.abort()
    release()
    await expect(first).rejects.toMatchObject({ name: 'AbortError' })
    await expect(second).resolves.toEqual(new Uint8Array([1, 2]))
    expect(fetcher).toHaveBeenCalledTimes(1)
  })

  it('does not repopulate a cleared cache with an old in-flight response', async () => {
    let release!: () => void
    let calls = 0
    const fetcher = vi.fn(() => {
      calls += 1
      if (calls === 1) return new Promise<Response>(resolve => {
        release = () => resolve(new Response(new Uint8Array([1]), { status: 206 }))
      })
      return Promise.resolve(new Response(new Uint8Array([1]), { status: 206 }))
    }) as unknown as typeof fetch
    const cache = new ContentRangeCache({ fetcher, chunkBytes: 1 })
    const pending = cache.read(ref, { offset: 0, limit: 1 })
    cache.clear()
    release()
    await pending
    await cache.read(ref, { offset: 0, limit: 1 })
    expect(fetcher).toHaveBeenCalledTimes(2)
  })

  it('keeps revisions with equal byte lengths in separate cache entries', async () => {
    const fetcher = vi.fn(async (url: string) => {
      const revision = new URL(url, 'http://localhost').searchParams.get('revision')
      return new Response(new TextEncoder().encode(revision === 'r2' ? 'new' : 'old'), { status: 206 })
    }) as unknown as typeof fetch
    const cache = new ContentRangeCache({ fetcher, chunkBytes: 3 })
    await expect(cache.read({ ...ref, byteLength: 3, revision: 'r1' }, { limit: 3 }))
      .resolves.toEqual(new TextEncoder().encode('old'))
    await expect(cache.read({ ...ref, byteLength: 3, revision: 'r2' }, { limit: 3 }))
      .resolves.toEqual(new TextEncoder().encode('new'))
    expect(fetcher).toHaveBeenCalledTimes(2)
  })
})
