import type { RpcCallOptions, RpcEventHandler } from '@/lib/rpc'
import {
  SESSIONS_SUBSCRIBE_METHOD,
} from '@/contracts/generated/v4/sessionsSubscribe'
import {
  SESSIONS_UNSUBSCRIBE_METHOD,
} from '@/contracts/generated/v4/sessionsUnsubscribe'
import {
  SESSIONS_CHANGED_EVENT,
  type SessionsChangedEventPayload,
} from '@/contracts/generated/v4/sessionsChanged'
import { validateSessionsChangedEventPayload } from '@/contracts/generated/v4/sessionsChangedValidators.mjs'
import type {
  SessionDirectoryChange,
  SessionDirectoryChangeReason,
  SessionDirectoryChanges,
  SessionDirectoryChangeSubscription,
  SessionDirectoryTask,
} from '@/modules/sessionDirectoryChanges'

const SESSION_DIRECTORY_CHANGE_TIMEOUT_MS = 10_000
const SESSION_DIRECTORY_CHANGE_CALL_OPTIONS: RpcCallOptions = {
  timeoutMs: SESSION_DIRECTORY_CHANGE_TIMEOUT_MS,
  // A directory lease is a logical subscription. Cancelling it must never
  // recycle the shared WebSocket used by chat and other domains.
  timeoutAction: 'reject',
  abortAction: 'reject',
}
const PRIVATE_STATE_EVENT = '_state'

interface SessionDirectoryChangesRpcTransport {
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
  markUnsupported?(method: string): void
}

interface SessionDirectoryChangesEventTransport {
  subscribe(event: string, handler: RpcEventHandler): { close(): void }
}

export interface SessionDirectoryChangesAdapterOptions {
  warn?: (message: string, error?: unknown) => void
}

function objectValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function textValue(...values: unknown[]): string | undefined {
  for (const value of values) {
    if (typeof value === 'string' && value.trim()) return value.trim()
  }
  return undefined
}

function errorCode(error: unknown): string {
  if (!error || typeof error !== 'object') return ''
  const candidate = error as {
    code?: unknown
    data?: { code?: unknown }
  }
  const code = candidate.code ?? candidate.data?.code
  return typeof code === 'string' ? code.toUpperCase() : ''
}

function normalizeReason(reason: string): SessionDirectoryChangeReason {
  switch (reason) {
    case 'created':
      return 'created'
    case 'deleted':
      return 'deleted'
    case 'renamed':
      return 'renamed'
    case 'forked':
      return 'forked'
    case 'task_queued':
      return 'taskQueued'
    case 'task_running':
      return 'taskRunning'
    case 'task_terminal':
      return 'taskTerminal'
    case 'auto_titled':
      return 'autoTitled'
    case 'cron_static_message':
      return 'cronStaticMessage'
    case 'turn_complete':
    case 'cron_result':
    case 'cron_system_event':
    case 'updated':
      // These reasons invalidate the directory but do not carry a task
      // lifecycle transition that the domain needs to interpret.
      return 'updated'
    default:
      // Preserve forward-compatibility without leaking an open-ended wire
      // reason string into the domain API. Consumers treat this exactly as
      // a normal invalidation and must not infer task attention from it.
      return 'unknown'
  }
}

function normalizeTask(value: unknown): SessionDirectoryTask | undefined {
  const task = objectValue(value)
  if (!task) return undefined
  const id = textValue(task.task_id) || textValue(task.taskId) || textValue(task.id)
  if (!id) return undefined
  const status = textValue(task.status)
  return status ? { id, status } : { id }
}

/**
 * Keep the decoder tolerant of the two shapes that pre-Contract v4 clients
 * actually emitted:
 *
 *   { key: "..." }                 // fork/invalidation-only notifications
 *   { session_key: "..." }         // older event helpers
 *
 * These are deliberately normalized only in the Adapter's input copy.  The
 * language-neutral Contract and the server producer still require the
 * canonical `key` + `reason` fields, while an old event can continue to
 * invalidate the directory instead of silently disabling refreshes.
 */
function normalizeLegacyIdentityPayload(payload: unknown): unknown {
  const value = objectValue(payload)
  if (!value) return payload

  const key = textValue(value.key, value.session_key, value.sessionKey)
  if (!key) return payload

  const normalized: Record<string, unknown> = { ...value, key }
  // A missing reason carries no task semantics, but it is still a useful
  // directory invalidation.  Do not coerce a present non-string reason; the
  // generated validator should continue to reject that malformed payload.
  if (value.reason === undefined || value.reason === null) normalized.reason = 'unknown'
  return normalized
}

/**
 * Decode canonical and older unversioned sessions.changed payloads into the
 * small semantic projection consumed by the WebUI. Generated validators are
 * intentionally used only at this Adapter boundary.
 */
export function decodeSessionDirectoryChange(
  payload: unknown,
): SessionDirectoryChange | null {
  const normalizedPayload = normalizeLegacyIdentityPayload(payload)
  if (!validateSessionsChangedEventPayload(normalizedPayload)) return null
  const value = objectValue(normalizedPayload as SessionsChangedEventPayload)
  if (!value) return null
  const key = textValue(value.key)
  const wireReason = textValue(value.reason)
  if (!key || !wireReason) return null

  const reason = normalizeReason(wireReason)
  let changedTask = normalizeTask(value.changed_task) || normalizeTask(value.changedTask)
  let lastTask = normalizeTask(value.last_task) || normalizeTask(value.lastTask)
  const topLevelStatus = textValue(value.status)

  // Older scheduler paths publish taskId/task_id and status at the top level.
  // Project those fields once here so attention and directory consumers do not
  // each maintain another snake/camel compatibility branch.
  if (!changedTask && !lastTask) {
    const taskId = textValue(value.task_id) || textValue(value.taskId)
    if (taskId) {
      const status = textValue(value.status)
      const task = status ? { id: taskId, status } : { id: taskId }
      if (reason === 'taskTerminal' || reason === 'cronStaticMessage') lastTask = task
      else changedTask = task
    }
  }

  // Some older producers put the terminal status beside an otherwise
  // complete last_task/changed_task object. Preserve that fallback once so
  // the domain attention policy does not need another wire-specific branch.
  if (topLevelStatus) {
    if (changedTask && !changedTask.status) changedTask = { ...changedTask, status: topLevelStatus }
    if (lastTask && !lastTask.status) lastTask = { ...lastTask, status: topLevelStatus }
  }

  // `status` is a task status on legacy scheduler payloads, not the
  // session's run status. It has already been projected onto the task above;
  // do not relabel it as `runStatus` in the domain projection.
  const runStatus = textValue(value.run_status) || textValue(value.runStatus)
  return {
    key,
    reason,
    ...(runStatus ? { runStatus } : {}),
    ...(changedTask ? { changedTask } : {}),
    ...(lastTask ? { lastTask } : {}),
  }
}

function isExpectedUnavailable(error: unknown): boolean {
  const code = errorCode(error)
  return code === 'METHOD_NOT_FOUND'
    || code === 'UNSUPPORTED'
    || code === 'UNAUTHORIZED'
    || code === 'FORBIDDEN'
}

/**
 * Own the session-directory subscription as a logical lease. There is one raw
 * event listener and at most one server subscribe per physical connection
 * generation, regardless of how many UI consumers subscribe locally.
 */
export function createV4SessionDirectoryChanges(
  rpc: SessionDirectoryChangesRpcTransport,
  events: SessionDirectoryChangesEventTransport,
  options: SessionDirectoryChangesAdapterOptions = {},
): SessionDirectoryChanges {
  const listeners = new Set<(change: SessionDirectoryChange) => void>()
  const warn = options.warn || ((message: string, error?: unknown) => {
    console.warn(`[SessionDirectoryChanges] ${message}`, error)
  })

  let disposed = false
  let resumeRequested = false
  let boundGeneration: number | null = null
  let unavailableGeneration: number | null = null
  let bindWork: Promise<void> | null = null
  let releaseWork: Promise<void> | null = null
  let releaseRequested = false

  const eventSubscription = events.subscribe(
    SESSIONS_CHANGED_EVENT,
    payload => {
      const change = decodeSessionDirectoryChange(payload)
      if (!change) {
        warn('Dropped malformed sessions.changed event')
        return
      }
      for (const listener of [...listeners]) {
        try {
          listener(change)
        } catch (error) {
          // One view must not prevent the rest of the application from
          // observing an invalidation.
          warn('Session directory listener failed', error)
        }
      }
    },
  )
  const stateSubscription = events.subscribe(
    PRIVATE_STATE_EVENT,
    state => {
      if (state === 'connected') {
        // Rebind automatically when a live consumer still owns the logical
        // lease. The composition root may also call resume() here to trigger
        // its snapshot refresh; bindLease() coalesces both requests.
        if (resumeRequested && listeners.size > 0) void bindLease()
        return
      }
      // A server-side subscription belongs to the old physical connection;
      // never issue unsubscribe against a replacement generation.
      boundGeneration = null
      unavailableGeneration = null
    },
  )

  async function releaseLease(): Promise<void> {
    if (releaseWork) return releaseWork
    const generation = boundGeneration
    if (generation === null) {
      releaseRequested = false
      return
    }
    // Mark it free before awaiting so a new listener can schedule a fresh
    // lease without treating this release as an active binding.
    boundGeneration = null
    releaseRequested = false
    if (rpc.generation !== generation) return

    let work!: Promise<void>
    work = (async () => {
      try {
        await rpc.request(
          SESSIONS_UNSUBSCRIBE_METHOD,
          {},
          { ...SESSION_DIRECTORY_CHANGE_CALL_OPTIONS, expectedGeneration: generation },
        )
      } catch (error) {
        // Disconnect cleanup is authoritative. A missing/forbidden legacy
        // method is a compatibility fallback, not a reason to recycle the
        // shared transport.
        if (!isExpectedUnavailable(error)) warn('Session directory unsubscribe failed', error)
      } finally {
        if (releaseWork === work) releaseWork = null
      }
    })()
    releaseWork = work
    return work
  }

  async function bindLease(): Promise<void> {
    if (disposed || !resumeRequested || listeners.size === 0) return
    if (boundGeneration !== null && boundGeneration === rpc.generation) return
    if (bindWork) return bindWork

    let work!: Promise<void>
    work = (async () => {
      try {
        // Keep the server-side lease operations linearisable. A new local
        // listener may arrive while the previous generation's unsubscribe is
        // still in flight; wait for that cleanup before issuing subscribe so
        // an old unsubscribe cannot remove the new lease.
        if (releaseWork) await releaseWork
        if (disposed || !resumeRequested || listeners.size === 0) return
        // `ready` uses reject actions deliberately: this logical lease does
        // not own the physical socket, even while a replacement is handshaking.
        await rpc.ready?.({
          timeoutMs: SESSION_DIRECTORY_CHANGE_TIMEOUT_MS,
          timeoutAction: 'reject',
          abortAction: 'reject',
        })
        if (disposed || !resumeRequested || listeners.size === 0) return
        const generation = rpc.generation
        if (boundGeneration === generation) return
        if (unavailableGeneration === generation) return

        await rpc.request(
          SESSIONS_SUBSCRIBE_METHOD,
          {},
          { ...SESSION_DIRECTORY_CHANGE_CALL_OPTIONS, expectedGeneration: generation },
        )
        // A request can resolve after a disconnect only if it belonged to an
        // obsolete generation. Do not claim that new socket as subscribed.
        if (rpc.generation !== generation) return
        boundGeneration = generation
        if (!resumeRequested || listeners.size === 0) {
          releaseRequested = true
        }
      } catch (error) {
        const generation = rpc.generation
        if (isExpectedUnavailable(error)) {
          unavailableGeneration = generation
          if (errorCode(error) === 'METHOD_NOT_FOUND' || errorCode(error) === 'UNSUPPORTED') {
            rpc.markUnsupported?.(SESSIONS_SUBSCRIBE_METHOD)
          }
          return
        }
        if (!disposed && resumeRequested) warn('Session directory subscription failed', error)
      } finally {
        if (bindWork === work) bindWork = null
        if (releaseRequested && !bindWork) void releaseLease()
      }
    })()
    bindWork = work
    return work
  }

  function subscribe(
    listener: (change: SessionDirectoryChange) => void,
  ): SessionDirectoryChangeSubscription {
    if (disposed) return { close() {} }
    listeners.add(listener)
    if (resumeRequested) void bindLease()
    let closed = false
    return {
      close() {
        if (closed) return
        closed = true
        listeners.delete(listener)
        if (listeners.size === 0) {
          resumeRequested = false
          releaseRequested = true
          if (!bindWork) void releaseLease()
        }
      },
    }
  }

  async function resume(): Promise<void> {
    if (disposed) return
    resumeRequested = true
    await bindLease()
  }

  function dispose(): void {
    if (disposed) return
    disposed = true
    resumeRequested = false
    releaseRequested = true
    eventSubscription.close()
    stateSubscription.close()
    listeners.clear()
    if (!bindWork) void releaseLease()
  }

  return { subscribe, resume, dispose }
}
