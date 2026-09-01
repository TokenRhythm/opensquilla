import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useChatAttachments } from './useChatAttachments'
import type { Attachment } from '@/types/chat'
import type { ArtifactContentAccess } from '@/modules/artifactWorkbench'

const pushToast = vi.hoisted(() => vi.fn())

vi.mock('@/composables/useToasts', () => ({
  useToasts: () => ({ pushToast }),
}))

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

class ControlledFileReader {
  static instances: ControlledFileReader[] = []

  result: string | ArrayBuffer | null = null
  onload: ((event: ProgressEvent<FileReader>) => void) | null = null
  onerror: ((event: ProgressEvent<FileReader>) => void) | null = null

  readAsDataURL(_blob: Blob) {
    ControlledFileReader.instances.push(this)
  }

  succeed(dataUrl: string) {
    this.result = dataUrl
    this.onload?.({ target: this } as unknown as ProgressEvent<FileReader>)
  }

  fail() {
    this.onerror?.({ target: this } as unknown as ProgressEvent<FileReader>)
  }
}

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

function nextSessionAttachment(localId: number): Attachment {
  return {
    kind: 'inline',
    local_id: localId,
    name: 'session-b.txt',
    mime: 'text/plain',
    data: 'Qg==',
  }
}

async function flushUpload() {
  await new Promise(resolve => setTimeout(resolve, 0))
}

function useTestChatAttachments() {
  const content: ArtifactContentAccess = {
    fetchArtifact: vi.fn(async () => ({
      ok: false as const,
      status: 0,
      url: '',
      message: 'not used',
    })),
    openArtifact: vi.fn(async () => ({
      ok: false as const,
      status: 0,
      url: '',
      message: 'not used',
    })),
    openArtifactBlob: vi.fn(async () => ({
      ok: false as const,
      status: 0,
      url: '',
      message: 'not used',
    })),
    clearPreviewStorage: vi.fn(async () => undefined),
    fetchAttachment: vi.fn(async () => ({
      ok: false as const,
      status: 0,
      source: 'none' as const,
      url: '',
      message: 'not used',
    })),
    async uploadAttachment(file, mime) {
      const form = new FormData()
      form.append('file', file, file.name)
      form.append('mime', mime)
      const token = globalThis.sessionStorage?.getItem('opensquilla.wsToken')?.trim()
      const response = await fetch('/api/v1/files/upload', {
        method: 'POST',
        body: form,
        credentials: 'same-origin',
        headers: token ? { Authorization: `Bearer ${token}` } : undefined,
      })
      if (!response.ok) {
        const detail = await response.text().catch(() => '')
        throw new Error(`HTTP ${response.status} ${detail}`)
      }
      const raw = await response.json() as Record<string, unknown>
      const fileUuid = typeof raw.file_uuid === 'string' ? raw.file_uuid.trim() : ''
      if (!fileUuid) throw new Error('Upload response missing file_uuid')
      return {
        fileUuid,
        ...(typeof raw.expires_at === 'number' ? { expiresAt: raw.expires_at } : {}),
        ...(typeof raw.ttl_seconds === 'number' ? { ttlSeconds: raw.ttl_seconds } : {}),
      }
    },
  }
  return useChatAttachments(content)
}

describe('useChatAttachments', () => {
  beforeEach(() => {
    pushToast.mockClear()
    ControlledFileReader.instances = []
    vi.stubGlobal('sessionStorage', {
      getItem: vi.fn((key: string) => key === 'opensquilla.wsToken' ? 'token-123' : null),
    })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it.each([
    ['successful', (sniff: ReturnType<typeof deferred<ArrayBuffer>>) => {
      sniff.resolve(new TextEncoder().encode('session A text').buffer)
    }],
    ['failed', (sniff: ReturnType<typeof deferred<ArrayBuffer>>) => {
      sniff.reject(new Error('sniff failed'))
    }],
  ])('ignores a delayed %s MIME sniff after attachments retire', async (_outcome, settle) => {
    vi.stubGlobal('FileReader', ControlledFileReader)
    const attachments = useTestChatAttachments()
    const sniff = deferred<ArrayBuffer>()
    const file = new File(['session A'], 'draft.unknown', { type: 'application/x-unknown' })
    Object.defineProperty(file, 'arrayBuffer', {
      configurable: true,
      value: vi.fn(() => sniff.promise),
    })

    const adding = attachments.addAttachment(file)
    expect(attachments.pendingAttachments.value).toEqual([])
    expect(attachments.hasPendingAttachmentWork()).toBe(true)

    attachments.retireAttachments()
    expect(attachments.hasPendingAttachmentWork()).toBe(false)
    const sessionBAttachment = nextSessionAttachment(1)
    attachments.pendingAttachments.value = [sessionBAttachment]
    settle(sniff)
    await adding

    expect(ControlledFileReader.instances).toHaveLength(0)
    expect(attachments.pendingAttachments.value).toEqual([sessionBAttachment])
    expect(attachments.hasPendingAttachmentWork()).toBe(false)
    expect(pushToast).not.toHaveBeenCalled()
  })

  it('keeps an unknown-MIME selection in the send busy gate until its placeholder is ready', async () => {
    vi.stubGlobal('FileReader', ControlledFileReader)
    const attachments = useTestChatAttachments()
    const sniff = deferred<ArrayBuffer>()
    const file = new File(['selected context'], 'context.unknown', { type: 'application/x-unknown' })
    Object.defineProperty(file, 'arrayBuffer', {
      configurable: true,
      value: vi.fn(() => sniff.promise),
    })

    const adding = attachments.addAttachment(file)

    expect(attachments.pendingAttachments.value).toEqual([])
    expect(attachments.hasPendingAttachmentWork()).toBe(true)

    sniff.resolve(new TextEncoder().encode('selected context').buffer)
    await adding

    expect(ControlledFileReader.instances).toHaveLength(1)
    expect(attachments.pendingAttachments.value).toMatchObject([
      { kind: 'inline_pending', name: 'context.unknown' },
    ])
    expect(attachments.hasPendingAttachmentWork()).toBe(true)

    ControlledFileReader.instances[0]!.succeed('data:text/plain;base64,c2VsZWN0ZWQgY29udGV4dA==')

    expect(attachments.pendingAttachments.value).toMatchObject([
      { kind: 'inline', name: 'context.unknown' },
    ])
    expect(attachments.hasPendingAttachmentWork()).toBe(false)
  })

  it('does not let retired MIME-sniff cleanup release next-generation intake', async () => {
    vi.stubGlobal('FileReader', ControlledFileReader)
    const attachments = useTestChatAttachments()
    const firstSniff = deferred<ArrayBuffer>()
    const secondSniff = deferred<ArrayBuffer>()
    const firstFile = new File(['first'], 'first.unknown', { type: 'application/x-unknown' })
    const secondFile = new File(['second'], 'second.unknown', { type: 'application/x-unknown' })
    Object.defineProperty(firstFile, 'arrayBuffer', {
      configurable: true,
      value: vi.fn(() => firstSniff.promise),
    })
    Object.defineProperty(secondFile, 'arrayBuffer', {
      configurable: true,
      value: vi.fn(() => secondSniff.promise),
    })

    const firstAdding = attachments.addAttachment(firstFile)
    attachments.retireAttachments()
    const secondAdding = attachments.addAttachment(secondFile)
    expect(attachments.hasPendingAttachmentWork()).toBe(true)

    firstSniff.resolve(new TextEncoder().encode('first').buffer)
    await firstAdding

    expect(ControlledFileReader.instances).toHaveLength(0)
    expect(attachments.pendingAttachments.value).toEqual([])
    expect(attachments.hasPendingAttachmentWork()).toBe(true)

    secondSniff.resolve(new TextEncoder().encode('second').buffer)
    await secondAdding

    expect(ControlledFileReader.instances).toHaveLength(1)
    expect(attachments.pendingAttachments.value).toMatchObject([
      { kind: 'inline_pending', name: 'second.unknown' },
    ])
    expect(attachments.hasPendingAttachmentWork()).toBe(true)

    ControlledFileReader.instances[0]!.succeed('data:text/plain;base64,c2Vjb25k')
    expect(attachments.pendingAttachments.value).toMatchObject([
      { kind: 'inline', name: 'second.unknown' },
    ])
    expect(attachments.hasPendingAttachmentWork()).toBe(false)
  })

  it.each(['load', 'error'] as const)(
    'ignores a delayed FileReader %s callback after attachments retire',
    async outcome => {
      vi.stubGlobal('FileReader', ControlledFileReader)
      const attachments = useTestChatAttachments()

      await attachments.addAttachment(
        new File(['session A'], 'draft.txt', { type: 'text/plain' }),
      )

      expect(ControlledFileReader.instances).toHaveLength(1)
      const reader = ControlledFileReader.instances[0]!
      const localId = attachments.pendingAttachments.value[0]!.local_id
      attachments.retireAttachments()
      const sessionBAttachment = nextSessionAttachment(localId)
      attachments.pendingAttachments.value = [sessionBAttachment]

      if (outcome === 'load') reader.succeed('data:text/plain;base64,QQ==')
      else reader.fail()

      expect(attachments.pendingAttachments.value).toEqual([sessionBAttachment])
      expect(pushToast).not.toHaveBeenCalled()
    },
  )

  it.each(['success', 'failure'] as const)(
    'ignores a delayed staged upload %s after attachments retire',
    async outcome => {
      const upload = deferred<unknown>()
      vi.stubGlobal('fetch', vi.fn(() => upload.promise))
      const attachments = useTestChatAttachments()

      await attachments.addAttachment(stagedPdf('session-a.pdf'))

      expect(attachments.pendingAttachments.value).toMatchObject([
        { kind: 'uploading', name: 'session-a.pdf' },
      ])
      const localId = attachments.pendingAttachments.value[0]!.local_id
      attachments.retireAttachments()
      const sessionBAttachment = nextSessionAttachment(localId)
      attachments.pendingAttachments.value = [sessionBAttachment]

      if (outcome === 'success') upload.resolve(successfulUploadResponse('file-session-a'))
      else upload.reject(new Error('late upload failure'))
      await flushUpload()

      expect(attachments.pendingAttachments.value).toEqual([sessionBAttachment])
      expect(pushToast).not.toHaveBeenCalled()
    },
  )

  it.each(['success', 'failure'] as const)(
    'retires delayed staged refresh %s state without touching the next session',
    async outcome => {
      const upload = deferred<unknown>()
      vi.stubGlobal('fetch', vi.fn(() => upload.promise))
      const attachments = useTestChatAttachments()
      const sessionAFile = stagedPdf('session-a-refresh.pdf')
      attachments.pendingAttachments.value = [{
        kind: 'staged',
        local_id: 1,
        name: sessionAFile.name,
        mime: sessionAFile.type,
        file_uuid: 'file-expired',
        expires_at: Date.now() / 1000 - 1,
        file: sessionAFile,
      }]

      const preparing = attachments.prepareAttachmentsForSend()
      expect(attachments.hasPendingAttachmentWork()).toBe(true)
      attachments.retireAttachments()
      const sessionBAttachment = nextSessionAttachment(1)
      attachments.pendingAttachments.value = [sessionBAttachment]

      if (outcome === 'success') upload.resolve(successfulUploadResponse('file-session-a-fresh'))
      else upload.reject(new Error('late refresh failure'))

      await expect(preparing).resolves.toBe(false)
      expect(attachments.hasPendingAttachmentWork()).toBe(false)
      expect(attachments.pendingAttachments.value).toEqual([sessionBAttachment])
      expect(pushToast).not.toHaveBeenCalled()
    },
  )

  it('does not let retired refresh cleanup release same-id work in the next session', async () => {
    const sessionAUpload = deferred<unknown>()
    const sessionBUpload = deferred<unknown>()
    const fetchMock = vi.fn()
      .mockImplementationOnce(() => sessionAUpload.promise)
      .mockImplementationOnce(() => sessionBUpload.promise)
    vi.stubGlobal('fetch', fetchMock)
    const attachments = useTestChatAttachments()
    const sessionAFile = stagedPdf('session-a-refresh.pdf')
    attachments.pendingAttachments.value = [{
      kind: 'staged',
      local_id: 1,
      name: sessionAFile.name,
      mime: sessionAFile.type,
      file_uuid: 'file-a-expired',
      expires_at: Date.now() / 1000 - 1,
      file: sessionAFile,
    }]

    const preparingSessionA = attachments.prepareAttachmentsForSend()
    attachments.retireAttachments()
    const sessionBFile = stagedPdf('session-b-refresh.pdf')
    attachments.pendingAttachments.value = [{
      kind: 'staged',
      local_id: 1,
      name: sessionBFile.name,
      mime: sessionBFile.type,
      file_uuid: 'file-b-expired',
      expires_at: Date.now() / 1000 - 1,
      file: sessionBFile,
    }]
    const preparingSessionB = attachments.prepareAttachmentsForSend()

    sessionAUpload.resolve(successfulUploadResponse('file-a-fresh'))
    await expect(preparingSessionA).resolves.toBe(false)
    expect(attachments.hasPendingAttachmentWork()).toBe(true)
    expect(attachments.pendingAttachments.value).toMatchObject([
      { name: 'session-b-refresh.pdf', file_uuid: 'file-b-expired' },
    ])
    await expect(attachments.prepareAttachmentsForSend({
      ownership: 'composer',
      attachments: attachments.pendingAttachments.value.map(attachment => ({ ...attachment })),
      isCurrent: () => true,
    })).resolves.toBe(false)
    expect(fetchMock).toHaveBeenCalledTimes(2)

    sessionBUpload.resolve(successfulUploadResponse('file-b-fresh'))
    await expect(preparingSessionB).resolves.toBe(true)
    expect(attachments.hasPendingAttachmentWork()).toBe(false)
    expect(attachments.pendingAttachments.value).toMatchObject([
      { name: 'session-b-refresh.pdf', file_uuid: 'file-b-fresh' },
    ])
    expect(pushToast).not.toHaveBeenCalled()
  })

  it('serializes cloned composer snapshots within one attachment generation', async () => {
    const firstUpload = deferred<unknown>()
    const releasedUpload = deferred<unknown>()
    const fetchMock = vi.fn()
      .mockImplementationOnce(() => firstUpload.promise)
      .mockImplementationOnce(() => releasedUpload.promise)
    vi.stubGlobal('fetch', fetchMock)
    const attachments = useTestChatAttachments()
    const sourceFile = stagedPdf('composer-refresh.pdf')
    const source: Attachment = {
      kind: 'staged',
      local_id: 1,
      name: sourceFile.name,
      mime: sourceFile.type,
      file_uuid: 'file-expired',
      expires_at: 0,
      file: sourceFile,
    }
    const firstSnapshot = [{ ...source }]
    const secondSnapshot = [{ ...source }]

    const firstPreparation = attachments.prepareAttachmentsForSend({
      ownership: 'composer',
      attachments: firstSnapshot,
      isCurrent: () => true,
    })
    await expect(attachments.prepareAttachmentsForSend({
      ownership: 'composer',
      attachments: secondSnapshot,
      isCurrent: () => true,
    })).resolves.toBe(false)

    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(attachments.hasPendingAttachmentWork()).toBe(true)
    firstUpload.resolve(successfulUploadResponse('file-first-fresh'))
    await expect(firstPreparation).resolves.toBe(true)
    expect(firstSnapshot).toMatchObject([{ file_uuid: 'file-first-fresh' }])
    expect(secondSnapshot).toMatchObject([{ file_uuid: 'file-expired' }])
    expect(attachments.hasPendingAttachmentWork()).toBe(false)

    const preparationAfterRelease = attachments.prepareAttachmentsForSend({
      ownership: 'composer',
      attachments: secondSnapshot,
      isCurrent: () => true,
    })
    expect(fetchMock).toHaveBeenCalledTimes(2)
    releasedUpload.resolve(successfulUploadResponse('file-second-fresh'))
    await expect(preparationAfterRelease).resolves.toBe(true)
    expect(secondSnapshot).toMatchObject([{ file_uuid: 'file-second-fresh' }])
    expect(attachments.hasPendingAttachmentWork()).toBe(false)
  })

  it('keeps a detached handoff refresh alive when the visible composer retires', async () => {
    const upload = deferred<unknown>()
    vi.stubGlobal('fetch', vi.fn(() => upload.promise))
    const attachments = useTestChatAttachments()
    const handoffFile = stagedPdf('handoff-refresh.pdf')
    const handoffAttachments: Attachment[] = [{
      kind: 'staged',
      local_id: 1,
      name: handoffFile.name,
      mime: handoffFile.type,
      file_uuid: 'file-handoff-expired',
      expires_at: Date.now() / 1000 - 1,
      file: handoffFile,
    }]

    const preparing = attachments.prepareAttachmentsForSend({
      attachments: handoffAttachments,
      isCurrent: () => true,
      ownership: 'detached',
    })

    expect(attachments.hasPendingAttachmentWork()).toBe(false)
    attachments.retireAttachments()
    const sessionBAttachment = nextSessionAttachment(1)
    attachments.pendingAttachments.value = [sessionBAttachment]
    upload.resolve(successfulUploadResponse('file-handoff-fresh'))

    await expect(preparing).resolves.toBe(true)
    expect(handoffAttachments).toMatchObject([
      { kind: 'staged', file_uuid: 'file-handoff-fresh' },
    ])
    expect(attachments.pendingAttachments.value).toEqual([sessionBAttachment])
    expect(attachments.hasPendingAttachmentWork()).toBe(false)
    expect(pushToast).not.toHaveBeenCalled()
  })

  it('isolates equal local IDs across concurrent detached attachment collections', async () => {
    const firstUpload = deferred<unknown>()
    const secondUpload = deferred<unknown>()
    const fetchMock = vi.fn()
      .mockImplementationOnce(() => firstUpload.promise)
      .mockImplementationOnce(() => secondUpload.promise)
    vi.stubGlobal('fetch', fetchMock)
    const attachments = useTestChatAttachments()
    const firstFile = stagedPdf('first-handoff.pdf')
    const secondFile = stagedPdf('second-handoff.pdf')
    const firstCollection: Attachment[] = [{
      kind: 'staged',
      local_id: 1,
      name: firstFile.name,
      mime: firstFile.type,
      file_uuid: 'first-expired',
      expires_at: 0,
      file: firstFile,
    }]
    const secondCollection: Attachment[] = [{
      kind: 'staged',
      local_id: 1,
      name: secondFile.name,
      mime: secondFile.type,
      file_uuid: 'second-expired',
      expires_at: 0,
      file: secondFile,
    }]

    const firstPreparation = attachments.prepareAttachmentsForSend({
      ownership: 'detached',
      attachments: firstCollection,
      isCurrent: () => true,
    })
    const secondPreparation = attachments.prepareAttachmentsForSend({
      ownership: 'detached',
      attachments: secondCollection,
      isCurrent: () => true,
    })

    expect(fetchMock).toHaveBeenCalledTimes(2)
    firstUpload.resolve(successfulUploadResponse('first-fresh'))
    secondUpload.resolve(successfulUploadResponse('second-fresh'))

    await expect(firstPreparation).resolves.toBe(true)
    await expect(secondPreparation).resolves.toBe(true)
    expect(firstCollection).toMatchObject([{ file_uuid: 'first-fresh' }])
    expect(secondCollection).toMatchObject([{ file_uuid: 'second-fresh' }])
    expect(attachments.hasPendingAttachmentWork()).toBe(false)
  })

  it('refreshes duplicate restored IDs by attachment identity within one collection', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(successfulUploadResponse('first-fresh'))
      .mockResolvedValueOnce(successfulUploadResponse('second-fresh'))
    vi.stubGlobal('fetch', fetchMock)
    const attachments = useTestChatAttachments()
    const firstFile = stagedPdf('first-restored.pdf')
    const secondFile = stagedPdf('second-restored.pdf')
    const restored: Attachment[] = [
      {
        kind: 'staged',
        local_id: 1,
        name: firstFile.name,
        mime: firstFile.type,
        file_uuid: 'first-expired',
        expires_at: 0,
        file: firstFile,
      },
      {
        kind: 'staged',
        local_id: 1,
        name: secondFile.name,
        mime: secondFile.type,
        file_uuid: 'second-expired',
        expires_at: 0,
        file: secondFile,
      },
    ]

    await expect(attachments.prepareAttachmentsForSend({
      ownership: 'detached',
      attachments: restored,
      isCurrent: () => true,
    })).resolves.toBe(true)

    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(restored).toMatchObject([
      { local_id: 1, name: 'first-restored.pdf', file_uuid: 'first-fresh' },
      { local_id: 1, name: 'second-restored.pdf', file_uuid: 'second-fresh' },
    ])
  })

  it('keeps the collection lock registered across a multi-attachment refresh', async () => {
    const firstUpload = deferred<unknown>()
    const secondUpload = deferred<unknown>()
    const fetchMock = vi.fn()
      .mockImplementationOnce(() => firstUpload.promise)
      .mockImplementationOnce(() => secondUpload.promise)
    vi.stubGlobal('fetch', fetchMock)
    const attachments = useTestChatAttachments()
    const firstFile = stagedPdf('first.pdf')
    const secondFile = stagedPdf('second.pdf')
    const collection: Attachment[] = [
      {
        kind: 'staged',
        local_id: 1,
        name: firstFile.name,
        mime: firstFile.type,
        file_uuid: 'first-expired',
        expires_at: 0,
        file: firstFile,
      },
      {
        kind: 'staged',
        local_id: 2,
        name: secondFile.name,
        mime: secondFile.type,
        file_uuid: 'second-expired',
        expires_at: 0,
        file: secondFile,
      },
    ]
    const options = {
      ownership: 'detached' as const,
      attachments: collection,
      isCurrent: () => true,
    }

    const firstPreparation = attachments.prepareAttachmentsForSend(options)
    firstUpload.resolve(successfulUploadResponse('first-fresh'))
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))

    await expect(attachments.prepareAttachmentsForSend(options)).resolves.toBe(false)
    expect(fetchMock).toHaveBeenCalledTimes(2)

    secondUpload.resolve(successfulUploadResponse('second-fresh'))
    await expect(firstPreparation).resolves.toBe(true)
    expect(collection).toMatchObject([
      { local_id: 1, file_uuid: 'first-fresh' },
      { local_id: 2, file_uuid: 'second-fresh' },
    ])
  })

  it('allocates around restored attachment IDs before starting async work', async () => {
    vi.stubGlobal('FileReader', ControlledFileReader)
    const attachments = useTestChatAttachments()
    const restoredAttachment: Attachment = {
      kind: 'inline',
      local_id: 1,
      name: 'restored.txt',
      mime: 'text/plain',
      data: 'cmVzdG9yZWQ=',
    }
    attachments.pendingAttachments.value = [restoredAttachment]

    await attachments.addAttachment(
      new File(['new attachment'], 'new.txt', { type: 'text/plain' }),
    )

    expect(attachments.pendingAttachments.value).toMatchObject([
      { local_id: 1, kind: 'inline', name: 'restored.txt' },
      { local_id: 2, kind: 'inline_pending', name: 'new.txt' },
    ])
    ControlledFileReader.instances[0]!.succeed('data:text/plain;base64,bmV3IGF0dGFjaG1lbnQ=')

    expect(attachments.pendingAttachments.value).toMatchObject([
      { local_id: 1, kind: 'inline', name: 'restored.txt', data: 'cmVzdG9yZWQ=' },
      { local_id: 2, kind: 'inline', name: 'new.txt', data: 'bmV3IGF0dGFjaG1lbnQ=' },
    ])
  })

  it('accepts every file type in a mixed batch (opaque binaries included)', async () => {
    const fetchMock = vi.fn().mockResolvedValue(successfulUploadResponse('file-valid'))
    vi.stubGlobal('fetch', fetchMock)

    const attachments = useTestChatAttachments()

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

    const attachments = useTestChatAttachments()

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

    const attachments = useTestChatAttachments()
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
    const attachments = useTestChatAttachments()

    await attachments.addAttachments([new File([], 'empty.txt', { type: 'text/plain' })])

    expect(fetchMock).not.toHaveBeenCalled()
    expect(attachments.pendingAttachments.value).toHaveLength(0)
    expect(pushToast).toHaveBeenCalledWith('Empty file: empty.txt', { tone: 'danger' })
  })

  it('enforces the frontend aggregate attachment count before upload work starts', async () => {
    const fetchMock = vi.fn().mockResolvedValue(successfulUploadResponse('file-count'))
    vi.stubGlobal('fetch', fetchMock)
    const attachments = useTestChatAttachments()

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
    const attachments = useTestChatAttachments()

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
    const attachments = useTestChatAttachments()
    const hugePdf = new File([new Uint8Array(30 * 1024 * 1024 + 1)], 'huge.pdf', { type: 'application/pdf' })

    await attachments.addAttachments([hugePdf])

    expect(fetchMock).not.toHaveBeenCalled()
    expect(attachments.pendingAttachments.value).toHaveLength(0)
    expect(pushToast).toHaveBeenCalledWith('File too large: huge.pdf (max 30 MiB)', { tone: 'danger' })
  })

  it('never states a rounded-up cap the rejected file already satisfies', async () => {
    vi.stubGlobal('fetch', vi.fn())
    const attachments = useTestChatAttachments()
    const bigEmail = new File([new Uint8Array(2_000_001)], 'mail.eml', { type: 'message/rfc822' })

    await attachments.addAttachments([bigEmail])

    expect(attachments.pendingAttachments.value).toHaveLength(0)
    expect(pushToast).toHaveBeenCalledWith('File too large: mail.eml (max 1.9 MiB)', { tone: 'danger' })
  })

  it('emits a single total-size toast for a batch that overflows the aggregate cap', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    const attachments = useTestChatAttachments()
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
    const attachments = useTestChatAttachments()
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

    const attachments = useTestChatAttachments()
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

    const attachments = useTestChatAttachments()
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

    const attachments = useTestChatAttachments()
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

    const attachments = useTestChatAttachments()
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
    const attachments = useTestChatAttachments()
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

  it('refreshes a queued attachment collection without touching the composer', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        file_uuid: 'file-queued-fresh',
        expires_at: Date.now() / 1000 + 600,
        ttl_seconds: 600,
      }),
      text: async () => '',
    })
    vi.stubGlobal('fetch', fetchMock)
    const attachments = useTestChatAttachments()
    const composerAttachment: Attachment = {
      kind: 'inline',
      local_id: 1,
      name: 'draft.txt',
      mime: 'text/plain',
      data: 'ZHJhZnQ=',
    }
    const queuedAttachments: Attachment[] = [{
      kind: 'staged',
      local_id: 2,
      name: 'queued.pdf',
      mime: 'application/pdf',
      file_uuid: 'file-queued-expired',
      expires_at: Date.now() / 1000 - 1,
      file: stagedPdf('queued.pdf'),
    }]
    attachments.pendingAttachments.value = [composerAttachment]

    const ready = await attachments.prepareAttachmentsForSend({
      attachments: queuedAttachments,
      ownership: 'detached',
      isCurrent: () => true,
    })

    expect(ready).toBe(true)
    expect(attachments.pendingAttachments.value).toEqual([composerAttachment])
    expect(queuedAttachments).toMatchObject([
      { kind: 'staged', name: 'queued.pdf', file_uuid: 'file-queued-fresh' },
    ])
  })

  it('does not refresh staged uploads that are outside the expiration grace window', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    const attachments = useTestChatAttachments()
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
    const attachments = useTestChatAttachments()
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

    const ready = attachments.prepareAttachmentsForSend({
      ownership: 'composer',
      isCurrent: () => current,
    })

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
    const attachments = useTestChatAttachments()
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
    const attachments = useTestChatAttachments()
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
