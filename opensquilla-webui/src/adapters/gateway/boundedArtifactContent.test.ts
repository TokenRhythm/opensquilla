import { afterEach, describe, expect, it, vi } from 'vitest'
import type { DisplayAttachment } from '@/types/chat'
import { httpBinaryResponse, httpTransportTestDouble } from '@/testing/httpTransport.test-helper'
import { createV4ArtifactContentAccess } from './artifactAccessV4'
import { createV4AttachmentContentAccess } from './attachmentAccessV4'

function attachment(overrides: Partial<DisplayAttachment> = {}): DisplayAttachment {
  return { kind: 'file', name: 'drawing.svg', mime: 'text/plain',
    displayId: 'fixture-image', renderKey: 'fixture-image', ...overrides }
}

afterEach(() => vi.restoreAllMocks())

describe('optional content byte ceilings', () => {
  it('passes artifact limits through the public content capability without limiting downloads', async () => {
    const http = httpTransportTestDouble({ requestBinary: vi.fn(async () => httpBinaryResponse('12345')) })
    const content = createV4ArtifactContentAccess(http)
    const item = { id: 'fixture-image', name: 'drawing.svg', mime: 'image/svg+xml' }
    expect(await content.fetchArtifact(item, { maxBytes: 4 })).toMatchObject({
      ok: false, errorCode: 'too_large',
    })
    expect(await content.fetchArtifact(item)).toMatchObject({ ok: true })
    expect(await content.fetchArtifact(item, { maxBytes: 5 })).toMatchObject({ ok: true })
  })

  it('applies limits to credential-free external artifact content too', async () => {
    const fetchExternalArtifact = vi.fn(async () => httpBinaryResponse('12345'))
    const http = httpTransportTestDouble({ fetchExternalArtifact })
    const result = await createV4ArtifactContentAccess(http).fetchArtifact({
      download_url: 'https://files.example.test/drawing.svg',
    }, { maxBytes: 4, sessionKey: 'fixture-session' })
    expect(result).toMatchObject({ ok: false, errorCode: 'too_large' })
    expect(fetchExternalArtifact).toHaveBeenCalledWith('https://files.example.test/drawing.svg', undefined)
  })

  it('rejects local files by real size before loading bytes or falling through to a URL', async () => {
    const localFile = new File(['12345'], 'drawing.svg', { type: 'image/svg+xml' })
    const arrayBuffer = vi.spyOn(localFile, 'arrayBuffer')
    const http = httpTransportTestDouble({ requestBinary: vi.fn() })
    const content = createV4AttachmentContentAccess(http)
    const item = attachment({ localFile, download_url: '/api/v1/attachments/fixture-image', size: 1 })
    expect(await content.fetchAttachment(item, { maxBytes: 4 })).toMatchObject({
      ok: false, source: 'local-file', errorCode: 'too_large',
    })
    expect(arrayBuffer).not.toHaveBeenCalled()
    expect(http.requestBinary).not.toHaveBeenCalled()
    expect(await content.fetchAttachment(item)).toMatchObject({ ok: true, blob: localFile })
  })

  it.each(['downloadData', 'data'] as const)('bounds inline %s before base64 decoding', async field => {
    const decode = vi.spyOn(globalThis, 'atob')
    const content = createV4AttachmentContentAccess(httpTransportTestDouble())
    const item = attachment({ [field]: 'MTIzNDU=' })
    expect(await content.fetchAttachment(item, { maxBytes: 4 })).toMatchObject({
      ok: false, source: 'inline', errorCode: 'too_large',
    })
    expect(decode).not.toHaveBeenCalled()
    const accepted = await content.fetchAttachment(item, { maxBytes: 5 })
    expect(accepted.ok).toBe(true)
    if (accepted.ok) expect(await accepted.blob.text()).toBe('12345')
  })

  it.each(['MTIzNA==', ' MTIz\nNA ', 'MTIzNA'])('accepts padded, whitespace and unpadded base64 at the exact ceiling: %s', async data => {
    const content = createV4AttachmentContentAccess(httpTransportTestDouble())
    const result = await content.fetchAttachment(attachment({ downloadData: data }), { maxBytes: 4 })
    expect(result.ok).toBe(true)
    if (result.ok) expect(await result.blob.text()).toBe('1234')
  })

  it('bounds staged attachments and preserves the existing scope and filename', async () => {
    const requestBinary = vi.fn(async () => httpBinaryResponse('12345', {
      contentType: 'image/svg+xml', filename: 'original.svg',
    }))
    const content = createV4AttachmentContentAccess(httpTransportTestDouble({ requestBinary }))
    const item = attachment({ kind: 'staged', download_url: '/api/v1/attachments/fixture-image' })
    expect(await content.fetchAttachment(item, { maxBytes: 4, sessionKey: 'fixture-session' }))
      .toMatchObject({ ok: false, source: 'staged', errorCode: 'too_large' })
    const result = await content.fetchAttachment(item, { maxBytes: 5, sessionKey: 'fixture-session' })
    expect(result).toMatchObject({ ok: true, source: 'staged', filename: 'original.svg' })
    expect(requestBinary).toHaveBeenCalledWith('/api/v1/attachments/fixture-image', {
      sessionKey: 'fixture-session', signal: undefined, timeoutMs: 0,
    })
  })

  it('keeps SVG data URLs outside attachment decoding even for bounded requests', async () => {
    const content = createV4AttachmentContentAccess(httpTransportTestDouble())
    expect(await content.fetchAttachment(attachment({ dataUrl: 'data:image/svg+xml;base64,MTIzNA==' }), { maxBytes: 4 }))
      .toMatchObject({ ok: false, source: 'none' })
  })

  it('does not start any attachment source or artifact request after cancellation', async () => {
    const controller = new AbortController()
    controller.abort()
    const http = httpTransportTestDouble({ requestBinary: vi.fn() })
    const content = createV4AttachmentContentAccess(http)
    const request = { maxBytes: 4, signal: controller.signal }
    for (const item of [
      attachment({ localFile: new File(['12'], 'drawing.svg') }),
      attachment({ downloadData: 'MTI=' }),
      attachment({ download_url: '/api/v1/attachments/fixture-image' }),
    ]) {
      await expect(content.fetchAttachment(item, request)).rejects.toMatchObject({ name: 'AbortError' })
    }
    await expect(createV4ArtifactContentAccess(http).fetchArtifact({ id: 'fixture-image' }, request))
      .rejects.toMatchObject({ name: 'AbortError' })
    expect(http.requestBinary).not.toHaveBeenCalled()
  })
})
