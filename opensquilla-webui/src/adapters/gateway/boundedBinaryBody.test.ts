import { describe, expect, it, vi } from 'vitest'
import { BinaryBodyTooLargeError, readBinaryBlob, type ReadableBinaryBody } from './boundedBinaryBody'

function body(parts: string[], contentLength?: number) {
  const cancel = vi.fn()
  let index = 0
  const stream = new ReadableStream<Uint8Array>({
    pull(controller) {
      const part = parts[index++]
      if (part === undefined) controller.close()
      else controller.enqueue(new TextEncoder().encode(part))
    },
    cancel,
  }, { highWaterMark: 0 })
  const response: ReadableBinaryBody = {
    metadata: { contentLength, contentType: 'image/png' },
    stream: vi.fn(() => stream),
    blob: vi.fn(async () => new Blob(parts, { type: 'image/png' })),
  }
  return { response, cancel }
}

describe('bounded binary reads', () => {
  it('leaves ordinary downloads on their existing Blob path', async () => {
    const { response } = body(['full', ' file'], 9)
    expect(await (await readBinaryBlob(response)).text()).toBe('full file')
    expect(response.blob).toHaveBeenCalledOnce()
    expect(response.stream).not.toHaveBeenCalled()
  })

  it('rejects advertised oversized content before reading and cancels the body', async () => {
    const { response, cancel } = body(['large'], 5)
    await expect(readBinaryBlob(response, { maxBytes: 4 })).rejects.toBeInstanceOf(BinaryBodyTooLargeError)
    expect(response.blob).not.toHaveBeenCalled()
    expect(cancel).toHaveBeenCalledOnce()
  })

  it.each([undefined, 1])('enforces actual streamed bytes with content length %s', async contentLength => {
    const { response, cancel } = body(['123', '456'], contentLength)
    await expect(readBinaryBlob(response, { maxBytes: 5 })).rejects.toBeInstanceOf(BinaryBodyTooLargeError)
    expect(cancel).toHaveBeenCalledOnce()
    expect(response.blob).not.toHaveBeenCalled()
  })

  it('accepts the exact byte ceiling and retains the response MIME', async () => {
    const { response } = body(['12', '345'], 5)
    const result = await readBinaryBlob(response, { maxBytes: 5 })
    expect(result.size).toBe(5)
    expect(result.type).toBe('image/png')
    expect(await result.text()).toBe('12345')
  })

  it('checks the fallback Blob size when streaming is unavailable', async () => {
    const response: ReadableBinaryBody = {
      metadata: {}, stream: () => null, blob: async () => new Blob(['12345']),
    }
    await expect(readBinaryBlob(response, { maxBytes: 4 })).rejects.toBeInstanceOf(BinaryBodyTooLargeError)
  })

  it('aborts a stalled stream promptly even if its cancellation never settles', async () => {
    const controller = new AbortController()
    const cancel = vi.fn(() => new Promise<void>(() => {}))
    const response: ReadableBinaryBody = {
      metadata: {}, blob: vi.fn(),
      stream: () => new ReadableStream({ cancel }),
    }
    const pending = readBinaryBlob(response, { maxBytes: 5, signal: controller.signal })
    controller.abort()
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' })
    expect(cancel).toHaveBeenCalledOnce()
  })

  it('aborts a non-streaming Blob read without waiting for it to resolve', async () => {
    const controller = new AbortController()
    const response: ReadableBinaryBody = {
      metadata: {}, stream: () => null, blob: () => new Promise(() => {}),
    }
    const pending = readBinaryBlob(response, { maxBytes: 5, signal: controller.signal })
    controller.abort()
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' })
  })
})
