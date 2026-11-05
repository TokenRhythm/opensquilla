// @vitest-environment happy-dom
import { describe, expect, it, vi } from 'vitest'
import type { ArtifactContentAccess } from '@/modules/artifactWorkbench'
import type { ChatMessage, DisplayAttachment } from '@/types/chat'
import type { NativeAttachmentContext, NativeAttachmentSelection, PlatformFilesApi } from '@/platform/types'
import { useChatAttachments } from '@/composables/chat/useChatAttachments'
import { isSendableAttachment, serializeChatFiles, serializeDisplayAttachment } from './attachments'
import { recoverMessageAttachments } from './regenerateAttachments'

vi.mock('@/composables/useToasts', () => ({ useToasts: () => ({ pushToast: vi.fn() }) }))

const display = (overrides: Partial<DisplayAttachment> = {}): DisplayAttachment => ({
  kind: 'staged', displayId: 'image', renderKey: 'image', name: 'shape.png', mime: 'image/png',
  download_url: '/api/v1/attachments/original', ...overrides,
})
function setup(blob = new Blob(['original'], { type: 'image/png' })) {
  const content = {
    fetchAttachment: vi.fn(async () => ({ ok: true as const, status: 200, source: 'staged' as const,
      url: '/api/v1/attachments/original', blob, filename: 'shape.png' })),
    uploadAttachment: vi.fn(async () => ({ fileUuid: 'fresh-upload', expiresAt: 12345, ttlSeconds: 3600 })),
  } satisfies Pick<ArtifactContentAccess, 'fetchAttachment' | 'uploadAttachment'>
  const message: ChatMessage = { role: 'user', ts: null, text: 'What is this?', attachments: [display()] }
  const controller = new AbortController()
  return { content, message, controller }
}

describe('original message attachment recovery', () => {
  it.each([
    ['scan.tiff', 'image/tiff'],
    ['scan.bmp', 'image/bmp'],
    ['drawing.svg', 'image/svg+xml'],
  ])('recovers a normally accepted 6 MiB opaque %s without applying the model-image cap', async (name, mime) => {
    const size = 6 * 1024 * 1024
    const { content, message, controller } = setup(new Blob([new Uint8Array(size)], { type: mime }))
    const context: NativeAttachmentContext = { gatewayInstanceId: 'gateway', sessionKey: 'source', sessionId: 'id', sessionEpoch: 0 }
    const selection: NativeAttachmentSelection = { token: 'file', name, mime, size }
    const native: Pick<PlatformFilesApi, 'chooseAttachments' | 'importAttachmentSelection'> = {
      chooseAttachments: async () => [selection],
      importAttachmentSelection: async () => ({ ...selection, file_uuid: 'original-upload' }),
    }
    const intake = useChatAttachments(undefined, { native, nativeContext: () => context })
    await intake.chooseAttachments()
    const accepted = intake.pendingAttachments.value.filter(isSendableAttachment)
    expect(accepted).toHaveLength(1)
    message.attachments = accepted.map(serializeDisplayAttachment)

    const recovered = await recoverMessageAttachments(content, message, 'source', controller.signal)
    expect(content.fetchAttachment).toHaveBeenCalledWith(message.attachments[0], {
      sessionKey: 'source', signal: controller.signal, maxBytes: 50 * 1024 * 1024,
    })
    expect(recovered).toEqual([expect.objectContaining({ kind: 'staged', name, mime, size, file_uuid: 'fresh-upload' })])
  })

  it.each(['message/rfc822', 'application/mbox', 'application/vnd.ms-outlook'])(
    'retains the 2 MB payload ceiling for %s even when the reader ignores its bound', async mime => {
      const { content, message, controller } = setup(new Blob([new Uint8Array(2_000_001)], { type: mime }))
      message.attachments = [display({ name: 'message', mime })]
      await expect(recoverMessageAttachments(content, message, 'source', controller.signal)).rejects.toThrow('too large')
      expect(content.fetchAttachment).toHaveBeenCalledWith(message.attachments[0], {
        sessionKey: 'source', signal: controller.signal, maxBytes: 2_000_000,
      })
      expect(content.uploadAttachment).not.toHaveBeenCalled()
    },
  )

  it('can stage a bounded email when preceding attachments fill the inline budget', async () => {
    const { content, message, controller } = setup(new Blob([new Uint8Array(1_500_000)], { type: 'message/rfc822' }))
    message.attachments = [display({ name: 'one.eml', mime: 'message/rfc822' }), display({ name: 'two.eml', mime: 'message/rfc822' })]
    const recovered = await recoverMessageAttachments(content, message, 'source', controller.signal)
    expect(recovered.map(attachment => attachment.kind)).toEqual(['inline', 'staged'])
    expect(content.uploadAttachment).toHaveBeenCalledWith(expect.objectContaining({ size: 1_500_000, name: 'two.eml' }), 'message/rfc822')
    expect(serializeChatFiles(recovered.filter(isSendableAttachment)).attachments[1]).toMatchObject({
      mime: 'message/rfc822', name: 'two.eml', file_uuid: 'fresh-upload',
    })
  })

  it('keeps a large native workspace email reference without treating it as transported email bytes', async () => {
    const { content, message, controller } = setup()
    const workspaceFile = { workspaceId: 'project', relativePath: 'large.eml', name: 'large.eml', mime: 'message/rfc822', size: 6 * 1024 * 1024 }
    message.attachments = [display({ download_url: undefined, name: workspaceFile.name, mime: workspaceFile.mime, workspaceFile })]
    const recovered = await recoverMessageAttachments(content, message, 'source', controller.signal)
    expect(recovered[0]).toMatchObject({ kind: 'workspace', workspaceFile })
    expect(content.fetchAttachment).not.toHaveBeenCalled()
    expect(content.uploadAttachment).not.toHaveBeenCalled()
  })

  it.each(['staged', 'inline'] as const)('keeps a full batch of small %s images within the RPC frame budget', async (kind) => {
    const { content, message, controller } = setup(new Blob([new Uint8Array(1_700_000)], { type: 'image/png' }))
    const count = kind === 'staged' ? 12 : 16
    const context: NativeAttachmentContext = { gatewayInstanceId: 'gateway', sessionKey: 'source', sessionId: 'id', sessionEpoch: 0 }
    const choices: NativeAttachmentSelection[] = Array.from({ length: count }, (_, i) => ({
      token: String(i), name: `image-${i}.png`, mime: 'image/png', size: 1_700_000,
    }))
    const native: Pick<PlatformFilesApi, 'chooseAttachments' | 'importAttachmentSelection'> = {
      chooseAttachments: async () => choices,
      importAttachmentSelection: async (_context, token) => ({ ...choices[Number(token)]!, file_uuid: `old-${token}` }),
    }
    const intake = useChatAttachments(undefined, { native, nativeContext: () => context })
    await intake.chooseAttachments()
    expect(intake.pendingAttachments.value).toHaveLength(count)
    const accepted = intake.pendingAttachments.value.filter(isSendableAttachment)
    expect(accepted).toHaveLength(count)
    expect(new TextEncoder().encode(JSON.stringify(serializeChatFiles(accepted))).length).toBeLessThan(25 * 1024 * 1024)
    message.attachments = accepted.map(attachment => ({
      ...serializeDisplayAttachment(attachment), kind,
      download_url: '/api/v1/attachments/original',
    }))
    let upload = 0
    content.uploadAttachment.mockImplementation(async () => ({ fileUuid: `fresh-${++upload}`, expiresAt: 12345, ttlSeconds: 3600 }))

    const recovered = await recoverMessageAttachments(content, message, 'source', controller.signal)
    const sendable = recovered.filter(isSendableAttachment)
    expect(sendable).toHaveLength(count)
    expect(sendable.map(attachment => attachment.name)).toEqual(choices.map(choice => choice.name))
    const wire = serializeChatFiles(sendable)
    expect(new TextEncoder().encode(JSON.stringify(wire)).length).toBeLessThan(25 * 1024 * 1024)
    expect(wire.attachments.some(attachment => attachment.file_uuid?.startsWith('fresh-'))).toBe(true)
    expect(wire.attachments.every(attachment => !attachment.file_uuid?.startsWith('old-'))).toBe(true)
  })

  it('preserves a normally accepted native workspace batch without charging references to the upload budget', async () => {
    const { content, message, controller } = setup()
    const context: NativeAttachmentContext = { gatewayInstanceId: 'gateway', sessionKey: 'source', sessionId: 'id', sessionEpoch: 0 }
    const choices: NativeAttachmentSelection[] = Array.from({ length: 13 }, (_, i) => ({
      token: String(i), name: `image-${i}.png`, mime: 'image/png', size: 5 * 1024 * 1024,
    }))
    const native: Pick<PlatformFilesApi, 'chooseAttachments' | 'importAttachmentSelection'> = {
      chooseAttachments: async () => choices,
      importAttachmentSelection: async (_context, token) => ({ ...choices[Number(token)]!, workspaceFile: {
        workspaceId: 'project', relativePath: `image-${token}.png`, name: `image-${token}.png`, mime: 'image/png', size: 5 * 1024 * 1024,
      } }),
    }
    const intake = useChatAttachments(undefined, { native, nativeContext: () => context })
    await intake.chooseAttachments()
    const accepted = intake.pendingAttachments.value.filter(isSendableAttachment)
    expect(accepted).toHaveLength(13)
    message.attachments = accepted.map(serializeDisplayAttachment)
    const recovered = await recoverMessageAttachments(content, message, 'source', controller.signal)
    expect(serializeChatFiles(recovered.filter(isSendableAttachment))).toEqual(serializeChatFiles(accepted))
    expect(content.fetchAttachment).not.toHaveBeenCalled()
    expect(content.uploadAttachment).not.toHaveBeenCalled()
  })

  it('keeps upload bytes and workspace reference sizes on separate budgets in a mixed batch', async () => {
    const { content, message, controller } = setup(new Blob([new Uint8Array(50 * 1024 * 1024)]))
    message.attachments = [
      display({ name: 'large.pdf', mime: 'application/pdf' }),
      ...Array.from({ length: 3 }, (_, index) => display({ download_url: undefined, workspaceFile: {
        workspaceId: 'project', relativePath: `image-${index}.png`, name: `image-${index}.png`, mime: 'image/png', size: 5 * 1024 * 1024,
      } })),
    ]
    const recovered = await recoverMessageAttachments(content, message, 'source', controller.signal)
    expect(recovered).toHaveLength(4)
    expect(content.fetchAttachment).toHaveBeenCalledOnce()
    expect(content.uploadAttachment).toHaveBeenCalledOnce()
  })

  it('still rejects actual payload bytes exceeding the aggregate upload budget', async () => {
    const { content, message, controller } = setup(new Blob([new Uint8Array(31 * 1024 * 1024)]))
    message.attachments = [display({ name: 'one.pdf', mime: 'application/pdf' }), display({ name: 'two.pdf', mime: 'application/pdf' })]
    await expect(recoverMessageAttachments(content, message, 'source', controller.signal)).rejects.toThrow('too large')
    expect(content.fetchAttachment).toHaveBeenLastCalledWith(message.attachments[1], {
      sessionKey: 'source', signal: controller.signal, maxBytes: 29 * 1024 * 1024,
    })
  })

  it('reads authorized original session bytes and creates an independent send payload', async () => {
    const { content, message, controller } = setup()
    const recovered = await recoverMessageAttachments(content, message, 'source-session', controller.signal)
    expect(content.fetchAttachment).toHaveBeenCalledWith(message.attachments![0], {
      sessionKey: 'source-session', signal: controller.signal, maxBytes: 5 * 1024 * 1024,
    })
    expect(recovered).toEqual([expect.objectContaining({ kind: 'inline', name: 'shape.png', data: btoa('original') })])
    expect(content.uploadAttachment).not.toHaveBeenCalled()
    recovered[0]!.name = 'changed.png'
    expect(message.attachments![0]!.name).toBe('shape.png')
  })

  it('preserves a native workspace-bound reference without borrowing mutable source metadata', async () => {
    const { content, message, controller } = setup()
    const workspaceFile = { workspaceId: 'workspace-original', relativePath: 'uploads/image.png', name: 'image.png', mime: 'image/png', size: 123 }
    message.attachments = [display({ download_url: undefined, workspaceFile })]
    const recovered = await recoverMessageAttachments(content, message, 'source-session', controller.signal)
    expect(recovered[0]).toMatchObject({ kind: 'workspace', workspaceFile })
    expect(content.fetchAttachment).not.toHaveBeenCalled()
    workspaceFile.relativePath = 'uploads/unrelated.png'
    expect(recovered[0]?.workspaceFile?.relativePath).toBe('uploads/image.png')
  })

  it('prefers retained original bytes over a workspace path on historical messages', async () => {
    const { content, message, controller } = setup()
    message.attachments![0]!.workspaceFile = { workspaceId: 'workspace-original', relativePath: 'uploads/image.png', name: 'image.png', mime: 'image/png' }
    const recovered = await recoverMessageAttachments(content, message, 'source-session', controller.signal)
    expect(content.fetchAttachment).toHaveBeenCalledOnce()
    expect(recovered[0]).toMatchObject({ kind: 'inline', data: btoa('original') })
    expect(recovered[0]?.workspaceFile).toBeUndefined()
  })

  it('reuploads larger historical files using fresh staging metadata', async () => {
    const { content, message, controller } = setup(new Blob([new Uint8Array(2_000_001)], { type: 'image/png' }))
    const recovered = await recoverMessageAttachments(content, message, 'source', controller.signal)
    expect(content.uploadAttachment).toHaveBeenCalledOnce()
    expect(recovered[0]).toMatchObject({ kind: 'staged', file_uuid: 'fresh-upload', expires_at: 12345, ttl_seconds: 3600 })
    expect(recovered[0]?.file?.size).toBe(2_000_001)
    expect(recovered[0]?.data).toBeUndefined()
  })

  it('refuses oversized in-memory images even if the provider ignores the read bound', async () => {
    const { content, message, controller } = setup(new Blob([new Uint8Array(5 * 1024 * 1024 + 1)]))
    await expect(recoverMessageAttachments(content, message, 'source', controller.signal)).rejects.toThrow('too large')
    expect(content.uploadAttachment).not.toHaveBeenCalled()
  })

  it('fails the entire recovery when one of multiple original attachments is unavailable', async () => {
    const { content, message, controller } = setup()
    message.attachments!.push(display({ name: 'missing.png' }))
    content.fetchAttachment.mockRejectedValueOnce(new Error('Original material expired'))
    await expect(recoverMessageAttachments(content, message, 'source', controller.signal)).rejects.toThrow('expired')
    expect(message.attachments).toHaveLength(2)
    expect(content.uploadAttachment).not.toHaveBeenCalled()
  })

  it('does not continue into staging after cancellation of a historical read', async () => {
    const { content, message, controller } = setup(new Blob([new Uint8Array(2_000_001)]))
    const originalRead = content.fetchAttachment.getMockImplementation()!
    content.fetchAttachment.mockImplementation(async () => {
      const result = await originalRead()
      controller.abort()
      return result
    })
    await expect(recoverMessageAttachments(content, message, 'source', controller.signal)).rejects.toMatchObject({ name: 'AbortError' })
    expect(content.uploadAttachment).not.toHaveBeenCalled()
  })

  it('discards a completed upload when the session was cancelled during upload', async () => {
    const { content, message, controller } = setup(new Blob([new Uint8Array(2_000_001)]))
    content.uploadAttachment.mockImplementation(async () => {
      controller.abort()
      return { fileUuid: 'late-upload', expiresAt: 12345, ttlSeconds: 3600 }
    })
    await expect(recoverMessageAttachments(content, message, 'source', controller.signal)).rejects.toMatchObject({ name: 'AbortError' })
  })
})
