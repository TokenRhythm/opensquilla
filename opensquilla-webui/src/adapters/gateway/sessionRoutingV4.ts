import type {
  TransportCallOptions as RpcCallOptions,
  TransportEventHandler as RpcEventHandler,
} from './transportTypes'
import {
  SESSIONS_ROUTING_GET_METHOD,
  type Result as GetResult,
} from '@/contracts/generated/v4/sessionsRoutingGet'
import { validateResult as validateGetResult } from '@/contracts/generated/v4/sessionsRoutingGetValidators.mjs'
import {
  SESSIONS_ROUTING_SET_METHOD,
  type Result as SetResult,
} from '@/contracts/generated/v4/sessionsRoutingSet'
import { validateResult as validateSetResult } from '@/contracts/generated/v4/sessionsRoutingSetValidators.mjs'
import {
  SESSIONS_ROUTING_CHANGED_EVENT,
  type Payload as ChangedPayload,
} from '@/contracts/generated/v4/sessionsRoutingChanged'
import { validatePayload as validateChangedPayload } from '@/contracts/generated/v4/sessionsRoutingChangedValidators.mjs'
import {
  SessionRoutingError,
  type SessionRouting,
  type SessionRoutingSetInput,
  type SessionRoutingSnapshot,
} from '@/modules/sessionRouting'

interface SessionRoutingRpcTransport {
  request<T = unknown>(
    method: string,
    params?: Record<string, unknown>,
    options?: RpcCallOptions,
  ): Promise<T>
  supports?(method: string): boolean
}

interface SessionRoutingEventTransport {
  subscribe(event: string, handler: RpcEventHandler): { close(): void }
}

function objectValue(...values: unknown[]): Record<string, unknown> | null {
  const value = values.find(
    candidate => candidate && typeof candidate === 'object' && !Array.isArray(candidate),
  )
  return value ? value as Record<string, unknown> : null
}

function text(...values: unknown[]): string {
  for (const value of values) {
    if (typeof value === 'string' && value.trim()) return value.trim()
  }
  return ''
}

function number(...values: unknown[]): number | null {
  for (const value of values) {
    if (typeof value === 'boolean' || value === null || value === undefined || value === '') continue
    const n = Number(value)
    if (Number.isInteger(n) && n >= 0) return n
  }
  return null
}

function snapshot(value: unknown): SessionRoutingSnapshot | null {
  const source = objectValue(value)
  if (!source) return null
  const nested = objectValue(
    source.routing,
    source.sessionRouting,
    source.modelRouting,
    source.model_routing,
  )
  const route = nested || source
  const key = text(source.key, source.sessionKey, source.session_key)
  const rawMode = text(route.mode, route.routingMode, route.routing_mode)
  const mode = ({
    off: 'direct',
    squilla_router: 'router',
    llm_ensemble: 'ensemble',
    direct: 'direct',
    router: 'router',
    ensemble: 'ensemble',
  } as Record<string, SessionRoutingSnapshot['mode']>)[rawMode]
  const revision = number(route.revision, route.routingRevision, route.routing_revision)
  if (!key || !mode || revision === null) return null
  return {
    key,
    mode,
    revision,
    source: text(route.source) || 'session',
    initialized: route.initialized === true,
    appliesTo: text(route.appliesTo, route.applies_to) || 'next_accepted_turn',
  }
}

function normalizeChangedPayload(value: unknown): unknown {
  const source = objectValue(value)
  if (!source) return value
  const key = text(source.key, source.sessionKey, source.session_key)
  return key ? { ...source, key } : source
}

function rpcCode(error: unknown): string {
  const value = objectValue(error)
  const data = objectValue(value?.data)
  return text(value?.code, data?.code).toUpperCase()
}

function mapError(error: unknown): SessionRoutingError {
  if (error instanceof SessionRoutingError) return error
  const value = objectValue(error)
  const data = objectValue(value?.data)
  const code = rpcCode(error)
  const message = text(value?.message) || (error instanceof Error ? error.message : 'Session routing request failed')
  const details = value?.details ?? data?.details
  if (code === 'SESSION_NOT_FOUND' || code === 'NOT_FOUND') return new SessionRoutingError('not-found', message, { details, cause: error })
  if (code === 'METHOD_NOT_FOUND' || code === 'UNSUPPORTED') return new SessionRoutingError('unsupported', message, { details, cause: error })
  if (code === 'UNAUTHORIZED' || code === 'FORBIDDEN') return new SessionRoutingError('forbidden', message, { details, cause: error })
  if (code === 'SESSION_ROUTING_CHANGED' || code === 'CONFLICT') return new SessionRoutingError('conflict', message, { details, retryable: true, cause: error })
  if (code === 'INVALID_REQUEST' || code === 'INVALID_PARAMS') return new SessionRoutingError('invalid', message, { details, cause: error })
  return new SessionRoutingError('unavailable', message, { details, cause: error })
}

function isAbort(error: unknown, signal?: AbortSignal): boolean {
  return signal?.aborted === true
    || (error instanceof Error && (
      error.name === 'AbortError' || rpcCode(error) === 'RPC_ABORTED'
    ))
}

function requestOptionsFor(signal: AbortSignal | undefined): RpcCallOptions {
  return {
    timeoutMs: 10_000,
    timeoutAction: 'reject',
    abortAction: 'reject',
    ...(signal ? { signal } : {}),
  }
}

export function createV4SessionRouting(
  rpc: SessionRoutingRpcTransport,
  events: SessionRoutingEventTransport,
): SessionRouting {
  const listeners = new Set<(value: SessionRoutingSnapshot) => void>()
  let disposed = false
  const get = async (sessionKey: string, requestOptions?: { signal?: AbortSignal }) => {
    try {
      const value = await rpc.request<GetResult>(
        SESSIONS_ROUTING_GET_METHOD,
        { sessionKey },
        requestOptionsFor(requestOptions?.signal),
      )
      if (!validateGetResult(value)) {
        throw new Error('sessions.routing.get returned an invalid snapshot')
      }
      const result = snapshot(value)
      if (!result) throw new Error('sessions.routing.get returned an invalid snapshot')
      return result
    } catch (error) {
      if (isAbort(error, requestOptions?.signal)) throw error
      throw mapError(error)
    }
  }
  const set = async (
    input: SessionRoutingSetInput,
    requestOptions?: { signal?: AbortSignal },
  ) => {
    try {
      const value = await rpc.request<SetResult>(SESSIONS_ROUTING_SET_METHOD, {
        sessionKey: input.sessionKey,
        mode: input.mode,
        expectedRevision: input.expectedRevision,
      }, requestOptionsFor(requestOptions?.signal))
      if (!validateSetResult(value)) {
        throw new Error('sessions.routing.set returned an invalid snapshot')
      }
      const result = snapshot(value)
      if (!result) throw new Error('sessions.routing.set returned an invalid snapshot')
      return result
    } catch (error) {
      if (isAbort(error, requestOptions?.signal)) throw error
      throw mapError(error)
    }
  }
  const eventSubscription = events.subscribe(SESSIONS_ROUTING_CHANGED_EVENT, payload => {
    if (disposed) return
    const normalized = normalizeChangedPayload(payload)
    if (!validateChangedPayload(normalized)) return
    const result = snapshot(normalized as ChangedPayload)
    if (!result) return
    for (const listener of listeners) {
      try {
        listener(result)
      } catch (error) {
        console.error('[SessionRouting] listener failed', error)
      }
    }
  })
  return {
    available: () => (rpc.supports?.(SESSIONS_ROUTING_GET_METHOD) !== false)
      && (rpc.supports?.(SESSIONS_ROUTING_SET_METHOD) !== false),
    get,
    set,
    subscribe(listener) {
      if (disposed) return { close: () => undefined }
      listeners.add(listener)
      return { close: () => listeners.delete(listener) }
    },
    dispose() {
      if (disposed) return
      disposed = true
      listeners.clear()
      eventSubscription.close()
    },
  }
}

export { rpcCode }
