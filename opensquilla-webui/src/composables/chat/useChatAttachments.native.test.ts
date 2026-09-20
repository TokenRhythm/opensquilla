// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useChatAttachments } from './useChatAttachments'
import type { ArtifactContentAccess } from '@/modules/artifactWorkbench'
import type { NativeAttachmentReceipt, PlatformFilesApi } from '@/platform/types'

const pushToast = vi.hoisted(() => vi.fn())
vi.mock('@/composables/useToasts', () => ({ useToasts: () => ({ pushToast }) }))

const context = { gatewayInstanceId: 'instance-test', sessionKey: 'session-test', sessionId: 'session-id-test', sessionEpoch: 0 }
const selection = { token: 'selection-test', name: 'selected.txt', mime: 'text/plain', size: 4 }
const receipt = { file_uuid: 'file-test', name: selection.name, mime: selection.mime, size: selection.size }
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}
function fixture(overrides: Partial<PlatformFilesApi> = {}) {
  const uploadAttachment = vi.fn(async () => ({ fileUuid: 'ordinary-file' }))
  const native = {
    chooseAttachments: vi.fn(async () => [selection]),
    selectAttachmentFile: vi.fn(async () => selection),
    importAttachmentSelection: vi.fn(async (): Promise<NativeAttachmentReceipt> => receipt),
    cancelAttachmentSelections: vi.fn(async () => {}),
    ...overrides,
  }
  const attachments = useChatAttachments({ uploadAttachment } as unknown as ArtifactContentAccess, {
    native, nativeContext: () => context,
  })
  return { attachments, native, uploadAttachment }
}

describe('native chat attachment intake', () => {
  beforeEach(() => pushToast.mockClear())

  it('uses the same receipt for picked and dropped files without reading renderer bytes', async () => {
    const { attachments, native, uploadAttachment } = fixture()
    await attachments.chooseAttachments()
    const file = new File(['text'], selection.name, { type: selection.mime })
    const arrayBuffer = vi.spyOn(file, 'arrayBuffer')
    await attachments.addAttachments([file])
    expect(attachments.pendingAttachments.value).toMatchObject([
      { kind: 'staged', name: selection.name, file_uuid: receipt.file_uuid },
      { kind: 'staged', name: selection.name, file_uuid: receipt.file_uuid },
    ])
    expect(native.importAttachmentSelection).toHaveBeenCalledTimes(2)
    expect(native.selectAttachmentFile).toHaveBeenCalledExactlyOnceWith(context, file)
    expect(arrayBuffer).not.toHaveBeenCalled()
    expect(uploadAttachment).not.toHaveBeenCalled()
  })

  it('blocks immediate send while picker and native import are pending', async () => {
    const picker = deferred<typeof selection[]>()
    const imported = deferred<NativeAttachmentReceipt>()
    const { attachments } = fixture({ chooseAttachments: () => picker.promise,
      importAttachmentSelection: () => imported.promise })
    const adding = attachments.chooseAttachments()
    expect(attachments.hasPendingAttachmentWork()).toBe(true)
    await Promise.resolve()
    picker.resolve([selection])
    await vi.waitFor(() => expect(attachments.pendingAttachments.value[0]?.kind).toBe('uploading'))
    expect(attachments.hasPendingAttachmentWork()).toBe(true)
    imported.resolve(receipt)
    await adding
    expect(attachments.hasPendingAttachmentWork()).toBe(false)
  })

  it('retiring the draft cancels native selection and discards a late picker response', async () => {
    const picker = deferred<typeof selection[]>()
    const { attachments, native } = fixture({ chooseAttachments: () => picker.promise })
    const adding = attachments.chooseAttachments()
    await Promise.resolve()
    attachments.retireAttachments()
    picker.resolve([selection])
    await adding
    expect(native.cancelAttachmentSelections).toHaveBeenCalledOnce()
    expect(native.importAttachmentSelection).not.toHaveBeenCalled()
    expect(attachments.pendingAttachments.value).toEqual([])
    expect(attachments.hasPendingAttachmentWork()).toBe(false)
  })

  it('removing an in-flight native attachment does not restore it on import completion', async () => {
    const imported = deferred<NativeAttachmentReceipt>()
    const { attachments } = fixture({ importAttachmentSelection: () => imported.promise })
    const adding = attachments.addAttachments([new File(['text'], selection.name)])
    await vi.waitFor(() => expect(attachments.pendingAttachments.value).toHaveLength(1))
    attachments.removeAttachment(0)
    imported.resolve(receipt)
    await adding
    expect(attachments.pendingAttachments.value).toEqual([])
  })

  it('does not persist drop bytes while the native import decision is still pending', async () => {
    const imported = deferred<NativeAttachmentReceipt>()
    const store = { load: vi.fn(async () => []), save: vi.fn(async () => {}) }
    const attachments = useChatAttachments(undefined, {
      native: { selectAttachmentFile: async () => selection, importAttachmentSelection: () => imported.promise },
      nativeContext: () => context,
      draftScope: () => ({ identity: 'fixture-owner', sessionKey: 'fixture-session' }), draftStore: store,
    })
    await vi.waitFor(() => expect(attachments.hasPendingAttachmentWork()).toBe(false))
    const file = new File(['text'], selection.name)
    const adding = attachments.addAttachments([file])
    await vi.waitFor(() => expect(attachments.pendingAttachments.value[0]?.kind).toBe('uploading'))
    await attachments.flushAttachmentDraft()
    expect(store.save).toHaveBeenLastCalledWith(expect.anything(), [expect.not.objectContaining({ file })], expect.stringMatching(/\S/))
    expect(attachments.pendingAttachments.value[0].file).toBeUndefined()
    imported.resolve(receipt)
    await adding
    await attachments.flushAttachmentDraft()
    expect(await attachments.pendingAttachments.value[0].file?.text()).toBe('text')
  })

  it('native permission denial remains a failed attachment and never becomes a byte upload', async () => {
    const { attachments, uploadAttachment } = fixture({
      importAttachmentSelection: vi.fn(async () => { throw new Error('Permission denied') }),
    })
    await attachments.addAttachments([new File(['text'], selection.name)])
    expect(attachments.pendingAttachments.value[0]).toMatchObject({ kind: 'failed', error: 'Permission denied' })
    expect(attachments.pendingAttachments.value[0].file).toBeUndefined()
    expect(uploadAttachment).not.toHaveBeenCalled()
  })

  it('synthetic screenshot Files follow normal content handling when preload reports no native path', async () => {
    const { attachments, native } = fixture({ selectAttachmentFile: vi.fn(async () => null) })
    const image = new File(['png'], 'screenshot.png', { type: 'image/png' })
    await attachments.addAttachments([image])
    await vi.waitFor(() => expect(attachments.pendingAttachments.value[0]?.kind).toBe('inline'))
    expect(native.importAttachmentSelection).not.toHaveBeenCalled()
    expect(attachments.pendingAttachments.value[0]).toMatchObject({ mime: 'image/png', file: image })
  })

  it('workspace receipts preserve live file identity and cannot refresh into uploaded copies', async () => {
    const workspaceFile = { workspaceId: 'project-test', relativePath: selection.name,
      name: selection.name, mime: selection.mime, size: selection.size }
    const { attachments, uploadAttachment } = fixture({
      importAttachmentSelection: async () => ({ ...receipt, file_uuid: undefined, workspaceFile }),
    })
    await attachments.addAttachments([new File(['text'], selection.name)])
    expect(attachments.pendingAttachments.value[0]).toMatchObject({ kind: 'workspace', workspaceFile })
    expect(attachments.pendingAttachments.value[0].file).toBeUndefined()
    expect(await attachments.prepareAttachmentsForSend()).toBe(true)
    expect(uploadAttachment).not.toHaveBeenCalled()
  })
  it('automatically prepares restored browser Blob bytes without restoring native authority', async () => {
    const native = { selectAttachmentFile: vi.fn(async () => null),
      importAttachmentSelection: vi.fn(async () => receipt) }
    const store = { load: vi.fn(async () => [{ kind: 'failed' as const, local_id: 1,
      name: 'draft.txt', mime: 'text/plain', size: 4, file: new File(['text'], 'draft.txt'),
      error: 'Draft restored; retry to prepare the file' }]), save: vi.fn(async () => {}) }
    const attachments = useChatAttachments(undefined, { native, nativeContext: () => context,
      draftScope: () => ({ identity: 'gateway-user-test', sessionKey: context.sessionKey }), draftStore: store })
    await vi.waitFor(() => expect(attachments.pendingAttachments.value[0]?.kind).toBe('inline'))
    await attachments.flushAttachmentDraft()
    expect(attachments.pendingAttachments.value[0]).toMatchObject({ name: 'draft.txt', data: 'dGV4dA==' })
    expect(native.importAttachmentSelection).not.toHaveBeenCalled()
    expect(attachments.hasPendingAttachmentWork()).toBe(false)
  })

  it('saves a new file after retiring a draft while the session scope stays the same', async () => {
    const store = { load: vi.fn(async () => []), save: vi.fn(async () => {}) }
    const attachments = useChatAttachments(undefined, {
      draftScope: () => ({ identity: 'fixture-owner', sessionKey: 'fixture-session' }), draftStore: store,
    })
    await vi.waitFor(() => expect(attachments.hasPendingAttachmentWork()).toBe(false))
    await attachments.addAttachments([new File(['old'], 'old.txt', { type: 'text/plain' })])
    await vi.waitFor(() => expect(attachments.pendingAttachments.value[0]?.kind).toBe('inline'))
    attachments.retireAttachments()
    await attachments.addAttachments([new File(['new'], 'new.txt', { type: 'text/plain' })])
    await vi.waitFor(() => expect(attachments.pendingAttachments.value[0]?.kind).toBe('inline'))
    await attachments.flushAttachmentDraft()
    expect(store.save).toHaveBeenLastCalledWith(
      { identity: 'fixture-owner', sessionKey: 'fixture-session' },
      [expect.objectContaining({ name: 'new.txt', data: 'bmV3' })],
      expect.stringMatching(/\S/),
    )
  })

  it('a new task without durable native session identity uses the ordinary picker and content path', async () => {
    const native = { chooseAttachments: vi.fn(async () => [selection]),
      selectAttachmentFile: vi.fn(async () => selection), importAttachmentSelection: vi.fn(async () => receipt) }
    const attachments = useChatAttachments(undefined, { native, nativeContext: () => null })
    expect(await attachments.chooseAttachments()).toBe(false)
    expect(native.chooseAttachments).not.toHaveBeenCalled()
    await attachments.addAttachments([new File(['text'], 'new-task.txt', { type: 'text/plain' })])
    await vi.waitFor(() => expect(attachments.pendingAttachments.value[0]?.kind).toBe('inline'))
    expect(native.selectAttachmentFile).not.toHaveBeenCalled()
    expect(native.importAttachmentSelection).not.toHaveBeenCalled()
  })

  it('a session epoch change while native import waits cannot attach its stale receipt', async () => {
    const imported = deferred<NativeAttachmentReceipt>()
    let current = true
    const attachments = useChatAttachments(undefined, {
      native: { chooseAttachments: async () => [selection], importAttachmentSelection: () => imported.promise },
      nativeContext: () => context, nativeIsCurrent: () => current,
    })
    const adding = attachments.chooseAttachments()
    await vi.waitFor(() => expect(attachments.pendingAttachments.value[0]?.kind).toBe('uploading'))
    current = false
    imported.resolve(receipt)
    await adding
    expect(attachments.pendingAttachments.value[0]).toMatchObject({ kind: 'failed', error: 'Session changed; select the file again' })
    expect(attachments.pendingAttachments.value[0].file_uuid).toBeUndefined()
    expect(attachments.pendingAttachments.value[0].file).toBeUndefined()
  })

})
