import { createHash, webcrypto } from 'node:crypto'
import { IDBFactory, IDBObjectStore } from 'fake-indexeddb'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { DELIVERY_SALT_KEY, deliverySalt, deliveryTargetId } from './deliveryIdentity'

const DATABASE = 'opensquilla-delivery-identity'
const legacySalt = '1234567890abcdef1234567890abcdef'
let stored: Map<string, string>

beforeEach(() => {
  stored = new Map()
  vi.stubGlobal('indexedDB', new IDBFactory())
  vi.stubGlobal('navigator', {})
  vi.stubGlobal('crypto', webcrypto)
  vi.stubGlobal('localStorage', {
    getItem: (key: string) => stored.get(key) ?? null,
    setItem: (key: string, value: string) => stored.set(key, value),
  })
})
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals() })

async function metadata(value?: unknown): Promise<unknown> {
  const db = await new Promise<IDBDatabase>((resolve, reject) => {
    const request = indexedDB.open(DATABASE, 1)
    request.onupgradeneeded = () => request.result.createObjectStore('metadata')
    request.onsuccess = () => resolve(request.result)
    request.onerror = () => reject(request.error)
  })
  try {
    return await new Promise((resolve, reject) => {
      const tx = db.transaction('metadata', value === undefined ? 'readonly' : 'readwrite')
      const store = tx.objectStore('metadata')
      if (value !== undefined) store.put(value, DELIVERY_SALT_KEY)
      const request = store.get(DELIVERY_SALT_KEY)
      tx.oncomplete = () => resolve(request.result)
      tx.onabort = () => reject(tx.error)
    })
  } finally { db.close() }
}

describe('durable origin identity salt on HTTP', () => {
  it('creates one canonical salt across concurrent first-use callers without Web Locks', async () => {
    const salts = await Promise.all(Array.from({ length: 12 }, () => deliverySalt()))
    expect(new Set(salts).size).toBe(1)
    expect(salts[0]).toMatch(/^[0-9a-f]{32}$/)
    expect(stored.get(DELIVERY_SALT_KEY)).toBe(salts[0])
    expect(await metadata()).toBe(salts[0])
    expect(await deliverySalt()).toBe(salts[0])
  })

  it('imports the exact legacy salt and recreates only its missing compatibility mirror', async () => {
    stored.set(DELIVERY_SALT_KEY, legacySalt)
    expect(await deliverySalt()).toBe(legacySalt)
    expect(await metadata()).toBe(legacySalt)
    stored.delete(DELIVERY_SALT_KEY)
    expect(await deliverySalt()).toBe(legacySalt)
    expect(stored.get(DELIVERY_SALT_KEY)).toBe(legacySalt)
  })

  it('keeps the original Web Lock and reads an old tab salt only after acquiring it', async () => {
    let enter!: () => void
    const request = vi.fn((_name: string, action: () => Promise<string>) => new Promise<string>((resolve, reject) => {
      enter = () => { action().then(resolve, reject) }
    }))
    vi.stubGlobal('navigator', { locks: { request } })
    const pending = deliverySalt()
    expect(request).toHaveBeenCalledWith(DELIVERY_SALT_KEY, expect.any(Function))
    stored.set(DELIVERY_SALT_KEY, legacySalt)
    enter()
    expect(await pending).toBe(legacySalt)
    expect(await metadata()).toBe(legacySalt)
  })

  it('serializes new callers with and without Web Locks through the same IDB authority', async () => {
    const withoutLocks = deliverySalt()
    vi.stubGlobal('navigator', { locks: { request: async (_name: string, action: () => Promise<string>) => action() } })
    const withLocks = deliverySalt()
    const [first, second] = await Promise.all([withoutLocks, withLocks])
    expect(first).toBe(second)
    expect(await metadata()).toBe(first)
  })

  it('refuses differing persisted salts without overwriting either value', async () => {
    await metadata(legacySalt)
    const peer = 'b'.repeat(32)
    stored.set(DELIVERY_SALT_KEY, peer)
    await expect(deliverySalt()).rejects.toThrow('salts conflict')
    expect(await metadata()).toBe(legacySalt)
    expect(stored.get(DELIVERY_SALT_KEY)).toBe(peer)
  })

  it.each(['legacy', 'canonical'])('refuses malformed %s storage without replacing it', async source => {
    if (source === 'legacy') stored.set(DELIVERY_SALT_KEY, 'invalid-salt')
    else await metadata('invalid-salt')
    await expect(deliverySalt()).rejects.toThrow('salt is invalid')
    expect(source === 'legacy' ? stored.get(DELIVERY_SALT_KEY) : await metadata()).toBe('invalid-salt')
  })

  it('does not persist an identity when legacy storage cannot publish it', async () => {
    vi.spyOn(localStorage, 'setItem').mockImplementation(() => { throw new Error('synthetic quota') })
    await expect(deliverySalt()).rejects.toThrow('synthetic quota')
    expect(await metadata()).toBeUndefined()
    expect(stored.has(DELIVERY_SALT_KEY)).toBe(false)
  })

  it('reuses the already published legacy value after an aborted canonical write', async () => {
    const write = vi.spyOn(IDBObjectStore.prototype, 'put').mockImplementationOnce(() => { throw new Error('synthetic abort') })
    await expect(deliverySalt()).rejects.toThrow('synthetic abort')
    const published = stored.get(DELIVERY_SALT_KEY)
    expect(published).toMatch(/^[0-9a-f]{32}$/)
    expect(await metadata()).toBeUndefined()
    write.mockRestore()
    expect(await deliverySalt()).toBe(published)
    expect(await metadata()).toBe(published)
  })

  it('rejects a legacy writer changing the salt during publication', async () => {
    vi.spyOn(localStorage, 'setItem').mockImplementation((key: string) => stored.set(key, 'c'.repeat(32)))
    await expect(deliverySalt()).rejects.toThrow('salt changed')
    expect(await metadata()).toBeUndefined()
    expect(stored.get(DELIVERY_SALT_KEY)).toBe('c'.repeat(32))
  })

  it.each(['indexedDB', 'localStorage', 'random'])('fails closed when %s is unavailable', async unavailable => {
    if (unavailable === 'indexedDB') vi.stubGlobal('indexedDB', undefined)
    if (unavailable === 'localStorage') vi.spyOn(localStorage, 'getItem').mockImplementation(() => { throw new Error('storage denied') })
    if (unavailable === 'random') vi.stubGlobal('crypto', {})
    await expect(deliverySalt()).rejects.toThrow()
    expect(stored.has(DELIVERY_SALT_KEY)).toBe(false)
  })
})

describe('byte-compatible HTTP target fingerprints', () => {
  const targets = [
    JSON.stringify(['browser', 'ws://nas:18791/ws', '']),
    JSON.stringify(['browser', 'ws://nas:18791/ws?token=a%2Fb', '引号"\\令牌🔒']),
    JSON.stringify(['desktop', 'profile/一', 'x'.repeat(200)]),
    ...[0, 1, 55, 56, 63, 64, 65, 119, 120, 128, 10000].map(length => 'x'.repeat(length)),
  ]
  it.each(targets)('matches Node SHA-256 and native digest for input %#', async target => {
    const expected = createHash('sha256').update(JSON.stringify([legacySalt, target]), 'utf8').digest('hex')
    expect(await deliveryTargetId(legacySalt, target)).toBe(expected)
    vi.stubGlobal('crypto', { getRandomValues: webcrypto.getRandomValues.bind(webcrypto) })
    expect(await deliveryTargetId(legacySalt, target)).toBe(expected)
  })

  it('preserves native failure instead of masking it with another identity path', async () => {
    vi.stubGlobal('crypto', { subtle: { digest: vi.fn(async () => { throw new Error('native crypto rejected') }) } })
    await expect(deliveryTargetId(legacySalt, 'target')).rejects.toThrow('native crypto rejected')
  })
})
