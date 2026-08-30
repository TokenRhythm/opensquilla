import type { InjectionKey } from 'vue'

// S13 deliberately keeps the existing command payload/response shapes so the
// migration changes ownership, not wire semantics. These type-only imports are
// a temporary seam: S14 replaces them with generated Contract/domain shapes
// and removes the corresponding declarations from types/rpc.ts.
import type {
  ChatSendParams,
  ChatSendResponse,
  SessionSteerV2Params,
  SessionSteerV2Response,
} from '@/types/rpc'

/** Options shared by turn commands without exposing transport details. */
export interface TurnCommandRequestOptions {
  signal?: AbortSignal
}

/** The two admission paths currently used by the WebUI. */
export interface PendingInputDispatchRequest {
  key: string
  pendingInputId: string
  clientRequestId: string
  requestFingerprint: string
}

/**
 * A request whose acceptance may need to be replayed after a lost response.
 * `kind` is an application concern; v4 method names stay in the Adapter.
 */
export type TurnSendRequest =
  | { kind: 'new-turn'; params: ChatSendParams }
  | { kind: 'pending-input'; params: PendingInputDispatchRequest }

export interface TurnCancelRequest {
  sessionKey: string
  source?: string
  scope?: string
  taskId?: string
}

export interface TurnCancelResponse {
  aborted?: boolean
  reason?: string
  [key: string]: unknown
}

export type TurnCommandCapability = 'same-turn-steer' | 'durable-steer'

/**
 * Application-facing turn command seam.
 *
 * Consumers own admission state and idempotency decisions, but do not know
 * which v4 alias carries a request. `send` covers both ordinary turn
 * admission and dispatch of an already durable pending input: both are one
 * domain operation (make this logical input eligible for execution), while
 * the Adapter selects the transport route. The transitional response types
 * still mirror the existing wire shape; S14 will replace them with generated
 * Contract types after the command semantics are stable.
 */
export interface TurnCommands {
  send(
    request: TurnSendRequest,
    options?: TurnCommandRequestOptions,
  ): Promise<ChatSendResponse>
  cancel(
    request: TurnCancelRequest,
    options?: TurnCommandRequestOptions,
  ): Promise<TurnCancelResponse>
  steer(
    request: SessionSteerV2Params,
    options?: TurnCommandRequestOptions,
  ): Promise<SessionSteerV2Response>
  supports(capability: TurnCommandCapability): boolean
}

export const TURN_COMMANDS_KEY: InjectionKey<TurnCommands> = Symbol('TurnCommands')
