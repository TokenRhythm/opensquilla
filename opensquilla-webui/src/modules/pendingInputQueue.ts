import type { InjectionKey } from 'vue'
import type { Attachment } from '@/types/chat'

/** Domain-facing representation of a durable pending-input row. */
export interface PendingInputServerItem {
  readonly pendingInputId: string
  readonly clientRequestId: string
  readonly clientMessageId: string
  readonly message?: string
  readonly displayText?: string
  readonly attachments?: readonly PendingInputServerAttachment[]
  readonly position?: number
  readonly revision?: number
  readonly requestFingerprint?: string
  readonly promptAnnotationIds?: readonly string[]
  readonly intent?: string | null
  readonly confirmedPlainText?: boolean
}

export interface PendingInputServerAttachment {
  readonly name: string
  readonly mime: string
  readonly size?: number
}

export interface PendingInputEnqueueRequest {
  key: string
  pendingInputId: string
  clientRequestId?: string
  clientMessageId?: string
  message: string
  attachments: readonly unknown[]
  promptAnnotationIds?: readonly string[]
  confirmedPlainText?: boolean
  displayText?: string
  intent?: string | null
  position?: number
}

export interface PendingInputEnqueueResult {
  requestFingerprint?: string
  revision?: number
  position?: number
}

export interface PendingInputCancelRequest {
  key: string
  pendingInputId: string
  expectedRevision?: number
}

export interface PendingInputReorderRequest {
  key: string
  items: readonly {
    pendingInputId?: string
    expectedRevision?: number
  }[]
}

export interface PendingInputReorderResult {
  items: PendingInputServerItem[]
}

export type PendingInputQueueErrorKind =
  | 'unsupported'
  | 'cancelled'
  | 'already-dispatched'
  | 'attachment-expired'
  | 'attachment-lost'
  | 'rejected'
  | 'unavailable'
  | 'invalid'

/** Durable-queue failure projected by the Gateway Adapter. */
export class PendingInputQueueError extends Error {
  constructor(
    readonly kind: PendingInputQueueErrorKind,
    message: string,
    readonly accepted: boolean | null = null,
    readonly retryable = false,
    readonly retryAfterMs = 0,
    readonly cause?: unknown,
  ) {
    super(message)
    this.name = 'PendingInputQueueError'
  }
}

/**
 * Stable domain port for durable pending-input operations. RPC method names,
 * wire aliases and transport generations stay behind the Gateway Adapter.
 */
export interface PendingInputQueuePort {
  readonly supportsQueue: () => boolean
  readonly supportsReorder: () => boolean
  enqueue: (request: PendingInputEnqueueRequest) => Promise<PendingInputEnqueueResult>
  list: (sessionKey: string) => Promise<PendingInputServerItem[]>
  cancel: (request: PendingInputCancelRequest) => Promise<void>
  reorder: (request: PendingInputReorderRequest) => Promise<PendingInputReorderResult>
  ready?: (options?: {
    timeoutMs?: number
    signal?: AbortSignal
  }) => Promise<void>
}

export const PENDING_INPUT_QUEUE_KEY: InjectionKey<PendingInputQueuePort> =
  Symbol('PendingInputQueue')

// Keep Attachment referenced in this module's public seam for consumers that
// import the domain request alongside composer payloads, without leaking wire
// contract types through the Module/UI boundary.
export type PendingInputAttachment = Attachment
