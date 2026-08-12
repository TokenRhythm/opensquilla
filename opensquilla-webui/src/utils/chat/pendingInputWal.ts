import type { Attachment } from '@/types/chat'

const DATABASE_NAME = 'opensquilla-chat-pending-inputs'
const DATABASE_VERSION = 1
const STORE_NAME = 'pending_chat_inputs'

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
  attachments: Attachment[]
  intent: string | null
  ownerRequestId?: string
  state: PendingInputWalState
  /** True once enqueue may have crossed the browser/Gateway boundary. */
  mayHaveServerCopy?: boolean
  requestFingerprint?: string
  serverRevision?: number
  createdAt: number
  updatedAt: number
}

export interface PendingInputWal {
  put: (record: PendingInputWalRecord) => Promise<void>
  list: (sessionKey: string) => Promise<PendingInputWalRecord[]>
  delete: (pendingInputId: string) => Promise<void>
  close: () => void
}

const WAL_STATES = new Set<PendingInputWalState>([
  'saving',
  'staged',
  'local_only',
  'retryable',
  'cancelling',
])

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
    && Array.isArray(record.attachments)
    && record.attachments.every(attachment => (
      attachment !== null && typeof attachment === 'object'
    ))
    && (record.intent === null || typeof record.intent === 'string')
    && typeof record.state === 'string'
    && WAL_STATES.has(record.state as PendingInputWalState)
    && (
      record.mayHaveServerCopy === undefined
      || typeof record.mayHaveServerCopy === 'boolean'
    )
    && typeof record.createdAt === 'number'
    && Number.isFinite(record.createdAt)
    && typeof record.updatedAt === 'number'
    && Number.isFinite(record.updatedAt)
}

function cloneRecord(record: PendingInputWalRecord): PendingInputWalRecord {
  return {
    ...record,
    attachments: record.attachments.map(attachment => ({ ...attachment })),
  }
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
        if (database.objectStoreNames.contains(STORE_NAME)) return
        const store = database.createObjectStore(STORE_NAME, {
          keyPath: 'pendingInputId',
        })
        store.createIndex('session_created', ['sessionKey', 'createdAt'], {
          unique: false,
        })
      }
      request.onsuccess = () => resolve(request.result)
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
        left.createdAt - right.createdAt
        || left.pendingInputId.localeCompare(right.pendingInputId)
      ))
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
