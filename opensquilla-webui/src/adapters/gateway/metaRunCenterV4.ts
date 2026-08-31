import type { RpcCallOptions, RpcEventHandler } from '@/lib/rpc'
import type {
  MetaDraft,
  MetaDraftDiscardResult,
  MetaDraftListResult,
  MetaEvent,
  MetaLaunchResult,
  MetaPreflightConfirmation,
  MetaPreflightInput,
  MetaReplay,
  MetaReplayInput,
  MetaRunCenter,
  MetaRunRecovery,
  MetaRunRequestOptions,
} from '@/modules/metaRunCenter'
import type { MetaSetupReadiness } from '@/types/metaSetup'
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

function callOptions(options?: MetaRunRequestOptions): RpcCallOptions | undefined {
  return options?.signal
    ? { signal: options.signal, abortAction: 'reject', timeoutAction: 'reject' }
    : undefined
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
  const steps = Array.isArray(recovery.step_states)
    ? recovery.step_states.filter(item => item && typeof item === 'object' && !Array.isArray(item)) as JsonObject[]
    : Array.isArray(recovery.stepStates)
      ? recovery.stepStates.filter(item => item && typeof item === 'object' && !Array.isArray(item)) as JsonObject[]
      : []
  const announced = recovery.announced && typeof recovery.announced === 'object'
    ? recovery.announced as JsonObject : undefined
  const completed = recovery.completed && typeof recovery.completed === 'object'
    ? recovery.completed as JsonObject : undefined
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
  return {
    kind,
    payload: body,
    sessionKey: decoded.sessionKey,
    streamSeq: decoded.streamSeq,
    streamGeneration: decoded.streamGeneration,
  }
}

export function createV4MetaRunCenter(
  transport: MetaRunTransport,
  events: MetaEventTransport,
): MetaRunCenter {
  return {
    async launch(input, options): Promise<MetaLaunchResult> {
      await ready(transport, options?.signal)
      const result = validated<JsonObject>(await transport.request(METHODS.launch, {
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
      await ready(transport, options?.signal)
      if (transport.supports && !transport.supports(METHODS.drafts)) {
        transport.markUnsupported?.(METHODS.drafts)
        return { drafts: [], durable: false }
      }
      const result = validated<JsonObject>(await transport.request(METHODS.drafts, query as JsonObject, callOptions(options)), METHODS.drafts, validateMetaDraftsListResult)
      const drafts = Array.isArray(result?.drafts)
        ? result.drafts.map(mapDraft).filter((item): item is MetaDraft => Boolean(item))
        : []
      return { drafts, durable: result?.durable === true }
    },

    async discardDraft(input, options): Promise<MetaDraftDiscardResult> {
      await ready(transport, options?.signal)
      const result = object(validated(await transport.request(METHODS.discard, input, callOptions(options)), METHODS.discard, validateMetaDraftsDiscardResult))
      return { discarded: result.discarded === true, accepted: result.accepted === true }
    },

    async recover(sessionKey, options): Promise<MetaRunRecovery | null> {
      await ready(transport, options?.signal)
      return mapRecovery(validated(await transport.request(METHODS.recovery, { sessionKey }, callOptions(options)), METHODS.recovery, validateMetaRunsRecoveryResult))
    },

    async confirmPreflight(input: MetaPreflightInput, options): Promise<MetaPreflightConfirmation> {
      await ready(transport, options?.signal)
      const result = object(validated(await transport.request(METHODS.confirm, {
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
      await ready(transport, options?.signal)
      const result = validated(await transport.request(METHODS.replay, {
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

    async setupPlan(name: string, options?: MetaRunRequestOptions): Promise<JsonObject> {
      await ready(transport, options?.signal)
      return object(validated(await transport.request(METHODS.setupPlan, { name }, callOptions(options)), METHODS.setupPlan, validateMetaSetupPlanResult))
    },

    async setupStatus(input: { jobId: string; sessionKey: string }, options?: MetaRunRequestOptions): Promise<JsonObject> {
      await ready(transport, options?.signal)
      return object(validated(await transport.request(METHODS.setupStatus, input, callOptions(options)), METHODS.setupStatus, validateMetaSetupStatusResult))
    },

    async setupInstall(input: { name: string; sessionKey: string; confirmed: boolean; actionIds: readonly string[] }, options?: MetaRunRequestOptions): Promise<JsonObject> {
      await ready(transport, options?.signal)
      return object(validated(await transport.request(METHODS.setupInstall, {
        name: input.name,
        sessionKey: input.sessionKey,
        confirmed: input.confirmed,
        action_ids: input.actionIds,
      }, callOptions(options)), METHODS.setupInstall, validateMetaSetupInstallResult))
    },

    subscribe(listener): { close(): void } {
      const handles = EVENTS.map(([name, kind]) => events.subscribe(name, ((payload: unknown, meta?: unknown) => {
        try { listener(eventProjection(kind, payload, meta)) } catch { /* malformed event is isolated */ }
      }) as RpcEventHandler))
      return { close: () => handles.forEach(handle => handle.close()) }
    },
  }
}
