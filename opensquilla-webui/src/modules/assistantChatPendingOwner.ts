import { toRaw } from 'vue'
import type { PendingInputWal, PendingInputWalRecord } from '@/utils/chat/pendingInputWal'
import { createPendingQueuePolicy } from '@/utils/chat/pendingQueuePolicy'
import { createMemoryAttachmentDraftStore, type AttachmentDraftStore } from '@/utils/chat/attachmentDrafts'

/** Temporary backing for the existing queue contract, owned by the side tab.
 * Hiding/remounting a reader retains it; retiring its owner releases it. No
 * side draft or child binding is written to IndexedDB or localStorage. */
const owners = new Map<string, ReturnType<typeof createOwner>>()

function cloneRecord(record: PendingInputWalRecord): PendingInputWalRecord {
  return structuredClone({ ...toRaw(record), attachments: record.attachments.map(item => ({ ...toRaw(item) })) })
}

function createOwner() {
  const records = new Map<string, PendingInputWalRecord>()
  let closed = false
  const drafts = new Map<string, string>()
  const draftStorage: Pick<Storage, 'getItem' | 'setItem' | 'removeItem'> = {
    getItem: key => drafts.get(key) ?? null,
    setItem: (key, value) => { if (closed) throw new Error('Side conversation is closed'); drafts.set(key, value) },
    removeItem: key => { drafts.delete(key) },
  }
  const policy = createPendingQueuePolicy(draftStorage)
  const attachmentMemory = createMemoryAttachmentDraftStore()
  const attachmentDraftStore: AttachmentDraftStore = {
    load: scope => closed ? Promise.resolve([]) : attachmentMemory.load(scope),
    loadSnapshot: scope => closed ? Promise.resolve({ attachments: [] }) : attachmentMemory.loadSnapshot!(scope),
    save: async (scope, attachments, revision) => {
      if (closed) throw new Error('Side conversation is closed')
      await attachmentMemory.save(scope, attachments, revision)
    },
    consume: (scope, revision, indexes) => closed ? Promise.resolve(false) : attachmentMemory.consume!(scope, revision, indexes),
  }
  const put = (record: PendingInputWalRecord) => {
    if (closed) throw new Error('Side conversation is closed')
    records.set(record.pendingInputId, cloneRecord(record))
  }
  const wal: PendingInputWal = {
    put: async record => put(record),
    putMany: async rows => { rows.forEach(put) },
    list: async key => [...records.values()].filter(row => row.sessionKey === key).map(cloneRecord),
    delete: async id => { records.delete(id) },
    commitOrder: async (key, ids, revisions) => {
      const rows = ids.map((id, position) => {
        const row = records.get(id)
        if (!row || row.sessionKey !== key || row.walRevision !== revisions[id]) throw new Error('Queue order changed')
        return { ...row, position, walRevision: (row.walRevision || 0) + 1, updatedAt: Date.now() }
      })
      rows.forEach(put)
      return { records: rows.map(cloneRecord) }
    },
    // This closes a reader. Only explicit owner retirement clears the data.
    close: () => {},
  }
  return { wal, policy, draftStorage, attachmentDraftStore, isClosed: () => closed,
    close: () => { closed = true; records.clear(); drafts.clear(); attachmentMemory.clear() } }
}

export function assistantChatPendingOwner(binding: string) {
  let owner = owners.get(binding)
  if (!owner) { owner = createOwner(); owners.set(binding, owner) }
  return owner
}

export function retireAssistantChatPendingOwner(binding: string) {
  owners.get(binding)?.close()
  owners.delete(binding)
}
