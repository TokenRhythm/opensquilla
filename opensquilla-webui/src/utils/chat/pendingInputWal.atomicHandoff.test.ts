import { afterEach, describe, expect, it, vi } from 'vitest'
import { nextTick, ref } from 'vue'
import { useChatPendingQueue } from '@/composables/chat/useChatPendingQueue'
import { createPendingQueuePolicy } from './pendingQueuePolicy'

import {
  createPendingInputWal,
  type PendingInputWalRecord,
  type ResponseHandoffWalRecord,
  type DeliveryWalRecord,
} from './pendingInputWal'

const PENDING_STORE = 'pending_chat_inputs'
const HANDOFF_STORE = 'response_handoffs'
afterEach(() => vi.unstubAllGlobals())

async function stoppedQueueHandoffFixture() {
  vi.stubGlobal('IDBKeyRange', { bound: (lower: unknown, upper: unknown) => ({ lower, upper }) })
  const factory = new ControlledIdbFactory()
  const wal = createPendingInputWal(factory.idbFactory)!
  const source = { sessionKey: 'handoff-parent', deliveryIdentity: 'handoff-account' }
  const target = { ...source, sessionKey: 'handoff-child' }
  const ownerRequestId = 'handoff-request'
  const values = new Map<string, string>()
  const storage = {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => { values.set(key, value) },
  }
  for (const [position, text] of ['C', 'D'].entries()) {
    await wal.put({ schemaVersion: 1, pendingInputId: text, sessionKey: source.sessionKey,
      clientRequestId: `request-${text}`, clientMessageId: `message-${text}`,
      text, attachments: [], intent: null, ownerRequestId, state: 'local_only',
      position, walRevision: 1, createdAt: 1, updatedAt: 1 })
  }
  await wal.putHandoff!({ schemaVersion: 1, ownerRequestId, requestSessionKey: source.sessionKey,
    clientRequestId: ownerRequestId, clientMessageId: 'message-A',
    params: { sessionKey: source.sessionKey, message: 'A', clientRequestId: ownerRequestId,
      clientMessageId: 'message-A' }, composerText: 'A',
    recoveryAttachments: [], state: 'submitting', createdAt: 1, updatedAt: 1 })
  const makeQueue = (initialSession = source.sessionKey) => {
    const policy = createPendingQueuePolicy(storage)
    const sessionKey = ref(initialSession)
    const send = vi.fn(async () => 'accepted' as const)
    const queue = useChatPendingQueue({ sessionKey, deliveryIdentity: ref(source.deliveryIdentity),
      pendingInputWal: wal, pendingQueuePolicy: policy, inputText: ref(''),
      pendingAttachments: ref([]), pendingSessionIntent: ref(null), isStreaming: ref(false),
      isBlocked: () => false, hasComposer: () => true, autoResizeTextarea: vi.fn(),
      resetInputHistory: vi.fn(), sendCurrentInput: vi.fn(),
      dispatchPendingItem: async (_item, _key, guard) => guard?.() === false ? 'deferred' : send(),
    })
    return { queue, policy, sessionKey, send }
  }
  return { factory, wal, source, target, ownerRequestId, storage, makeQueue }
}

describe('queue Stop across the real WAL handoff', () => {
  it.each(['adopt', 'recover'] as const)('releases only the temporary hold after an ordinary %s', async path => {
    const f = await stoppedQueueHandoffFixture()
    const first = f.makeQueue()
    try {
      f.factory.holdNextAtomicTransaction()
      const moving = path === 'adopt'
        ? first.queue.adoptPendingQueue(f.target.sessionKey, f.ownerRequestId)
        : first.queue.recoverPendingQueueHandoff(f.source.sessionKey, f.target.sessionKey, f.ownerRequestId)
      await f.factory.waitForHeldWrites()
      expect(first.policy.read(f.target).paused).toBe(true)
      f.factory.releaseHeldTransaction()
      await moving
      expect(first.policy.read(f.target).paused).toBe(false)
      expect(first.policy.read(f.source).paused).toBe(false)
      expect((await f.wal.list(f.target.sessionKey)).map(item => item.text)).toEqual(['C', 'D'])
    } finally { first.queue.cleanup(); f.wal.close() }
  })

  it.each([
    ['adopt', 'before'], ['recover', 'before'],
    ['adopt', 'during'], ['recover', 'during'],
  ] as const)('keeps C/D paused when Stop is %s / %s the migration wait', async (path, stopAt) => {
    const f = await stoppedQueueHandoffFixture()
    const first = f.makeQueue()
    let reloaded: ReturnType<typeof f.makeQueue> | undefined
    try {
      await first.queue.hydratePendingQueue(f.source.sessionKey)
      const peer = createPendingQueuePolicy(f.storage)
      if (stopAt === 'before') peer.pause(f.source)
      f.factory.holdNextAtomicTransaction()
      const moving = path === 'adopt'
        ? first.queue.adoptPendingQueue(f.target.sessionKey, f.ownerRequestId)
        : first.queue.recoverPendingQueueHandoff(f.source.sessionKey, f.target.sessionKey, f.ownerRequestId)
      await f.factory.waitForHeldWrites()
      expect(createPendingQueuePolicy(f.storage).read(f.target).paused).toBe(true)
      if (stopAt === 'during') peer.pause(f.source)
      // Unmount before commit: durable state still has to be correct for the next page.
      first.queue.cleanup()
      f.factory.releaseHeldTransaction()
      await moving
      expect(await f.wal.list(f.source.sessionKey)).toEqual([])
      expect((await f.wal.list(f.target.sessionKey)).map(item => item.text)).toEqual(['C', 'D'])
      reloaded = f.makeQueue(f.target.sessionKey)
      await reloaded.queue.hydratePendingQueue(f.target.sessionKey)
      expect(reloaded.queue.autoSendPaused.value).toBe(true)
      reloaded.queue.schedulePendingDrainAfterTerminal()
      await new Promise(resolve => setTimeout(resolve, 100))
      expect(reloaded.send).not.toHaveBeenCalled()
      expect(reloaded.queue.pendingQueue.value.map(item => item.text)).toEqual(['C', 'D'])
      expect(peer.read(f.source).paused).toBe(true)
      // A repeated accepted receipt must preserve a later explicit child Resume.
      reloaded.policy.resume(f.target)
      const resumed = reloaded.policy.read(f.target)
      await reloaded.queue.recoverPendingQueueHandoff(f.source.sessionKey, f.target.sessionKey, f.ownerRequestId)
      expect(reloaded.policy.read(f.target)).toMatchObject(resumed)
      reloaded.queue.pausePendingAutoSend()
      const stopped = reloaded.policy.read(f.target)
      await reloaded.queue.recoverPendingQueueHandoff(f.source.sessionKey, f.target.sessionKey, f.ownerRequestId)
      expect(reloaded.policy.read(f.target)).toMatchObject(stopped)
    } finally { first.queue.cleanup(); reloaded?.queue.cleanup(); f.wal.close() }
  })

  it('keeps the child held after WAL commit even before the accepting page resumes', async () => {
    const f = await stoppedQueueHandoffFixture()
    const first = f.makeQueue()
    let reloaded: ReturnType<typeof f.makeQueue> | undefined
    let release!: () => void
    try {
      const accept = f.wal.acceptHandoff!.bind(f.wal)
      let committed = false
      f.wal.acceptHandoff = async (...args) => {
        const result = await accept(...args)
        committed = true
        await new Promise<void>(resolve => { release = resolve })
        return result
      }
      const moving = first.queue.adoptPendingQueue(f.target.sessionKey, f.ownerRequestId)
      await vi.waitFor(() => expect(committed).toBe(true))
      first.queue.pausePendingAutoSend()
      first.queue.cleanup()
      reloaded = f.makeQueue(f.target.sessionKey)
      await reloaded.queue.hydratePendingQueue(f.target.sessionKey)
      await nextTick()
      expect(reloaded.queue.autoSendPaused.value).toBe(true)
      reloaded.queue.schedulePendingDrainAfterTerminal()
      await new Promise(resolve => setTimeout(resolve, 100))
      expect(reloaded.send).not.toHaveBeenCalled()
      f.wal.acceptHandoff = accept
      await reloaded.queue.recoverPendingQueueHandoff(f.source.sessionKey, f.target.sessionKey, f.ownerRequestId)
      expect(reloaded.queue.autoSendPaused.value).toBe(true)
      release()
      await moving
    } finally { release?.(); first.queue.cleanup(); reloaded?.queue.cleanup(); f.wal.close() }
  })

  it('does not move durable rows when the child hold cannot be persisted', async () => {
    const f = await stoppedQueueHandoffFixture()
    const first = f.makeQueue()
    try {
      f.storage.setItem = () => { throw new Error('synthetic quota failure') }
      await expect(first.queue.adoptPendingQueue(f.target.sessionKey, f.ownerRequestId))
        .rejects.toThrow('Could not persist the pending queue handoff policy')
      expect((await f.wal.list(f.source.sessionKey)).map(item => item.text)).toEqual(['C', 'D'])
      expect(await f.wal.list(f.target.sessionKey)).toEqual([])
    } finally { first.queue.cleanup(); f.wal.close() }
  })
})

type StoreName = typeof PENDING_STORE | typeof HANDOFF_STORE
type StoredValue = PendingInputWalRecord | ResponseHandoffWalRecord | DeliveryWalRecord

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
  readonly versions: number[] = []
  private readonly stores = new Map<StoreName, Map<IDBValidKey, StoredValue>>()
  private holdNextAtomic = false
  private heldTransaction: ControlledTransaction | null = null
  private heldWriteStores: StoreName[] = []
  private heldWritesPromise: Promise<readonly StoreName[]> = Promise.resolve([])
  private resolveHeldWrites: ((stores: readonly StoreName[]) => void) | null = null

  readonly idbFactory = {
    open: (_name: string, version: number) => { this.versions.push(version); return this.open() },
  } as unknown as IDBFactory

  open(): IDBOpenDBRequest {
    const request = new ControlledOpenRequest()
    queueMicrotask(() => {
      const database = new ControlledDatabase(this)
      request.result = database as unknown as IDBDatabase
      if (!this.hasStore(PENDING_STORE)) {
        request.onupgradeneeded?.(new Event('upgradeneeded') as IDBVersionChangeEvent)
      }
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

  releaseHeldTransaction(): void {
    this.heldTransaction?.release()
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
    private held: boolean,
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

  getAll(store: StoreName, filter: (value: StoredValue) => boolean = () => true): IDBRequest<StoredValue[]> {
    return this.request(() => [...(this.working.get(store)?.values() || [])].filter(filter).map(clone))
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

  release(): void {
    this.held = false
    this.queueCompletion()
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

  index(name: string): IDBIndex {
    if (name !== 'session_created') throw new Error(`Unsupported controlled index: ${name}`)
    return {
      getAll: (range: IDBKeyRange) => this.transaction.getAll(this.name, value => (
        (value as PendingInputWalRecord).sessionKey === range.lower[0]
      )),
    } as unknown as IDBIndex
  }

  put(value: StoredValue): IDBRequest<IDBValidKey> {
    return this.transaction.put(this.name, value)
  }

  delete(key: IDBValidKey): IDBRequest<undefined> {
    return this.transaction.delete(this.name, key)
  }
}

describe('BrowserPendingInputWal atomic handoff cancellation', () => {
  it('copies explicit path metadata and degrades invalid metadata to full editable text', async () => {
    vi.stubGlobal('IDBKeyRange', { bound: (lower: unknown, upper: unknown) => ({ lower, upper }) })
    const factory = new ControlledIdbFactory()
    const wal = createPendingInputWal(factory.idbFactory)!
    const path = 'C:\\book.pdf'
    const record: PendingInputWalRecord = {
      schemaVersion: 1, pendingInputId: 'path-ref', sessionKey: 'session',
      clientRequestId: 'request', clientMessageId: 'message', text: `Read\n${path}`,
      localPathReferences: [path], attachments: [], intent: null, state: 'local_only',
      createdAt: 1, updatedAt: 1,
    }
    try {
      await wal.put(record)
      record.localPathReferences![0] = 'C:\\changed.pdf'
      const stored = await wal.list('session')
      expect(stored[0]?.localPathReferences).toEqual([path])
      expect(stored[0]?.text).toBe(`Read\n${path}`)
      await wal.put({ ...record, pendingInputId: 'invalid-ref', text: 'Original manual text' })
      const invalid = (await wal.list('session')).find(item => item.pendingInputId === 'invalid-ref')
      expect(invalid?.text).toBe('Original manual text')
      expect(invalid?.localPathReferences).toEqual([])
    } finally { wal.close() }
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


it('retains the initial model pin through durable handoff storage without adding it to pending inputs', async () => {
  const factory = new ControlledIdbFactory()
  const wal = createPendingInputWal(factory.idbFactory)!
  const record: ResponseHandoffWalRecord = {
    schemaVersion: 1, ownerRequestId: 'pin-request', requestSessionKey: 'agent:main:webchat:draft',
    clientRequestId: 'pin-request', clientMessageId: 'pin-message', composerText: 'Start', recoveryAttachments: [],
    state: 'submitting', createdAt: 1, updatedAt: 1,
    params: {
      sessionKey: 'agent:main:webchat:draft', clientRequestId: 'pin-request', clientMessageId: 'pin-message',
      message: 'Start', intent: 'new_chat', initialRoutingMode: 'direct',
      initialModel: 'model-a', initialProvider: 'provider-a',
    },
  }
  await wal.putHandoff!(record)
  record.params.initialModel = 'locally-edited'
  expect((await wal.listHandoffs!())[0]?.params.initialModel).toBe('model-a')
  expect((await wal.listHandoffs!())[0]?.params.initialProvider).toBe('provider-a')
  expect(factory.snapshot([PENDING_STORE]).get(PENDING_STORE)?.size).toBe(0)
  await wal.acceptHandoff!('pin-request', 'agent:main:webchat:accepted')
  expect((await wal.listHandoffs!())[0]?.params.initialModel).toBe('model-a')
  wal.close()
})

function deliveryRecord(id = 'synthetic-delivery'): DeliveryWalRecord {
  return {
    schemaVersion: 2, ownerRequestId: id, deliveryIdentity: 'synthetic-identity', requestSessionKey: 'synthetic-session',
    request: { kind: 'send', request: { kind: 'new-turn', params: {
      sessionKey: 'synthetic-session', clientRequestId: id, clientMessageId: 'synthetic-message', message: 'synthetic text',
    } } }, phase: 'unknown', revision: 1, createdAt: 1, updatedAt: 1,
  }
}

it('uses v3 and preserves delivery authority while handoff fields change', async () => {
  const factory = new ControlledIdbFactory()
  const wal = createPendingInputWal(factory.idbFactory)!
  const record = deliveryRecord()
  await wal.prepareDelivery!(record)
  expect(factory.versions).toEqual([3])
  const handoff: ResponseHandoffWalRecord = {
    schemaVersion: 1, ownerRequestId: record.ownerRequestId, requestSessionKey: record.requestSessionKey,
    clientRequestId: record.ownerRequestId, clientMessageId: 'synthetic-message', composerText: 'synthetic text',
    recoveryAttachments: [], params: { sessionKey: record.requestSessionKey, clientRequestId: record.ownerRequestId,
      clientMessageId: 'synthetic-message', message: 'synthetic text' },
    state: 'submitting', createdAt: 1, updatedAt: 1,
  }
  await wal.putHandoff!(handoff)
  await wal.acceptHandoff!(record.ownerRequestId, 'synthetic-target')
  expect(await wal.getDelivery!(record.ownerRequestId)).toMatchObject({
    schemaVersion: 2, deliveryIdentity: 'synthetic-identity', phase: 'unknown',
    handoff: { state: 'accepted', acceptedSessionKey: 'synthetic-target' },
  })
  await wal.deleteHandoff!(record.ownerRequestId)
  expect(await wal.getDelivery!(record.ownerRequestId)).toMatchObject({ schemaVersion: 2, phase: 'unknown' })
  expect(await wal.listHandoffs!()).toEqual([])
  expect(factory.snapshot([HANDOFF_STORE]).get(HANDOFF_STORE)?.size).toBe(1)
  wal.close()
})

it('rejects stale delivery CAS and does not adopt legacy records without a live handoff owner', async () => {
  const factory = new ControlledIdbFactory()
  const wal = createPendingInputWal(factory.idbFactory)!
  const record = deliveryRecord()
  await wal.prepareDelivery!(record)
  const stopped = { ...record, revision: 2, stop: { requested: true as const } }
  expect((await wal.compareAndSwapDelivery!(record.ownerRequestId, 1, stopped)).applied).toBe(true)
  expect((await wal.compareAndSwapDelivery!(record.ownerRequestId, 1, { ...record, revision: 2, phase: 'accepted' })).applied).toBe(false)
  expect((await wal.getDelivery!(record.ownerRequestId))?.stop?.requested).toBe(true)
  const legacy: ResponseHandoffWalRecord = {
    schemaVersion: 1, ownerRequestId: 'legacy-request', requestSessionKey: 'synthetic-session', clientRequestId: 'legacy-request',
    clientMessageId: 'synthetic-message', composerText: 'synthetic text', recoveryAttachments: [],
    params: { sessionKey: 'synthetic-session', clientRequestId: 'legacy-request', clientMessageId: 'synthetic-message', message: 'synthetic text' },
    state: 'submitting', createdAt: 1, updatedAt: 1,
  }
  await wal.putHandoff!(legacy)
  expect(await wal.prepareDelivery!(deliveryRecord('legacy-request'))).toEqual({ applied: false, record: null })
  expect(factory.record(HANDOFF_STORE, 'legacy-request')).toEqual(legacy)
  wal.close()
})

it('closes a late successful blocked open and never deletes the database on a downgrade error', async () => {
  const blocked = new ControlledOpenRequest()
  const downgraded = new ControlledOpenRequest()
  const close = vi.fn()
  const indexedDb = { open: vi.fn().mockReturnValueOnce(blocked).mockReturnValueOnce(downgraded), deleteDatabase: vi.fn() }
  const wal = createPendingInputWal(indexedDb as unknown as IDBFactory)!
  const waiting = wal.listDeliveries!()
  blocked.onblocked?.(new Event('blocked'))
  await expect(waiting).rejects.toThrow('blocked')
  blocked.result = { close } as unknown as IDBDatabase
  blocked.onsuccess?.(new Event('success'))
  expect(close).toHaveBeenCalledTimes(1)
  const retry = wal.listDeliveries!()
  downgraded.error = new DOMException('Synthetic newer schema exists', 'VersionError')
  downgraded.onerror?.(new Event('error'))
  await expect(retry).rejects.toMatchObject({ name: 'VersionError' })
  expect(indexedDb.deleteDatabase).not.toHaveBeenCalled()
  wal.close()
})
