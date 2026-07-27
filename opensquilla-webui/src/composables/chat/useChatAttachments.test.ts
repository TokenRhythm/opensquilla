import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useChatAttachments } from './useChatAttachments'
import type { Attachment } from '@/types/chat'

const pushToast = vi.hoisted(() => vi.fn())

vi.mock('@/composables/useToasts', () => ({
  useToasts: () => ({ pushToast }),
}))

function stagedPdf(name = 'paper.pdf') {
  return new File([new Uint8Array(2_000_001)], name, { type: 'application/pdf' })
}

function stagedBinary(name = 'bad.bin') {
  const bytes = new Uint8Array(2_000_001)
  bytes[1] = 0xff
  return new File([bytes], name, { type: 'application/octet-stream' })
}

function stagedZip(name = 'paper.zip') {
  const bytes = new Uint8Array(2_000_001)
  bytes.set([0x50, 0x4b, 0x03, 0x04])
  return new File([bytes], name, { type: 'application/zip' })
}

function successfulUploadResponse(fileUuid = 'file-1') {
  return {
    ok: true,
    status: 200,
    json: async () => ({ file_uuid: fileUuid }),
    text: async () => '',
  }
}

async function flushUpload() {
  await new Promise(resolve => setTimeout(resolve, 0))
}

describe('useChatAttachments', () => {
  beforeEach(() => {
    pushToast.mockClear()
    vi.stubGlobal('sessionStorage', {
      getItem: vi.fn((key: string) => key === 'opensquilla.wsToken' ? 'token-123' : null),
    })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('accepts every file type in a mixed batch (opaque binaries included)', async () => {
    const fetchMock = vi.fn().mockResolvedValue(successfulUploadResponse('file-valid'))
    vi.stubGlobal('fetch', fetchMock)

    const attachments = useChatAttachments()

    await attachments.addAttachments([stagedPdf('valid.pdf'), stagedBinary()])
    await flushUpload()

    expect(attachments.pendingAttachments.value).toMatchObject([
      { kind: 'staged', name: 'valid.pdf', file_uuid: 'file-valid' },
      { kind: 'staged', name: 'bad.bin', mime: 'application/octet-stream', file_uuid: 'file-valid' },
    ])
    expect(pushToast).not.toHaveBeenCalled()
  })

  it('stages a zip archive above the inline threshold under its own mime', async () => {
    const fetchMock = vi.fn().mockResolvedValue(successfulUploadResponse('file-zip'))
    vi.stubGlobal('fetch', fetchMock)

    const attachments = useChatAttachments()

    await attachments.addAttachment(stagedZip())
    await flushUpload()

    expect(attachments.pendingAttachments.value).toMatchObject([
      { kind: 'staged', name: 'paper.zip', mime: 'application/zip', file_uuid: 'file-zip' },
    ])
    const form = fetchMock.mock.calls[0][1].body as FormData
    expect(form.get('mime')).toBe('application/zip')
  })

  it('stages large text files instead of rejecting them at the inline cap', async () => {
    const fetchMock = vi.fn().mockResolvedValue(successfulUploadResponse('file-text'))
    vi.stubGlobal('fetch', fetchMock)

    const attachments = useChatAttachments()
    const bigText = new File(['a'.repeat(2_000_001)], 'huge.tex', { type: '' })

    await attachments.addAttachment(bigText)
    await flushUpload()

    expect(attachments.pendingAttachments.value).toMatchObject([
      { kind: 'staged', name: 'huge.tex', mime: 'text/plain', file_uuid: 'file-text' },
    ])
  })

  it('rejects zero-byte files before read or upload work starts', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    const attachments = useChatAttachments()

    await attachments.addAttachments([new File([], 'empty.txt', { type: 'text/plain' })])

    expect(fetchMock).not.toHaveBeenCalled()
    expect(attachments.pendingAttachments.value).toHaveLength(0)
    expect(pushToast).toHaveBeenCalledWith('Empty file: empty.txt', { tone: 'danger' })
  })

  it('enforces the frontend aggregate attachment count before upload work starts', async () => {
    const fetchMock = vi.fn().mockResolvedValue(successfulUploadResponse('file-count'))
    vi.stubGlobal('fetch', fetchMock)
    const attachments = useChatAttachments()

    const files = Array.from({ length: 11 }, (_, index) => stagedPdf(`paper-${index}.pdf`))
    await attachments.addAttachments(files)
    await flushUpload()

    expect(fetchMock).toHaveBeenCalledTimes(10)
    expect(attachments.pendingAttachments.value).toHaveLength(10)
    expect(pushToast).toHaveBeenCalledWith('Too many attachments: max 10', { tone: 'danger' })
  })

  it('emits a single count-cap toast for a batch far over the limit', async () => {
    const fetchMock = vi.fn().mockResolvedValue(successfulUploadResponse('file-count'))
    vi.stubGlobal('fetch', fetchMock)
    const attachments = useChatAttachments()

    const files = Array.from({ length: 15 }, (_, index) => stagedPdf(`paper-${index}.pdf`))
    await attachments.addAttachments(files)
    await flushUpload()

    expect(attachments.pendingAttachments.value).toHaveLength(10)
    expect(pushToast).toHaveBeenCalledTimes(1)
    expect(pushToast).toHaveBeenCalledWith('Too many attachments: max 10', { tone: 'danger' })
  })

  it('names the per-type cap when rejecting an oversized file', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    const attachments = useChatAttachments()
    const hugePdf = new File([new Uint8Array(30 * 1024 * 1024 + 1)], 'huge.pdf', { type: 'application/pdf' })

    await attachments.addAttachments([hugePdf])

    expect(fetchMock).not.toHaveBeenCalled()
    expect(attachments.pendingAttachments.value).toHaveLength(0)
    expect(pushToast).toHaveBeenCalledWith('File too large: huge.pdf (max 30 MiB)', { tone: 'danger' })
  })

  it('never states a rounded-up cap the rejected file already satisfies', async () => {
    vi.stubGlobal('fetch', vi.fn())
    const attachments = useChatAttachments()
    const bigEmail = new File([new Uint8Array(2_000_001)], 'mail.eml', { type: 'message/rfc822' })

    await attachments.addAttachments([bigEmail])

    expect(attachments.pendingAttachments.value).toHaveLength(0)
    expect(pushToast).toHaveBeenCalledWith('File too large: mail.eml (max 1.9 MiB)', { tone: 'danger' })
  })

  it('emits a single total-size toast for a batch that overflows the aggregate cap', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    const attachments = useChatAttachments()
    attachments.pendingAttachments.value = Array.from({ length: 4 }, (_, index) => ({
      kind: 'staged',
      local_id: index + 1,
      name: `existing-${index}.pdf`,
      mime: 'application/pdf',
      size: 15 * 1024 * 1024,
      file_uuid: `existing-${index}`,
    }))

    await attachments.addAttachments([stagedPdf('a.pdf'), stagedPdf('b.pdf'), stagedPdf('c.pdf')])

    expect(fetchMock).not.toHaveBeenCalled()
    expect(attachments.pendingAttachments.value).toHaveLength(4)
    expect(pushToast).toHaveBeenCalledTimes(1)
    expect(pushToast).toHaveBeenCalledWith(
      'Attachments too large: a.pdf would exceed 60 MiB total',
      { tone: 'danger' },
    )
  })

  it('enforces the frontend aggregate attachment size before upload work starts', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    const attachments = useChatAttachments()
    attachments.pendingAttachments.value = Array.from({ length: 4 }, (_, index) => ({
      kind: 'staged',
      local_id: index + 1,
      name: `existing-${index}.pdf`,
      mime: 'application/pdf',
      size: 15 * 1024 * 1024,
      file_uuid: `existing-${index}`,
    }))

    await attachments.addAttachment(stagedPdf('overflow.pdf'))

    expect(fetchMock).not.toHaveBeenCalled()
    expect(attachments.pendingAttachments.value).toHaveLength(4)
    expect(pushToast).toHaveBeenCalledWith(
      'Attachments too large: overflow.pdf would exceed 60 MiB total',
      { tone: 'danger' },
    )
  })

  it('adds the WebSocket token as a bearer header on staged uploads', async () => {
    const fetchMock = vi.fn().mockResolvedValue(successfulUploadResponse('file-token'))
    vi.stubGlobal('fetch', fetchMock)

    const attachments = useChatAttachments()
    await attachments.addAttachment(stagedPdf())
    await flushUpload()

    expect(fetchMock).toHaveBeenCalledWith('/api/v1/files/upload', expect.objectContaining({
      method: 'POST',
      credentials: 'same-origin',
      headers: { Authorization: 'Bearer token-123' },
    }))
  })

  it('marks a staged upload failed when the upload response omits file_uuid', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({}),
      text: async () => '',
    })
    vi.stubGlobal('fetch', fetchMock)

    const attachments = useChatAttachments()
    await attachments.addAttachment(stagedPdf('missing-uuid.pdf'))
    await flushUpload()

    expect(attachments.pendingAttachments.value).toMatchObject([
      { kind: 'failed', name: 'missing-uuid.pdf', error: 'Upload response missing file_uuid' },
    ])
    expect(pushToast).toHaveBeenCalledWith(
      'Upload failed for missing-uuid.pdf: Upload response missing file_uuid',
      { tone: 'danger' },
    )
  })

  it('keeps failed staged uploads retryable without reselecting the file', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: false,
        status: 500,
        text: async () => 'boom',
        json: async () => ({}),
      })
      .mockResolvedValueOnce(successfulUploadResponse('file-retry'))
    vi.stubGlobal('fetch', fetchMock)

    const attachments = useChatAttachments()
    await attachments.addAttachment(stagedPdf('retry.pdf'))
    await flushUpload()

    expect(attachments.pendingAttachments.value).toMatchObject([
      { kind: 'failed', name: 'retry.pdf', error: 'HTTP 500 boom' },
    ])
    expect(attachments.pendingAttachments.value[0].file).toBeInstanceOf(File)

    await attachments.retryAttachment(0)
    await flushUpload()

    expect(attachments.pendingAttachments.value).toMatchObject([
      { kind: 'staged', name: 'retry.pdf', file_uuid: 'file-retry' },
    ])
  })

  it('refreshes expired staged uploads before send when the original file is available', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ file_uuid: 'file-expired', expires_at: Date.now() / 1000 - 1, ttl_seconds: 600 }),
        text: async () => '',
      })
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ file_uuid: 'file-fresh', expires_at: Date.now() / 1000 + 600, ttl_seconds: 600 }),
        text: async () => '',
      })
    vi.stubGlobal('fetch', fetchMock)

    const attachments = useChatAttachments()
    await attachments.addAttachment(stagedPdf('refresh.pdf'))
    await flushUpload()

    expect(attachments.pendingAttachments.value).toMatchObject([
      { kind: 'staged', name: 'refresh.pdf', file_uuid: 'file-expired' },
    ])

    const ready = await attachments.prepareAttachmentsForSend()

    expect(ready).toBe(true)
    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(attachments.pendingAttachments.value).toMatchObject([
      { kind: 'staged', name: 'refresh.pdf', file_uuid: 'file-fresh' },
    ])
  })

  it('refreshes staged uploads that are inside the expiration grace window', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ file_uuid: 'file-fresh', expires_at: Date.now() / 1000 + 600, ttl_seconds: 600 }),
      text: async () => '',
    })
    vi.stubGlobal('fetch', fetchMock)
    const attachments = useChatAttachments()
    attachments.pendingAttachments.value = [
      {
        kind: 'staged',
        local_id: 1,
        name: 'near-expiry.pdf',
        mime: 'application/pdf',
        file_uuid: 'file-near-expiry',
        expires_at: Date.now() / 1000 + 10,
        file: stagedPdf('near-expiry.pdf'),
      },
    ]

    const ready = await attachments.prepareAttachmentsForSend()

    expect(ready).toBe(true)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(attachments.pendingAttachments.value).toMatchObject([
      { kind: 'staged', name: 'near-expiry.pdf', file_uuid: 'file-fresh' },
    ])
  })

  it('does not refresh staged uploads that are outside the expiration grace window', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    const attachments = useChatAttachments()
    const stagedAttachment: Attachment = {
      kind: 'staged',
      local_id: 1,
      name: 'fresh.pdf',
      mime: 'application/pdf',
      file_uuid: 'file-fresh-enough',
      expires_at: Date.now() / 1000 + 120,
      file: stagedPdf('fresh.pdf'),
    }
    attachments.pendingAttachments.value = [stagedAttachment]

    const ready = await attachments.prepareAttachmentsForSend()

    expect(ready).toBe(true)
    expect(fetchMock).not.toHaveBeenCalled()
    expect(attachments.pendingAttachments.value).toEqual([stagedAttachment])
  })

  it('does not rewrite or toast when refresh completes after preparation is stale', async () => {
    let resolveUpload!: (response: unknown) => void
    const fetchMock = vi.fn(() => new Promise(resolve => {
      resolveUpload = resolve
    }))
    vi.stubGlobal('fetch', fetchMock)
    const attachments = useChatAttachments()
    const stagedAttachment: Attachment = {
      kind: 'staged',
      local_id: 1,
      name: 'stale-session.pdf',
      mime: 'application/pdf',
      file_uuid: 'file-expired',
      expires_at: Date.now() / 1000 - 1,
      file: stagedPdf('stale-session.pdf'),
    }
    attachments.pendingAttachments.value = [stagedAttachment]
    let current = true

    const ready = attachments.prepareAttachmentsForSend({ isCurrent: () => current })

    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(attachments.hasPendingAttachmentWork()).toBe(true)
    expect(attachments.pendingAttachments.value).toEqual([stagedAttachment])

    current = false
    resolveUpload({
      ok: true,
      status: 200,
      json: async () => ({ file_uuid: 'file-fresh', expires_at: Date.now() / 1000 + 600, ttl_seconds: 600 }),
      text: async () => '',
    })

    await expect(ready).resolves.toBe(false)
    expect(attachments.hasPendingAttachmentWork()).toBe(false)
    expect(attachments.pendingAttachments.value).toEqual([stagedAttachment])
    expect(pushToast).not.toHaveBeenCalled()
  })

  it('marks expired staged uploads failed when the original file is unavailable', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    const attachments = useChatAttachments()
    attachments.pendingAttachments.value = [
      {
        kind: 'staged',
        local_id: 1,
        name: 'missing-local-file.pdf',
        mime: 'application/pdf',
        file_uuid: 'file-expired',
        expires_at: Date.now() / 1000 - 1,
      },
    ]

    const ready = await attachments.prepareAttachmentsForSend()

    expect(ready).toBe(false)
    expect(fetchMock).not.toHaveBeenCalled()
    expect(attachments.pendingAttachments.value).toMatchObject([
      { kind: 'failed', name: 'missing-local-file.pdf', error: 'Upload expired; select the file again' },
    ])
    expect(attachments.pendingAttachments.value[0].file_uuid).toBeUndefined()
    expect(pushToast).toHaveBeenCalledWith(
      'Upload expired for missing-local-file.pdf: select the file again',
      { tone: 'danger' },
    )
  })

  it('marks expired staged uploads failed and retryable when refresh upload fails', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: false,
      status: 503,
      text: async () => 'unavailable',
      json: async () => ({}),
    })
    vi.stubGlobal('fetch', fetchMock)
    const attachments = useChatAttachments()
    attachments.pendingAttachments.value = [
      {
        kind: 'staged',
        local_id: 1,
        name: 'refresh-fails.pdf',
        mime: 'application/pdf',
        file_uuid: 'file-expired',
        expires_at: Date.now() / 1000 - 1,
        file: stagedPdf('refresh-fails.pdf'),
      },
    ]

    const ready = await attachments.prepareAttachmentsForSend()

    expect(ready).toBe(false)
    expect(attachments.pendingAttachments.value).toMatchObject([
      { kind: 'failed', name: 'refresh-fails.pdf', error: 'HTTP 503 unavailable' },
    ])
    expect(attachments.pendingAttachments.value[0].file).toBeInstanceOf(File)
    expect(pushToast).toHaveBeenCalledWith(
      expect.stringContaining('Upload failed for refresh-fails.pdf: HTTP 503 unavailable'),
      { tone: 'danger' },
    )
  })
})
