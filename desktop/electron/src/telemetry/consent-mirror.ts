import { lstatSync, readFileSync } from 'node:fs'
import { chmod, lstat, mkdir, open, rename, rm, unlink } from 'node:fs/promises'
import { randomUUID } from 'node:crypto'
import { dirname } from 'node:path'

import {
  CURRENT_NOTICE_VERSION_BY_SCOPE,
  isSafeTelemetryVersion,
  type TelemetryScope,
} from './contracts.js'

export const CONSENT_MIRROR_SCHEMA_VERSION = 1
const MAX_CONSENT_MIRROR_BYTES = 16 * 1024
const UTC_TIMESTAMP_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$/
const TRUE_VALUES = new Set(['1', 'true', 'yes', 'on'])

export interface MirroredScopeConsent {
  enabled: boolean | null
  notice_version: string | null
  consented_at_utc: string | null
  forced_off: boolean
}

export interface ConsentMirror {
  schema_version: 1
  reliability: MirroredScopeConsent
  growth: MirroredScopeConsent
}

function validateConsentMirrorForWrite(mirror: ConsentMirror): void {
  if (mirror.schema_version !== CONSENT_MIRROR_SCHEMA_VERSION) {
    throw new TypeError('Unsupported consent mirror schema version.')
  }
  for (const scope of ['reliability', 'growth'] as const) {
    const state = mirror[scope]
    if (state.enabled !== null && typeof state.enabled !== 'boolean') {
      throw new TypeError(`Invalid ${scope} consent decision.`)
    }
    if (typeof state.forced_off !== 'boolean') throw new TypeError(`Invalid ${scope} forced-off state.`)
    if (state.enabled !== true && (state.notice_version !== null || state.consented_at_utc !== null)) {
      throw new TypeError(`Disabled ${scope} consent must not retain grant metadata.`)
    }
    if (
      state.enabled === true
      && (
        !isSafeTelemetryVersion(state.notice_version)
        || !isUtcConsentTimestamp(state.consented_at_utc)
      )
    ) {
      throw new TypeError(`Enabled ${scope} consent requires valid metadata.`)
    }
  }
}

/**
 * Atomically replace the profile-scoped mirror. The temporary file is created
 * in the destination directory so rename stays on one filesystem on every OS.
 */
export async function writeConsentMirror(path: string, mirror: ConsentMirror): Promise<void> {
  validateConsentMirrorForWrite(mirror)
  const directory = dirname(path)
  await mkdir(directory, { recursive: true, mode: 0o700 })
  const directoryMetadata = await lstat(directory)
  if (directoryMetadata.isSymbolicLink() || !directoryMetadata.isDirectory()) {
    throw new Error('Consent mirror directory is not a real directory.')
  }
  await chmod(directory, 0o700).catch(() => undefined)
  try {
    const targetMetadata = await lstat(path)
    if (targetMetadata.isSymbolicLink() || !targetMetadata.isFile()) {
      throw new Error('Consent mirror target is not a regular file.')
    }
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error
  }

  const temporaryPath = `${path}.${randomUUID()}.tmp`
  let handle: Awaited<ReturnType<typeof open>> | null = null
  try {
    handle = await open(temporaryPath, 'wx', 0o600)
    await handle.writeFile(`${JSON.stringify(mirror)}\n`, 'utf8')
    await handle.sync()
    await handle.close()
    handle = null
    await rename(temporaryPath, path)
    await chmod(path, 0o600).catch(() => undefined)
    // Directory fsync is unsupported on Windows; it is a durability bonus, not
    // a reason to turn a successfully replaced fail-closed snapshot into error.
    try {
      const directoryHandle = await open(directory, 'r')
      try {
        await directoryHandle.sync()
      } finally {
        await directoryHandle.close()
      }
    } catch {
      // Best effort on platforms that do not permit opening directories.
    }
  } catch (error) {
    if (handle !== null) await handle.close().catch(() => undefined)
    await rm(temporaryPath, { force: true }).catch(() => undefined)
    throw error
  }
}

/** Delete the purpose-specific Growth identity without following a link. */
export async function clearGrowthAnalyticsIdentity(path: string): Promise<boolean> {
  try {
    const metadata = await lstat(path)
    if (metadata.isSymbolicLink() || !metadata.isFile()) {
      throw new Error('Growth analytics identity is not a regular file.')
    }
    await unlink(path)
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') return false
    throw error
  }

  // Make the withdrawal durable where directory fsync is supported. The file
  // has already been removed, so unsupported directory handles stay best-effort.
  try {
    const directoryHandle = await open(dirname(path), 'r')
    try {
      await directoryHandle.sync()
    } finally {
      await directoryHandle.close()
    }
  } catch {
    // Windows and some managed filesystems do not permit opening directories.
  }
  return true
}

export type MirroredConsentBlockReason =
  | 'missing_or_invalid_mirror'
  | 'consent_unset'
  | 'consent_declined'
  | 'consent_incomplete'
  | 'notice_stale'
  | 'forced_off'
  | 'environment_forced_off'

export interface EffectiveMirroredConsent {
  scope: TelemetryScope
  enabled: boolean
  noticeVersion: string | null
  consentedAtUtc: string | null
  blockReason: MirroredConsentBlockReason | null
}

type Environment = Readonly<Record<string, string | undefined>>

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function hasExactKeys(value: Record<string, unknown>, expected: readonly string[]): boolean {
  const actual = Object.keys(value)
  const expectedSet = new Set(expected)
  return actual.length === expected.length && actual.every((key) => expectedSet.has(key))
}

function isUtcConsentTimestamp(value: unknown): value is string {
  if (typeof value !== 'string' || !UTC_TIMESTAMP_RE.test(value)) return false
  return Number.isFinite(Date.parse(value))
}

function parseScopeConsent(value: unknown): MirroredScopeConsent | null {
  if (!isRecord(value)) return null
  if (!hasExactKeys(value, ['enabled', 'notice_version', 'consented_at_utc', 'forced_off'])) {
    return null
  }
  const enabled = value.enabled
  if (enabled !== null && typeof enabled !== 'boolean') return null
  const noticeVersion = value.notice_version
  if (noticeVersion !== null && !isSafeTelemetryVersion(noticeVersion)) return null
  const consentedAt = value.consented_at_utc
  if (consentedAt !== null && !isUtcConsentTimestamp(consentedAt)) return null
  if (typeof value.forced_off !== 'boolean') return null
  return {
    enabled,
    notice_version: noticeVersion,
    consented_at_utc: consentedAt,
    forced_off: value.forced_off,
  }
}

/** Read a closed, no-follow consent snapshot. Invalid state is indistinguishable from absence. */
export function readConsentMirror(path: string): ConsentMirror | null {
  try {
    const metadata = lstatSync(path)
    if (metadata.isSymbolicLink() || !metadata.isFile() || metadata.size > MAX_CONSENT_MIRROR_BYTES) {
      return null
    }
    const parsed: unknown = JSON.parse(readFileSync(path, 'utf8'))
    if (!isRecord(parsed)) return null
    if (!hasExactKeys(parsed, ['schema_version', 'reliability', 'growth'])) return null
    if (parsed.schema_version !== CONSENT_MIRROR_SCHEMA_VERSION) return null
    const reliability = parseScopeConsent(parsed.reliability)
    const growth = parseScopeConsent(parsed.growth)
    if (reliability === null || growth === null) return null
    return {
      schema_version: CONSENT_MIRROR_SCHEMA_VERSION,
      reliability,
      growth,
    }
  } catch {
    return null
  }
}

function isTruthy(value: string | undefined): boolean {
  return typeof value === 'string' && TRUE_VALUES.has(value.trim().toLowerCase())
}

function environmentForcesOff(scope: TelemetryScope, env: Environment): boolean {
  const scopeVariable =
    scope === 'reliability'
      ? 'OPENSQUILLA_PRIVACY_DISABLE_RELIABILITY_DIAGNOSTICS'
      : 'OPENSQUILLA_PRIVACY_DISABLE_PRODUCT_ANALYTICS'
  if (
    isTruthy(env.OPENSQUILLA_PRIVACY_DISABLE_NETWORK_OBSERVABILITY) ||
    isTruthy(env.OPENSQUILLA_TELEMETRY_DISABLED) ||
    isTruthy(env.DO_NOT_TRACK) ||
    isTruthy(env[scopeVariable])
  ) {
    return true
  }
  if (
    isTruthy(env.CI) ||
    isTruthy(env.GITHUB_ACTIONS) ||
    isTruthy(env.OPENSQUILLA_TESTING)
  ) {
    return true
  }
  return typeof env.PYTEST_CURRENT_TEST === 'string' && env.PYTEST_CURRENT_TEST.trim().length > 0
}

/** Resolve one scope without turning a runtime veto into a saved decline. */
export function resolveMirroredConsent(
  mirrorPath: string,
  scope: TelemetryScope,
  env: Environment = process.env,
): EffectiveMirroredConsent {
  const mirror = readConsentMirror(mirrorPath)
  if (mirror === null) {
    return {
      scope,
      enabled: false,
      noticeVersion: null,
      consentedAtUtc: null,
      blockReason: 'missing_or_invalid_mirror',
    }
  }
  const state = mirror[scope]
  if (environmentForcesOff(scope, env)) {
    return {
      scope,
      enabled: false,
      noticeVersion: state.notice_version,
      consentedAtUtc: state.consented_at_utc,
      blockReason: 'environment_forced_off',
    }
  }
  if (state.forced_off) {
    return {
      scope,
      enabled: false,
      noticeVersion: state.notice_version,
      consentedAtUtc: state.consented_at_utc,
      blockReason: 'forced_off',
    }
  }
  if (state.enabled === null) {
    return {
      scope,
      enabled: false,
      noticeVersion: state.notice_version,
      consentedAtUtc: state.consented_at_utc,
      blockReason: 'consent_unset',
    }
  }
  if (state.enabled === false) {
    return {
      scope,
      enabled: false,
      noticeVersion: state.notice_version,
      consentedAtUtc: state.consented_at_utc,
      blockReason: 'consent_declined',
    }
  }
  if (state.notice_version === null || state.consented_at_utc === null) {
    return {
      scope,
      enabled: false,
      noticeVersion: state.notice_version,
      consentedAtUtc: state.consented_at_utc,
      blockReason: 'consent_incomplete',
    }
  }
  if (state.notice_version !== CURRENT_NOTICE_VERSION_BY_SCOPE[scope]) {
    return {
      scope,
      enabled: false,
      noticeVersion: state.notice_version,
      consentedAtUtc: state.consented_at_utc,
      blockReason: 'notice_stale',
    }
  }
  return {
    scope,
    enabled: true,
    noticeVersion: state.notice_version,
    consentedAtUtc: state.consented_at_utc,
    blockReason: null,
  }
}
