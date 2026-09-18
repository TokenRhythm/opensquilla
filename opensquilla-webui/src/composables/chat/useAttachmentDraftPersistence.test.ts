import { describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'
import { useAttachmentDraftPersistence } from './useAttachmentDraftPersistence'
import type { AttachmentDraftScope, AttachmentDraftStore } from '@/utils/chat/attachmentDrafts'
import type { Attachment } from '@/types/chat'

const item: Attachment = { kind: 'staged', local_id: 1, name: 'draft.txt', mime: 'text/plain', size: 4, file_uuid: 'draft-file' }
const initial = { identity: 'gateway-and-user-A', sessionKey: 'session-A' }
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}
function fixture(overrides: Partial<AttachmentDraftStore> = {}, options: {
  ownerState?: () => unknown
  restore?: (restored: Attachment[], attachments: ReturnType<typeof ref<Attachment[]>>) => Promise<void | boolean>
} = {}) {
  const data = new Map<string, Attachment[]>()
  const versions = new Map<string, string | undefined>()
  const key = (scope: AttachmentDraftScope) => JSON.stringify(scope)
  const store = {
    load: vi.fn(async (scope: AttachmentDraftScope) => data.get(key(scope)) || []),
    save: vi.fn(async (scope: AttachmentDraftScope, attachments: readonly Attachment[], revision?: string) => {
      data.set(key(scope), [...attachments])
      versions.set(key(scope), revision)
    }),
    consume: vi.fn(async (scope: AttachmentDraftScope, revision: string, indexes: readonly number[]) => {
      if (versions.get(key(scope)) !== revision) return false
      data.set(key(scope), (data.get(key(scope)) || []).filter((_item, index) => !indexes.includes(index)))
      versions.delete(key(scope))
      return true
    }),
    ...overrides,
  }
  const scope = ref<AttachmentDraftScope | null>(initial)
  const attachments = ref<Attachment[]>([])
  const onError = vi.fn()
  const persistence = useAttachmentDraftPersistence({ attachments, scope: () => scope.value, store,
    beforeScopeChange: () => { attachments.value = [] },
    ownerState: options.ownerState,
    restore: restored => options.restore ? options.restore(restored, attachments) : Promise.resolve().then(() => { attachments.value = restored }), onError })
  return { data, versions, store, scope, attachments, persistence, onError }
}

async function ready(persistence: ReturnType<typeof useAttachmentDraftPersistence>) {
  await vi.waitFor(() => expect(persistence.restoring.value).toBe(false))
  await persistence.flush()
}

describe('attachment draft ownership', () => {
  it('restores a Blob-backed draft only for the matching gateway/user/session scope', async () => {
    const f = fixture({ load: vi.fn(async () => [{ ...item, file: new File(['text'], item.name) }]) })
    await ready(f.persistence)
    expect(f.store.load).toHaveBeenCalledWith(initial)
    expect(f.attachments.value[0].file?.size).toBe(4)
    f.persistence.dispose()
  })

  it('keeps the previous draft on navigation and restores the destination independently', async () => {
    const f = fixture()
    await ready(f.persistence)
    f.attachments.value = [item]
    f.persistence.retire()
    f.attachments.value = []
    f.scope.value = { ...initial, sessionKey: 'session-B' }
    await ready(f.persistence)
    expect(f.data.get(JSON.stringify(initial))).toEqual([item])
    expect(f.attachments.value).toEqual([])
    f.persistence.dispose()
  })

  it('clears only local draft data after composer ownership moves to an accepted queue/send', async () => {
    const f = fixture()
    await ready(f.persistence)
    f.attachments.value = [item]
    await f.persistence.flush()
    f.attachments.value = []
    await f.persistence.flush()
    expect(f.data.get(JSON.stringify(initial))).toEqual([])
    expect(Object.keys(f.store).sort()).toEqual(['consume', 'load', 'save'])
    f.persistence.dispose()
  })

  it('retains the source when a same-owner destination save fails', async () => {
    const f = fixture()
    await ready(f.persistence)
    f.attachments.value = [item]
    await f.persistence.flush()
    // The source checkpoint precedes the destination write, so fail only B.
    vi.mocked(f.store.save).mockImplementation(async (scope, attachments) => {
      if (scope.sessionKey === 'session-B') throw new Error('Destination quota exceeded')
      f.data.set(JSON.stringify(scope), [...attachments])
    })
    f.scope.value = { ...initial, sessionKey: 'session-B' }
    await f.persistence.flush()
    expect(f.data.get(JSON.stringify(initial))).toEqual([item])
    expect(f.data.has(JSON.stringify(f.scope.value))).toBe(false)
    expect(f.attachments.value).toEqual([item])
    expect(f.onError).toHaveBeenCalledOnce()
    f.persistence.dispose()
  })

  it('removes the handoff source only after the destination acknowledges its save', async () => {
    const f = fixture()
    await ready(f.persistence)
    f.attachments.value = [item]
    await f.persistence.flush()
    const destination = deferred<void>()
    vi.mocked(f.store.save).mockImplementation(async (scope, attachments) => {
      if (scope.sessionKey === 'session-B') await destination.promise
      f.data.set(JSON.stringify(scope), [...attachments])
    })
    f.scope.value = { ...initial, sessionKey: 'session-B' }
    await vi.waitFor(() => expect(f.store.save).toHaveBeenCalledWith(f.scope.value, [item], expect.any(String)))
    expect(f.data.get(JSON.stringify(initial))).toEqual([item])
    destination.resolve()
    await f.persistence.flush()
    expect(f.data.get(JSON.stringify(initial))).toEqual([])
    expect(f.data.get(JSON.stringify(f.scope.value))).toEqual([item])
    f.persistence.dispose()
  })

  it('consumes only accepted source entries after navigation and ignores duplicate acceptance', async () => {
    const f = fixture()
    await ready(f.persistence)
    const failed: Attachment = { ...item, local_id: 2, kind: 'failed', name: 'retry.txt', error: 'Retry upload' }
    f.attachments.value = [item, failed]
    const accepted = f.persistence.captureConsumption([item])!
    f.persistence.retire()
    f.attachments.value = []
    f.scope.value = { ...initial, sessionKey: 'session-B' }
    await accepted.consume()
    await accepted.consume()
    expect(f.data.get(JSON.stringify(initial))).toEqual([failed])
    expect(f.attachments.value).toEqual([])
    f.persistence.dispose()
  })

  it('a late acceptance cannot consume a newer draft with reused local attachment IDs', async () => {
    const f = fixture()
    await ready(f.persistence)
    f.attachments.value = [item]
    const accepted = f.persistence.captureConsumption([item])!
    const newer = { ...item, name: 'newer.txt' }
    f.attachments.value = [newer]
    f.persistence.retire()
    f.attachments.value = []
    f.scope.value = { ...initial, sessionKey: 'session-B' }
    await accepted.consume()
    expect(f.data.get(JSON.stringify(initial))).toEqual([newer])
    f.persistence.dispose()
  })

  it('consumes the captured identity without touching a different account at the same session key', async () => {
    const f = fixture()
    await ready(f.persistence)
    f.attachments.value = [item]
    const accepted = f.persistence.captureConsumption([item])!
    f.scope.value = { ...initial, identity: 'another-account' }
    await ready(f.persistence)
    const other = { ...item, name: 'other-account.txt' }
    f.attachments.value = [other]
    await accepted.consume()
    expect(f.data.get(JSON.stringify(initial))).toEqual([])
    expect(f.data.get(JSON.stringify(f.scope.value))).toEqual([other])
    expect(f.attachments.value).toEqual([other])
    f.persistence.dispose()
  })

  it.each(['during restore', 'after consumption'])("keeps another window's same-looking draft written %s on close", async timing => {
    let restoring: ReturnType<typeof deferred<void>> | undefined
    const restoreStarted = vi.fn()
    const f = fixture({}, { restore: async (restored, attachments) => {
      restoreStarted()
      if (restoring) await restoring.promise
      attachments.value = restored
    } })
    f.store.loadSnapshot = async scope => ({
      attachments: (f.data.get(JSON.stringify(scope)) || []).map(value => ({ ...value })),
      revision: f.versions.get(JSON.stringify(scope)),
    })
    await ready(f.persistence)
    f.attachments.value = [item]
    const accepted = f.persistence.captureConsumption([item])!
    f.persistence.retire()
    f.attachments.value = []
    f.scope.value = { ...initial, sessionKey: 'session-B' }
    await ready(f.persistence)
    f.persistence.retire()
    f.attachments.value = []
    restoreStarted.mockClear()
    if (timing === 'during restore') restoring = deferred<void>()
    f.scope.value = initial
    if (restoring) {
      await vi.waitFor(() => expect(restoreStarted).toHaveBeenCalledOnce())
      await f.store.save(initial, [{ ...item }], 'other-window-revision')
      restoring.resolve()
    }
    await ready(f.persistence)
    expect(accepted.isRestoredCurrent()).toBe(true)
    const onConsumed = vi.fn()
    await accepted.consumeCurrent(() => accepted.isRestoredCurrent(), onConsumed)
    expect(onConsumed).toHaveBeenCalledTimes(timing === 'during restore' ? 0 : 1)
    if (timing === 'after consumption') await f.store.save(initial, [{ ...item }], 'other-window-revision')
    f.persistence.dispose()
    await f.persistence.flush()
    expect(f.data.get(JSON.stringify(initial))).toEqual([item])
    expect(f.versions.get(JSON.stringify(initial))).toBe('other-window-revision')
  })

  it('invalidates owner edits once without rewriting attachment Blobs for every keystroke', async () => {
    const text = ref('original')
    const f = fixture({}, { ownerState: () => text.value })
    await ready(f.persistence)
    f.attachments.value = [item]
    await f.persistence.flush()
    const accepted = f.persistence.captureConsumption([item])!
    vi.mocked(f.store.save).mockClear()
    text.value = 'e'
    await f.persistence.flush()
    text.value = 'ed'
    text.value = 'edit'
    text.value = 'original'
    await f.persistence.flush()
    expect(f.store.save).toHaveBeenCalledOnce()
    const onConsumed = vi.fn()
    await accepted.consumeCurrent(() => true, onConsumed)
    expect(onConsumed).not.toHaveBeenCalled()
    expect(f.attachments.value).toEqual([item])
    f.persistence.dispose()
  })

  it('gateway/account changes never restore late source files into the new composer', async () => {
    const oldLoad = deferred<Attachment[]>()
    const f = fixture({ load: vi.fn().mockImplementationOnce(() => oldLoad.promise).mockResolvedValue([]) })
    await vi.waitFor(() => expect(f.store.load).toHaveBeenCalledWith(initial))
    f.scope.value = { identity: 'gateway-and-user-B', sessionKey: 'session-A' }
    oldLoad.resolve([item])
    await ready(f.persistence)
    expect(f.attachments.value).toEqual([])
    f.persistence.dispose()
  })

  it('a late restore never replaces attachments selected while IndexedDB was loading', async () => {
    const load = deferred<Attachment[]>()
    const f = fixture({ load: () => load.promise })
    f.attachments.value = [{ ...item, name: 'new.txt' }]
    load.resolve([item])
    await ready(f.persistence)
    expect(f.attachments.value[0].name).toBe('new.txt')
    f.persistence.dispose()
  })

  it('preserves structured workspace identity without reconstituting a native capability', async () => {
    const workspace = { kind: 'workspace' as const, local_id: 2, name: 'project.txt', mime: 'text/plain',
      workspaceFile: { workspaceId: 'project-A', relativePath: 'project.txt', name: 'project.txt', mime: 'text/plain' } }
    const f = fixture({ load: vi.fn(async () => [workspace]) })
    await ready(f.persistence)
    expect(f.attachments.value[0]).toEqual(workspace)
    expect(f.attachments.value[0]).not.toHaveProperty('token')
    expect(f.attachments.value[0]).not.toHaveProperty('file')
    f.persistence.dispose()
  })

  it('reports actual storage failure once while keeping the visible attachments', async () => {
    const f = fixture({ save: vi.fn(async () => { throw new Error('Storage quota exceeded') }) })
    await ready(f.persistence)
    f.attachments.value = [item]
    await f.persistence.flush()
    f.attachments.value = [{ ...item, name: 'changed.txt' }]
    await f.persistence.flush()
    expect(f.onError).toHaveBeenCalledTimes(1)
    expect(f.attachments.value).toHaveLength(1)
    f.persistence.dispose()
  })
})
