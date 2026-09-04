import type { TransportCallOptions as RpcCallOptions } from './transportTypes'
import {
  SESSIONS_MESSAGES_HYDRATE_METHOD,
  type SessionsMessagesHydrateParams,
  type SessionsMessagesHydrateResult,
} from '@/contracts/generated/v4/sessionsMessagesHydrate'
import {
  validateSessionsMessagesHydrateParams,
  validateSessionsMessagesHydrateResult,
} from '@/contracts/generated/v4/sessionsMessagesHydrateValidators.mjs'
import {
  SESSIONS_MESSAGES_SNAPSHOT_METHOD,
  type SessionsMessagesSnapshotParams,
  type SessionsMessagesSnapshotResult,
} from '@/contracts/generated/v4/sessionsMessagesSnapshot'
import {
  validateSessionsMessagesSnapshotParams,
  validateSessionsMessagesSnapshotResult,
} from '@/contracts/generated/v4/sessionsMessagesSnapshotValidators.mjs'
import {
  SESSIONS_MESSAGES_SUBSCRIBE_METHOD,
  type SessionsMessagesSubscribeParams,
  type SessionsMessagesSubscribeResult,
} from '@/contracts/generated/v4/sessionsMessagesSubscribe'
import {
  validateSessionsMessagesSubscribeParams,
  validateSessionsMessagesSubscribeResult,
} from '@/contracts/generated/v4/sessionsMessagesSubscribeValidators.mjs'
import {
  SESSIONS_MESSAGES_UNSUBSCRIBE_METHOD,
  type SessionsMessagesUnsubscribeParams,
} from '@/contracts/generated/v4/sessionsMessagesUnsubscribe'
import {
  validateSessionsMessagesUnsubscribeParams,
  validateSessionsMessagesUnsubscribeResult,
} from '@/contracts/generated/v4/sessionsMessagesUnsubscribeValidators.mjs'
import { projectConversationSnapshotEvent } from './conversationContentV4'
import {
  requestV4SessionHistory,
  type SessionHistoryV4Policy,
} from './sessionHistoryV4'
import {
  SessionReadContractError,
  type SessionReadActivity,
  type SessionReadHistoryPage,
  type SessionReadJsonObject,
  type SessionReadMetadata,
  type SessionReadPort,
  type SessionReadPortHistoryRequest,
  type SessionReadPortLease,
  type SessionReadPortLive,
  type SessionReadPortOpenRequest,
  type SessionReadSnapshot,
} from '@/modules/sessionReadLifecycle'
import { mapSessionReadError } from './sessionReadErrorMapping'

const READY_TIMEOUT_MS = 15_000
const READ_TIMEOUT_MS = 15_000
const SNAPSHOT_TIMEOUT_MS = 3_000
const INITIAL_HISTORY_LIMIT = 100

interface SessionReadV4Transport {
  request<T = unknown>(
    method: string,
    params?: Record<string, unknown>,
    options?: RpcCallOptions,
  ): Promise<T>
  ready?(options?: {
    timeoutMs?: number
    signal?: AbortSignal
    timeoutAction?: 'reject' | 'reconnect'
    abortAction?: 'reject' | 'reconnect'
  }): Promise<void>
  readonly generation: number
}

export interface SessionReadV4AdapterOptions {
  readonly concurrentHistoryReads?: () => boolean
  readonly now?: () => number
}

type MetadataWire = SessionsMessagesSubscribeResult | SessionsMessagesHydrateResult

interface SentLatch {
  readonly promise: Promise<number>
  sent(generation: number): void
  failed(error: unknown): void
}

interface OpenContext {
  readonly expectedGeneration: number
  readonly criticalRequestsQueued: Promise<void>
  readonly live: Promise<SessionReadPortLive>
  readonly metadata: Promise<SessionReadMetadata>
  readHistory(request: SessionReadPortHistoryRequest): Promise<SessionReadHistoryPage>
  retryMetadata(): Promise<SessionReadMetadata>
}

function sentLatch(): SentLatch {
  let settled = false
  let resolve!: (generation: number) => void
  let reject!: (error: unknown) => void
  const promise = new Promise<number>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  // A sibling critical-frame failure can make a later latch unreachable.
  // Observe it internally while retaining rejection semantics for awaiters.
  void promise.catch(() => {})
  return {
    promise,
    sent(generation) {
      if (settled) return
      settled = true
      resolve(generation)
    },
    failed(error) {
      if (settled) return
      settled = true
      reject(error)
    },
  }
}

function callOptions(
  signal: AbortSignal,
  timeoutMs: number,
  expectedGeneration: number,
  onSent?: (generation: number) => void,
): RpcCallOptions {
  return {
    signal,
    timeoutMs,
    timeoutAction: 'reject',
    abortAction: 'reject',
    expectedGeneration,
    ...(onSent ? { onSent } : {}),
  }
}

function releaseOptions(expectedGeneration: number): RpcCallOptions {
  return {
    timeoutMs: READ_TIMEOUT_MS,
    timeoutAction: 'reject',
    abortAction: 'reject',
    expectedGeneration,
  }
}

function errorCode(error: unknown): string {
  if (!error || typeof error !== 'object') return ''
  const candidate = error as { code?: unknown; data?: { code?: unknown } }
  const code = candidate.code ?? candidate.data?.code
  return typeof code === 'string' ? code.toUpperCase() : ''
}

function isMissingMethod(error: unknown): boolean {
  return errorCode(error) === 'METHOD_NOT_FOUND'
}

function subscriptionError(error: unknown): unknown {
  return mapSessionReadError(error)
}

function invalidContract(method: string): SessionReadContractError {
  return new SessionReadContractError(`${method} violated its generated v4 Contract.`)
}

function requireParams(
  method: string,
  params: object,
  validate: (value: unknown) => boolean,
): void {
  if (!validate(params)) throw invalidContract(`${method} params`)
}

function requireResult<T>(
  method: string,
  result: unknown,
  validate: (value: unknown) => boolean,
): T {
  if (!validate(result)) throw invalidContract(`${method} result`)
  return result as T
}

function abortError(message = 'Session read lease is closed.'): Error {
  const error = new Error(message)
  error.name = 'AbortError'
  return error
}

function objectValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function projectJson(value: unknown): unknown {
  if (Array.isArray(value)) return Object.freeze(value.map(projectJson))
  const item = objectValue(value)
  if (!item) return value
  return Object.freeze(Object.fromEntries(
    Object.entries(item).map(([key, child]) => [key, projectJson(child)]),
  ))
}

function projectObject(value: unknown): SessionReadJsonObject | null {
  const item = objectValue(value)
  return item ? projectJson(item) as SessionReadJsonObject : null
}

function projectObjectArray(value: unknown): readonly SessionReadJsonObject[] {
  if (!Array.isArray(value)) return Object.freeze([])
  return Object.freeze(value.flatMap(item => {
    const projected = projectObject(item)
    return projected ? [projected] : []
  }))
}

function additionalFields(
  value: Record<string, unknown>,
  known: ReadonlySet<string>,
): SessionReadJsonObject {
  return Object.freeze(Object.fromEntries(
    Object.entries(value)
      .filter(([key]) => !known.has(key))
      .map(([key, child]) => [key, projectJson(child)]),
  ))
}

function textValue(...values: unknown[]): string | null {
  for (const value of values) {
    if (typeof value === 'string' && value.trim()) return value.trim()
    if (typeof value === 'number' && Number.isFinite(value)) return String(value)
  }
  return null
}

function numberValue(...values: unknown[]): number | null {
  for (const value of values) {
    if (typeof value === 'number' && Number.isFinite(value)) return value
  }
  return null
}

const METADATA_FIELDS = new Set([
  'key',
  'workspaceId',
  'projectWorkspace',
  'projectWorkspaceDeferred',
  'active_task_group_ids',
  'run_mode_lock',
  'pendingUserInputs',
  'collaboration',
  'routing',
  'currentPlan',
  'activePlanRun',
  'goal',
  'goalSnapshotStreamSeq',
  'tasks',
  'active_task',
  'last_task',
  'run_status',
  'queued_task_ids',
  'epoch',
  'hydration_complete',
  'deferred_fields',
  'subscribed',
  'stream_generation',
  'current_stream_seq',
  'replay_complete',
  'replay_gap_reason',
  'replayed_count',
])

function projectMetadata(value: MetadataWire): SessionReadMetadata {
  const lock = value.run_mode_lock
  const raw = value as unknown as Record<string, unknown>
  const rawLock = lock as unknown as Record<string, unknown>
  return Object.freeze({
    sessionKey: value.key,
    workspaceId: value.workspaceId,
    projectWorkspace: projectObject(value.projectWorkspace),
    projectWorkspaceDeferred: value.projectWorkspaceDeferred,
    activeTaskGroupIds: Object.freeze([...value.active_task_group_ids]),
    runModeLock: Object.freeze({
      locked: lock.locked,
      runMode: lock.runMode === 'safe' || lock.runMode === 'full' ? lock.runMode : null,
      source: textValue(lock.source),
      additional: additionalFields(rawLock, new Set(['locked', 'runMode', 'source'])),
    }),
    pendingUserInputs: projectObjectArray(value.pendingUserInputs),
    collaboration: projectObject(value.collaboration),
    routing: projectObject(value.routing),
    currentPlan: projectObject(value.currentPlan),
    activePlanRun: projectObject(value.activePlanRun),
    goal: projectObject(value.goal),
    goalSnapshotStreamSeq: numberValue(value.goalSnapshotStreamSeq),
    tasks: projectObjectArray(value.tasks),
    activeTask: projectObject(value.active_task),
    lastTask: projectObject(value.last_task),
    runStatus: value.run_status,
    queuedTaskIds: Object.freeze([...(value.queued_task_ids ?? [])]),
    epoch: numberValue(value.epoch),
    hydrationComplete: value.hydration_complete,
    deferredFields: Object.freeze([...value.deferred_fields]),
    additional: additionalFields(raw, METADATA_FIELDS),
  })
}

function activeTaskId(value: MetadataWire): string | null {
  const task = objectValue(value.active_task)
  return textValue(task?.task_id, task?.taskId, task?.id)
}

function activity(
  value: MetadataWire,
  snapshot: SessionsMessagesSnapshotResult | null,
): SessionReadActivity {
  if (snapshot?.task_id) return 'foreground'
  const status = value.run_status.trim().toLowerCase()
  if (status === 'queued' || status === 'running' || status === 'approval_pending') {
    return 'foreground'
  }
  if (value.active_task_group_ids.length > 0) return 'background'
  if (status === 'idle' || status === 'completed' || status === 'succeeded') return 'idle'
  return 'unknown'
}

function projectSnapshot(value: SessionsMessagesSnapshotResult): SessionReadSnapshot {
  return Object.freeze({
    sessionKey: value.key,
    taskId: value.task_id,
    events: Object.freeze(value.events.flatMap(event => {
      const projected = projectConversationSnapshotEvent(event.event, projectObject(event.payload))
      return projected ? [Object.freeze({ ...projected, payload: Object.freeze(projected.payload) })] : []
    })),
  })
}

async function hydrate(
  rpc: SessionReadV4Transport,
  sessionKey: string,
  signal: AbortSignal,
  expectedGeneration: number,
): Promise<SessionReadMetadata> {
  const params: SessionsMessagesHydrateParams = { key: sessionKey }
  requireParams(SESSIONS_MESSAGES_HYDRATE_METHOD, params, validateSessionsMessagesHydrateParams)
  let raw: unknown
  try {
    raw = await rpc.request(
      SESSIONS_MESSAGES_HYDRATE_METHOD,
      params,
      callOptions(signal, READ_TIMEOUT_MS, expectedGeneration),
    )
  } catch (error) {
    throw mapSessionReadError(error)
  }
  const result = requireResult<SessionsMessagesHydrateResult>(
    SESSIONS_MESSAGES_HYDRATE_METHOD,
    raw,
    validateSessionsMessagesHydrateResult,
  )
  if (result.key !== sessionKey) throw invalidContract(SESSIONS_MESSAGES_HYDRATE_METHOD)
  return projectMetadata(result)
}

async function optionalSnapshot(
  rpc: SessionReadV4Transport,
  params: SessionsMessagesSnapshotParams,
  signal: AbortSignal,
  expectedGeneration: number,
  latch: SentLatch,
): Promise<SessionsMessagesSnapshotResult | null> {
  try {
    const raw = await rpc.request(
      SESSIONS_MESSAGES_SNAPSHOT_METHOD,
      params,
      callOptions(signal, SNAPSHOT_TIMEOUT_MS, expectedGeneration, latch.sent),
    )
    return requireResult<SessionsMessagesSnapshotResult>(
      SESSIONS_MESSAGES_SNAPSHOT_METHOD,
      raw,
      validateSessionsMessagesSnapshotResult,
    )
  } catch (error) {
    if (isMissingMethod(error)) {
      // A pre-capability Gateway can reject before onSent. Treat only this
      // capability miss as a terminally absent snapshot frame.
      latch.sent(expectedGeneration)
      return null
    }
    const projected = mapSessionReadError(error)
    latch.failed(projected)
    throw projected
  }
}

function assertFrameGeneration(expected: number, generations: readonly number[]): void {
  if (generations.some(generation => generation !== expected)) {
    throw new SessionReadContractError(
      'Session read critical requests crossed a connection generation.',
    )
  }
}

/**
 * Generated-contract Adapter. It owns wire validation/projection, connection
 * admission, frame order and generation-pinned release; no generated type
 * crosses SessionReadPort.
 */
export function createV4SessionReadPort(
  rpc: SessionReadV4Transport,
  options: SessionReadV4AdapterOptions = {},
): SessionReadPort {
  const historyPolicy: SessionHistoryV4Policy = {
    concurrentHistoryReads: options.concurrentHistoryReads ?? (() => true),
    ...(options.now ? { now: options.now } : {}),
  }
  return Object.freeze({
    open(request: SessionReadPortOpenRequest): SessionReadPortLease {
      let closed = false
      let subscribedGeneration: number | null = null

      const setup = (async (): Promise<OpenContext> => {
        await rpc.ready?.({
          timeoutMs: READY_TIMEOUT_MS,
          signal: request.signal,
          timeoutAction: 'reject',
          abortAction: 'reject',
        })
        if (closed || request.signal.aborted) {
          throw abortError('Session read closed before connection admission.')
        }
        const expectedGeneration = rpc.generation
        const subscribeParams: SessionsMessagesSubscribeParams = {
          key: request.sessionKey,
          since_stream_generation: request.resumeFrom.streamGeneration,
          since_stream_seq: request.resumeFrom.streamSeq,
          fast_ack: true,
        }
        const snapshotParams: SessionsMessagesSnapshotParams = { key: request.sessionKey }
        requireParams(
          SESSIONS_MESSAGES_SUBSCRIBE_METHOD,
          subscribeParams,
          validateSessionsMessagesSubscribeParams,
        )
        requireParams(
          SESSIONS_MESSAGES_SNAPSHOT_METHOD,
          snapshotParams,
          validateSessionsMessagesSnapshotParams,
        )

        const subscribeSent = sentLatch()
        const snapshotSent = sentLatch()
        const subscribePromise = rpc.request(
          SESSIONS_MESSAGES_SUBSCRIBE_METHOD,
          subscribeParams,
          callOptions(
            request.signal,
            READ_TIMEOUT_MS,
            expectedGeneration,
            generation => {
              subscribedGeneration = generation
              subscribeSent.sent(generation)
            },
          ),
        ).then(raw => requireResult<SessionsMessagesSubscribeResult>(
          SESSIONS_MESSAGES_SUBSCRIBE_METHOD,
          raw,
          validateSessionsMessagesSubscribeResult,
        )).then(result => {
          if (result.key !== request.sessionKey || !result.subscribed) {
            throw invalidContract(SESSIONS_MESSAGES_SUBSCRIBE_METHOD)
          }
          return result
        }).catch(error => {
          const projected = subscriptionError(error)
          subscribeSent.failed(projected)
          throw projected
        })
        const snapshotPromise = optionalSnapshot(
          rpc,
          snapshotParams,
          request.signal,
          expectedGeneration,
          snapshotSent,
        ).then(result => {
          if (result && result.key !== request.sessionKey) {
            throw invalidContract(SESSIONS_MESSAGES_SNAPSHOT_METHOD)
          }
          return result
        })

        const liveFramesQueued = Promise.all([
          subscribeSent.promise,
          snapshotSent.promise,
        ]).then(generations => {
          assertFrameGeneration(expectedGeneration, generations)
        })

        const historySent = request.includeInitialHistory ? sentLatch() : null
        const initialHistory = historySent
          ? liveFramesQueued.then(() => requestV4SessionHistory(
              rpc,
              request.sessionKey,
              {
                direction: 'latest',
                limit: INITIAL_HISTORY_LIMIT,
                signal: request.signal,
              },
              {
                includeSummaries: true,
                expectedGeneration,
                onSent: historySent.sent,
                policy: historyPolicy,
                contractError: invalidContract,
              },
            )).catch(error => {
              historySent.failed(error)
              throw error
            })
          : null
        // Eager history may finish before a consumer asks for it. Observe the
        // rejection here while preserving it for the first latest() call.
        void initialHistory?.catch(() => {})

        const criticalRequestsQueued = liveFramesQueued.then(async () => {
          if (!historySent) return
          const historyGeneration = await historySent.promise
          assertFrameGeneration(expectedGeneration, [historyGeneration])
        })

        const live = Promise.all([
          subscribePromise,
          snapshotPromise,
          criticalRequestsQueued,
        ]).then(([subscription, snapshot]) => Object.freeze({
          sessionKey: request.sessionKey,
          activity: activity(subscription, snapshot),
          activeTaskId: snapshot?.task_id ?? activeTaskId(subscription),
          initialMetadata: projectMetadata(subscription),
          snapshot: snapshot ? projectSnapshot(snapshot) : null,
          cursor: Object.freeze({
            sessionKey: request.sessionKey,
            sessionEpoch: subscription.epoch,
            streamGeneration: subscription.stream_generation,
            currentStreamSeq: subscription.current_stream_seq,
            replayComplete: subscription.replay_complete,
            replayGapReason: subscription.replay_gap_reason,
          }),
          snapshotCursor: snapshot
            ? Object.freeze({
                sessionKey: request.sessionKey,
                sessionEpoch: subscription.epoch,
                streamGeneration: snapshot.stream_generation,
                currentStreamSeq: snapshot.current_stream_seq,
              })
            : null,
        } satisfies SessionReadPortLive))
        void live.catch(() => {})

        const metadata = subscribePromise.then(subscription => {
          if (subscription.hydration_complete) return projectMetadata(subscription)
          return criticalRequestsQueued.then(() => hydrate(
            rpc,
            request.sessionKey,
            request.signal,
            expectedGeneration,
          ))
        })
        void metadata.catch(() => {})
        void criticalRequestsQueued.catch(() => {})

        let initialHistoryAvailable = initialHistory !== null
        let retry: Promise<SessionReadMetadata> | null = null

        async function readHistory(
          historyRequest: SessionReadPortHistoryRequest,
        ): Promise<SessionReadHistoryPage> {
          if (closed || historyRequest.signal.aborted) throw abortError()
          if (historyRequest.direction !== 'latest' && !historyRequest.cursor.trim()) {
            throw new TypeError(`${historyRequest.direction} session history requires a cursor.`)
          }
          if (
            historyRequest.direction === 'latest'
            && historyRequest.limit === INITIAL_HISTORY_LIMIT
            && initialHistoryAvailable
            && initialHistory
          ) {
            initialHistoryAvailable = false
            return initialHistory
          }
          return requestV4SessionHistory(
            rpc,
            request.sessionKey,
            historyRequest,
            {
              includeSummaries: true,
              expectedGeneration,
              policy: historyPolicy,
              contractError: invalidContract,
            },
          )
        }

        function retryMetadata(): Promise<SessionReadMetadata> {
          if (closed || request.signal.aborted) return Promise.reject(abortError())
          if (retry) return retry
          const current = criticalRequestsQueued.then(() => hydrate(
            rpc,
            request.sessionKey,
            request.signal,
            expectedGeneration,
          ))
          const observed = current.finally(() => {
            if (retry === observed) retry = null
          })
          retry = observed
          void retry.catch(() => {})
          return retry
        }

        return {
          expectedGeneration,
          criticalRequestsQueued,
          live,
          metadata,
          readHistory,
          retryMetadata,
        }
      })().catch(error => {
        throw mapSessionReadError(error)
      })
      void setup.catch(() => {})

      const historyRead = (historyRequest: SessionReadPortHistoryRequest) => setup.then(
        context => context.readHistory(historyRequest),
      )

      async function close(): Promise<void> {
        if (closed) return
        closed = true
        try {
          await setup
        } catch {
          // A physical subscribe send can precede a synchronous setup/ACK
          // failure. Release that generation below when it is still current.
        }
        const generation = subscribedGeneration
        if (generation === null || rpc.generation !== generation) return
        const params: SessionsMessagesUnsubscribeParams = { key: request.sessionKey }
        requireParams(
          SESSIONS_MESSAGES_UNSUBSCRIBE_METHOD,
          params,
          validateSessionsMessagesUnsubscribeParams,
        )
        try {
          const result = await rpc.request(
            SESSIONS_MESSAGES_UNSUBSCRIBE_METHOD,
            params,
            releaseOptions(generation),
          )
          if (!validateSessionsMessagesUnsubscribeResult(result)) {
            throw invalidContract(SESSIONS_MESSAGES_UNSUBSCRIBE_METHOD)
          }
        } catch (error) {
          if (!isMissingMethod(error)) throw error
        }
      }

      return Object.freeze({
        criticalRequestsQueued: setup.then(context => context.criticalRequestsQueued),
        live: setup.then(context => context.live),
        metadata: setup.then(context => context.metadata),
        readHistory: historyRead,
        retryMetadata: () => setup.then(context => context.retryMetadata()),
        close,
      })
    },
  })
}
