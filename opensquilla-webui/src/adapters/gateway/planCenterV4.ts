import type { RpcCallOptions, RpcEventHandler } from '@/lib/rpc'
import type { PlanCardActionTarget, PlanRevisionRequest } from '@/types/plans'
import type { PlanCenter, PlanEvent, PlanMutationResult } from '@/modules/planCenter'

interface PlanTransport {
  request<T = unknown>(method: string, params?: Record<string, unknown>, options?: RpcCallOptions): Promise<T>
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
    setMode(sessionKey, mode, expectedRevision, options) {
      return transport.request('plans.setMode', { sessionKey, mode, expectedRevision }, optionsFor(options?.signal)).then(normalizeResult)
    },
    revise(sessionKey, request: PlanRevisionRequest, clientRequestId, options) {
      return transport.request('plans.revise', {
        sessionKey, planRevisionId: request.revisionId, prompt: request.prompt.trim(), clientRequestId,
      }, optionsFor(options?.signal)).then(normalizeResult)
    },
    implement(sessionKey, target: PlanCardActionTarget, clientRequestId, options) {
      return transport.request('plans.implement', {
        sessionKey, planRevisionId: target.revisionId, clientRequestId,
        ...(options?.intent ? { intent: options.intent } : {}),
      }, optionsFor(options?.signal)).then(normalizeResult)
    },
    cancelRun(sessionKey, runId, expectedStateRevision, options) {
      return transport.request('plans.cancelRun', {
        sessionKey, runId,
        ...(expectedStateRevision !== undefined ? { expectedStateRevision } : {}),
      }, optionsFor(options?.signal)).then(normalizeResult)
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
