import type { TurnSendSource } from '@/modules/turnCommands'
import type { GatewayModelRoutingMode } from '@/types/modelRouting'

export interface HiddenControlRequestSnapshot {
  intent: string | null
  initialRoutingMode: GatewayModelRoutingMode | null
  source: TurnSendSource
}

export interface HiddenControlOutboxItem {
  sessionKey: string
  clientRequestId: string
  providerText: string
  displayText: string
  createdAtMs: number
  dispatchAttempted: boolean | null
  requestSnapshot: HiddenControlRequestSnapshot | null
}

export type HiddenControlStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>
export type HiddenControlPersistResult =
  | 'persisted'
  | 'matched'
  | 'conflict'
  | 'invalid'
  | 'unavailable'
  | 'failed'

type HiddenControlOutboxInput = Omit<
  HiddenControlOutboxItem,
  'createdAtMs' | 'dispatchAttempted' | 'requestSnapshot'
> & {
  createdAtMs?: number
  dispatchAttempted?: boolean
  requestSnapshot?: HiddenControlRequestSnapshot
}

const STORAGE_KEY = 'opensquilla.chat.hiddenControlOutbox:v1'
const MAX_ITEMS = 20
const MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000
const REQUEST_ID_PATTERN = /^\S{1,256}$/

function normalizeRequestSnapshot(value: unknown): HiddenControlRequestSnapshot | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const candidate = value as Partial<HiddenControlRequestSnapshot>
  const intent = candidate.intent
  const initialRoutingMode = candidate.initialRoutingMode
  const source = candidate.source
  if (
    !(intent === null || (typeof intent === 'string' && intent.length <= 512))
    || !(
      initialRoutingMode === null
      || initialRoutingMode === 'direct'
      || initialRoutingMode === 'router'
      || initialRoutingMode === 'ensemble'
    )
    || !source
    || typeof source !== 'object'
    || Array.isArray(source)
    || (source.runMode !== 'safe' && source.runMode !== 'full')
    || !(source.elevated === undefined || typeof source.elevated === 'string')
  ) return null
  return {
    intent,
    initialRoutingMode,
    source: {
      ...(source.elevated ? { elevated: source.elevated } : {}),
      runMode: source.runMode,
    },
  }
}

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
  const dispatchAttempted = typeof candidate.dispatchAttempted === 'boolean'
    ? candidate.dispatchAttempted
    : null
  const requestSnapshot = normalizeRequestSnapshot(candidate.requestSnapshot)
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
  return {
    sessionKey,
    clientRequestId,
    providerText,
    displayText,
    createdAtMs,
    dispatchAttempted,
    requestSnapshot,
  }
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
  item: HiddenControlOutboxInput,
  storage: HiddenControlStorage | null = defaultStorage(),
): HiddenControlPersistResult {
  const normalized = normalizeItem({
    ...item,
    createdAtMs: item.createdAtMs ?? Date.now(),
    dispatchAttempted: item.dispatchAttempted ?? false,
    requestSnapshot: item.requestSnapshot ?? null,
  })
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
  item: HiddenControlOutboxInput,
  storage: HiddenControlStorage | null = defaultStorage(),
): boolean {
  const result = persistHiddenControlResult(item, storage)
  return result === 'persisted' || result === 'matched'
}

export function hiddenControlDispatchAttempted(
  sessionKey: string,
  clientRequestId: string,
  storage: HiddenControlStorage | null = defaultStorage(),
): boolean {
  return read(storage).some(candidate => (
    candidate.sessionKey === sessionKey
    && candidate.clientRequestId === clientRequestId
    && candidate.dispatchAttempted
  ))
}

export function hiddenControlReceiptReplayEligible(
  sessionKey: string,
  clientRequestId: string,
  allowLegacyUnknown: boolean,
  storage: HiddenControlStorage | null = defaultStorage(),
): boolean {
  const item = read(storage).find(candidate => (
    candidate.sessionKey === sessionKey
    && candidate.clientRequestId === clientRequestId
  ))
  return item?.dispatchAttempted === true
    || (item?.dispatchAttempted === null && allowLegacyUnknown)
}

export function markHiddenControlDispatchAttempted(
  sessionKey: string,
  clientRequestId: string,
  storage: HiddenControlStorage | null = defaultStorage(),
): boolean {
  const state = readResult(storage)
  if (!state.ok) return false
  const index = state.items.findIndex(candidate => (
    candidate.sessionKey === sessionKey
    && candidate.clientRequestId === clientRequestId
  ))
  if (index < 0) return false
  const current = state.items[index]!
  if (current.dispatchAttempted) return true
  state.items[index] = { ...current, dispatchAttempted: true }
  return write(storage, state.items)
}

export function markHiddenControlDispatchDefinitelyRejected(
  sessionKey: string,
  clientRequestId: string,
  storage: HiddenControlStorage | null = defaultStorage(),
): boolean {
  const state = readResult(storage)
  if (!state.ok) return false
  const index = state.items.findIndex(candidate => (
    candidate.sessionKey === sessionKey
    && candidate.clientRequestId === clientRequestId
  ))
  if (index < 0) return false
  const current = state.items[index]!
  if (current.dispatchAttempted === false) return true
  state.items[index] = { ...current, dispatchAttempted: false }
  return write(storage, state.items)
}

export function getHiddenControlRequestSnapshot(
  sessionKey: string,
  clientRequestId: string,
  storage: HiddenControlStorage | null = defaultStorage(),
): HiddenControlRequestSnapshot | null {
  return read(storage).find(candidate => (
    candidate.sessionKey === sessionKey
    && candidate.clientRequestId === clientRequestId
  ))?.requestSnapshot ?? null
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
