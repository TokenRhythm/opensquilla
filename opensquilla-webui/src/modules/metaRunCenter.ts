import type { InjectionKey } from 'vue'
import type { ConversationCursorSignal } from '@/modules/conversationRuntime'
import type { MetaSetupJob, MetaSetupReadiness } from '@/types/metaSetup'

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
  readonly announced?: MetaRunAnnouncedPayload
  readonly stepStates: readonly MetaStepStatePayload[]
  readonly completed?: MetaRunCompletedPayload
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

export interface MetaPreflightRequestTemplate {
  language?: string
  outcome?: string
  deliverable?: string
  fields?: MetaPreflightFieldSpec[]
}

export interface MetaPreflightPayload {
  runId?: string
  metaSkillName?: string
  language?: string
  interpretedRequest?: string
  missingFields?: string[]
  assumptions?: string[]
  requestTemplate?: MetaPreflightRequestTemplate
  canSkip?: boolean
  requiresConfirmation?: boolean
}

export interface MetaRunStepSpec {
  id?: string
  label?: string
  kind?: string
  dependsOn?: string[]
}

export interface MetaRunAnnouncedPayload {
  runId?: string
  metaSkillName?: string
  language?: string
  userLanguage?: string
  metaLanguage?: string
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

export interface MetaStepStatePayload {
  runId?: string
  stepId?: string
  state?: string
  statusText?: string | null
  error?: string
  substituteFor?: string | null
  rescue?: MetaStepRescue
}

export interface MetaRunCompletedPayload {
  runId?: string
  outcome?: string
  completedSteps?: string[]
  failedSteps?: string[]
  recoveredSteps?: string[]
  skippedSteps?: string[]
}

/** Canonical cursor facts shared with the conversation runtime. */
interface MetaEventContext extends ConversationCursorSignal {
  readonly sessionKey: string | null
  readonly sessionEpoch: number | null
  readonly streamSeq: number | null
  readonly streamGeneration: string | null
}

/** Canonical event projection; all wire aliases stay in the Gateway Adapter. */
export type MetaEvent =
  | (MetaEventContext & { readonly kind: 'preflight'; readonly payload: MetaPreflightPayload })
  | (MetaEventContext & { readonly kind: 'run-announced'; readonly payload: MetaRunAnnouncedPayload })
  | (MetaEventContext & { readonly kind: 'step-state'; readonly payload: MetaStepStatePayload })
  | (MetaEventContext & { readonly kind: 'run-completed'; readonly payload: MetaRunCompletedPayload })

export interface MetaRunRequestOptions {
  readonly signal?: AbortSignal
}

export interface MetaRunSubscription {
  close(): void
}

export type MetaRunErrorCode =
  | 'not-found'
  | 'unsupported'
  | 'forbidden'
  | 'conflict'
  | 'unavailable'
  | 'invalid'
  | 'draft-discarded'

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

/** Setup failures reject with MetaRunCenterError; successful results need no wire envelope. */
export interface MetaSetupPlan {
  readonly readiness: MetaSetupReadiness
}

export interface MetaSetupStatus {
  readonly job: MetaSetupJob
}

export type MetaSetupInstallation =
  | { readonly alreadyReady: true; readonly readiness?: MetaSetupReadiness; readonly job?: never }
  | { readonly job: MetaSetupJob; readonly alreadyReady?: false }

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
  setupPlan(name: string, options?: MetaRunRequestOptions): Promise<MetaSetupPlan>
  setupStatus(input: { jobId: string; sessionKey: string }, options?: MetaRunRequestOptions): Promise<MetaSetupStatus>
  setupInstall(input: { name: string; sessionKey: string; confirmed: boolean; actionIds: readonly string[] }, options?: MetaRunRequestOptions): Promise<MetaSetupInstallation>
  subscribe(listener: (event: MetaEvent) => void): MetaRunSubscription
}

export const META_RUN_CENTER_KEY: InjectionKey<MetaRunCenter> = Symbol('MetaRunCenter')
