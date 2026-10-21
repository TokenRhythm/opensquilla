import { computed, ref } from 'vue'
import type { SessionCreateRequest, SessionLifecycle } from '@/modules/sessionLifecycle'
import type { SessionModelSelection, SessionRouting } from '@/modules/sessionRouting'
import type { GatewayModelRoutingMode } from '@/types/modelRouting'
import { PlanModeConflictError, type PlanCenter } from '@/modules/planCenter'
import type { CollaborationMode } from '@/types/plans'

export async function configureWorkbenchSessionCollaboration(
  plans: PlanCenter,
  sessionKey: string,
  mode: CollaborationMode,
  expectedRevision: number,
  isCurrent: () => boolean,
): Promise<void> {
  try {
    await plans.setMode(sessionKey, mode, expectedRevision)
  } catch (error) {
    if (!(error instanceof PlanModeConflictError) || !isCurrent()) throw error
    // A lost response may already have committed this exact desired mode.
    if (error.current.mode === mode) return
    // Reconcile a prior partial attempt only once using its authoritative CAS
    // revision. Further conflicts surface normally; navigation cancels retry.
    await plans.setMode(sessionKey, mode, error.current.revision)
  }
}

/** A create retry can encounter the empty row made before a previous failure. */
export async function configureWorkbenchSessionRouting(
  routing: SessionRouting,
  sessionKey: string,
  mode: GatewayModelRoutingMode | null,
  modelSelection: SessionModelSelection | null,
  isCurrent: () => boolean,
): Promise<void> {
  const snapshot = await routing.get(sessionKey)
  if (!isCurrent()) return
  const nextMode = mode ?? snapshot.mode
  const sameModel = (snapshot.modelSelection?.model ?? null) === (modelSelection?.model ?? null)
    && (snapshot.modelSelection?.provider ?? null) === (modelSelection?.provider ?? null)
  if (snapshot.mode === nextMode && sameModel) return
  await routing.set({
    sessionKey, mode: nextMode, expectedRevision: snapshot.revision, modelSelection,
  })
}

export interface WorkbenchSessionPreparationState {
  key: string
  durable: boolean
  canCreate: boolean
  /** Includes connection, route, project and initial model/routing ownership. */
  scope: string
  request: SessionCreateRequest
}

/** Explicit panel activation materializes a draft; mounting the sidebar never does. */
export function useWorkbenchSessionPreparation(options: {
  state: () => WorkbenchSessionPreparationState
  lifecycle: Pick<SessionLifecycle, 'create'>
  configure: (state: WorkbenchSessionPreparationState, isCurrent: () => boolean) => Promise<void>
  commit: (key: string) => Promise<boolean>
}) {
  const busy = ref(false)
  let pending: { scope: string; promise: Promise<string | null> } | null = null

  function prepare(expectedKey: string): Promise<string | null> {
    const state = options.state()
    if (!expectedKey || state.key !== expectedKey) return Promise.resolve(null)
    if (state.durable) return Promise.resolve(state.key)
    if (pending) return pending.scope === state.scope ? pending.promise : Promise.resolve(null)
    if (!state.canCreate) return Promise.resolve(null)
    const current = () => {
      const next = options.state()
      return next.key === state.key && next.scope === state.scope && next.canCreate && !next.durable
    }
    busy.value = true
    // Install the single-flight record before any lifecycle callback can re-enter.
    const promise = Promise.resolve().then(async () => {
      if (!current()) return null
      const created = await options.lifecycle.create({ ...state.request, sessionKey: state.key })
      if (!current()) return null
      if (created.key !== state.key) throw new Error('Gateway did not preserve the draft session identity')
      await options.configure(state, current)
      if (!current()) return null
      return await options.commit(state.key) ? state.key : null
    }).finally(() => {
      if (pending?.promise === promise) {
        pending = null
        busy.value = false
      }
    })
    pending = { scope: state.scope, promise }
    return promise
  }

  const canPrepare = computed(() => {
    const state = options.state()
    return state.durable || state.canCreate
  })
  return { busy, canPrepare, prepare }
}
