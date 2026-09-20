import { describe, expect, it, vi } from 'vitest'
import { createWorkingFileAccess } from './privateWorkingFileAccess'
import { HttpTransportError } from './privateHttpTransport'
import { BinaryBodyTooLargeError } from './boundedBinaryBody'

const request = { sessionKey: 'agent:main:webchat:fixture', documentId: 'doc_one', pagePath: 'pages/editorial.html' }
const info = { documentId: 'doc_one', pagePath: request.pagePath, workspace: '/task',
  sourcePath: '/task/site/pages/editorial.html', name: 'editorial.html', mime: 'text/html', size: 20 }
describe('working file transport', () => {
  it('binds page, document, session and cancellation to the authenticated transport', async () => {
    const blob = new Blob([JSON.stringify(info)], { type: 'application/json' })
    const http = { requestBinary: vi.fn().mockResolvedValue({ blob: async () => blob }) }
    const content = createWorkingFileAccess(http)
    const signal = new AbortController().signal
    expect(await content.workingFileMetadata!({ ...request, signal })).toEqual(info)
    expect(http.requestBinary).toHaveBeenCalledWith(
      '/api/v1/artifact-documents/doc_one/working-file?format=metadata&pagePath=pages%2Feditorial.html',
      { sessionKey: request.sessionKey, signal })
    expect(await content.fetchWorkingFile!({ ...request, signal })).toBe(blob)
    expect(http.requestBinary.mock.lastCall![0]).toContain('format=content')
  })
  it.each([404, 405, 501])('handles missing old Gateway capability (%s) without changing sources', async status => {
    const http = { requestBinary: vi.fn().mockRejectedValue(new HttpTransportError('http-status', 'unavailable', status)) }
    expect(await createWorkingFileAccess(http).workingFileMetadata!(request)).toBeNull()
    expect(http.requestBinary).toHaveBeenCalledOnce()
  })
  it.each([{ documentId: 'doc_other' }, { pagePath: 'index.html' }, { size: -1 }])('rejects mismatched metadata %j', async patch => {
    const http = { requestBinary: vi.fn().mockResolvedValue({ blob: async () => new Blob([JSON.stringify({ ...info, ...patch })]) }) }
    await expect(createWorkingFileAccess(http).workingFileMetadata!(request)).rejects.toThrow('Invalid working file metadata')
  })
  it('does not fetch untrusted paths or hide authorization failures', async () => {
    const http = { requestBinary: vi.fn().mockRejectedValue(new HttpTransportError('http-status', 'forbidden', 403)) }
    const content = createWorkingFileAccess(http)
    await expect(content.workingFileMetadata!({ ...request, pagePath: '../secret.html' })).rejects.toThrow('Invalid working file identity')
    expect(http.requestBinary).not.toHaveBeenCalled()
    await expect(content.workingFileMetadata!(request)).rejects.toThrow('forbidden')
  })

  it.each([undefined, 1, 6])('bounds authenticated working-file HTTP bytes with reported length %s', async contentLength => {
    const cancelled = vi.fn()
    let index = 0
    const body = new ReadableStream<Uint8Array>({
      pull(controller) {
        if (index++ < 2) controller.enqueue(new TextEncoder().encode('123'))
        else controller.close()
      },
      cancel: cancelled,
    }, { highWaterMark: 0 })
    const response = {
      metadata: { contentLength, contentType: 'image/svg+xml' },
      stream: () => body,
      blob: vi.fn(async () => new Blob(['123456'], { type: 'image/svg+xml' })),
    }
    const http = { requestBinary: vi.fn(async () => response) }
    const signal = new AbortController().signal
    await expect(createWorkingFileAccess(http).fetchWorkingFile!({ ...request, maxBytes: 5, signal }))
      .rejects.toBeInstanceOf(BinaryBodyTooLargeError)
    expect(cancelled).toHaveBeenCalledOnce()
    expect(response.blob).not.toHaveBeenCalled()
    expect(http.requestBinary).toHaveBeenCalledWith(
      '/api/v1/artifact-documents/doc_one/working-file?format=content&pagePath=pages%2Feditorial.html',
      { sessionKey: request.sessionKey, signal, timeoutMs: 0 })
  })

  it('preserves exact bytes and MIME at the working-file ceiling', async () => {
    const original = new Blob(['<svg/>'], { type: 'image/svg+xml' })
    const response = { metadata: { contentLength: original.size, contentType: original.type },
      stream: () => original.stream(), blob: vi.fn(async () => original) }
    const content = createWorkingFileAccess({ requestBinary: vi.fn(async () => response) })
    const result = await content.fetchWorkingFile!({ ...request, maxBytes: original.size })
    expect(result.type).toBe('image/svg+xml')
    expect(await result.text()).toBe('<svg/>')
    expect(response.blob).not.toHaveBeenCalled()
  })

  it('retains blob-only compatibility and still checks bounded fallback bytes', async () => {
    const original = new Blob(['<svg/>'])
    const content = createWorkingFileAccess({ requestBinary: vi.fn(async () => ({ blob: async () => original })) })
    expect(await content.fetchWorkingFile!(request)).toBe(original)
    await expect(content.fetchWorkingFile!({ ...request, maxBytes: 5 })).rejects.toBeInstanceOf(BinaryBodyTooLargeError)
    await expect(content.fetchWorkingFile!({ ...request, maxBytes: 5 })).rejects.toMatchObject({ code: 'too_large' })
  })

  it('cancels a stalled working-file stream without waiting for more bytes', async () => {
    const controller = new AbortController()
    const cancelled = vi.fn()
    const response = { metadata: {}, stream: () => new ReadableStream<Uint8Array>({ cancel: cancelled }),
      blob: vi.fn() }
    const content = createWorkingFileAccess({ requestBinary: vi.fn(async () => response) })
    const pending = content.fetchWorkingFile!({ ...request, maxBytes: 5, signal: controller.signal })
    await Promise.resolve()
    controller.abort()
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' })
    expect(cancelled).toHaveBeenCalledOnce()
  })
})
