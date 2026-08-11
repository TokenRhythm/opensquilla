import { computed, onBeforeUnmount, ref, watch, type Ref } from 'vue'
import type { SessionMessagesSubscribeResponse } from '@/types/rpc'
import { localizeGoalRpcError } from '@/lib/rpcErrors'
import { createClientRequestId } from '@/utils/chat/messageIdentity'

export type GoalStatus = 'active' | 'paused' | 'blocked' | 'usage_limited' | 'complete'
export type GoalExecutionState = 'idle' | 'queued' | 'working'
export type GoalProgressStepStatus = 'pending' | 'in_progress' | 'completed'
export type GoalContinuationDeferredReason =
  | 'pending_user'
  | 'busy'
  | 'plan_mode'
  | 'owner_disconnected'

export interface GoalProgressStep {
  text: string
  status: GoalProgressStepStatus
}

export interface GoalProgress {
  explanation: string | null
  steps: GoalProgressStep[]
}

export interface GoalUsage {
  inputTokens: number
  outputTokens: number
  reasoningTokens: number
  cacheReadTokens: number
  cacheWriteTokens: number
  totalTokens: number
}

export interface GoalSnapshot {
  goalId: string
  sessionKey: string
  sessionId: string
  epoch: number
  objective: string
  status: GoalStatus
  stateRevision: number
  objectiveRevision: number
  progressRevision: number
  progress: GoalProgress | null
  continuationSeq: number
  activeTaskId: string | null
  /** Durable transcript row that created the current Goal. */
  sourceMessageId?: string | null
  /** Owning turn that committed the current structured terminal result. */
  terminalTurnId?: string | null
  executionState: GoalExecutionState
  continuationDeferredReason: GoalContinuationDeferredReason | null
  turnsStarted: number
  turnsSettled: number
  windowTurnsStarted: number
  activeTimeMs: number
  windowActiveTimeMs: number
  usage: GoalUsage
  pauseReason: string | null
  blockedReason: string | null
  terminalReason: string | null
  createdAt: number
  updatedAt: number
  finishedAt: number | null
}

export interface GoalMutationResponse {
  accepted: true
  clientRequestId: string
  sessionKey: string
  sessionId: string
  epoch: number
  taskId: string | null
  userMessageId: string | null
  previousGoalId: string | null
  goal: GoalSnapshot | null
  continuityToken?: string
}

export interface GoalSetAcceptedPayload {
  objective: string
  clientMessageId: string
  response: GoalMutationResponse
}

interface GoalReattachResponse {
  accepted: true
  sessionKey: string
  sessionId: string
  epoch: number
  goal: GoalSnapshot
  continuityToken?: string
}

export interface GoalContinuityStorage {
  readonly length: number
  key: (index: number) => string | null
  getItem: (key: string) => string | null
  setItem: (key: string, value: string) => void
  removeItem: (key: string) => void
}

interface GoalStatusResult {
  goal?: unknown
}

type RpcClient = {
  call: <T = unknown>(
    method: string,
    params?: Record<string, unknown>,
  ) => Promise<T>
  on: (event: string, handler: (...args: unknown[]) => void) => () => void
}

export interface UseChatGoalsOptions {
  rpc: RpcClient
  sessionKey: Ref<string>
  currentEpoch?: Ref<number>
  // A Goal may only be accepted after the target session is materialized and
  // its message subscription is registered. The host owns those two steps.
  ensureSessionKey?: () => Promise<string>
  ensureSubscribed?: (sessionKey: string) => Promise<boolean>
  // The Goal mutation already durably accepted its transcript row. Let the
  // host project that confirmed row immediately instead of waiting for a
  // later history refresh to discover it.
  onSetAccepted?: (payload: GoalSetAcceptedPayload) => void | Promise<void>
  notify?: (message: string) => void
  // Production uses window.sessionStorage. Tests may inject the same narrow
  // contract without exposing a durable/local-storage fallback.
  continuityStorage?: GoalContinuityStorage
}

const GOAL_STATUSES = new Set<GoalStatus>([
  'active',
  'paused',
  'blocked',
  'usage_limited',
  'complete',
])
const GOAL_EXECUTION_STATES = new Set<GoalExecutionState>(['idle', 'queued', 'working'])
const GOAL_PROGRESS_STATUSES = new Set<GoalProgressStepStatus>([
  'pending',
  'in_progress',
  'completed',
])
const GOAL_CONTINUITY_STORAGE_PREFIX = 'opensquilla.goal.continuity.v1:'

interface GoalContinuityRecord {
  version: 1
  goalId: string
  sessionId: string
  epoch: number
  token: string
}

export function goalStatusIsTerminal(status: string | undefined): boolean {
  return status === 'complete'
}

/**
 * A structured terminal decision is not yet a displayable outcome while its
 * owning task is still queued or running. Waiting for settlement keeps the
 * outcome aligned with the final assistant row and the authoritative usage
 * accounting revision.
 *
 * Missing ownership fields are normalized to the settled values, preserving
 * compatibility with snapshots produced before those fields were published.
 */
export function goalHasSettledTerminalOutcome(
  goal: GoalSnapshot | null | undefined,
): boolean {
  return Boolean(
    goalStatusIsTerminal(goal?.status)
    && (goal?.activeTaskId === null || goal?.activeTaskId === undefined)
    && goal?.executionState === 'idle',
  )
}

interface GoalOutcomeAnchorMessage {
  displayRole: string
  turnId?: string
  stopNotice?: boolean
}

export function goalHasRenderedTerminalAnchor(
  goal: GoalSnapshot | null | undefined,
  messages: readonly GoalOutcomeAnchorMessage[],
): boolean {
  if (!goal || !goalHasSettledTerminalOutcome(goal)) return false
  const terminalTurnId = String(goal.terminalTurnId || '').trim()
  if (!terminalTurnId) return false
  return messages.some(message => (
    message.displayRole === 'assistant'
    && !message.stopNotice
    && message.turnId === terminalTurnId
  ))
}

function goalObjectiveIsValid(objective: string): boolean {
  let codePoints = 0
  for (const _codePoint of objective) {
    codePoints += 1
    if (codePoints > 4000) return false
  }
  return codePoints > 0
}

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function stringField(source: Record<string, unknown>, ...keys: string[]): string | undefined {
  for (const key of keys) {
    const raw = source[key]
    if (typeof raw === 'string' && raw) return raw
  }
  return undefined
}

function nullableStringField(
  source: Record<string, unknown>,
  ...keys: string[]
): string | null {
  return stringField(source, ...keys) ?? null
}

function numberField(source: Record<string, unknown>, ...keys: string[]): number | undefined {
  for (const key of keys) {
    const raw = source[key]
    if (typeof raw === 'number' && Number.isFinite(raw)) return raw
  }
  return undefined
}

function integerField(source: Record<string, unknown>, ...keys: string[]): number | undefined {
  const value = numberField(source, ...keys)
  return value !== undefined && Number.isInteger(value) && value >= 0 ? value : undefined
}

function continuityStorageSessionPrefix(sessionKey: string): string {
  return `${GOAL_CONTINUITY_STORAGE_PREFIX}${encodeURIComponent(sessionKey)}:`
}

function continuityStorageKey(
  identity: Pick<GoalSnapshot, 'sessionKey' | 'sessionId' | 'epoch' | 'goalId'>,
): string {
  return `${continuityStorageSessionPrefix(identity.sessionKey)}${[
    encodeURIComponent(identity.sessionId),
    String(identity.epoch),
    encodeURIComponent(identity.goalId),
  ].join(':')}`
}

function browserContinuityStorage(): GoalContinuityStorage | undefined {
  try {
    return typeof window === 'undefined' ? undefined : window.sessionStorage
  } catch {
    // Privacy controls may deny sessionStorage. In that case a refresh cannot
    // prove lease continuity and the Goal safely remains detached.
    return undefined
  }
}

function normalizeContinuityRecord(value: unknown): GoalContinuityRecord | null {
  const source = record(value)
  const goalId = source ? stringField(source, 'goalId') : undefined
  const sessionId = source ? stringField(source, 'sessionId') : undefined
  const epoch = source ? integerField(source, 'epoch') : undefined
  const token = source ? stringField(source, 'token') : undefined
  if (
    source?.version !== 1
    || !goalId
    || !sessionId
    || epoch === undefined
    || !token
    || token.length > 256
  ) return null
  return { version: 1, goalId, sessionId, epoch, token }
}

function normalizeProgress(value: unknown): GoalProgress | null {
  if (value === null || value === undefined) return null
  const source = record(value)
  if (!source) return null
  const rawSteps = source.steps
  if (!Array.isArray(rawSteps)) return null
  const steps: GoalProgressStep[] = []
  for (const rawStep of rawSteps) {
    const step = record(rawStep)
    if (!step) return null
    const text = stringField(step, 'text', 'step')
    const status = stringField(step, 'status') as GoalProgressStepStatus | undefined
    if (!text || !status || !GOAL_PROGRESS_STATUSES.has(status)) return null
    steps.push({ text, status })
  }
  return {
    explanation: nullableStringField(source, 'explanation'),
    steps,
  }
}

function normalizeUsage(value: unknown): GoalUsage {
  const source = record(value) ?? {}
  return {
    inputTokens: integerField(source, 'inputTokens', 'input_tokens') ?? 0,
    outputTokens: integerField(source, 'outputTokens', 'output_tokens') ?? 0,
    reasoningTokens: integerField(source, 'reasoningTokens', 'reasoning_tokens') ?? 0,
    cacheReadTokens: integerField(source, 'cacheReadTokens', 'cache_read_tokens') ?? 0,
    cacheWriteTokens: integerField(source, 'cacheWriteTokens', 'cache_write_tokens') ?? 0,
    totalTokens: integerField(source, 'totalTokens', 'total_tokens') ?? 0,
  }
}

export function normalizeGoal(value: unknown): GoalSnapshot | null {
  const source = record(value)
  if (!source) return null
  const goalId = stringField(source, 'goalId', 'goal_id')
  const sessionKey = stringField(source, 'sessionKey', 'session_key')
  const sessionId = stringField(source, 'sessionId', 'session_id')
  const objective = stringField(source, 'objective', 'goalText', 'goal_text')
  const status = stringField(source, 'status') as GoalStatus | undefined
  const epoch = integerField(source, 'epoch', 'sessionEpoch', 'session_epoch')
  const stateRevision = integerField(source, 'stateRevision', 'state_revision')
  const objectiveRevision = integerField(source, 'objectiveRevision', 'objective_revision')
  const progressRevision = integerField(source, 'progressRevision', 'progress_revision')
  if (
    !goalId
    || !sessionKey
    || !sessionId
    || !objective
    || !status
    || !GOAL_STATUSES.has(status)
    || epoch === undefined
    || stateRevision === undefined
    || objectiveRevision === undefined
    || progressRevision === undefined
  ) return null

  const executionStateRaw = stringField(source, 'executionState', 'execution_state')
  const executionState = GOAL_EXECUTION_STATES.has(executionStateRaw as GoalExecutionState)
    ? executionStateRaw as GoalExecutionState
    : 'idle'
  const deferredRaw = nullableStringField(
    source,
    'continuationDeferredReason',
    'continuation_deferred_reason',
  )
  const continuationDeferredReason = (
    deferredRaw === 'pending_user'
    || deferredRaw === 'busy'
    || deferredRaw === 'plan_mode'
    || deferredRaw === 'owner_disconnected'
  ) ? deferredRaw : null

  return {
    goalId,
    sessionKey,
    sessionId,
    epoch,
    objective,
    status,
    stateRevision,
    objectiveRevision,
    progressRevision,
    progress: normalizeProgress(source.progress),
    continuationSeq: integerField(source, 'continuationSeq', 'continuation_seq') ?? 0,
    activeTaskId: nullableStringField(source, 'activeTaskId', 'active_task_id'),
    sourceMessageId: nullableStringField(
      source,
      'sourceMessageId',
      'source_message_id',
      'source_user_message_id',
    ),
    terminalTurnId: nullableStringField(
      source,
      'terminalTurnId',
      'terminal_turn_id',
    ),
    executionState,
    continuationDeferredReason,
    turnsStarted: integerField(source, 'turnsStarted', 'turns_started') ?? 0,
    turnsSettled: integerField(source, 'turnsSettled', 'turns_settled') ?? 0,
    windowTurnsStarted: integerField(source, 'windowTurnsStarted', 'window_turns_started') ?? 0,
    activeTimeMs: integerField(source, 'activeTimeMs', 'active_time_ms') ?? 0,
    windowActiveTimeMs: integerField(source, 'windowActiveTimeMs', 'window_active_time_ms') ?? 0,
    usage: normalizeUsage(source.usage),
    pauseReason: nullableStringField(source, 'pauseReason', 'pause_reason'),
    blockedReason: nullableStringField(source, 'blockedReason', 'blocked_reason'),
    terminalReason: nullableStringField(source, 'terminalReason', 'terminal_reason'),
    createdAt: integerField(source, 'createdAt', 'created_at') ?? 0,
    updatedAt: integerField(source, 'updatedAt', 'updated_at') ?? 0,
    finishedAt: integerField(source, 'finishedAt', 'finished_at') ?? null,
  }
}

export function formatGoalDuration(durationMs: number | undefined): string {
  const totalSeconds = Math.max(0, Math.floor((durationMs ?? 0) / 1000))
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60
  if (hours > 0) return `${hours}h ${String(minutes).padStart(2, '0')}m`
  if (minutes > 0) return `${minutes}m ${String(seconds).padStart(2, '0')}s`
  return `${seconds}s`
}

function envelopeSessionKey(value: unknown): string | undefined {
  const source = record(value)
  return source ? stringField(source, 'sessionKey', 'session_key', 'key') : undefined
}

function envelopeSessionId(value: unknown): string | undefined {
  const source = record(value)
  return source ? stringField(source, 'sessionId', 'session_id') : undefined
}

function envelopeEpoch(value: unknown): number | undefined {
  const source = record(value)
  return source ? integerField(source, 'epoch', 'sessionEpoch', 'session_epoch') : undefined
}

function envelopeStreamSeq(value: unknown): number | undefined {
  const source = record(value)
  return source ? integerField(source, 'streamSeq', 'stream_seq') : undefined
}

function goalSnapshotStreamSeq(value: unknown): number | undefined {
  const source = record(value)
  return source
    ? integerField(source, 'goalSnapshotStreamSeq', 'goal_snapshot_stream_seq')
    : undefined
}

export function useChatGoals(options: UseChatGoalsOptions) {
  const draftArmed = ref(false)
  const goal = ref<GoalSnapshot | null>(null)
  const busy = ref(false)
  const connectionTakeoverAvailable = ref(false)
  const reattaching = ref(false)

  let acceptedSessionId = ''
  let acceptedEpoch = 0
  let acceptedStreamSeq = -1
  let mutationOwner: symbol | null = null
  // Goal set owns materialization, subscription, and mutation as one UI-side
  // admission. Keep this fence separate from mutationOwner because the
  // expected provisional -> durable session switch resets generation state.
  let startGoalOwner: symbol | null = null
  const tombstones = new Map<string, number>()
  const reattachInFlight = new Set<string>()
  const automaticReattachWatermarks = new Map<string, number>()
  const continuityStorage = options.continuityStorage ?? browserContinuityStorage()

  const activeGoal = computed(() => {
    const current = goal.value
    if (!current || goalHasSettledTerminalOutcome(current)) return null
    return current
  })
  const lastGoal = computed(() => (
    goalHasSettledTerminalOutcome(goal.value) ? goal.value : null
  ))
  const elapsed = computed(() => formatGoalDuration(activeGoal.value?.activeTimeMs))
  const lastGoalElapsed = computed(() => formatGoalDuration(lastGoal.value?.activeTimeMs))

  function resetGeneration(preserveStartAdmission = false) {
    goal.value = null
    acceptedSessionId = ''
    acceptedEpoch = 0
    acceptedStreamSeq = -1
    tombstones.clear()
    automaticReattachWatermarks.clear()
    mutationOwner = null
    if (!preserveStartAdmission) startGoalOwner = null
    busy.value = startGoalOwner !== null
    connectionTakeoverAvailable.value = false
    reattaching.value = false
  }

  function arm() {
    draftArmed.value = true
  }

  function disarm() {
    draftArmed.value = false
  }

  function continuityKeysForSession(sessionKey: string): string[] {
    if (!continuityStorage || !sessionKey) return []
    const prefix = continuityStorageSessionPrefix(sessionKey)
    const keys: string[] = []
    try {
      for (let index = 0; index < continuityStorage.length; index += 1) {
        const key = continuityStorage.key(index)
        if (key?.startsWith(prefix)) keys.push(key)
      }
    } catch {
      return []
    }
    return keys
  }

  function removeContinuityRecordsForSession(sessionKey: string, keepKey?: string) {
    if (!continuityStorage) return
    try {
      for (const key of continuityKeysForSession(sessionKey)) {
        if (key !== keepKey) continuityStorage.removeItem(key)
      }
    } catch {
      // Storage availability is an optimization for safe same-tab continuity;
      // a storage failure must never change durable Goal state.
    }
  }

  function removeContinuityRecord(current: GoalSnapshot) {
    if (!continuityStorage) return
    try {
      continuityStorage.removeItem(continuityStorageKey(current))
    } catch {
      // See removeContinuityRecordsForSession.
    }
  }

  function readContinuityRecord(current: GoalSnapshot): GoalContinuityRecord | null {
    if (!continuityStorage) return null
    const key = continuityStorageKey(current)
    try {
      const raw = continuityStorage.getItem(key)
      if (!raw) return null
      const stored = normalizeContinuityRecord(JSON.parse(raw))
      if (
        !stored
        || stored.goalId !== current.goalId
        || stored.sessionId !== current.sessionId
        || stored.epoch !== current.epoch
      ) {
        continuityStorage.removeItem(key)
        return null
      }
      return stored
    } catch {
      removeContinuityRecord(current)
      return null
    }
  }

  function rememberContinuityToken(value: unknown): boolean {
    if (!continuityStorage) return false
    const source = record(value)
    const incomingGoal = source ? normalizeGoal(source.goal) : null
    const token = source ? stringField(source, 'continuityToken', 'continuity_token') : undefined
    if (!incomingGoal || !token || token.length > 256) return false
    if (incomingGoal.sessionKey !== options.sessionKey.value) return false
    const stored: GoalContinuityRecord = {
      version: 1,
      goalId: incomingGoal.goalId,
      sessionId: incomingGoal.sessionId,
      epoch: incomingGoal.epoch,
      token,
    }
    try {
      const key = continuityStorageKey(incomingGoal)
      removeContinuityRecordsForSession(incomingGoal.sessionKey, key)
      continuityStorage.setItem(
        key,
        JSON.stringify(stored),
      )
      return true
    } catch {
      return false
    }
  }

  function acceptIdentity(value: unknown, incomingGoal: GoalSnapshot | null): boolean {
    const key = envelopeSessionKey(value) ?? incomingGoal?.sessionKey
    if (key && key !== options.sessionKey.value) return false
    const sessionId = envelopeSessionId(value) ?? incomingGoal?.sessionId
    const epoch = envelopeEpoch(value) ?? incomingGoal?.epoch
    if (epoch !== undefined && epoch < acceptedEpoch) return false
    if (sessionId && acceptedSessionId && sessionId !== acceptedSessionId && epoch === acceptedEpoch) {
      return false
    }
    if (epoch !== undefined && epoch > acceptedEpoch) {
      goal.value = null
      tombstones.clear()
      acceptedEpoch = epoch
      acceptedSessionId = sessionId ?? ''
    } else if (sessionId && !acceptedSessionId) {
      acceptedSessionId = sessionId
    }
    if (epoch !== undefined && epoch > (options.currentEpoch?.value ?? 0)) {
      if (options.currentEpoch) options.currentEpoch.value = epoch
    }
    return true
  }

  function shouldAdoptGoal(incoming: GoalSnapshot): boolean {
    const tombstoneRevision = tombstones.get(incoming.goalId)
    if (tombstoneRevision !== undefined && incoming.stateRevision <= tombstoneRevision) {
      return false
    }
    const current = goal.value
    if (!current || current.goalId !== incoming.goalId) return true
    if (incoming.stateRevision < current.stateRevision) return false
    if (
      incoming.stateRevision === current.stateRevision
      && incoming.progressRevision < current.progressRevision
    ) return false
    if (
      incoming.stateRevision === current.stateRevision
      && incoming.progressRevision === current.progressRevision
      && incoming.objectiveRevision < current.objectiveRevision
    ) return false
    return true
  }

  function tombstonePreviousGoal(
    source: Record<string, unknown> | null,
    incomingGoal: GoalSnapshot | null,
  ) {
    if (!source) return
    const previousGoalId = stringField(source, 'previousGoalId', 'previous_goal_id')
    if (!previousGoalId || previousGoalId === incomingGoal?.goalId) return
    // Goal ids are never reused. Once a replacement has been accepted, no
    // cursorless response for its predecessor may make that Goal current again.
    tombstones.set(previousGoalId, Number.MAX_SAFE_INTEGER)
  }

  function applySnapshot(
    value: unknown,
    options_: {
      streamSeq?: number
      allowClear?: boolean
      tombstoneReplacedCurrent?: boolean
    } = {},
  ): boolean {
    const source = record(value)
    const rawGoal = source && 'goal' in source ? source.goal : value
    const incomingGoal = normalizeGoal(rawGoal)
    if (!acceptIdentity(value, incomingGoal)) return false
    const streamSeq = options_.streamSeq ?? envelopeStreamSeq(value)
    if (streamSeq !== undefined && streamSeq <= acceptedStreamSeq) return false

    if (!incomingGoal) {
      if (!options_.allowClear || (rawGoal !== null && rawGoal !== undefined)) return false
      const previousGoalId = source
        ? stringField(source, 'previousGoalId', 'previous_goal_id')
        : undefined
      const current = goal.value
      if (current && (!previousGoalId || previousGoalId === current.goalId)) {
        const clearRevision = integerField(source ?? {}, 'stateRevision', 'state_revision')
          ?? current.stateRevision + 1
        tombstones.set(
          current.goalId,
          Math.max(tombstones.get(current.goalId) ?? -1, clearRevision),
        )
        goal.value = null
      }
      if (streamSeq !== undefined) acceptedStreamSeq = streamSeq
      return true
    }
    if (incomingGoal.sessionKey !== options.sessionKey.value) return false
    if (!shouldAdoptGoal(incomingGoal)) {
      if (streamSeq !== undefined) acceptedStreamSeq = streamSeq
      return false
    }
    const current = goal.value
    if (
      options_.tombstoneReplacedCurrent
      && current
      && current.goalId !== incomingGoal.goalId
    ) {
      tombstones.set(current.goalId, Number.MAX_SAFE_INTEGER)
    }
    tombstonePreviousGoal(source, incomingGoal)
    goal.value = incomingGoal
    if (streamSeq !== undefined) acceptedStreamSeq = streamSeq
    return true
  }

  function applyMutationResponse(value: unknown): boolean {
    const source = record(value)
    if (!source || source.accepted !== true || !('goal' in source)) return false
    const incomingGoal = normalizeGoal(source.goal)
    // Mutation responses do not carry a stream cursor. Record their explicit
    // replacement fence even when an equal-revision event already won the race.
    tombstonePreviousGoal(source, incomingGoal)
    const current = goal.value
    if (
      incomingGoal
      && current
      && current.goalId === incomingGoal.goalId
      && current.sessionKey === incomingGoal.sessionKey
      && current.sessionId === incomingGoal.sessionId
      && current.epoch === incomingGoal.epoch
      && (
        current.stateRevision > incomingGoal.stateRevision
        || (
          current.stateRevision === incomingGoal.stateRevision
          && current.progressRevision > incomingGoal.progressRevision
        )
        || (
          current.stateRevision === incomingGoal.stateRevision
          && current.progressRevision === incomingGoal.progressRevision
          && current.objectiveRevision >= incomingGoal.objectiveRevision
        )
      )
    ) {
      // A mutation response has no stream cursor. If an equal-revision live
      // event already arrived, it may carry a newer derived executionState
      // (queued -> working). It may also already have advanced the same Goal
      // to a newer durable revision. In both cases the mutation was accepted;
      // keep the newer live state and let the caller project its transcript row.
      return true
    }
    return applySnapshot(source, { allowClear: true })
  }

  function applyHydration(value: SessionMessagesSubscribeResponse | unknown): boolean {
    const source = record(value)
    if (!source || !('goal' in source)) return false
    const deferred = source.deferredFields ?? source.deferred_fields
    if (Array.isArray(deferred) && deferred.some(field => (
      field === 'goal' || field === 'goalSnapshotStreamSeq' || field === 'goal_snapshot_stream_seq'
    ))) return false
    const watermark = goalSnapshotStreamSeq(source)
    // The watermark was captured before the durable Goal row was read. Any
    // event already applied after it is newer than this late hydration result.
    if (watermark !== undefined && acceptedStreamSeq > watermark) return false
    // Equality is different from an older hydrate: the authoritative row was
    // read *after* this watermark and may contain a commit whose event has not
    // yet been appended. Apply its revisions without trying to advance the
    // already-consumed cursor.
    const hydrationCursor = watermark === acceptedStreamSeq ? undefined : watermark
    const applied = applySnapshot(source, {
      streamSeq: hydrationCursor,
      allowClear: true,
      tombstoneReplacedCurrent: true,
    })
    if (applied) reconcileContinuityAfterHydration(goal.value, watermark)
    return applied
  }

  function onGoalEvent(payload: unknown) {
    const source = record(payload)
    if (!source) return
    const eventType = stringField(source, 'eventType', 'event_type')
    const applied = applySnapshot(source, {
      allowClear: eventType === 'cleared',
    })
    if (applied) discardInvalidContinuity(goal.value, envelopeSessionKey(source))
  }

  function discardInvalidContinuity(
    current: GoalSnapshot | null,
    fallbackSessionKey?: string,
  ) {
    if (!current) {
      removeContinuityRecordsForSession(fallbackSessionKey ?? options.sessionKey.value)
      return
    }
    if (current.status !== 'active') {
      removeContinuityRecord(current)
      connectionTakeoverAvailable.value = false
      return
    }
    removeContinuityRecordsForSession(current.sessionKey, continuityStorageKey(current))
    const stored = readContinuityRecord(current)
    connectionTakeoverAvailable.value = current.continuationDeferredReason
      === 'owner_disconnected' && !stored
  }

  function applyReattachResponse(value: unknown, expected: GoalSnapshot): boolean {
    const source = record(value)
    const incoming = source?.accepted === true ? normalizeGoal(source.goal) : null
    if (!incoming || !acceptIdentity(source, incoming)) return false
    if (
      incoming.goalId !== expected.goalId
      || incoming.sessionId !== expected.sessionId
      || incoming.epoch !== expected.epoch
    ) return false
    const current = goal.value
    if (!current || current.goalId !== incoming.goalId || current.status !== 'active') return false
    if (!shouldAdoptGoal(incoming)) return false
    if (
      current.stateRevision === incoming.stateRevision
      && current.progressRevision === incoming.progressRevision
      && current.objectiveRevision === incoming.objectiveRevision
    ) {
      // Reattachment changes only the process-local execution overlay. Preserve
      // a newer queued/working projection that may already have arrived while
      // adopting the authoritative detached/attached reason.
      goal.value = {
        ...current,
        continuationDeferredReason: incoming.continuationDeferredReason,
      }
      connectionTakeoverAvailable.value = incoming.continuationDeferredReason
        === 'owner_disconnected'
      return true
    }
    return applySnapshot(source)
  }

  function reconcileContinuityAfterHydration(
    current: GoalSnapshot | null,
    watermark: number | undefined,
  ) {
    if (!current) {
      removeContinuityRecordsForSession(options.sessionKey.value)
      connectionTakeoverAvailable.value = false
      return
    }
    if (current.status !== 'active') {
      removeContinuityRecord(current)
      connectionTakeoverAvailable.value = false
      return
    }
    removeContinuityRecordsForSession(current.sessionKey, continuityStorageKey(current))
    const stored = readContinuityRecord(current)
    if (current.continuationDeferredReason !== 'owner_disconnected') {
      connectionTakeoverAvailable.value = false
      return
    }
    if (!stored) {
      connectionTakeoverAvailable.value = true
      return
    }
    const fence = `${current.sessionKey}:${current.sessionId}:${current.epoch}:${current.goalId}`
    const attemptWatermark = watermark ?? acceptedStreamSeq
    if (
      reattachInFlight.has(fence)
      || automaticReattachWatermarks.get(fence) === attemptWatermark
    ) return
    reattachInFlight.add(fence)
    automaticReattachWatermarks.set(fence, attemptWatermark)
    reattaching.value = true
    connectionTakeoverAvailable.value = false
    void (async () => {
      try {
        const response = await options.rpc.call<GoalReattachResponse>('goals.reattach', {
          sessionKey: current.sessionKey,
          sessionId: current.sessionId,
          epoch: current.epoch,
          expectedGoalId: current.goalId,
          continuityToken: stored.token,
          sourceKind: 'web',
        })
        const latest = goal.value
        const stillOwned = latest ? readContinuityRecord(latest) : null
        if (
          !latest
          || latest.status !== 'active'
          || latest.goalId !== current.goalId
          || latest.sessionId !== current.sessionId
          || latest.epoch !== current.epoch
          || stillOwned?.token !== stored.token
        ) return
        if (applyReattachResponse(response, current)) rememberContinuityToken(response)
      } catch {
        // Network loss is precisely the case this token is meant to bridge.
        // Keep it for explicit takeover or a later page bootstrap; never loop
        // and never auto-Resume within this authenticated hydration.
        const latest = goal.value
        if (
          latest?.goalId === current.goalId
          && latest.status === 'active'
          && latest.continuationDeferredReason === 'owner_disconnected'
        ) connectionTakeoverAvailable.value = true
      } finally {
        reattachInFlight.delete(fence)
        reattaching.value = false
      }
    })()
  }

  async function takeOverConnection(): Promise<boolean> {
    const key = options.sessionKey.value
    const current = goal.value
    if (
      !key
      || !current
      || current.status !== 'active'
      || current.continuationDeferredReason !== 'owner_disconnected'
      || busy.value
    ) return false
    try {
      if (options.ensureSubscribed && !await options.ensureSubscribed(key)) return false
    } catch (error) {
      options.notify?.(localizeGoalRpcError(error))
      return false
    }
    const owner = Symbol('goals.reattach.takeover')
    mutationOwner = owner
    busy.value = true
    try {
      const response = await options.rpc.call<GoalReattachResponse>('goals.reattach', {
        sessionKey: current.sessionKey,
        sessionId: current.sessionId,
        epoch: current.epoch,
        expectedGoalId: current.goalId,
        takeover: true,
        sourceKind: 'web',
      })
      if (owner !== mutationOwner || key !== options.sessionKey.value) return false
      const accepted = applyReattachResponse(response, current)
      if (accepted) rememberContinuityToken(response)
      connectionTakeoverAvailable.value = !accepted
      return accepted
    } catch (error) {
      applyConflictSnapshot(error)
      options.notify?.(localizeGoalRpcError(error))
      return false
    } finally {
      if (owner === mutationOwner) {
        mutationOwner = null
        busy.value = false
      }
    }
  }

  async function startGoal(text: string): Promise<boolean> {
    const objective = String(text || '').trim()
    if (!goalObjectiveIsValid(objective)) return false
    if (busy.value || startGoalOwner !== null) return false
    const owner = Symbol('goal-set')
    startGoalOwner = owner
    busy.value = true
    const resolveKey = options.ensureSessionKey ?? (async () => options.sessionKey.value)
    let key = ''
    try {
      key = await resolveKey()
      if (owner !== startGoalOwner || !key || key !== options.sessionKey.value) return false
      if (options.ensureSubscribed && !await options.ensureSubscribed(key)) return false
      if (owner !== startGoalOwner || key !== options.sessionKey.value) return false
      mutationOwner = owner
      busy.value = true
      const clientRequestId = createClientRequestId()
      const clientMessageId = createClientRequestId()
      const response = await options.rpc.call<GoalMutationResponse>('goals.set', {
        sessionKey: key,
        objective,
        clientRequestId,
        clientMessageId,
      })
      if (owner !== mutationOwner || key !== options.sessionKey.value) return false
      const applied = applyMutationResponse(response)
      if (applied) {
        rememberContinuityToken(response)
        try {
          await options.onSetAccepted?.({ objective, clientMessageId, response })
        } catch (error) {
          // The server has already committed the Goal and its transcript row.
          // A local projection failure must not report a false mutation
          // failure or replay the idempotent command.
          console.warn('Failed to project the accepted Goal message:', error)
        }
      }
      return applied && response.accepted === true
    } catch (error) {
      applyConflictSnapshot(error)
      options.notify?.(localizeGoalRpcError(error))
      return false
    } finally {
      if (owner === mutationOwner) mutationOwner = null
      if (owner === startGoalOwner) startGoalOwner = null
      if (mutationOwner === null && startGoalOwner === null) busy.value = false
    }
  }

  function applyConflictSnapshot(error: unknown) {
    const source = record(error)
    if (!source) return
    const data = record(source.data) ?? record(source.details) ?? source
    const current = data.current ?? data.goal ?? data.snapshot
    if (current !== undefined) applySnapshot(current)
  }

  async function mutate(
    method: 'goals.edit' | 'goals.pause' | 'goals.resume' | 'goals.clear',
    params: Record<string, unknown> = {},
  ): Promise<boolean> {
    const key = options.sessionKey.value
    const current = goal.value
    if (!key || !current || busy.value) return false
    const owner = Symbol(method)
    mutationOwner = owner
    busy.value = true
    try {
      const response = await options.rpc.call<GoalMutationResponse>(method, {
        sessionKey: key,
        clientRequestId: createClientRequestId(),
        expectedGoalId: current.goalId,
        expectedStateRevision: current.stateRevision,
        ...params,
      })
      if (owner !== mutationOwner || key !== options.sessionKey.value) return false
      const applied = applyMutationResponse(response)
      if ((method === 'goals.resume' || method === 'goals.edit') && applied) {
        rememberContinuityToken(response)
      }
      if (method === 'goals.pause' || method === 'goals.clear') {
        removeContinuityRecord(current)
      }
      return response.accepted === true
    } catch (error) {
      applyConflictSnapshot(error)
      options.notify?.(localizeGoalRpcError(error))
      return false
    } finally {
      if (owner === mutationOwner) {
        mutationOwner = null
        busy.value = false
      }
    }
  }

  const pause = () => mutate('goals.pause')
  const resume = () => mutate('goals.resume')
  const clear = () => mutate('goals.clear')
  const edit = (objective: string) => {
    const normalized = String(objective || '').trim()
    if (!goalObjectiveIsValid(normalized)) {
      options.notify?.(localizeGoalRpcError(
        Object.assign(new Error(), { code: 'INVALID_GOAL_OBJECTIVE' }),
      ))
      return Promise.resolve(false)
    }
    return mutate('goals.edit', { objective: normalized })
  }

  async function status(): Promise<GoalSnapshot | null> {
    const key = options.sessionKey.value
    if (!key) return null
    const result = await options.rpc.call<GoalStatusResult>('goals.status', { sessionKey: key })
    const snapshot = normalizeGoal(result?.goal)
    return snapshot?.sessionKey === key ? snapshot : null
  }

  const unsubscribeGoal = options.rpc.on('session.event.goal', onGoalEvent)

  watch(options.sessionKey, () => {
    disarm()
    // The Goal host intentionally switches provisional drafts after
    // sessions.create. Preserve the outer admission until ensureSessionKey
    // returns; its key fences distinguish that switch from stale navigation.
    resetGeneration(true)
  }, { flush: 'sync' })
  if (options.currentEpoch) {
    watch(options.currentEpoch, epoch => {
      if (!Number.isInteger(epoch) || epoch < 0 || epoch === acceptedEpoch) return
      if (epoch < acceptedEpoch) {
        if (epoch === 0) resetGeneration()
        return
      }
      if (goal.value) removeContinuityRecord(goal.value)
      goal.value = null
      tombstones.clear()
      acceptedSessionId = ''
      acceptedEpoch = epoch
      acceptedStreamSeq = -1
      connectionTakeoverAvailable.value = false
      reattaching.value = false
      automaticReattachWatermarks.clear()
    }, { flush: 'sync' })
  }

  onBeforeUnmount(unsubscribeGoal)

  return {
    draftArmed,
    goal,
    activeGoal,
    lastGoal,
    busy,
    connectionTakeoverAvailable,
    reattaching,
    elapsed,
    lastGoalElapsed,
    arm,
    disarm,
    startGoal,
    edit,
    pause,
    resume,
    takeOverConnection,
    clear,
    status,
    reset: resetGeneration,
    applyMutationResponse,
    applyHydration,
  }
}
