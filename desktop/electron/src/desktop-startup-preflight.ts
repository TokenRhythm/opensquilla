import {
  accessSync, closeSync, constants, fstatSync, lstatSync, openSync, opendirSync,
  readSync, statSync, type Stats,
} from 'node:fs'
import { homedir } from 'node:os'
import { basename, dirname, isAbsolute, join, resolve } from 'node:path'
import { parse } from 'smol-toml'
import { isRecoveryProfileId } from './desktop-profile-context.js'
import { mayNeedPythonTomlParser } from './desktop-startup-toml.js'

// This is an eligibility check for omitting maintenance subprocesses, not a
// RecoveryReport or a write authorization. The Gateway still checks the profile
// under its lifetime writer lock, and explicit settings retain full inspect/CAS.
export type DesktopStartupPreflight =
  | { canSkipMaintenance: true; stateDirectory: string; workspaceDirectory: string }
  | { canSkipMaintenance: false; reason: string }

const CONFIG_MAX_BYTES = 1024 * 1024
const METADATA_MAX_BYTES = 128 * 1024
const MAX_CONSOLIDATION_RECEIPTS = 32
const CONSOLIDATION_MAX_BYTES = 512 * 1024
type MetadataSnapshots = Map<string, Stats | null>
// Keep this conservative bound aligned with opensquilla/config_version.py.
const SUPPORTED_CONFIG_VERSION = 2
const ROOT_ENVIRONMENT_KEYS = [
  'OPENSQUILLA_GATEWAY_STATE_DIR',
  'OPENSQUILLA_GATEWAY_WORKSPACE_DIR',
  'OPENSQUILLA_WORKSPACE_DIR',
] as const
const LEGACY_STATE_ENTRIES = [
  'workspace', 'skills', 'skills-taps.json', 'skills-lock.json',
  'session-archive', 'router', '.env', 'state',
] as const

function optionalStat(path: string): Stats | null {
  try { return lstatSync(path) } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') return null
    throw error
  }
}

function sameFile(left: Stats, right: Stats): boolean {
  return left.dev === right.dev && left.ino === right.ino && left.mode === right.mode && left.nlink === right.nlink
    && left.size === right.size && left.mtimeMs === right.mtimeMs && left.ctimeMs === right.ctimeMs
}

function readMetadata(path: string, maximumBytes: number, snapshots?: MetadataSnapshots): string {
  const before = lstatSync(path)
  if (!before.isFile() || before.isSymbolicLink() || before.nlink !== 1 || before.size > maximumBytes) {
    throw new Error('metadata_shape')
  }
  const handle = openSync(path, constants.O_RDONLY | (constants.O_NOFOLLOW || 0))
  try {
    if (!sameFile(before, fstatSync(handle))) throw new Error('metadata_changed')
    // Bound both allocation and I/O even if the file grows after fstat.
    const buffer = Buffer.allocUnsafe(before.size + 1)
    let bytesRead = 0
    while (bytesRead < buffer.length) {
      const count = readSync(handle, buffer, bytesRead, buffer.length - bytesRead, null)
      if (count === 0) break
      bytesRead += count
    }
    const data = buffer.subarray(0, bytesRead)
    const after = lstatSync(path)
    if (bytesRead !== before.size || !sameFile(before, after) || !sameFile(before, fstatSync(handle))) {
      throw new Error('metadata_changed')
    }
    snapshots?.set(path, after)
    if (data[0] === 0xef && data[1] === 0xbb && data[2] === 0xbf) throw new Error('metadata_encoding')
    return new TextDecoder('utf-8', { fatal: true }).decode(data)
  } finally { closeSync(handle) }
}

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : null
}

function hasDateValue(value: unknown): boolean {
  if (value instanceof Date) return true
  if (Array.isArray(value)) return value.some(hasDateValue)
  const table = record(value)
  return table !== null && Object.values(table).some(hasDateValue)
}

function emptyOrMissingDirectory(path: string, snapshots?: MetadataSnapshots): boolean {
  const info = optionalStat(path)
  snapshots?.set(path, info)
  if (!info) return true
  if (!info.isDirectory() || info.isSymbolicLink()) return false
  const directory = opendirSync(path)
  try { return directory.readSync() === null } finally { directory.closeSync() }
}

function completedConsolidationBackups(userData: string, home: string, snapshots: MetadataSnapshots): boolean {
  const backups = join(userData, 'backups')
  const root = join(backups, 'profile-consolidation')
  for (const path of [backups, root]) {
    const info = optionalStat(path)
    snapshots.set(path, info)
    if (!info) return true
    if (!info.isDirectory() || info.isSymbolicLink()) return false
  }
  const directory = opendirSync(root)
  let count = 0
  let remainingBytes = CONSOLIDATION_MAX_BYTES
  try {
    for (let entry = directory.readSync(); entry; entry = directory.readSync()) {
      if (++count > MAX_CONSOLIDATION_RECEIPTS || !isRecoveryProfileId(entry.name)) return false
      const transaction = join(root, entry.name)
      const info = lstatSync(transaction)
      if (!info.isDirectory() || info.isSymbolicLink()) return false
      snapshots.set(transaction, info)
      const receiptPath = join(transaction, 'receipt.json')
      const raw = readMetadata(receiptPath, Math.min(METADATA_MAX_BYTES, remainingBytes), snapshots)
      remainingBytes -= Buffer.byteLength(raw, 'utf8')
      const receipt = record(JSON.parse(raw))
      // Require every receipt to be terminal, so Python's latest-receipt choice
      // cannot reveal a pending adoption. No timestamp ordering or cache needed.
      if (!receipt || receipt.schema_version !== 1 || receipt.outcome !== 'consolidated'
        || receipt.stable_code !== 'profile_consolidation_complete' || receipt.transaction_id !== entry.name
        || receipt.primary_home !== home || receipt.backup_path !== transaction || receipt.receipt_path !== receiptPath
        || !Number.isSafeInteger(receipt.revision) || (receipt.revision as number) < 0
        || !Array.isArray(receipt.errors) || receipt.errors.length !== 0
        || !Array.isArray(receipt.consumed_recovery_ids) || !receipt.consumed_recovery_ids.every(isRecoveryProfileId)) {
        return false
      }
      const source = receipt.configuration_source_recovery_id
      if (source !== null && (typeof source !== 'string' || !receipt.consumed_recovery_ids.includes(source))) return false
      const credentialPath = receipt.configuration_source_credential_path
      const digest = receipt.configuration_source_credential_sha256
      const size = receipt.configuration_source_credential_size
      if (receipt.credential_adoption_status === 'not_required') {
        if (credentialPath !== null || digest !== null || size !== null) return false
      } else if (receipt.credential_adoption_status === 'complete') {
        if (typeof source !== 'string'
          || credentialPath !== join(transaction, 'recovery-profiles', source, 'desktop-credential.json')
          || typeof digest !== 'string' || !/^[0-9a-f]{64}$/.test(digest)
          || !Number.isSafeInteger(size) || (size as number) < 0) return false
      } else return false
      // Completed archives need not retain their original credential or DB.
      // The existing Python validator likewise only reads pending credentials.
    }
    return true
  } finally { directory.closeSync() }
}

/** Resolve TOML data roots using the profile home, including the current-user ~. */
export function configuredDesktopDataDirectory(home: string, value: unknown, fallback: string): string | null {
  // Python str.strip and JavaScript trim disagree on these characters. Defer
  // their path semantics rather than silently choosing a different state root.
  if (typeof value === 'string' && /[\u001c-\u001f\u0085\ufeff]/.test(value)) return null
  if (value === undefined || (typeof value === 'string' && !value.trim())) return join(home, fallback)
  if (typeof value !== 'string' || value.includes('\0')) return null
  // Check before ~ expansion: node:path.join would already fold these segments.
  if (process.platform !== 'win32' && (value.split('/').includes('..') || value.startsWith('//'))) return null
  let path = value
  if (path === '~') path = homedir()
  else if (path.startsWith('~/') || (process.platform === 'win32' && path.startsWith('~\\'))) {
    path = join(homedir(), path.slice(2))
  } else if (path.startsWith('~')) return null // Named-user expansion belongs to Python.
  // Drive-relative and rooted-without-drive paths depend on process drive state.
  if (process.platform === 'win32' && !isAbsolute(path) && /^[A-Za-z]:/.test(path)) return null
  if (process.platform === 'win32' && /^[\\/](?![\\/])/.test(path)) return null
  return resolve(home, path)
}

function usableDirectory(path: string, explicit: boolean, writable: boolean): boolean {
  const own = lstatSync(path)
  if (own.isSymbolicLink() && !explicit) return false
  if (!(own.isSymbolicLink() ? statSync(path) : own).isDirectory()) return false
  accessSync(path, constants.R_OK | constants.X_OK | (writable ? constants.W_OK : 0))
  return true
}

export function inspectDesktopStartupMetadata(
  userData: string,
  home: string,
  environment: NodeJS.ProcessEnv = process.env,
): DesktopStartupPreflight {
  const fallback = (reason: string): DesktopStartupPreflight => ({ canSkipMaintenance: false, reason })
  try {
    if (resolve(home) !== resolve(userData, 'opensquilla')) return fallback('non_primary_home')
    // Python recognizes this reserved identity before any caller-supplied kind.
    // A custom userData nested here must retain recovery-specific boundaries.
    if (basename(dirname(userData)).toLowerCase() === 'recovery-profiles') return fallback('recovery_profile_identity')
    const parentBefore = lstatSync(userData)
    if (!parentBefore.isDirectory() || parentBefore.isSymbolicLink()) return fallback('user_data_shape')
    const homeBefore = lstatSync(home)
    if (!homeBefore.isDirectory() || homeBefore.isSymbolicLink()) return fallback('home_shape')
    if (ROOT_ENVIRONMENT_KEYS.some((name) => Boolean(environment[name]))) return fallback('root_environment')
    const snapshots: MetadataSnapshots = new Map()

    const parent = dirname(home)
    const name = basename(home)
    const markers = [
      ...['profile-replace', 'import-commit', 'profile-cleanup', 'desktop-settings-transaction', 'workspace-patch']
        .map((suffix) => join(parent, `.${name}.${suffix}.json`)),
      join(userData, '.opensquilla-profile-consolidation.json'),
      join(userData, '.opensquilla-profile-consolidation-deferred'),
      join(userData, 'migration-provider-setup.json'),
    ]
    if (markers.some((path) => optionalStat(path) !== null)) return fallback('pending_or_environment_metadata')
    const dotenvPath = join(home, '.env')
    const dotenvBefore = optionalStat(dotenvPath)
    if (dotenvBefore) {
      // Do not parse dotenv or interpret its values. Any mention of a data-root
      // variable, including comments/quoted text, retains Python's root handling.
      const dotenv = readMetadata(dotenvPath, METADATA_MAX_BYTES).toUpperCase()
      if (ROOT_ENVIRONMENT_KEYS.some((key) => dotenv.includes(key))) return fallback('root_dotenv')
    }
    // Active sources still require consolidation; completed archives only need
    // bounded receipt metadata, never a recursive walk or database inspection.
    if (!emptyOrMissingDirectory(join(userData, 'recovery-profiles'), snapshots)
      || !completedConsolidationBackups(userData, home, snapshots)) {
      return fallback('historical_profiles')
    }
    const contextPath = join(userData, 'desktop-profile-context.json')
    if (optionalStat(contextPath)) {
      const context = record(JSON.parse(readMetadata(contextPath, METADATA_MAX_BYTES)))
      if (!context || context.schema_version !== 1 || context.active_profile_kind !== 'primary'
        || context.active_recovery_id !== null || typeof context.updated_at !== 'string') {
        return fallback('historical_profile_selection')
      }
    }
    const marker = record(JSON.parse(readMetadata(join(home, 'desktop-layout-v2.json'), METADATA_MAX_BYTES)))
    if (!marker || marker.schema_version !== 2 || typeof marker.migratedAt !== 'string' || !Array.isArray(marker.moved)) {
      return fallback('layout_marker')
    }
    const configPath = join(home, 'config.toml')
    const configBefore = lstatSync(configPath)
    // Preserve TOML integer/float identity: config_version = 2.0 is invalid in
    // Python even though a normal JavaScript number parser yields integer 2.
    const configRaw = readMetadata(configPath, CONFIG_MAX_BYTES)
    if (mayNeedPythonTomlParser(configRaw)) return fallback('python_toml_syntax')
    const config = parse(configRaw, { integersAsBigInt: true })
    // The library's Date compatibility mode accepts some invalid calendar
    // dates. Let Python decide every datetime instead of duplicating validation.
    if (hasDateValue(config)) return fallback('python_toml_datetime')
    const configVersion = config.config_version ?? 0n
    if (typeof configVersion !== 'bigint'
      || configVersion < 0n || configVersion > BigInt(SUPPORTED_CONFIG_VERSION)) return fallback('config_version')
    const stateDirectory = configuredDesktopDataDirectory(home, config.state_dir, 'state')
    const workspaceDirectory = configuredDesktopDataDirectory(home, config.workspace_dir, 'workspace')
    if (!stateDirectory || !workspaceDirectory) return fallback('data_root')
    if (!usableDirectory(stateDirectory, typeof config.state_dir === 'string' && Boolean(config.state_dir.trim()), true)
      || !usableDirectory(workspaceDirectory, typeof config.workspace_dir === 'string' && Boolean(config.workspace_dir.trim()), false)) {
      return fallback('data_root_shape')
    }
    if (LEGACY_STATE_ENTRIES.some((entry) => optionalStat(join(home, 'state', entry)) !== null)) {
      return fallback('legacy_layout')
    }
    // Stat only: schema checks, opening SQLite, backups and quick_check do not
    // belong in Electron's healthy launch path.
    for (const name of ['sessions.db', 'scheduler.db']) {
      const mainExists = optionalStat(join(stateDirectory, name)) !== null
      for (const suffix of ['', '-wal', '-shm', '-journal']) {
        const info = optionalStat(join(stateDirectory, `${name}${suffix}`))
        if (info && (!info.isFile() || info.isSymbolicLink() || info.nlink !== 1
          || (!mainExists && suffix !== ''))) return fallback('database_shape')
      }
    }
    const dotenvAfter = optionalStat(dotenvPath)
    if ((dotenvBefore ? !dotenvAfter || !sameFile(dotenvBefore, dotenvAfter) : dotenvAfter !== null)
      || !sameFile(parentBefore, lstatSync(userData)) || !sameFile(homeBefore, lstatSync(home))
      || !sameFile(configBefore, lstatSync(configPath))
      || [...snapshots].some(([path, before]) => {
        const after = optionalStat(path)
        return before ? !after || !sameFile(before, after) : after !== null
      })) {
      return fallback('metadata_changed')
    }
    return { canSkipMaintenance: true, stateDirectory, workspaceDirectory }
  } catch {
    // Malformed TOML/JSON, unsupported metadata, permissions and concurrent
    // changes all take the existing recovery path; never initialize here.
    return fallback('metadata_unavailable')
  }
}
