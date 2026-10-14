import { afterEach, describe, expect, it, vi } from 'vitest'
import type { DisplayAttachment } from '@/types/chat'
import { downloadRetainedAttachment } from './attachmentDownloadV4'
import { createPrivateHttpTransport } from './privateHttpTransport'

const mocks = vi.hoisted(() => ({ nativeSave: vi.fn() }))
vi.mock('@/platform', () => ({ getPlatform: () => ({
  id: 'desktop',
  files: { saveAttachment: mocks.nativeSave },
  gateway: { getConnection: async () => ({ status: 'ready', instanceId: 'external-a', authToken: null }) },
}) }))

const attachment: DisplayAttachment = {
  kind: 'file', renderKey: 'retained-a', displayId: 'retained-a',
  name: 'folder/history.txt', mime: 'text/plain', sha256_ref: 'a'.repeat(64),
  fileSnapshotRef: { messageId: 'message-1', toolUseId: 'write-1' },
}

afterEach(() => { vi.unstubAllGlobals(); vi.resetAllMocks() })

function destination() {
  const written: Uint8Array[] = []
  const target = {
    write: vi.fn(async (bytes: Uint8Array) => { written.push(bytes.slice()) }),
    close: vi.fn().mockResolvedValue(undefined),
    abort: vi.fn().mockResolvedValue(undefined),
  }
  const picker = vi.fn().mockResolvedValue({ createWritable: async () => target })
  vi.stubGlobal('showSaveFilePicker', picker)
  return { target, picker, written }
}

describe('external Desktop retained download with real HTTP transport and stream saver', () => {
  it.each(['https://gateway.example/control/chat', 'opensquilla-app://desktop/chat'])(
    'preserves historical bytes and header-only credentials from %s', async baseUrl => {
      const { target, picker, written } = destination()
      const text = '\ufeff历史版本 🦐\r\n'.repeat(8_000)
      const bytes = new TextEncoder().encode(text)
      const fetch = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => {
        expect(picker).toHaveBeenCalledOnce()
        return new Response(new ReadableStream<Uint8Array>({ start(controller) {
          // Split multibyte characters across chunks to detect accidental text conversion.
          for (let offset = 0; offset < bytes.length; offset += 1021) {
            controller.enqueue(bytes.slice(offset, offset + 1021))
          }
          controller.close()
        } }), { headers: { 'content-type': 'text/plain;charset=utf-8' } })
      })
      const http = createPrivateHttpTransport({ baseUrl, authToken: () => 'external-operator-token', fetch })
      expect(await downloadRetainedAttachment(http, { ...attachment, size: bytes.length },
        { sessionKey: 'session-a' })).toBe('saved')

      expect(mocks.nativeSave).not.toHaveBeenCalled()
      expect(fetch).toHaveBeenCalledOnce()
      const [url, init] = fetch.mock.calls[0]!
      const expectedUrl = new URL(`/api/v1/attachments/${attachment.sha256_ref}?messageId=message-1&toolUseId=write-1`, baseUrl)
      expect(String(url)).toBe(expectedUrl.href)
      const headers = new Headers(init?.headers)
      expect(headers.get('authorization')).toBe('Bearer external-operator-token')
      expect(headers.get('x-opensquilla-session-key')).toBe('session-a')
      expect(picker).toHaveBeenCalledWith({ suggestedName: 'history.txt' })
      expect(written.length).toBeGreaterThan(1)
      expect(new Uint8Array(await new Blob(written as BlobPart[]).arrayBuffer())).toEqual(bytes)
      expect(target.close).toHaveBeenCalledOnce()
      expect(target.abort).not.toHaveBeenCalled()
    },
  )

  it('cancels the external save before any HTTP fetch', async () => {
    vi.stubGlobal('showSaveFilePicker', vi.fn().mockRejectedValue(new DOMException('Cancelled', 'AbortError')))
    const fetch = vi.fn()
    const http = createPrivateHttpTransport({ baseUrl: 'https://gateway.example/control/', fetch })
    expect(await downloadRetainedAttachment(http, attachment, { sessionKey: 'session-a' })).toBe('cancelled')
    expect(fetch).not.toHaveBeenCalled()
    expect(mocks.nativeSave).not.toHaveBeenCalled()
  })

  it('reports external HTTP failure and aborts the destination without native retry', async () => {
    const { target } = destination()
    const fetch = vi.fn(async () => new Response('Unauthorized', { status: 401 }))
    const http = createPrivateHttpTransport({ baseUrl: 'https://gateway.example/control/', fetch })
    await expect(downloadRetainedAttachment(http, attachment, { sessionKey: 'session-a' }))
      .rejects.toMatchObject({ kind: 'http-status', status: 401 })
    expect(fetch).toHaveBeenCalledOnce()
    expect(target.write).not.toHaveBeenCalled()
    expect(target.close).not.toHaveBeenCalled()
    expect(target.abort).toHaveBeenCalledOnce()
    expect(mocks.nativeSave).not.toHaveBeenCalled()
  })
})
