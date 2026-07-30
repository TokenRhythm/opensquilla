import { spawnSync } from 'node:child_process'
import { createHash, createHmac, randomBytes, timingSafeEqual } from 'node:crypto'
import { existsSync, lstatSync, readFileSync, realpathSync } from 'node:fs'
import { join, normalize, resolve } from 'node:path'

export const DESKTOP_GATEWAY_OWNERSHIP_SCHEMA_VERSION = 1
export const DESKTOP_GATEWAY_OWNERSHIP_PROTOCOL = 'opensquilla-desktop-gateway-ownership-v1'

const OWNER_TOKEN_RE = /^[A-Za-z0-9_-]{32,128}$/
const PROFILE_FINGERPRINT_RE = /^[0-9a-f]{64}$/
const RECORD_MAX_BYTES = 16 * 1024
const IDENTITY_RESPONSE_KEYS = [
  'challenge',
  'pid',
  'port',
  'profile_fingerprint',
  'proof',
  'protocol',
  'schema_version',
  'start_identity',
  'version',
] as const

export interface DesktopGatewayOwnershipRecord {
  schema_version: 1
  protocol: typeof DESKTOP_GATEWAY_OWNERSHIP_PROTOCOL
  profile_fingerprint: string
  pid: number
  start_identity: string
  port: number
  version: string
  instance_nonce: string
}

export interface DesktopGatewayLaunchAuthority {
  instanceNonce: string
  profileFingerprint: string
  port: number
}

/**
 * Bind a record to the launch secret and profile, not the immediate child PID.
 * In development `uv run` is the Electron ChildProcess while the Python
 * Gateway is its descendant, so their PIDs are deliberately different.
 */
export function desktopGatewayOwnershipMatchesLaunch(
  record: DesktopGatewayOwnershipRecord,
  authority: DesktopGatewayLaunchAuthority,
): boolean {
  return record.instance_nonce === authority.instanceNonce
    && record.profile_fingerprint === authority.profileFingerprint
    && record.port === authority.port
}

export interface DesktopGatewayIdentityPayload {
  schema_version: 1
  protocol: typeof DESKTOP_GATEWAY_OWNERSHIP_PROTOCOL
  profile_fingerprint: string
  pid: number
  start_identity: string
  port: number
  version: string
  challenge: string
  proof: string
}

export interface DesktopGatewayShutdownPayload {
  action: 'shutdown'
  schema_version: 1
  protocol: typeof DESKTOP_GATEWAY_OWNERSHIP_PROTOCOL
  profile_fingerprint: string
  pid: number
  start_identity: string
  port: number
  version: string
  challenge: string
}

export type DesktopGatewayOwnershipRecordLoad =
  | { status: 'missing'; record: null }
  | { status: 'invalid'; record: null }
  | { status: 'valid'; record: DesktopGatewayOwnershipRecord }

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function validOpaqueText(value: unknown, maxLength = 256): value is string {
  return typeof value === 'string' && value.length > 0 && value.length <= maxLength
}

export function parseDesktopGatewayOwnershipRecord(
  value: unknown,
): DesktopGatewayOwnershipRecord | null {
  if (!isRecord(value)) return null
  if (value.schema_version !== DESKTOP_GATEWAY_OWNERSHIP_SCHEMA_VERSION) return null
  if (value.protocol !== DESKTOP_GATEWAY_OWNERSHIP_PROTOCOL) return null
  if (
    typeof value.profile_fingerprint !== 'string'
    || !PROFILE_FINGERPRINT_RE.test(value.profile_fingerprint)
  ) return null
  if (!Number.isSafeInteger(value.pid) || Number(value.pid) <= 0) return null
  if (!validOpaqueText(value.start_identity)) return null
  if (!Number.isInteger(value.port) || Number(value.port) < 1 || Number(value.port) > 65_535) {
    return null
  }
  if (!validOpaqueText(value.version, 128)) return null
  if (typeof value.instance_nonce !== 'string' || !OWNER_TOKEN_RE.test(value.instance_nonce)) {
    return null
  }
  return {
    schema_version: 1,
    protocol: DESKTOP_GATEWAY_OWNERSHIP_PROTOCOL,
    profile_fingerprint: value.profile_fingerprint,
    pid: Number(value.pid),
    start_identity: value.start_identity,
    port: Number(value.port),
    version: value.version,
    instance_nonce: value.instance_nonce,
  }
}

export function desktopGatewayOwnershipRecordPath(stateDir: string): string {
  return join(resolve(stateDir), 'desktop-gateway.json')
}

/** Match recovery.locking.profile_lock_key without exposing the profile path. */
export function desktopProfileFingerprint(profileHome: string): string {
  let canonical: string
  try {
    canonical = realpathSync.native(profileHome)
  } catch {
    canonical = resolve(profileHome)
  }
  canonical = normalize(canonical)
  if (process.platform === 'win32') canonical = canonical.toLowerCase()
  return createHash('sha256').update(canonical, 'utf8').digest('hex')
}

/**
 * Read, but never repair or delete, the runtime-owned record. A malformed,
 * linked, oversized, future-schema, or concurrently replaced record is
 * untrusted; startup may still let the profile lock decide whether a writer is
 * actually live, but it must never stop a process based on such a record.
 */
export function loadDesktopGatewayOwnershipRecord(
  stateDir: string,
): DesktopGatewayOwnershipRecordLoad {
  const path = desktopGatewayOwnershipRecordPath(stateDir)
  try {
    const before = lstatSync(path)
    if (!before.isFile() || before.isSymbolicLink() || before.size > RECORD_MAX_BYTES) {
      return { status: 'invalid', record: null }
    }
    const raw = readFileSync(path, 'utf8')
    const after = lstatSync(path)
    if (
      before.dev !== after.dev
      || before.ino !== after.ino
      || before.size !== after.size
      || before.mtimeMs !== after.mtimeMs
    ) return { status: 'invalid', record: null }
    const parsed = parseDesktopGatewayOwnershipRecord(JSON.parse(raw))
    return parsed
      ? { status: 'valid', record: parsed }
      : { status: 'invalid', record: null }
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') {
      return { status: 'missing', record: null }
    }
    return { status: 'invalid', record: null }
  }
}

function parseDesktopGatewayIdentityPayload(value: unknown): DesktopGatewayIdentityPayload | null {
  if (!isRecord(value)) return null
  const keys = Object.keys(value).sort()
  if (
    keys.length !== IDENTITY_RESPONSE_KEYS.length
    || keys.some((key, index) => key !== IDENTITY_RESPONSE_KEYS[index])
  ) return null
  const record = parseDesktopGatewayOwnershipRecord({
    ...value,
    instance_nonce: '________________________________',
  })
  if (!record) return null
  if (typeof value.challenge !== 'string' || !OWNER_TOKEN_RE.test(value.challenge)) return null
  if (typeof value.proof !== 'string' || !/^[0-9a-f]{64}$/.test(value.proof)) return null
  return {
    schema_version: 1,
    protocol: DESKTOP_GATEWAY_OWNERSHIP_PROTOCOL,
    profile_fingerprint: record.profile_fingerprint,
    pid: record.pid,
    start_identity: record.start_identity,
    port: record.port,
    version: record.version,
    challenge: value.challenge,
    proof: value.proof,
  }
}

function canonicalSortedAsciiJson(payload: object): string {
  // Python uses json.dumps(..., sort_keys=True, separators=(',', ':'),
  // ensure_ascii=True). Every value in this protocol is ASCII, so sorted key
  // insertion plus JSON.stringify is byte-identical.
  const sorted = Object.fromEntries(
    Object.entries(payload).sort(([left], [right]) => (
      left < right ? -1 : left > right ? 1 : 0
    )),
  )
  return JSON.stringify(sorted)
}

export function canonicalDesktopGatewayIdentityPayload(
  payload: Omit<DesktopGatewayIdentityPayload, 'proof'>,
): string {
  return canonicalSortedAsciiJson(payload)
}

export function desktopGatewayIdentityProof(
  nonce: string,
  payload: Omit<DesktopGatewayIdentityPayload, 'proof'>,
): string {
  return createHmac('sha256', nonce)
    .update(canonicalDesktopGatewayIdentityPayload(payload), 'utf8')
    .digest('hex')
}

function desktopGatewayShutdownPayload(
  record: DesktopGatewayOwnershipRecord,
  challenge: string,
): DesktopGatewayShutdownPayload {
  return {
    action: 'shutdown',
    schema_version: record.schema_version,
    protocol: record.protocol,
    profile_fingerprint: record.profile_fingerprint,
    pid: record.pid,
    start_identity: record.start_identity,
    port: record.port,
    version: record.version,
    challenge,
  }
}

export function canonicalDesktopGatewayShutdownPayload(
  record: DesktopGatewayOwnershipRecord,
  challenge: string,
): string {
  return canonicalSortedAsciiJson(desktopGatewayShutdownPayload(record, challenge))
}

export function desktopGatewayShutdownProof(
  record: DesktopGatewayOwnershipRecord,
  challenge: string,
): string {
  return createHmac('sha256', record.instance_nonce)
    .update(canonicalDesktopGatewayShutdownPayload(record, challenge), 'utf8')
    .digest('hex')
}

function safeHexEqual(left: string, right: string): boolean {
  if (!/^[0-9a-f]{64}$/.test(left) || !/^[0-9a-f]{64}$/.test(right)) return false
  return timingSafeEqual(Buffer.from(left, 'hex'), Buffer.from(right, 'hex'))
}

function identityMatchesRecord(
  identity: DesktopGatewayIdentityPayload,
  record: DesktopGatewayOwnershipRecord,
  challenge: string,
): boolean {
  if (identity.challenge !== challenge) return false
  for (const key of [
    'schema_version',
    'protocol',
    'profile_fingerprint',
    'pid',
    'start_identity',
    'port',
    'version',
  ] as const) {
    if (identity[key] !== record[key]) return false
  }
  const { proof, ...unsigned } = identity
  return safeHexEqual(proof, desktopGatewayIdentityProof(record.instance_nonce, unsigned))
}

export interface DesktopGatewayIdentityVerificationOptions {
  fetchImpl?: typeof fetch
  timeoutMs?: number
  challenge?: string
}

/**
 * Prove that the loopback listener is the exact Desktop-owned process named by
 * the record. A 200 health check, PID match, or record alone is never enough.
 */
export async function verifyDesktopGatewayOwnership(
  record: DesktopGatewayOwnershipRecord,
  options: DesktopGatewayIdentityVerificationOptions = {},
): Promise<boolean> {
  const challenge = options.challenge ?? randomBytes(32).toString('base64url')
  if (!OWNER_TOKEN_RE.test(challenge)) return false
  try {
    const response = await (options.fetchImpl ?? fetch)(
      `http://127.0.0.1:${record.port}/api/desktop/identity`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ challenge }),
        signal: AbortSignal.timeout(options.timeoutMs ?? 1500),
      },
    )
    if (!response.ok) return false
    const identity = parseDesktopGatewayIdentityPayload(await response.json().catch(() => null))
    return Boolean(identity && identityMatchesRecord(identity, record, challenge))
  } catch {
    return false
  }
}

/**
 * Ask only the already-verified Desktop instance to drain. The request carries
 * an instance-bound HMAC so a listener that replaces the port between identity
 * verification and shutdown cannot be stopped accidentally.
 */
export async function requestVerifiedDesktopGatewayShutdown(
  record: DesktopGatewayOwnershipRecord,
  options: DesktopGatewayIdentityVerificationOptions = {},
): Promise<boolean> {
  const challenge = options.challenge ?? randomBytes(32).toString('base64url')
  if (!OWNER_TOKEN_RE.test(challenge)) return false
  try {
    const response = await (options.fetchImpl ?? fetch)(
      `http://127.0.0.1:${record.port}/api/desktop/shutdown`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          challenge,
          proof: desktopGatewayShutdownProof(record, challenge),
        }),
        signal: AbortSignal.timeout(options.timeoutMs ?? 2000),
      },
    )
    return response.status === 202
  } catch {
    return false
  }
}

export function createDesktopGatewayInstanceNonce(): string {
  return randomBytes(32).toString('base64url')
}

export interface DesktopGatewayOwnershipReleaseWaitOptions {
  timeoutMs?: number
  pollIntervalMs?: number
}

/**
 * The Python owner removes this record only after the profile/legacy writer
 * leases are released. Therefore a missing record is the cross-process handoff
 * barrier; a different valid owner is never treated as ours to replace.
 */
export async function waitForDesktopGatewayOwnershipRelease(
  stateDir: string,
  record: DesktopGatewayOwnershipRecord,
  options: DesktopGatewayOwnershipReleaseWaitOptions = {},
): Promise<boolean> {
  const timeoutMs = options.timeoutMs ?? 80_000
  const pollIntervalMs = options.pollIntervalMs ?? 200
  const deadline = Date.now() + timeoutMs
  do {
    const current = loadDesktopGatewayOwnershipRecord(stateDir)
    if (current.status === 'missing') return true
    if (
      current.status === 'valid'
      && !sameDesktopGatewayOwnershipInstance(current.record, record)
    ) return false
    if (Date.now() >= deadline) break
    await new Promise((resolveWait) => setTimeout(resolveWait, pollIntervalMs))
  } while (true)
  return false
}

export function sameDesktopGatewayOwnershipInstance(
  left: DesktopGatewayOwnershipRecord,
  right: DesktopGatewayOwnershipRecord,
): boolean {
  return (
    left.protocol === right.protocol
    && left.profile_fingerprint === right.profile_fingerprint
    && left.pid === right.pid
    && left.start_identity === right.start_identity
    && left.port === right.port
    && left.instance_nonce === right.instance_nonce
  )
}

// The Gateway records its own process-start identity precisely so a recycled
// PID can be told apart from the live owner. These probes mirror the Gateway's
// per-platform identity formats; the strings must stay byte-identical to what
// the Python runtime writes into the ownership record.
const PROCESS_START_IDENTITY_SCHEMES = [
  'linux-proc-start-ticks:',
  'windows-creation-filetime:',
  'posix-ps-lstart:',
] as const

/** Extract the start-ticks identity from a /proc/<pid>/stat line. */
export function linuxProcStatStartIdentity(statText: string): string | null {
  const closeParen = String(statText ?? '').lastIndexOf(')')
  if (closeParen < 0) return null
  // ``comm`` is parenthesized and may contain spaces or ``)``. Fields after
  // its final close-paren begin at field 3; starttime is field 22.
  const fields = statText.slice(closeParen + 1).trim().split(/\s+/)
  const startTicks = fields[19]
  if (!startTicks || !/^\d+$/.test(startTicks)) return null
  return `linux-proc-start-ticks:${startTicks}`
}

/** Normalize ``ps -o lstart=`` output the same way the Gateway does. */
export function posixPsLstartIdentity(stdout: string): string | null {
  const value = String(stdout ?? '').split(/\s+/).filter(Boolean).join(' ')
  if (!value) return null
  return `posix-ps-lstart:${value}`
}

function windowsProcessStartIdentity(pid: number): string | null {
  // GetProcessTimes creation time, via the .NET filetime round-trip. The
  // query needs only limited-information access; a denied or missing process
  // yields null and the caller stays on the conservative path.
  const result = spawnSync(
    'powershell.exe',
    [
      '-NoProfile',
      '-NonInteractive',
      '-Command',
      `$ErrorActionPreference='Stop'; (Get-Process -Id ${Math.trunc(pid)}).StartTime.ToFileTime()`,
    ],
    { windowsHide: true, timeout: 2000, encoding: 'utf8' },
  )
  if (result.error || result.status !== 0) return null
  const value = String(result.stdout ?? '').trim()
  if (!/^\d+$/.test(value)) return null
  return `windows-creation-filetime:${value}`
}

function posixProcessStartIdentity(pid: number): string | null {
  const command = existsSync('/bin/ps') ? '/bin/ps' : 'ps'
  const result = spawnSync(command, ['-o', 'lstart=', '-p', String(Math.trunc(pid))], {
    timeout: 2000,
    encoding: 'utf8',
    env: { ...process.env, LC_ALL: 'C', LANG: 'C' },
  })
  if (result.error || result.status !== 0) return null
  return posixPsLstartIdentity(result.stdout ?? '')
}

/**
 * Best-effort start identity of the live process occupying ``pid``, in the
 * same format the Gateway records about itself. Returns null when the
 * platform cannot answer; callers must fail open on null.
 */
export function desktopProcessStartIdentity(pid: number): string | null {
  if (!Number.isSafeInteger(pid) || pid <= 0) return null
  try {
    if (process.platform === 'linux') {
      return linuxProcStatStartIdentity(readFileSync(`/proc/${pid}/stat`, 'utf8'))
    }
    if (process.platform === 'win32') return windowsProcessStartIdentity(pid)
    return posixProcessStartIdentity(pid)
  } catch {
    return null
  }
}

/**
 * True only when the live process at the recorded PID provably started at a
 * different time than the recorded owner — i.e. the OS recycled the PID and
 * the record is stale. A null or cross-scheme identity (including the
 * Gateway's opaque ``runtime-start:`` fallback) never conflicts; this check
 * may only shortcut waiting, never grant authority over a process.
 */
export function desktopGatewayStartIdentityConflict(
  recordedStartIdentity: string,
  liveStartIdentity: string | null,
): boolean {
  if (!liveStartIdentity) return false
  const scheme = PROCESS_START_IDENTITY_SCHEMES.find(
    (prefix) => liveStartIdentity.startsWith(prefix),
  )
  if (!scheme || !recordedStartIdentity.startsWith(scheme)) return false
  return recordedStartIdentity !== liveStartIdentity
}
