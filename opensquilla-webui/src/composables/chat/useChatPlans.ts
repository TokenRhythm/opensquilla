import { computed, ref, watch, type Ref } from 'vue'
import type {
  CollaborationMode,
  CollaborationSnapshot,
  PlanCardAction,
  PlanCardActionTarget,
  PlanPresentationRequest,
  PlanPresentationSnapshot,
  PlanRevisionRequest,
  PlanRevisionSnapshot,
  PlanRunSnapshot,
} from '@/types/plans'
import type { PlanCenter } from '@/modules/planCenter'
import { createClientRequestId } from '@/utils/chat/messageIdentity'
import {
  forgetPlanImplementation,
  planImplementationIdentity,
  recoverPlanImplementation,
} from '@/utils/chat/planImplementationRecovery'
import {
  normalizeCollaborationSnapshot,
  normalizePlanRevisionSnapshot,
  normalizePlanRunSnapshot,
  payloadBelongsToSession,
} from '@/utils/chat/plans'

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

function responseProperty(
  source: Record<string, unknown>,
  names: readonly string[],
): { present: boolean; value: unknown } {
  for (const name of names) {
    if (Object.prototype.hasOwnProperty.call(source, name)) {
      return { present: true, value: source[name] }
    }
  }
  return { present: false, value: undefined }
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
  planCenter: PlanCenter
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
  const planPresentations = ref<Record<string, PlanPresentationSnapshot>>({})
  const presentationPending = ref<string | null>(null)
  const activePlanRun = ref<PlanRunSnapshot | null>(null)
  // A terminal run can be cleared by an authoritative ``activePlanRun: null``
  // snapshot while replayed events from the same subscription are still in
  // flight. Keep the terminal watermark outside the visible state so a late
  // running update cannot resurrect the old execution.
  const terminalPlanRuns = new Map<string, PlanRunSnapshot>()
  // An explicit null activePlanRun is an authoritative empty snapshot. Keep
  // that fence until a mutation/bootstrap supplies a new active run; replayed
  // historical running events must not recreate the old execution.
  let emptyActiveRunRevisionId: string | null = null
  const settledTaskIds = ref<ReadonlySet<string>>(new Set())
  const visiblePlanRun = computed<PlanRunSnapshot | null>(() => {
    const run = activePlanRun.value
    if (
      run?.activeTaskId
      && settledTaskIds.value.has(run.activeTaskId)
      && (run.status === 'queued' || run.status === 'running')
    ) {
      // The task has ended, but its separate PlanRun update may still be in
      // flight. Pause presentation without changing authoritative progress or
      // creating a terminal state that would prevent the run from resuming.
      return { ...run, status: 'paused', activeTaskId: undefined }
    }
    return run
  })
  const modeBusy = ref(false)
  const pendingAction = ref<PlanCardAction | 'cancel-run' | 'revise' | null>(null)
  const modeAppliesNextTurn = ref(false)
  const replanTarget = ref<PlanCardActionTarget | null>(null)

  const currentPlanRevisionId = computed(() => currentPlan.value?.revisionId || '')
  const replanActive = computed(() => replanTarget.value !== null)
  let acceptedEpoch = 0
  let modeMutationOwner: symbol | null = null
  let actionMutationOwner: symbol | null = null
  let presentationMutationOwner: symbol | null = null

  function clearPlanState() {
    // Reset/session changes invalidate in-flight UI mutations. Their delayed
    // catch/finally blocks must not report into, or unlock, the new epoch.
    modeMutationOwner = null
    actionMutationOwner = null
    presentationMutationOwner = null
    collaboration.value = { mode: 'default', revision: 0 }
    currentPlan.value = null
    planPresentations.value = {}
    presentationPending.value = null
    activePlanRun.value = null
    terminalPlanRuns.clear()
    emptyActiveRunRevisionId = null
    settledTaskIds.value = new Set()
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
    const previousRevisionId = currentPlan.value?.revisionId
    currentPlan.value = { ...plan, current: true }
    if (previousRevisionId !== plan.revisionId) {
      emptyActiveRunRevisionId = null
    }
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
    const terminal = run ? terminalPlanRuns.get(run.runId) ?? null : null
    const current = activePlanRun.value ?? terminal
    if (
      !run
      || !currentPlan.value
      || run.planRevisionId !== currentPlan.value.revisionId
      || (
        emptyActiveRunRevisionId === currentPlan.value.revisionId
        && !TERMINAL_RUN_STATUSES.has(run.status)
      )
      || !shouldAdoptPlanRun(run, current)
    ) return false
    activePlanRun.value = run
    if (TERMINAL_RUN_STATUSES.has(run.status)) {
      terminalPlanRuns.set(run.runId, run)
    }
    return true
  }

  function applyResponse(value: unknown) {
    const source = objectRecord(value) ?? {}
    applyPresentations(source.planPresentations ?? source.plan_presentations)
    const incomingCollaborationRevision = collaborationRevisionFrom(source)
    const staleEnvelope = incomingCollaborationRevision !== undefined
      && incomingCollaborationRevision < collaboration.value.revision
    if (source.collaboration !== undefined) {
      applyCollaboration(source)
    }
    const planProperty = responseProperty(source, [
      'currentPlan', 'current_plan', 'planRevision', 'plan_revision', 'plan', 'snapshot',
    ])
    if (planProperty.present) {
      const rawPlan = planProperty.value
      if (rawPlan !== null) {
        if (!staleEnvelope) {
          applyPlanRevision(rawPlan, source)
        }
      } else if (!staleEnvelope) {
        currentPlan.value = null
        activePlanRun.value = null
        emptyActiveRunRevisionId = null
      }
    }
    const runProperty = responseProperty(source, [
      'activePlanRun', 'active_plan_run', 'planRun', 'plan_run', 'run',
    ])
    if (runProperty.present) {
      const rawRun = runProperty.value
      if (rawRun !== null) {
        if (!staleEnvelope) {
          emptyActiveRunRevisionId = null
          applyPlanRun(rawRun)
        }
      } else if (!staleEnvelope) {
        emptyActiveRunRevisionId = currentPlan.value?.revisionId ?? null
        // Preserve a terminal snapshot long enough for the run-order gate to
        // reject replayed running events that arrive after the empty snapshot.
        if (!activePlanRun.value || !TERMINAL_RUN_STATUSES.has(activePlanRun.value.status)) {
          activePlanRun.value = null
        }
      }
    }
  }

  function applyPresentations(value: unknown) {
    if (!Array.isArray(value)) return
    const next = { ...planPresentations.value }
    for (const item of value) {
      const source = objectRecord(item)
      if (!source || typeof source.revisionId !== 'string' || !source.revisionId
        || typeof source.dismissed !== 'boolean'
        || typeof source.stateRevision !== 'number'
        || !Number.isInteger(source.stateRevision) || source.stateRevision < 0) continue
      const current = next[source.revisionId]
      if (current && current.stateRevision >= source.stateRevision) continue
      next[source.revisionId] = {
        revisionId: source.revisionId,
        dismissed: source.dismissed,
        stateRevision: source.stateRevision,
      }
    }
    planPresentations.value = next
  }

  function applyBootstrap(snapshot: unknown) {
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
    const subscription = options.planCenter.subscribe(event => {
      const identity = { sessionKey: event.sessionKey, epoch: event.epoch }
      if (event.kind === 'collaboration') applyCollaborationEvent({ ...identity, collaboration: event.collaboration })
      if (event.kind === 'revision') applyPlanRevisionEvent({ ...identity, planRevision: event.plan, collaboration: event.collaboration })
      if (event.kind === 'run') applyPlanRunEvent({ ...identity, planRun: event.run })
      if (event.kind === 'presentation' && payloadBelongsToSession(identity, options.sessionKey.value)
        && acceptEpoch(identity)) applyPresentations(event.planPresentations)
    })
    return () => subscription.close()
  }

  function noteTaskSettled(taskId: string, epoch?: number) {
    if (!acceptEpoch({ epoch }, true)) return
    if (!taskId || settledTaskIds.value.has(taskId)) return
    const next = new Set(settledTaskIds.value)
    next.add(taskId)
    if (next.size > 256) next.delete(next.values().next().value!)
    settledTaskIds.value = next
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
      const response = await options.planCenter.setMode(key, mode, expectedRevision)
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
      const response = await options.planCenter.revise(key, request, createClientRequestId())
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
    const identity = planImplementationIdentity(sourceKey, sourceEpoch, target.revisionId, inNewSession)
    const recovery = recoverPlanImplementation(identity, () => inNewSession
      ? options.createSessionKey(options.agentId())
      : sourceKey)
    const targetKey = recovery.targetSessionKey
    const owner = Symbol('plan-action-mutation')
    actionMutationOwner = owner
    pendingAction.value = inNewSession ? 'implement-new' : 'implement-current'
    try {
      const response = await options.planCenter.implement(
        targetKey,
        target,
        recovery.clientRequestId,
        inNewSession ? { intent: 'new_chat' } : undefined,
      )
      if (sourceKey !== options.sessionKey.value || sourceEpoch !== acceptedEpoch) return
      const acceptedKey = response.sessionKey || targetKey
      if (inNewSession) {
        await options.switchToSession(acceptedKey)
      } else {
        applyResponse(response)
        options.onMutationAccepted?.()
      }
      forgetPlanImplementation(identity)
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

  async function setPresentation(request: PlanPresentationRequest): Promise<boolean> {
    if (!options.sessionKey.value || presentationPending.value
      || !options.planCenter.available('presentation')) return false
    const key = options.sessionKey.value
    const epoch = acceptedEpoch
    const owner = Symbol('plan-presentation-mutation')
    presentationMutationOwner = owner
    presentationPending.value = request.revisionId
    try {
      const response = await options.planCenter.setPresentation({
        sessionKey: key,
        revisionId: request.revisionId,
        dismissed: request.dismissed,
        expectedEpoch: epoch,
        expectedPresentationRevision: planPresentations.value[request.revisionId]?.stateRevision ?? 0,
        clientRequestId: createClientRequestId(),
      })
      if (key !== options.sessionKey.value || epoch !== acceptedEpoch) return false
      applyResponse(response)
      return true
    } catch (error) {
      if (presentationMutationOwner === owner && key === options.sessionKey.value
        && epoch === acceptedEpoch) {
        const details = objectRecord(objectRecord(error)?.details)
        if (details && payloadBelongsToSession(details, key) && acceptEpoch(details)) {
          applyPresentations(details.planPresentations)
        }
        options.notifyError(error instanceof Error ? error.message : String(error))
      }
      return false
    } finally {
      if (presentationMutationOwner === owner) {
        presentationMutationOwner = null
        presentationPending.value = null
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
      const response = await options.planCenter.cancelRun(key, run.runId, run.stateRevision)
      if (key !== options.sessionKey.value || epoch !== acceptedEpoch) return
      applyResponse(response)
      options.onMutationAccepted?.()
    } catch (error) {
      if (
        actionMutationOwner === owner
        && key === options.sessionKey.value
        && epoch === acceptedEpoch
      ) {
        const details = objectRecord(objectRecord(error)?.details)
        const latest = normalizePlanRunSnapshot(details?.planRun)
        if (latest?.runId === run.runId) applyPlanRun(latest)
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
    planPresentations,
    presentationPending,
    currentPlanRevisionId,
    activePlanRun: visiblePlanRun,
    modeBusy,
    modeAppliesNextTurn,
    pendingAction,
    replanTarget,
    replanActive,
    reset,
    applyBootstrap,
    subscribe,
    noteTaskSettled,
    setMode,
    toggleMode,
    beginReplan,
    cancelReplan,
    revise,
    implement,
    setPresentation,
    cancelRun,
  }
}
