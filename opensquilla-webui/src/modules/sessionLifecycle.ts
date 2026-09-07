import type { InjectionKey } from 'vue'

/** Options shared by lifecycle commands without exposing transport details. */
export interface SessionLifecycleRequestOptions {
  signal?: AbortSignal
}

/** The semantic input for creating a durable session. */
export interface SessionCreateRequest extends SessionLifecycleRequestOptions {
  agentId?: string
  kind?: string
  workspaceId?: string
  title?: string
  message?: string
  model?: string
}

/** Identity returned by the Gateway after a session is materialized. */
export interface CreatedSession {
  key: string
  sessionId: string
  seededMessage?: boolean
  note?: string
}

/** Fork a conversation, optionally including one complete terminal turn. */
export interface SessionForkRequest extends SessionLifecycleRequestOptions {
  key: string
  throughTurnId?: string
}

/** Durable child identity; wire acknowledgement details stay in the Adapter. */
export interface ForkedSession {
  key: string
}

export interface SessionRenameRequest extends SessionLifecycleRequestOptions {
  key: string
  title: string
}

export interface SessionRenameResult {
  key: string
  updatedFields: string[]
}

/** Partial-success result: a failed key does not hide successful deletions. */
export interface SessionDeleteResult {
  deleted: string[]
  errors: string[]
}

export type SessionLifecycleErrorCode =
  | 'not-found'
  | 'unsupported'
  | 'forbidden'
  | 'conflict'
  | 'unavailable'
  | 'invalid'

export class SessionLifecycleError extends Error {
  readonly code: SessionLifecycleErrorCode
  readonly cause?: unknown

  constructor(
    code: SessionLifecycleErrorCode,
    message: string,
    options?: { cause?: unknown },
  ) {
    super(message)
    this.name = 'SessionLifecycleError'
    this.code = code
    this.cause = options?.cause
  }
}

/**
 * Session lifecycle is a domain seam.  Callers know titles and identities,
 * never RPC method names, wire aliases, or transport generations.
 */
export interface SessionLifecycle {
  create(request?: SessionCreateRequest): Promise<CreatedSession>
  fork(request: SessionForkRequest): Promise<ForkedSession>
  rename(request: SessionRenameRequest): Promise<SessionRenameResult>
  remove(
    keys: readonly string[],
    options?: SessionLifecycleRequestOptions,
  ): Promise<SessionDeleteResult>
}

export const SESSION_LIFECYCLE_KEY: InjectionKey<SessionLifecycle> =
  Symbol('SessionLifecycle')
