import type { RpcCallOptions } from '@/lib/rpc'
import {
  type PendingInputCancelRequest,
  type PendingInputEnqueueRequest,
  type PendingInputEnqueueResult,
  type PendingInputQueuePort,
  type PendingInputReorderRequest,
  type PendingInputReorderResult,
  type PendingInputServerItem,
} from '@/modules/pendingInputQueue'
import { SESSIONS_PENDING_INPUTS_ENQUEUE_METHOD, type Result as SessionsPendingInputsEnqueueResult } from '@/contracts/generated/v4/sessionsPendingInputsEnqueue'
import { SESSIONS_PENDING_INPUTS_LIST_METHOD, type Result as SessionsPendingInputsListResult } from '@/contracts/generated/v4/sessionsPendingInputsList'
import { SESSIONS_PENDING_INPUTS_CANCEL_METHOD } from '@/contracts/generated/v4/sessionsPendingInputsCancel'
import { SESSIONS_PENDING_INPUTS_REORDER_METHOD, type Result as SessionsPendingInputsReorderResult } from '@/contracts/generated/v4/sessionsPendingInputsReorder'
import { validateResult as validateEnqueueResult } from '@/contracts/generated/v4/sessionsPendingInputsEnqueueValidators.mjs'
import { validateResult as validateListResult } from '@/contracts/generated/v4/sessionsPendingInputsListValidators.mjs'
import { validateResult as validateReorderResult } from '@/contracts/generated/v4/sessionsPendingInputsReorderValidators.mjs'
import { validateResult as validateCancelResult } from '@/contracts/generated/v4/sessionsPendingInputsCancelValidators.mjs'

const METHODS = {
  enqueue: SESSIONS_PENDING_INPUTS_ENQUEUE_METHOD,
  list: SESSIONS_PENDING_INPUTS_LIST_METHOD,
  cancel: SESSIONS_PENDING_INPUTS_CANCEL_METHOD,
  reorder: SESSIONS_PENDING_INPUTS_REORDER_METHOD,
} as const

interface PendingInputRequestSource {
  request<T = unknown>(method: string, params?: Record<string, unknown>): Promise<T>
  supports?(method: string): boolean
}

interface PendingInputQueueMethods {
  enqueue: string
  list: string
  cancel: string
  reorder: string
}

function createPendingInputQueuePort(
  source: PendingInputRequestSource,
  methods: PendingInputQueueMethods,
): PendingInputQueuePort {
  const supports = (method: string) => source.supports?.(method) === true
  return {
    supportsQueue: () => supports(methods.enqueue),
    supportsReorder: () => supports(methods.reorder),
    enqueue: request => source.request<PendingInputEnqueueResult>(methods.enqueue, {
      ...request,
      attachments: [...request.attachments],
    }),
    list: async sessionKey => {
      const response = await source.request<{ items?: PendingInputServerItem[] }>(
        methods.list,
        { key: sessionKey },
      )
      return Array.isArray(response.items) ? response.items : []
    },
    cancel: async request => {
      await source.request(methods.cancel, { ...request })
    },
    reorder: request => source.request<PendingInputReorderResult>(methods.reorder, {
      key: request.key,
      items: request.items.map(item => ({ ...item })),
    }),
  }
}

interface PendingInputV4Transport {
  request<T = unknown>(
    method: string,
    params?: Record<string, unknown>,
    options?: RpcCallOptions,
  ): Promise<T>
  supports?(method: string): boolean
  ready?(options?: { timeoutMs?: number; signal?: AbortSignal }): Promise<void>
}

/** Typed v4 Gateway Adapter for the pending-input domain port. */
export function createV4PendingInputQueue(
  transport: PendingInputV4Transport,
): PendingInputQueuePort {
  const port = createPendingInputQueuePort(transport, METHODS)
  if (transport.ready) {
    port.waitForConnection = options => transport.ready!(options)
  }
  return port
}

/**
 * Compatibility adapter for composables/tests that still expose the legacy
 * `call`/`supportsMethod` store shape. New composition roots should pass the
 * v4 adapter created from a private RpcTransport instead.
 */
export function createLegacyPendingInputQueue(source: PendingInputRequestSource): PendingInputQueuePort {
  const port = createPendingInputQueuePort(source, METHODS)
  const rawEnqueue = port.enqueue
  port.enqueue = async request => {
    const result = await rawEnqueue(request) as SessionsPendingInputsEnqueueResult
    if (!validateEnqueueResult(result) && typeof result.request_fingerprint !== 'string') {
      throw new Error('Invalid pending enqueue response')
    }
    return result
  }
  const rawList = port.list
  port.list = async key => {
    const items = await rawList(key)
    const result = { items } as SessionsPendingInputsListResult
    const legacyCompatible = items.every(item => (
      typeof item.pendingInputId === 'string' || typeof item.pending_input_id === 'string'
    ) && (
      typeof item.clientRequestId === 'string' || typeof item.client_request_id === 'string'
    ) && (
      typeof item.clientMessageId === 'string' || typeof item.client_message_id === 'string'
    ))
    if (!validateListResult(result) && !legacyCompatible) throw new Error('Invalid pending list response')
    return result.items
  }
  const rawCancel = port.cancel
  port.cancel = async request => {
    await rawCancel(request)
    // The cancel command has historically returned an open object (or an
    // empty payload); retain that permissive compatibility surface.
    validateCancelResult({})
  }
  const rawReorder = port.reorder
  port.reorder = async request => {
    const result = await rawReorder(request) as SessionsPendingInputsReorderResult
    if (!validateReorderResult(result)) throw new Error('Invalid pending reorder response')
    return result
  }
  return port
}

export type {
  PendingInputCancelRequest,
  PendingInputEnqueueRequest,
  PendingInputEnqueueResult,
  PendingInputReorderRequest,
  PendingInputReorderResult,
}
