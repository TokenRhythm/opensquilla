import { describe, expect, it, vi } from 'vitest'
import type { DisplayAttachment } from '@/types/chat'
import {
  attachmentAccessUrl,
  fetchDisplayAttachmentBlob,
} from './attachmentAccess'

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
    const fetchImpl = vi.fn()

    const result = await fetchDisplayAttachmentBlob(attachment({
      name: '../local.html',
      localFile,
      downloadData: 'aW5saW5l',
      download_url: '/api/v1/attachments/a',
    }), { baseOrigin: 'http://127.0.0.1:18793', fetchImpl })

    expect(result.ok).toBe(true)
    if (result.ok) {
      expect(result.source).toBe('local-file')
      expect(result.blob).toBe(localFile)
      expect(result.filename).toBe('local.html')
    }
    expect(fetchImpl).not.toHaveBeenCalled()
  })

  it('decodes inline HTML bytes to a Blob without constructing a data URL', async () => {
    const result = await fetchDisplayAttachmentBlob(attachment({
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
    const fetchImpl = vi.fn(async () => new Response('staged', {
      status: 200,
      headers: {
        'content-type': 'application/pdf',
        'content-disposition': 'attachment; filename="server.pdf"',
      },
    }))

    const result = await fetchDisplayAttachmentBlob(attachment({
      kind: 'staged',
      name: 'fallback.pdf',
      mime: 'application/pdf',
      download_url: '/api/v1/attachments/a?token=old&sessionKey=old',
    }), {
      baseOrigin: 'http://127.0.0.1:18793',
      sessionKey: 'agent:main:webchat:ok',
      authToken: 'secret',
      fetchImpl,
    })

    expect(result.ok).toBe(true)
    expect(fetchImpl).toHaveBeenCalledWith('/api/v1/attachments/a', {
      method: 'GET',
      headers: {
        'x-opensquilla-session-key': 'agent:main:webchat:ok',
        Authorization: 'Bearer secret',
      },
      credentials: 'same-origin',
      redirect: 'error',
      signal: undefined,
    })
    if (result.ok) expect(result.filename).toBe('server.pdf')
  })

  it('fails closed before fetch for cross-origin staged URLs', async () => {
    const fetchImpl = vi.fn()
    const result = await fetchDisplayAttachmentBlob(attachment({
      download_url: 'https://files.example.test/report.txt?token=secret',
    }), { baseOrigin: 'http://127.0.0.1:18793', fetchImpl })

    expect(result.ok).toBe(false)
    expect(fetchImpl).not.toHaveBeenCalled()
  })
})
