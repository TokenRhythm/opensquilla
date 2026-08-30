/**
 * ApprovalCenter v4 wire Contract adapter.
 *
 * Generated wire types stay behind this adapter.  It owns legacy aliases,
 * validation, and redaction while exposing only transport-independent
 * projections to the ApprovalCenter domain module.
 */

import {
  APPROVAL_EVENTS_EVENT_METADATA,
} from '@/contracts/generated/v4/approvalEvents'
import { validateApprovalEventPayload } from '@/contracts/generated/v4/approvalEventsValidators.mjs'
import {
  EXEC_APPROVAL_EXTEND_METHOD,
} from '@/contracts/generated/v4/approvalExtend'
import {
  validateApprovalExtendParams,
  validateApprovalExtendResult,
} from '@/contracts/generated/v4/approvalExtendValidators.mjs'
import {
  EXEC_APPROVAL_RESOLVE_METHOD,
} from '@/contracts/generated/v4/approvalResolve'
import {
  validateApprovalResolveParams,
  validateApprovalResolveResult,
} from '@/contracts/generated/v4/approvalResolveValidators.mjs'
import {
  EXEC_APPROVAL_SNAPSHOT_METHOD,
} from '@/contracts/generated/v4/approvalSnapshot'
import { validateExecApprovalSnapshotResult } from '@/contracts/generated/v4/approvalSnapshotValidators.mjs'
import {
  EXEC_APPROVAL_STATUS_METHOD,
} from '@/contracts/generated/v4/approvalStatus'
import {
  validateApprovalStatusParams,
  validateApprovalStatusResult,
} from '@/contracts/generated/v4/approvalStatusValidators.mjs'

export type ApprovalNamespace = 'exec' | 'plugin'
export type ApprovalMode = 'prompt' | 'auto-approve' | 'auto-deny'

export interface ApprovalRequestOptions {
  signal?: AbortSignal
  timeoutMs?: number
}

export interface ApprovalCenterContractTransport {
  request(
    method: string,
    params?: Record<string, unknown>,
    options?: ApprovalRequestOptions,
  ): Promise<unknown>
}

export interface ApprovalCenterContractEventTransport {
  subscribe(
    event: string,
    handler: (payload: unknown, meta?: unknown, sequence?: unknown) => void,
  ): { close(): void }
}

export class ApprovalCenterContractError extends Error {
  readonly operation?: string
  readonly validationErrors: readonly unknown[]

  constructor(operation: string, message: string, validationErrors: readonly unknown[] = []) {
    super(`${operation}: ${message}`)
    this.name = 'ApprovalCenterContractError'
    this.operation = operation
    this.validationErrors = validationErrors
  }
}

export interface ApprovalStatusProjection {
  readonly id: string
  readonly namespace: ApprovalNamespace
  readonly found?: boolean
  readonly pending: boolean
  readonly resolutionInProgress: boolean
  readonly resolved: boolean
  readonly approved: boolean
  readonly resolution: string
  readonly consumed: boolean
  readonly deadline: number | null
  readonly metadata?: Readonly<Record<string, unknown>>
}

export interface ApprovalSnapshotProjection {
  readonly mode: ApprovalMode
  readonly metadata?: Readonly<Record<string, unknown>>
}

export interface ApprovalEventProjection {
  readonly event: string
  readonly approvalId: string
  readonly namespace: ApprovalNamespace
  readonly sessionKey: string | null
  readonly toolName: string | null
  readonly command: string | null
  readonly approvalKind: string | null
  readonly agent: string | null
  readonly args: Readonly<Record<string, unknown>> | null
  readonly warning: string | null
  readonly displayKind: string | null
  readonly displayTarget: string | null
  readonly destructive: boolean | null
  readonly irreversible: boolean | null
  readonly backupState: string | null
  readonly createdAt: number | null
  readonly emittedAt: number | null
  readonly deadline: number | null
  readonly approved: boolean | null
  readonly resolution: string | null
  readonly streamSeq: number | null
  readonly schemaVersion: 1 | null
  readonly legacy: boolean
  /** Presence markers preserve the old lean-push hydration fallback. */
  readonly hasArgs: boolean
  readonly hasWarning: boolean
  readonly metadata?: Readonly<Record<string, unknown>>
}

export interface ApprovalHttpItem {
  readonly id: string
  readonly namespace: ApprovalNamespace
  readonly createdAt: number | null
  readonly mode: ApprovalMode
  readonly toolName: string
  readonly sessionKey: string
  readonly agent: string
  readonly args: Readonly<Record<string, unknown>> | null
  readonly command: string
  readonly warning: string
  readonly approvalKind: string
  readonly actionKind: string
  readonly displayKind: string
  readonly displayTarget: string
  readonly destructive: boolean
  readonly irreversible: boolean
  readonly backupState: string
  readonly deadline: number | null
}

export interface ApprovalHttpSnapshot {
  readonly pending: readonly ApprovalHttpItem[]
  readonly mode: ApprovalMode
  readonly allowPatterns: readonly string[]
  readonly denyPatterns: readonly string[]
}

type JsonObject = Record<string, unknown>
export interface ApprovalResolveInput {
  id: string
  approved: boolean
  choice?: string | null
  decision?: string | null
  allowAlways?: boolean | null
  rememberIntent?: boolean | null
  [key: string]: unknown
}

type ContractValidator = ((value: unknown) => boolean) & {
  errors?: readonly unknown[] | null
}

const APPROVAL_EVENT_WIRE_NAMES: readonly string[] = Object.freeze(
  APPROVAL_EVENTS_EVENT_METADATA.wireNames.filter(value => typeof value === 'string'),
)

const APPROVAL_METHOD_BY_NAMESPACE: Record<ApprovalNamespace, {
  status: string
  resolve: string
  extend: string
}> = {
  exec: {
    status: EXEC_APPROVAL_STATUS_METHOD,
    resolve: EXEC_APPROVAL_RESOLVE_METHOD,
    extend: EXEC_APPROVAL_EXTEND_METHOD,
  },
  plugin: {
    status: 'plugin.approval.status',
    resolve: 'plugin.approval.resolve',
    extend: 'plugin.approval.extend',
  },
}

const STATUS_KEYS = [
  'found', 'id', 'namespace', 'pending', 'resolutionInProgress', 'resolved',
  'approved', 'resolution', 'consumed', 'deadline',
]
const RESULT_KEYS = [
  'id', 'mode', 'approved', 'resolved', 'resolution', 'deadline', 'consumed',
  'pending', 'resolutionInProgress',
]
const EVENT_KEYS = [
  'schema_version', 'approval_id', 'approvalId', 'namespace', 'session_key',
  'sessionKey', 'tool_name', 'toolName', 'command', 'approval_kind',
  'approvalKind', 'agent', 'args', 'warning', 'display_kind', 'displayKind',
  'display_target', 'displayTarget', 'destructive', 'irreversible',
  'backup_state', 'backupState', 'created_at', 'createdAt', 'deadline',
  'approved', 'resolution', 'stream_seq', 'streamSeq', 'emitted_at', 'emittedAt',
]

function objectValue(value: unknown): JsonObject | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as JsonObject
    : null
}

function aliasValue(value: JsonObject, ...keys: string[]): unknown {
  const present = keys
    .filter(key => Object.prototype.hasOwnProperty.call(value, key)
      && value[key] !== undefined
      && value[key] !== null)
    .map(key => ({ key, value: value[key] }))
  if (present.length === 0) return undefined
  const first = present[0]?.value
  if (present.some(item => JSON.stringify(item.value) !== JSON.stringify(first))) {
    throw new ApprovalCenterContractError(
      APPROVAL_EVENTS_EVENT_METADATA.name,
      `conflicting aliases: ${present.map(item => item.key).join(', ')}`,
    )
  }
  return first
}

function textAlias(value: JsonObject, ...keys: string[]): string | null {
  const candidate = aliasValue(value, ...keys)
  if (candidate === undefined) return null
  if (typeof candidate !== 'string') {
    throw new ApprovalCenterContractError(
      APPROVAL_EVENTS_EVENT_METADATA.name,
      `${keys[0]} must be a string`,
    )
  }
  return candidate.trim()
}

function numberAlias(value: JsonObject, ...keys: string[]): number | null {
  const candidate = aliasValue(value, ...keys)
  if (candidate === undefined) return null
  if (typeof candidate !== 'number' || !Number.isFinite(candidate)) {
    throw new ApprovalCenterContractError(
      APPROVAL_EVENTS_EVENT_METADATA.name,
      `${keys[0]} must be a finite JSON number`,
    )
  }
  return candidate
}

function integerAlias(value: JsonObject, ...keys: string[]): number | null {
  const candidate = numberAlias(value, ...keys)
  if (candidate === null) return null
  if (!Number.isInteger(candidate) || candidate < 0) {
    throw new ApprovalCenterContractError(
      APPROVAL_EVENTS_EVENT_METADATA.name,
      `${keys[0]} must be a non-negative JSON integer`,
    )
  }
  return candidate
}

// `args` and additive event fields are intentionally open for forward
// compatibility.  Redact them at this boundary before a domain listener can
// observe them; otherwise an adapter consumer could accidentally render a
// token or an internal approval claim.
const PRIVATE_DISPLAY_KEY = /(authorization|cookie|credential|fingerprint|password|review.?action|secret|session.?(?:key|id)|token|claim)/i
const INTERNAL_DISPLAY_KEY = /^(action|actions|choice|choices|params|policy|reviewer)$/i

function redactDisplayValue(value: unknown, depth = 0): unknown {
  if (value == null || typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') return value
  if (depth >= 2) return undefined
  if (Array.isArray(value)) {
    return value.slice(0, 20)
      .map(item => redactDisplayValue(item, depth + 1))
      .filter(item => item !== undefined)
  }
  const object = objectValue(value)
  if (!object) return undefined
  const safe: JsonObject = {}
  for (const [key, item] of Object.entries(object)) {
    const isRedactedMarker = typeof item === 'string' && item === '[REDACTED]'
    if (INTERNAL_DISPLAY_KEY.test(key) || (PRIVATE_DISPLAY_KEY.test(key) && !isRedactedMarker)) continue
    const normalized = redactDisplayValue(item, depth + 1)
    if (normalized !== undefined) safe[key] = normalized
  }
  return safe
}

function redactedRecord(value: unknown): Readonly<Record<string, unknown>> | null {
  const normalized = redactDisplayValue(value)
  return normalized && typeof normalized === 'object' && !Array.isArray(normalized)
    ? normalized as Readonly<Record<string, unknown>>
    : null
}

/**
 * Project approval arguments for display.  Policy-heavy sandbox payloads are
 * intentionally allow-listed; all other kinds receive the same bounded,
 * recursively redacted projection used by event metadata.
 */
export function projectApprovalDisplayArgs(
  kind: string,
  source: Readonly<Record<string, unknown>> | null,
): Readonly<Record<string, unknown>> | null {
  if (!source) return null
  if (kind === 'sandbox_path') {
    const value: JsonObject = {}
    for (const key of ['path', 'access', 'workspace']) {
      if (['string', 'number', 'boolean'].includes(typeof source[key])) value[key] = source[key]
    }
    return Object.keys(value).length ? value : null
  }
  if (kind === 'sandbox_network') {
    const value: JsonObject = {}
    for (const key of ['host', 'bundle_id', 'workspace']) {
      if (['string', 'number', 'boolean'].includes(typeof source[key])) value[key] = source[key]
    }
    return Object.keys(value).length ? value : null
  }
  if (kind.startsWith('sandbox_')) return null
  return redactedRecord(source)
}

function metadataFor(value: JsonObject, known: readonly string[]): Readonly<Record<string, unknown>> | undefined {
  const knownKeys = new Set(known)
  const metadata: JsonObject = {}
  for (const [key, item] of Object.entries(value)) {
    if (knownKeys.has(key) || PRIVATE_DISPLAY_KEY.test(key) || INTERNAL_DISPLAY_KEY.test(key)) continue
    const normalized = redactDisplayValue(item)
    if (normalized !== undefined) metadata[key] = normalized
  }
  return Object.keys(metadata).length > 0 ? metadata : undefined
}

function assertValid(
  operation: string,
  value: unknown,
  validator: ContractValidator,
): JsonObject {
  if (!validator(value)) {
    throw new ApprovalCenterContractError(operation, 'value violated its generated v4 Contract', validator.errors ?? [])
  }
  const object = objectValue(value)
  if (!object) {
    throw new ApprovalCenterContractError(operation, 'value must be a JSON object')
  }
  return object
}

function mode(value: unknown, operation: string): ApprovalMode {
  if (value === 'prompt' || value === 'auto-approve' || value === 'auto-deny') return value
  throw new ApprovalCenterContractError(operation, 'mode is invalid')
}

function namespace(value: unknown, fallback: ApprovalNamespace, operation: string): ApprovalNamespace {
  if (value === undefined || value === null || value === '') return fallback
  if (value === 'exec' || value === 'plugin') return value
  throw new ApprovalCenterContractError(operation, 'namespace is invalid')
}

function projectStatus(
  value: unknown,
  operation: string,
  fallbackNamespace?: ApprovalNamespace,
): ApprovalStatusProjection {
  const object = assertValid(
    operation,
    value,
    (operation.endsWith('.status') ? validateApprovalStatusResult :
      operation.endsWith('.resolve') ? validateApprovalResolveResult :
        validateApprovalExtendResult) as ContractValidator,
  )
  const id = textAlias(object, 'id')
  if (!id) throw new ApprovalCenterContractError(operation, 'id is required')
  const resolvedNamespace = namespace(object.namespace, fallbackNamespace ?? 'exec', operation)
  return {
    id,
    namespace: resolvedNamespace,
    ...(typeof object.found === 'boolean' ? { found: object.found } : {}),
    pending: object.pending === true,
    resolutionInProgress: object.resolutionInProgress === true,
    resolved: object.resolved === true,
    approved: object.approved === true,
    resolution: typeof object.resolution === 'string' ? object.resolution : '',
    consumed: object.consumed === true,
    deadline: numberAlias(object, 'deadline'),
    ...(metadataFor(object, object.found === undefined ? RESULT_KEYS : STATUS_KEYS)
      ? { metadata: metadataFor(object, object.found === undefined ? RESULT_KEYS : STATUS_KEYS) } : {}),
  }
}

function projectSnapshot(value: unknown): ApprovalSnapshotProjection {
  const object = assertValid(
    EXEC_APPROVAL_SNAPSHOT_METHOD,
    value,
    validateExecApprovalSnapshotResult as ContractValidator,
  )
  const snapshotMode = mode(object.mode, EXEC_APPROVAL_SNAPSHOT_METHOD)
  const metadata = metadataFor(object, ['mode'])
  return metadata ? { mode: snapshotMode, metadata } : { mode: snapshotMode }
}

/** Decode one existing approval event into a safe domain projection. */
export function decodeApprovalEvent(
  event: string,
  payload: unknown,
  options: { allowLegacy?: boolean } = {},
): ApprovalEventProjection {
  if (!APPROVAL_EVENT_WIRE_NAMES.includes(event)) {
    throw new ApprovalCenterContractError(event, 'event name is not in the approval Contract')
  }
  const object = assertValid(
    event,
    payload,
    validateApprovalEventPayload as ContractValidator,
  )
  const approvalId = textAlias(object, 'approval_id', 'approvalId')
  if (!approvalId) {
    throw new ApprovalCenterContractError(event, 'approval_id is required')
  }
  const hasVersion = Object.prototype.hasOwnProperty.call(object, 'schema_version')
    && object.schema_version !== null
    && object.schema_version !== undefined
  if (!hasVersion && options.allowLegacy === false) {
    throw new ApprovalCenterContractError(event, 'legacy schema_version is not allowed')
  }
  if (hasVersion && object.schema_version !== 1) {
    throw new ApprovalCenterContractError(event, 'schema_version must be 1')
  }
  const fallbackNamespace: ApprovalNamespace = event.startsWith('plugin.') ? 'plugin' : 'exec'
  const eventNamespace = namespace(object.namespace, fallbackNamespace, event)
  if (eventNamespace !== fallbackNamespace) {
    throw new ApprovalCenterContractError(event, 'namespace does not match event name')
  }
  const args = object.args
  if (args !== null && args !== undefined && objectValue(args) === null) {
    throw new ApprovalCenterContractError(event, 'args must be an object or null')
  }
  const metadata = metadataFor(object, EVENT_KEYS)
  return {
    event,
    approvalId,
    namespace: eventNamespace,
    sessionKey: textAlias(object, 'session_key', 'sessionKey'),
    toolName: textAlias(object, 'tool_name', 'toolName'),
    command: textAlias(object, 'command'),
    approvalKind: textAlias(object, 'approval_kind', 'approvalKind'),
    agent: textAlias(object, 'agent'),
    args: redactedRecord(args),
    warning: textAlias(object, 'warning'),
    displayKind: textAlias(object, 'display_kind', 'displayKind'),
    displayTarget: textAlias(object, 'display_target', 'displayTarget'),
    destructive: typeof object.destructive === 'boolean' ? object.destructive : null,
    irreversible: typeof object.irreversible === 'boolean' ? object.irreversible : null,
    backupState: textAlias(object, 'backup_state', 'backupState'),
    createdAt: numberAlias(object, 'created_at', 'createdAt'),
    emittedAt: numberAlias(object, 'emitted_at', 'emittedAt'),
    deadline: numberAlias(object, 'deadline'),
    approved: typeof object.approved === 'boolean' ? object.approved : null,
    resolution: textAlias(object, 'resolution'),
    streamSeq: integerAlias(object, 'stream_seq', 'streamSeq'),
    schemaVersion: hasVersion ? 1 : null,
    legacy: !hasVersion,
    hasArgs: Object.prototype.hasOwnProperty.call(object, 'args'),
    hasWarning: Object.prototype.hasOwnProperty.call(object, 'warning'),
    ...(metadata ? { metadata } : {}),
  }
}

function closeNoop(): { close(): void } {
  return { close: () => undefined }
}

/**
 * Create an ApprovalCenter seam over the existing v4 transports.
 * Mutation methods validate only; they do not duplicate or shadow-execute the
 * Gateway implementation.
 */
export function createApprovalCenterV4Contract(
  transport: ApprovalCenterContractTransport,
  events: ApprovalCenterContractEventTransport,
  options: { onViolation?: (error: ApprovalCenterContractError) => void } = {},
) {
  let disposed = false
  const listeners = new Set<(event: ApprovalEventProjection) => void>()
  const upstream = APPROVAL_EVENT_WIRE_NAMES.map(event => events.subscribe(event, payload => {
    if (disposed) return
    try {
      const decoded = decodeApprovalEvent(event, payload)
      for (const listener of listeners) {
        try {
          listener(decoded)
        } catch {
          // Event consumers are best-effort and must not break the transport.
        }
      }
    } catch (error) {
      if (error instanceof ApprovalCenterContractError) options.onViolation?.(error)
    }
  }))

  const snapshot = async (requestOptions?: ApprovalRequestOptions) => {
    const value = await transport.request(
      EXEC_APPROVAL_SNAPSHOT_METHOD,
      undefined,
      requestOptions,
    )
    return projectSnapshot(value)
  }

  const status = async (
    approvalNamespace: ApprovalNamespace,
    id: string,
    requestOptions?: ApprovalRequestOptions,
  ) => {
    const params = { id }
    if (!validateApprovalStatusParams(params)) {
      throw new ApprovalCenterContractError('approval.status', 'params violated its generated Contract', validateApprovalStatusParams.errors ?? [])
    }
    const method = APPROVAL_METHOD_BY_NAMESPACE[approvalNamespace].status
    const value = await transport.request(method, params, requestOptions)
    return projectStatus(value, method, approvalNamespace)
  }

  const resolve = async (
    approvalNamespace: ApprovalNamespace,
    input: ApprovalResolveInput,
    requestOptions?: ApprovalRequestOptions,
  ) => {
    if (!validateApprovalResolveParams(input)) {
      throw new ApprovalCenterContractError('approval.resolve', 'params violated its generated Contract', validateApprovalResolveParams.errors ?? [])
    }
    const method = APPROVAL_METHOD_BY_NAMESPACE[approvalNamespace].resolve
    const value = await transport.request(method, input, requestOptions)
    return projectStatus(value, method, approvalNamespace)
  }

  const extend = async (
    approvalNamespace: ApprovalNamespace,
    id: string,
    seconds?: number | null,
    requestOptions?: ApprovalRequestOptions,
  ) => {
    const params: Record<string, unknown> = { id, ...(seconds === undefined ? {} : { seconds }) }
    if (!validateApprovalExtendParams(params)) {
      throw new ApprovalCenterContractError('approval.extend', 'params violated its generated Contract', validateApprovalExtendParams.errors ?? [])
    }
    const method = APPROVAL_METHOD_BY_NAMESPACE[approvalNamespace].extend
    const value = await transport.request(method, params, requestOptions)
    return projectStatus(value, method, approvalNamespace)
  }

  const subscribe = (listener: (event: ApprovalEventProjection) => void) => {
    if (disposed) return closeNoop()
    listeners.add(listener)
    return { close: () => listeners.delete(listener) }
  }

  return {
    snapshot,
    status,
    resolve,
    extend,
    subscribe,
    dispose() {
      if (disposed) return
      disposed = true
      listeners.clear()
      for (const subscription of upstream) subscription.close()
    },
  }
}

/** Validate and project the redacted companion ``GET /api/approvals`` shape. */
export function projectApprovalHttpSnapshot(value: unknown): ApprovalHttpSnapshot {
  const object = objectValue(value)
  if (!object) throw new ApprovalCenterContractError('/api/approvals', 'snapshot must be an object')
  const snapshotMode = mode(object.mode, '/api/approvals')
  if (!Array.isArray(object.pending)) {
    throw new ApprovalCenterContractError('/api/approvals', 'pending must be an array')
  }
  const projectItem = (raw: unknown): ApprovalHttpItem => {
    const item = objectValue(raw)
    if (!item || typeof item.id !== 'string' || !item.id) {
      throw new ApprovalCenterContractError('/api/approvals', 'pending item id is required')
    }
    if (Object.keys(item).some(key => /(?:params|token|secret|password|credential|authorization|fingerprint|review|claim)/i.test(key))) {
      throw new ApprovalCenterContractError('/api/approvals', 'pending item contains a private queue field')
    }
    const itemNamespace = namespace(item.namespace, 'exec', '/api/approvals')
    const args = item.args
    if (args !== null && args !== undefined && objectValue(args) === null) {
      throw new ApprovalCenterContractError('/api/approvals', 'pending item args must be an object or null')
    }
    const textFields = [
      'toolName', 'sessionKey', 'agent', 'command', 'warning', 'approvalKind',
      'actionKind', 'displayKind', 'displayTarget', 'backupState', 'mode',
    ] as const
    for (const key of textFields) {
      if (item[key] !== undefined && typeof item[key] !== 'string') {
        throw new ApprovalCenterContractError('/api/approvals', `pending item ${key} must be a string`)
      }
    }
    for (const key of ['created_at', 'createdAt', 'deadline'] as const) {
      if (item[key] !== undefined && item[key] !== null
        && (typeof item[key] !== 'number' || !Number.isFinite(item[key]))) {
        throw new ApprovalCenterContractError('/api/approvals', `pending item ${key} must be a finite number or null`)
      }
    }
    if (item.created_at !== undefined && item.created_at !== null
      && item.createdAt !== undefined && item.createdAt !== null
      && item.created_at !== item.createdAt) {
      throw new ApprovalCenterContractError(
        '/api/approvals',
        'pending item created_at and createdAt aliases conflict',
      )
    }
    for (const key of ['destructive', 'irreversible'] as const) {
      if (item[key] !== undefined && typeof item[key] !== 'boolean') {
        throw new ApprovalCenterContractError('/api/approvals', `pending item ${key} must be a boolean`)
      }
    }
    return {
      id: item.id,
      namespace: itemNamespace,
      createdAt: typeof item.created_at === 'number' && Number.isFinite(item.created_at)
        ? item.created_at
        : typeof item.createdAt === 'number' && Number.isFinite(item.createdAt)
          ? item.createdAt
          : null,
      mode: mode(item.mode ?? snapshotMode, '/api/approvals'),
      toolName: typeof item.toolName === 'string' ? item.toolName : '',
      sessionKey: typeof item.sessionKey === 'string' ? item.sessionKey : '',
      agent: typeof item.agent === 'string' ? item.agent : '',
      args: redactedRecord(args),
      command: typeof item.command === 'string' ? item.command : '',
      warning: typeof item.warning === 'string' ? item.warning : '',
      approvalKind: typeof item.approvalKind === 'string' ? item.approvalKind : '',
      actionKind: typeof item.actionKind === 'string' ? item.actionKind : '',
      displayKind: typeof item.displayKind === 'string' ? item.displayKind : '',
      displayTarget: typeof item.displayTarget === 'string' ? item.displayTarget : '',
      destructive: item.destructive === true,
      irreversible: item.irreversible === true,
      backupState: typeof item.backupState === 'string' ? item.backupState : '',
      deadline: typeof item.deadline === 'number' && Number.isFinite(item.deadline)
        ? item.deadline
        : null,
    }
  }
  const allowPatterns = Array.isArray(object.allowPatterns)
    ? object.allowPatterns.filter((item): item is string => typeof item === 'string')
    : []
  const denyPatterns = Array.isArray(object.denyPatterns)
    ? object.denyPatterns.filter((item): item is string => typeof item === 'string')
    : []
  return {
    pending: object.pending.map(projectItem),
    mode: snapshotMode,
    allowPatterns,
    denyPatterns,
  }
}

export { APPROVAL_EVENT_WIRE_NAMES }
