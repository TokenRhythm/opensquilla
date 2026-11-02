import { shallowRef, type ShallowRef } from 'vue'

import { isModelRoutingMode, type ModelRoutingMode } from '@/types/modelRouting'

export const LAST_MANUAL_CHAT_SELECTION_STORAGE_KEY = 'opensquilla.chat.last-manual-selection'
const ISSUED_KEY = `${LAST_MANUAL_CHAT_SELECTION_STORAGE_KEY}.issued`
const LOCK_NAME = 'opensquilla.chat.manual-selection'

export interface LastManualChatSelection {
  mode: ModelRoutingMode
  model: { model: string; provider: string } | null
}

export type ChatSelectionStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>

export interface UseLastManualChatSelectionOptions {
  /** Embedded hosts use their own draft storage rather than browser storage. */
  storage?: ChatSelectionStorage | null
}

interface SelectionOrder { epoch: string; sequence: number }
interface ScopeState { issued: number; committed: number; queue: Promise<unknown> }
/** Captured when the picker action starts, before its asynchronous mutation. */
export interface ManualChatSelectionToken {
  readonly scope: object
  readonly localSequence: number
  readonly order: SelectionOrder | null
}

const scopes = new WeakMap<object, ScopeState>()
let lockDatabase: Promise<IDBDatabase> | null = null

function scopeState(scope: object): ScopeState {
  let state = scopes.get(scope)
  if (!state) {
    state = { issued: 0, committed: 0, queue: Promise.resolve() }
    scopes.set(scope, state)
  }
  return state
}

function browserStorage(): ChatSelectionStorage | null {
  try { return typeof localStorage === 'undefined' ? null : localStorage } catch { return null }
}

function validIdentifier(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0 && value.length <= 512
    && value === value.trim() && !/[\u0000-\u001f\u007f-\u009f]/.test(value)
}

function normalize(value: unknown): LastManualChatSelection | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const source = value as Record<string, unknown>
  if (!isModelRoutingMode(source.mode)) return null
  if (source.mode !== 'off') return { mode: source.mode, model: null }
  if (source.model === null) return { mode: 'off', model: null }
  if (!source.model || typeof source.model !== 'object' || Array.isArray(source.model)) return null
  const model = source.model as Record<string, unknown>
  if (!validIdentifier(model.model) || !validIdentifier(model.provider)) return null
  return { mode: 'off', model: { model: model.model, provider: model.provider } }
}

function clone(value: LastManualChatSelection | null): LastManualChatSelection | null {
  return value ? { mode: value.mode, model: value.model ? { ...value.model } : null } : null
}

function orderFrom(value: unknown): SelectionOrder | null {
  if (!value || typeof value !== 'object') return null
  const order = value as Partial<SelectionOrder>
  return typeof order.epoch === 'string' && order.epoch.length > 0
    && Number.isSafeInteger(order.sequence) && order.sequence! >= 0
    ? { epoch: order.epoch, sequence: order.sequence! } : null
}

function parse(value: string | null): unknown {
  try { return JSON.parse(value ?? 'null') } catch { return null }
}

function readStoredSelection(storage: ChatSelectionStorage | null): {
  available: boolean
  selection: LastManualChatSelection | null
  order: SelectionOrder | null
} {
  try {
    if (storage) {
      const value = parse(storage.getItem(LAST_MANUAL_CHAT_SELECTION_STORAGE_KEY))
      return {
        available: true,
        selection: normalize(value),
        order: orderFrom((value as { manualSelectionOrder?: unknown } | null)?.manualSelectionOrder),
      }
    }
  } catch { /* Storage restrictions must not prevent local choices. */ }
  return { available: false, selection: null, order: null }
}

function openLockDatabase(): Promise<IDBDatabase> {
  if (!lockDatabase) {
    lockDatabase = new Promise<IDBDatabase>((resolve, reject) => {
      const request = indexedDB.open('opensquilla-chat-selection-lock', 1)
      let blocked = false
      request.onupgradeneeded = () => request.result.createObjectStore('locks')
      request.onerror = () => reject(request.error)
      request.onblocked = () => { blocked = true; reject(new Error('Selection lock database blocked')) }
      request.onsuccess = () => {
        if (blocked) { request.result.close(); return }
        request.result.onversionchange = () => { request.result.close(); lockDatabase = null }
        resolve(request.result)
      }
    }).catch(error => { lockDatabase = null; throw error })
  }
  return lockDatabase
}

/** HTTP origins may lack Web Locks; an IDB write transaction also serializes tabs. */
async function withBrowserLock<T>(work: () => T): Promise<T> {
  let ran = false
  if (typeof navigator !== 'undefined' && navigator.locks) {
    try {
      return await navigator.locks.request(LOCK_NAME, () => { ran = true; return work() })
    } catch (error) {
      if (ran) throw error
    }
  }
  const database = await openLockDatabase()
  return new Promise<T>((resolve, reject) => {
    const transaction = database.transaction('locks', 'readwrite')
    let result: T
    let completedWork = false
    transaction.oncomplete = () => resolve(result)
    // The synchronous storage commit is already complete if the transaction
    // is aborted afterwards; do not turn that into a failed picker action.
    const failed = () => completedWork ? resolve(result)
      : reject(transaction.error ?? new Error('Selection lock aborted'))
    transaction.onerror = failed
    transaction.onabort = failed
    const request = transaction.objectStore('locks').get(LOCK_NAME)
    request.onsuccess = () => {
      try { result = work(); completedWork = true } catch (error) { transaction.abort(); reject(error) }
    }
  })
}

/**
 * Orders explicit picker actions across views and tabs, independently of RPC
 * completion order. Locks cover only short storage work, never a network call.
 */
export function useLastManualChatSelection(
  options: UseLastManualChatSelectionOptions = {},
): {
  selection: ShallowRef<LastManualChatSelection | null>
  readLatest: () => LastManualChatSelection | null
  beginSelection: () => Promise<ManualChatSelectionToken>
  remember: (value: unknown, token?: ManualChatSelectionToken) => Promise<LastManualChatSelection | null>
  clear: () => Promise<void>
} {
  const storage = options.storage === undefined ? browserStorage() : options.storage
  const scope = storage ?? {}
  const state = scopeState(scope)
  let current = readStoredSelection(storage).selection
  let unpersisted = false
  const selection = shallowRef<LastManualChatSelection | null>(clone(current))

  async function serialized<T>(work: (persistent: boolean) => T): Promise<T> {
    const operation = state.queue.catch(() => {}).then(async () => {
      if (!storage || options.storage !== undefined) return work(storage !== null)
      let ran = false
      try {
        return await withBrowserLock(() => { ran = true; return work(true) })
      } catch (error) {
        if (ran) throw error
        // If both cross-tab lock mechanisms are unavailable, retain the choice
        // in this view instead of risking an unordered shared-storage write.
        return work(false)
      }
    })
    state.queue = operation
    return operation
  }

  function publish(): LastManualChatSelection | null {
    selection.value = clone(current)
    return clone(current)
  }

  function readLatest(): LastManualChatSelection | null {
    if (!unpersisted) {
      const stored = readStoredSelection(storage)
      if (stored.available) current = stored.selection
    }
    return publish()
  }

  async function beginSelection(): Promise<ManualChatSelectionToken> {
    const localSequence = ++state.issued
    return serialized(persistent => {
      let order: SelectionOrder | null = null
      if (persistent && storage) {
        try {
          const issued = orderFrom(parse(storage.getItem(ISSUED_KEY)))
          order = issued && issued.sequence < Number.MAX_SAFE_INTEGER
            ? { epoch: issued.epoch, sequence: issued.sequence + 1 }
            : { epoch: Array.from(crypto.getRandomValues(new Uint32Array(4)), value => value.toString(16).padStart(8, '0')).join(''), sequence: 1 }
          storage.setItem(ISSUED_KEY, JSON.stringify(order))
        } catch { order = null }
      }
      return Object.freeze({ scope, localSequence, order: order && Object.freeze(order) })
    })
  }

  async function remember(value: unknown, token?: ManualChatSelectionToken): Promise<LastManualChatSelection | null> {
    const next = normalize(value)
    const action = token ?? await beginSelection()
    return serialized(persistent => {
      if (action.scope !== scope || action.localSequence < state.committed) return readLatest()
      if (action.order && persistent && storage) {
        try {
          const issued = orderFrom(parse(storage.getItem(ISSUED_KEY)))
          const stored = readStoredSelection(storage)
          if (!stored.available) throw new Error('Selection storage unavailable')
          if (issued?.epoch !== action.order.epoch || action.order.sequence > issued.sequence
            || (stored.order?.epoch === action.order.epoch && stored.order.sequence > action.order.sequence)) {
            return readLatest()
          }
          // Order and value are one write: an interrupted commit cannot expose
          // a newer value with an older committed order. Null is a tombstone.
          storage.setItem(LAST_MANUAL_CHAT_SELECTION_STORAGE_KEY, JSON.stringify({
            ...(next ?? { mode: null, model: null }), manualSelectionOrder: action.order,
          }))
          unpersisted = false
          state.committed = action.localSequence
          current = next
          return publish()
        } catch { /* The newest applied choice remains available in memory. */ }
      }
      state.committed = action.localSequence
      current = next
      unpersisted = true
      return publish()
    })
  }

  async function clear() { await remember(null) }

  return { selection, readLatest, beginSelection, remember, clear }
}
