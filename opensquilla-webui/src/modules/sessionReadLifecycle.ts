import type { InjectionKey } from 'vue'
import type {
  ConversationCursor,
  ConversationCursorSignal,
  ConversationRuntime,
} from './conversationRuntime'
import type { ConversationSemanticEventKind } from './conversationEvents'
import type {
  ConversationSubscriptionAttempt,
  ConversationSubscriptionLifecycle,
} from './conversationSubscriptionLifecycle'

export type SessionReadActivity = 'idle' | 'foreground' | 'background' | 'unknown'
export type SessionReadReloadReason = 'generationChanged' | 'replayGap'
export type SessionReadHistoryDirection = 'latest' | 'before' | 'after'
export type SessionReadHistoryScope = 'complete' | 'latestWindow' | 'compacted'
export type SessionReadTimestamp = string | number | null
export type SessionReadJsonObject = Readonly<Record<string, unknown>>

export interface SessionReadRunModeLock {
  readonly locked: boolean
  readonly runMode: 'safe' | 'full' | null
  readonly source: string | null
  readonly additional: SessionReadJsonObject
}

/** Complete metadata projection returned by subscribe/hydrate. */
export interface SessionReadMetadata {
  readonly sessionKey: string
  readonly workspaceId: string | null
  readonly projectWorkspace: SessionReadJsonObject | null
  readonly projectWorkspaceDeferred: boolean
  readonly activeTaskGroupIds: readonly string[]
  readonly runModeLock: SessionReadRunModeLock
  readonly pendingUserInputs: readonly SessionReadJsonObject[]
  readonly collaboration: SessionReadJsonObject | null
  readonly routing: SessionReadJsonObject | null
  readonly currentPlan: SessionReadJsonObject | null
  readonly activePlanRun: SessionReadJsonObject | null
  readonly goal: SessionReadJsonObject | null
  readonly goalSnapshotStreamSeq: number | null
  readonly tasks: readonly SessionReadJsonObject[]
  readonly activeTask: SessionReadJsonObject | null
  readonly lastTask: SessionReadJsonObject | null
  readonly runStatus: string
  readonly queuedTaskIds: readonly string[]
  readonly epoch: number | null
  readonly hydrationComplete: boolean
  readonly deferredFields: readonly string[]
  readonly additional: SessionReadJsonObject
}

export interface SessionReadSnapshotEvent {
  readonly semanticKind: ConversationSemanticEventKind
  /** The event payload is immutable but otherwise opaque to this read Module. */
  readonly payload: SessionReadJsonObject
}

export interface SessionReadSnapshot {
  readonly sessionKey: string
  readonly taskId: string | null
  readonly events: readonly SessionReadSnapshotEvent[]
}

export interface SessionReadLive {
  readonly sessionKey: string
  readonly activity: SessionReadActivity
  readonly activeTaskId: string | null
  /** Fast-ACK metadata may be incomplete. Await `lease.metadata` for hydration. */
  readonly initialMetadata: SessionReadMetadata
  readonly snapshot: SessionReadSnapshot | null
  readonly reloadRequired: SessionReadReloadReason | null
}

export interface SessionReadMessageProvenance {
  readonly kind: string | null
  readonly sourceSessionKey: string | null
  readonly sourceTool: string | null
}

export interface SessionReadTurnContext {
  readonly turnId: string | null
  readonly promotedTurnId: string | null
  readonly appliedIteration: number | null
  readonly activityMarkers: readonly unknown[]
  readonly additional: SessionReadJsonObject
}

export interface SessionReadMessage {
  readonly id: string
  readonly messageId: string | null
  readonly transcriptId: string | null
  readonly role: string
  readonly text: string
  readonly createdAt: SessionReadTimestamp
  readonly reasoningContent: string | null
  readonly routerDecision: SessionReadJsonObject | null
  readonly artifacts: readonly SessionReadJsonObject[]
  readonly toolCalls: readonly unknown[]
  readonly timeline: readonly unknown[]
  readonly attachments: readonly SessionReadJsonObject[]
  readonly promptAnnotations: readonly unknown[]
  readonly provenance: SessionReadMessageProvenance
  readonly turnContext: SessionReadTurnContext | null
  readonly usage: SessionReadJsonObject | null
  readonly model: string | null
  readonly inputTokens: number | null
  readonly outputTokens: number | null
  readonly additional: SessionReadJsonObject
}

export interface SessionReadCompactionSummary {
  readonly id: string | null
  readonly compactionId: string | null
  readonly compactionIndex: number | null
  readonly triggerReason: string | null
  readonly summaryText: string
  readonly summaryFormat: string
  readonly coverageStatus: string
  readonly removedCount: number | null
  readonly keptCount: number | null
  readonly coveredThroughId: string | null
  readonly createdAt: number | null
  readonly additional: SessionReadJsonObject
}

export interface SessionReadTurnOutcome {
  readonly turnId: string
  readonly taskId: string | null
  readonly status: string
  readonly startedAt: number | null
  readonly finishedAt: number | null
  readonly outcome: SessionReadJsonObject
  readonly errorClass: string | null
  readonly retryable: boolean | null
  readonly activitySnapshot: SessionReadJsonObject | null
  readonly usage: SessionReadJsonObject | null
  readonly replayProof: Readonly<{
    usageCallIndex: number | null
    noPriorProviderDispatch: boolean | null
    replaySafe: boolean | null
    retryAfterMs: number | null
    userMessageId: string | null
    terminalMessage: string | null
  }>
  readonly additional: SessionReadJsonObject
}

export interface SessionReadHistoryPage {
  readonly messages: readonly SessionReadMessage[]
  readonly hasMore: boolean
  readonly oldestCursor: string | null
  readonly newestCursor: string | null
  readonly scope: SessionReadHistoryScope
  readonly loadedCount: number
  readonly pageSize: number
  /** Null means the legacy Gateway did not publish this proof field. */
  readonly canonicalAvailable: boolean | null
  /** Null means the legacy Gateway did not publish this proof field. */
  readonly canonicalComplete: boolean | null
  readonly compactionSummaries: readonly SessionReadCompactionSummary[]
  readonly turnOutcomes: readonly SessionReadTurnOutcome[]
  readonly additional: SessionReadJsonObject
}

export interface SessionReadHistoryOptions {
  readonly limit?: number
  readonly signal?: AbortSignal
  /** Relative request budget. The Adapter converts it to transport timeout policy. */
  readonly budgetMs?: number
  /** Absolute epoch-millisecond deadline shared across retries. */
  readonly deadlineAt?: number
}

export interface SessionReadHistoryReader {
  latest(options?: SessionReadHistoryOptions): Promise<SessionReadHistoryPage>
  before(cursor: string, options?: SessionReadHistoryOptions): Promise<SessionReadHistoryPage>
  after(cursor: string, options?: SessionReadHistoryOptions): Promise<SessionReadHistoryPage>
}

export interface SessionReadOpenRequest {
  readonly sessionKey: string
  /** Queue the first latest-history frame behind subscribe/snapshot. Defaults to true. */
  readonly includeInitialHistory?: boolean
}

/**
 * One conversation read lease. Connection admission, generation fencing,
 * physical frame ordering and generated wire objects stay behind the Module.
 */
export interface SessionReadLease {
  readonly criticalRequestsQueued: Promise<void>
  readonly live: Promise<SessionReadLive>
  readonly metadata: Promise<SessionReadMetadata>
  readonly history: SessionReadHistoryReader
  retryMetadata(): Promise<SessionReadMetadata>
  close(): Promise<void>
}

export interface SessionReadLeaseReader {
  current(): SessionReadLease | null
}

export interface SessionReadLifecycle extends SessionReadLeaseReader {
  open(request: SessionReadOpenRequest): SessionReadLease
}

/** Canonical, Adapter-facing resume position. It never crosses the public lease. */
export interface SessionReadPortCursor {
  readonly streamGeneration: string | null
  readonly streamSeq: number
}

export interface SessionReadPortOpenRequest {
  readonly sessionKey: string
  readonly includeInitialHistory: boolean
  readonly resumeFrom: SessionReadPortCursor
  readonly signal: AbortSignal
}

export interface SessionReadPortLive extends Omit<SessionReadLive, 'reloadRequired'> {
  readonly cursor: ConversationCursorSignal
  readonly snapshotCursor: ConversationCursorSignal | null
}

interface SessionReadPortHistoryRequestCommon {
  readonly limit: number
  readonly signal: AbortSignal
  readonly budgetMs?: number
  readonly deadlineAt?: number
}

export type SessionReadPortHistoryRequest =
  | (SessionReadPortHistoryRequestCommon & {
      readonly direction: 'latest'
      readonly cursor?: never
    })
  | (SessionReadPortHistoryRequestCommon & {
      readonly direction: 'before' | 'after'
      readonly cursor: string
    })

export interface SessionReadPortLease {
  readonly criticalRequestsQueued: Promise<void>
  readonly live: Promise<SessionReadPortLive>
  readonly metadata: Promise<SessionReadMetadata>
  readHistory(request: SessionReadPortHistoryRequest): Promise<SessionReadHistoryPage>
  retryMetadata(): Promise<SessionReadMetadata>
  close(): Promise<void>
}

/** Remote-but-owned seam implemented by generated-v4 and in-memory Adapters. */
export interface SessionReadPort {
  open(request: SessionReadPortOpenRequest): SessionReadPortLease
}

export interface SessionReadRuntimeOwner {
  readonly cursor: ConversationRuntime
  readonly subscriptions: ConversationSubscriptionLifecycle<SessionReadPortLease>
}

export interface SessionReadLifecycleFactory {
  create(owner: SessionReadRuntimeOwner): SessionReadLifecycle
}

export const SESSION_READ_LIFECYCLE_FACTORY_KEY: InjectionKey<SessionReadLifecycleFactory> =
  Symbol('SessionReadLifecycleFactory')

export type SessionReadLeaseCloseReason = 'closed' | 'superseded'

export class SessionReadLeaseClosedError extends Error {
  constructor(readonly reason: SessionReadLeaseCloseReason) {
    super(reason === 'superseded'
      ? 'The session read was superseded by a newer lease.'
      : 'The session read lease is closed.')
    this.name = 'SessionReadLeaseClosedError'
  }
}

export class SessionReadContractError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'SessionReadContractError'
  }
}

/** Domain failure projected by an Adapter when the requested session is gone. */
export class SessionReadSessionMissingError extends Error {
  readonly code = 'session-missing'

  constructor(message: string, readonly cause?: unknown) {
    super(message)
    this.name = 'SessionReadSessionMissingError'
  }
}

interface ActiveSessionRead {
  readonly sessionKey: string
  cursor: ConversationCursor
  attempt: ConversationSubscriptionAttempt | null
  portLease: SessionReadPortLease | null
  closedReason: SessionReadLeaseCloseReason | null
  close(reason: SessionReadLeaseCloseReason): Promise<void>
}

export interface CreateSessionReadLifecycleOptions {
  readonly port: SessionReadPort
  /** Externally owned, shared conversation consistency policy. */
  readonly runtime: ConversationRuntime
  /** Externally owned subscription identity/cancellation owner. */
  readonly subscriptions: ConversationSubscriptionLifecycle<SessionReadPortLease>
}

const DEFAULT_HISTORY_LIMIT = 100
const MAX_HISTORY_LIMIT = 200

function normalizedSessionKey(value: string): string {
  const key = value.trim()
  if (!key) throw new TypeError('SessionReadOpenRequest.sessionKey must not be empty.')
  return key
}

function normalizedCursor(value: string, direction: 'before' | 'after'): string {
  const cursor = value.trim()
  if (!cursor) throw new TypeError(`SessionReadHistoryReader.${direction} cursor must not be empty.`)
  return cursor
}

function normalizedLimit(value: number | undefined): number {
  if (value === undefined) return DEFAULT_HISTORY_LIMIT
  if (!Number.isInteger(value) || value < 1 || value > MAX_HISTORY_LIMIT) {
    throw new RangeError(`SessionReadHistoryOptions.limit must be between 1 and ${MAX_HISTORY_LIMIT}.`)
  }
  return value
}

function normalizedPositiveNumber(value: number | undefined, name: string): number | undefined {
  if (value === undefined) return undefined
  if (!Number.isFinite(value) || value <= 0) {
    throw new RangeError(`${name} must be a positive finite number.`)
  }
  return value
}

function linkAbortSignals(
  owner: AbortSignal,
  caller: AbortSignal | undefined,
): { signal: AbortSignal, dispose(): void } {
  if (!caller || caller === owner) return { signal: owner, dispose() {} }
  const controller = new AbortController()
  const abort = () => controller.abort()
  if (owner.aborted || caller.aborted) controller.abort()
  else {
    owner.addEventListener('abort', abort, { once: true })
    caller.addEventListener('abort', abort, { once: true })
  }
  return {
    signal: controller.signal,
    dispose() {
      owner.removeEventListener('abort', abort)
      caller.removeEventListener('abort', abort)
    },
  }
}

function assertCurrent(
  state: ActiveSessionRead,
  subscriptions: ConversationSubscriptionLifecycle<SessionReadPortLease>,
): void {
  if (state.closedReason) throw new SessionReadLeaseClosedError(state.closedReason)
  if (
    !state.attempt
    || !subscriptions.isCurrent(state.attempt, state.sessionKey)
  ) {
    throw new SessionReadLeaseClosedError('superseded')
  }
}

/**
 * Build the conversation-read Module around existing runtime ownership. The
 * Module creates no event source, cursor store or changes queue of its own.
 */
export function createSessionReadLifecycle(
  options: CreateSessionReadLifecycleOptions,
): SessionReadLifecycle {
  let active: ActiveSessionRead | null = null
  let currentLease: SessionReadLease | null = null
  let openSequence = 0

  function open(request: SessionReadOpenRequest): SessionReadLease {
    const sessionKey = normalizedSessionKey(request.sessionKey)
    const prior = active
    const seed = prior?.sessionKey === sessionKey
      ? prior.cursor
      : options.runtime.createCursor(sessionKey)
    const releasePrior = prior
      ? prior.close('superseded').catch(() => {})
      : Promise.resolve()

    const state: ActiveSessionRead = {
      sessionKey,
      cursor: seed,
      attempt: null,
      portLease: null,
      closedReason: null,
      close: async () => {},
    }
    active = state

    const identity = {
      key: sessionKey,
      sinceStreamGeneration: seed.streamGeneration,
      sinceStreamSeq: seed.streamSeq,
      bootstrapGeneration: ++openSequence,
      bootstrapAttempt: 0,
    }
    const acquire = options.subscriptions.start(identity, undefined, async attempt => {
      state.attempt = attempt
      // A generation-pinned unsubscribe for the prior lease can still target
      // the same physical connection. Finish that release before sending the
      // replacement subscribe, otherwise the late unsubscribe can tear down
      // the new subscription on the Gateway.
      await releasePrior
      assertCurrent(state, options.subscriptions)
      const portLease = options.port.open({
        sessionKey,
        includeInitialHistory: request.includeInitialHistory !== false,
        resumeFrom: {
          streamGeneration: seed.streamGeneration,
          streamSeq: seed.streamSeq,
        },
        signal: attempt.controller.signal,
      })
      state.portLease = portLease
      return portLease
    })

    const criticalRequestsQueued = acquire.then(portLease => portLease.criticalRequestsQueued)

    const live = acquire.then(portLease => portLease.live).then(value => {
      assertCurrent(state, options.subscriptions)
      if (value.sessionKey !== sessionKey) {
        throw new SessionReadContractError(
          'SessionReadPort returned live state for a different session.',
        )
      }
      const generation = options.runtime.observeGeneration(state.cursor, value.cursor)
      state.cursor = generation.cursor
      if (value.snapshotCursor) {
        const snapshot = options.runtime.acceptSnapshot(state.cursor, value.snapshotCursor)
        if (snapshot.accepted) state.cursor = snapshot.cursor
      }
      const replay = options.runtime.applyReplayCursor(
        state.cursor,
        value.cursor,
        generation.reset,
      )
      state.cursor = replay.cursor
      return Object.freeze({
        sessionKey: value.sessionKey,
        activity: value.activity,
        activeTaskId: value.activeTaskId,
        initialMetadata: value.initialMetadata,
        snapshot: value.snapshot,
        reloadRequired: replay.requiresHistory
          ? (generation.reset ? 'generationChanged' : 'replayGap')
          : null,
      } satisfies SessionReadLive)
    })

    const metadata = acquire.then(portLease => portLease.metadata).then(value => {
      assertCurrent(state, options.subscriptions)
      if (value.sessionKey !== sessionKey) {
        throw new SessionReadContractError(
          'SessionReadPort returned metadata for a different session.',
        )
      }
      return value
    })

    async function readHistory(
      direction: SessionReadHistoryDirection,
      cursor: string | null,
      historyOptions: SessionReadHistoryOptions = {},
    ): Promise<SessionReadHistoryPage> {
      const limit = normalizedLimit(historyOptions.limit)
      const budgetMs = normalizedPositiveNumber(
        historyOptions.budgetMs,
        'SessionReadHistoryOptions.budgetMs',
      )
      const deadlineAt = normalizedPositiveNumber(
        historyOptions.deadlineAt,
        'SessionReadHistoryOptions.deadlineAt',
      )
      assertCurrent(state, options.subscriptions)
      const portLease = await acquire
      const ownerSignal = state.attempt?.controller.signal ?? AbortSignal.abort()
      const linked = linkAbortSignals(ownerSignal, historyOptions.signal)
      const portRequest: SessionReadPortHistoryRequest = direction === 'latest'
        ? { direction, limit, signal: linked.signal, budgetMs, deadlineAt }
        : {
            direction,
            cursor: cursor ?? '',
            limit,
            signal: linked.signal,
            budgetMs,
            deadlineAt,
          }
      try {
        const value = await portLease.readHistory(portRequest)
        assertCurrent(state, options.subscriptions)
        return value
      } finally {
        linked.dispose()
      }
    }

    const history: SessionReadHistoryReader = Object.freeze({
      latest: (historyOptions?: SessionReadHistoryOptions) => readHistory(
        'latest',
        null,
        historyOptions,
      ),
      before: (cursor: string, historyOptions?: SessionReadHistoryOptions) => readHistory(
        'before',
        normalizedCursor(cursor, 'before'),
        historyOptions,
      ),
      after: (cursor: string, historyOptions?: SessionReadHistoryOptions) => readHistory(
        'after',
        normalizedCursor(cursor, 'after'),
        historyOptions,
      ),
    })

    async function retryMetadata(): Promise<SessionReadMetadata> {
      assertCurrent(state, options.subscriptions)
      const portLease = await acquire
      const value = await portLease.retryMetadata()
      assertCurrent(state, options.subscriptions)
      if (value.sessionKey !== sessionKey) {
        throw new SessionReadContractError(
          'SessionReadPort returned metadata for a different session.',
        )
      }
      return value
    }

    async function closeState(reason: SessionReadLeaseCloseReason): Promise<void> {
      if (state.closedReason) return
      state.closedReason = reason
      if (
        state.attempt
        && options.subscriptions.isCurrent(state.attempt, sessionKey)
      ) {
        options.subscriptions.cancel()
      }
      // start() finishes after Port acquisition, so cancel() may no longer own
      // this attempt. Abort the stored controller as the authoritative fence.
      state.attempt?.controller.abort()
      if (active === state) {
        active = null
        currentLease = null
      }
      const portLease = state.portLease
      if (portLease) {
        await portLease.close()
        return
      }
      try {
        await (await acquire).close()
      } catch {
        // A locally closed acquisition has no remote lease left to release.
      }
    }
    state.close = closeState

    // A failed live acquisition has no useful subscription left. Metadata and
    // history failures remain independent and do not tear down a healthy live lease.
    void live.catch(async error => {
      // Preserve a terminal domain absence long enough for the lease consumer
      // to project it. Closing aborts the owner signal synchronously; without
      // this one-turn handoff the consumer would misclassify the Adapter's
      // SessionReadSessionMissingError as an ordinary local cancellation.
      // Other live failures keep the existing immediate close/fence behavior.
      if (error instanceof SessionReadSessionMissingError) await Promise.resolve()
      if (!state.closedReason) await closeState('closed').catch(() => {})
    })
    void criticalRequestsQueued.catch(() => {})
    void metadata.catch(() => {})

    const lease = Object.freeze({
      criticalRequestsQueued,
      live,
      metadata,
      history,
      retryMetadata,
      close: () => closeState('closed'),
    })
    currentLease = lease
    return lease
  }

  return Object.freeze({
    open,
    current: () => currentLease,
  })
}

/** Bind the private production Port once; each UI owner supplies its shared runtime. */
export function createSessionReadLifecycleFactory(
  port: SessionReadPort,
): SessionReadLifecycleFactory {
  return Object.freeze({
    create(owner: SessionReadRuntimeOwner) {
      return createSessionReadLifecycle({
        port,
        runtime: owner.cursor,
        subscriptions: owner.subscriptions,
      })
    },
  })
}
