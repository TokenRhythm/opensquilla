import { randomBytes } from 'node:crypto'
import { closeSync, fstatSync, lstatSync, mkdirSync, openSync, realpathSync, unlinkSync, type Stats } from 'node:fs'
import { join } from 'node:path'

const ACTIVATION_TTL_MS = 5_000
const MAX_OWNED_ACKNOWLEDGEMENTS = 32
const NONCE = /^[a-f0-9]{32}$/

export interface DesktopActivationRequest {
  version: 1
  nonce: string
  expiresAt: number
}

const ownedAcknowledgements = new Map<string, { identity: Stats; timer: NodeJS.Timeout }>()

export function createDesktopActivationRequest(): DesktopActivationRequest {
  return { version: 1, nonce: randomBytes(16).toString('hex'), expiresAt: Date.now() + ACTIVATION_TTL_MS }
}

function requestShape(value: unknown): DesktopActivationRequest | null {
  if (!value || typeof value !== 'object') return null
  const candidate = value as Partial<DesktopActivationRequest>
  return candidate.version === 1 && typeof candidate.nonce === 'string' && NONCE.test(candidate.nonce)
    && Number.isSafeInteger(candidate.expiresAt)
    ? candidate as DesktopActivationRequest : null
}

function acknowledgementPath(userData: string, request: DesktopActivationRequest, create = false): string {
  // The other process supplies only a nonce, never a path. Canonicalize the
  // existing userData parent and reject a redirected acknowledgement directory.
  const directory = join(realpathSync(userData), 'desktop-activation')
  if (create) {
    try { mkdirSync(directory, { mode: 0o700 }) } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'EEXIST') throw error
    }
  }
  const directoryStat = lstatSync(directory)
  if (!directoryStat.isDirectory() || directoryStat.isSymbolicLink()) throw new Error('Unsafe activation directory')
  return join(directory, `${request.nonce}.ack`)
}

function receiptMatches(stat: Stats, request: DesktopActivationRequest): boolean {
  // ACKs are empty exclusive-created files. No acknowledgement read follows a
  // symlink or reads caller-selected content; an old receipt is not a new ACK.
  return stat.isFile() && !stat.isSymbolicLink() && stat.nlink === 1 && stat.size === 0
    && stat.mtimeMs >= request.expiresAt - ACTIVATION_TTL_MS
}

function sameReceipt(current: Stats, created: Stats): boolean {
  return current.isFile() && !current.isSymbolicLink() && current.nlink === 1
    && current.dev === created.dev && current.ino === created.ino
    && current.mtimeMs === created.mtimeMs && current.size === 0
}

export function hasDesktopActivationAcknowledgement(userData: string, request: DesktopActivationRequest): boolean {
  try {
    if (!requestShape(request) || request.expiresAt < Date.now()) return false
    return receiptMatches(lstatSync(acknowledgementPath(userData, request)), request)
  } catch { return false }
}

export function disposeDesktopActivationRequest(userData: string, request: DesktopActivationRequest): void {
  try {
    if (!requestShape(request)) return
    const path = acknowledgementPath(userData, request)
    if (receiptMatches(lstatSync(path), request)) unlinkSync(path)
  } catch { /* Receipts are advisory; never turn cleanup into a launch failure. */ }
}

function clearOwnedAcknowledgement(path: string): void {
  const owned = ownedAcknowledgements.get(path)
  if (!owned) return
  ownedAcknowledgements.delete(path)
  clearTimeout(owned.timer)
  try {
    const current = lstatSync(path)
    if (sameReceipt(current, owned.identity)) unlinkSync(path)
  } catch { /* The requesting process normally removes its own receipt first. */ }
}

/** Called only after the primary has actually revealed a window while running. */
export function acknowledgeDesktopActivation(userData: string, additionalData: unknown): boolean {
  try {
    if (!additionalData || typeof additionalData !== 'object') return false
    const request = requestShape((additionalData as { desktopActivation?: unknown }).desktopActivation)
    const remaining = request ? request.expiresAt - Date.now() : -1
    if (!request || remaining < 0 || remaining > ACTIVATION_TTL_MS) return false
    if (ownedAcknowledgements.size >= MAX_OWNED_ACKNOWLEDGEMENTS) return false
    const path = acknowledgementPath(userData, request, true)
    // wx refuses existing files and final-component symlinks on Windows too.
    // Repeated deliveries therefore cannot overwrite another receipt or extend
    // the first one's cleanup deadline.
    const descriptor = openSync(path, 'wx', 0o600)
    let identity: Stats
    try { identity = fstatSync(descriptor) } finally { closeSync(descriptor) }
    // Ownership comes from the exclusively-created handle, never a later path
    // lookup that could already identify somebody else's replacement file.
    if (!sameReceipt(lstatSync(path), identity)) return false
    const timer = setTimeout(() => clearOwnedAcknowledgement(path), Math.max(1, remaining + 1_000))
    timer.unref()
    ownedAcknowledgements.set(path, { identity, timer })
    return true
  } catch { return false }
}

export function clearDesktopActivationAcknowledgements(): void {
  for (const path of ownedAcknowledgements.keys()) clearOwnedAcknowledgement(path)
}
