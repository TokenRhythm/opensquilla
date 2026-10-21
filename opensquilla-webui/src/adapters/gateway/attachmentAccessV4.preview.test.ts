import { afterEach, describe, expect, it, vi } from 'vitest'
import { fetchDisplayAttachmentBlob, readDisplayAttachmentPreview } from './attachmentAccessV4'
import { createPrivateHttpTransport } from './privateHttpTransport'
import type { DisplayAttachment } from '@/types/chat'

const attachment: DisplayAttachment = { kind: 'file', displayId: 'snapshot', renderKey: 'snapshot', name: 'a.txt', mime: 'text/plain', sha256_ref: 'a'.repeat(64) }
const options = { sessionKey: 'session-a', baseOrigin: 'http://localhost' }
afterEach(() => { vi.useRealTimers() })
function response(raw: unknown, contentLength?: number) {
  const bytes = new TextEncoder().encode(JSON.stringify(raw))
  return { metadata: { status: 200, contentLength }, blob: vi.fn(),
    stream: () => new ReadableStream<Uint8Array>({ start(controller) { controller.enqueue(bytes); controller.close() } }) }
}

describe('bounded attachment preview transport', () => {
  it('addresses just the selected legacy call without confusing character count with bytes', async () => {
    const http = { requestBinary: vi.fn().mockResolvedValue(response({ content: '中', truncated: false, totalBytes: 3 })) }
    await expect(readDisplayAttachmentPreview(http, { ...attachment,
      fileSnapshotRef: { messageId: 'message-1', toolUseId: 'call/1' } }, options))
      .resolves.toEqual({ content: '中', truncated: false, totalBytes: 3 })
    expect(http.requestBinary).toHaveBeenCalledWith(`/api/v1/attachments/${'a'.repeat(64)}?messageId=message-1&toolUseId=call%2F1&preview=1`,
      expect.objectContaining({ sessionKey: 'session-a' }))
  })
  it('requests the semantic preview and preserves saved BOM, CRLF and Unicode', async () => {
    const content = '\uFEFF中🙂\r\n'
    const http = { requestBinary: vi.fn().mockResolvedValue(response({ content, truncated: false, totalBytes: new TextEncoder().encode(content).length })) }
    expect(await readDisplayAttachmentPreview(http, attachment, options)).toEqual({ content, truncated: false, totalBytes: 12 })
    expect(http.requestBinary).toHaveBeenCalledWith(`/api/v1/attachments/${'a'.repeat(64)}?preview=1`, expect.objectContaining({ sessionKey: 'session-a' }))
  })
  it('defensively bounds a server preview that exceeds rendering budgets', async () => {
    const content = 'x'.repeat(10000)
    const http = { requestBinary: vi.fn().mockResolvedValue(response({ content, truncated: false, totalBytes: content.length })) }
    expect(await readDisplayAttachmentPreview(http, attachment, options)).toEqual({ content: 'x'.repeat(8192), truncated: true, totalBytes: 10000 })
  })
  it('rejects an incomplete response rather than treating it as complete', async () => {
    const http = { requestBinary: vi.fn().mockResolvedValue(response({ content: 'short', truncated: false, totalBytes: 99 })) }
    await expect(readDisplayAttachmentPreview(http, attachment, options)).rejects.toThrow('Incomplete')
  })
  it('rejects oversized declared bodies without buffering them', async () => {
    const cancel = vi.fn()
    const blob = vi.fn()
    const stream = new ReadableStream<Uint8Array>({ cancel })
    const http = { requestBinary: vi.fn().mockResolvedValue({ metadata: { status: 200, contentLength: 3 * 1024 * 1024 }, blob, stream: () => stream }) }
    await expect(readDisplayAttachmentPreview(http, attachment, options)).rejects.toMatchObject({ code: 'too_large' })
    expect(cancel).toHaveBeenCalled()
    expect(blob).not.toHaveBeenCalled()
  })
  it('bounds chunked bodies with a dishonest Content-Length and cancels at the cap', async () => {
    const cancel = vi.fn()
    let chunks = 0
    const stream = new ReadableStream<Uint8Array>({ pull(controller) { chunks += 1; controller.enqueue(new Uint8Array(512 * 1024)) }, cancel }, { highWaterMark: 0 })
    const http = { requestBinary: vi.fn().mockResolvedValue({ metadata: { status: 200, contentLength: 1 }, blob: vi.fn(), stream: () => stream }) }
    await expect(readDisplayAttachmentPreview(http, attachment, options)).rejects.toMatchObject({ code: 'too_large' })
    expect(chunks).toBe(5)
    expect(cancel).toHaveBeenCalled()
  })
  it('refuses an unbounded blob fallback when streaming is unavailable', async () => {
    const blob = vi.fn()
    const http = { requestBinary: vi.fn().mockResolvedValue({ metadata: { status: 200 }, blob, stream: () => null }) }
    await expect(readDisplayAttachmentPreview(http, attachment, options)).rejects.toThrow('streaming')
    expect(blob).not.toHaveBeenCalled()
  })
  it('cancels a stalled body on close', async () => {
    const cancel = vi.fn()
    const controller = new AbortController()
    const stream = new ReadableStream<Uint8Array>({ cancel })
    const http = { requestBinary: vi.fn().mockResolvedValue({ metadata: { status: 200 }, blob: vi.fn(), stream: () => stream }) }
    const read = readDisplayAttachmentPreview(http, attachment, { ...options, signal: controller.signal })
    await Promise.resolve()
    controller.abort()
    await expect(read).rejects.toMatchObject({ name: 'AbortError' })
    expect(cancel).toHaveBeenCalled()
  })

  it('uses the existing Gateway deadline when preview response headers stall', async () => {
    vi.useFakeTimers()
    let requestSignal!: AbortSignal
    const http = createPrivateHttpTransport({ baseUrl: options.baseOrigin, authToken: () => '',
      fetch: vi.fn((_url, init) => new Promise<Response>((_resolve, reject) => {
        requestSignal = init!.signal!
        requestSignal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true })
      })) })
    const read = readDisplayAttachmentPreview(http, attachment, options)
    const failure = expect(read).rejects.toMatchObject({ kind: 'timeout' })
    await vi.advanceTimersByTimeAsync(14_999)
    expect(requestSignal.aborted).toBe(false)
    await vi.advanceTimersByTimeAsync(1)
    await failure
    expect(requestSignal.aborted).toBe(true)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('keeps the deadline active after preview headers and cancels a stalled body', async () => {
    vi.useFakeTimers()
    const cancel = vi.fn()
    const http = createPrivateHttpTransport({ baseUrl: options.baseOrigin, authToken: () => '',
      fetch: vi.fn(async () => new Response(new ReadableStream<Uint8Array>({ cancel }))) })
    const read = readDisplayAttachmentPreview(http, attachment, options)
    const failure = expect(read).rejects.toMatchObject({ kind: 'timeout' })
    await vi.advanceTimersByTimeAsync(15_000)
    await failure
    expect(cancel).toHaveBeenCalled()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('does not impose the preview deadline on a full attachment download', async () => {
    vi.useFakeTimers()
    let body!: ReadableStreamDefaultController<Uint8Array>
    let requestSignal!: AbortSignal
    const cancel = vi.fn()
    const http = createPrivateHttpTransport({ baseUrl: options.baseOrigin, authToken: () => '',
      fetch: vi.fn(async (_url, init) => {
        requestSignal = init!.signal!
        return new Response(new ReadableStream<Uint8Array>({ start(controller) { body = controller }, cancel }))
      }) })
    const read = fetchDisplayAttachmentBlob(http, attachment, options)
    await vi.advanceTimersByTimeAsync(30_000)
    expect(requestSignal.aborted).toBe(false)
    expect(cancel).not.toHaveBeenCalled()
    body.enqueue(new TextEncoder().encode('complete file'))
    body.close()
    const downloaded = await read
    expect(downloaded.ok).toBe(true)
    if (downloaded.ok) expect(await downloaded.blob.text()).toBe('complete file')
    expect(vi.getTimerCount()).toBe(0)
  })
})
