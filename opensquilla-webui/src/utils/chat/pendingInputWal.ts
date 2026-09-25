import { copySelectedSkills, isSelectedSkills } from '@/types/selectedSkills'
import type { SelectedSkillRef } from '@/types/selectedSkills'
import { normalizePageContext, type ChatPageContext } from '@/types/pageContext'
import type { Attachment } from '@/types/chat'
import { snapshotAttachment } from './attachments'
import type {
  TurnCancelRequest, TurnReceiptRequest, TurnSendParams, TurnSendResponse, TurnSteerResponse,
} from '@/modules/turnCommands'

const DATABASE_NAME = 'opensquilla-chat-pending-inputs'
// Older clients opening v2 must fail with VersionError, never replay v3 data
// without the identity fence or delete the database to repair that error.
const DATABASE_VERSION = 3
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
  draftIds?: string[]
  /** Read-only upgrade input; never sent to Gateway. */
  promptAnnotationIds?: string[]
  retiredAnnotationInput?: boolean
  selectedSkills?: SelectedSkillRef[]
  pageContext?: ChatPageContext
  attachments: Attachment[]
  intent: string | null
  confirmedPlainText?: boolean
  ownerRequestId?: string
  state: PendingInputWalState
  /** True once enqueue may have crossed the browser/Gateway boundary. */
  mayHaveServerCopy?: boolean
  /** Credential-free Gateway/subject fingerprint for a never-sent offline draft. */
  deliveryIdentity?: string
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
  params: TurnSendParams
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

/** One authority for a delivery and its optional session handoff projection. */
export interface DeliveryWalRecord {
  schemaVersion: 2
  ownerRequestId: string
  deliveryIdentity: string
  requestSessionKey: string
  request?: TurnReceiptRequest
  phase: 'prepared' | 'submitting' | 'unknown' | 'accepted' | 'not-sent'
  response?: TurnSendResponse | TurnSteerResponse
  stop?: { requested: true; request?: TurnCancelRequest; completed?: boolean }
  paused?: 'authority' | 'conflict'
  handoff?: ResponseHandoffWalRecord
  revision: number
  lease?: { owner: string; epoch: number; expiresAt: number }
  createdAt: number
  updatedAt: number
}

export interface DeliveryWalMutation {
  applied: boolean
  record: DeliveryWalRecord | null
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
    shouldAccept?: () => boolean,
    handoffSignal?: AbortSignal,
  ) => Promise<AcceptedHandoffCommit | null>
  deleteHandoff?: (ownerRequestId: string) => Promise<void>
  listDeliveries?: () => Promise<DeliveryWalRecord[]>
  listRecoveryDeliveries?: (after?: string, limit?: number) => Promise<{ records: DeliveryWalRecord[]; next?: string }>
  findDeliveryByTask?: (identity: string, sessionKey: string, taskId: string) => Promise<DeliveryWalRecord | null>
  findSteerDeliveries?: (identity: string, sessionKey: string, expectedTurnId: string) => Promise<DeliveryWalRecord[]>
  onInvalidated?: (listener: () => void) => () => void
  countQuarantinedDeliveries?: () => Promise<number>
  getDelivery?: (ownerRequestId: string) => Promise<DeliveryWalRecord | null>
  /** Create only, or attach to the exact handoff prepared by this live caller. */
  prepareDelivery?: (
    record: DeliveryWalRecord,
    handoffOwner?: { owner: string; revision: number },
  ) => Promise<DeliveryWalMutation>
  /** An IndexedDB read/write transaction is the cross-tab CAS authority. */
  compareAndSwapDelivery?: (
    ownerRequestId: string,
    expectedRevision: number,
    record: DeliveryWalRecord | null,
  ) => Promise<DeliveryWalMutation>
  close: () => void
}

function isDeliveryWalRecord(value: unknown): value is DeliveryWalRecord {
  if (!value || typeof value !== 'object') return false
  const record = value as DeliveryWalRecord
  return record.schemaVersion === 2
    && typeof record.ownerRequestId === 'string' && record.ownerRequestId.length > 0
    && typeof record.deliveryIdentity === 'string' && record.deliveryIdentity.length > 0
    && typeof record.requestSessionKey === 'string' && record.requestSessionKey.length > 0
    && ['prepared', 'submitting', 'unknown', 'accepted', 'not-sent'].includes(record.phase)
    && Number.isSafeInteger(record.revision) && record.revision >= 1
    && Number.isFinite(record.createdAt) && Number.isFinite(record.updatedAt)
    && (record.request?.kind === 'send' || record.request?.kind === 'steer' || !!record.stop?.request?.taskId)
}

function handoffFromStored(value: unknown): ResponseHandoffWalRecord | null {
  const candidate = isDeliveryWalRecord(value) ? value.handoff : value
  return isResponseHandoffWalRecord(candidate) ? candidate : null
}

function withHandoff(value: unknown, handoff: ResponseHandoffWalRecord | null): unknown {
  return isDeliveryWalRecord(value)
    ? indexedDelivery({ ...value, handoff: handoff || undefined, revision: value.revision + 1, updatedAt: Date.now() })
    : handoff
}

function indexedDelivery(record: DeliveryWalRecord) {
  const response = record.response
  const promotedTask = response && 'disposition' in response && response.disposition === 'promoted' ? response.promotedTurnId : undefined
  const task = promotedTask || record.stop?.request?.taskId || response?.taskId
    || (response && 'promotedTurnId' in response ? response.promotedTurnId : undefined)
    || (response && 'turnId' in response ? response.turnId : undefined)
  const pending = ['prepared', 'unknown', 'submitting'].includes(record.phase) || !!(record.stop && !record.stop.completed)
  return { ...record, recoveryState: pending ? 'pending' : 'settled',
    ...(record.request?.kind === 'steer' ? { steerScope: [record.deliveryIdentity, record.request.request.key, record.request.request.expectedTurnId] } : {}),
    ...(task ? { taskScope: [record.deliveryIdentity, record.stop?.request?.sessionKey
      || response?.sessionKey || response?.key || record.requestSessionKey, task] } : {}),
  }
}

const WAL_STATES = new Set<PendingInputWalState>([
  'saving',
  'staged',
  'local_only',
  'retryable',
  'cancelling',
])

function validAnnotationDraftIds(value: unknown): boolean {
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
    && (record.deliveryIdentity === undefined || (
      typeof record.deliveryIdentity === 'string'
      && record.deliveryIdentity.length > 0
    ))
    && validAnnotationDraftIds(record.draftIds)
    && (record.selectedSkills === undefined || isSelectedSkills(record.selectedSkills))
    && (record.pageContext === undefined || normalizePageContext(record.pageContext) !== null)
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
  const params = record.params as Partial<TurnSendParams> | undefined
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
    && (params?.selectedSkills === undefined || isSelectedSkills(params.selectedSkills))
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
  const { promptAnnotationIds, ...current } = record
  return {
    ...current,
    ...(promptAnnotationIds?.length ? { retiredAnnotationInput: true } : {}),
    ...(record.pageContext ? { pageContext: normalizePageContext(record.pageContext)! } : {}),
    ...(record.selectedSkills ? { selectedSkills: copySelectedSkills(record.selectedSkills) } : {}),
    ...(record.draftIds
      ? { draftIds: [...record.draftIds] }
      : {}),
    attachments: record.attachments.map(snapshotAttachment),
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
  private openEpoch = 0
  private invalidated = false
  private invalidationListeners = new Set<() => void>()

  constructor(private readonly indexedDb: IDBFactory) {}

  private database(): Promise<IDBDatabase> {
    if (this.invalidated) return Promise.reject(new Error('Pending-input WAL version changed; reload this client'))
    if (this.databasePromise) return this.databasePromise
    const epoch = ++this.openEpoch
    this.databasePromise = new Promise<IDBDatabase>((resolve, reject) => {
      let abandoned = false
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
        const handoffs = !database.objectStoreNames.contains(HANDOFF_STORE_NAME)
          ? database.createObjectStore(HANDOFF_STORE_NAME, {
            keyPath: 'ownerRequestId',
          })
          : request.transaction!.objectStore(HANDOFF_STORE_NAME)
        if (!handoffs.indexNames?.contains('recovery_state')) handoffs.createIndex('recovery_state', 'recoveryState', { unique: false })
        if (!handoffs.indexNames?.contains('task_scope')) handoffs.createIndex('task_scope', 'taskScope', { unique: false })
        if (!handoffs.indexNames?.contains('handoff_session')) handoffs.createIndex('handoff_session', 'handoff.requestSessionKey', { unique: false })
        if (!handoffs.indexNames?.contains('steer_scope')) handoffs.createIndex('steer_scope', 'steerScope', { unique: false })
      }
      request.onsuccess = () => {
        if (abandoned || epoch !== this.openEpoch) {
          request.result.close()
          reject(new Error('Pending-input WAL open was superseded'))
          return
        }
        request.result.onversionchange = () => {
          this.invalidated = true
          request.result.close()
          if (epoch === this.openEpoch) this.databasePromise = null
          for (const listener of this.invalidationListeners) listener()
        }
        resolve(request.result)
      }
      request.onerror = () => {
        abandoned = true
        if (epoch === this.openEpoch) this.databasePromise = null
        reject(request.error || new Error('Unable to open pending-input WAL'))
      }
      request.onblocked = () => {
        abandoned = true
        if (epoch === this.openEpoch) this.databasePromise = null
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
    const store = transaction.objectStore(HANDOFF_STORE_NAME)
    const current = await requestResult(store.get(record.ownerRequestId))
    if (current !== undefined && !handoffFromStored(current) && !isDeliveryWalRecord(current)) {
      throw new Error('Unrecognized delivery record is quarantined')
    }
    store.put(withHandoff(current, cloneHandoffRecord(record)))
    await transactionDone(transaction)
  }

  async prepareHandoff(
    record: ResponseHandoffWalRecord,
  ): Promise<ResponseHandoffWalMutation> {
    const database = await this.database()
    const transaction = database.transaction(HANDOFF_STORE_NAME, 'readwrite')
    const store = transaction.objectStore(HANDOFF_STORE_NAME)
    const stored = await requestResult(store.get(record.ownerRequestId))
    const current = handoffFromStored(stored)
    if (stored !== undefined) {
      await transactionDone(transaction)
      return { applied: false, record: current ? cloneHandoffRecord(current) : null }
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
    const stored = await requestResult(store.get(ownerRequestId))
    const current = handoffFromStored(stored)
    if (
      !current
      || current.walOwnerId !== expectedWalOwnerId
      || current.walRevision !== expectedWalRevision
    ) {
      await transactionDone(transaction)
      return {
        applied: false,
        record: current ? cloneHandoffRecord(current) : null,
      }
    }
    if (!record) {
      if (isDeliveryWalRecord(stored)) store.put(withHandoff(stored, null))
      else store.delete(ownerRequestId)
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
    store.put(withHandoff(stored, next))
    await transactionDone(transaction)
    return { applied: true, record: next }
  }

  async listHandoffs(requestSessionKey?: string): Promise<ResponseHandoffWalRecord[]> {
    const database = await this.database()
    const transaction = database.transaction(HANDOFF_STORE_NAME, 'readonly')
    const store = transaction.objectStore(HANDOFF_STORE_NAME)
    const raw = await requestResult(requestSessionKey
      ? store.index('handoff_session').getAll(requestSessionKey)
      : store.getAll())
    await transactionDone(transaction)
    return (raw as unknown[])
      .map(handoffFromStored)
      .filter((record): record is ResponseHandoffWalRecord => record !== null)
      .filter(record => !requestSessionKey || record.requestSessionKey === requestSessionKey)
      .map(cloneHandoffRecord)
      .sort((left, right) => left.createdAt - right.createdAt)
  }

  async acceptHandoff(
    ownerRequestId: string,
    acceptedSessionKey: string,
    shouldAccept: () => boolean = () => true,
    handoffSignal?: AbortSignal,
  ): Promise<AcceptedHandoffCommit | null> {
    if (!shouldAccept() || handoffSignal?.aborted) return null
    const database = await this.database()
    if (!shouldAccept() || handoffSignal?.aborted) return null
    const transaction = database.transaction(
      [STORE_NAME, HANDOFF_STORE_NAME],
      'readwrite',
    )
    let abortedByHandoff = false
    const abortTransaction = () => {
      abortedByHandoff = true
      try {
        transaction.abort()
      } catch {
        // oncomplete may already have won. Its promise continuation runs
        // before a later navigation task can invalidate this epoch.
      }
    }
    if (handoffSignal?.aborted) abortTransaction()
    else handoffSignal?.addEventListener('abort', abortTransaction, { once: true })
    const handoffStore = transaction.objectStore(HANDOFF_STORE_NAME)
    const pendingStore = transaction.objectStore(STORE_NAME)
    try {
      const stored = await requestResult(handoffStore.get(ownerRequestId))
      const rawHandoff = handoffFromStored(stored)
      if (!rawHandoff) {
        transaction.abort()
        throw new Error('Response handoff no longer exists')
      }
      if (rawHandoff.walOwnerId && rawHandoff.state !== 'accepted') {
        transaction.abort()
        throw new Error('Response handoff is not durably accepted')
      }
      const rawPending = await requestResult(pendingStore.getAll())
      if (!shouldAccept() || handoffSignal?.aborted) {
        abortTransaction()
        return null
      }
      const handoff = cloneHandoffRecord({
        ...rawHandoff,
        state: 'accepted',
        acceptedSessionKey,
        updatedAt: Date.now(),
      })
      handoffStore.put(withHandoff(stored, handoff))
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
    } catch (error) {
      if (abortedByHandoff || handoffSignal?.aborted || !shouldAccept()) return null
      throw error
    } finally {
      handoffSignal?.removeEventListener('abort', abortTransaction)
    }
  }

  async deleteHandoff(ownerRequestId: string): Promise<void> {
    const database = await this.database()
    const transaction = database.transaction(HANDOFF_STORE_NAME, 'readwrite')
    const store = transaction.objectStore(HANDOFF_STORE_NAME)
    const stored = await requestResult(store.get(ownerRequestId))
    if (isDeliveryWalRecord(stored)) store.put(withHandoff(stored, null))
    else if (isResponseHandoffWalRecord(stored)) store.delete(ownerRequestId)
    await transactionDone(transaction)
  }

  async listDeliveries(): Promise<DeliveryWalRecord[]> {
    const database = await this.database()
    const transaction = database.transaction(HANDOFF_STORE_NAME, 'readonly')
    const raw = await requestResult(transaction.objectStore(HANDOFF_STORE_NAME).getAll())
    await transactionDone(transaction)
    return (raw as unknown[]).filter(isDeliveryWalRecord).map(record => structuredClone(record))
  }

  async listRecoveryDeliveries(after?: string, limit = 16): Promise<{ records: DeliveryWalRecord[]; next?: string }> {
    const database = await this.database()
    const transaction = database.transaction(HANDOFF_STORE_NAME, 'readonly')
    const records: DeliveryWalRecord[] = []
    let next: string | undefined
    const request = transaction.objectStore(HANDOFF_STORE_NAME).index('recovery_state').openCursor(IDBKeyRange.only('pending'))
    await new Promise<void>((resolve, reject) => {
      request.onerror = () => reject(request.error || new Error('Delivery recovery cursor failed'))
      request.onsuccess = () => {
        const cursor = request.result
        if (!cursor) { resolve(); return }
        if (after) {
          const position = this.indexedDb.cmp(cursor.primaryKey, after)
          // Every page opens the same index range. Seek to its primary-key
          // boundary instead of reading the entire pending prefix again.
          if (position < 0) { cursor.continuePrimaryKey('pending', after); return }
          if (position === 0) { cursor.continue(); return }
        }
        if (records.length >= Math.max(1, Math.min(limit, 64))) {
          next = records[records.length - 1]?.ownerRequestId
          resolve()
          return
        }
        if (isDeliveryWalRecord(cursor.value)) records.push(structuredClone(cursor.value))
        cursor.continue()
      }
    })
    await transactionDone(transaction)
    return { records, ...(next ? { next } : {}) }
  }

  async findDeliveryByTask(identity: string, sessionKey: string, taskId: string): Promise<DeliveryWalRecord | null> {
    const database = await this.database()
    const transaction = database.transaction(HANDOFF_STORE_NAME, 'readonly')
    const raw = await requestResult(transaction.objectStore(HANDOFF_STORE_NAME).index('task_scope').get([identity, sessionKey, taskId]))
    await transactionDone(transaction)
    return isDeliveryWalRecord(raw) ? structuredClone(raw) : null
  }

  async countQuarantinedDeliveries(): Promise<number> {
    const database = await this.database()
    const transaction = database.transaction(HANDOFF_STORE_NAME, 'readonly')
    const store = transaction.objectStore(HANDOFF_STORE_NAME)
    // Count native index entries; legacy attachment payloads are never loaded.
    const [total, indexed] = await Promise.all([requestResult(store.count()), requestResult(store.index('recovery_state').count())])
    await transactionDone(transaction)
    return total - indexed
  }

  async findSteerDeliveries(identity: string, sessionKey: string, expectedTurnId: string): Promise<DeliveryWalRecord[]> {
    const database = await this.database()
    const transaction = database.transaction(HANDOFF_STORE_NAME, 'readonly')
    const raw = await requestResult(transaction.objectStore(HANDOFF_STORE_NAME).index('steer_scope').getAll([identity, sessionKey, expectedTurnId]))
    await transactionDone(transaction)
    return (raw as unknown[]).filter(isDeliveryWalRecord).map(record => structuredClone(record))
  }

  onInvalidated(listener: () => void): () => void {
    this.invalidationListeners.add(listener)
    return () => { this.invalidationListeners.delete(listener) }
  }

  async getDelivery(ownerRequestId: string): Promise<DeliveryWalRecord | null> {
    const database = await this.database()
    const transaction = database.transaction(HANDOFF_STORE_NAME, 'readonly')
    const raw = await requestResult(transaction.objectStore(HANDOFF_STORE_NAME).get(ownerRequestId))
    await transactionDone(transaction)
    return isDeliveryWalRecord(raw) ? structuredClone(raw) : null
  }

  async prepareDelivery(
    record: DeliveryWalRecord,
    handoffOwner?: { owner: string; revision: number },
  ): Promise<DeliveryWalMutation> {
    if (!isDeliveryWalRecord(record)) throw new Error('Invalid delivery record')
    const database = await this.database()
    const transaction = database.transaction(HANDOFF_STORE_NAME, 'readwrite')
    const store = transaction.objectStore(HANDOFF_STORE_NAME)
    const raw = await requestResult(store.get(record.ownerRequestId))
    const handoff = handoffFromStored(raw)
    const canAdopt = !isDeliveryWalRecord(raw) && handoff && handoffOwner
      && handoff.walOwnerId === handoffOwner.owner
      && handoff.walRevision === handoffOwner.revision
      && handoff.requestSessionKey === record.requestSessionKey
      && handoff.state === 'submitting'
    if (raw !== undefined && !canAdopt) {
      await transactionDone(transaction)
      return { applied: false, record: isDeliveryWalRecord(raw) ? structuredClone(raw) : null }
    }
    const next = structuredClone({ ...record, ...(canAdopt ? { handoff } : {}) })
    store.put(indexedDelivery(next))
    await transactionDone(transaction)
    return { applied: true, record: next }
  }

  async compareAndSwapDelivery(
    ownerRequestId: string,
    expectedRevision: number,
    record: DeliveryWalRecord | null,
  ): Promise<DeliveryWalMutation> {
    const database = await this.database()
    const transaction = database.transaction(HANDOFF_STORE_NAME, 'readwrite')
    const store = transaction.objectStore(HANDOFF_STORE_NAME)
    const raw = await requestResult(store.get(ownerRequestId))
    if (!isDeliveryWalRecord(raw) || raw.revision !== expectedRevision) {
      await transactionDone(transaction)
      return { applied: false, record: isDeliveryWalRecord(raw) ? structuredClone(raw) : null }
    }
    if (record) {
      if (!isDeliveryWalRecord(record) || record.ownerRequestId !== ownerRequestId
        || record.revision !== expectedRevision + 1 || record.deliveryIdentity !== raw.deliveryIdentity) {
        throw new Error('Invalid delivery compare-and-swap transition')
      }
      store.put(indexedDelivery(structuredClone(record)))
    } else store.delete(ownerRequestId)
    await transactionDone(transaction)
    return { applied: true, record: record ? structuredClone(record) : null }
  }

  async delete(pendingInputId: string): Promise<void> {
    const database = await this.database()
    const transaction = database.transaction(STORE_NAME, 'readwrite')
    transaction.objectStore(STORE_NAME).delete(pendingInputId)
    await transactionDone(transaction)
  }

  close(): void {
    this.openEpoch += 1
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
