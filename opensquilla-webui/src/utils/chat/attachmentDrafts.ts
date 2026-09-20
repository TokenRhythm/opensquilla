import type { Attachment, WorkspaceFileReference } from '@/types/chat'
import { createClientRequestId } from './messageIdentity'

const DATABASE = 'opensquilla-attachment-drafts'
const STORE = 'drafts'
export const ATTACHMENT_DRAFT_TTL_MS = 24 * 60 * 60 * 1000
export const ATTACHMENT_DRAFT_MAX_BYTES = 60 * 1024 * 1024
export const ATTACHMENT_DRAFT_TOTAL_BYTES = 120 * 1024 * 1024
const MAX_DRAFTS = 20

export interface AttachmentDraftScope { identity: string; sessionKey: string }
interface StoredAttachment {
  name: string; mime: string; size: number
  blob?: Blob
  workspaceFile?: WorkspaceFileReference
  fileUuid?: string; expiresAt?: number; ttlSeconds?: number
  error?: string
}
interface DraftRecord {
  key: string; version: 1; updatedAt: number; expiresAt: number; bytes: number
  revision?: string
  attachments: StoredAttachment[]
}
export interface AttachmentDraftStore {
  load(scope: AttachmentDraftScope): Promise<Attachment[]>
  loadSnapshot?(scope: AttachmentDraftScope): Promise<{ attachments: Attachment[]; revision?: string }>
  save(scope: AttachmentDraftScope, attachments: readonly Attachment[], revision?: string): Promise<void>
  consume?(scope: AttachmentDraftScope, revision: string, indexes: readonly number[]): Promise<boolean>
}
export interface AttachmentDraftConsumption {
  consume(): Promise<void>
  isRestoredCurrent(): boolean
  consumeCurrent(isCurrent: () => boolean, onConsumed: () => void, isOriginal?: () => boolean): Promise<void>
}

export function attachmentDraftKey(scope: AttachmentDraftScope): string {
  if (!scope.identity || scope.identity.length > 8192 || !scope.sessionKey || scope.sessionKey.length > 512) {
    throw new Error('Attachment draft identity is unavailable')
  }
  return JSON.stringify([scope.identity, scope.sessionKey])
}
function inlineBlob(attachment: Attachment): Blob | undefined {
  if (attachment.file instanceof Blob) return attachment.file.slice(0, attachment.file.size, attachment.mime)
  const data = attachment.data || attachment.dataUrl?.match(/^data:[^;,]+;base64,([A-Za-z0-9+/=]+)$/)?.[1]
  if (!data) return undefined
  if (data.length > Math.ceil(30 * 1024 * 1024 * 4 / 3) + 4) throw new Error('Attachment draft exceeds the file size limit')
  const decoded = atob(data)
  const bytes = Uint8Array.from(decoded, character => character.charCodeAt(0))
  return new Blob([bytes], { type: attachment.mime })
}
function storedAttachment(attachment: Attachment): StoredAttachment {
  const base = { name: attachment.name, mime: attachment.mime, size: attachment.size ?? 0 }
  if (attachment.kind === 'workspace' && attachment.workspaceFile) {
    // Persist identity, never a native token or a reusable filesystem authority.
    return { ...base, workspaceFile: { ...attachment.workspaceFile } }
  }
  const blob = inlineBlob(attachment)
  return { ...base, size: blob?.size ?? base.size,
    ...(blob ? { blob } : {}),
    ...(attachment.kind === 'staged' && attachment.file_uuid ? {
      fileUuid: attachment.file_uuid, expiresAt: attachment.expires_at, ttlSeconds: attachment.ttl_seconds,
    } : {}),
    ...(attachment.error ? { error: attachment.error } : {}),
  }
}
function restore(record: DraftRecord, now: number): Attachment[] {
  if (record.version !== 1 || !Array.isArray(record.attachments) || record.attachments.length > 10) {
    throw new Error('Saved attachment draft is invalid; select the files again')
  }
  let bytes = 0
  return record.attachments.map((item, index) => {
    if (!item || typeof item.name !== 'string' || typeof item.mime !== 'string'
      || !Number.isSafeInteger(item.size) || item.size < 0) throw new Error('Saved attachment draft is invalid')
    bytes += item.blob instanceof Blob ? item.blob.size : item.size
    if (bytes > ATTACHMENT_DRAFT_MAX_BYTES) throw new Error('Saved attachment draft exceeds its size limit')
    const base = { local_id: index + 1, name: item.name, mime: item.mime, size: item.size }
    if (item.workspaceFile) {
      const ref = item.workspaceFile
      if (typeof ref.workspaceId !== 'string' || typeof ref.relativePath !== 'string'
        || !ref.workspaceId || !ref.relativePath || ref.relativePath.startsWith('/')
        || ref.relativePath.includes('\\') || ref.relativePath.split('/').some(part => !part || part === '..' || part === '.')) {
        throw new Error('Saved project file reference is invalid; select the file again')
      }
      return { ...base, kind: 'workspace', workspaceFile: { ...ref } }
    }
    if (item.blob instanceof Blob && item.blob.size !== item.size) throw new Error('Saved attachment size changed')
    const file = item.blob instanceof Blob ? new File([item.blob], item.name, { type: item.mime }) : undefined
    if (item.fileUuid && typeof item.fileUuid === 'string'
      && (file || (typeof item.expiresAt === 'number' && item.expiresAt * 1000 > now))) {
      return { ...base, kind: 'staged', file_uuid: item.fileUuid,
        expires_at: item.expiresAt ?? 0, ttl_seconds: item.ttlSeconds, file }
    }
    // Restored bytes go through the ordinary bounded upload path before send.
    // No native path/selection survives a reload, including browser File objects.
    return { ...base, kind: 'failed', file,
      error: item.error || (file ? 'Draft restored; retry to prepare the file' : 'Select the file again to restore this attachment') }
  })
}
function complete(transaction: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve()
    transaction.onerror = transaction.onabort = () => reject(transaction.error || new Error('Unable to save attachment draft'))
  })
}

export class IndexedDbAttachmentDraftStore implements AttachmentDraftStore {
  private databasePromise: Promise<IDBDatabase> | null = null
  constructor(private readonly indexedDb: IDBFactory, private readonly now: () => number = Date.now) {}
  private database(): Promise<IDBDatabase> {
    if (this.databasePromise) return this.databasePromise
    this.databasePromise = new Promise((resolve, reject) => {
      const request = this.indexedDb.open(DATABASE, 1)
      request.onupgradeneeded = () => request.result.createObjectStore(STORE, { keyPath: 'key' })
      request.onsuccess = () => {
        request.result.onversionchange = () => { request.result.close(); this.databasePromise = null }
        resolve(request.result)
      }
      request.onerror = request.onblocked = () => {
        this.databasePromise = null
        reject(request.error || new Error('Attachment draft storage is unavailable'))
      }
    })
    return this.databasePromise
  }
  async load(scope: AttachmentDraftScope): Promise<Attachment[]> {
    return (await this.loadSnapshot(scope)).attachments
  }
  async loadSnapshot(scope: AttachmentDraftScope): Promise<{ attachments: Attachment[]; revision?: string }> {
    const key = attachmentDraftKey(scope)
    const db = await this.database()
    const transaction = db.transaction(STORE, 'readwrite')
    const done = complete(transaction)
    const store = transaction.objectStore(STORE)
    let record: DraftRecord | undefined
    const request = store.get(key)
    request.onsuccess = () => {
      record = request.result
      if (record && (!Number.isFinite(record.expiresAt) || record.expiresAt <= this.now())) {
        store.delete(key)
        record = undefined
      }
      if (record && !record.revision) {
        record = { ...record, revision: createClientRequestId() }
        store.put(record)
      }
    }
    await done
    return record ? { attachments: restore(record, this.now()), revision: record.revision } : { attachments: [] }
  }
  async save(scope: AttachmentDraftScope, attachments: readonly Attachment[], revision?: string): Promise<void> {
    const key = attachmentDraftKey(scope)
    if (attachments.length > 10) throw new Error('Too many attachments to save as a draft')
    const stored = attachments.map(storedAttachment)
    const bytes = stored.reduce((sum, item) => sum + item.size, 0)
    if (!Number.isSafeInteger(bytes) || bytes < 0 || bytes > ATTACHMENT_DRAFT_MAX_BYTES) {
      throw new Error('Attachment draft exceeds its storage limit')
    }
    const now = this.now()
    const db = await this.database()
    const transaction = db.transaction(STORE, 'readwrite')
    const done = complete(transaction)
    const store = transaction.objectStore(STORE)
    if (!stored.length) {
      store.delete(key)
      await done
      return
    }
    // One read/write transaction makes the aggregate limit hold across tabs.
    const request = store.getAll()
    let quotaError = false
    request.onsuccess = () => {
      let total = bytes
      let count = 1
      for (const record of request.result as DraftRecord[]) {
        if (record.key === key) continue
        if (!Number.isFinite(record.expiresAt) || record.expiresAt <= now) { store.delete(record.key); continue }
        if (!Number.isSafeInteger(record.bytes) || record.bytes < 0) { store.delete(record.key); continue }
        total += record.bytes
        count += 1
      }
      if (total > ATTACHMENT_DRAFT_TOTAL_BYTES || count > MAX_DRAFTS) {
        quotaError = true
        transaction.abort()
        return
      }
      store.put({ key, version: 1, updatedAt: now, expiresAt: now + ATTACHMENT_DRAFT_TTL_MS,
        ...(revision ? { revision } : {}),
        bytes, attachments: stored } satisfies DraftRecord)
    }
    try { await done } catch (error) {
      if (quotaError) throw new Error('Attachment draft storage is full; clear older drafts to enable recovery')
      throw error
    }
  }
  /** A late acceptance may consume its exact draft version, never a newer tab's files. */
  async consume(scope: AttachmentDraftScope, revision: string, indexes: readonly number[]): Promise<boolean> {
    const key = attachmentDraftKey(scope)
    const db = await this.database()
    const transaction = db.transaction(STORE, 'readwrite')
    const done = complete(transaction)
    const store = transaction.objectStore(STORE)
    const request = store.get(key)
    let consumed = false
    request.onsuccess = () => {
      const record = request.result as DraftRecord | undefined
      if (!record || record.revision !== revision || !Array.isArray(record.attachments)) return
      consumed = true
      const accepted = new Set(indexes)
      const attachments = record.attachments.filter((_item, index) => !accepted.has(index))
      if (!attachments.length) store.delete(key)
      else store.put({ ...record, revision: undefined, attachments,
        bytes: attachments.reduce((sum, item) => sum + item.size, 0) })
    }
    await done
    return consumed
  }
}
export function createAttachmentDraftStore(): AttachmentDraftStore | null {
  try { return globalThis.indexedDB ? new IndexedDbAttachmentDraftStore(globalThis.indexedDB) : null } catch { return null }
}
