import { sha256 } from '@noble/hashes/sha2.js'

export const DELIVERY_SALT_KEY = 'opensquilla.deliverySalt.v1'
const DATABASE_NAME = 'opensquilla-delivery-identity'
const STORE_NAME = 'metadata'
const SALT_PATTERN = /^[0-9a-f]{32}$/

function checkedSalt(value: unknown): string | null {
  if (value === null || value === undefined) return null
  if (typeof value !== 'string' || !SALT_PATTERN.test(value)) {
    throw new Error('Stored delivery identity salt is invalid')
  }
  return value
}

function openIdentityDatabase(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = globalThis.indexedDB.open(DATABASE_NAME, 1)
    let abandoned = false
    request.onupgradeneeded = () => {
      request.result.createObjectStore(STORE_NAME)
    }
    request.onerror = () => reject(request.error || new Error('Delivery identity storage is unavailable'))
    request.onblocked = () => {
      abandoned = true
      reject(new Error('Delivery identity storage is blocked'))
    }
    request.onsuccess = () => {
      if (abandoned) { request.result.close(); return }
      request.result.onversionchange = () => request.result.close()
      resolve(request.result)
    }
  })
}

/** Keep one origin-wide salt without changing the existing WAL identity format. */
export async function deliverySalt(): Promise<string> {
  const initialize = async () => {
    const database = await openIdentityDatabase()
    try {
      return await new Promise<string>((resolve, reject) => {
        const transaction = database.transaction(STORE_NAME, 'readwrite')
        const store = transaction.objectStore(STORE_NAME)
        let salt: string | null = null
        let failure: unknown
        const request = store.get(DELIVERY_SALT_KEY)
        request.onsuccess = () => {
          try {
            // Read legacy storage only after this transaction can run. All new
            // tabs serialize here, including tabs without the Web Locks API.
            const canonical = checkedSalt(request.result)
            const legacy = checkedSalt(localStorage.getItem(DELIVERY_SALT_KEY))
            if (canonical && legacy && canonical !== legacy) {
              throw new Error('Stored delivery identity salts conflict; reload after resolving browser storage')
            }
            salt = canonical || legacy
            if (!salt) {
              // Unlike client request IDs, identity salts must never use a
              // weak random fallback. getRandomValues is available on HTTP.
              const bytes = globalThis.crypto.getRandomValues(new Uint8Array(16))
              salt = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('')
            }
            // Publish the compatible legacy value before committing the IDB
            // authority. An aborted transaction leaves a value we can reuse,
            // not an IDB-only salt an old tab could unknowingly replace.
            if (!legacy) localStorage.setItem(DELIVERY_SALT_KEY, salt)
            if (localStorage.getItem(DELIVERY_SALT_KEY) !== salt) {
              throw new Error('Delivery identity salt changed while saving')
            }
            if (!canonical) store.put(salt, DELIVERY_SALT_KEY)
          } catch (error) {
            failure = error
            transaction.abort()
          }
        }
        transaction.onabort = () => reject(failure || transaction.error || new Error('Delivery identity storage was aborted'))
        transaction.onerror = () => { failure ||= transaction.error }
        transaction.oncomplete = () => {
          try {
            if (!salt || localStorage.getItem(DELIVERY_SALT_KEY) !== salt) {
              throw new Error('Delivery identity salt changed while saving')
            }
            resolve(salt)
          } catch (error) { reject(error) }
        }
      })
    } finally { database.close() }
  }
  // Keep the original lock name for older tabs that only know localStorage.
  // New tabs always use the IDB transaction, even when Web Locks is present.
  const locks = globalThis.navigator?.locks
  return locks?.request ? locks.request(DELIVERY_SALT_KEY, initialize) : initialize()
}

/** Byte-compatible with the existing SHA-256 target fingerprint on HTTP. */
export async function deliveryTargetId(salt: string, target: string): Promise<string> {
  const bytes = new TextEncoder().encode(JSON.stringify([salt, target]))
  const subtle = globalThis.crypto?.subtle
  // Keep native errors visible: only API absence selects the compatibility
  // implementation; neither branch changes the serialized identity bytes.
  const digest = typeof subtle?.digest === 'function'
    ? new Uint8Array(await subtle.digest('SHA-256', bytes)) : sha256(bytes)
  return Array.from(digest, byte => byte.toString(16).padStart(2, '0')).join('')
}
