import { randomUUID } from 'node:crypto'
import {
  chmodSync,
  closeSync,
  constants,
  existsSync,
  fsyncSync,
  lstatSync,
  mkdirSync,
  openSync,
  readFileSync,
  readdirSync,
  renameSync,
  rmdirSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs'
import { join, resolve } from 'node:path'

import { resolveMirroredConsent } from './consent-mirror.js'
import {
  validateDesktopEarlyTelemetryEvent,
  type DesktopEarlyTelemetryEvent,
  type TelemetryScope,
} from './contracts.js'

export const EARLY_SPOOL_MAX_FILES = 512
export const EARLY_SPOOL_MAX_BYTES = 4 * 1024 * 1024
export const EARLY_SPOOL_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000
export const EARLY_SPOOL_DURABLE_MARKER_RESERVATION_BYTES = 32 * 1024
export const DESKTOP_RELIABILITY_SESSION_MARKER_NAME = '.desktop-reliability-session.tmp'
export const DESKTOP_UPDATE_TRANSITION_MARKER_NAME = '.desktop-update-transition.tmp'
export const DESKTOP_RELIABILITY_RECOVERY_MARKER_PREFIX = '.desktop-reliability-recovery-'

function isDurableTelemetryMarker(name: string): boolean {
  return name === DESKTOP_RELIABILITY_SESSION_MARKER_NAME
    || name === DESKTOP_UPDATE_TRANSITION_MARKER_NAME
    || (
      name.startsWith(DESKTOP_RELIABILITY_RECOVERY_MARKER_PREFIX)
      && name.endsWith('.tmp')
      && /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(name.slice(
        DESKTOP_RELIABILITY_RECOVERY_MARKER_PREFIX.length,
        -'.tmp'.length,
      ))
    )
}

type Environment = Readonly<Record<string, string | undefined>>

export interface EarlySpoolOptions {
  spoolRoot: string
  consentMirrorPath: string
  event: unknown
  runtimeGate: DesktopTelemetryRuntimeGate
  env?: Environment
  now?: Date
}

export type EarlySpoolResult =
  | { status: 'written'; path: string }
  | { status: 'duplicate'; path: string }
  | {
      status: 'dropped'
      reason: 'invalid_event' | 'consent_blocked' | 'quota_exceeded' | 'unsafe_path' | 'io_error'
    }

interface SpoolUsage {
  files: number
  bytes: number
}

export interface DurableMarkerCapacityOptions {
  spoolRoot: string
  scope: TelemetryScope
  markerName: string
  payloadBytes: number
  now?: Date
}

export interface EarlyScopeCleanupResult {
  removed: number
  failed: number
  unsafe: boolean
}

/**
 * Process-local fail-closed gate. It opens only after the current profile's
 * authoritative config has been mirrored successfully during this process.
 */
export class DesktopTelemetryRuntimeGate {
  #synchronized = false

  close(): void {
    this.#synchronized = false
  }

  openAfterConsentSync(): void {
    this.#synchronized = true
  }

  isOpen(): boolean {
    return this.#synchronized
  }
}

function bestEffortChmod(path: string, mode: number): void {
  try {
    chmodSync(path, mode)
  } catch {
    // Windows and some managed filesystems do not expose POSIX permission bits.
  }
}

function requireRealDirectory(path: string): void {
  const metadata = lstatSync(path)
  if (metadata.isSymbolicLink() || !metadata.isDirectory()) {
    throw new Error('unsafe telemetry spool directory')
  }
}

function ensureScopeDirectory(spoolRoot: string, scope: TelemetryScope): string {
  mkdirSync(spoolRoot, { recursive: true, mode: 0o700 })
  requireRealDirectory(spoolRoot)
  bestEffortChmod(spoolRoot, 0o700)

  const scopeDirectory = join(spoolRoot, scope)
  try {
    mkdirSync(scopeDirectory, { recursive: false, mode: 0o700 })
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'EEXIST') throw error
  }
  requireRealDirectory(scopeDirectory)
  bestEffortChmod(scopeDirectory, 0o700)
  return scopeDirectory
}

function existingScopeDirectories(spoolRoot: string): string[] {
  const directories: string[] = []
  for (const scope of ['reliability', 'growth'] as const) {
    const directory = join(spoolRoot, scope)
    if (!existsSync(directory)) continue
    requireRealDirectory(directory)
    directories.push(directory)
  }
  return directories
}

function pruneAndMeasure(spoolRoot: string, nowMs: number): SpoolUsage {
  let files = 0
  let bytes = 0
  for (const directory of existingScopeDirectories(spoolRoot)) {
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      const path = join(directory, entry.name)
      const metadata = lstatSync(path)
      if (metadata.isDirectory()) throw new Error('unsafe telemetry spool entry')
      const recognized =
        entry.name.endsWith('.ready') ||
        entry.name.includes('.processing.') ||
        entry.name.includes('.tmp')
      if (
        recognized
        && !isDurableTelemetryMarker(entry.name)
        && nowMs - metadata.mtimeMs > EARLY_SPOOL_MAX_AGE_MS
      ) {
        unlinkSync(path)
        continue
      }
      files += 1
      bytes += isDurableTelemetryMarker(entry.name)
        ? Math.max(metadata.size, EARLY_SPOOL_DURABLE_MARKER_RESERVATION_BYTES)
        : metadata.size
    }
  }
  return { files, bytes }
}

/**
 * Apply the same global file/byte quota to a durable marker write. Replacing an
 * existing marker receives credit only for that exact no-follow regular file;
 * new recovery markers therefore cannot silently grow past the queue bound.
 */
export function canWriteDurableTelemetryMarker(
  options: DurableMarkerCapacityOptions,
): boolean {
  if (
    options.scope !== 'reliability'
    || !isDurableTelemetryMarker(options.markerName)
    || !Number.isSafeInteger(options.payloadBytes)
    || options.payloadBytes < 0
    || options.payloadBytes > EARLY_SPOOL_MAX_BYTES
  ) return false
  const now = options.now ?? new Date()
  if (!Number.isFinite(now.valueOf())) return false

  try {
    const root = resolve(options.spoolRoot)
    const scopeDirectory = join(root, options.scope)
    requireRealDirectory(root)
    requireRealDirectory(scopeDirectory)
    const target = join(scopeDirectory, options.markerName)
    let replacedFiles = 0
    let replacedBytes = 0
    if (existsSync(target)) {
      const metadata = lstatSync(target)
      if (metadata.isSymbolicLink() || !metadata.isFile()) return false
      replacedFiles = 1
      replacedBytes = Math.max(
        metadata.size,
        EARLY_SPOOL_DURABLE_MARKER_RESERVATION_BYTES,
      )
    }
    const usage = pruneAndMeasure(root, now.valueOf())
    const nextFiles = usage.files - replacedFiles + 1
    const nextBytes = usage.bytes - replacedBytes + Math.max(
      options.payloadBytes,
      EARLY_SPOOL_DURABLE_MARKER_RESERVATION_BYTES,
    )
    if (nextFiles <= EARLY_SPOOL_MAX_FILES && nextBytes <= EARLY_SPOOL_MAX_BYTES) return true
    // An upgraded spool may already exceed an older bound. Permit only a
    // no-growth replacement so acknowledgement bits can still advance and the
    // backlog can converge; never grant this exception to a new marker.
    return replacedFiles === 1
      && nextFiles <= usage.files
      && nextBytes <= usage.bytes
  } catch {
    return false
  }
}

function syncDirectoryBestEffort(directory: string): void {
  let descriptor: number | null = null
  try {
    descriptor = openSync(directory, constants.O_RDONLY)
    fsyncSync(descriptor)
  } catch {
    // Directory fsync is not available on every supported Windows/filesystem pair.
  } finally {
    if (descriptor !== null) {
      try {
        closeSync(descriptor)
      } catch {
        // The durable file fsync already completed; close errors stay best-effort.
      }
    }
  }
}

function writeReadyFile(
  scopeDirectory: string,
  event: DesktopEarlyTelemetryEvent,
  payload: Buffer,
): EarlySpoolResult {
  const destination = join(scopeDirectory, `${event.event_id}.ready`)
  if (existsSync(destination)) {
    try {
      const metadata = lstatSync(destination)
      if (
        metadata.isSymbolicLink() ||
        !metadata.isFile() ||
        metadata.size !== payload.byteLength
      ) {
        return { status: 'dropped', reason: 'unsafe_path' }
      }
      const existing = readFileSync(destination)
      if (!existing.equals(payload)) return { status: 'dropped', reason: 'unsafe_path' }
    } catch {
      return { status: 'dropped', reason: 'unsafe_path' }
    }
    return { status: 'duplicate', path: destination }
  }

  const temporary = join(
    scopeDirectory,
    `.${event.event_id}.${process.pid}.${randomUUID()}.tmp`,
  )
  let descriptor: number | null = null
  try {
    descriptor = openSync(
      temporary,
      constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY,
      0o600,
    )
    writeFileSync(descriptor, payload)
    fsyncSync(descriptor)
    closeSync(descriptor)
    descriptor = null
    bestEffortChmod(temporary, 0o600)
    renameSync(temporary, destination)
    bestEffortChmod(destination, 0o600)
    syncDirectoryBestEffort(scopeDirectory)
    return { status: 'written', path: destination }
  } catch {
    if (descriptor !== null) {
      try {
        closeSync(descriptor)
      } catch {
        // Cleanup remains best-effort.
      }
    }
    try {
      unlinkSync(temporary)
    } catch {
      // The rename may have completed, or the temp file was never created.
    }
    return { status: 'dropped', reason: 'io_error' }
  }
}

function existingReadyFileResult(
  scopeDirectory: string,
  event: DesktopEarlyTelemetryEvent,
  payload: Buffer,
): EarlySpoolResult | null {
  const destination = join(scopeDirectory, `${event.event_id}.ready`)
  if (!existsSync(destination)) return null
  try {
    const metadata = lstatSync(destination)
    if (
      metadata.isSymbolicLink()
      || !metadata.isFile()
      || metadata.size !== payload.byteLength
      || !readFileSync(destination).equals(payload)
    ) return { status: 'dropped', reason: 'unsafe_path' }
  } catch {
    return { status: 'dropped', reason: 'unsafe_path' }
  }
  return { status: 'duplicate', path: destination }
}

/**
 * Persist one Electron-owned event for later Python ingestion. This function
 * never performs network I/O and never creates spool state without live consent.
 */
export function spoolEarlyTelemetryEvent(options: EarlySpoolOptions): EarlySpoolResult {
  if (!(options.runtimeGate instanceof DesktopTelemetryRuntimeGate) || !options.runtimeGate.isOpen()) {
    return { status: 'dropped', reason: 'consent_blocked' }
  }
  let event: DesktopEarlyTelemetryEvent
  try {
    event = validateDesktopEarlyTelemetryEvent(options.event)
  } catch {
    return { status: 'dropped', reason: 'invalid_event' }
  }

  const consent = resolveMirroredConsent(
    options.consentMirrorPath,
    event.consent_scope,
    options.env ?? process.env,
  )
  if (!consent.enabled || consent.noticeVersion !== event.notice_version) {
    return { status: 'dropped', reason: 'consent_blocked' }
  }

  const spoolRoot = resolve(options.spoolRoot)
  const now = options.now ?? new Date()
  if (!Number.isFinite(now.valueOf())) return { status: 'dropped', reason: 'io_error' }

  try {
    const scopeDirectory = ensureScopeDirectory(spoolRoot, event.consent_scope)
    // Re-read after the scope directory exists. A withdrawal closes the
    // mirror before atomically detaching this directory, so a producer that
    // observed an older grant either stops here or loses its old pathname.
    const currentConsent = resolveMirroredConsent(
      options.consentMirrorPath,
      event.consent_scope,
      options.env ?? process.env,
    )
    if (!currentConsent.enabled || currentConsent.noticeVersion !== event.notice_version) {
      return { status: 'dropped', reason: 'consent_blocked' }
    }
    const payload = Buffer.from(JSON.stringify(event), 'utf8')
    const existing = existingReadyFileResult(scopeDirectory, event, payload)
    if (existing !== null) return existing
    const payloadBytes = payload.byteLength
    const usage = pruneAndMeasure(spoolRoot, now.valueOf())
    if (
      payloadBytes > EARLY_SPOOL_MAX_BYTES ||
      usage.files + 1 > EARLY_SPOOL_MAX_FILES ||
      usage.bytes + payloadBytes > EARLY_SPOOL_MAX_BYTES
    ) {
      return { status: 'dropped', reason: 'quota_exceeded' }
    }
    return writeReadyFile(scopeDirectory, event, payload)
  } catch (error) {
    const code = (error as NodeJS.ErrnoException).code
    if (code === 'ELOOP' || code === 'ENOTDIR' || code === 'EISDIR') {
      return { status: 'dropped', reason: 'unsafe_path' }
    }
    if (error instanceof Error && error.message.startsWith('unsafe telemetry')) {
      return { status: 'dropped', reason: 'unsafe_path' }
    }
    return { status: 'dropped', reason: 'io_error' }
  }
}

function isManagedScopeEntry(name: string): boolean {
  return (
    name.endsWith('.ready') ||
    name.includes('.processing.') ||
    (name.startsWith('.') && name.endsWith('.tmp'))
  )
}

function quarantinePrefix(scope: TelemetryScope): string {
  return `.revoked-${scope}-`
}

function isScopeQuarantine(name: string, scope: TelemetryScope): boolean {
  const suffix = name.slice(quarantinePrefix(scope).length)
  return name.startsWith(quarantinePrefix(scope))
    && /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(suffix)
}

/** Remove one scope's pending local telemetry after a durable opt-out. */
export function clearEarlyTelemetryScope(
  spoolRoot: string,
  scope: TelemetryScope,
): EarlyScopeCleanupResult {
  const root = resolve(spoolRoot)
  if (!existsSync(root)) return { removed: 0, failed: 0, unsafe: false }
  try {
    requireRealDirectory(root)
  } catch {
    return { removed: 0, failed: 0, unsafe: true }
  }
  const scopeDirectory = join(root, scope)
  if (existsSync(scopeDirectory)) {
    try {
      requireRealDirectory(scopeDirectory)
      const quarantine = join(root, `${quarantinePrefix(scope)}${randomUUID()}`)
      renameSync(scopeDirectory, quarantine)
      syncDirectoryBestEffort(root)
    } catch {
      return { removed: 0, failed: 1, unsafe: true }
    }
  }

  let removed = 0
  let failed = 0
  let unsafe = false
  try {
    const quarantines = readdirSync(root, { withFileTypes: true })
      .filter((entry) => isScopeQuarantine(entry.name, scope))
      .map((entry) => join(root, entry.name))
    for (const quarantine of quarantines) {
      try {
        requireRealDirectory(quarantine)
        for (const entry of readdirSync(quarantine, { withFileTypes: true })) {
          const path = join(quarantine, entry.name)
          if (!isManagedScopeEntry(entry.name)) {
            unsafe = true
            continue
          }
          try {
            const metadata = lstatSync(path)
            if (metadata.isSymbolicLink()) {
              unsafe = true
              continue
            }
            if (!metadata.isFile()) {
              failed += 1
              continue
            }
            unlinkSync(path)
            removed += 1
          } catch {
            failed += 1
          }
        }
        try {
          rmdirSync(quarantine)
        } catch {
          if (readdirSync(quarantine).length > 0) unsafe = true
          else failed += 1
        }
      } catch {
        unsafe = true
      }
    }
    syncDirectoryBestEffort(root)
  } catch {
    failed += 1
  }
  return { removed, failed, unsafe }
}
