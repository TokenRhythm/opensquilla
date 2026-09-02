import { describe, expect, it } from 'vitest'

import {
  createPendingInputWal,
  type PendingInputWalRecord,
  type ResponseHandoffWalRecord,
} from './pendingInputWal'

const PENDING_STORE = 'pending_chat_inputs'
const HANDOFF_STORE = 'response_handoffs'

type StoreName = typeof PENDING_STORE | typeof HANDOFF_STORE
type StoredValue = PendingInputWalRecord | ResponseHandoffWalRecord

function clone<T>(value: T): T {
  return structuredClone(value)
}

class ControlledRequest<T> {
  result!: T
  error: DOMException | null = null
  onsuccess: ((event: Event) => void) | null = null
  onerror: ((event: Event) => void) | null = null

  succeed(result: T): void {
    this.result = result
    this.onsuccess?.(new Event('success'))
  }
}

class ControlledOpenRequest extends ControlledRequest<IDBDatabase> {
  onupgradeneeded: ((event: IDBVersionChangeEvent) => void) | null = null
  onblocked: ((event: Event) => void) | null = null
}

class ControlledIdbFactory {
  private readonly stores = new Map<StoreName, Map<IDBValidKey, StoredValue>>()
  private holdNextAtomic = false
  private heldTransaction: ControlledTransaction | null = null
  private heldWriteStores: StoreName[] = []
  private heldWritesPromise: Promise<readonly StoreName[]> = Promise.resolve([])
  private resolveHeldWrites: ((stores: readonly StoreName[]) => void) | null = null

  readonly idbFactory = {
    open: () => this.open(),
  } as unknown as IDBFactory

  open(): IDBOpenDBRequest {
    const request = new ControlledOpenRequest()
    queueMicrotask(() => {
      const database = new ControlledDatabase(this)
      request.result = database as unknown as IDBDatabase
      request.onupgradeneeded?.(new Event('upgradeneeded') as IDBVersionChangeEvent)
      request.onsuccess?.(new Event('success'))
    })
    return request as unknown as IDBOpenDBRequest
  }

  createStore(name: string): void {
    if (name === PENDING_STORE || name === HANDOFF_STORE) {
      if (!this.stores.has(name)) this.stores.set(name, new Map())
    }
  }

  hasStore(name: string): boolean {
    return name === PENDING_STORE || name === HANDOFF_STORE
      ? this.stores.has(name)
      : false
  }

  createTransaction(names: StoreName[], mode: IDBTransactionMode): ControlledTransaction {
    const hold = this.holdNextAtomic
      && mode === 'readwrite'
      && names.includes(PENDING_STORE)
      && names.includes(HANDOFF_STORE)
    if (hold) this.holdNextAtomic = false
    const transaction = new ControlledTransaction(this, names, hold)
    if (hold) this.heldTransaction = transaction
    return transaction
  }

  snapshot(names: StoreName[]): Map<StoreName, Map<IDBValidKey, StoredValue>> {
    return new Map(names.map(name => [
      name,
      new Map(
        [...(this.stores.get(name) || new Map()).entries()]
          .map(([key, value]) => [key, clone(value)]),
      ),
    ]))
  }

  commit(snapshot: Map<StoreName, Map<IDBValidKey, StoredValue>>): void {
    for (const [name, records] of snapshot) {
      this.stores.set(name, new Map(
        [...records.entries()].map(([key, value]) => [key, clone(value)]),
      ))
    }
  }

  holdNextAtomicTransaction(): void {
    this.holdNextAtomic = true
    this.heldTransaction = null
    this.heldWriteStores = []
    this.heldWritesPromise = new Promise(resolve => {
      this.resolveHeldWrites = resolve
    })
  }

  noteWrite(transaction: ControlledTransaction, store: StoreName): void {
    if (transaction !== this.heldTransaction) return
    if (!this.heldWriteStores.includes(store)) this.heldWriteStores.push(store)
    if (
      this.heldWriteStores.includes(HANDOFF_STORE)
      && this.heldWriteStores.includes(PENDING_STORE)
    ) {
      this.resolveHeldWrites?.([...this.heldWriteStores])
      this.resolveHeldWrites = null
    }
  }

  waitForHeldWrites(): Promise<readonly StoreName[]> {
    return this.heldWritesPromise
  }

  heldTransactionIsActive(): boolean {
    return this.heldTransaction?.isActive() === true
  }

  record(store: StoreName, key: IDBValidKey): StoredValue | undefined {
    const value = this.stores.get(store)?.get(key)
    return value ? clone(value) : undefined
  }
}

class ControlledDatabase {
  onversionchange: ((event: Event) => void) | null = null

  constructor(private readonly factory: ControlledIdbFactory) {}

  get objectStoreNames(): DOMStringList {
    return {
      contains: name => this.factory.hasStore(name),
    } as DOMStringList
  }

  createObjectStore(name: string): IDBObjectStore {
    this.factory.createStore(name)
    return {
      createIndex: () => ({} as IDBIndex),
    } as unknown as IDBObjectStore
  }

  transaction(
    storeNames: string | Iterable<string>,
    mode: IDBTransactionMode = 'readonly',
  ): IDBTransaction {
    const names = typeof storeNames === 'string' ? [storeNames] : [...storeNames]
    return this.factory.createTransaction(names as StoreName[], mode) as unknown as IDBTransaction
  }

  close(): void {}
}

class ControlledTransaction {
  oncomplete: ((event: Event) => void) | null = null
  onabort: ((event: Event) => void) | null = null
  onerror: ((event: Event) => void) | null = null
  error: DOMException | null = null

  private readonly working: Map<StoreName, Map<IDBValidKey, StoredValue>>
  private active = true
  private outstandingRequests = 0
  private completionQueued = false

  constructor(
    private readonly factory: ControlledIdbFactory,
    names: StoreName[],
    private readonly held: boolean,
  ) {
    this.working = factory.snapshot(names)
  }

  objectStore(name: string): IDBObjectStore {
    return new ControlledObjectStore(this, name as StoreName) as unknown as IDBObjectStore
  }

  get(store: StoreName, key: IDBValidKey): IDBRequest<StoredValue | undefined> {
    return this.request(() => {
      const value = this.working.get(store)?.get(key)
      return value ? clone(value) : undefined
    })
  }

  getAll(store: StoreName): IDBRequest<StoredValue[]> {
    return this.request(() => [...(this.working.get(store)?.values() || [])].map(clone))
  }

  put(store: StoreName, value: StoredValue): IDBRequest<IDBValidKey> {
    const key = store === PENDING_STORE
      ? (value as PendingInputWalRecord).pendingInputId
      : (value as ResponseHandoffWalRecord).ownerRequestId
    this.working.get(store)?.set(key, clone(value))
    this.factory.noteWrite(this, store)
    this.queueCompletion()
    return {} as IDBRequest<IDBValidKey>
  }

  delete(store: StoreName, key: IDBValidKey): IDBRequest<undefined> {
    this.working.get(store)?.delete(key)
    this.queueCompletion()
    return {} as IDBRequest<undefined>
  }

  abort(): void {
    if (!this.active) throw new DOMException('Transaction is not active', 'InvalidStateError')
    this.active = false
    queueMicrotask(() => this.onabort?.(new Event('abort')))
  }

  isActive(): boolean {
    return this.active
  }

  private request<T>(read: () => T): IDBRequest<T> {
    const request = new ControlledRequest<T>()
    this.outstandingRequests += 1
    queueMicrotask(() => {
      if (!this.active) return
      request.succeed(read())
      this.outstandingRequests -= 1
      this.queueCompletion()
    })
    return request as unknown as IDBRequest<T>
  }

  private queueCompletion(): void {
    if (this.held || !this.active || this.outstandingRequests > 0 || this.completionQueued) return
    this.completionQueued = true
    queueMicrotask(() => {
      this.completionQueued = false
      if (this.held || !this.active || this.outstandingRequests > 0) return
      this.active = false
      this.factory.commit(this.working)
      this.oncomplete?.(new Event('complete'))
    })
  }
}

class ControlledObjectStore {
  constructor(
    private readonly transaction: ControlledTransaction,
    private readonly name: StoreName,
  ) {}

  get(key: IDBValidKey): IDBRequest<StoredValue | undefined> {
    return this.transaction.get(this.name, key)
  }

  getAll(): IDBRequest<StoredValue[]> {
    return this.transaction.getAll(this.name)
  }

  put(value: StoredValue): IDBRequest<IDBValidKey> {
    return this.transaction.put(this.name, value)
  }

  delete(key: IDBValidKey): IDBRequest<undefined> {
    return this.transaction.delete(this.name, key)
  }
}

describe('BrowserPendingInputWal atomic mutations', () => {
  it('does not retain a cancelled draft after another owner deletes its WAL row', async () => {
    const factory = new ControlledIdbFactory()
    const wal = createPendingInputWal(factory.idbFactory)
    expect(wal).not.toBeNull()
    const cancelling: PendingInputWalRecord = {
      schemaVersion: 1,
      pendingInputId: 'pending-retain-cas',
      sessionKey: 'agent:main:webchat:retain-cas',
      clientRequestId: 'request-retain-cas',
      clientMessageId: 'message-retain-cas',
      text: 'retain only while this tombstone owns the row',
      attachments: [],
      intent: null,
      state: 'cancelling',
      mayHaveServerCopy: false,
      retainAfterCancel: true,
      walRevision: 2,
      createdAt: 1,
      updatedAt: 2,
    }
    const retained = {
      ...cancelling,
      state: 'local_only' as const,
      walRevision: 3,
    }
    await wal!.put(cancelling)
    await wal!.delete(cancelling.pendingInputId)

    await expect(wal!.retainCancelled!(retained, 2)).resolves.toBeNull()
    expect(factory.record(PENDING_STORE, cancelling.pendingInputId)).toBeUndefined()

    await wal!.put(cancelling)
    await expect(wal!.retainCancelled!(retained, 2)).resolves.toMatchObject({
      state: 'local_only',
      retainAfterCancel: true,
      walRevision: 3,
    })
    expect(factory.record(PENDING_STORE, cancelling.pendingInputId)).toMatchObject({
      state: 'local_only',
      walRevision: 3,
    })
    wal!.close()
  })

  it('commits equivalent legacy session aliases under the canonical reorder owner', async () => {
    const factory = new ControlledIdbFactory()
    const wal = createPendingInputWal(factory.idbFactory)
    expect(wal).not.toBeNull()
    const canonicalSession = 'agent:main:webchat:alias-order'
    const legacySession = 'agent:default:webchat:alias-order'
    const record = (
      pendingInputId: string,
      sessionKey: string,
      text: string,
      position: number,
    ): PendingInputWalRecord => ({
      schemaVersion: 1,
      pendingInputId,
      sessionKey,
      clientRequestId: `request-${pendingInputId}`,
      clientMessageId: `message-${pendingInputId}`,
      text,
      attachments: [],
      intent: null,
      state: 'local_only',
      mayHaveServerCopy: false,
      position,
      walRevision: 1,
      createdAt: position + 1,
      updatedAt: position + 1,
    })
    const legacy = record('legacy-row', legacySession, 'legacy', 0)
    const canonical = record('canonical-row', canonicalSession, 'canonical', 1)
    await wal!.put(legacy)
    await wal!.put(canonical)

    const result = await wal!.commitOrder!(
      canonicalSession,
      ['canonical-row', 'legacy-row'],
      { 'legacy-row': 1, 'canonical-row': 1 },
      [canonicalSession, legacySession],
    )

    expect(result.records).toMatchObject([
      {
        pendingInputId: 'canonical-row',
        sessionKey: canonicalSession,
        position: 0,
        walRevision: 2,
      },
      {
        pendingInputId: 'legacy-row',
        sessionKey: canonicalSession,
        position: 1,
        walRevision: 2,
      },
    ])
    expect(factory.record(PENDING_STORE, 'legacy-row')).toMatchObject({
      sessionKey: canonicalSession,
      position: 1,
      walRevision: 2,
    })
    wal!.close()
  })

  it('rolls back both stores when the handoff epoch aborts after both writes are queued', async () => {
    const factory = new ControlledIdbFactory()
    const wal = createPendingInputWal(factory.idbFactory)
    expect(wal).not.toBeNull()

    const ownerRequestId = 'owner-atomic-abort'
    const sourceSessionKey = 'agent:main:webchat:A'
    const targetSessionKey = 'agent:main:webchat:B'
    const pendingInputId = 'pending-atomic-abort'
    const pending: PendingInputWalRecord = {
      schemaVersion: 1,
      pendingInputId,
      sessionKey: sourceSessionKey,
      clientRequestId: ownerRequestId,
      clientMessageId: 'message-atomic-abort',
      text: 'remain owned by A unless both writes commit',
      attachments: [],
      intent: null,
      ownerRequestId,
      state: 'saving',
      walRevision: 1,
      createdAt: 1,
      updatedAt: 1,
    }
    const handoff: ResponseHandoffWalRecord = {
      schemaVersion: 1,
      ownerRequestId,
      requestSessionKey: sourceSessionKey,
      clientRequestId: ownerRequestId,
      clientMessageId: pending.clientMessageId,
      params: {
        sessionKey: sourceSessionKey,
        message: pending.text,
        clientRequestId: ownerRequestId,
        clientMessageId: pending.clientMessageId,
      },
      composerText: pending.text,
      recoveryAttachments: [],
      state: 'submitting',
      createdAt: 1,
      updatedAt: 1,
    }

    await wal!.put(pending)
    await wal!.putHandoff!(handoff)

    factory.holdNextAtomicTransaction()
    const controller = new AbortController()
    const adoption = wal!.acceptHandoff!(
      ownerRequestId,
      targetSessionKey,
      () => true,
      controller.signal,
    )

    await expect(factory.waitForHeldWrites()).resolves.toEqual([
      HANDOFF_STORE,
      PENDING_STORE,
    ])
    expect(factory.heldTransactionIsActive()).toBe(true)

    controller.abort()
    await expect(adoption).resolves.toBeNull()

    expect(factory.record(HANDOFF_STORE, ownerRequestId)).toEqual(handoff)
    expect(factory.record(PENDING_STORE, pendingInputId)).toEqual(pending)

    const committed = await wal!.acceptHandoff!(ownerRequestId, targetSessionKey)
    expect(committed?.handoff).toMatchObject({
      state: 'accepted',
      acceptedSessionKey: targetSessionKey,
    })
    expect(committed?.records).toEqual([
      expect.objectContaining({
        pendingInputId,
        sessionKey: targetSessionKey,
        ownerRequestId: undefined,
        walRevision: 2,
      }),
    ])
    expect(factory.record(HANDOFF_STORE, ownerRequestId)).toMatchObject({
      state: 'accepted',
      acceptedSessionKey: targetSessionKey,
    })
    expect(factory.record(PENDING_STORE, pendingInputId)).toMatchObject({
      sessionKey: targetSessionKey,
      ownerRequestId: undefined,
      walRevision: 2,
    })

    wal!.close()
  })
})
