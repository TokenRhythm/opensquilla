import type { InjectionKey } from 'vue'
import type { GatewayModelRoutingMode } from '@/types/modelRouting'
import type { CollaborationMode } from '@/types/plans'
import type { SandboxRunMode } from '@/types/sandbox'
import type { ArtifactProductFailure } from '@/utils/artifactProductErrors'

/** Options shared by turn commands without exposing transport details. */
export interface TurnCommandRequestOptions {
  signal?: AbortSignal
}

export type TurnCommandFailureKind =
  | 'aborted'
  | 'timeout'
  | 'transport'
  | 'queue-capacity'
  | 'session-changed'
  | 'conflict'
  | 'unavailable'
  | 'rejected'

/** Semantic failure projected by the Gateway Adapter for turn recovery. */
export class TurnCommandError extends Error {
  constructor(
    readonly kind: TurnCommandFailureKind,
    message: string,
    readonly failureCode?: string,
    readonly accepted?: boolean | null,
    readonly retryable?: boolean,
    readonly retryAfterMs?: number,
    readonly details?: unknown,
    readonly artifactFailure?: ArtifactProductFailure,
  ) {
    super(message)
    this.name = 'TurnCommandError'
  }
}

/** Exact editable document head bound to one turn admission. */
export interface TurnDocumentContext {
  documentId: string
  headRevisionId: string
}

/** Source policy attached to a turn without exposing the v4 `_source` alias. */
export interface TurnSendSource {
  elevated?: string
  runMode?: SandboxRunMode
  noMemoryCapture?: boolean
  [key: string]: unknown
}

/** Serialized attachment fields owned by the turn domain, not the v4 wire. */
export interface TurnSendAttachment {
  type: string
  mime: string
  name: string
  data?: string
  file_uuid?: string
  size?: number
}

/**
 * Domain input for a new turn.
 *
 * This is intentionally not a generated wire type. The Gateway adapter owns
 * field aliases and validation; the Module exposes semantic names while
 * retaining additive options for forward compatibility.
 */
export interface TurnSendParams {
  message: string
  sessionKey: string
  /** Stable idempotency key for one logical send attempt. */
  clientRequestId?: string
  /** Stable client identity for reconciling the optimistic user row. */
  clientMessageId?: string
  /** Ordered durable drafts consumed atomically with this chat ingress. */
  promptAnnotationIds?: string[]
  /** Current editable document head made available only to this turn. */
  documentContext?: TurnDocumentContext
  /** Source policy; the v4 Adapter maps this to `_source`. */
  source?: TurnSendSource
  intent?: string
  workspaceId?: string
  collaborationMode?: CollaborationMode
  initialRoutingMode?: GatewayModelRoutingMode
  forkBeforeMessageId?: string
  displayText?: string
  attachments?: TurnSendAttachment[]
  /** Explicit admission mode used by ordinary and queued sends. */
  queueMode?: string
  [key: string]: unknown
}

/**
 * Fields consumed by the WebUI after a turn is accepted.
 *
 * The v4 Adapter normalizes the historical snake_case spellings into these
 * names.  Additive fields that have no domain meaning are retained in
 * `metadata`; they are deliberately not spread onto this interface so wire
 * aliases cannot become an accidental application API.
 */
export interface TurnSendResponse {
  ok?: boolean
  status?: string
  sessionKey?: string
  key?: string
  messageId?: string
  userMessageId?: string
  clientMessageId?: string
  taskId?: string
  replayed?: boolean
  instantAccept?: boolean
  taskStatus?: string
  terminalReason?: string
  terminalMessage?: string
  reason?: string
  acceptedPromptAnnotationIds?: string[]
  metadata?: Readonly<Record<string, unknown>>
}

/** The two admission paths currently used by the WebUI. */
export interface PendingInputDispatchRequest {
  key: string
  pendingInputId: string
  clientRequestId: string
  requestFingerprint: string
}

/** Domain request for same-turn or durable steer admission. */
export interface TurnSteerRequest {
  key: string
  message: string
  expectedTurnId: string
  clientRequestId: string
  clientMessageId: string
  pendingInputId?: string
  requestFingerprint?: string
  expectedRevision?: number
  surfaceId?: string
  source?: { [key: string]: unknown }
  [key: string]: unknown
}

export type TurnSteerDisposition =
  | 'steering'
  | 'applied'
  | 'promoted'
  | 'cancelled'
  | 'rejected'

/** Fields consumed by the WebUI's steer delivery state machine. */
export interface TurnSteerResponse {
  status?: string
  accepted?: boolean
  replayed?: boolean
  key?: string
  sessionKey?: string
  sessionId?: string
  expectedTurnId?: string
  taskId?: string
  turnId?: string
  userMessageId?: string
  clientRequestId?: string
  clientMessageId?: string
  surfaceId?: string
  disposition?: TurnSteerDisposition
  revision?: number
  promotedTurnId?: string
  promotedFromTurnId?: string
  activeTurnId?: string
  appliedIteration?: number
  modelCallId?: string
  fallbackSafe?: boolean
  failureCode?: string
  retryable?: boolean
  recovery?: string
  reason?: string
  steerCapability?: { [key: string]: unknown }
  metadata?: Readonly<Record<string, unknown>>
}

/**
 * A request whose acceptance may need to be replayed after a lost response.
 * `kind` is an application concern; v4 method names stay in the Adapter.
 */
export type TurnSendRequest =
  | { kind: 'new-turn'; params: TurnSendParams }
  | { kind: 'pending-input'; params: PendingInputDispatchRequest }

export interface TurnCancelRequest {
  sessionKey: string
  source?: string
  scope?: string
  taskId?: string
}

export interface TurnCancelResponse {
  status?: string
  aborted?: boolean
  sessionKey?: string
  taskId?: string
  reason?: string
  metadata?: Readonly<Record<string, unknown>>
}

export type TurnCommandCapability = 'same-turn-steer' | 'durable-steer'

/**
 * Application-facing turn command seam.
 *
 * Consumers own admission state and idempotency decisions, but do not know
 * which v4 alias carries a request. `send` covers both ordinary turn
 * admission and dispatch of an already durable pending input: both are one
 * domain operation (make this logical input eligible for execution), while
 * the Adapter selects the transport route. Response types are owned by this
 * Module; generated wire types remain confined to the Adapter.
 */
export interface TurnCommands {
  send(
    request: TurnSendRequest,
    options?: TurnCommandRequestOptions,
  ): Promise<TurnSendResponse>
  cancel(
    request: TurnCancelRequest,
    options?: TurnCommandRequestOptions,
  ): Promise<TurnCancelResponse>
  steer(
    request: TurnSteerRequest,
    options?: TurnCommandRequestOptions,
  ): Promise<TurnSteerResponse>
  supports(capability: TurnCommandCapability): boolean
}

export const TURN_COMMANDS_KEY: InjectionKey<TurnCommands> = Symbol('TurnCommands')
