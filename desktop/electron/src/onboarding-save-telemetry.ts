import { performance } from 'node:perf_hooks'

export type OnboardingSaveTelemetryCode =
  | 'onboarding_inactive'
  | 'save_in_progress'
  | 'recovery_required'
  | 'lifecycle_deferred'

export type OnboardingSaveTelemetryStage =
  | 'primary_recovery_inspect'
  | 'pending_setup_read'
  | 'settings_persist'
  | 'local_finalize'
  | 'flow_handoff'

export type OnboardingSaveTelemetryEvent =
  | 'onboarding_save_started'
  | 'onboarding_save_stage_started'
  | 'onboarding_save_stage_finished'
  | 'onboarding_save_finished'

export type OnboardingSaveTelemetrySink = (
  event: OnboardingSaveTelemetryEvent,
  detail: Record<string, unknown>,
) => void

type OnboardingSaveReturnedResult =
  | { ok: true }
  | { ok: false; code: OnboardingSaveTelemetryCode }

type OnboardingSaveTerminalOutcome = 'ok' | 'returned_error' | 'threw'

function elapsedMilliseconds(startedAt: number, finishedAt: number): number {
  const elapsed = finishedAt - startedAt
  return Number.isFinite(elapsed) ? Math.max(0, Math.round(elapsed)) : 0
}

/**
 * Emits bounded, content-free timing breadcrumbs for one real onboarding save.
 * The caller controls save semantics; this observer never logs payloads or errors.
 */
export class OnboardingSaveTelemetry {
  private readonly overallStartedAt: number
  private lastStage: OnboardingSaveTelemetryStage | null = null
  private terminalOutcome: OnboardingSaveTerminalOutcome = 'threw'
  private terminalCode: OnboardingSaveTelemetryCode | undefined
  private writerAdmitted = false
  private settingsPersistedConfirmed = false
  private finished = false

  constructor(
    private readonly attempt: number,
    private readonly packaged: boolean,
    private readonly sink: OnboardingSaveTelemetrySink,
    private readonly now: () => number = () => performance.now(),
  ) {
    this.overallStartedAt = this.now()
    this.emit('onboarding_save_started', {
      attempt: this.attempt,
      packaged: this.packaged,
    })
  }

  async stage<T>(
    stage: OnboardingSaveTelemetryStage,
    operation: () => Promise<T>,
  ): Promise<T> {
    this.lastStage = stage
    this.emit('onboarding_save_stage_started', { attempt: this.attempt, stage })
    // Exclude the synchronous breadcrumb append itself from the measured work.
    const startedAt = this.now()
    let outcome: 'completed' | 'threw' = 'threw'
    try {
      const value = await operation()
      outcome = 'completed'
      return value
    } finally {
      const durationMs = elapsedMilliseconds(startedAt, this.now())
      this.emit('onboarding_save_stage_finished', {
        attempt: this.attempt,
        stage,
        durationMs,
        outcome,
      })
    }
  }

  stageSync<T>(stage: OnboardingSaveTelemetryStage, operation: () => T): T {
    this.lastStage = stage
    this.emit('onboarding_save_stage_started', { attempt: this.attempt, stage })
    // Flow handoff is synchronous. Keeping it synchronous prevents observability
    // from introducing a lifecycle-relevant yield.
    const startedAt = this.now()
    let outcome: 'completed' | 'threw' = 'threw'
    try {
      const value = operation()
      outcome = 'completed'
      return value
    } finally {
      const durationMs = elapsedMilliseconds(startedAt, this.now())
      this.emit('onboarding_save_stage_finished', {
        attempt: this.attempt,
        stage,
        durationMs,
        outcome,
      })
    }
  }

  markWriterAdmitted(): void {
    this.writerAdmitted = true
  }

  markSettingsPersistedConfirmed(): void {
    this.settingsPersistedConfirmed = true
  }

  recordReturned<Result extends OnboardingSaveReturnedResult>(result: Result): Result {
    this.terminalOutcome = result.ok ? 'ok' : 'returned_error'
    this.terminalCode = result.ok ? undefined : result.code
    return result
  }

  finish(): void {
    if (this.finished) return
    this.finished = true
    const detail: Record<string, unknown> = {
      attempt: this.attempt,
      totalDurationMs: elapsedMilliseconds(this.overallStartedAt, this.now()),
      outcome: this.terminalOutcome,
      writerAdmitted: this.writerAdmitted,
      settingsPersistedConfirmed: this.settingsPersistedConfirmed,
      lastStage: this.lastStage,
    }
    if (this.terminalCode !== undefined) detail.code = this.terminalCode
    this.emit('onboarding_save_finished', detail)
  }

  private emit(event: OnboardingSaveTelemetryEvent, detail: Record<string, unknown>): void {
    try {
      this.sink(event, detail)
    } catch {
      // Observability must never alter onboarding persistence or lifecycle state.
    }
  }
}
