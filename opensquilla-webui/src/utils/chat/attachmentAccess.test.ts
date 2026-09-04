import { describe, expect, it, vi } from 'vitest'
import type { DisplayAttachment } from '@/types/chat'
import {
  httpTransportTestDouble,
  type TestHttpTransport,
} from '@/testing/httpTransport.test-helper'
import {
  attachmentAccessUrl,
  fetchDisplayAttachmentBlob,
} from '@/adapters/gateway/attachmentAccessV4'

function httpTransport(
  requestBinary = vi.fn<TestHttpTransport['requestBinary']>(),
): TestHttpTransport {
  return httpTransportTestDouble({ requestBinary })
}

function attachment(overrides: Partial<DisplayAttachment> = {}): DisplayAttachment {
  return {
    kind: 'file',
    displayId: 'attachment-1',
    renderKey: 'attachment-1',
    name: 'report.txt',
    mime: 'text/plain',
    ...overrides,
  }
}

describe('attachmentAccessUrl', () => {
  it('accepts only same-origin HTTP(S) URLs and strips credential query values', () => {
    expect(attachmentAccessUrl(
      '/api/v1/attachments/a?token=old&access_token=old&session=old&sessionKey=one&session_key=two&session_id=three&variant=download#secret',
      'http://127.0.0.1:18793',
    )).toBe('/api/v1/attachments/a?variant=download')
    expect(attachmentAccessUrl('https://files.example.test/a', 'http://127.0.0.1:18793')).toBe('')
    expect(attachmentAccessUrl('javascript:alert(1)', 'http://127.0.0.1:18793')).toBe('')
    expect(attachmentAccessUrl('data:text/html,payload', 'http://127.0.0.1:18793')).toBe('')
  })
})

describe('fetchDisplayAttachmentBlob', () => {
  it('prefers the local file over inline bytes and staged URLs', async () => {
    const localFile = new File(['local'], '../local.html', { type: 'text/html' })
    const http = httpTransport()

    const result = await fetchDisplayAttachmentBlob(http, attachment({
      name: '../local.html',
      localFile,
      downloadData: 'aW5saW5l',
      download_url: '/api/v1/attachments/a',
    }), { baseOrigin: 'http://127.0.0.1:18793' })

    expect(result.ok).toBe(true)
    if (result.ok) {
      expect(result.source).toBe('local-file')
      expect(result.blob).toBe(localFile)
      expect(result.filename).toBe('local.html')
    }
    expect(http.requestBinary).not.toHaveBeenCalled()
  })

  it('decodes inline HTML bytes to a Blob without constructing a data URL', async () => {
    const result = await fetchDisplayAttachmentBlob(httpTransport(), attachment({
      name: 'page.html',
      mime: 'text/html',
      downloadData: 'PGgxPk9LPC9oMT4=',
    }))

    expect(result.ok).toBe(true)
    if (result.ok) {
      expect(result.source).toBe('inline')
      expect(result.blob.type).toBe('text/html')
      expect(await result.blob.text()).toBe('<h1>OK</h1>')
    }
  })

  it('fetches staged bytes with sanitized URL and WebUI credentials', async () => {
    const blob = new Blob(['staged'], { type: 'application/pdf' })
    const response = {
      metadata: {
        status: 200,
        contentLength: blob.size,
        contentType: blob.type,
        filename: 'server.pdf',
      },
      blob: async () => blob,
      stream: () => blob.stream(),
    }
    const requestBinary = vi.fn(async () => response)
    const http = httpTransport(requestBinary)

    const result = await fetchDisplayAttachmentBlob(http, attachment({
      kind: 'staged',
      name: 'fallback.pdf',
      mime: 'application/pdf',
      download_url: '/api/v1/attachments/a?token=old&sessionKey=old',
    }), {
      baseOrigin: 'http://127.0.0.1:18793',
      sessionKey: 'agent:main:webchat:ok',
    })

    expect(result.ok).toBe(true)
    expect(requestBinary).toHaveBeenCalledWith('/api/v1/attachments/a', {
      sessionKey: 'agent:main:webchat:ok',
      signal: undefined,
      timeoutMs: 0,
    })
    if (result.ok) expect(result.filename).toBe('server.pdf')
  })

  it('fails closed before fetch for cross-origin staged URLs', async () => {
    const http = httpTransport()
    const result = await fetchDisplayAttachmentBlob(http, attachment({
      download_url: 'https://files.example.test/report.txt?token=secret',
    }), { baseOrigin: 'http://127.0.0.1:18793' })

    expect(result.ok).toBe(false)
    expect(http.requestBinary).not.toHaveBeenCalled()
  })
})
