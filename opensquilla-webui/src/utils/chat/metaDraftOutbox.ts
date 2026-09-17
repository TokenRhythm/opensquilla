export interface DeferredMetaDraft {
  sessionKey: string
  launchText: string
  createdAtMs: number
}

export type MetaDraftStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>

const STORAGE_KEY = 'opensquilla.chat.metaDraftOutbox:v1'
const MAX_ITEMS = 20
const MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000

function defaultStorage(): MetaDraftStorage | null {
  if (typeof window === 'undefined') return null
  try {
    return window.sessionStorage
  } catch {
    return null
  }
}

function normalize(value: unknown, nowMs = Date.now()): DeferredMetaDraft | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const candidate = value as Partial<DeferredMetaDraft>
  const sessionKey = typeof candidate.sessionKey === 'string' ? candidate.sessionKey.trim() : ''
  const launchText = typeof candidate.launchText === 'string' ? candidate.launchText.trim() : ''
  const createdAtMs = candidate.createdAtMs
  if (
    !sessionKey
    || sessionKey.length > 512
    || !launchText.startsWith('/meta ')
    || launchText.length > 128_000
    || typeof createdAtMs !== 'number'
    || !Number.isFinite(createdAtMs)
    || createdAtMs > nowMs
    || nowMs - createdAtMs > MAX_AGE_MS
  ) return null
  return { sessionKey, launchText, createdAtMs }
}

function read(
  storage: MetaDraftStorage | null,
  nowMs = Date.now(),
): { items: DeferredMetaDraft[], ok: boolean } {
  if (!storage) return { items: [], ok: false }
  try {
    const raw = storage.getItem(STORAGE_KEY)
    if (!raw) return { items: [], ok: true }
    const parsed = JSON.parse(raw)
    if (!Array.isArray(parsed)) return { items: [], ok: true }
    return {
      items: parsed
        .map(value => normalize(value, nowMs))
        .filter((value): value is DeferredMetaDraft => value !== null)
        .slice(-MAX_ITEMS),
      ok: true,
    }
  } catch {
    return { items: [], ok: false }
  }
}

function write(storage: MetaDraftStorage | null, items: DeferredMetaDraft[]): boolean {
  if (!storage) return false
  try {
    if (items.length === 0) storage.removeItem(STORAGE_KEY)
    else storage.setItem(STORAGE_KEY, JSON.stringify(items.slice(-MAX_ITEMS)))
    return true
  } catch {
    return false
  }
}

export function persistDeferredMetaDraft(
  item: Omit<DeferredMetaDraft, 'createdAtMs'> & { createdAtMs?: number },
  storage: MetaDraftStorage | null = defaultStorage(),
): boolean {
  const normalized = normalize({ ...item, createdAtMs: item.createdAtMs ?? Date.now() })
  if (!normalized || !storage) return false
  const state = read(storage)
  if (!state.ok) return false
  if (state.items.some(existing => (
    existing.sessionKey === normalized.sessionKey
    && existing.launchText === normalized.launchText
  ))) return true
  return write(storage, [...state.items, normalized])
}

export function takeDeferredMetaDrafts(
  sessionKey: string,
  storage: MetaDraftStorage | null = defaultStorage(),
): string[] {
  const target = String(sessionKey || '').trim()
  if (!target || !storage) return []
  const state = read(storage)
  if (!state.ok) return []
  const selected = state.items.filter(item => item.sessionKey === target)
  const retained = state.items.filter(item => item.sessionKey !== target)
  if (!write(storage, retained)) return []
  return selected.map(item => item.launchText)
}
