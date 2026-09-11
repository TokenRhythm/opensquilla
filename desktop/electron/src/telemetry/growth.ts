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
  renameSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs'
import { join } from 'node:path'

import { resolveMirroredConsent } from './consent-mirror.js'
import {
  CURRENT_NOTICE_VERSION_BY_SCOPE,
  validateDesktopEarlyTelemetryEvent,
  type DesktopEarlyTelemetryEvent,
  type DesktopPlatform,
  type FirstAppReadyEvent,
  type OnboardingCompletedEvent,
} from './contracts.js'
import {
  DesktopTelemetryRuntimeGate,
  spoolEarlyTelemetryEvent,
} from './early-spool.js'

export const GROWTH_COHORT_STATE_NAME = 'growth_cohort.json'
export const GROWTH_IDENTITY_STATE_NAME = 'growth_identity.json'
export const DESKTOP_GROWTH_MILESTONE_STATE_NAME = 'growth_desktop_milestones.json'
export const GATEWAY_GROWTH_MILESTONE_STATE_NAME = 'growth_gateway_milestones.json'

const MAX_STATE_BYTES = 16 * 1024
const UUID4_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
const UTC_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$/

type Environment = Readonly<Record<string, string | undefined>>
type MilestoneName = 'onboarding_result' | 'first_app_ready'
type MilestoneStatus = 'pending' | 'enqueued'

interface DesktopGrowthPaths {
  profileKey: string
  telemetryDirectory: string
  spoolRoot: string
  consentMirrorPath: string
}

interface GrowthIdentity {
  schema_version: 1
  kind: 'analytics_user_id'
  value: string
  created_at_utc: string
}

interface ActiveGrowthCohort {
  schema_version: 1
  state: 'active'
  activated_at_utc: string
}

interface MilestoneRecord {
  status: MilestoneStatus
  event: OnboardingCompletedEvent | FirstAppReadyEvent
}

interface DesktopGrowthMilestoneState {
  schema_version: 1
  marker_kind: 'growth_desktop_milestones'
  onboarding_result: MilestoneRecord | null
  first_app_ready: MilestoneRecord | null
}

type ReadState<T> =
  | { status: 'absent' }
  | { status: 'invalid' }
  | { status: 'valid'; value: T }

export interface DesktopGrowthTelemetryOptions {
  runtimeGate: DesktopTelemetryRuntimeGate
  appVersion: () => string
  platform: DesktopPlatform
  env?: Environment
  nowDate?: () => Date
  randomId?: () => string
}

export interface GrowthProfileInspection {
  profileKey: string
  stableCode: string
  importedOrMigrated?: boolean
}

/**
 * Electron-owned new-user eligibility and first desktop milestones.
 *
 * Missing files never prove freshness. Only the recovery engine's exact
 * `fresh_profile` result can arm this process, and activation occurs only
 * after the current Growth-consent mirror is effective.
 */
export class DesktopGrowthTelemetry {
  private readonly runtimeGate: DesktopTelemetryRuntimeGate
  private readonly appVersion: () => string
  private readonly platform: DesktopPlatform
  private readonly env: Environment
  private readonly nowDate: () => Date
  private readonly randomId: () => string
  private inspectedProfileKey: string | null = null
  private freshCandidate = false
  private paths: DesktopGrowthPaths | null = null
  private identity: GrowthIdentity | null = null

  constructor(options: DesktopGrowthTelemetryOptions) {
    this.runtimeGate = options.runtimeGate
    this.appVersion = options.appVersion
    this.platform = options.platform
    this.env = options.env ?? process.env
    this.nowDate = options.nowDate ?? (() => new Date())
    this.randomId = options.randomId ?? randomUUID
  }

  observeProfileInspection(inspection: GrowthProfileInspection): void {
    this.inspectedProfileKey = inspection.profileKey
    this.freshCandidate = inspection.stableCode === 'fresh_profile'
      && inspection.importedOrMigrated !== true
    if (this.paths?.profileKey !== inspection.profileKey) {
      this.paths = null
      this.identity = null
    }
  }

  synchronize(paths: DesktopGrowthPaths): void {
    this.paths = paths
    this.identity = null
    const consent = resolveMirroredConsent(paths.consentMirrorPath, 'growth', this.env)
    if (!consent.enabled) return

    const cohortPath = join(paths.telemetryDirectory, GROWTH_COHORT_STATE_NAME)
    const identityPath = join(paths.telemetryDirectory, GROWTH_IDENTITY_STATE_NAME)
    const cohort = readCohort(cohortPath)
    const identity = readIdentity(identityPath)

    if (cohort.status === 'invalid' || identity.status === 'invalid') return
    if (cohort.status === 'valid') {
      this.identity = identity.status === 'valid'
        ? identity.value
        : createIdentity(identityPath, this.nowDate, this.randomId)
      if (this.identity !== null) this.retryPendingMilestones()
      this.freshCandidate = false
      return
    }

    const canActivate = this.freshCandidate
      && this.inspectedProfileKey === paths.profileKey
      && identity.status === 'absent'
    if (!canActivate) return

    // Cohort first: if the process dies before identity creation, this
    // consented receipt safely authorizes recovery on the next launch.
    const activatedAt = canonicalNow(this.nowDate)
    if (activatedAt === null) return
    if (!writeStateAtomic(cohortPath, {
      schema_version: 1,
      state: 'active',
      activated_at_utc: activatedAt,
    } satisfies ActiveGrowthCohort)) return
    this.identity = createIdentity(identityPath, this.nowDate, this.randomId)
    this.freshCandidate = false
    if (this.identity !== null) this.retryPendingMilestones()
  }

  recordOnboardingCompleted(): void {
    this.recordMilestone('onboarding_result')
    // Finishing onboarding without an active cohort permanently closes the
    // in-memory candidate; a later settings opt-in must not backfill it.
    this.freshCandidate = false
  }

  recordFirstAppReady(): void {
    this.recordMilestone('first_app_ready')
    this.freshCandidate = false
  }

  private retryPendingMilestones(): void {
    const paths = this.paths
    if (paths === null || this.identity === null) return
    const marker = readMilestoneState(
      join(paths.telemetryDirectory, DESKTOP_GROWTH_MILESTONE_STATE_NAME),
    )
    if (marker.status !== 'valid') return
    for (const name of ['onboarding_result', 'first_app_ready'] as const) {
      const record = marker.value[name]
      if (record?.status === 'pending') this.recordMilestone(name)
    }
  }

  private recordMilestone(name: MilestoneName): void {
    const paths = this.paths
    const identity = this.identity
    if (paths === null || identity === null || !this.runtimeGate.isOpen()) return
    const consent = resolveMirroredConsent(paths.consentMirrorPath, 'growth', this.env)
    if (!consent.enabled || consent.noticeVersion !== CURRENT_NOTICE_VERSION_BY_SCOPE.growth) {
      return
    }

    const markerPath = join(paths.telemetryDirectory, DESKTOP_GROWTH_MILESTONE_STATE_NAME)
    const loaded = readMilestoneState(markerPath)
    if (loaded.status === 'invalid') return
    const state = loaded.status === 'valid' ? loaded.value : emptyMilestoneState()
    const existing = state[name]
    if (existing?.status === 'enqueued') return
    if (
      existing !== null
      && existing.event.analytics_user_id !== identity.value
    ) return

    const event = existing?.event ?? this.buildEvent(name, identity.value)
    if (event === null) return
    if (existing === null) {
      state[name] = { status: 'pending', event }
      if (!writeStateAtomic(markerPath, state)) return
    }

    const result = spoolEarlyTelemetryEvent({
      spoolRoot: paths.spoolRoot,
      consentMirrorPath: paths.consentMirrorPath,
      event,
      runtimeGate: this.runtimeGate,
      env: this.env,
      now: this.nowDate(),
    })
    if (result.status !== 'written' && result.status !== 'duplicate') return

    const current = readMilestoneState(markerPath)
    if (current.status !== 'valid') return
    const currentRecord = current.value[name]
    if (currentRecord?.event.event_id !== event.event_id) return
    current.value[name] = { status: 'enqueued', event }
    writeStateAtomic(markerPath, current.value)
  }

  private buildEvent(
    name: MilestoneName,
    analyticsUserId: string,
  ): OnboardingCompletedEvent | FirstAppReadyEvent | null {
    const occurredAt = canonicalNow(this.nowDate)
    const eventId = this.randomId()
    if (occurredAt === null || !UUID4_RE.test(eventId)) return null
    const common = {
      event_version: 1 as const,
      event_id: eventId,
      occurred_at_utc: occurredAt,
      source: 'desktop' as const,
      app_version: this.appVersion(),
      platform: this.platform,
      error_code: null,
      duration_ms: null,
      consent_scope: 'growth' as const,
      notice_version: CURRENT_NOTICE_VERSION_BY_SCOPE.growth,
      sample_rate: 1 as const,
      analytics_user_id: analyticsUserId,
    }
    try {
      return validateDesktopEarlyTelemetryEvent(
        name === 'onboarding_result'
          ? {
              ...common,
              event_name: name,
              outcome: 'completed',
              flow_version: 1,
            }
          : {
              ...common,
              event_name: name,
              outcome: null,
            },
      ) as OnboardingCompletedEvent | FirstAppReadyEvent
    } catch {
      return null
    }
  }
}

export function clearDesktopGrowthTelemetryState(telemetryDirectory: string): void {
  for (const name of [
    GROWTH_IDENTITY_STATE_NAME,
    GROWTH_COHORT_STATE_NAME,
    DESKTOP_GROWTH_MILESTONE_STATE_NAME,
    GATEWAY_GROWTH_MILESTONE_STATE_NAME,
  ]) {
    const path = join(telemetryDirectory, name)
    if (!existsSync(path)) continue
    const metadata = lstatSync(path)
    if (metadata.isSymbolicLink() || !metadata.isFile()) {
      throw new Error('Growth telemetry state is not a regular file.')
    }
    unlinkSync(path)
  }
  syncDirectoryBestEffort(telemetryDirectory)
}

function emptyMilestoneState(): DesktopGrowthMilestoneState {
  return {
    schema_version: 1,
    marker_kind: 'growth_desktop_milestones',
    onboarding_result: null,
    first_app_ready: null,
  }
}

function readCohort(path: string): ReadState<ActiveGrowthCohort> {
  const loaded = readJsonObject(path)
  if (loaded.status !== 'valid') return loaded
  const value = loaded.value
  if (
    !hasExactKeys(value, ['schema_version', 'state', 'activated_at_utc'])
    || value.schema_version !== 1
    || value.state !== 'active'
    || !isUtcTimestamp(value.activated_at_utc)
  ) return { status: 'invalid' }
  return { status: 'valid', value: value as unknown as ActiveGrowthCohort }
}

function readIdentity(path: string): ReadState<GrowthIdentity> {
  const loaded = readJsonObject(path)
  if (loaded.status !== 'valid') return loaded
  const value = loaded.value
  if (
    !hasExactKeys(value, ['schema_version', 'kind', 'value', 'created_at_utc'])
    || value.schema_version !== 1
    || value.kind !== 'analytics_user_id'
    || typeof value.value !== 'string'
    || !UUID4_RE.test(value.value)
    || !isUtcTimestamp(value.created_at_utc)
  ) return { status: 'invalid' }
  return { status: 'valid', value: value as unknown as GrowthIdentity }
}

function createIdentity(
  path: string,
  nowDate: () => Date,
  randomId: () => string,
): GrowthIdentity | null {
  const existing = readIdentity(path)
  if (existing.status === 'valid') return existing.value
  if (existing.status === 'invalid') return null
  const value = randomId()
  const createdAt = canonicalNow(nowDate)
  if (!UUID4_RE.test(value) || createdAt === null) return null
  const identity: GrowthIdentity = {
    schema_version: 1,
    kind: 'analytics_user_id',
    value,
    created_at_utc: createdAt,
  }
  return writeStateAtomic(path, identity) ? identity : null
}

function readMilestoneState(path: string): ReadState<DesktopGrowthMilestoneState> {
  const loaded = readJsonObject(path)
  if (loaded.status !== 'valid') return loaded
  const value = loaded.value
  if (
    !hasExactKeys(value, [
      'schema_version',
      'marker_kind',
      'onboarding_result',
      'first_app_ready',
    ])
    || value.schema_version !== 1
    || value.marker_kind !== 'growth_desktop_milestones'
  ) return { status: 'invalid' }
  const onboarding = parseMilestoneRecord('onboarding_result', value.onboarding_result)
  const appReady = parseMilestoneRecord('first_app_ready', value.first_app_ready)
  if (onboarding === undefined || appReady === undefined) return { status: 'invalid' }
  return {
    status: 'valid',
    value: {
      schema_version: 1,
      marker_kind: 'growth_desktop_milestones',
      onboarding_result: onboarding,
      first_app_ready: appReady,
    },
  }
}

function parseMilestoneRecord(
  expectedName: MilestoneName,
  value: unknown,
): MilestoneRecord | null | undefined {
  if (value === null) return null
  if (!isRecord(value) || !hasExactKeys(value, ['status', 'event'])) return undefined
  if (value.status !== 'pending' && value.status !== 'enqueued') return undefined
  try {
    const event = validateDesktopEarlyTelemetryEvent(value.event)
    if (event.event_name !== expectedName) return undefined
    return {
      status: value.status,
      event: event as OnboardingCompletedEvent | FirstAppReadyEvent,
    }
  } catch {
    return undefined
  }
}

function readJsonObject(path: string): ReadState<Record<string, unknown>> {
  try {
    const metadata = lstatSync(path)
    if (metadata.isSymbolicLink() || !metadata.isFile() || metadata.size > MAX_STATE_BYTES) {
      return { status: 'invalid' }
    }
    const value: unknown = JSON.parse(readFileSync(path, 'utf8'))
    return isRecord(value) ? { status: 'valid', value } : { status: 'invalid' }
  } catch (error) {
    return (error as NodeJS.ErrnoException).code === 'ENOENT'
      ? { status: 'absent' }
      : { status: 'invalid' }
  }
}

function writeStateAtomic(path: string, value: object): boolean {
  const payload = Buffer.from(`${JSON.stringify(value)}\n`, 'utf8')
  if (payload.byteLength > MAX_STATE_BYTES) return false
  const directory = join(path, '..')
  let descriptor: number | null = null
  const temporary = `${path}.${randomUUID()}.tmp`
  try {
    mkdirSync(directory, { recursive: true, mode: 0o700 })
    const directoryMetadata = lstatSync(directory)
    if (directoryMetadata.isSymbolicLink() || !directoryMetadata.isDirectory()) return false
    bestEffortChmod(directory, 0o700)
    if (existsSync(path)) {
      const targetMetadata = lstatSync(path)
      if (targetMetadata.isSymbolicLink() || !targetMetadata.isFile()) return false
    }
    descriptor = openSync(
      temporary,
      constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY,
      0o600,
    )
    writeFileSync(descriptor, payload)
    fsyncSync(descriptor)
    closeSync(descriptor)
    descriptor = null
    renameSync(temporary, path)
    bestEffortChmod(path, 0o600)
    syncDirectoryBestEffort(directory)
    return true
  } catch {
    if (descriptor !== null) {
      try { closeSync(descriptor) } catch { /* best effort */ }
    }
    try { unlinkSync(temporary) } catch { /* best effort */ }
    return false
  }
}

function canonicalNow(nowDate: () => Date): string | null {
  try {
    const value = nowDate()
    return Number.isFinite(value.valueOf()) ? value.toISOString() : null
  } catch {
    return null
  }
}

function isUtcTimestamp(value: unknown): value is string {
  if (typeof value !== 'string' || !UTC_RE.test(value)) return false
  const parsed = new Date(value)
  return Number.isFinite(parsed.valueOf()) && parsed.toISOString() === value
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function hasExactKeys(value: Record<string, unknown>, expected: readonly string[]): boolean {
  const actual = Object.keys(value)
  const expectedSet = new Set(expected)
  return actual.length === expected.length && actual.every((key) => expectedSet.has(key))
}

function bestEffortChmod(path: string, mode: number): void {
  try { chmodSync(path, mode) } catch { /* unsupported on some filesystems */ }
}

function syncDirectoryBestEffort(directory: string): void {
  let descriptor: number | null = null
  try {
    descriptor = openSync(directory, constants.O_RDONLY)
    fsyncSync(descriptor)
  } catch {
    // Windows and some managed filesystems do not expose directory fsync.
  } finally {
    if (descriptor !== null) {
      try { closeSync(descriptor) } catch { /* best effort */ }
    }
  }
}
