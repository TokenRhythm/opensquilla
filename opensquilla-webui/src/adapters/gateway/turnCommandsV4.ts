import type { TransportCallOptions as RpcCallOptions } from './transportTypes'
import { readTransportFailure } from './transportTypes'
import {
  CHAT_ABORT_METHOD,
  type ChatAbortParams,
  type ChatAbortResult,
} from '@/contracts/generated/v4/chatAbort'
import { validateChatAbortResult } from '@/contracts/generated/v4/chatAbortValidators.mjs'
import {
  CHAT_SEND_METHOD,
  type ChatSendResult,
} from '@/contracts/generated/v4/chatSend'
import { validateChatSendResult } from '@/contracts/generated/v4/chatSendValidators.mjs'
import {
  SESSIONS_PENDING_INPUTS_DISPATCH_METHOD,
  type SessionsPendingInputsDispatchParams,
  type SessionsPendingInputsDispatchResult,
} from '@/contracts/generated/v4/pendingInputsDispatch'
import {
  validatePendingInputsDispatchResult,
} from '@/contracts/generated/v4/pendingInputsDispatchValidators.mjs'
import {
  SESSIONS_PENDING_INPUTS_STEER_METHOD,
  type SessionsPendingInputsSteerParams,
  type SessionsPendingInputsSteerResult,
} from '@/contracts/generated/v4/pendingInputsSteer'
import {
  validatePendingInputsSteerResult,
} from '@/contracts/generated/v4/pendingInputsSteerValidators.mjs'
import {
  SESSIONS_STEER_V2_METHOD,
  type SessionsSteerV2Params,
  type SessionsSteerV2Result,
} from '@/contracts/generated/v4/sessionsSteerV2'
import {
  validateSessionsSteerV2Result,
} from '@/contracts/generated/v4/sessionsSteerV2Validators.mjs'
import {
  type TurnSendRequest,
  type TurnCancelRequest,
  type TurnCancelResponse,
  type TurnCommandCapability,
  type TurnCommands,
  type TurnCommandRequestOptions,
  TurnCommandError,
  type TurnSendParams,
  type TurnSendResponse,
  type TurnSteerDisposition,
  type TurnSteerRequest,
  type TurnSteerResponse,
} from '@/modules/turnCommands'
import { mapArtifactProductFailure } from './artifactErrorMapping'

function mapTurnCommandError(error: unknown): TurnCommandError {
  if (error instanceof TurnCommandError) return error
  const failure = readTransportFailure(error)
  const code = failure.code
  const kind = code === 'RPC_ABORTED'
    ? 'aborted'
    : code === 'RPC_TIMEOUT'
      ? 'timeout'
      : code === 'RPC_TRANSPORT_ERROR'
        ? 'transport'
        : code === 'QUEUE_FULL' || code === 'QUEUE_FULL_DIRTY'
          ? 'queue-capacity'
          : code === 'SESSION_CHANGED'
            ? 'session-changed'
            : code?.includes('CONFLICT')
              ? 'conflict'
              : code === 'UNAVAILABLE' || code === 'STORAGE_BUSY' || code === 'CANCEL_TIMEOUT'
                ? 'unavailable'
                : 'rejected'
  return new TurnCommandError(
    kind,
    failure.message,
    code,
    failure.accepted,
    failure.retryable,
    failure.retryAfterMs,
    failure.details,
    mapArtifactProductFailure(error),
  )
}

/** Narrow wire port owned by this Adapter. */
export interface TurnCommandsTransport {
  request<T = unknown>(
    method: string,
    params?: Record<string, unknown>,
    options?: RpcCallOptions,
  ): Promise<T>
  supports?(method: string): boolean
}

/**
 * A response crossed the v4 Adapter boundary without satisfying its
 * generated Contract.  This is intentionally a transport-facing error: the
 * domain Module must never have to understand JSON-RPC envelopes or aliases.
 */
export class TurnCommandContractError extends Error {
  readonly method: string
  readonly validationErrors: readonly unknown[]

  constructor(method: string, validationErrors: readonly unknown[] = []) {
    super(`${method} returned a response that violates its v4 Contract`)
    this.name = 'TurnCommandContractError'
    this.method = method
    this.validationErrors = validationErrors
  }
}

type ContractValidator = ((value: unknown) => boolean) & {
  errors?: readonly unknown[] | null
}

type JsonObject = Record<string, unknown>

function asObject(value: unknown): JsonObject {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as JsonObject
    : {}
}

function firstDefined(object: JsonObject, ...keys: string[]): unknown {
  for (const key of keys) {
    if (
      Object.prototype.hasOwnProperty.call(object, key)
      && object[key] !== null
      && object[key] !== undefined
    ) {
      return object[key]
    }
  }
  return undefined
}

function optionalString(value: unknown): string | undefined {
  return typeof value === 'string' ? value : undefined
}

function optionalBoolean(value: unknown): boolean | undefined {
  return typeof value === 'boolean' ? value : undefined
}

function optionalNumber(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined
}

function optionalDisposition(value: unknown): TurnSteerDisposition | undefined {
  return value === 'steering'
    || value === 'applied'
    || value === 'promoted'
    || value === 'cancelled'
    || value === 'rejected'
    ? value
    : undefined
}

function metadataFor(object: JsonObject, known: readonly string[]): Readonly<Record<string, unknown>> | undefined {
  const knownKeys = new Set(known)
  const metadata = Object.fromEntries(
    Object.entries(object).filter(([key]) => !knownKeys.has(key)),
  )
  return Object.keys(metadata).length > 0 ? metadata : undefined
}

const SEND_RESULT_KEYS = [
  'ok', 'status', 'sessionKey', 'session_key', 'key',
  'messageId', 'message_id', 'userMessageId', 'user_message_id',
  'clientMessageId', 'client_message_id', 'taskId', 'task_id',
  'replayed', 'instantAccept', 'instant_accept', 'taskStatus', 'task_status',
  'terminalReason', 'terminal_reason', 'terminalMessage', 'terminal_message',
  'reason', 'acceptedPromptAnnotationIds', 'accepted_prompt_annotation_ids',
]

const CANCEL_RESULT_KEYS = [
  'ok', 'status', 'aborted', 'sessionKey', 'session_key', 'key',
  'taskId', 'task_id', 'reason', 'cancelled',
]

const STEER_RESULT_KEYS = [
  'status', 'accepted', 'replayed', 'key', 'sessionKey', 'session_key',
  'sessionId', 'session_id', 'expectedTurnId', 'expected_turn_id',
  'taskId', 'task_id', 'turnId', 'turn_id', 'userMessageId', 'user_message_id',
  'clientRequestId', 'client_request_id', 'clientMessageId', 'client_message_id',
  'surfaceId', 'surface_id', 'disposition', 'revision',
  'promotedTurnId', 'promoted_turn_id', 'promotedFromTurnId', 'promoted_from_turn_id',
  'activeTurnId', 'active_turn_id', 'appliedIteration', 'applied_iteration',
  'modelCallId', 'model_call_id', 'fallbackSafe', 'fallback_safe',
  'failureCode', 'failure_code', 'retryable', 'recovery', 'reason',
  'steerCapability', 'steer_capability',
]

/** Convert a validated v4 result into the application-facing send shape. */
function projectSendResult(raw: ChatSendResult | SessionsPendingInputsDispatchResult): TurnSendResponse {
  const object = asObject(raw)
  const result: TurnSendResponse = {
    ...(optionalBoolean(object.ok) !== undefined ? { ok: object.ok as boolean } : {}),
    ...(optionalString(object.status) !== undefined ? { status: object.status as string } : {}),
    ...(optionalString(firstDefined(object, 'sessionKey', 'session_key')) !== undefined
      ? { sessionKey: firstDefined(object, 'sessionKey', 'session_key') as string }
      : {}),
    ...(optionalString(object.key) !== undefined ? { key: object.key as string } : {}),
    ...(optionalString(firstDefined(object, 'messageId', 'message_id')) !== undefined
      ? { messageId: firstDefined(object, 'messageId', 'message_id') as string }
      : {}),
    ...(optionalString(firstDefined(object, 'userMessageId', 'user_message_id')) !== undefined
      ? { userMessageId: firstDefined(object, 'userMessageId', 'user_message_id') as string }
      : {}),
    ...(optionalString(firstDefined(object, 'clientMessageId', 'client_message_id')) !== undefined
      ? { clientMessageId: firstDefined(object, 'clientMessageId', 'client_message_id') as string }
      : {}),
    ...(optionalString(firstDefined(object, 'taskId', 'task_id')) !== undefined
      ? { taskId: firstDefined(object, 'taskId', 'task_id') as string }
      : {}),
    ...(optionalBoolean(object.replayed) !== undefined ? { replayed: object.replayed as boolean } : {}),
    ...(optionalBoolean(firstDefined(object, 'instantAccept', 'instant_accept')) !== undefined
      ? { instantAccept: firstDefined(object, 'instantAccept', 'instant_accept') as boolean }
      : {}),
    ...(optionalString(firstDefined(object, 'taskStatus', 'task_status')) !== undefined
      ? { taskStatus: firstDefined(object, 'taskStatus', 'task_status') as string }
      : {}),
    ...(optionalString(firstDefined(object, 'terminalReason', 'terminal_reason')) !== undefined
      ? { terminalReason: firstDefined(object, 'terminalReason', 'terminal_reason') as string }
      : {}),
    ...(optionalString(firstDefined(object, 'terminalMessage', 'terminal_message')) !== undefined
      ? { terminalMessage: firstDefined(object, 'terminalMessage', 'terminal_message') as string }
      : {}),
    ...(optionalString(object.reason) !== undefined ? { reason: object.reason as string } : {}),
    ...(Array.isArray(firstDefined(object, 'acceptedPromptAnnotationIds', 'accepted_prompt_annotation_ids'))
      ? { acceptedPromptAnnotationIds: firstDefined(object, 'acceptedPromptAnnotationIds', 'accepted_prompt_annotation_ids') as string[] }
      : {}),
  }
  const metadata = metadataFor(object, SEND_RESULT_KEYS)
  return metadata ? { ...result, metadata } : result
}

function projectCancelResult(raw: ChatAbortResult): TurnCancelResponse {
  const object = asObject(raw)
  const result: TurnCancelResponse = {
    ...(optionalString(object.status) !== undefined ? { status: object.status as string } : {}),
    ...(optionalBoolean(object.aborted) !== undefined ? { aborted: object.aborted as boolean } : {}),
    ...(optionalString(firstDefined(object, 'sessionKey', 'session_key')) !== undefined
      ? { sessionKey: firstDefined(object, 'sessionKey', 'session_key') as string }
      : {}),
    ...(optionalString(firstDefined(object, 'taskId', 'task_id')) !== undefined
      ? { taskId: firstDefined(object, 'taskId', 'task_id') as string }
      : {}),
    ...(optionalString(object.reason) !== undefined ? { reason: object.reason as string } : {}),
  }
  const metadata = metadataFor(object, CANCEL_RESULT_KEYS)
  return metadata ? { ...result, metadata } : result
}

function projectSteerResult(raw: SessionsSteerV2Result | SessionsPendingInputsSteerResult): TurnSteerResponse {
  const object = asObject(raw)
  const disposition = optionalDisposition(object.disposition)
  const result: TurnSteerResponse = {
    ...(optionalString(object.status) !== undefined ? { status: object.status as string } : {}),
    ...(optionalBoolean(object.accepted) !== undefined ? { accepted: object.accepted as boolean } : {}),
    ...(optionalBoolean(object.replayed) !== undefined ? { replayed: object.replayed as boolean } : {}),
    ...(optionalString(object.key) !== undefined ? { key: object.key as string } : {}),
    ...(optionalString(firstDefined(object, 'sessionKey', 'session_key')) !== undefined
      ? { sessionKey: firstDefined(object, 'sessionKey', 'session_key') as string }
      : {}),
    ...(optionalString(firstDefined(object, 'sessionId', 'session_id')) !== undefined
      ? { sessionId: firstDefined(object, 'sessionId', 'session_id') as string }
      : {}),
    ...(optionalString(firstDefined(object, 'expectedTurnId', 'expected_turn_id')) !== undefined
      ? { expectedTurnId: firstDefined(object, 'expectedTurnId', 'expected_turn_id') as string }
      : {}),
    ...(optionalString(firstDefined(object, 'taskId', 'task_id')) !== undefined
      ? { taskId: firstDefined(object, 'taskId', 'task_id') as string }
      : {}),
    ...(optionalString(firstDefined(object, 'turnId', 'turn_id')) !== undefined
      ? { turnId: firstDefined(object, 'turnId', 'turn_id') as string }
      : {}),
    ...(optionalString(firstDefined(object, 'userMessageId', 'user_message_id')) !== undefined
      ? { userMessageId: firstDefined(object, 'userMessageId', 'user_message_id') as string }
      : {}),
    ...(optionalString(firstDefined(object, 'clientRequestId', 'client_request_id')) !== undefined
      ? { clientRequestId: firstDefined(object, 'clientRequestId', 'client_request_id') as string }
      : {}),
    ...(optionalString(firstDefined(object, 'clientMessageId', 'client_message_id')) !== undefined
      ? { clientMessageId: firstDefined(object, 'clientMessageId', 'client_message_id') as string }
      : {}),
    ...(optionalString(firstDefined(object, 'surfaceId', 'surface_id')) !== undefined
      ? { surfaceId: firstDefined(object, 'surfaceId', 'surface_id') as string }
      : {}),
    ...(disposition ? { disposition } : {}),
    ...(optionalNumber(object.revision) !== undefined ? { revision: object.revision as number } : {}),
    ...(optionalString(firstDefined(object, 'promotedTurnId', 'promoted_turn_id')) !== undefined
      ? { promotedTurnId: firstDefined(object, 'promotedTurnId', 'promoted_turn_id') as string }
      : {}),
    ...(optionalString(firstDefined(object, 'promotedFromTurnId', 'promoted_from_turn_id')) !== undefined
      ? { promotedFromTurnId: firstDefined(object, 'promotedFromTurnId', 'promoted_from_turn_id') as string }
      : {}),
    ...(optionalString(firstDefined(object, 'activeTurnId', 'active_turn_id')) !== undefined
      ? { activeTurnId: firstDefined(object, 'activeTurnId', 'active_turn_id') as string }
      : {}),
    ...(optionalNumber(firstDefined(object, 'appliedIteration', 'applied_iteration')) !== undefined
      ? { appliedIteration: firstDefined(object, 'appliedIteration', 'applied_iteration') as number }
      : {}),
    ...(optionalString(firstDefined(object, 'modelCallId', 'model_call_id')) !== undefined
      ? { modelCallId: firstDefined(object, 'modelCallId', 'model_call_id') as string }
      : {}),
    ...(optionalBoolean(firstDefined(object, 'fallbackSafe', 'fallback_safe')) !== undefined
      ? { fallbackSafe: firstDefined(object, 'fallbackSafe', 'fallback_safe') as boolean }
      : {}),
    ...(optionalString(firstDefined(object, 'failureCode', 'failure_code')) !== undefined
      ? { failureCode: firstDefined(object, 'failureCode', 'failure_code') as string }
      : {}),
    ...(optionalBoolean(object.retryable) !== undefined ? { retryable: object.retryable as boolean } : {}),
    ...(optionalString(object.recovery) !== undefined ? { recovery: object.recovery as string } : {}),
    ...(optionalString(object.reason) !== undefined ? { reason: object.reason as string } : {}),
    ...(object.steerCapability && typeof object.steerCapability === 'object'
      ? { steerCapability: object.steerCapability as { [key: string]: unknown } }
      : object.steer_capability && typeof object.steer_capability === 'object'
        ? { steerCapability: object.steer_capability as { [key: string]: unknown } }
        : {}),
  }
  const metadata = metadataFor(object, STEER_RESULT_KEYS)
  return metadata ? { ...result, metadata } : result
}

/**
 * Project canonical send input back to the unchanged v4 wire shape.
 *
 * The WebUI Module deliberately does not expose `_source` or persisted
 * snake_case aliases. This is the only production projection for chat.send;
 * additive fields from a recovered WAL record remain opaque extensions.
 */
export function toWireSendParams(request: TurnSendParams): Record<string, unknown> {
  const sourceRecord = request as TurnSendParams & Record<string, unknown>
  const {
    message,
    sessionKey,
    clientRequestId,
    clientMessageId,
    promptAnnotationIds,
    documentContext,
    source,
    intent,
    workspaceId,
    collaborationMode,
    initialRoutingMode,
    forkBeforeMessageId,
    displayText,
    attachments,
    queueMode,
    // Legacy aliases can exist in handoff WAL records written by an older
    // client. They are removed when a canonical value is present below, but
    // remain available as a fallback when only the old spelling was stored.
    session_key: legacySessionKey,
    key: legacyKey,
    client_request_id: legacyClientRequestId,
    client_message_id: legacyClientMessageId,
    prompt_annotation_ids: legacyPromptAnnotationIds,
    document_context: legacyDocumentContext,
    _source: legacySource,
    workspace_id: legacyWorkspaceId,
    collaboration_mode: legacyCollaborationMode,
    initial_routing_mode: legacyInitialRoutingMode,
    fork_before_message_id: legacyForkBeforeMessageId,
    display_text: legacyDisplayText,
    ...extensions
  } = sourceRecord

  return {
    ...extensions,
    message,
    ...(sessionKey !== undefined
      ? { sessionKey }
      : legacySessionKey !== undefined
        ? { session_key: legacySessionKey }
        : legacyKey !== undefined
          ? { key: legacyKey }
          : {}),
    ...(clientRequestId !== undefined
      ? { clientRequestId }
      : legacyClientRequestId !== undefined
        ? { client_request_id: legacyClientRequestId }
        : {}),
    ...(clientMessageId !== undefined
      ? { clientMessageId }
      : legacyClientMessageId !== undefined
        ? { client_message_id: legacyClientMessageId }
        : {}),
    ...(promptAnnotationIds !== undefined
      ? { promptAnnotationIds }
      : legacyPromptAnnotationIds !== undefined
        ? { prompt_annotation_ids: legacyPromptAnnotationIds }
        : {}),
    ...(documentContext !== undefined
      ? { documentContext }
      : legacyDocumentContext !== undefined
        ? { document_context: legacyDocumentContext }
        : {}),
    ...(source !== undefined
      ? { _source: source }
      : legacySource !== undefined
        ? { _source: legacySource }
        : {}),
    ...(intent !== undefined ? { intent } : {}),
    ...(workspaceId !== undefined
      ? { workspaceId }
      : legacyWorkspaceId !== undefined
        ? { workspace_id: legacyWorkspaceId }
        : {}),
    ...(collaborationMode !== undefined
      ? { collaborationMode }
      : legacyCollaborationMode !== undefined
        ? { collaboration_mode: legacyCollaborationMode }
        : {}),
    ...(initialRoutingMode !== undefined
      ? { initialRoutingMode }
      : legacyInitialRoutingMode !== undefined
        ? { initial_routing_mode: legacyInitialRoutingMode }
        : {}),
    ...(forkBeforeMessageId !== undefined
      ? { forkBeforeMessageId }
      : legacyForkBeforeMessageId !== undefined
        ? { fork_before_message_id: legacyForkBeforeMessageId }
        : {}),
    ...(displayText !== undefined
      ? { displayText }
      : legacyDisplayText !== undefined
        ? { display_text: legacyDisplayText }
        : {}),
    ...(attachments !== undefined ? { attachments } : {}),
    ...(queueMode !== undefined ? { queueMode } : {}),
  }
}

/** Project canonical steer input back to the unchanged v4 wire aliases. */
function toWireSteerParams(request: TurnSteerRequest): Record<string, unknown> {
  const {
    key,
    message,
    expectedTurnId,
    clientRequestId,
    clientMessageId,
    pendingInputId,
    requestFingerprint,
    expectedRevision,
    surfaceId,
    source,
    ...extensions
  } = request
  return {
    ...extensions,
    key,
    message,
    ...(expectedTurnId !== undefined ? { expected_turn_id: expectedTurnId } : {}),
    ...(clientRequestId !== undefined ? { client_request_id: clientRequestId } : {}),
    ...(clientMessageId !== undefined ? { client_message_id: clientMessageId } : {}),
    ...(pendingInputId !== undefined ? { pendingInputId } : {}),
    ...(requestFingerprint !== undefined ? { requestFingerprint } : {}),
    ...(expectedRevision !== undefined ? { expectedRevision } : {}),
    ...(surfaceId !== undefined ? { surface_id: surfaceId } : {}),
    ...(source !== undefined ? { _source: source } : {}),
  }
}

function requestOptions(options?: TurnCommandRequestOptions): RpcCallOptions | undefined {
  return options?.signal ? { signal: options.signal } : undefined
}

function forward<T>(
  transport: TurnCommandsTransport,
  method: string,
  params: Record<string, unknown>,
  options?: TurnCommandRequestOptions,
): Promise<T> {
  const rpcOptions = requestOptions(options)
  const request = rpcOptions
    ? transport.request<T>(method, params, rpcOptions)
    : transport.request<T>(method, params)
  return request.catch(error => {
    throw mapTurnCommandError(error)
  })
}

function response<T>(
  method: string,
  validator: ContractValidator,
  raw: unknown,
): T {
  let valid = false
  try {
    valid = validator(raw)
  } catch (error) {
    throw new TurnCommandContractError(method, [error])
  }
  if (!valid) {
    throw new TurnCommandContractError(method, validator.errors ?? [])
  }
  return raw as T
}

function forwardContract<T>(
  transport: TurnCommandsTransport,
  method: string,
  params: Record<string, unknown>,
  responseValidator: ContractValidator,
  options?: TurnCommandRequestOptions,
): Promise<T> {
  return forward<unknown>(transport, method, params, options).then(raw => (
    response<T>(method, responseValidator, raw)
  ))
}

/**
 * Adapt semantic turn commands to the unchanged v4 JSON wire.
 *
 * This is intentionally a compatibility Adapter, not a second implementation:
 * it selects legacy method aliases, projects canonical domain inputs to the
 * historical wire spellings, and projects responses back without touching
 * backend command behavior.
 */
export function createV4TurnCommands(transport: TurnCommandsTransport): TurnCommands {
  const hasRpcMethod = (method: string): boolean => (
    transport.supports?.(method) ?? false
  )

  return {
    send: async (
      request: TurnSendRequest,
      options?: TurnCommandRequestOptions,
    ): Promise<TurnSendResponse> => {
      if (request.kind === 'pending-input') {
        const params = request.params as unknown as SessionsPendingInputsDispatchParams
        return forwardContract<SessionsPendingInputsDispatchResult>(
          transport,
          SESSIONS_PENDING_INPUTS_DISPATCH_METHOD,
          params as unknown as Record<string, unknown>,
          validatePendingInputsDispatchResult,
          options,
        ).then(projectSendResult) as Promise<TurnSendResponse>
      }
      const params = toWireSendParams(request.params)
      return forwardContract<ChatSendResult>(
        transport,
        CHAT_SEND_METHOD,
        params,
        validateChatSendResult,
        options,
      ).then(projectSendResult)
    },

    cancel: (
      request: TurnCancelRequest,
      options?: TurnCommandRequestOptions,
    ): Promise<TurnCancelResponse> => {
      const params = request as unknown as ChatAbortParams
      return forwardContract<ChatAbortResult>(
        transport,
        CHAT_ABORT_METHOD,
        params as unknown as Record<string, unknown>,
        validateChatAbortResult,
        options,
      ).then(projectCancelResult)
    },

    steer: (
      request: TurnSteerRequest,
      options?: TurnCommandRequestOptions,
    ): Promise<TurnSteerResponse> => {
      if (request.pendingInputId) {
        const params = toWireSteerParams(request) as SessionsPendingInputsSteerParams
        return forwardContract<SessionsPendingInputsSteerResult>(
          transport,
          SESSIONS_PENDING_INPUTS_STEER_METHOD,
          params as unknown as Record<string, unknown>,
          validatePendingInputsSteerResult,
          options,
        ).then(projectSteerResult)
      }
      const params = toWireSteerParams(request) as SessionsSteerV2Params
      return forwardContract<SessionsSteerV2Result>(
        transport,
        SESSIONS_STEER_V2_METHOD,
        params as unknown as Record<string, unknown>,
        validateSessionsSteerV2Result,
        options,
      ).then(projectSteerResult)
    },

    supports: (capability: TurnCommandCapability): boolean => {
      if (capability === 'same-turn-steer') return hasRpcMethod(SESSIONS_STEER_V2_METHOD)
      return hasRpcMethod(SESSIONS_PENDING_INPUTS_STEER_METHOD)
    },
  }
}

/**
 * Keep the method mapping testable without exposing a generic RPC client from
 * the application Module.  This helper is useful for transitional call-site
 * tests while production composition uses `createPrivateGatewayTransports`.
 */
export function createV4TurnCommandsFromRpcClient(client: {
  call<T = unknown>(
    method: string,
    params?: Record<string, unknown>,
    options?: RpcCallOptions,
  ): Promise<T>
  hasRpcMethod?(method: string): boolean
}, hasRpcMethod?: (method: string) => boolean): TurnCommands {
  return createV4TurnCommands({
    request: (method, params, options) => options
      ? client.call(method, params, options)
      : client.call(method, params),
    supports: method => hasRpcMethod?.(method)
      ?? client.hasRpcMethod?.(method)
      ?? false,
  })
}
