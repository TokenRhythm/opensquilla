import type { DeliveryWalRecord, PendingInputWal } from '../../src/utils/chat/pendingInputWal'

const DATABASE = 'opensquilla-chat-pending-inputs'
const STORE = 'response_handoffs'

export function recoveryRecord(ownerRequestId: string): DeliveryWalRecord {
  return {
    schemaVersion: 2, ownerRequestId, deliveryIdentity: 'synthetic-identity',
    requestSessionKey: 'synthetic-session', phase: 'unknown', revision: 1,
    createdAt: 1, updatedAt: 1,
    request: { kind: 'send', request: { kind: 'new-turn', params: {
      sessionKey: 'synthetic-session', clientRequestId: ownerRequestId,
      clientMessageId: `message-${ownerRequestId}`, message: 'Synthetic recovery input',
    } } },
  }
}

/** Bulk fixture loading is outside the measured production read path. */
export async function seedRecoveryRows(
  wal: PendingInputWal, pendingIds: string[], settledIds: string[] = [],
): Promise<void> {
  await wal.listRecoveryDeliveries!()
  const database = await new Promise<IDBDatabase>((resolve, reject) => {
    const request = indexedDB.open(DATABASE, 3)
    request.onsuccess = () => resolve(request.result)
    request.onerror = () => reject(request.error)
  })
  try {
    const transaction = database.transaction(STORE, 'readwrite')
    const done = new Promise<void>((resolve, reject) => {
      transaction.oncomplete = () => resolve()
      transaction.onabort = () => reject(transaction.error || new Error('Fixture write aborted'))
      transaction.onerror = () => reject(transaction.error)
    })
    const store = transaction.objectStore(STORE)
    store.clear()
    for (const id of pendingIds) store.put({ ...recoveryRecord(id), recoveryState: 'pending' })
    for (const id of settledIds) store.put({ ...recoveryRecord(id), phase: 'not-sent', recoveryState: 'settled' })
    await done
  } finally {
    database.close()
  }
}

/** Count actual native IDB callbacks, including the end-of-range callback. */
export async function measureRecoveryTraversal(wal: PendingInputWal, limit = 16) {
  const originalOpen = IDBIndex.prototype.openCursor
  const originalSeek = IDBCursor.prototype.continuePrimaryKey
  let cursorCallbacks = 0
  let seekCalls = 0
  IDBIndex.prototype.openCursor = function (...args) {
    const request = originalOpen.apply(this, args)
    if (this.name === 'recovery_state') {
      request.addEventListener('success', () => { cursorCallbacks += 1 })
    }
    return request
  }
  IDBCursor.prototype.continuePrimaryKey = function (...args) {
    seekCalls += 1
    return originalSeek.apply(this, args)
  }
  const ids: string[] = []
  const pageSizes: number[] = []
  const started = performance.now()
  try {
    let after: string | undefined
    do {
      const page = await wal.listRecoveryDeliveries!(after, limit)
      ids.push(...page.records.map(record => record.ownerRequestId))
      pageSizes.push(page.records.length)
      if (page.next && after && indexedDB.cmp(page.next, after) <= 0) {
        throw new Error('Recovery pagination failed to advance')
      }
      after = page.next
    } while (after)
    return { ids, pageSizes, cursorCallbacks, seekCalls, wallMs: performance.now() - started }
  } finally {
    IDBIndex.prototype.openCursor = originalOpen
    IDBCursor.prototype.continuePrimaryKey = originalSeek
  }
}
