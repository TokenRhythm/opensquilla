// @vitest-environment happy-dom
import { describe, expect, it, vi } from 'vitest'
import { createArtifactPreviewResource, MARKDOWN_IMAGE_BYTE_LIMIT, MARKDOWN_IMAGE_COUNT_LIMIT, MARKDOWN_IMAGES_TOTAL_BYTE_LIMIT } from './artifactPreviewResourceV4'
import { httpBinaryResponse, httpTransportTestDouble, type TestHttpBinaryResponse } from '@/testing/httpTransport.test-helper'
import { applyMarkdownImageResources } from '@/utils/workbench/markdownImageResources'

// Test the resource boundary with the already-sanitized placeholder contract;
// native browser tests exercise Markdown parsing/sanitization end to end.
vi.mock('@/utils/markdown/renderCore', () => ({ renderMarkdownCore: (source: string) => source }))

const image = (source: string, alt = 'Chart') => `<span data-markdown-image="${source}" data-markdown-image-alt="${alt}">${alt}</span>`
function mount(source: string, imageResponse?: (url: string, signal?: AbortSignal) => Promise<TestHttpBinaryResponse>) {
  const createObjectUrl = vi.fn((_blob: Blob) => `blob:image-${createObjectUrl.mock.calls.length}`)
  const revokeObjectUrl = vi.fn()
  const requestBinary = vi.fn(async (url: string, options?: { signal?: AbortSignal }) => url === '/api/v1/artifacts/notes'
    ? httpBinaryResponse(source, { contentType: 'text/markdown' })
    : imageResponse ? imageResponse(url, options?.signal) : httpBinaryResponse('<svg xmlns="http://www.w3.org/2000/svg"/>', { contentType: 'image/svg+xml' }))
  const controller = createArtifactPreviewResource(httpTransportTestDouble({ requestBinary }), {
    artifact: () => ({ id: 'notes', name: 'notes.md', mime: 'text/markdown' }),
    baseOrigin: () => 'https://gateway.example', sessionKey: () => 'session-a',
    createObjectUrl, revokeObjectUrl,
  })
  return { controller, requestBinary, createObjectUrl, revokeObjectUrl }
}

describe('bounded Markdown image resources', () => {
  it('loads authorized images once, retains missing alt and external links without a request', async () => {
    const state = mount(image('/api/v1/artifacts/chart') + image('/api/v1/artifacts/chart')
      + image('https://external.invalid/tracker.png', 'External chart') + image('file:///C:/secret.png', 'Local chart'))
    await state.controller.load()
    expect(state.controller.state.value).toBe('ready')
    expect(state.requestBinary).toHaveBeenCalledTimes(2)
    expect(state.requestBinary).toHaveBeenLastCalledWith('/api/v1/artifacts/chart', expect.objectContaining({ sessionKey: 'session-a', timeoutMs: 15_000 }))
    const node = document.createElement('div')
    node.innerHTML = state.controller.markdownHtml.value
    applyMarkdownImageResources(node, state.controller.markdownImages!.value)
    expect(node.querySelectorAll('img')).toHaveLength(2)
    expect(node.querySelector('svg')).toBeNull()
    expect(node.querySelector('a')?.href).toBe('https://external.invalid/tracker.png')
    expect(node.textContent).toContain('Local chart')
    state.controller.dispose()
    expect(state.revokeObjectUrl).toHaveBeenCalledExactlyOnceWith('blob:image-1')
  })

  it('rejects oversized declared images before consuming a body', async () => {
    const body = vi.fn()
    const state = mount(image('/api/v1/artifacts/large'), async () => ({
      metadata: { status: 200, contentLength: MARKDOWN_IMAGE_BYTE_LIMIT + 1, contentType: 'image/png' },
      blob: body, stream: () => null,
    }))
    await state.controller.load()
    expect(body).not.toHaveBeenCalled()
    expect(state.createObjectUrl).not.toHaveBeenCalled()
    expect(state.controller.markdownHtml.value).toContain('Chart')
    expect(state.controller.state.value).toBe('ready')
    state.controller.dispose()
  })

  it('cancels an oversized unknown-length stream and keeps the document', async () => {
    const cancel = vi.fn()
    const state = mount(image('/api/v1/artifacts/large'), async () => ({
      metadata: { status: 200, contentType: 'image/png' },
      blob: vi.fn(),
      stream: () => new ReadableStream({ start(stream) { stream.enqueue(new Uint8Array(MARKDOWN_IMAGE_BYTE_LIMIT + 1)) }, cancel }),
    }))
    await state.controller.load()
    expect(cancel).toHaveBeenCalledOnce()
    expect(state.createObjectUrl).not.toHaveBeenCalled()
    expect(state.controller.state.value).toBe('ready')
    state.controller.dispose()
  })

  it('caps total fetched bytes and does not start another image after the budget', async () => {
    const body = new Uint8Array(MARKDOWN_IMAGE_BYTE_LIMIT)
    const state = mount(Array.from({ length: 6 }, (_, index) => image(`/api/v1/artifacts/image-${index}`)).join(''), async () => httpBinaryResponse(body, { contentType: 'image/png' }))
    await state.controller.load()
    expect(state.createObjectUrl).toHaveBeenCalledTimes(MARKDOWN_IMAGES_TOTAL_BYTE_LIMIT / MARKDOWN_IMAGE_BYTE_LIMIT)
    expect(state.requestBinary).toHaveBeenCalledTimes(5)
    state.controller.dispose()
    expect(state.revokeObjectUrl).toHaveBeenCalledTimes(4)
  })

  it('caps the number of images, retaining later alt labels', async () => {
    const state = mount(Array.from({ length: 30 }, (_, index) => image(`/api/v1/artifacts/image-${index}`, `Image ${index}`)).join(''))
    await state.controller.load()
    expect(state.requestBinary).toHaveBeenCalledTimes(MARKDOWN_IMAGE_COUNT_LIMIT + 1)
    expect(state.controller.markdownHtml.value).toContain('Image 29')
    state.controller.dispose()
  })

  it('aborts pending image work and releases prior image URLs on close', async () => {
    let finish!: (value: TestHttpBinaryResponse) => void
    let activeSignal: AbortSignal | undefined
    let started!: () => void
    const began = new Promise<void>(resolve => { started = resolve })
    const state = mount(image('/api/v1/artifacts/first') + image('/api/v1/artifacts/second'), async (url, signal) => {
      if (url.endsWith('first')) return httpBinaryResponse('first', { contentType: 'image/png' })
      activeSignal = signal
      started()
      return new Promise(resolve => { finish = resolve })
    })
    const loading = state.controller.load()
    await began
    state.controller.dispose()
    expect(activeSignal?.aborted).toBe(true)
    finish(httpBinaryResponse('late', { contentType: 'image/png' }))
    await loading
    expect(state.controller.markdownHtml.value).toBe('')
    expect(state.createObjectUrl).toHaveBeenCalledOnce()
    expect(state.revokeObjectUrl).toHaveBeenCalledOnce()
  })

  it('publishes readable text before a slow image finishes', async () => {
    let finish!: (value: TestHttpBinaryResponse) => void
    let started!: () => void
    const began = new Promise<void>(resolve => { started = resolve })
    const state = mount('<p>Readable now</p>' + image('/api/v1/artifacts/slow'), async () => {
      started()
      return new Promise(resolve => { finish = resolve })
    })
    const loading = state.controller.load()
    await began
    expect(state.controller.state.value).toBe('ready')
    expect(state.controller.progress.value).toBe(100)
    expect(state.controller.markdownHtml.value).toContain('Readable now')
    finish(httpBinaryResponse('image', { contentType: 'image/png' }))
    await loading
    expect(state.controller.markdownImages!.value.get('/api/v1/artifacts/slow')).toBe('blob:image-1')
    state.controller.dispose()
  })

  it('publishes the first image while a later image is pending without replacing Markdown', async () => {
    let finish!: (value: TestHttpBinaryResponse) => void
    let started!: () => void
    const began = new Promise<void>(resolve => { started = resolve })
    const state = mount('<p>Readable now</p>' + image('/api/v1/artifacts/first') + image('/api/v1/artifacts/slow'), async url => {
      if (url.endsWith('/first')) return httpBinaryResponse('first', { contentType: 'image/png' })
      started()
      return new Promise(resolve => { finish = resolve })
    })
    const loading = state.controller.load()
    await began
    const html = state.controller.markdownHtml.value
    expect(state.controller.markdownImages!.value.get('/api/v1/artifacts/first')).toBe('blob:image-1')
    expect(state.controller.markdownImages!.value.has('/api/v1/artifacts/slow')).toBe(false)
    finish(httpBinaryResponse('slow', { contentType: 'image/png' }))
    await loading
    expect(state.controller.markdownHtml.value).toBe(html)
    expect(state.controller.markdownImages!.value.size).toBe(2)
    state.controller.dispose()
    expect(state.controller.markdownImages!.value.size).toBe(0)
  })

  it('resumes only interrupted images across repeated Source visits and ignores late cancelled responses', async () => {
    const attempts: { signal?: AbortSignal; finish: (response: TestHttpBinaryResponse) => void }[] = []
    const state = mount('<p>Keep this document</p>' + image('/api/v1/artifacts/first') + image('/api/v1/artifacts/slow'), async (url, signal) => {
      if (url.endsWith('/first')) return httpBinaryResponse('first', { contentType: 'image/png' })
      return new Promise(resolve => attempts.push({ signal, finish: resolve }))
    })
    try {
      const initial = state.controller.load()
      await vi.waitFor(() => expect(attempts).toHaveLength(1))
      const html = state.controller.markdownHtml.value
      const firstImage = state.controller.markdownImages!.value.get('/api/v1/artifacts/first')
      for (let index = 0; index < 2; index += 1) {
        state.controller.suspend()
        expect(attempts[index]!.signal?.aborted).toBe(true)
        await state.controller.resume()
        await vi.waitFor(() => expect(attempts).toHaveLength(index + 2))
        expect(state.controller.state.value).toBe('ready')
        expect(state.controller.markdownHtml.value).toBe(html)
        expect(state.controller.markdownImages!.value.get('/api/v1/artifacts/first')).toBe(firstImage)
        attempts[index]!.finish(httpBinaryResponse('cancelled image', { contentType: 'image/png' }))
      }
      attempts[2]!.finish(httpBinaryResponse('current image', { contentType: 'image/png' }))
      await initial
      await vi.waitFor(() => expect(state.controller.markdownImages!.value.size).toBe(2))
      expect(state.requestBinary.mock.calls.filter(([url]) => url.endsWith('/notes'))).toHaveLength(1)
      expect(state.requestBinary.mock.calls.filter(([url]) => url.endsWith('/first'))).toHaveLength(1)
      expect(state.createObjectUrl).toHaveBeenCalledTimes(2)
      expect(state.revokeObjectUrl).not.toHaveBeenCalled()
    } finally { state.controller.dispose() }
    expect(state.revokeObjectUrl).toHaveBeenCalledTimes(2)
  })

  it('retains the shared byte budget when pending images resume', async () => {
    let interrupted = false
    const body = new Uint8Array(MARKDOWN_IMAGE_BYTE_LIMIT)
    const state = mount(Array.from({ length: 6 }, (_, index) => image(`/api/v1/artifacts/image-${index}`)).join(''), async (url, signal) => {
      if (url.endsWith('image-3') && !interrupted) {
        interrupted = true
        return new Promise((_resolve, reject) => {
          signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true })
        })
      }
      return httpBinaryResponse(body, { contentType: 'image/png' })
    })
    try {
      const initial = state.controller.load()
      await vi.waitFor(() => expect(interrupted).toBe(true))
      state.controller.suspend()
      await initial
      await state.controller.resume()
      await vi.waitFor(() => expect(state.createObjectUrl).toHaveBeenCalledTimes(4))
      expect(state.controller.markdownImages!.value.size).toBe(4)
      expect(state.requestBinary.mock.calls.some(([url]) => /image-[45]$/.test(url))).toBe(false)
    } finally { state.controller.dispose() }
  })

  it('does not advance the resumed cursor when a cancelled invalid-image cleanup finishes late', async () => {
    let cleanupStarted = false
    let finishCleanup!: () => void
    let finishCurrent!: (response: TestHttpBinaryResponse) => void
    let firstAttempts = 0
    const state = mount(['one', 'two', 'three'].map(name => image(`/api/v1/artifacts/${name}`)).join(''), async url => {
      if (url.endsWith('/one')) {
        firstAttempts += 1
        if (firstAttempts === 1) return {
          metadata: { status: 200, contentType: 'text/html' },
          blob: vi.fn(),
          stream: () => new ReadableStream({ cancel() {
            cleanupStarted = true
            return new Promise<void>(resolve => { finishCleanup = resolve })
          } }),
        }
        return new Promise(resolve => { finishCurrent = resolve })
      }
      return httpBinaryResponse('image', { contentType: 'image/png' })
    })
    try {
      const initial = state.controller.load()
      await vi.waitFor(() => expect(cleanupStarted).toBe(true))
      state.controller.suspend()
      await state.controller.resume()
      await vi.waitFor(() => expect(firstAttempts).toBe(2))
      finishCleanup()
      await initial
      finishCurrent(httpBinaryResponse('one', { contentType: 'image/png' }))
      await vi.waitFor(() => expect(state.controller.markdownImages!.value.size).toBe(3))
      expect(state.requestBinary.mock.calls.map(([url]) => url)).toEqual([
        '/api/v1/artifacts/notes', '/api/v1/artifacts/one', '/api/v1/artifacts/one',
        '/api/v1/artifacts/two', '/api/v1/artifacts/three',
      ])
    } finally { state.controller.dispose() }
  })

  it('does not overwrite a newer document when a previous image resolves late', async () => {
    let finish!: (value: TestHttpBinaryResponse) => void
    let started!: () => void
    const began = new Promise<void>(resolve => { started = resolve })
    let current = 'first'
    const requestBinary = vi.fn(async (url: string) => {
      if (url === '/api/v1/artifacts/first') return httpBinaryResponse(image('/api/v1/artifacts/slow'), { contentType: 'text/markdown' })
      if (url === '/api/v1/artifacts/second') return httpBinaryResponse('<p>New document</p>', { contentType: 'text/markdown' })
      started()
      return new Promise<TestHttpBinaryResponse>(resolve => { finish = resolve })
    })
    const controller = createArtifactPreviewResource(httpTransportTestDouble({ requestBinary }), {
      artifact: () => ({ id: current, name: `${current}.md`, mime: 'text/markdown' }),
      baseOrigin: () => 'https://gateway.example', createObjectUrl: () => 'blob:stale', revokeObjectUrl: vi.fn(),
    })
    const firstLoad = controller.load()
    await began
    current = 'second'
    await controller.reload()
    finish(httpBinaryResponse('old image', { contentType: 'image/png' }))
    await firstLoad
    expect(controller.markdownHtml.value).toBe('<p>New document</p>')
    expect(controller.markdownImages!.value.size).toBe(0)
    controller.dispose()
  })

  it('rejects executable MIME types and never sends auth to external artifact-shaped URLs', async () => {
    const state = mount(image('/api/v1/artifacts/html') + image('https://evil.invalid/api/v1/artifacts/secret'), async () => httpBinaryResponse('<script>bad()</script>', { contentType: 'text/html' }))
    await state.controller.load()
    expect(state.requestBinary).toHaveBeenCalledTimes(2)
    expect(state.createObjectUrl).not.toHaveBeenCalled()
    expect(state.controller.markdownHtml.value).not.toContain('<script>')
    state.controller.dispose()
  })
})
