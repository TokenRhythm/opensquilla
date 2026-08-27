import type { Attachment } from '@/types/chat'
import type { ChatSendParams } from '@/types/rpc'

const DATABASE_NAME = 'opensquilla-chat-pending-inputs'
const DATABASE_VERSION = 2
const STORE_NAME = 'pending_chat_inputs'
const HANDOFF_STORE_NAME = 'response_handoffs'

export type PendingInputWalState =
  | 'saving'
  | 'staged'
  | 'local_only'
  | 'retryable'
  | 'cancelling'

export interface PendingInputWalRecord {
  schemaVersion: 1
  pendingInputId: string
  sessionKey: string
  clientRequestId: string
  clientMessageId: string
  text: string
  /** Annotation batch retained across IndexedDB/WAL queue recovery. */
  promptAnnotationIds?: string[]
  attachments: Attachment[]
  intent: string | null
  confirmedPlainText?: boolean
  ownerRequestId?: string
  state: PendingInputWalState
  /** True once enqueue may have crossed the browser/Gateway boundary. */
  mayHaveServerCopy?: boolean
  /** Complete an in-flight tombstone by preserving the text as a local draft. */
  retainAfterCancel?: boolean
  requestFingerprint?: string
  serverRevision?: number
  position?: number
  walRevision?: number
  createdAt: number
  updatedAt: number
}

export type ResponseHandoffWalState = 'preparing' | 'submitting' | 'accepted' | 'failed'

export interface ResponseHandoffWalRecord {
  schemaVersion: 1
  ownerRequestId: string
  requestSessionKey: string
  clientRequestId: string
  clientMessageId: string
  params: ChatSendParams
  composerText: string
  recoveryAttachments: Attachment[]
  /** A protocol-owned replay must never be restored into the user composer. */
  restoreComposerOnFailure?: boolean
  /** Stable source-session + barrier identity used for cross-tab coordination. */
  replayCoordinationKey?: string
  /** Identifies the live dispatcher allowed to arm an unsubmitted handoff. */
  walOwnerId?: string
  /** Monotonic compare-and-swap revision for handoff state transitions. */
  walRevision?: number
  state: ResponseHandoffWalState
  acceptedSessionKey?: string
  errorCode?: string
  createdAt: number
  updatedAt: number
}

export interface PendingInputOrderCommit {
  records: PendingInputWalRecord[]
}

export interface AcceptedHandoffCommit {
  handoff: ResponseHandoffWalRecord
  records: PendingInputWalRecord[]
}

export interface ResponseHandoffWalMutation {
  applied: boolean
  record: ResponseHandoffWalRecord | null
}

export interface PendingInputWal {
  put: (record: PendingInputWalRecord) => Promise<void>
  list: (sessionKey: string) => Promise<PendingInputWalRecord[]>
  delete: (pendingInputId: string) => Promise<void>
  putMany?: (records: PendingInputWalRecord[]) => Promise<void>
  commitOrder?: (
    sessionKey: string,
    orderedIds: string[],
    expectedWalRevisions: Record<string, number>,
  ) => Promise<PendingInputOrderCommit>
  putHandoff?: (record: ResponseHandoffWalRecord) => Promise<void>
  /** Atomically create a handoff without replacing another dispatcher's record. */
  prepareHandoff?: (
    record: ResponseHandoffWalRecord,
  ) => Promise<ResponseHandoffWalMutation>
  /** Atomically replace/delete a handoff only while its owner and revision match. */
  compareAndSwapHandoff?: (
    ownerRequestId: string,
    expectedWalOwnerId: string,
    expectedWalRevision: number,
    record: ResponseHandoffWalRecord | null,
  ) => Promise<ResponseHandoffWalMutation>
  listHandoffs?: (requestSessionKey?: string) => Promise<ResponseHandoffWalRecord[]>
  acceptHandoff?: (
    ownerRequestId: string,
    acceptedSessionKey: string,
  ) => Promise<AcceptedHandoffCommit>
  deleteHandoff?: (ownerRequestId: string) => Promise<void>
  close: () => void
}

const WAL_STATES = new Set<PendingInputWalState>([
  'saving',
  'staged',
  'local_only',
  'retryable',
  'cancelling',
])

function validPromptAnnotationIds(value: unknown): boolean {
  if (value === undefined) return true
  if (!Array.isArray(value) || value.length > 16) return false
  return value.every((item, index) => (
    typeof item === 'string'
    && item.trim().length > 0
    && value.indexOf(item) === index
  ))
}

function isPendingInputWalRecord(value: unknown): value is PendingInputWalRecord {
  if (!value || typeof value !== 'object') return false
  const record = value as Partial<PendingInputWalRecord>
  return record.schemaVersion === 1
    && typeof record.pendingInputId === 'string'
    && record.pendingInputId.length > 0
    && typeof record.sessionKey === 'string'
    && record.sessionKey.length > 0
    && typeof record.clientRequestId === 'string'
    && record.clientRequestId.length > 0
    && typeof record.clientMessageId === 'string'
    && record.clientMessageId.length > 0
    && typeof record.text === 'string'
    && validPromptAnnotationIds(record.promptAnnotationIds)
    && Array.isArray(record.attachments)
    && record.attachments.every(attachment => (
      attachment !== null && typeof attachment === 'object'
    ))
    && (record.intent === null || typeof record.intent === 'string')
    && (
      record.confirmedPlainText === undefined
      || typeof record.confirmedPlainText === 'boolean'
    )
    && typeof record.state === 'string'
    && WAL_STATES.has(record.state as PendingInputWalState)
    && (
      record.mayHaveServerCopy === undefined
      || typeof record.mayHaveServerCopy === 'boolean'
    )
    && (
      record.retainAfterCancel === undefined
      || typeof record.retainAfterCancel === 'boolean'
    )
    && (
      record.position === undefined
      || (Number.isSafeInteger(record.position) && record.position >= 0)
    )
    && (
      record.walRevision === undefined
      || (Number.isSafeInteger(record.walRevision) && record.walRevision >= 1)
    )
    && typeof record.createdAt === 'number'
    && Number.isFinite(record.createdAt)
    && typeof record.updatedAt === 'number'
    && Number.isFinite(record.updatedAt)
}

function isResponseHandoffWalRecord(value: unknown): value is ResponseHandoffWalRecord {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const record = value as Partial<ResponseHandoffWalRecord>
  const params = record.params as Partial<ChatSendParams> | undefined
  return record.schemaVersion === 1
    && typeof record.ownerRequestId === 'string'
    && record.ownerRequestId.length > 0
    && typeof record.requestSessionKey === 'string'
    && record.requestSessionKey.length > 0
    && typeof record.clientRequestId === 'string'
    && record.clientRequestId === record.ownerRequestId
    && typeof record.clientMessageId === 'string'
    && record.clientMessageId.length > 0
    && Boolean(params && typeof params === 'object')
    && params?.clientRequestId === record.clientRequestId
    && params?.clientMessageId === record.clientMessageId
    && params?.sessionKey === record.requestSessionKey
    && typeof record.composerText === 'string'
    && Array.isArray(record.recoveryAttachments)
    && record.recoveryAttachments.every(attachment => (
      attachment !== null && typeof attachment === 'object'
    ))
    && (
      record.restoreComposerOnFailure === undefined
      || typeof record.restoreComposerOnFailure === 'boolean'
    )
    && (
      record.replayCoordinationKey === undefined
      || (
        typeof record.replayCoordinationKey === 'string'
        && record.replayCoordinationKey.length > 0
      )
    )
    && (
      record.walOwnerId === undefined
      || (typeof record.walOwnerId === 'string' && record.walOwnerId.length > 0)
    )
    && (
      record.walRevision === undefined
      || (Number.isSafeInteger(record.walRevision) && record.walRevision >= 1)
    )
    && ['preparing', 'submitting', 'accepted', 'failed'].includes(String(record.state || ''))
    && (
      record.state !== 'preparing'
      || (
        typeof record.walOwnerId === 'string'
        && record.walOwnerId.length > 0
        && Number.isSafeInteger(record.walRevision)
        && record.walRevision! >= 1
      )
    )
    && typeof record.createdAt === 'number'
    && Number.isFinite(record.createdAt)
    && typeof record.updatedAt === 'number'
    && Number.isFinite(record.updatedAt)
}

function cloneRecord(record: PendingInputWalRecord): PendingInputWalRecord {
  return {
    ...record,
    ...(record.promptAnnotationIds
      ? { promptAnnotationIds: [...record.promptAnnotationIds] }
      : {}),
    attachments: record.attachments.map(attachment => ({ ...attachment })),
  }
}

function cloneHandoffRecord(record: ResponseHandoffWalRecord): ResponseHandoffWalRecord {
  return structuredClone(record)
}

function requestResult<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    request.onsuccess = () => resolve(request.result)
    request.onerror = () => reject(request.error || new Error('IndexedDB request failed'))
  })
}

function transactionDone(transaction: IDBTransaction): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    transaction.oncomplete = () => resolve()
    transaction.onabort = () => reject(
      transaction.error || new Error('IndexedDB transaction was aborted'),
    )
    transaction.onerror = () => reject(
      transaction.error || new Error('IndexedDB transaction failed'),
    )
  })
}

class BrowserPendingInputWal implements PendingInputWal {
  private databasePromise: Promise<IDBDatabase> | null = null

  constructor(private readonly indexedDb: IDBFactory) {}

  private database(): Promise<IDBDatabase> {
    if (this.databasePromise) return this.databasePromise
    this.databasePromise = new Promise<IDBDatabase>((resolve, reject) => {
      const request = this.indexedDb.open(DATABASE_NAME, DATABASE_VERSION)
      request.onupgradeneeded = () => {
        const database = request.result
        if (!database.objectStoreNames.contains(STORE_NAME)) {
          const store = database.createObjectStore(STORE_NAME, {
            keyPath: 'pendingInputId',
          })
          store.createIndex('session_created', ['sessionKey', 'createdAt'], {
            unique: false,
          })
        }
        if (!database.objectStoreNames.contains(HANDOFF_STORE_NAME)) {
          database.createObjectStore(HANDOFF_STORE_NAME, {
            keyPath: 'ownerRequestId',
          })
        }
      }
      request.onsuccess = () => {
        request.result.onversionchange = () => request.result.close()
        resolve(request.result)
      }
      request.onerror = () => {
        this.databasePromise = null
        reject(request.error || new Error('Unable to open pending-input WAL'))
      }
      request.onblocked = () => {
        this.databasePromise = null
        reject(new Error('Pending-input WAL upgrade is blocked by another tab'))
      }
    })
    return this.databasePromise
  }

  async put(record: PendingInputWalRecord): Promise<void> {
    const database = await this.database()
    const transaction = database.transaction(STORE_NAME, 'readwrite')
    transaction.objectStore(STORE_NAME).put(cloneRecord(record))
    await transactionDone(transaction)
  }

  async putMany(records: PendingInputWalRecord[]): Promise<void> {
    const database = await this.database()
    const transaction = database.transaction(STORE_NAME, 'readwrite')
    const store = transaction.objectStore(STORE_NAME)
    for (const record of records) store.put(cloneRecord(record))
    await transactionDone(transaction)
  }

  async list(sessionKey: string): Promise<PendingInputWalRecord[]> {
    const database = await this.database()
    const transaction = database.transaction(STORE_NAME, 'readonly')
    const index = transaction.objectStore(STORE_NAME).index('session_created')
    const range = IDBKeyRange.bound(
      [sessionKey, Number.MIN_SAFE_INTEGER],
      [sessionKey, Number.MAX_SAFE_INTEGER],
    )
    const records = await requestResult(index.getAll(range))
    await transactionDone(transaction)
    return (records as unknown[])
      .filter(isPendingInputWalRecord)
      .map(cloneRecord)
      .sort((left, right) => (
        (left.position ?? Number.MAX_SAFE_INTEGER)
        - (right.position ?? Number.MAX_SAFE_INTEGER)
        || left.createdAt - right.createdAt
        || left.pendingInputId.localeCompare(right.pendingInputId)
      ))
  }

  async commitOrder(
    sessionKey: string,
    orderedIds: string[],
    expectedWalRevisions: Record<string, number>,
  ): Promise<PendingInputOrderCommit> {
    const database = await this.database()
    const transaction = database.transaction(STORE_NAME, 'readwrite')
    const store = transaction.objectStore(STORE_NAME)
    const index = store.index('session_created')
    const range = IDBKeyRange.bound(
      [sessionKey, Number.MIN_SAFE_INTEGER],
      [sessionKey, Number.MAX_SAFE_INTEGER],
    )
    const raw = await requestResult(index.getAll(range))
    const records = (raw as unknown[]).filter(isPendingInputWalRecord)
    const byId = new Map(records.map(record => [record.pendingInputId, record]))
    if (
      orderedIds.length !== records.length
      || new Set(orderedIds).size !== orderedIds.length
      || orderedIds.some(id => !byId.has(id))
    ) {
      transaction.abort()
      throw new Error('Pending queue changed before local reorder')
    }
    const committed = orderedIds.map((pendingInputId, position) => {
      const record = byId.get(pendingInputId)!
      const currentRevision = record.walRevision ?? 1
      if (expectedWalRevisions[pendingInputId] !== currentRevision) {
        transaction.abort()
        throw new Error('Pending queue changed before local reorder')
      }
      const next = cloneRecord({
        ...record,
        position,
        walRevision: currentRevision + 1,
        updatedAt: Date.now(),
      })
      store.put(next)
      return next
    })
    await transactionDone(transaction)
    return { records: committed }
  }

  async putHandoff(record: ResponseHandoffWalRecord): Promise<void> {
    const database = await this.database()
    const transaction = database.transaction(HANDOFF_STORE_NAME, 'readwrite')
    transaction.objectStore(HANDOFF_STORE_NAME).put(cloneHandoffRecord(record))
    await transactionDone(transaction)
  }

  async prepareHandoff(
    record: ResponseHandoffWalRecord,
  ): Promise<ResponseHandoffWalMutation> {
    const database = await this.database()
    const transaction = database.transaction(HANDOFF_STORE_NAME, 'readwrite')
    const store = transaction.objectStore(HANDOFF_STORE_NAME)
    const current = await requestResult(store.get(record.ownerRequestId))
    if (isResponseHandoffWalRecord(current)) {
      await transactionDone(transaction)
      return { applied: false, record: cloneHandoffRecord(current) }
    }
    const prepared = cloneHandoffRecord(record)
    store.put(prepared)
    await transactionDone(transaction)
    return { applied: true, record: prepared }
  }

  async compareAndSwapHandoff(
    ownerRequestId: string,
    expectedWalOwnerId: string,
    expectedWalRevision: number,
    record: ResponseHandoffWalRecord | null,
  ): Promise<ResponseHandoffWalMutation> {
    const database = await this.database()
    const transaction = database.transaction(HANDOFF_STORE_NAME, 'readwrite')
    const store = transaction.objectStore(HANDOFF_STORE_NAME)
    const current = await requestResult(store.get(ownerRequestId))
    if (
      !isResponseHandoffWalRecord(current)
      || current.walOwnerId !== expectedWalOwnerId
      || current.walRevision !== expectedWalRevision
    ) {
      await transactionDone(transaction)
      return {
        applied: false,
        record: isResponseHandoffWalRecord(current) ? cloneHandoffRecord(current) : null,
      }
    }
    if (!record) {
      store.delete(ownerRequestId)
      await transactionDone(transaction)
      return { applied: true, record: null }
    }
    if (
      record.ownerRequestId !== ownerRequestId
      || record.walOwnerId !== expectedWalOwnerId
      || record.walRevision !== expectedWalRevision + 1
    ) {
      transaction.abort()
      throw new Error('Invalid response handoff compare-and-swap transition')
    }
    const next = cloneHandoffRecord(record)
    store.put(next)
    await transactionDone(transaction)
    return { applied: true, record: next }
  }

  async listHandoffs(requestSessionKey?: string): Promise<ResponseHandoffWalRecord[]> {
    const database = await this.database()
    const transaction = database.transaction(HANDOFF_STORE_NAME, 'readonly')
    const raw = await requestResult(transaction.objectStore(HANDOFF_STORE_NAME).getAll())
    await transactionDone(transaction)
    return (raw as unknown[])
      .filter(isResponseHandoffWalRecord)
      .filter(record => !requestSessionKey || record.requestSessionKey === requestSessionKey)
      .map(cloneHandoffRecord)
      .sort((left, right) => left.createdAt - right.createdAt)
  }

  async acceptHandoff(
    ownerRequestId: string,
    acceptedSessionKey: string,
  ): Promise<AcceptedHandoffCommit> {
    const database = await this.database()
    const transaction = database.transaction(
      [STORE_NAME, HANDOFF_STORE_NAME],
      'readwrite',
    )
    const handoffStore = transaction.objectStore(HANDOFF_STORE_NAME)
    const pendingStore = transaction.objectStore(STORE_NAME)
    const rawHandoff = await requestResult(handoffStore.get(ownerRequestId))
    if (!isResponseHandoffWalRecord(rawHandoff)) {
      transaction.abort()
      throw new Error('Response handoff no longer exists')
    }
    if (rawHandoff.walOwnerId && rawHandoff.state !== 'accepted') {
      transaction.abort()
      throw new Error('Response handoff is not durably accepted')
    }
    const handoff = cloneHandoffRecord({
      ...rawHandoff,
      state: 'accepted',
      acceptedSessionKey,
      updatedAt: Date.now(),
    })
    handoffStore.put(handoff)
    const rawPending = await requestResult(pendingStore.getAll())
    const records = (rawPending as unknown[])
      .filter(isPendingInputWalRecord)
      .filter(record => record.ownerRequestId === ownerRequestId)
      .map(record => {
        const next = cloneRecord({
          ...record,
          sessionKey: acceptedSessionKey,
          ownerRequestId: undefined,
          state: 'saving',
          walRevision: (record.walRevision ?? 1) + 1,
          updatedAt: Date.now(),
        })
        pendingStore.put(next)
        return next
      })
    await transactionDone(transaction)
    return { handoff, records }
  }

  async deleteHandoff(ownerRequestId: string): Promise<void> {
    const database = await this.database()
    const transaction = database.transaction(HANDOFF_STORE_NAME, 'readwrite')
    transaction.objectStore(HANDOFF_STORE_NAME).delete(ownerRequestId)
    await transactionDone(transaction)
  }

  async delete(pendingInputId: string): Promise<void> {
    const database = await this.database()
    const transaction = database.transaction(STORE_NAME, 'readwrite')
    transaction.objectStore(STORE_NAME).delete(pendingInputId)
    await transactionDone(transaction)
  }

  close(): void {
    if (!this.databasePromise) return
    void this.databasePromise.then(database => database.close(), () => {})
    this.databasePromise = null
  }
}

/** Return a durable browser WAL, or null when IndexedDB is unavailable. */
export function createPendingInputWal(
  indexedDb?: IDBFactory,
): PendingInputWal | null {
  let candidate = indexedDb
  if (arguments.length === 0) {
    try {
      candidate = globalThis.indexedDB
    } catch {
      // Privacy modes and hardened embedders may expose a throwing accessor.
      // Queue admission must fail closed before the composer is cleared.
      return null
    }
  }
  return candidate ? new BrowserPendingInputWal(candidate) : null
}
