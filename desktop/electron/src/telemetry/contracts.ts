export type TelemetryScope = 'reliability' | 'growth'
export type DesktopPlatform = 'macos' | 'windows' | 'linux'

export const CURRENT_NOTICE_VERSION_BY_SCOPE: Readonly<Record<TelemetryScope, string>> =
  Object.freeze({
    reliability: 'reliability-v1',
    growth: 'growth-v1',
  })

export const TELEMETRY_PROTOCOL_FINGERPRINT_SHA256 =
  '74d821c7d6ea2f3f08b5e27280da24ff17a51a913a165d5314d413d6204c1b7b'

type ResultOutcome = 'success' | 'fail' | 'timeout' | 'cancel'

interface EventEnvelope {
  event_name: string
  event_version: 1
  event_id: string
  occurred_at_utc: string
  source: 'desktop' | 'updater'
  app_version: string
  platform: DesktopPlatform
  outcome: string | null
  error_code: string | null
  duration_ms: number | null
  consent_scope: TelemetryScope
  notice_version: string
  sample_rate: number
}

interface ReliabilityEnvelope extends EventEnvelope {
  consent_scope: 'reliability'
  app_session_id: string
}

interface GrowthEnvelope extends EventEnvelope {
  consent_scope: 'growth'
  analytics_user_id: string
  error_code: null
  duration_ms: null
  sample_rate: 1
}

export interface AppStartResultEvent extends ReliabilityEnvelope {
  event_name: 'app_start_result'
  source: 'desktop'
  outcome: ResultOutcome
  failure_stage: 'profile' | 'gateway_start' | 'gateway_health' | 'control_ui' | 'ready' | null
}

export interface GatewayStartResultEvent extends ReliabilityEnvelope {
  event_name: 'gateway_start_result'
  source: 'desktop'
  outcome: ResultOutcome
  failure_stage: 'spawn' | 'health' | 'control_ui' | 'ownership' | null
  startup_mode: 'spawned' | 'reused' | 'external'
}

export interface AppCrashDetectedEvent extends ReliabilityEnvelope {
  event_name: 'app_crash_detected'
  source: 'desktop'
  outcome: 'detected'
  duration_ms: null
  component: 'desktop_main' | 'desktop_renderer' | 'gateway' | 'gpu' | 'utility' | 'unknown'
  error_fingerprint: string
  runtime_ms: number
}

export interface UpdateResultEvent extends ReliabilityEnvelope {
  event_name: 'update_result'
  source: 'updater'
  outcome: ResultOutcome
  update_stage: 'check' | 'download' | 'install' | 'restart'
  old_version: string
  new_version: string | null
  result: 'available' | 'not_available' | null
}

export interface PerformanceSummaryEvent extends ReliabilityEnvelope {
  event_name: 'performance_summary'
  source: 'desktop'
  outcome: 'success'
  error_code: null
  sample_rate: 1
  summary_kind: 'session_end' | 'recovered_abnormal'
  coverage: 'complete' | 'partial'
  turn_count: number
  stalled_turn_count: number
  stall_count: number
  stall_threshold_ms: 15000
  monitored_request_count: number
  slow_request_count: number
  slow_request_threshold_ms: 30000
  foreground_duration_ms: number
  background_duration_ms: number
}

export interface OnboardingCompletedEvent extends GrowthEnvelope {
  event_name: 'onboarding_result'
  source: 'desktop'
  outcome: 'completed'
  flow_version: 1
}

export interface FirstAppReadyEvent extends GrowthEnvelope {
  event_name: 'first_app_ready'
  source: 'desktop'
  outcome: null
}

export type DesktopEarlyTelemetryEvent =
  | AppStartResultEvent
  | GatewayStartResultEvent
  | AppCrashDetectedEvent
  | UpdateResultEvent
  | PerformanceSummaryEvent
  | OnboardingCompletedEvent
  | FirstAppReadyEvent

const UUID4_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
const RFC3339_UTC_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/
const SAFE_VERSION_RE = /^[A-Za-z0-9][A-Za-z0-9._+\-]{0,63}$/
const FINGERPRINT_RE = /^[a-f0-9]{64}$/
const MAX_DURATION_MS = 365 * 24 * 60 * 60 * 1000
const MAX_COUNTER = 2 ** 31 - 1

const RESULT_OUTCOMES = new Set(['success', 'fail', 'timeout', 'cancel'])
const PLATFORMS = new Set(['macos', 'windows', 'linux'])
const APP_START_FAILURE_STAGES = new Set([
  'profile',
  'gateway_start',
  'gateway_health',
  'control_ui',
  'ready',
])
const APP_START_ERROR_CODES = new Set([
  'profile_recovery_required',
  'keychain_unavailable',
  'profile_in_use',
  'runtime_unavailable',
  'spawn_failed',
  'health_timeout',
  'control_ui_timeout',
  'ownership_unverified',
  'renderer_load_failed',
  'startup_cancelled',
  'internal_error',
])
const GATEWAY_FAILURE_STAGES = new Set(['spawn', 'health', 'control_ui', 'ownership'])
const GATEWAY_START_ERROR_CODES = new Set([
  'runtime_unavailable',
  'spawn_failed',
  'health_timeout',
  'control_ui_timeout',
  'ownership_unverified',
  'startup_cancelled',
  'internal_error',
])
const CRASH_COMPONENTS = new Set([
  'desktop_main',
  'desktop_renderer',
  'gateway',
  'gpu',
  'utility',
  'unknown',
])
const CRASH_ERROR_CODES = new Set([
  'uncaught_exception',
  'renderer_crashed',
  'renderer_killed',
  'gateway_unexpected_exit',
  'child_process_crashed',
  'stale_session_marker',
  'unknown',
])
const UPDATE_ERROR_CODES = new Set([
  'source_unreachable',
  'manifest_invalid',
  'checksum_unavailable',
  'integrity_failed',
  'download_failed',
  'install_failed',
  'gateway_shutdown_timeout',
  'version_unchanged',
  'restart_not_ready',
  'operation_cancelled',
  'internal_error',
])

const COMMON_KEYS = [
  'event_name',
  'event_version',
  'event_id',
  'occurred_at_utc',
  'source',
  'app_version',
  'platform',
  'outcome',
  'error_code',
  'duration_ms',
  'consent_scope',
  'notice_version',
  'sample_rate',
] as const

const EVENT_KEYS: Readonly<Record<string, readonly string[]>> = Object.freeze({
  app_start_result: [...COMMON_KEYS, 'app_session_id', 'failure_stage'],
  gateway_start_result: [
    ...COMMON_KEYS,
    'app_session_id',
    'failure_stage',
    'startup_mode',
  ],
  app_crash_detected: [
    ...COMMON_KEYS,
    'app_session_id',
    'component',
    'error_fingerprint',
    'runtime_ms',
  ],
  update_result: [
    ...COMMON_KEYS,
    'app_session_id',
    'update_stage',
    'old_version',
    'new_version',
    'result',
  ],
  performance_summary: [
    ...COMMON_KEYS,
    'app_session_id',
    'summary_kind',
    'coverage',
    'turn_count',
    'stalled_turn_count',
    'stall_count',
    'stall_threshold_ms',
    'monitored_request_count',
    'slow_request_count',
    'slow_request_threshold_ms',
    'foreground_duration_ms',
    'background_duration_ms',
  ],
  onboarding_result: [...COMMON_KEYS, 'analytics_user_id', 'flow_version'],
  first_app_ready: [...COMMON_KEYS, 'analytics_user_id'],
})

export const DESKTOP_EARLY_EVENT_SCOPES: Readonly<
  Record<keyof typeof EVENT_KEYS, TelemetryScope>
> = Object.freeze({
  app_start_result: 'reliability',
  gateway_start_result: 'reliability',
  app_crash_detected: 'reliability',
  update_result: 'reliability',
  performance_summary: 'reliability',
  onboarding_result: 'growth',
  first_app_ready: 'growth',
})

function asRecord(value: unknown): Record<string, unknown> {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw new TypeError('telemetry event must be an object')
  }
  return value as Record<string, unknown>
}

function assertCondition(condition: boolean, message: string): asserts condition {
  if (!condition) throw new TypeError(message)
}

function hasStringValue(value: unknown, choices: ReadonlySet<string>): value is string {
  return typeof value === 'string' && choices.has(value)
}

function isBoundedInteger(value: unknown, maximum: number): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 && value <= maximum
}

export function isSafeTelemetryVersion(value: unknown): value is string {
  return typeof value === 'string' && SAFE_VERSION_RE.test(value)
}

export function isUtcTelemetryTimestamp(value: unknown): value is string {
  if (typeof value !== 'string' || !RFC3339_UTC_RE.test(value)) return false
  const parsed = new Date(value)
  return Number.isFinite(parsed.valueOf()) && parsed.toISOString() === value
}

function assertExactKeys(record: Record<string, unknown>, expected: readonly string[]): void {
  const actual = Object.keys(record)
  if (actual.length !== expected.length) {
    throw new TypeError('telemetry event does not match its closed field set')
  }
  const expectedSet = new Set(expected)
  if (actual.some((key) => !expectedSet.has(key))) {
    throw new TypeError('telemetry event does not match its closed field set')
  }
}

function validateCommon(record: Record<string, unknown>): void {
  assertCondition(record.event_version === 1, 'telemetry event version is unsupported')
  assertCondition(
    typeof record.event_id === 'string' && UUID4_RE.test(record.event_id),
    'telemetry event id must be UUIDv4',
  )
  assertCondition(isUtcTelemetryTimestamp(record.occurred_at_utc), 'telemetry timestamp must be UTC')
  assertCondition(isSafeTelemetryVersion(record.app_version), 'telemetry app version is invalid')
  assertCondition(hasStringValue(record.platform, PLATFORMS), 'telemetry platform is invalid')
  assertCondition(isSafeTelemetryVersion(record.notice_version), 'telemetry notice version is invalid')
  assertCondition(
    typeof record.sample_rate === 'number' &&
      Number.isFinite(record.sample_rate) &&
      record.sample_rate > 0 &&
      record.sample_rate <= 1,
    'telemetry sample rate is invalid',
  )
}

function validateReliabilityCommon(record: Record<string, unknown>): void {
  assertCondition(record.consent_scope === 'reliability', 'telemetry scope is invalid')
  assertCondition(
    typeof record.app_session_id === 'string' && UUID4_RE.test(record.app_session_id),
    'telemetry app session id must be UUIDv4',
  )
}

function validateGrowthCommon(record: Record<string, unknown>): void {
  assertCondition(record.consent_scope === 'growth', 'telemetry scope is invalid')
  assertCondition(
    typeof record.analytics_user_id === 'string' && UUID4_RE.test(record.analytics_user_id),
    'telemetry analytics id must be UUIDv4',
  )
  assertCondition(record.error_code === null, 'growth telemetry cannot include an error code')
  assertCondition(record.duration_ms === null, 'growth telemetry cannot include a duration')
  assertCondition(record.sample_rate === 1, 'growth telemetry cannot be sampled')
}

function validateTerminalPair(
  record: Record<string, unknown>,
  errorCodes: ReadonlySet<string>,
): void {
  assertCondition(hasStringValue(record.outcome, RESULT_OUTCOMES), 'telemetry outcome is invalid')
  if (record.outcome === 'success') {
    assertCondition(record.error_code === null, 'successful telemetry cannot include an error code')
  } else {
    assertCondition(
      hasStringValue(record.error_code, errorCodes),
      'failed telemetry requires a closed error code',
    )
  }
}

function validateAppStart(record: Record<string, unknown>): void {
  assertCondition(record.source === 'desktop', 'app start source is invalid')
  validateTerminalPair(record, APP_START_ERROR_CODES)
  assertCondition(
    isBoundedInteger(record.duration_ms, MAX_DURATION_MS),
    'app start duration is invalid',
  )
  if (record.outcome === 'success') {
    assertCondition(record.failure_stage === null, 'successful app start cannot have a failure stage')
  } else {
    assertCondition(
      hasStringValue(record.failure_stage, APP_START_FAILURE_STAGES),
      'failed app start requires a closed failure stage',
    )
  }
}

function validateGatewayStart(record: Record<string, unknown>): void {
  assertCondition(record.source === 'desktop', 'gateway start source is invalid')
  validateTerminalPair(record, GATEWAY_START_ERROR_CODES)
  assertCondition(
    isBoundedInteger(record.duration_ms, MAX_DURATION_MS),
    'gateway start duration is invalid',
  )
  assertCondition(
    hasStringValue(record.startup_mode, new Set(['spawned', 'reused', 'external'])),
    'gateway startup mode is invalid',
  )
  if (record.outcome === 'success') {
    assertCondition(
      record.failure_stage === null,
      'successful gateway start cannot have a failure stage',
    )
  } else {
    assertCondition(
      hasStringValue(record.failure_stage, GATEWAY_FAILURE_STAGES),
      'failed gateway start requires a closed failure stage',
    )
  }
}

function validateCrash(record: Record<string, unknown>): void {
  assertCondition(record.source === 'desktop', 'crash source is invalid')
  assertCondition(record.outcome === 'detected', 'crash outcome is invalid')
  assertCondition(
    hasStringValue(record.error_code, CRASH_ERROR_CODES),
    'crash error code is invalid',
  )
  assertCondition(record.duration_ms === null, 'crash duration must be null')
  assertCondition(hasStringValue(record.component, CRASH_COMPONENTS), 'crash component is invalid')
  assertCondition(
    typeof record.error_fingerprint === 'string' && FINGERPRINT_RE.test(record.error_fingerprint),
    'crash fingerprint is invalid',
  )
  assertCondition(isBoundedInteger(record.runtime_ms, MAX_DURATION_MS), 'crash runtime is invalid')
}

function validateUpdate(record: Record<string, unknown>): void {
  assertCondition(record.source === 'updater', 'update source is invalid')
  validateTerminalPair(record, UPDATE_ERROR_CODES)
  assertCondition(isBoundedInteger(record.duration_ms, MAX_DURATION_MS), 'update duration is invalid')
  assertCondition(
    hasStringValue(record.update_stage, new Set(['check', 'download', 'install', 'restart'])),
    'update stage is invalid',
  )
  assertCondition(isSafeTelemetryVersion(record.old_version), 'old app version is invalid')
  assertCondition(
    record.new_version === null || isSafeTelemetryVersion(record.new_version),
    'new app version is invalid',
  )
  assertCondition(
    record.result === null || record.result === 'available' || record.result === 'not_available',
    'update result is invalid',
  )
  if (record.update_stage === 'check') {
    if (record.outcome === 'success') {
      assertCondition(record.result !== null, 'successful update check requires a result')
      if (record.result === 'available') {
        assertCondition(record.new_version !== null, 'available update requires a new version')
      } else {
        assertCondition(record.new_version === null, 'not-available update cannot have a new version')
      }
    } else {
      assertCondition(record.result === null, 'failed update check cannot have a result')
    }
  } else {
    assertCondition(record.result === null, 'only update checks can include a result')
    assertCondition(record.new_version !== null, 'update operation requires a new version')
  }
}

function validatePerformanceSummary(record: Record<string, unknown>): void {
  assertCondition(record.source === 'desktop', 'performance summary source is invalid')
  assertCondition(record.outcome === 'success', 'performance summary outcome is invalid')
  assertCondition(record.error_code === null, 'performance summary error code must be null')
  assertCondition(
    isBoundedInteger(record.duration_ms, MAX_DURATION_MS),
    'performance summary duration is invalid',
  )
  assertCondition(record.sample_rate === 1, 'performance summaries cannot be sampled')
  assertCondition(
    record.summary_kind === 'session_end' || record.summary_kind === 'recovered_abnormal',
    'performance summary kind is invalid',
  )
  assertCondition(
    record.coverage === 'complete' || record.coverage === 'partial',
    'performance coverage is invalid',
  )
  for (const field of [
    'turn_count',
    'stalled_turn_count',
    'stall_count',
    'monitored_request_count',
    'slow_request_count',
  ]) {
    assertCondition(isBoundedInteger(record[field], MAX_COUNTER), 'performance counter is invalid')
  }
  for (const field of ['foreground_duration_ms', 'background_duration_ms']) {
    assertCondition(
      isBoundedInteger(record[field], MAX_DURATION_MS),
      'performance duration is invalid',
    )
  }
  assertCondition(record.stall_threshold_ms === 15000, 'stall threshold is invalid')
  assertCondition(record.slow_request_threshold_ms === 30000, 'slow threshold is invalid')
  assertCondition(
    (record.stalled_turn_count as number) <= (record.turn_count as number),
    'stalled turn count exceeds turn count',
  )
  assertCondition(
    (record.stall_count as number) >= (record.stalled_turn_count as number),
    'stall count is lower than stalled turn count',
  )
  assertCondition(
    (record.slow_request_count as number) <= (record.monitored_request_count as number),
    'slow request count exceeds monitored request count',
  )
  assertCondition(
    (record.foreground_duration_ms as number) + (record.background_duration_ms as number) <=
      (record.duration_ms as number),
    'foreground and background durations exceed total duration',
  )
  if (record.summary_kind === 'session_end') {
    assertCondition(record.coverage === 'complete', 'session summary coverage is invalid')
  } else {
    assertCondition(record.coverage === 'partial', 'recovered summary coverage is invalid')
  }
}

function validateGrowth(record: Record<string, unknown>): void {
  assertCondition(record.source === 'desktop', 'growth event source is invalid')
  validateGrowthCommon(record)
  if (record.event_name === 'onboarding_result') {
    assertCondition(record.outcome === 'completed', 'onboarding outcome is invalid')
    assertCondition(record.flow_version === 1, 'onboarding flow version is invalid')
  } else {
    assertCondition(record.outcome === null, 'first app ready outcome must be null')
  }
}

/**
 * Validate the closed subset of telemetry events owned by the Electron shell.
 * Gateway/runtime-owned events are intentionally rejected at this boundary.
 */
export function validateDesktopEarlyTelemetryEvent(value: unknown): DesktopEarlyTelemetryEvent {
  const record = asRecord(value)
  const eventName = typeof record.event_name === 'string' ? record.event_name : ''
  const expectedKeys = EVENT_KEYS[eventName]
  assertCondition(expectedKeys !== undefined, 'telemetry event is not Electron-owned')
  assertExactKeys(record, expectedKeys)
  validateCommon(record)

  switch (eventName) {
    case 'app_start_result':
      validateReliabilityCommon(record)
      validateAppStart(record)
      break
    case 'gateway_start_result':
      validateReliabilityCommon(record)
      validateGatewayStart(record)
      break
    case 'app_crash_detected':
      validateReliabilityCommon(record)
      validateCrash(record)
      break
    case 'update_result':
      validateReliabilityCommon(record)
      validateUpdate(record)
      break
    case 'performance_summary':
      validateReliabilityCommon(record)
      validatePerformanceSummary(record)
      break
    case 'onboarding_result':
    case 'first_app_ready':
      validateGrowth(record)
      break
    default:
      throw new TypeError('telemetry event is not Electron-owned')
  }
  return record as unknown as DesktopEarlyTelemetryEvent
}
