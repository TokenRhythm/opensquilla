import type {
  TransportCallOptions as RpcCallOptions,
  TransportEventHandler as RpcEventHandler,
} from './transportTypes'
import type { PlanCardActionTarget, PlanRevisionRequest } from '@/types/plans'
import type { PlanCenter, PlanEvent, PlanMutationResult } from '@/modules/planCenter'
import { PLANS_SET_MODE_METHOD } from '@/contracts/generated/v4/plansSetMode'
import { validateResult as validateSetModeResult } from '@/contracts/generated/v4/plansSetModeValidators.mjs'
import { PLANS_REVISE_METHOD } from '@/contracts/generated/v4/plansRevise'
import { validateResult as validateReviseResult } from '@/contracts/generated/v4/plansReviseValidators.mjs'
import { PLANS_IMPLEMENT_METHOD } from '@/contracts/generated/v4/plansImplement'
import { validateResult as validateImplementResult } from '@/contracts/generated/v4/plansImplementValidators.mjs'
import { PLANS_CANCEL_RUN_METHOD } from '@/contracts/generated/v4/plansCancelRun'
import { validateResult as validateCancelRunResult } from '@/contracts/generated/v4/plansCancelRunValidators.mjs'

interface PlanTransport {
  request<T = unknown>(method: string, params?: Record<string, unknown>, options?: RpcCallOptions): Promise<T>
  supports?(method: string): boolean
}
interface PlanEvents { subscribe(event: string, handler: RpcEventHandler): { close(): void } }
type JsonObject = Record<string, unknown>

function object(value: unknown): JsonObject {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as JsonObject : {}
}
function text(value: unknown): string | undefined { return typeof value === 'string' && value.trim() ? value : undefined }
function optionsFor(signal?: AbortSignal): RpcCallOptions | undefined {
  return signal ? { signal, abortAction: 'reject', timeoutAction: 'reject' } : undefined
}

function normalizeResult(value: unknown): PlanMutationResult {
  const source = object(value)
  return {
    ...source,
    accepted: typeof source.accepted === 'boolean' ? source.accepted : undefined,
    replayed: typeof source.replayed === 'boolean' ? source.replayed : undefined,
    sessionKey: text(source.sessionKey) ?? text(source.session_key),
    collaboration: source.collaboration as PlanMutationResult['collaboration'],
    currentPlan: (source.currentPlan ?? source.current_plan ?? source.planRevision ?? source.plan_revision) as PlanMutationResult['currentPlan'],
    planRevision: (source.planRevision ?? source.plan_revision ?? source.currentPlan ?? source.current_plan) as PlanMutationResult['planRevision'],
    planRun: (source.planRun ?? source.plan_run) as PlanMutationResult['planRun'],
    activePlanRun: (source.activePlanRun ?? source.active_plan_run ?? source.planRun ?? source.plan_run) as PlanMutationResult['activePlanRun'],
  }
}

function requestResult<T>(
  transport: PlanTransport,
  method: string,
  validator: (value: unknown) => boolean,
  params: Record<string, unknown>,
  signal?: AbortSignal,
): Promise<PlanMutationResult> {
  return transport.request<T>(method, params, optionsFor(signal)).then(value => {
    if (!validator(value)) throw new Error(`${method} returned an invalid response`)
    return normalizeResult(value)
  })
}

function event(kind: PlanEvent['kind'], payload: unknown): PlanEvent {
  const source = object(payload)
  return {
    kind,
    sessionKey: text(source.sessionKey) ?? text(source.session_key) ?? text(source.key),
    collaboration: source.collaboration as PlanEvent['collaboration'],
    plan: (source.planRevision ?? source.plan_revision ?? source.currentPlan ?? source.current_plan ?? source.plan) as PlanEvent['plan'],
    run: (source.planRun ?? source.plan_run ?? source.run) as PlanEvent['run'],
  }
}

export function createV4PlanCenter(transport: PlanTransport, events: PlanEvents): PlanCenter {
  return {
    available(operation = 'mutations') {
      if (!transport.supports) return true
      if (operation === 'mode') return transport.supports(PLANS_SET_MODE_METHOD) && transport.supports('plans.capabilities')
      return transport.supports(PLANS_SET_MODE_METHOD)
        && transport.supports(PLANS_REVISE_METHOD)
        && transport.supports(PLANS_IMPLEMENT_METHOD)
        && transport.supports(PLANS_CANCEL_RUN_METHOD)
    },
    setMode(sessionKey, mode, expectedRevision, options) {
      return requestResult(transport, PLANS_SET_MODE_METHOD, validateSetModeResult, { sessionKey, mode, expectedRevision }, options?.signal)
    },
    revise(sessionKey, request: PlanRevisionRequest, clientRequestId, options) {
      return requestResult(transport, PLANS_REVISE_METHOD, validateReviseResult, {
        sessionKey, planRevisionId: request.revisionId, prompt: request.prompt.trim(), clientRequestId,
      }, options?.signal)
    },
    implement(sessionKey, target: PlanCardActionTarget, clientRequestId, options) {
      return requestResult(transport, PLANS_IMPLEMENT_METHOD, validateImplementResult, {
        sessionKey, planRevisionId: target.revisionId, clientRequestId,
        ...(options?.intent ? { intent: options.intent } : {}),
      }, options?.signal)
    },
    cancelRun(sessionKey, runId, expectedStateRevision, options) {
      return requestResult(transport, PLANS_CANCEL_RUN_METHOD, validateCancelRunResult, {
        sessionKey, runId,
        ...(expectedStateRevision !== undefined ? { expectedStateRevision } : {}),
      }, options?.signal)
    },
    subscribe(listener) {
      const subscriptions = [
        ['session.event.collaboration_mode', 'collaboration'],
        ['collaboration_mode', 'collaboration'],
        ['session.event.plan_revision', 'revision'],
        ['plan_revision', 'revision'],
        ['session.event.plan_run', 'run'],
        ['plan_run', 'run'],
      ] as const
      const handles = subscriptions.map(([name, kind]) => events.subscribe(name, payload => listener(event(kind, payload))))
      return { close: () => handles.forEach(handle => handle.close()) }
    },
  }
}
