import type {
  TransportCallOptions as RpcCallOptions,
  TransportEventHandler as RpcEventHandler,
} from './transportTypes'
import { readTransportFailure } from './transportTypes'
import type {
  MetaDraft,
  MetaDraftDiscardResult,
  MetaDraftListResult,
  MetaEvent,
  MetaLaunchResult,
  MetaPreflightConfirmation,
  MetaPreflightFieldSpec,
  MetaPreflightInput,
  MetaPreflightPayload,
  MetaReplay,
  MetaReplayInput,
  MetaRunAnnouncedPayload,
  MetaRunCenter,
  MetaRunCompletedPayload,
  MetaRunRecovery,
  MetaRunRequestOptions,
  MetaRunStepSpec,
  MetaSetupInstallation,
  MetaSetupPlan,
  MetaSetupStatus,
  MetaStepRescue,
  MetaStepRescueAction,
  MetaStepStatePayload,
} from '@/modules/metaRunCenter'
import { MetaRunCenterError } from '@/modules/metaRunCenter'
import type { MetaSetupJob, MetaSetupReadiness } from '@/types/metaSetup'
import { META_RUN_METHOD } from '@/contracts/generated/v4/metaRun'
import { validateResult as validateMetaRunResult } from '@/contracts/generated/v4/metaRunValidators.mjs'
import { META_DRAFTS_LIST_METHOD } from '@/contracts/generated/v4/metaDraftsList'
import { validateResult as validateMetaDraftsListResult } from '@/contracts/generated/v4/metaDraftsListValidators.mjs'
import { META_DRAFTS_DISCARD_METHOD } from '@/contracts/generated/v4/metaDraftsDiscard'
import { validateResult as validateMetaDraftsDiscardResult } from '@/contracts/generated/v4/metaDraftsDiscardValidators.mjs'
import { META_RUNS_RECOVERY_METHOD } from '@/contracts/generated/v4/metaRunsRecovery'
import { validateResult as validateMetaRunsRecoveryResult } from '@/contracts/generated/v4/metaRunsRecoveryValidators.mjs'
import { META_RUNS_CONFIRM_PREFLIGHT_METHOD } from '@/contracts/generated/v4/metaRunsConfirmPreflight'
import { validateResult as validateMetaRunsConfirmResult } from '@/contracts/generated/v4/metaRunsConfirmPreflightValidators.mjs'
import { META_RUNS_REPLAY_METHOD } from '@/contracts/generated/v4/metaRunsReplay'
import { validateResult as validateMetaRunsReplayResult } from '@/contracts/generated/v4/metaRunsReplayValidators.mjs'
import { META_SETUP_PLAN_METHOD } from '@/contracts/generated/v4/metaSetupPlan'
import { validateResult as validateMetaSetupPlanResult } from '@/contracts/generated/v4/metaSetupPlanValidators.mjs'
import { META_SETUP_STATUS_METHOD } from '@/contracts/generated/v4/metaSetupStatus'
import { validateResult as validateMetaSetupStatusResult } from '@/contracts/generated/v4/metaSetupStatusValidators.mjs'
import { META_SETUP_INSTALL_METHOD } from '@/contracts/generated/v4/metaSetupInstall'
import { validateResult as validateMetaSetupInstallResult } from '@/contracts/generated/v4/metaSetupInstallValidators.mjs'
import { decodeConversationEvent } from './conversationEventsV4'

/**
 * Meta wire names are intentionally private to this adapter.  They remain
 * v4-compatible aliases until the Meta Contract schemas are promoted; no
 * composable or page should need to know these strings.
 */
const METHODS = Object.freeze({
  launch: META_RUN_METHOD,
  drafts: META_DRAFTS_LIST_METHOD,
  discard: META_DRAFTS_DISCARD_METHOD,
  recovery: META_RUNS_RECOVERY_METHOD,
  confirm: META_RUNS_CONFIRM_PREFLIGHT_METHOD,
  replay: META_RUNS_REPLAY_METHOD,
  setupPlan: META_SETUP_PLAN_METHOD,
  setupStatus: META_SETUP_STATUS_METHOD,
  setupInstall: META_SETUP_INSTALL_METHOD,
})

const EVENTS = Object.freeze([
  ['session.event.meta_preflight', 'preflight'],
  ['meta_preflight', 'preflight'],
  ['session.event.meta_run_announced', 'run-announced'],
  ['meta_run_announced', 'run-announced'],
  ['session.event.meta_step_state', 'step-state'],
  ['meta_step_state', 'step-state'],
  ['session.event.meta_run_completed', 'run-completed'],
  ['meta_run_completed', 'run-completed'],
] as const)

interface MetaRunTransport {
  request<T = unknown>(method: string, params?: Record<string, unknown>, options?: RpcCallOptions): Promise<T>
  ready?(options?: { timeoutMs?: number; signal?: AbortSignal; abortAction?: 'reject' | 'reconnect'; timeoutAction?: 'reject' | 'reconnect' }): Promise<void>
  supports?(method: string): boolean
  markUnsupported?(method: string): void
}

interface MetaEventTransport {
  subscribe(event: string, handler: RpcEventHandler): { close(): void }
}

type JsonObject = Record<string, unknown>

function object(value: unknown): JsonObject {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as JsonObject
    : {}
}

function isObject(value: unknown): value is JsonObject {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value)
}

function text(...values: unknown[]): string | null {
  for (const value of values) {
    if (typeof value === 'string' && value.trim()) return value.trim()
  }
  return null
}

function integer(...values: unknown[]): number | null {
  for (const value of values) {
    if (typeof value === 'number' && Number.isInteger(value) && value >= 0) return value
  }
  return null
}

function optionalText(...values: unknown[]): string | undefined {
  for (const value of values) {
    if (typeof value === 'string') return value
  }
  return undefined
}

function numberValue(...values: unknown[]): number | undefined {
  for (const value of values) {
    if (typeof value === 'number' && Number.isFinite(value)) return value
  }
  return undefined
}

function booleanValue(...values: unknown[]): boolean | undefined {
  for (const value of values) {
    if (typeof value === 'boolean') return value
  }
  return undefined
}

function nullableText(...values: unknown[]): string | null | undefined {
  for (const value of values) {
    if (value === null) return null
    if (typeof value === 'string') return value
  }
  return undefined
}

function stringList(...values: unknown[]): string[] | undefined {
  for (const value of values) {
    if (Array.isArray(value)) return value.filter((item): item is string => typeof item === 'string')
  }
  return undefined
}

function objectList(value: unknown): JsonObject[] {
  return Array.isArray(value) ? value.filter(isObject) : []
}

function setupFailure(value: unknown, fallback: string): MetaRunCenterError {
  if (value instanceof MetaRunCenterError && value.code !== 'unavailable') return value
  const message = value instanceof Error ? value.message : text(value) || fallback
  if (/(?:not found|404|unknown (?:meta )?setup job|setup job (?:is )?unknown)/i.test(message)) {
    return new MetaRunCenterError('not-found', message, value)
  }
  return value instanceof MetaRunCenterError ? value : new MetaRunCenterError('unavailable', message, value)
}

function setupResult(value: unknown, validator: (candidate: unknown) => boolean): JsonObject {
  if (!validator(value) || !isObject(value)) {
    throw new MetaRunCenterError('invalid', 'MetaSkill setup returned an invalid result')
  }
  if (value.ok === false || (typeof value.error === 'string' && !value.job && !value.readiness)) {
    throw setupFailure(value.error, 'MetaSkill setup is unavailable')
  }
  return value
}

function setupReadiness(value: unknown): MetaSetupReadiness {
  if (!isObject(value)) throw new MetaRunCenterError('invalid', 'MetaSkill readiness is unavailable')
  const missingEnvAny = value.missing_env_any ?? value.missingEnvAny
  // These names also belong to durable setup checkpoints; only the Adapter reads wire aliases.
  const result: MetaSetupReadiness = {
    ready: booleanValue(value.ready), status: optionalText(value.status),
    missing_bins: stringList(value.missing_bins, value.missingBins),
    missing_env: stringList(value.missing_env, value.missingEnv),
    missing_env_any: Array.isArray(missingEnvAny)
      ? missingEnvAny.filter(Array.isArray).map(values => stringList(values) || []) : undefined,
    missing_skills: stringList(value.missing_skills, value.missingSkills),
    missing_capabilities: stringList(value.missing_capabilities, value.missingCapabilities),
    missing_provider_capabilities: stringList(value.missing_provider_capabilities, value.missingProviderCapabilities),
    reasons: stringList(value.reasons),
  }
  const actions = value.setup_actions ?? value.setupActions
  if (Array.isArray(actions)) result.setup_actions = objectList(actions).flatMap(action => {
    const id = text(action.id)
    return id ? [{
      id, skill: optionalText(action.skill), install_id: optionalText(action.install_id, action.installId),
      kind: optionalText(action.kind), label: optionalText(action.label), bins: stringList(action.bins),
      available: booleanValue(action.available), reason: optionalText(action.reason),
      version: optionalText(action.version),
      download_size_bytes: action.download_size_bytes === null || action.downloadSizeBytes === null
        ? null : numberValue(action.download_size_bytes, action.downloadSizeBytes),
      download_size_is_minimum: booleanValue(action.download_size_is_minimum, action.downloadSizeIsMinimum),
      source: optionalText(action.source), license: optionalText(action.license),
      requires_admin: booleanValue(action.requires_admin, action.requiresAdmin),
    }] : []
  })
  const manual = value.manual_setup_actions ?? value.manualSetupActions
  if (Array.isArray(manual)) result.manual_setup_actions = objectList(manual).flatMap(action => {
    const id = text(action.id), kind = text(action.kind)
    return id && kind ? [{
      id, kind, provider_id: optionalText(action.provider_id, action.providerId),
      label: optionalText(action.label), capability_ids: stringList(action.capability_ids, action.capabilityIds),
      reason_code: optionalText(action.reason_code, action.reasonCode), recommended: booleanValue(action.recommended),
      available: booleanValue(action.available), reason: optionalText(action.reason),
    }] : []
  })
  return result
}

function setupJob(value: unknown): MetaSetupJob {
  const source = object(value)
  const id = text(source.job_id, source.jobId), name = text(source.name)
  const sessionKey = text(source.sessionKey, source.session_key)
  const status = text(source.status), phase = text(source.phase)
  if (!id || !name || !sessionKey || !status || !phase) {
    throw new MetaRunCenterError('invalid', 'MetaSkill setup job is unavailable')
  }
  return {
    job_id: id, name, sessionKey, status, phase,
    action_ids: stringList(source.action_ids, source.actionIds) || [],
    message: optionalText(source.message), current_action: optionalText(source.current_action, source.currentAction),
    downloaded_bytes: numberValue(source.downloaded_bytes, source.downloadedBytes),
    download_total_bytes: numberValue(source.download_total_bytes, source.downloadTotalBytes),
    completed_actions: stringList(source.completed_actions, source.completedActions),
    error: optionalText(source.error), started_at_ms: numberValue(source.started_at_ms, source.startedAtMs),
    finished_at_ms: numberValue(source.finished_at_ms, source.finishedAtMs),
    readiness: source.readiness == null ? source.readiness : setupReadiness(source.readiness),
  }
}

function mapPreflightField(value: unknown): MetaPreflightFieldSpec {
  const source = object(value)
  return {
    name: optionalText(source.name),
    label: optionalText(source.label),
    title: optionalText(source.title),
    type: optionalText(source.type),
    kind: optionalText(source.kind),
    multiline: booleanValue(source.multiline),
    required: booleanValue(source.required),
    default: source.default,
    description: optionalText(source.description),
    help: optionalText(source.help),
    hint: optionalText(source.hint),
    options: Array.isArray(source.options) ? [...source.options] : undefined,
    choices: Array.isArray(source.choices) ? [...source.choices] : undefined,
  }
}

function mapPreflight(value: unknown): MetaPreflightPayload {
  const source = object(value)
  const template = object(source.requestTemplate ?? source.request_template)
  return {
    runId: optionalText(source.runId, source.run_id),
    metaSkillName: optionalText(source.metaSkillName, source.meta_skill_name),
    language: optionalText(source.language),
    interpretedRequest: optionalText(source.interpretedRequest, source.interpreted_request),
    missingFields: stringList(source.missingFields, source.missing_fields),
    assumptions: stringList(source.assumptions),
    requestTemplate: Object.keys(template).length > 0 ? {
      language: optionalText(template.language),
      outcome: optionalText(template.outcome),
      deliverable: optionalText(template.deliverable),
      fields: objectList(template.fields).map(mapPreflightField),
    } : undefined,
    canSkip: booleanValue(source.canSkip, source.can_skip),
    requiresConfirmation: booleanValue(
      source.requiresConfirmation,
      source.requires_confirmation,
    ),
  }
}

function mapRunStep(value: unknown): MetaRunStepSpec {
  const source = object(value)
  return {
    id: optionalText(source.id),
    label: optionalText(source.label),
    kind: optionalText(source.kind),
    dependsOn: stringList(source.dependsOn, source.depends_on),
  }
}

function mapRunAnnounced(value: unknown): MetaRunAnnouncedPayload {
  const source = object(value)
  return {
    runId: optionalText(source.runId, source.run_id),
    metaSkillName: optionalText(source.metaSkillName, source.meta_skill_name),
    language: optionalText(source.language),
    userLanguage: optionalText(source.userLanguage, source.user_language),
    metaLanguage: optionalText(source.metaLanguage, source.meta_language),
    steps: objectList(source.steps).map(mapRunStep),
    total: numberValue(source.total),
  }
}

function mapRescueAction(value: unknown): MetaStepRescueAction {
  const source = object(value)
  return { id: optionalText(source.id), label: optionalText(source.label) }
}

function mapRescue(value: unknown): MetaStepRescue | undefined {
  if (!isObject(value)) return undefined
  return { actions: objectList(value.actions).map(mapRescueAction) }
}

function mapStepState(value: unknown): MetaStepStatePayload {
  const source = object(value)
  return {
    runId: optionalText(source.runId, source.run_id),
    stepId: optionalText(source.stepId, source.step_id),
    state: optionalText(source.state),
    statusText: nullableText(source.statusText, source.status_text),
    error: optionalText(source.error),
    substituteFor: nullableText(source.substituteFor, source.substitute_for),
    rescue: mapRescue(source.rescue),
  }
}

function mapRunCompleted(value: unknown): MetaRunCompletedPayload {
  const source = object(value)
  return {
    runId: optionalText(source.runId, source.run_id),
    outcome: optionalText(source.outcome),
    completedSteps: stringList(source.completedSteps, source.completed_steps),
    failedSteps: stringList(source.failedSteps, source.failed_steps),
    recoveredSteps: stringList(source.recoveredSteps, source.recovered_steps),
    skippedSteps: stringList(source.skippedSteps, source.skipped_steps),
  }
}

function callOptions(options?: MetaRunRequestOptions): RpcCallOptions | undefined {
  return options?.signal
    ? { signal: options.signal, abortAction: 'reject', timeoutAction: 'reject' }
    : undefined
}

function mapMetaRunError(error: unknown): MetaRunCenterError {
  if (error instanceof MetaRunCenterError) return error
  const failure = readTransportFailure(error)
  const code = failure.code
  const domainCode = code === 'META_DRAFT_DISCARDED'
    ? 'draft-discarded'
    : code === 'METHOD_NOT_FOUND'
      ? 'unsupported'
      : code === 'NOT_FOUND'
        ? 'not-found'
        : code === 'UNAUTHORIZED' || code === 'FORBIDDEN'
          ? 'forbidden'
          : code?.includes('CONFLICT')
            ? 'conflict'
            : code?.startsWith('INVALID_')
              ? 'invalid'
              : 'unavailable'
  return new MetaRunCenterError(domainCode, failure.message, error)
}

function guardedMetaRunTransport(transport: MetaRunTransport): MetaRunTransport {
  return {
    request: async (method, params, options) => {
      try {
        return await transport.request(method, params, options)
      } catch (error) {
        throw mapMetaRunError(error)
      }
    },
    ...(transport.ready
      ? {
          ready: async options => {
            try {
              await transport.ready?.(options)
            } catch (error) {
              throw mapMetaRunError(error)
            }
          },
        }
      : {}),
    ...(transport.supports ? { supports: method => transport.supports?.(method) === true } : {}),
    ...(transport.markUnsupported
      ? { markUnsupported: method => transport.markUnsupported?.(method) }
      : {}),
  }
}

function validated<T>(value: unknown, method: string, validator: (candidate: unknown) => boolean): T {
  if (!validator(value)) throw new Error(`${method} returned an invalid response`)
  return value as T
}

async function ready(transport: MetaRunTransport, signal?: AbortSignal): Promise<void> {
  await transport.ready?.({ timeoutMs: 15_000, signal, abortAction: 'reject', timeoutAction: 'reject' })
}

function mapDraft(value: unknown): MetaDraft | null {
  const source = object(value)
  const sessionKey = text(source.sessionKey, source.session_key)
  const clientRequestId = text(source.clientRequestId, source.client_request_id)
  const name = text(source.name, source.metaSkillName, source.meta_skill_name)
  const launchText = typeof source.launchText === 'string'
    ? source.launchText
    : typeof source.launch_text === 'string' ? source.launch_text : ''
  const createdAt = integer(source.createdAt, source.created_at)
  const expiresAt = integer(source.expiresAt, source.expires_at)
  if (!sessionKey || !clientRequestId || !name || createdAt === null || expiresAt === null) return null
  return {
    sessionKey,
    clientRequestId,
    name,
    launchText,
    createdAt,
    expiresAt,
    sessionExists: source.sessionExists === true || source.session_exists === true,
  }
}

function mapReplay(value: unknown): MetaReplay {
  const source = object(value)
  const nested = object(source.replay)
  const replay = Object.keys(nested).length > 0 ? nested : source
  const live = object(replay.live_replay ?? replay.liveReplay)
  return {
    message: typeof replay.message === 'string' ? replay.message : undefined,
    launchText: typeof replay.launch_text === 'string'
      ? replay.launch_text
      : typeof replay.launchText === 'string' ? replay.launchText : undefined,
    displayText: typeof replay.display_text === 'string'
      ? replay.display_text
      : typeof replay.displayText === 'string' ? replay.displayText : undefined,
    liveReplay: {
      available: live.available === true,
      replayToken: text(live.replay_token, live.replayToken) ?? undefined,
      committed: live.committed === true,
    },
  }
}

function mapRecovery(value: unknown): MetaRunRecovery | null {
  const source = object(value)
  const recovery = object(source.recovery)
  if (!Object.keys(recovery).length) return null
  const steps = objectList(recovery.stepStates ?? recovery.step_states).map(mapStepState)
  const announced = isObject(recovery.announced)
    ? mapRunAnnounced(recovery.announced) : undefined
  const completed = isObject(recovery.completed)
    ? mapRunCompleted(recovery.completed) : undefined
  return { announced, stepStates: steps, completed }
}

function eventProjection(kind: MetaEvent['kind'], payload: unknown, meta: unknown): MetaEvent {
  const decoded = decodeConversationEvent(
    kind === 'preflight' ? 'session.event.meta_preflight'
      : kind === 'run-announced' ? 'session.event.meta_run_announced'
        : kind === 'step-state' ? 'session.event.meta_step_state'
          : 'session.event.meta_run_completed',
    payload,
    meta,
  )
  const body = decoded.payload ?? {}
  const context = {
    sessionKey: decoded.sessionKey,
    sessionEpoch: integer(body.sessionEpoch, body.session_epoch, body.epoch),
    streamSeq: decoded.streamSeq,
    streamGeneration: decoded.streamGeneration,
  }
  if (kind === 'preflight') return { ...context, kind, payload: mapPreflight(body) }
  if (kind === 'run-announced') return { ...context, kind, payload: mapRunAnnounced(body) }
  if (kind === 'step-state') return { ...context, kind, payload: mapStepState(body) }
  return { ...context, kind, payload: mapRunCompleted(body) }
}

export function createV4MetaRunCenter(
  transport: MetaRunTransport,
  events: MetaEventTransport,
): MetaRunCenter {
  const rpc = guardedMetaRunTransport(transport)
  return {
    async launch(input, options): Promise<MetaLaunchResult> {
      await ready(rpc, options?.signal)
      const result = validated<JsonObject>(await rpc.request(METHODS.launch, {
        name: input.name,
        sessionKey: input.sessionKey,
        ...(input.clientRequestId ? { clientRequestId: input.clientRequestId } : {}),
        ...(input.launchText !== undefined ? { launchText: input.launchText } : {}),
      }, callOptions(options)), METHODS.launch, validateMetaRunResult)
      const source = object(result)
      return {
        ok: source.ok === true,
        name: text(source.name) ?? undefined,
        sessionKey: text(source.sessionKey, source.session_key) ?? undefined,
        clientRequestId: text(source.clientRequestId, source.client_request_id) ?? undefined,
        replayed: source.replayed === true,
        drafted: source.drafted === true,
        setupRequired: source.setup_required === true || source.setupRequired === true,
        readiness: source.readiness && typeof source.readiness === 'object' ? source.readiness as MetaSetupReadiness : undefined,
        error: typeof source.error === 'string' ? source.error : undefined,
      }
    },

    async listDrafts(query, options): Promise<MetaDraftListResult> {
      await ready(rpc, options?.signal)
      if (rpc.supports && !rpc.supports(METHODS.drafts)) {
        rpc.markUnsupported?.(METHODS.drafts)
        return { drafts: [], durable: false }
      }
      const result = validated<JsonObject>(await rpc.request(METHODS.drafts, query as JsonObject, callOptions(options)), METHODS.drafts, validateMetaDraftsListResult)
      const drafts = Array.isArray(result?.drafts)
        ? result.drafts.map(mapDraft).filter((item): item is MetaDraft => Boolean(item))
        : []
      return { drafts, durable: result?.durable === true }
    },

    async discardDraft(input, options): Promise<MetaDraftDiscardResult> {
      await ready(rpc, options?.signal)
      const result = object(validated(await rpc.request(METHODS.discard, input, callOptions(options)), METHODS.discard, validateMetaDraftsDiscardResult))
      return { discarded: result.discarded === true, accepted: result.accepted === true }
    },

    async recover(sessionKey, options): Promise<MetaRunRecovery | null> {
      await ready(rpc, options?.signal)
      return mapRecovery(validated(await rpc.request(METHODS.recovery, { sessionKey }, callOptions(options)), METHODS.recovery, validateMetaRunsRecoveryResult))
    },

    async confirmPreflight(input: MetaPreflightInput, options): Promise<MetaPreflightConfirmation> {
      await ready(rpc, options?.signal)
      const result = object(validated(await rpc.request(METHODS.confirm, {
        sessionKey: input.sessionKey,
        runId: input.runId,
        run_id: input.runId,
        ...(input.interpretedRequest !== undefined ? { interpretedRequest: input.interpretedRequest } : {}),
        ...(input.fields !== undefined ? { fields: input.fields } : {}),
        ...(input.useDefaults !== undefined ? { useDefaults: input.useDefaults } : {}),
      }, callOptions(options)), METHODS.confirm, validateMetaRunsConfirmResult))
      return { message: typeof result.message === 'string' ? result.message : undefined }
    },

    async replay(input: MetaReplayInput, options): Promise<MetaReplay> {
      await ready(rpc, options?.signal)
      const result = validated(await rpc.request(METHODS.replay, {
        sessionKey: input.sessionKey,
        runId: input.runId,
        run_id: input.runId,
        mode: input.mode,
        ...(input.action ? { action: input.action } : {}),
        ...(input.stepId ? { stepId: input.stepId } : {}),
        ...(input.prepareLive ? { prepareLive: true } : {}),
        ...(input.replayToken ? { replayToken: input.replayToken } : {}),
      }, callOptions(options)), METHODS.replay, validateMetaRunsReplayResult)
      return mapReplay(result)
    },

    async setupPlan(name: string, options?: MetaRunRequestOptions): Promise<MetaSetupPlan> {
      await ready(rpc, options?.signal)
      const result = setupResult(await rpc.request(METHODS.setupPlan, { name }, callOptions(options)), validateMetaSetupPlanResult)
      return { readiness: setupReadiness(result.readiness) }
    },

    async setupStatus(input: { jobId: string; sessionKey: string }, options?: MetaRunRequestOptions): Promise<MetaSetupStatus> {
      await ready(rpc, options?.signal)
      try {
        const result = setupResult(await rpc.request(METHODS.setupStatus, input, callOptions(options)), validateMetaSetupStatusResult)
        return { job: setupJob(result.job) }
      } catch (error) {
        throw setupFailure(error, 'Setup status is unavailable')
      }
    },

    async setupInstall(input: { name: string; sessionKey: string; confirmed: boolean; actionIds: readonly string[] }, options?: MetaRunRequestOptions): Promise<MetaSetupInstallation> {
      await ready(rpc, options?.signal)
      const result = setupResult(await rpc.request(METHODS.setupInstall, {
        name: input.name,
        sessionKey: input.sessionKey,
        confirmed: input.confirmed,
        action_ids: input.actionIds,
      }, callOptions(options)), validateMetaSetupInstallResult)
      return result.already_ready === true || result.alreadyReady === true
        ? { alreadyReady: true, readiness: result.readiness == null ? undefined : setupReadiness(result.readiness) }
        : { job: setupJob(result.job) }
    },

    subscribe(listener): { close(): void } {
      const handles = EVENTS.map(([name, kind]) => events.subscribe(name, ((payload: unknown, meta?: unknown) => {
        try { listener(eventProjection(kind, payload, meta)) } catch { /* malformed event is isolated */ }
      }) as RpcEventHandler))
      return { close: () => handles.forEach(handle => handle.close()) }
    },
  }
}
