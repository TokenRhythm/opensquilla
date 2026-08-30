import type { RpcCallOptions } from '@/lib/rpc'
import {
  type PendingInputCancelRequest,
  type PendingInputEnqueueRequest,
  type PendingInputEnqueueResult,
  type PendingInputQueuePort,
  type PendingInputReorderRequest,
  type PendingInputReorderResult,
  type PendingInputServerAttachment,
  type PendingInputServerItem,
} from '@/modules/pendingInputQueue'
import { SESSIONS_PENDING_INPUTS_ENQUEUE_METHOD } from '@/contracts/generated/v4/sessionsPendingInputsEnqueue'
import { SESSIONS_PENDING_INPUTS_LIST_METHOD } from '@/contracts/generated/v4/sessionsPendingInputsList'
import { SESSIONS_PENDING_INPUTS_CANCEL_METHOD } from '@/contracts/generated/v4/sessionsPendingInputsCancel'
import { SESSIONS_PENDING_INPUTS_REORDER_METHOD } from '@/contracts/generated/v4/sessionsPendingInputsReorder'
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

type WireRecord = Record<string, unknown>

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

interface RawPendingInputQueuePort {
  readonly supportsQueue: () => boolean
  readonly supportsReorder: () => boolean
  enqueue: (request: PendingInputEnqueueRequest) => Promise<unknown>
  list: (sessionKey: string) => Promise<unknown>
  cancel: (request: PendingInputCancelRequest) => Promise<unknown>
  reorder: (request: PendingInputReorderRequest) => Promise<unknown>
  waitForConnection?: PendingInputQueuePort['waitForConnection']
}

function isRecord(value: unknown): value is WireRecord {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value)
}

function firstValue(record: WireRecord, ...keys: string[]): unknown {
  for (const key of keys) {
    if (record[key] !== undefined) return record[key]
  }
  return undefined
}

function stringValue(value: unknown): string | undefined {
  return typeof value === 'string' && value.length > 0 ? value : undefined
}

function numberValue(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined
}

function stringListValue(value: unknown): string[] | undefined {
  if (!Array.isArray(value)) return undefined
  const values = value
    .map(entry => String(entry ?? '').trim())
    .filter(Boolean)
  return values.length ? [...new Set(values)] : []
}

function projectServerAttachment(value: unknown): PendingInputServerAttachment | null {
  if (!isRecord(value)) return null
  const name = stringValue(value.name) || 'attachment'
  const mime = stringValue(firstValue(value, 'mime', 'type')) || 'application/octet-stream'
  const size = numberValue(value.size)
  return {
    name,
    mime,
    ...(size !== undefined ? { size } : {}),
  }
}

/** Project current and legacy wire aliases into the domain-facing row. */
function projectPendingInputItem(value: unknown): PendingInputServerItem | null {
  if (!isRecord(value)) return null
  const pendingInputId = stringValue(firstValue(value, 'pendingInputId', 'pending_input_id'))
  const clientRequestId = stringValue(firstValue(value, 'clientRequestId', 'client_request_id'))
  const clientMessageId = stringValue(firstValue(value, 'clientMessageId', 'client_message_id'))
  if (!pendingInputId || !clientRequestId || !clientMessageId) return null

  const attachments = Array.isArray(value.attachments)
    ? value.attachments.flatMap(attachment => {
        const projected = projectServerAttachment(attachment)
        return projected ? [projected] : []
      })
    : undefined
  const promptAnnotationIds = stringListValue(
    firstValue(value, 'promptAnnotationIds', 'prompt_annotation_ids'),
  )
  const message = typeof value.message === 'string' ? value.message : undefined
  const displayValue = firstValue(value, 'displayText', 'display_text')
  const displayText = typeof displayValue === 'string' ? displayValue : undefined
  const intentValue = firstValue(value, 'intent')
  const intent = intentValue === null || typeof intentValue === 'string'
    ? intentValue
    : undefined
  const requestFingerprint = stringValue(
    firstValue(value, 'requestFingerprint', 'request_fingerprint'),
  )
  const revision = numberValue(value.revision)
  const position = numberValue(value.position)

  return {
    pendingInputId,
    clientRequestId,
    clientMessageId,
    ...(message !== undefined ? { message } : {}),
    ...(displayText !== undefined ? { displayText } : {}),
    ...(attachments !== undefined ? { attachments } : {}),
    ...(position !== undefined ? { position } : {}),
    ...(revision !== undefined ? { revision } : {}),
    ...(requestFingerprint !== undefined ? { requestFingerprint } : {}),
    ...(promptAnnotationIds !== undefined ? { promptAnnotationIds } : {}),
    ...(intent !== undefined ? { intent } : {}),
    ...(value.confirmedPlainText === true ? { confirmedPlainText: true } : {}),
  }
}

function projectServerItems(value: unknown): PendingInputServerItem[] | null {
  if (!Array.isArray(value)) return null
  const projected = value.flatMap(item => {
    const row = projectPendingInputItem(item)
    return row ? [row] : []
  })
  // A row without the identity fields cannot be reconciled safely by the
  // domain queue. Never silently drop it and report a partial success.
  return projected.length === value.length ? projected : null
}

function projectEnqueueResult(value: unknown): PendingInputEnqueueResult {
  if (!isRecord(value)) return {}
  const requestFingerprint = stringValue(
    firstValue(value, 'requestFingerprint', 'request_fingerprint'),
  )
  const revision = numberValue(value.revision)
  const position = numberValue(value.position)
  return {
    ...(requestFingerprint !== undefined ? { requestFingerprint } : {}),
    ...(revision !== undefined ? { revision } : {}),
    ...(position !== undefined ? { position } : {}),
  }
}

function invalidResponse(operation: string): Error {
  return new Error(`Invalid pending ${operation} response`)
}

function createRawPendingInputQueuePort(
  source: PendingInputRequestSource,
  methods: PendingInputQueueMethods,
): RawPendingInputQueuePort {
  const supports = (method: string) => source.supports?.(method) === true
  return {
    supportsQueue: () => supports(methods.enqueue),
    supportsReorder: () => supports(methods.reorder),
    enqueue: request => source.request(methods.enqueue, {
      ...request,
      attachments: [...request.attachments],
    }),
    list: sessionKey => source.request(methods.list, { key: sessionKey }),
    cancel: request => source.request(methods.cancel, { ...request }),
    reorder: request => source.request(methods.reorder, {
      key: request.key,
      items: request.items.map(item => ({ ...item })),
    }),
  }
}

/**
 * Validate every v4 response before projecting it into the domain port. The
 * same wrapper is used by the legacy test fixture, so compatibility cannot
 * bypass the production Contract boundary.
 */
function withPendingInputValidation(
  raw: RawPendingInputQueuePort,
): PendingInputQueuePort {
  const port: PendingInputQueuePort = {
    supportsQueue: raw.supportsQueue,
    supportsReorder: raw.supportsReorder,
    enqueue: async request => {
      const result = projectEnqueueResult(await raw.enqueue(request))
      if (!validateEnqueueResult(result)) throw invalidResponse('enqueue')
      return result
    },
    list: async sessionKey => {
      const result = await raw.list(sessionKey)
      if (!isRecord(result) || !Array.isArray(result.items)
        || result.items.some(item => !isRecord(item))) {
        throw invalidResponse('list')
      }
      const projected = projectServerItems(result.items)
      // Validate the projected canonical form so legacy snake-case rows remain
      // compatible while malformed structural payloads still fail closed.
      if (!projected || !validateListResult({ items: projected })) {
        throw invalidResponse('list')
      }
      return projected
    },
    cancel: async request => {
      const result = await raw.cancel(request)
      // Older Gateways intentionally return an empty payload for cancellation.
      const candidate = result === undefined ? {} : result
      if (!validateCancelResult(candidate)) throw invalidResponse('cancel')
    },
    reorder: async request => {
      const result = await raw.reorder(request)
      if (!validateReorderResult(result)) throw invalidResponse('reorder')
      const projectedItems = projectServerItems(isRecord(result) ? result.items : undefined)
      if (!projectedItems || !validateReorderResult({ items: projectedItems })) {
        throw invalidResponse('reorder')
      }
      return {
        items: projectedItems,
      }
    },
  }
  if (raw.waitForConnection) port.waitForConnection = raw.waitForConnection
  return port
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
  const raw = createRawPendingInputQueuePort(transport, METHODS)
  if (transport.ready) raw.waitForConnection = options => transport.ready!(options)
  return withPendingInputValidation(raw)
}

/**
 * Compatibility adapter for isolated tests that still expose a request /
 * capability source. It deliberately shares the exact production validator
 * and projection wrapper above.
 */
export function createLegacyPendingInputQueue(source: PendingInputRequestSource): PendingInputQueuePort {
  return withPendingInputValidation(createRawPendingInputQueuePort(source, METHODS))
}

export type {
  PendingInputCancelRequest,
  PendingInputEnqueueRequest,
  PendingInputEnqueueResult,
  PendingInputReorderRequest,
  PendingInputReorderResult,
}
