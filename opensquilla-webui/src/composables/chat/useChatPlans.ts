import { computed, ref, watch, type Ref } from 'vue'
import type {
  CollaborationMode,
  CollaborationSnapshot,
  PlanCardAction,
  PlanCardActionTarget,
  PlanRevisionRequest,
  PlanRevisionSnapshot,
  PlanRunSnapshot,
} from '@/types/plans'
import type { SessionMessagesSubscribeResponse } from '@/types/rpc'
import { createClientRequestId } from '@/utils/chat/messageIdentity'
import {
  normalizeCollaborationSnapshot,
  normalizePlanRevisionSnapshot,
  normalizePlanRunSnapshot,
  payloadBelongsToSession,
} from '@/utils/chat/plans'

type RpcClient = {
  call: <T = unknown>(method: string, params?: Record<string, unknown>) => Promise<T>
  on: (event: string, handler: (...args: unknown[]) => void) => () => void
}
interface PlanMutationResponse extends Record<string, unknown> {
  sessionKey?: string
  session_key?: string
}

const TERMINAL_RUN_STATUSES = new Set<PlanRunSnapshot['status']>([
  'completed',
  'cancelled',
  'superseded',
])

function objectRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function collaborationRevisionFrom(value: unknown): number | undefined {
  const source = objectRecord(value)
  if (!source) return undefined
  const nested = objectRecord(source.collaboration)
  const plan = objectRecord(
    source.currentPlan
    ?? source.current_plan
    ?? source.planRevision
    ?? source.plan_revision
    ?? source.plan
    ?? source.snapshot,
  )
  for (const candidate of [nested, source, plan]) {
    if (!candidate) continue
    for (const key of ['revision', 'collaborationRevision', 'collaboration_revision']) {
      const raw = candidate[key]
      if (raw === null || raw === undefined || raw === '' || typeof raw === 'boolean') continue
      const revision = Number(raw)
      if (Number.isInteger(revision) && revision >= 0) return revision
    }
  }
  return undefined
}

function shouldAdoptPlanRevision(
  incoming: PlanRevisionSnapshot,
  current: PlanRevisionSnapshot | null,
  incomingCollaborationRevision: number | undefined,
  currentCollaborationRevision: number,
): boolean {
  // A response captured before a newer collaboration mutation cannot move any
  // of the plan pointers backwards, even if it arrives after that mutation.
  if (
    incomingCollaborationRevision !== undefined
    && incomingCollaborationRevision < currentCollaborationRevision
  ) return false
  if (!current) return true
  if (incoming.revisionId === current.revisionId) return false

  // Generation is the authoritative lineage order. It is deliberately only
  // compared inside one plan because independent copied plans both start at 1.
  if (
    incoming.planId === current.planId
    && incoming.generation !== undefined
    && current.generation !== undefined
  ) {
    return incoming.generation > current.generation
  }
  if (incoming.parentRevisionId === current.revisionId) return true
  if (current.parentRevisionId === incoming.revisionId) return false

  // Cross-lineage snapshots are unusual within one session, but can occur
  // after an epoch/reset. Prefer their immutable creation order when present.
  if (incoming.createdAt !== undefined && current.createdAt !== undefined) {
    return incoming.createdAt > current.createdAt
  }
  return true
}

function shouldAdoptSameRun(
  incoming: PlanRunSnapshot,
  current: PlanRunSnapshot,
): boolean {
  const incomingRevision = incoming.stateRevision
  const currentRevision = current.stateRevision
  if (
    incomingRevision !== undefined
    && currentRevision !== undefined
    && incomingRevision < currentRevision
  ) return false

  const currentIsTerminal = TERMINAL_RUN_STATUSES.has(current.status)
  const incomingIsTerminal = TERMINAL_RUN_STATUSES.has(incoming.status)
  // Terminal run states are immutable. A delayed running/paused update must
  // not resurrect them, even if a malformed payload claims a larger revision.
  if (currentIsTerminal && incoming.status !== current.status) return false

  if (
    incomingRevision !== undefined
    && currentRevision !== undefined
    && incomingRevision === currentRevision
    && incoming.status !== current.status
  ) {
    // When duplicate version numbers disagree, only a terminal state may win.
    return incomingIsTerminal && !currentIsTerminal
  }

  if (
    incomingRevision === undefined
    || currentRevision === undefined
  ) {
    const incomingUpdatedAt = incoming.updatedAt
    const currentUpdatedAt = current.updatedAt
    if (
      incomingUpdatedAt !== undefined
      && currentUpdatedAt !== undefined
      && incomingUpdatedAt < currentUpdatedAt
    ) return false
  }
  return true
}

function shouldAdoptPlanRun(
  incoming: PlanRunSnapshot,
  current: PlanRunSnapshot | null,
): boolean {
  if (!current) return true
  if (incoming.runId === current.runId) return shouldAdoptSameRun(incoming, current)

  // stateRevision is local to a run. Distinct runs are ordered by their
  // immutable creation time; never use a large old stateRevision to compare
  // against a newer run.
  if (incoming.createdAt !== undefined && current.createdAt !== undefined) {
    return incoming.createdAt > current.createdAt
  }
  if (incoming.createdAt !== undefined && current.createdAt === undefined) return true
  return false
}

export interface UseChatPlansOptions {
  rpc: RpcClient
  sessionKey: Ref<string>
  currentEpoch: Ref<number>
  isStreaming: Ref<boolean>
  inputText: Ref<string>
  createSessionKey: (agentId?: string) => string
  agentId: () => string
  switchToSession: (sessionKey: string) => void | Promise<unknown>
  focusComposer: () => void
  notifyError: (message: string) => void
  onMutationAccepted?: () => void
  isDraft?: () => boolean
}

export function useChatPlans(options: UseChatPlansOptions) {
  const collaboration = ref<CollaborationSnapshot>({ mode: 'default', revision: 0 })
  const initialCollaborationMode = computed<CollaborationMode>(
    () => collaboration.value.mode,
  )
  const currentPlan = ref<PlanRevisionSnapshot | null>(null)
  const activePlanRun = ref<PlanRunSnapshot | null>(null)
  const modeBusy = ref(false)
  const pendingAction = ref<PlanCardAction | 'cancel-run' | 'revise' | null>(null)
  const modeAppliesNextTurn = ref(false)
  const replanTarget = ref<PlanCardActionTarget | null>(null)

  const currentPlanRevisionId = computed(() => currentPlan.value?.revisionId || '')
  const replanActive = computed(() => replanTarget.value !== null)
  let acceptedEpoch = 0
  let modeMutationOwner: symbol | null = null
  let actionMutationOwner: symbol | null = null

  function clearPlanState() {
    // Reset/session changes invalidate in-flight UI mutations. Their delayed
    // catch/finally blocks must not report into, or unlock, the new epoch.
    modeMutationOwner = null
    actionMutationOwner = null
    collaboration.value = { mode: 'default', revision: 0 }
    currentPlan.value = null
    activePlanRun.value = null
    modeBusy.value = false
    pendingAction.value = null
    modeAppliesNextTurn.value = false
    replanTarget.value = null
  }

  function reset() {
    clearPlanState()
    acceptedEpoch = 0
  }

  function payloadEpoch(value: unknown): number | undefined {
    const source = objectRecord(value)
    const raw = source?.epoch
    if (typeof raw !== 'number' || !Number.isInteger(raw) || raw < 0) return undefined
    return raw
  }

  function acceptEpoch(value: unknown, fallbackToCurrent = false): boolean {
    const incoming = payloadEpoch(value)
      ?? (fallbackToCurrent ? payloadEpoch({ epoch: options.currentEpoch.value }) : undefined)
    if (incoming === undefined) return true
    if (incoming < acceptedEpoch) return false
    if (incoming > acceptedEpoch) {
      // collaboration_revision restarts at zero after reset. Advance the
      // identity fence before applying that snapshot so the old-epoch
      // monotonic gate cannot mistake the reset for a stale response.
      clearPlanState()
      acceptedEpoch = incoming
    }
    if (incoming > options.currentEpoch.value) options.currentEpoch.value = incoming
    return true
  }

  // Session switches can start their subscribe/history requests immediately;
  // clear the prior session's plan pointers synchronously so a fast bootstrap
  // can never be overwritten by a queued reset from the old task.
  watch(options.sessionKey, reset, { flush: 'sync' })
  watch(options.currentEpoch, epoch => {
    if (!Number.isInteger(epoch) || epoch < 0 || epoch <= acceptedEpoch) return
    clearPlanState()
    acceptedEpoch = epoch
  }, { flush: 'sync' })
  watch(options.isStreaming, streaming => {
    if (!streaming) modeAppliesNextTurn.value = false
  })

  function applyCollaboration(
    value: unknown,
    fallback: CollaborationSnapshot = collaboration.value,
  ): boolean {
    const incoming = normalizeCollaborationSnapshot(value, fallback)
    if (incoming.revision < collaboration.value.revision) return false
    if (
      incoming.revision === collaboration.value.revision
      && incoming.mode !== collaboration.value.mode
    ) return false
    collaboration.value = incoming
    return true
  }

  function applyPlanRevision(value: unknown, envelope: unknown = value): boolean {
    const plan = normalizePlanRevisionSnapshot(value)
    if (!plan) return false
    if (!shouldAdoptPlanRevision(
      plan,
      currentPlan.value,
      collaborationRevisionFrom(envelope),
      collaboration.value.revision,
    )) return false
    currentPlan.value = { ...plan, current: true }
    if (
      activePlanRun.value
      && activePlanRun.value.planRevisionId !== plan.revisionId
    ) {
      activePlanRun.value = null
    }
    return true
  }

  function applyPlanRun(value: unknown): boolean {
    const run = normalizePlanRunSnapshot(value)
    if (
      !run
      || !currentPlan.value
      || run.planRevisionId !== currentPlan.value.revisionId
      || !shouldAdoptPlanRun(run, activePlanRun.value)
    ) return false
    activePlanRun.value = run
    return true
  }

  function applyResponse(value: unknown) {
    const source = objectRecord(value) ?? {}
    const incomingCollaborationRevision = collaborationRevisionFrom(source)
    const staleEnvelope = incomingCollaborationRevision !== undefined
      && incomingCollaborationRevision < collaboration.value.revision
    if (source.collaboration !== undefined) {
      applyCollaboration(source)
    }
    const rawPlan = source.currentPlan
      ?? source.current_plan
      ?? source.planRevision
      ?? source.plan_revision
      ?? source.plan
      ?? source.snapshot
    if (rawPlan !== undefined) {
      if (rawPlan !== null) {
        if (!staleEnvelope) {
          applyPlanRevision(rawPlan, source)
        }
      } else if (!staleEnvelope) {
        currentPlan.value = null
        activePlanRun.value = null
      }
    }
    const rawRun = source.activePlanRun
      ?? source.active_plan_run
      ?? source.planRun
      ?? source.plan_run
      ?? source.run
    if (rawRun !== undefined) {
      if (rawRun !== null) {
        if (!staleEnvelope) {
          applyPlanRun(rawRun)
        }
      } else if (!staleEnvelope) {
        activePlanRun.value = null
      }
    }
  }

  function applyBootstrap(snapshot: SessionMessagesSubscribeResponse) {
    if (!payloadBelongsToSession(snapshot, options.sessionKey.value)) return
    if (!acceptEpoch(snapshot, true)) return
    applyResponse(snapshot)
  }

  function applyPlanRevisionEvent(payload: unknown) {
    if (!payloadBelongsToSession(payload, options.sessionKey.value)) return
    if (!acceptEpoch(payload)) return
    applyPlanRevision(payload)
    applyCollaboration(payload)
  }

  function applyPlanRunEvent(payload: unknown) {
    if (!payloadBelongsToSession(payload, options.sessionKey.value)) return
    if (!acceptEpoch(payload)) return
    applyPlanRun(payload)
  }

  function applyCollaborationEvent(payload: unknown) {
    if (!payloadBelongsToSession(payload, options.sessionKey.value)) return
    if (!acceptEpoch(payload)) return
    applyCollaboration(payload)
  }

  function subscribe(): () => void {
    const unsubs = [
      options.rpc.on('session.event.collaboration_mode', applyCollaborationEvent),
      options.rpc.on('collaboration_mode', applyCollaborationEvent),
      options.rpc.on('session.event.plan_revision', applyPlanRevisionEvent),
      options.rpc.on('plan_revision', applyPlanRevisionEvent),
      options.rpc.on('session.event.plan_run', applyPlanRunEvent),
      options.rpc.on('plan_run', applyPlanRunEvent),
    ]
    return () => unsubs.forEach(unsubscribe => unsubscribe())
  }

  async function setMode(mode: CollaborationMode): Promise<boolean> {
    if (
      !options.sessionKey.value
      || modeBusy.value
      || pendingAction.value
    ) return false
    if (mode === collaboration.value.mode) return true
    if (options.isDraft?.()) {
      if (options.isStreaming.value) return false
      // A fresh-chat session key is only a client-side draft until chat.send
      // accepts intent=new_chat. Keep its initial mode local so selecting Plan
      // cannot materialize an empty durable session ahead of that atomic send.
      collaboration.value = { mode, revision: 0 }
      modeAppliesNextTurn.value = false
      return true
    }
    const key = options.sessionKey.value
    const epoch = acceptedEpoch
    const deferred = options.isStreaming.value
    const expectedRevision = collaboration.value.revision
    const owner = Symbol('plan-mode-mutation')
    modeMutationOwner = owner
    modeBusy.value = true
    try {
      const response = await options.rpc.call<PlanMutationResponse>('plans.setMode', {
        sessionKey: key,
        mode,
        expectedRevision,
      })
      if (key !== options.sessionKey.value || epoch !== acceptedEpoch) return false
      applyCollaboration(response, {
        mode,
        revision: expectedRevision + 1,
      })
      // If the active turn settled while the RPC was in flight, the mode is
      // already the effective choice for the next composer send; do not leave
      // a stale "next turn" notice waiting for another false transition.
      modeAppliesNextTurn.value = deferred
        && options.isStreaming.value
        && collaboration.value.mode === mode
      return collaboration.value.mode === mode
    } catch (error) {
      if (
        modeMutationOwner === owner
        && key === options.sessionKey.value
        && epoch === acceptedEpoch
      ) {
        options.notifyError(error instanceof Error ? error.message : String(error))
      }
      return false
    } finally {
      if (modeMutationOwner === owner) {
        modeMutationOwner = null
        modeBusy.value = false
      }
    }
  }

  function toggleMode() {
    return setMode(collaboration.value.mode === 'plan' ? 'default' : 'plan')
  }

  function beginReplan(target: PlanCardActionTarget) {
    replanTarget.value = target
    options.focusComposer()
  }

  function cancelReplan() {
    replanTarget.value = null
  }

  async function revise(request: PlanRevisionRequest): Promise<boolean> {
    if (!options.sessionKey.value || modeBusy.value || pendingAction.value) return false
    const prompt = request.prompt.trim()
    if (!prompt) return false
    const key = options.sessionKey.value
    const epoch = acceptedEpoch
    const owner = Symbol('plan-action-mutation')
    actionMutationOwner = owner
    pendingAction.value = 'revise'
    try {
      const response = await options.rpc.call<PlanMutationResponse>('plans.revise', {
        sessionKey: key,
        planRevisionId: request.revisionId,
        prompt,
        clientRequestId: createClientRequestId(),
      })
      if (key !== options.sessionKey.value || epoch !== acceptedEpoch) return false
      applyResponse(response)
      applyCollaboration(response, {
        mode: 'plan',
        revision: collaboration.value.revision + (collaboration.value.mode === 'plan' ? 0 : 1),
      })
      replanTarget.value = null
      options.onMutationAccepted?.()
      return true
    } catch (error) {
      if (
        actionMutationOwner === owner
        && key === options.sessionKey.value
        && epoch === acceptedEpoch
      ) {
        options.notifyError(error instanceof Error ? error.message : String(error))
      }
      return false
    } finally {
      if (actionMutationOwner === owner) {
        actionMutationOwner = null
        pendingAction.value = null
      }
    }
  }

  async function implement(target: PlanCardActionTarget, inNewSession: boolean) {
    if (!options.sessionKey.value || modeBusy.value || pendingAction.value) return
    const sourceKey = options.sessionKey.value
    const sourceEpoch = acceptedEpoch
    const targetKey = inNewSession
      ? options.createSessionKey(options.agentId())
      : sourceKey
    const owner = Symbol('plan-action-mutation')
    actionMutationOwner = owner
    pendingAction.value = inNewSession ? 'implement-new' : 'implement-current'
    try {
      const params: Record<string, unknown> = {
        sessionKey: targetKey,
        planRevisionId: target.revisionId,
        clientRequestId: createClientRequestId(),
      }
      if (inNewSession) params.intent = 'new_chat'
      const response = await options.rpc.call<PlanMutationResponse>('plans.implement', params)
      if (sourceKey !== options.sessionKey.value || sourceEpoch !== acceptedEpoch) return
      const acceptedKey = response.sessionKey || response.session_key || targetKey
      if (inNewSession) {
        await options.switchToSession(acceptedKey)
      } else {
        applyResponse(response)
        options.onMutationAccepted?.()
      }
    } catch (error) {
      if (
        actionMutationOwner === owner
        && sourceKey === options.sessionKey.value
        && sourceEpoch === acceptedEpoch
      ) {
        options.notifyError(error instanceof Error ? error.message : String(error))
      }
    } finally {
      if (actionMutationOwner === owner) {
        actionMutationOwner = null
        pendingAction.value = null
      }
    }
  }

  async function cancelRun() {
    const run = activePlanRun.value
    if (!run || modeBusy.value || pendingAction.value) return
    const key = options.sessionKey.value
    const epoch = acceptedEpoch
    const owner = Symbol('plan-action-mutation')
    actionMutationOwner = owner
    pendingAction.value = 'cancel-run'
    try {
      const response = await options.rpc.call<PlanMutationResponse>('plans.cancelRun', {
        sessionKey: key,
        runId: run.runId,
        ...(run.stateRevision !== undefined
          ? { expectedStateRevision: run.stateRevision }
          : {}),
      })
      if (key !== options.sessionKey.value || epoch !== acceptedEpoch) return
      applyResponse(response)
      options.onMutationAccepted?.()
    } catch (error) {
      if (
        actionMutationOwner === owner
        && key === options.sessionKey.value
        && epoch === acceptedEpoch
      ) {
        options.notifyError(error instanceof Error ? error.message : String(error))
      }
    } finally {
      if (actionMutationOwner === owner) {
        actionMutationOwner = null
        pendingAction.value = null
      }
    }
  }

  reset()

  return {
    collaboration,
    initialCollaborationMode,
    currentPlan,
    currentPlanRevisionId,
    activePlanRun,
    modeBusy,
    modeAppliesNextTurn,
    pendingAction,
    replanTarget,
    replanActive,
    reset,
    applyBootstrap,
    subscribe,
    setMode,
    toggleMode,
    beginReplan,
    cancelReplan,
    revise,
    implement,
    cancelRun,
  }
}
