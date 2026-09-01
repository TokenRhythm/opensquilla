import type { InjectionKey } from 'vue'
import type { MetaSetupReadiness } from '@/types/metaSetup'

/** A wire-independent MetaSkill draft used by recovery UI. */
export interface MetaDraft {
  readonly sessionKey: string
  readonly clientRequestId: string
  readonly name: string
  readonly launchText: string
  readonly createdAt: number
  readonly expiresAt: number
  readonly sessionExists: boolean
}

export interface MetaDraftListResult {
  readonly drafts: readonly MetaDraft[]
  readonly durable: boolean
}

/** Legacy name kept as a domain alias while older composables migrate. */
export type MetaLaunchDraftPayload = MetaDraft

export interface MetaDraftQuery {
  readonly sessionKey?: string
  readonly agentId?: string
}

export interface MetaDraftDiscardResult {
  readonly discarded: boolean
  readonly accepted: boolean
}

export interface MetaLaunchResult {
  readonly ok: boolean
  readonly name?: string
  readonly sessionKey?: string
  readonly clientRequestId?: string
  readonly replayed?: boolean
  readonly drafted?: boolean
  readonly setupRequired?: boolean
  readonly readiness?: MetaSetupReadiness
  readonly error?: string
}

export interface MetaRunRecovery {
  readonly announced?: Record<string, unknown>
  readonly stepStates: readonly Record<string, unknown>[]
  readonly completed?: Record<string, unknown>
}

export interface MetaPreflightConfirmation {
  readonly message?: string
}

export interface MetaPreflightInput {
  readonly sessionKey: string
  readonly runId: string
  readonly interpretedRequest?: string
  readonly fields?: unknown
  readonly useDefaults?: boolean
}

export interface MetaReplayInput {
  readonly sessionKey: string
  readonly runId: string
  readonly mode: string
  readonly action?: string
  readonly stepId?: string
  readonly prepareLive?: boolean
  readonly replayToken?: string
}

export interface MetaReplay {
  readonly message?: string
  readonly launchText?: string
  readonly displayText?: string
  readonly liveReplay?: {
    readonly available?: boolean
    readonly replayToken?: string
    readonly committed?: boolean
  }
}

export type MetaEventKind = 'preflight' | 'run-announced' | 'step-state' | 'run-completed'

/** Domain projections consumed by the Meta UI; wire snake_case aliases stay in the Adapter. */
export interface MetaPreflightFieldSpec {
  name?: string
  label?: string
  title?: string
  type?: string
  kind?: string
  multiline?: boolean
  required?: boolean
  default?: unknown
  description?: string
  help?: string
  hint?: string
  options?: unknown[]
  choices?: unknown[]
}

export interface MetaPreflightPayload extends MetaSessionEventPayload {
  run_id?: string
  meta_skill_name?: string
  language?: string
  interpreted_request?: string
  missing_fields?: string[]
  assumptions?: string[]
  request_template?: {
    language?: string
    outcome?: string
    deliverable?: string
    fields?: MetaPreflightFieldSpec[]
  }
  can_skip?: boolean
  requires_confirmation?: boolean
}

export interface MetaRunStepSpec {
  id?: string
  label?: string
  kind?: string
  depends_on?: string[]
}

export interface MetaRunAnnouncedPayload extends MetaSessionEventPayload {
  run_id?: string
  meta_skill_name?: string
  language?: string
  user_language?: string
  meta_language?: string
  steps?: MetaRunStepSpec[]
  total?: number
}

export interface MetaStepRescueAction {
  id?: string
  label?: string
}

export interface MetaStepRescue {
  actions?: MetaStepRescueAction[]
}

export interface MetaStepStatePayload extends MetaSessionEventPayload {
  run_id?: string
  step_id?: string
  state?: string
  status_text?: string | null
  error?: string
  substitute_for?: string | null
  rescue?: MetaStepRescue
}

export interface MetaRunCompletedPayload extends MetaSessionEventPayload {
  run_id?: string
  outcome?: string
  completed_steps?: string[]
  failed_steps?: string[]
  recovered_steps?: string[]
  skipped_steps?: string[]
}

export interface MetaSessionEventPayload {
  key?: string
  session_key?: string
  sessionKey?: string
  epoch?: number
  stream_seq?: number
  streamSeq?: number
  stream_generation?: string
  streamGeneration?: string
  [key: string]: unknown
}

/** Canonical event projection; session and sequence fencing stays in the adapter. */
export interface MetaEvent {
  readonly kind: MetaEventKind
  readonly payload: Readonly<Record<string, unknown>>
  readonly sessionKey: string | null
  readonly streamSeq: number | null
  readonly streamGeneration: string | null
}

export interface MetaRunRequestOptions {
  readonly signal?: AbortSignal
}

export interface MetaRunSubscription {
  close(): void
}

export type MetaRunErrorCode = 'not-found' | 'unsupported' | 'forbidden' | 'conflict' | 'unavailable' | 'invalid'

export class MetaRunCenterError extends Error {
  readonly name = 'MetaRunCenterError'

  constructor(
    readonly code: MetaRunErrorCode,
    message: string,
    readonly cause?: unknown,
  ) {
    super(message)
  }
}

/**
 * Domain seam for MetaSkill runs and durable launch recovery.
 *
 * RPC method names, event aliases, error codes and snake/camel wire fields are
 * deliberately absent from this interface. They belong to the Gateway
 * adapter, which allows the Meta UI and recovery controller to be tested with
 * an in-memory implementation.
 */
export interface MetaRunCenter {
  launch(
    input: {
      name: string
      sessionKey: string
      clientRequestId?: string
      launchText?: string
    },
    options?: MetaRunRequestOptions,
  ): Promise<MetaLaunchResult>
  listDrafts(query: MetaDraftQuery, options?: MetaRunRequestOptions): Promise<MetaDraftListResult>
  discardDraft(
    input: { sessionKey: string; clientRequestId: string },
    options?: MetaRunRequestOptions,
  ): Promise<MetaDraftDiscardResult>
  recover(sessionKey: string, options?: MetaRunRequestOptions): Promise<MetaRunRecovery | null>
  confirmPreflight(
    input: MetaPreflightInput,
    options?: MetaRunRequestOptions,
  ): Promise<MetaPreflightConfirmation>
  replay(input: MetaReplayInput, options?: MetaRunRequestOptions): Promise<MetaReplay>
  setupPlan(name: string, options?: MetaRunRequestOptions): Promise<Record<string, unknown>>
  setupStatus(input: { jobId: string; sessionKey: string }, options?: MetaRunRequestOptions): Promise<Record<string, unknown>>
  setupInstall(input: { name: string; sessionKey: string; confirmed: boolean; actionIds: readonly string[] }, options?: MetaRunRequestOptions): Promise<Record<string, unknown>>
  subscribe(listener: (event: MetaEvent) => void): MetaRunSubscription
}

export const META_RUN_CENTER_KEY: InjectionKey<MetaRunCenter> = Symbol('MetaRunCenter')
