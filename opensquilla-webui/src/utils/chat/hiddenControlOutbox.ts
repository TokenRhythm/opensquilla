export interface HiddenControlOutboxItem {
  sessionKey: string
  clientRequestId: string
  providerText: string
  displayText: string
  createdAtMs: number
}

export type HiddenControlStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>
export type HiddenControlPersistResult =
  | 'persisted'
  | 'matched'
  | 'conflict'
  | 'invalid'
  | 'unavailable'
  | 'failed'

const STORAGE_KEY = 'opensquilla.chat.hiddenControlOutbox:v1'
const MAX_ITEMS = 20
const MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000
const REQUEST_ID_PATTERN = /^\S{1,256}$/

function defaultStorage(): HiddenControlStorage | null {
  if (typeof window === 'undefined') return null
  try {
    return window.sessionStorage
  } catch {
    return null
  }
}

function normalizeItem(value: unknown, nowMs = Date.now()): HiddenControlOutboxItem | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const candidate = value as Partial<HiddenControlOutboxItem>
  const sessionKey = typeof candidate.sessionKey === 'string' ? candidate.sessionKey.trim() : ''
  const clientRequestId = typeof candidate.clientRequestId === 'string'
    ? candidate.clientRequestId.trim()
    : ''
  const providerText = typeof candidate.providerText === 'string' ? candidate.providerText : ''
  const displayText = typeof candidate.displayText === 'string' ? candidate.displayText : ''
  const createdAtMs = candidate.createdAtMs
  if (
    !sessionKey
    || sessionKey.length > 512
    || !REQUEST_ID_PATTERN.test(clientRequestId)
    || !providerText
    || providerText.length > 128_000
    || displayText.length > 128_000
    || typeof createdAtMs !== 'number'
    || !Number.isFinite(createdAtMs)
    || createdAtMs > nowMs
    || nowMs - createdAtMs > MAX_AGE_MS
  ) return null
  return { sessionKey, clientRequestId, providerText, displayText, createdAtMs }
}

function readResult(
  storage: HiddenControlStorage | null,
  nowMs = Date.now(),
): { items: HiddenControlOutboxItem[], ok: boolean } {
  if (!storage) return { items: [], ok: false }
  try {
    const raw = storage.getItem(STORAGE_KEY)
    if (!raw) return { items: [], ok: true }
    const parsed = JSON.parse(raw)
    if (!Array.isArray(parsed)) return { items: [], ok: true }
    return { items: parsed
      .map(value => normalizeItem(value, nowMs))
      .filter((value): value is HiddenControlOutboxItem => value !== null)
      .slice(-MAX_ITEMS), ok: true }
  } catch {
    return { items: [], ok: false }
  }
}

function read(storage: HiddenControlStorage | null, nowMs = Date.now()): HiddenControlOutboxItem[] {
  return readResult(storage, nowMs).items
}

function write(storage: HiddenControlStorage | null, items: HiddenControlOutboxItem[]): boolean {
  if (!storage) return false
  try {
    if (items.length === 0) storage.removeItem(STORAGE_KEY)
    else storage.setItem(STORAGE_KEY, JSON.stringify(items.slice(-MAX_ITEMS)))
    return true
  } catch {
    return false
  }
}

export function persistHiddenControlResult(
  item: Omit<HiddenControlOutboxItem, 'createdAtMs'> & { createdAtMs?: number },
  storage: HiddenControlStorage | null = defaultStorage(),
): HiddenControlPersistResult {
  const normalized = normalizeItem({ ...item, createdAtMs: item.createdAtMs ?? Date.now() })
  if (!normalized) return 'invalid'
  if (!storage) return 'unavailable'
  const state = readResult(storage)
  if (!state.ok) return 'failed'
  const items = state.items
  const existing = items.find(candidate => (
    candidate.sessionKey === normalized.sessionKey
    && candidate.clientRequestId === normalized.clientRequestId
  ))
  if (existing) {
    // A stable ingress identity is immutable. Never let a later caller replace
    // its provider/display payload and turn a safe retry into a fingerprint
    // conflict (or a different hidden action).
    return existing.providerText === normalized.providerText
      && existing.displayText === normalized.displayText
      ? 'matched'
      : 'conflict'
  }
  items.push(normalized)
  return write(storage, items) ? 'persisted' : 'failed'
}

export function persistHiddenControl(
  item: Omit<HiddenControlOutboxItem, 'createdAtMs'> & { createdAtMs?: number },
  storage: HiddenControlStorage | null = defaultStorage(),
): boolean {
  const result = persistHiddenControlResult(item, storage)
  return result === 'persisted' || result === 'matched'
}

export function removeHiddenControl(
  sessionKey: string,
  clientRequestId: string,
  storage: HiddenControlStorage | null = defaultStorage(),
): void {
  const items = read(storage)
  const retained = items.filter(candidate => !(
    candidate.sessionKey === sessionKey
    && candidate.clientRequestId === clientRequestId
  ))
  if (retained.length !== items.length) write(storage, retained)
}

export function listHiddenControls(
  sessionKey: string,
  storage: HiddenControlStorage | null = defaultStorage(),
): HiddenControlOutboxItem[] {
  const items = read(storage)
  // Also rewrite after validation so expired/corrupt entries cannot accumulate.
  write(storage, items)
  return items.filter(candidate => candidate.sessionKey === sessionKey)
}
