export interface PendingMetaDiscard {
  sessionKey: string
  clientRequestId: string
  createdAtMs: number
}

export type MetaDiscardStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>

const STORAGE_KEY = 'opensquilla.chat.metaDiscardOutbox:v1'
const MAX_ITEMS = 20
const REQUEST_ID_PATTERN = /^\S{1,256}$/

function defaultStorage(): MetaDiscardStorage | null {
  if (typeof window === 'undefined') return null
  try {
    return window.localStorage
  } catch {
    return null
  }
}

function normalize(value: unknown): PendingMetaDiscard | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const candidate = value as Partial<PendingMetaDiscard>
  const sessionKey = typeof candidate.sessionKey === 'string' ? candidate.sessionKey.trim() : ''
  const clientRequestId = typeof candidate.clientRequestId === 'string'
    ? candidate.clientRequestId.trim()
    : ''
  const createdAtMs = candidate.createdAtMs
  if (
    !sessionKey
    || sessionKey.length > 512
    || !REQUEST_ID_PATTERN.test(clientRequestId)
    || typeof createdAtMs !== 'number'
    || !Number.isFinite(createdAtMs)
  ) return null
  return {
    sessionKey,
    clientRequestId,
    createdAtMs,
  }
}

function read(storage: MetaDiscardStorage | null): PendingMetaDiscard[] {
  if (!storage) return []
  try {
    const raw = storage.getItem(STORAGE_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    return parsed
      .map(value => normalize(value))
      .filter((value): value is PendingMetaDiscard => value !== null)
  } catch {
    return []
  }
}

function write(storage: MetaDiscardStorage | null, items: PendingMetaDiscard[]): boolean {
  if (!storage) return false
  try {
    if (items.length === 0) storage.removeItem(STORAGE_KEY)
    else storage.setItem(STORAGE_KEY, JSON.stringify(items))
    return true
  } catch {
    return false
  }
}

export function persistPendingMetaDiscard(
  item: Omit<PendingMetaDiscard, 'createdAtMs'> & { createdAtMs?: number },
  storage: MetaDiscardStorage | null = defaultStorage(),
): boolean {
  const normalized = normalize({ ...item, createdAtMs: item.createdAtMs ?? Date.now() })
  if (!normalized || !storage) return false
  const items = read(storage)
  const existing = items.find(candidate => (
    candidate.sessionKey === normalized.sessionKey
    && candidate.clientRequestId === normalized.clientRequestId
  ))
  if (existing) return true
  // Never evict a cancellation tombstone: eviction could make a still-live
  // server draft launchable again. Refuse the new mutation instead.
  if (items.length >= MAX_ITEMS) return false
  return write(storage, [...items, normalized])
}

export function listPendingMetaDiscards(
  sessionKey?: string,
  storage: MetaDiscardStorage | null = defaultStorage(),
): PendingMetaDiscard[] {
  const items = read(storage)
  write(storage, items)
  const target = String(sessionKey || '').trim()
  return target ? items.filter(item => item.sessionKey === target) : items
}

export function removePendingMetaDiscard(
  sessionKey: string,
  clientRequestId: string,
  storage: MetaDiscardStorage | null = defaultStorage(),
): void {
  const items = read(storage)
  write(storage, items.filter(item => !(
    item.sessionKey === sessionKey
    && item.clientRequestId === clientRequestId
  )))
}
