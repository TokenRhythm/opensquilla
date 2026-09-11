import { createHash, randomUUID } from 'node:crypto'
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
  unlinkSync,
  writeFileSync,
} from 'node:fs'
import { join, resolve } from 'node:path'

import { resolveMirroredConsent } from './consent-mirror.js'
import {
  CURRENT_NOTICE_VERSION_BY_SCOPE,
  type AppCrashDetectedEvent,
  type AppStartResultEvent,
  type DesktopEarlyTelemetryEvent,
  type DesktopPlatform,
  type GatewayStartResultEvent,
  type PerformanceSummaryEvent,
  type UpdateResultEvent,
} from './contracts.js'
import {
  canWriteDurableTelemetryMarker,
  EARLY_SPOOL_DURABLE_MARKER_RESERVATION_BYTES,
  DESKTOP_RELIABILITY_RECOVERY_MARKER_PREFIX,
  DESKTOP_RELIABILITY_SESSION_MARKER_NAME,
  DESKTOP_UPDATE_TRANSITION_MARKER_NAME,
  DesktopTelemetryRuntimeGate,
  spoolEarlyTelemetryEvent,
  type EarlySpoolResult,
} from './early-spool.js'

const SESSION_MARKER_NAME = DESKTOP_RELIABILITY_SESSION_MARKER_NAME
const UPDATE_MARKER_NAME = DESKTOP_UPDATE_TRANSITION_MARKER_NAME
const MARKER_MAX_BYTES = EARLY_SPOOL_DURABLE_MARKER_RESERVATION_BYTES
const MAX_DURATION_MS = 365 * 24 * 60 * 60 * 1000
const MAX_COUNTER = 2 ** 31 - 1
const UUID4_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
const SHA256_RE = /^[a-f0-9]{64}$/
const SAFE_VERSION_RE = /^[A-Za-z0-9][A-Za-z0-9._+\-]{0,63}$/
const UTC_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/

export const DESKTOP_STALL_THRESHOLD_MS = 15_000
export const DESKTOP_SLOW_REQUEST_THRESHOLD_MS = 30_000
export const DESKTOP_RELIABILITY_MAX_RECOVERY_MARKERS = 32

export type AppStartFailureStage =
  | 'profile'
  | 'gateway_start'
  | 'gateway_health'
  | 'control_ui'
  | 'ready'

export type AppStartErrorCode =
  | 'profile_recovery_required'
  | 'keychain_unavailable'
  | 'profile_in_use'
  | 'runtime_unavailable'
  | 'spawn_failed'
  | 'health_timeout'
  | 'control_ui_timeout'
  | 'ownership_unverified'
  | 'renderer_load_failed'
  | 'startup_cancelled'
  | 'internal_error'

export type GatewayStartFailureStage = 'spawn' | 'health' | 'control_ui' | 'ownership'
export type GatewayStartErrorCode =
  | 'runtime_unavailable'
  | 'spawn_failed'
  | 'health_timeout'
  | 'control_ui_timeout'
  | 'ownership_unverified'
  | 'startup_cancelled'
  | 'internal_error'

export type CrashComponent =
  | 'desktop_main'
  | 'desktop_renderer'
  | 'gateway'
  | 'gpu'
  | 'utility'
  | 'unknown'

export type CrashErrorCode =
  | 'uncaught_exception'
  | 'renderer_crashed'
  | 'renderer_killed'
  | 'gateway_unexpected_exit'
  | 'child_process_crashed'
  | 'stale_session_marker'
  | 'unknown'

export type CrashFingerprintReason =
  | 'uncaught_exception'
  | 'crashed'
  | 'oom'
  | 'killed'
  | 'launch_failed'
  | 'integrity_failure'
  | 'abnormal_exit'
  | 'unknown'

export type CrashFingerprintSignature =
  | 'aggregate_error'
  | 'error'
  | 'eval_error'
  | 'range_error'
  | 'reference_error'
  | 'syntax_error'
  | 'type_error'
  | 'uri_error'
  | 'unknown'

export type UpdateErrorCode =
  | 'source_unreachable'
  | 'manifest_invalid'
  | 'checksum_unavailable'
  | 'integrity_failed'
  | 'download_failed'
  | 'install_failed'
  | 'gateway_shutdown_timeout'
  | 'version_unchanged'
  | 'restart_not_ready'
  | 'operation_cancelled'
  | 'internal_error'

export interface DesktopReliabilityPaths {
  spoolRoot: string
  consentMirrorPath: string
}

interface PerformanceSnapshot {
  turn_count: number
  stalled_turn_count: number
  stall_count: number
  monitored_request_count: number
  slow_request_count: number
  foreground_duration_ms: number
  background_duration_ms: number
}

interface CrashFact {
  component: CrashComponent
  error_code: CrashErrorCode
  error_fingerprint: string
  occurred_at_utc: string
  runtime_ms: number
}

interface PersistedAppStartResult {
  outcome: 'success' | 'fail' | 'timeout' | 'cancel'
  error_code: AppStartErrorCode | null
  failure_stage: AppStartFailureStage | null
  duration_ms: number
  completed_at_ms: number
}

interface SessionMarker {
  schema_version: 2
  marker_kind: 'desktop_reliability_session'
  app_session_id: string
  app_version: string
  started_at_ms: number
  last_observed_at_ms: number
  crash_event_id: string
  recovered_performance_event_id: string
  app_start_event_id: string
  app_start_started_at_ms: number
  app_start_stage: AppStartFailureStage
  app_start_result: PersistedAppStartResult | null
  app_start_result_emitted: boolean
  crash: CrashFact | null
  crash_detected_emitted: boolean
  clean_exit: boolean
  performance_summary_emitted: boolean
  performance: PerformanceSnapshot
}

interface UpdateMarker {
  schema_version: 1
  marker_kind: 'desktop_update_transition'
  status: 'handoff' | 'installed'
  source_app_session_id: string
  restart_app_session_id: string | null
  install_event_id: string
  restart_event_id: string
  old_version: string
  new_version: string
  handoff_at_ms: number
  restart_started_at_ms: number | null
  install_result: PersistedUpdateResult | null
  install_result_emitted: boolean
  restart_result: PersistedUpdateResult | null
  restart_result_emitted: boolean
}

interface PersistedUpdateResult {
  outcome: 'success' | 'fail'
  error_code: UpdateErrorCode | null
  completed_at_ms: number
  app_session_id: string
}

interface PerformanceAccumulatorOptions {
  nowMs: () => number
}

class PerformanceAccumulator {
  private readonly nowMs: () => number
  private startedAtMs: number
  private stateStartedAtMs: number
  private foreground = false
  private foregroundMs = 0
  private backgroundMs = 0
  private turnCount = 0
  private stalledTurnCount = 0
  private stallCount = 0
  private monitoredRequestCount = 0
  private slowRequestCount = 0
  private activeStallStartedAtMs: number | null = null

  constructor(options: PerformanceAccumulatorOptions) {
    this.nowMs = options.nowMs
    this.startedAtMs = options.nowMs()
    this.stateStartedAtMs = this.startedAtMs
  }

  reset(foreground: boolean): void {
    const now = this.nowMs()
    this.startedAtMs = now
    this.stateStartedAtMs = now
    this.foreground = foreground
    this.foregroundMs = 0
    this.backgroundMs = 0
    this.turnCount = 0
    this.stalledTurnCount = 0
    this.stallCount = 0
    this.monitoredRequestCount = 0
    this.slowRequestCount = 0
    this.activeStallStartedAtMs = null
  }

  setForeground(foreground: boolean): boolean {
    if (foreground === this.foreground) return false
    this.accumulateState(this.nowMs())
    this.foreground = foreground
    return true
  }

  beginStall(): boolean {
    if (this.activeStallStartedAtMs !== null) return false
    this.activeStallStartedAtMs = this.nowMs()
    return true
  }

  endStall(): number {
    if (this.activeStallStartedAtMs === null) return 0
    const duration = boundedDuration(this.nowMs() - this.activeStallStartedAtMs)
    this.activeStallStartedAtMs = null
    if (duration >= DESKTOP_STALL_THRESHOLD_MS) this.stallCount = boundedCount(this.stallCount + 1)
    return duration
  }

  recordMonitoredRequest(durationMs: number): boolean {
    this.monitoredRequestCount = boundedCount(this.monitoredRequestCount + 1)
    if (durationMs < DESKTOP_SLOW_REQUEST_THRESHOLD_MS) return false
    this.slowRequestCount = boundedCount(this.slowRequestCount + 1)
    return true
  }

  snapshot(): { durationMs: number; performance: PerformanceSnapshot } {
    const now = this.nowMs()
    const elapsedInState = boundedDuration(now - this.stateStartedAtMs)
    const foreground = boundedDuration(
      this.foregroundMs + (this.foreground ? elapsedInState : 0),
    )
    const background = boundedDuration(
      this.backgroundMs + (this.foreground ? 0 : elapsedInState),
    )
    const durationMs = Math.max(
      boundedDuration(now - this.startedAtMs),
      boundedDuration(foreground + background),
    )
    const activeStall = this.activeStallStartedAtMs !== null
      && now - this.activeStallStartedAtMs >= DESKTOP_STALL_THRESHOLD_MS
      ? 1
      : 0
    return {
      durationMs,
      performance: {
        turn_count: this.turnCount,
        stalled_turn_count: this.stalledTurnCount,
        stall_count: boundedCount(this.stallCount + activeStall),
        monitored_request_count: this.monitoredRequestCount,
        slow_request_count: this.slowRequestCount,
        foreground_duration_ms: Math.min(foreground, durationMs),
        background_duration_ms: Math.min(background, Math.max(0, durationMs - foreground)),
      },
    }
  }

  private accumulateState(now: number): void {
    const elapsed = boundedDuration(now - this.stateStartedAtMs)
    if (this.foreground) this.foregroundMs = boundedDuration(this.foregroundMs + elapsed)
    else this.backgroundMs = boundedDuration(this.backgroundMs + elapsed)
    this.stateStartedAtMs = now
  }
}

export interface DesktopReliabilityTelemetryOptions {
  runtimeGate: DesktopTelemetryRuntimeGate
  /**
   * The plain application version used by updater handoff state and semver
   * comparisons.  Keep this separate from the telemetry identity below.
   */
  appVersion: () => string
  /**
   * Optional version identity for reliability events.  Source builds may
   * append a validated commit id while updater state remains plain semver.
   */
  telemetryAppVersion?: () => string
  platform: DesktopPlatform
  processStartedAtMs: number
  env?: Readonly<Record<string, string | undefined>>
  appSessionId?: string
  nowMs?: () => number
  nowDate?: () => Date
  randomId?: () => string
}

export class DesktopReliabilityTelemetry {
  appSessionId: string

  private readonly runtimeGate: DesktopTelemetryRuntimeGate
  private readonly appVersion: () => string
  private readonly telemetryAppVersion: () => string
  private readonly platform: DesktopPlatform
  private readonly processStartedAtMs: number
  private readonly env: Readonly<Record<string, string | undefined>>
  private readonly nowMs: () => number
  private readonly nowDate: () => Date
  private readonly randomId: () => string
  private readonly performance: PerformanceAccumulator
  private paths: DesktopReliabilityPaths | null = null
  private desiredForeground = false
  private sessionStarted = false
  private sessionFinished = false
  private currentMarker: SessionMarker | null = null
  private pendingInstalledUpdate: UpdateMarker | null = null
  private requestCheckpointCounter = 0
  private appStartStage: AppStartFailureStage = 'profile'
  private appStartTerminalObserved = false
  private consentGrantGeneration: string | null = null

  constructor(options: DesktopReliabilityTelemetryOptions) {
    this.runtimeGate = options.runtimeGate
    this.appVersion = options.appVersion
    this.telemetryAppVersion = options.telemetryAppVersion ?? options.appVersion
    this.platform = options.platform
    this.processStartedAtMs = options.processStartedAtMs
    this.env = options.env ?? process.env
    this.nowMs = options.nowMs ?? (() => Date.now())
    this.nowDate = options.nowDate ?? (() => new Date())
    this.randomId = options.randomId ?? (() => randomUUID())
    this.appSessionId = options.appSessionId ?? this.randomId()
    this.performance = new PerformanceAccumulator({ nowMs: this.nowMs })
  }

  /** Reconcile the current profile only after its authoritative consent mirror is live. */
  synchronize(paths: DesktopReliabilityPaths): void {
    try {
      const nextPaths = {
        spoolRoot: resolve(paths.spoolRoot),
        consentMirrorPath: resolve(paths.consentMirrorPath),
      }
      if (
        this.paths
        && (
          this.paths.spoolRoot !== nextPaths.spoolRoot
          || this.paths.consentMirrorPath !== nextPaths.consentMirrorPath
        )
      ) {
        // A profile/state-dir switch closes one telemetry session and starts a
        // fresh one. First make the old aggregate/crash facts independently
        // durable; a full/failed sink may retry when that profile is active
        // again, without ever checkpointing A's in-memory state into B.
        if (this.sessionStarted && this.currentMarker && this.consentEnabled()) {
          const snapshot = this.performance.snapshot()
          const closed: SessionMarker = {
            ...this.currentMarker,
            last_observed_at_ms: Math.floor(this.nowMs()),
            clean_exit: true,
            performance: snapshot.performance,
          }
          if (this.writeSessionMarker(closed)) {
            const recoveryName = this.writeRecoveryMarker(closed)
            if (recoveryName !== null) {
              this.removeManagedMarker(SESSION_MARKER_NAME)
              this.flushSessionMarker(recoveryName, closed)
            }
          }
        }
        this.sessionStarted = false
        this.currentMarker = null
        this.pendingInstalledUpdate = null
        this.requestCheckpointCounter = 0
        this.consentGrantGeneration = null
        this.appSessionId = this.randomId()
      }
      this.paths = nextPaths
      if (!this.reconcileConsentGrant().enabled) return
      this.ensureSession()
    } catch {
      // Telemetry must never affect profile startup or consent reconciliation.
    }
  }

  /** Keep only the latest closed startup stage; never retain its label or error text. */
  observeAppStartStage(stage: AppStartFailureStage): void {
    if (this.appStartTerminalObserved) return
    this.appStartStage = stage
    if (!this.sessionStarted || !this.currentMarker || this.sessionFinished) return
    this.currentMarker = { ...this.currentMarker, app_start_stage: stage }
    this.checkpointSession()
  }

  setForeground(foreground: boolean): void {
    this.desiredForeground = foreground
    if (!this.prepareActivity()) return
    try {
      if (this.performance.setForeground(foreground)) this.checkpointSession()
    } catch {
      // A duration checkpoint is best-effort.
    }
  }

  beginStall(): void {
    if (!this.prepareActivity()) return
    try {
      if (this.performance.beginStall()) this.checkpointSession()
    } catch {
      // A stall checkpoint is best-effort.
    }
  }

  endStall(): number {
    if (!this.prepareActivity()) return 0
    try {
      const duration = this.performance.endStall()
      this.checkpointSession()
      return duration
    } catch {
      return 0
    }
  }

  recordMonitoredRequest(durationMs: number): void {
    if (!this.prepareActivity()) return
    try {
      const slow = this.performance.recordMonitoredRequest(boundedDuration(durationMs))
      this.requestCheckpointCounter += 1
      if (slow || this.requestCheckpointCounter >= 32) {
        this.requestCheckpointCounter = 0
        this.checkpointSession()
      }
    } catch {
      // Request counters cannot affect the observed request.
    }
  }

  recordAppStartResult(input: {
    outcome: 'success' | 'fail' | 'timeout' | 'cancel'
    durationMs: number
    failureStage: AppStartFailureStage | null
    errorCode: AppStartErrorCode | null
  }): EarlySpoolResult | null {
    if (this.appStartTerminalObserved) return null
    this.ensureSession()
    this.appStartTerminalObserved = true
    if (!this.sessionStarted || !this.currentMarker || this.currentMarker.app_start_result_emitted) {
      return null
    }
    if (this.currentMarker.app_start_result === null) {
      this.currentMarker = {
        ...this.currentMarker,
        app_start_result: {
          outcome: input.outcome,
          error_code: input.errorCode,
          failure_stage: input.failureStage,
          duration_ms: boundedDuration(input.durationMs),
          completed_at_ms: Math.floor(this.nowMs()),
        },
      }
      // The fixed event id and terminal fact must be durable before enqueue.
      if (!this.writeSessionMarker(this.currentMarker)) return null
    }
    const flushed = this.flushAppStartResult(SESSION_MARKER_NAME, this.currentMarker)
    this.currentMarker = flushed.marker
    if (input.outcome === 'success') this.finishPendingRestart('success')
    else this.finishPendingRestart('fail')
    this.checkpointSession()
    return flushed.result
  }

  recordGatewayStartResult(input: {
    outcome: 'success' | 'fail' | 'timeout' | 'cancel'
    durationMs: number
    failureStage: GatewayStartFailureStage | null
    errorCode: GatewayStartErrorCode | null
    startupMode: 'spawned' | 'reused' | 'external'
  }): EarlySpoolResult | null {
    if (!this.prepareActivity()) return null
    const result = this.emit({
      ...this.baseEnvelope('gateway_start_result', 'desktop'),
      outcome: input.outcome,
      error_code: input.errorCode,
      duration_ms: boundedDuration(input.durationMs),
      app_session_id: this.appSessionId,
      failure_stage: input.failureStage,
      startup_mode: input.startupMode,
    } satisfies GatewayStartResultEvent)
    this.checkpointSession()
    return result
  }

  recordUpdateResult(input: {
    outcome: 'success' | 'fail' | 'timeout' | 'cancel'
    durationMs: number
    updateStage: 'check' | 'download' | 'install' | 'restart'
    errorCode: UpdateErrorCode | null
    oldVersion: string
    newVersion: string | null
    result: 'available' | 'not_available' | null
  }): EarlySpoolResult | null {
    if (!this.prepareActivity()) return null
    return this.emitUpdateResult({
      eventId: this.randomId(),
      appSessionId: this.appSessionId,
      ...input,
    })
  }

  recordCrash(input: {
    component: CrashComponent
    errorCode: CrashErrorCode
    reason: CrashFingerprintReason
    signature?: CrashFingerprintSignature
  }): void {
    if (this.sessionFinished) return
    try {
      this.ensureSession()
      if (!this.sessionStarted || !this.currentMarker || this.currentMarker.crash) return
      const now = this.safeNowDate()
      const observedAtMs = Math.floor(this.nowMs())
      const runtimeMs = boundedDuration(observedAtMs - this.currentMarker.started_at_ms)
      this.currentMarker = {
        ...this.currentMarker,
        last_observed_at_ms: observedAtMs,
        crash: {
          component: input.component,
          error_code: input.errorCode,
          error_fingerprint: crashFingerprint(
            input.component,
            input.errorCode,
            input.reason,
            input.signature ?? 'unknown',
            this.telemetryAppVersion(),
          ),
          occurred_at_utc: now.toISOString(),
          runtime_ms: runtimeMs,
        },
        crash_detected_emitted: false,
        performance: this.performance.snapshot().performance,
      }
      this.writeSessionMarker(this.currentMarker)
    } catch {
      // Crash observation must not replace Electron's native crash behavior.
    }
  }

  /** Persist a content-free handoff fact before electron-updater owns process exit. */
  markUpdateHandoff(newVersion: string): boolean {
    try {
      this.ensureSession()
      if (!this.sessionStarted || !isSafeVersion(newVersion)) return false
      const marker: UpdateMarker = {
        schema_version: 1,
        marker_kind: 'desktop_update_transition',
        status: 'handoff',
        source_app_session_id: this.appSessionId,
        restart_app_session_id: null,
        install_event_id: this.randomId(),
        restart_event_id: this.randomId(),
        old_version: this.appVersion(),
        new_version: newVersion,
        handoff_at_ms: Math.floor(this.nowMs()),
        restart_started_at_ms: null,
        install_result: null,
        install_result_emitted: false,
        restart_result: null,
        restart_result_emitted: false,
      }
      return this.writeUpdateMarker(marker)
    } catch {
      return false
    }
  }

  clearUpdateHandoff(): void {
    this.removeManagedMarker(UPDATE_MARKER_NAME)
    this.pendingInstalledUpdate = null
  }

  /** Emit the clean-session aggregate once, at a committed exit seam. */
  finishSession(): void {
    if (this.sessionFinished) return
    this.sessionFinished = true
    try {
      if (!this.consentEnabled()) return
      if (this.pendingInstalledUpdate) this.finishPendingRestart('fail')
      if (!this.sessionStarted || !this.currentMarker) return
      const snapshot = this.performance.snapshot()
      const completedAtMs = Math.floor(this.nowMs())
      this.currentMarker = {
        ...this.currentMarker,
        last_observed_at_ms: completedAtMs,
        clean_exit: true,
        performance: snapshot.performance,
      }
      // Persist clean termination before enqueue/removal. If unlink later
      // fails, recovery sees the acknowledgement state instead of inventing a
      // stale-session crash.
      if (!this.writeSessionMarker(this.currentMarker)) return
      this.flushSessionMarker(SESSION_MARKER_NAME, this.currentMarker, false)
    } catch {
      // Exit must remain committed even when telemetry storage is unavailable.
    }
  }

  /** Stop tracking without recreating state after an approved profile deletion. */
  abandonSession(): void {
    this.sessionFinished = true
    this.sessionStarted = false
    this.currentMarker = null
    this.pendingInstalledUpdate = null
  }

  private resetAfterConsentWithdrawal(): void {
    if (this.sessionFinished) return
    this.sessionStarted = false
    this.currentMarker = null
    this.pendingInstalledUpdate = null
    this.requestCheckpointCounter = 0
    this.performance.reset(this.desiredForeground)
    this.appSessionId = this.randomId()
  }

  private ensureSession(): void {
    if (this.sessionFinished || this.paths === null) return
    if (!this.reconcileConsentGrant().enabled) return
    this.flushRecoveryMarkers()
    if (this.sessionStarted) {
      if (this.currentMarker) {
        this.writeSessionMarker(this.currentMarker)
        const flushed = this.flushAppStartResult(SESSION_MARKER_NAME, this.currentMarker)
        this.currentMarker = flushed.marker
      }
      this.recoverPendingUpdate(null)
      return
    }

    const previous = this.readSessionMarker()
    if (previous && previous.app_session_id !== this.appSessionId) {
      // Never overwrite the only durable copy until the recovery facts have
      // their own managed marker. A dropped sink can then retry without
      // preventing the new process from tracking its own session.
      const recoveryName = this.writeRecoveryMarker(previous)
      if (recoveryName === null) return
      this.flushSessionMarker(recoveryName, previous)
    }
    this.recoverPendingUpdate(previous)

    this.performance.reset(this.desiredForeground)
    const nowMs = Math.floor(this.nowMs())
    const marker: SessionMarker = {
      schema_version: 2,
      marker_kind: 'desktop_reliability_session',
      app_session_id: this.appSessionId,
      app_version: this.telemetryAppVersion(),
      started_at_ms: nowMs,
      last_observed_at_ms: nowMs,
      crash_event_id: this.randomId(),
      recovered_performance_event_id: this.randomId(),
      app_start_event_id: this.randomId(),
      app_start_started_at_ms: boundedTimestamp(this.processStartedAtMs, nowMs),
      app_start_stage: this.appStartStage,
      app_start_result: null,
      app_start_result_emitted: this.appStartTerminalObserved,
      crash: null,
      crash_detected_emitted: false,
      clean_exit: false,
      performance_summary_emitted: false,
      performance: emptyPerformanceSnapshot(),
    }
    if (!this.writeSessionMarker(marker)) return
    this.currentMarker = marker
    this.sessionStarted = true
  }

  private flushRecoveryMarkers(): void {
    const recovered: Array<{ name: string; marker: SessionMarker }> = []
    for (const name of this.recoveryMarkerNames()) {
      const marker = this.readSessionMarker(name)
      if (marker) recovered.push({ name, marker })
      else this.removeManagedMarker(name)
    }
    recovered.sort((left, right) => (
      left.marker.last_observed_at_ms - right.marker.last_observed_at_ms
      || left.name.localeCompare(right.name)
    ))
    const overflow = Math.max(
      0,
      recovered.length - DESKTOP_RELIABILITY_MAX_RECOVERY_MARKERS,
    )
    // Pre-cap builds could accumulate enough durable markers to consume every
    // queue slot and permanently block their own recovery. Bound that upgrade
    // state deterministically, preferring the newest diagnostics. New builds
    // never enter this lossy repair path because writeRecoveryMarker enforces
    // the same cap before creating another marker.
    for (const entry of recovered.slice(0, overflow)) {
      this.removeManagedMarker(entry.name)
    }
    for (const { name, marker } of recovered.slice(overflow)) {
      this.flushSessionMarker(name, marker)
    }
  }

  private flushAppStartResult(
    name: string,
    initial: SessionMarker,
  ): { marker: SessionMarker; result: EarlySpoolResult | null } {
    if (initial.app_start_result === null || initial.app_start_result_emitted) {
      return { marker: initial, result: null }
    }
    const fact = initial.app_start_result
    const result = this.emitAppStartResult({
      eventId: initial.app_start_event_id,
      appSessionId: initial.app_session_id,
      appVersion: initial.app_version,
      occurredAtUtc: utcTimestampFromMs(fact.completed_at_ms),
      outcome: fact.outcome,
      errorCode: fact.error_code,
      durationMs: fact.duration_ms,
      failureStage: fact.failure_stage,
    })
    if (!spoolResultAcknowledged(result)) return { marker: initial, result }
    const acknowledged = { ...initial, app_start_result_emitted: true }
    if (!this.writeSessionMarker(acknowledged, name)) return { marker: initial, result }
    return { marker: acknowledged, result }
  }

  private flushSessionMarker(
    name: string,
    initial: SessionMarker,
    allowCrashFact = true,
  ): void {
    let marker = initial
    const durationMs = boundedDuration(marker.last_observed_at_ms - marker.started_at_ms)
    const appStartDurationMs = boundedDuration(
      marker.last_observed_at_ms - marker.app_start_started_at_ms,
    )
    if (marker.app_start_result === null && !marker.app_start_result_emitted) {
      const interrupted = marker.crash !== null || !marker.clean_exit
      marker = {
        ...marker,
        app_start_result: interrupted
          ? {
              outcome: 'fail',
              error_code: 'internal_error',
              failure_stage: marker.app_start_stage,
              duration_ms: appStartDurationMs,
              completed_at_ms: marker.last_observed_at_ms,
            }
          : {
              outcome: 'cancel',
              error_code: 'startup_cancelled',
              failure_stage: marker.app_start_stage,
              duration_ms: appStartDurationMs,
              completed_at_ms: marker.last_observed_at_ms,
            },
      }
      if (!this.writeSessionMarker(marker, name)) return
    }
    const appStart = this.flushAppStartResult(name, marker)
    marker = appStart.marker
    if (!marker.app_start_result_emitted) return
    const needsCrashFact = marker.crash !== null || !marker.clean_exit
    if (allowCrashFact && needsCrashFact && !marker.crash_detected_emitted) {
      const crash = marker.crash ?? {
        component: 'unknown' as const,
        error_code: 'stale_session_marker' as const,
        error_fingerprint: crashFingerprint(
          'unknown',
          'stale_session_marker',
          'unknown',
          'unknown',
          marker.app_version,
        ),
        occurred_at_utc: utcTimestampFromMs(marker.last_observed_at_ms),
        runtime_ms: durationMs,
      }
      const result = this.emit({
        ...this.baseEnvelope(
          'app_crash_detected',
          'desktop',
          marker.crash_event_id,
          crash.occurred_at_utc,
          marker.app_version,
        ),
        outcome: 'detected',
        error_code: crash.error_code,
        duration_ms: null,
        app_session_id: marker.app_session_id,
        component: crash.component,
        error_fingerprint: crash.error_fingerprint,
        runtime_ms: crash.runtime_ms,
      } satisfies AppCrashDetectedEvent)
      if (!spoolResultAcknowledged(result)) return
      marker = { ...marker, crash_detected_emitted: true }
      if (!this.writeSessionMarker(marker, name)) return
    }

    if (!marker.performance_summary_emitted) {
      const result = this.emitPerformanceSummary({
        eventId: marker.recovered_performance_event_id,
        appSessionId: marker.app_session_id,
        appVersion: marker.app_version,
        durationMs,
        occurredAtUtc: utcTimestampFromMs(marker.last_observed_at_ms),
        summaryKind: marker.clean_exit ? 'session_end' : 'recovered_abnormal',
        coverage: marker.clean_exit ? 'complete' : 'partial',
        performance: clampPerformanceToDuration(marker.performance, durationMs),
      })
      if (!spoolResultAcknowledged(result)) return
      marker = { ...marker, performance_summary_emitted: true }
      if (!this.writeSessionMarker(marker, name)) return
    }

    if ((!needsCrashFact || marker.crash_detected_emitted) && marker.performance_summary_emitted) {
      this.removeManagedMarker(name)
      if (name === SESSION_MARKER_NAME) this.currentMarker = marker
    }
  }

  private recoverPendingUpdate(previousSession: SessionMarker | null): void {
    let marker = this.readUpdateMarker()
    if (!marker) return
    if (
      marker.install_result === null
      && marker.source_app_session_id === this.appSessionId
    ) return
    if (marker.install_result === null) {
      const completedAtMs = this.processStartedAtMs
      const installed = this.appVersion() === marker.new_version
      marker = {
        ...marker,
        status: installed ? 'installed' : 'handoff',
        restart_app_session_id: this.appSessionId,
        restart_started_at_ms: completedAtMs,
        install_result: {
          outcome: installed ? 'success' : 'fail',
          error_code: installed ? null : 'version_unchanged',
          completed_at_ms: completedAtMs,
          app_session_id: marker.source_app_session_id,
        },
        restart_result: installed
          ? null
          : {
              outcome: 'fail',
              error_code: 'version_unchanged',
              completed_at_ms: completedAtMs,
              app_session_id: this.appSessionId,
            },
      }
      if (!this.writeUpdateMarker(marker)) return
    } else if (
      marker.status === 'installed'
      && marker.restart_result === null
      && marker.restart_app_session_id !== this.appSessionId
    ) {
      const completedAtMs = previousSession
        && previousSession.app_session_id === marker.restart_app_session_id
        ? previousSession.last_observed_at_ms
        : marker.restart_started_at_ms ?? marker.install_result.completed_at_ms
      marker = {
        ...marker,
        restart_result: {
          outcome: 'fail',
          error_code: 'restart_not_ready',
          completed_at_ms: completedAtMs,
          app_session_id: marker.restart_app_session_id ?? this.appSessionId,
        },
      }
      if (!this.writeUpdateMarker(marker)) return
    }
    this.flushUpdateMarker(marker)
    const durableMarker = this.readUpdateMarker()
    if (durableMarker?.status === 'installed' && durableMarker.restart_result === null) {
      this.pendingInstalledUpdate = durableMarker
    }
  }

  private finishPendingRestart(result: 'success' | 'fail'): void {
    const marker = this.pendingInstalledUpdate
    if (!marker || marker.restart_result !== null) return
    const updated: UpdateMarker = {
      ...marker,
      restart_result: {
        outcome: result,
        error_code: result === 'success' ? null : 'restart_not_ready',
        completed_at_ms: Math.floor(this.nowMs()),
        app_session_id: marker.restart_app_session_id ?? this.appSessionId,
      },
    }
    if (!this.writeUpdateMarker(updated)) return
    this.pendingInstalledUpdate = updated
    this.flushUpdateMarker(updated)
  }

  private flushUpdateMarker(initial: UpdateMarker): void {
    let marker = initial
    if (marker.install_result && !marker.install_result_emitted) {
      const fact = marker.install_result
      const result = this.emitUpdateResult({
        eventId: marker.install_event_id,
        appSessionId: fact.app_session_id,
        outcome: fact.outcome,
        durationMs: boundedDuration(fact.completed_at_ms - marker.handoff_at_ms),
        occurredAtUtc: utcTimestampFromMs(fact.completed_at_ms),
        updateStage: 'install',
        errorCode: fact.error_code,
        oldVersion: marker.old_version,
        newVersion: marker.new_version,
        result: null,
      })
      if (!spoolResultAcknowledged(result)) return
      marker = { ...marker, install_result_emitted: true }
      if (!this.writeUpdateMarker(marker)) return
    }
    if (marker.restart_result && !marker.restart_result_emitted) {
      const fact = marker.restart_result
      const result = this.emitUpdateResult({
        eventId: marker.restart_event_id,
        appSessionId: fact.app_session_id,
        outcome: fact.outcome,
        durationMs: boundedDuration(
          fact.completed_at_ms - (marker.restart_started_at_ms ?? fact.completed_at_ms),
        ),
        occurredAtUtc: utcTimestampFromMs(fact.completed_at_ms),
        updateStage: 'restart',
        errorCode: fact.error_code,
        oldVersion: marker.old_version,
        newVersion: marker.new_version,
        result: null,
      })
      if (!spoolResultAcknowledged(result)) return
      marker = { ...marker, restart_result_emitted: true }
      if (!this.writeUpdateMarker(marker)) return
    }
    if (
      marker.install_result !== null
      && marker.install_result_emitted
      && marker.restart_result !== null
      && marker.restart_result_emitted
    ) {
      this.pendingInstalledUpdate = null
      this.removeManagedMarker(UPDATE_MARKER_NAME)
    } else if (marker.status === 'installed') {
      this.pendingInstalledUpdate = marker
    }
  }

  private checkpointSession(): void {
    if (!this.sessionStarted || !this.currentMarker || this.sessionFinished) return
    if (!this.consentEnabled()) return
    if (!this.sessionStarted || !this.currentMarker || this.sessionFinished) return
    this.currentMarker = {
      ...this.currentMarker,
      last_observed_at_ms: Math.floor(this.nowMs()),
      performance: this.performance.snapshot().performance,
    }
    this.writeSessionMarker(this.currentMarker)
  }

  private emitPerformanceSummary(input: {
    eventId: string
    appSessionId: string
    durationMs: number
    summaryKind: 'session_end' | 'recovered_abnormal'
    coverage: 'complete' | 'partial'
    performance: PerformanceSnapshot
    appVersion?: string
    occurredAtUtc?: string
  }): EarlySpoolResult | null {
    const performance = clampPerformanceToDuration(input.performance, input.durationMs)
    return this.emit({
      ...this.baseEnvelope(
        'performance_summary',
        'desktop',
        input.eventId,
        input.occurredAtUtc,
        input.appVersion,
      ),
      outcome: 'success',
      error_code: null,
      duration_ms: boundedDuration(input.durationMs),
      app_session_id: input.appSessionId,
      summary_kind: input.summaryKind,
      coverage: input.coverage,
      ...performance,
      stall_threshold_ms: DESKTOP_STALL_THRESHOLD_MS,
      slow_request_threshold_ms: DESKTOP_SLOW_REQUEST_THRESHOLD_MS,
    } satisfies PerformanceSummaryEvent)
  }

  private emitAppStartResult(input: {
    eventId: string
    appSessionId: string
    appVersion: string
    occurredAtUtc: string
    outcome: 'success' | 'fail' | 'timeout' | 'cancel'
    errorCode: AppStartErrorCode | null
    durationMs: number
    failureStage: AppStartFailureStage | null
  }): EarlySpoolResult | null {
    return this.emit({
      ...this.baseEnvelope(
        'app_start_result',
        'desktop',
        input.eventId,
        input.occurredAtUtc,
        input.appVersion,
      ),
      outcome: input.outcome,
      error_code: input.errorCode,
      duration_ms: boundedDuration(input.durationMs),
      app_session_id: input.appSessionId,
      failure_stage: input.failureStage,
    } satisfies AppStartResultEvent)
  }

  private emitUpdateResult(input: {
    eventId: string
    appSessionId: string
    outcome: 'success' | 'fail' | 'timeout' | 'cancel'
    durationMs: number
    updateStage: 'check' | 'download' | 'install' | 'restart'
    errorCode: UpdateErrorCode | null
    oldVersion: string
    newVersion: string | null
    result: 'available' | 'not_available' | null
    occurredAtUtc?: string
  }): EarlySpoolResult | null {
    return this.emit({
      ...this.baseEnvelope(
        'update_result',
        'updater',
        input.eventId,
        input.occurredAtUtc,
      ),
      outcome: input.outcome,
      error_code: input.errorCode,
      duration_ms: boundedDuration(input.durationMs),
      app_session_id: input.appSessionId,
      update_stage: input.updateStage,
      old_version: input.oldVersion,
      new_version: input.newVersion,
      result: input.result,
    } satisfies UpdateResultEvent)
  }

  private emit(event: DesktopEarlyTelemetryEvent): EarlySpoolResult | null {
    try {
      if (!this.paths || !this.consentEnabled()) return null
      return spoolEarlyTelemetryEvent({
        spoolRoot: this.paths.spoolRoot,
        consentMirrorPath: this.paths.consentMirrorPath,
        event,
        runtimeGate: this.runtimeGate,
        env: this.env,
        now: this.safeNowDate(),
      })
    } catch {
      return null
    }
  }

  private baseEnvelope<
    Name extends DesktopEarlyTelemetryEvent['event_name'],
    Source extends 'desktop' | 'updater',
  >(
    eventName: Name,
    source: Source,
    eventId = this.randomId(),
    occurredAt = this.safeNowDate().toISOString(),
    appVersion = this.telemetryAppVersion(),
  ) {
    return {
      event_name: eventName,
      event_version: 1 as const,
      event_id: eventId,
      occurred_at_utc: occurredAt,
      source,
      app_version: appVersion,
      platform: this.platform,
      consent_scope: 'reliability' as const,
      notice_version: CURRENT_NOTICE_VERSION_BY_SCOPE.reliability,
      sample_rate: 1 as const,
    }
  }

  private consentEnabled(): boolean {
    const consent = this.reconcileConsentGrant()
    return consent.enabled && !consent.generationChanged
  }

  private reconcileConsentGrant(): { enabled: boolean; generationChanged: boolean } {
    if (!this.runtimeGate.isOpen() || this.paths === null) {
      return { enabled: false, generationChanged: false }
    }
    const consent = resolveMirroredConsent(
      this.paths.consentMirrorPath,
      'reliability',
      this.env,
    )
    if (consent.blockReason === 'consent_declined') {
      if (
        this.consentGrantGeneration !== null
        || this.sessionStarted
        || this.currentMarker !== null
        || this.pendingInstalledUpdate !== null
      ) {
        this.resetAfterConsentWithdrawal()
      }
      this.consentGrantGeneration = null
      return { enabled: false, generationChanged: false }
    }
    if (
      !consent.enabled
      || consent.noticeVersion !== CURRENT_NOTICE_VERSION_BY_SCOPE.reliability
      || consent.consentedAtUtc === null
    ) {
      return { enabled: false, generationChanged: false }
    }
    const generation = `${consent.noticeVersion}\n${consent.consentedAtUtc}`
    if (this.consentGrantGeneration === null) {
      this.consentGrantGeneration = generation
      return { enabled: true, generationChanged: false }
    }
    if (this.consentGrantGeneration === generation) {
      return { enabled: true, generationChanged: false }
    }
    this.resetAfterConsentWithdrawal()
    this.consentGrantGeneration = generation
    return { enabled: true, generationChanged: true }
  }

  private prepareActivity(): boolean {
    if (this.sessionFinished) return false
    const consent = this.reconcileConsentGrant()
    if (!consent.enabled) return false
    if (!this.sessionStarted || !this.currentMarker) this.ensureSession()
    return this.sessionStarted && this.currentMarker !== null
  }

  private markerPath(name: string): string | null {
    if (!this.paths || !isManagedMarkerName(name)) return null
    const directory = existingReliabilityScopeDirectory(this.paths.spoolRoot)
    return directory ? join(directory, name) : null
  }

  private readSessionMarker(name = SESSION_MARKER_NAME): SessionMarker | null {
    return parseSessionMarker(this.readManagedMarker(name))
  }

  private readUpdateMarker(): UpdateMarker | null {
    return parseUpdateMarker(this.readManagedMarker(UPDATE_MARKER_NAME))
  }

  private readManagedMarker(name: string): unknown {
    const path = this.markerPath(name)
    if (!path) return null
    try {
      const metadata = lstatSync(path)
      if (metadata.isSymbolicLink() || !metadata.isFile() || metadata.size > MARKER_MAX_BYTES) {
        return null
      }
      return JSON.parse(readFileSync(path, 'utf8')) as unknown
    } catch {
      return null
    }
  }

  private recoveryMarkerNames(): string[] {
    if (!this.paths) return []
    const directory = existingReliabilityScopeDirectory(this.paths.spoolRoot)
    if (!directory) return []
    try {
      return readdirSync(directory).filter(isRecoveryMarkerName).sort()
    } catch {
      return []
    }
  }

  private writeSessionMarker(marker: SessionMarker, name = SESSION_MARKER_NAME): boolean {
    return this.writeManagedMarker(name, marker)
  }

  private writeRecoveryMarker(marker: SessionMarker): string | null {
    const name = recoveryMarkerName(marker.app_session_id)
    const existing = this.recoveryMarkerNames()
    if (
      !existing.includes(name)
      && existing.length >= DESKTOP_RELIABILITY_MAX_RECOVERY_MARKERS
    ) return null
    return this.writeSessionMarker(marker, name) ? name : null
  }

  private writeUpdateMarker(marker: UpdateMarker): boolean {
    return this.writeManagedMarker(UPDATE_MARKER_NAME, marker)
  }

  private writeManagedMarker(name: string, value: SessionMarker | UpdateMarker): boolean {
    if (!this.paths || !isManagedMarkerName(name) || !this.consentEnabled()) return false
    const payload = Buffer.from(`${JSON.stringify(value)}\n`, 'utf8')
    if (payload.byteLength > MARKER_MAX_BYTES) return false
    let temporary: string | null = null
    let descriptor: number | null = null
    try {
      const scopeDirectory = ensureReliabilityScopeDirectory(this.paths.spoolRoot)
      if (!this.consentEnabled()) return false
      if (!canWriteDurableTelemetryMarker({
        spoolRoot: this.paths.spoolRoot,
        scope: 'reliability',
        markerName: name,
        payloadBytes: payload.byteLength,
        now: this.safeNowDate(),
      })) return false
      const target = join(scopeDirectory, name)
      if (existsSync(target)) {
        const metadata = lstatSync(target)
        if (metadata.isSymbolicLink() || !metadata.isFile()) return false
      }
      temporary = join(scopeDirectory, `.${name.slice(1, -4)}.${this.randomId()}.tmp`)
      descriptor = openSync(
        temporary,
        constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY,
        0o600,
      )
      writeFileSync(descriptor, payload)
      fsyncSync(descriptor)
      closeSync(descriptor)
      descriptor = null
      if (!this.consentEnabled()) {
        unlinkSync(temporary)
        temporary = null
        return false
      }
      renameSync(temporary, target)
      temporary = null
      bestEffortChmod(target, 0o600)
      syncDirectoryBestEffort(scopeDirectory)
      return true
    } catch {
      return false
    } finally {
      if (descriptor !== null) {
        try {
          closeSync(descriptor)
        } catch {
          // Best effort after a failed marker write.
        }
      }
      if (temporary !== null) {
        try {
          unlinkSync(temporary)
        } catch {
          // The temporary file may never have been created.
        }
      }
    }
  }

  private removeManagedMarker(name: string): void {
    const path = this.markerPath(name)
    if (!path) return
    try {
      const metadata = lstatSync(path)
      if (metadata.isSymbolicLink() || !metadata.isFile()) return
      unlinkSync(path)
    } catch {
      // Absence and cleanup failures are intentionally non-fatal.
    }
  }

  private safeNowDate(): Date {
    const value = this.nowDate()
    return Number.isFinite(value.valueOf()) ? value : new Date(0)
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function hasExactKeys(value: Record<string, unknown>, expected: readonly string[]): boolean {
  const keys = Object.keys(value)
  const expectedSet = new Set(expected)
  return keys.length === expected.length && keys.every((key) => expectedSet.has(key))
}

function isUuid(value: unknown): value is string {
  return typeof value === 'string' && UUID4_RE.test(value)
}

function recoveryMarkerName(appSessionId: string): string {
  return `${DESKTOP_RELIABILITY_RECOVERY_MARKER_PREFIX}${appSessionId}.tmp`
}

function isRecoveryMarkerName(name: string): boolean {
  if (
    !name.startsWith(DESKTOP_RELIABILITY_RECOVERY_MARKER_PREFIX)
    || !name.endsWith('.tmp')
  ) return false
  return isUuid(name.slice(DESKTOP_RELIABILITY_RECOVERY_MARKER_PREFIX.length, -4))
}

function isManagedMarkerName(name: string): boolean {
  return name === SESSION_MARKER_NAME
    || name === UPDATE_MARKER_NAME
    || isRecoveryMarkerName(name)
}

function spoolResultAcknowledged(result: EarlySpoolResult | null): boolean {
  return result?.status === 'written' || result?.status === 'duplicate'
}

function utcTimestampFromMs(value: number): string {
  const date = new Date(value)
  return Number.isFinite(date.valueOf()) ? date.toISOString() : new Date(0).toISOString()
}

function isSafeVersion(value: unknown): value is string {
  return typeof value === 'string' && SAFE_VERSION_RE.test(value)
}

function isUtc(value: unknown): value is string {
  if (typeof value !== 'string' || !UTC_RE.test(value)) return false
  const parsed = new Date(value)
  return Number.isFinite(parsed.valueOf()) && parsed.toISOString() === value
}

function isBoundedInteger(value: unknown, maximum = MAX_DURATION_MS): value is number {
  return typeof value === 'number'
    && Number.isSafeInteger(value)
    && value >= 0
    && value <= maximum
}

function parsePerformanceSnapshot(value: unknown): PerformanceSnapshot | null {
  if (!isRecord(value) || !hasExactKeys(value, [
    'turn_count',
    'stalled_turn_count',
    'stall_count',
    'monitored_request_count',
    'slow_request_count',
    'foreground_duration_ms',
    'background_duration_ms',
  ])) return null
  for (const field of [
    'turn_count',
    'stalled_turn_count',
    'stall_count',
    'monitored_request_count',
    'slow_request_count',
  ]) {
    if (!isBoundedInteger(value[field], MAX_COUNTER)) return null
  }
  if (
    !isBoundedInteger(value.foreground_duration_ms)
    || !isBoundedInteger(value.background_duration_ms)
  ) return null
  if (Number(value.stalled_turn_count) > Number(value.turn_count)) return null
  if (Number(value.stall_count) < Number(value.stalled_turn_count)) return null
  if (Number(value.slow_request_count) > Number(value.monitored_request_count)) return null
  return value as unknown as PerformanceSnapshot
}

const CRASH_COMPONENTS = new Set<CrashComponent>([
  'desktop_main', 'desktop_renderer', 'gateway', 'gpu', 'utility', 'unknown',
])
const CRASH_ERROR_CODES = new Set<CrashErrorCode>([
  'uncaught_exception',
  'renderer_crashed',
  'renderer_killed',
  'gateway_unexpected_exit',
  'child_process_crashed',
  'stale_session_marker',
  'unknown',
])
const APP_START_STAGES = new Set<AppStartFailureStage>([
  'profile', 'gateway_start', 'gateway_health', 'control_ui', 'ready',
])
const APP_START_ERROR_CODES = new Set<AppStartErrorCode>([
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

function parsePersistedAppStartResult(value: unknown): PersistedAppStartResult | null {
  if (!isRecord(value) || !hasExactKeys(value, [
    'outcome', 'error_code', 'failure_stage', 'duration_ms', 'completed_at_ms',
  ])) return null
  if (
    (value.outcome !== 'success'
      && value.outcome !== 'fail'
      && value.outcome !== 'timeout'
      && value.outcome !== 'cancel')
    || (
      value.outcome === 'success'
        ? value.error_code !== null || value.failure_stage !== null
        : typeof value.error_code !== 'string'
          || !APP_START_ERROR_CODES.has(value.error_code as AppStartErrorCode)
          || typeof value.failure_stage !== 'string'
          || !APP_START_STAGES.has(value.failure_stage as AppStartFailureStage)
    )
    || !isBoundedInteger(value.duration_ms)
    || !isBoundedInteger(value.completed_at_ms, Number.MAX_SAFE_INTEGER)
    || !Number.isFinite(new Date(Number(value.completed_at_ms)).valueOf())
  ) return null
  return value as unknown as PersistedAppStartResult
}

function parseCrashFact(value: unknown): CrashFact | null {
  if (!isRecord(value) || !hasExactKeys(value, [
    'component', 'error_code', 'error_fingerprint', 'occurred_at_utc', 'runtime_ms',
  ])) return null
  if (typeof value.component !== 'string' || !CRASH_COMPONENTS.has(value.component as CrashComponent)) return null
  if (typeof value.error_code !== 'string' || !CRASH_ERROR_CODES.has(value.error_code as CrashErrorCode)) return null
  if (typeof value.error_fingerprint !== 'string' || !SHA256_RE.test(value.error_fingerprint)) return null
  if (!isUtc(value.occurred_at_utc) || !isBoundedInteger(value.runtime_ms)) return null
  return value as unknown as CrashFact
}

function parseSessionMarker(value: unknown): SessionMarker | null {
  if (!isRecord(value)) return null
  const legacyKeys = [
    'schema_version',
    'marker_kind',
    'app_session_id',
    'app_version',
    'started_at_ms',
    'last_observed_at_ms',
    'crash_event_id',
    'recovered_performance_event_id',
    'crash',
    'crash_detected_emitted',
    'clean_exit',
    'performance_summary_emitted',
    'performance',
  ] as const
  const currentKeys = [
    ...legacyKeys,
    'app_start_event_id',
    'app_start_started_at_ms',
    'app_start_stage',
    'app_start_result',
    'app_start_result_emitted',
  ] as const
  const legacy = value.schema_version === 1 && hasExactKeys(value, legacyKeys)
  const current = value.schema_version === 2 && hasExactKeys(value, currentKeys)
  if (!legacy && !current) return null
  const performance = parsePerformanceSnapshot(value.performance)
  const crash = value.crash === null ? null : parseCrashFact(value.crash)
  const appStart = current && value.app_start_result !== null
    ? parsePersistedAppStartResult(value.app_start_result)
    : null
  if (
    value.marker_kind !== 'desktop_reliability_session'
    || !isUuid(value.app_session_id)
    || !isSafeVersion(value.app_version)
    || !isBoundedInteger(value.started_at_ms, Number.MAX_SAFE_INTEGER)
    || !isBoundedInteger(value.last_observed_at_ms, Number.MAX_SAFE_INTEGER)
    || Number(value.last_observed_at_ms) < Number(value.started_at_ms)
    || !Number.isFinite(new Date(Number(value.last_observed_at_ms)).valueOf())
    || !isUuid(value.crash_event_id)
    || !isUuid(value.recovered_performance_event_id)
    || (value.crash !== null && crash === null)
    || typeof value.crash_detected_emitted !== 'boolean'
    || typeof value.clean_exit !== 'boolean'
    || typeof value.performance_summary_emitted !== 'boolean'
    || performance === null
  ) return null
  if (current && (
    !isUuid(value.app_start_event_id)
    || !isBoundedInteger(value.app_start_started_at_ms, Number.MAX_SAFE_INTEGER)
    || Number(value.app_start_started_at_ms) > Number(value.last_observed_at_ms)
    || !Number.isFinite(new Date(Number(value.app_start_started_at_ms)).valueOf())
    || typeof value.app_start_stage !== 'string'
    || !APP_START_STAGES.has(value.app_start_stage as AppStartFailureStage)
    || (value.app_start_result !== null && appStart === null)
    || typeof value.app_start_result_emitted !== 'boolean'
  )) return null
  if (legacy) {
    return {
      ...value,
      schema_version: 2,
      app_start_event_id: value.crash_event_id,
      app_start_started_at_ms: value.started_at_ms,
      app_start_stage: 'ready',
      app_start_result: null,
      // Legacy builds emitted app_start_result outside the marker. Its exact
      // status is unknowable, so recovery must never manufacture a duplicate.
      app_start_result_emitted: true,
      crash,
      performance,
    } as unknown as SessionMarker
  }
  return { ...value, app_start_result: appStart, crash, performance } as unknown as SessionMarker
}

const UPDATE_ERROR_CODES = new Set<UpdateErrorCode>([
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

function parsePersistedUpdateResult(value: unknown): PersistedUpdateResult | null {
  if (!isRecord(value) || !hasExactKeys(value, [
    'outcome', 'error_code', 'completed_at_ms', 'app_session_id',
  ])) return null
  if (
    (value.outcome !== 'success' && value.outcome !== 'fail')
    || (
      value.outcome === 'success'
        ? value.error_code !== null
        : typeof value.error_code !== 'string'
          || !UPDATE_ERROR_CODES.has(value.error_code as UpdateErrorCode)
    )
    || !isBoundedInteger(value.completed_at_ms, Number.MAX_SAFE_INTEGER)
    || !Number.isFinite(new Date(Number(value.completed_at_ms)).valueOf())
    || !isUuid(value.app_session_id)
  ) return null
  return value as unknown as PersistedUpdateResult
}

function parseUpdateMarker(value: unknown): UpdateMarker | null {
  if (!isRecord(value) || !hasExactKeys(value, [
    'schema_version',
    'marker_kind',
    'status',
    'source_app_session_id',
    'restart_app_session_id',
    'install_event_id',
    'restart_event_id',
    'old_version',
    'new_version',
    'handoff_at_ms',
    'restart_started_at_ms',
    'install_result',
    'install_result_emitted',
    'restart_result',
    'restart_result_emitted',
  ])) return null
  const installResult = value.install_result === null
    ? null
    : parsePersistedUpdateResult(value.install_result)
  const restartResult = value.restart_result === null
    ? null
    : parsePersistedUpdateResult(value.restart_result)
  if (
    value.schema_version !== 1
    || value.marker_kind !== 'desktop_update_transition'
    || (value.status !== 'handoff' && value.status !== 'installed')
    || !isUuid(value.source_app_session_id)
    || (value.restart_app_session_id !== null && !isUuid(value.restart_app_session_id))
    || !isUuid(value.install_event_id)
    || !isUuid(value.restart_event_id)
    || !isSafeVersion(value.old_version)
    || !isSafeVersion(value.new_version)
    || !isBoundedInteger(value.handoff_at_ms, Number.MAX_SAFE_INTEGER)
    || (
      value.restart_started_at_ms !== null
      && !isBoundedInteger(value.restart_started_at_ms, Number.MAX_SAFE_INTEGER)
    )
    || (value.install_result !== null && installResult === null)
    || typeof value.install_result_emitted !== 'boolean'
    || (value.restart_result !== null && restartResult === null)
    || typeof value.restart_result_emitted !== 'boolean'
    || (value.install_result_emitted && installResult === null)
    || (value.restart_result_emitted && restartResult === null)
  ) return null
  return { ...value, install_result: installResult, restart_result: restartResult } as unknown as UpdateMarker
}

function boundedDuration(value: number): number {
  if (!Number.isFinite(value)) return 0
  return Math.max(0, Math.min(MAX_DURATION_MS, Math.floor(value)))
}

function boundedTimestamp(value: number, fallback: number): number {
  if (!Number.isFinite(value) || value < 0) return Math.max(0, Math.floor(fallback))
  return Math.min(Math.max(0, Math.floor(value)), Math.max(0, Math.floor(fallback)))
}

function boundedCount(value: number): number {
  if (!Number.isFinite(value)) return 0
  return Math.max(0, Math.min(MAX_COUNTER, Math.floor(value)))
}

function emptyPerformanceSnapshot(): PerformanceSnapshot {
  return {
    turn_count: 0,
    stalled_turn_count: 0,
    stall_count: 0,
    monitored_request_count: 0,
    slow_request_count: 0,
    foreground_duration_ms: 0,
    background_duration_ms: 0,
  }
}

function clampPerformanceToDuration(
  performance: PerformanceSnapshot,
  durationMs: number,
): PerformanceSnapshot {
  const duration = boundedDuration(durationMs)
  const foreground = Math.min(boundedDuration(performance.foreground_duration_ms), duration)
  const background = Math.min(
    boundedDuration(performance.background_duration_ms),
    Math.max(0, duration - foreground),
  )
  return {
    turn_count: boundedCount(performance.turn_count),
    stalled_turn_count: Math.min(
      boundedCount(performance.stalled_turn_count),
      boundedCount(performance.turn_count),
    ),
    stall_count: Math.max(
      boundedCount(performance.stall_count),
      Math.min(boundedCount(performance.stalled_turn_count), boundedCount(performance.turn_count)),
    ),
    monitored_request_count: boundedCount(performance.monitored_request_count),
    slow_request_count: Math.min(
      boundedCount(performance.slow_request_count),
      boundedCount(performance.monitored_request_count),
    ),
    foreground_duration_ms: foreground,
    background_duration_ms: background,
  }
}

function crashFingerprint(
  component: CrashComponent,
  errorCode: CrashErrorCode,
  reason: CrashFingerprintReason,
  signature: CrashFingerprintSignature,
  appVersion: string,
): string {
  return createHash('sha256')
    .update(
      `desktop-crash-v2\n${component}\n${errorCode}\n${reason}\n${signature}\n${appVersion}`,
      'utf8',
    )
    .digest('hex')
}

function bestEffortChmod(path: string, mode: number): void {
  try {
    chmodSync(path, mode)
  } catch {
    // Windows and some managed filesystems do not expose POSIX modes.
  }
}

function requireRealDirectory(path: string): void {
  const metadata = lstatSync(path)
  if (metadata.isSymbolicLink() || !metadata.isDirectory()) {
    throw new Error('unsafe reliability telemetry directory')
  }
}

function existingReliabilityScopeDirectory(spoolRoot: string): string | null {
  const root = resolve(spoolRoot)
  try {
    requireRealDirectory(root)
    const scope = join(root, 'reliability')
    requireRealDirectory(scope)
    return scope
  } catch {
    return null
  }
}

function ensureReliabilityScopeDirectory(spoolRoot: string): string {
  const root = resolve(spoolRoot)
  mkdirSync(root, { recursive: true, mode: 0o700 })
  requireRealDirectory(root)
  bestEffortChmod(root, 0o700)
  const scope = join(root, 'reliability')
  try {
    mkdirSync(scope, { recursive: false, mode: 0o700 })
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'EEXIST') throw error
  }
  requireRealDirectory(scope)
  bestEffortChmod(scope, 0o700)
  return scope
}

function syncDirectoryBestEffort(directory: string): void {
  let descriptor: number | null = null
  try {
    descriptor = openSync(directory, constants.O_RDONLY)
    fsyncSync(descriptor)
  } catch {
    // Directory fsync is unavailable on some Windows/filesystem combinations.
  } finally {
    if (descriptor !== null) {
      try {
        closeSync(descriptor)
      } catch {
        // The file itself was already synchronized.
      }
    }
  }
}
