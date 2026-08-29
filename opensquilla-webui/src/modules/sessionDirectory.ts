import type { InjectionKey } from 'vue'

export interface SessionItem {
  key: string
  title: string
  subtitle: string
  groupLabel: string
  workspace?: string
  workspaceId?: string
  workspaceLabel?: string
  workspaceDisplayPath?: string
  effectiveAgentId: string
  sessionKind: string
  surface: string
  conversationKind: string
  status: string
  runStatus: string
  runLabel: string
  messageCount: number | null
  updatedAt: number
  model: string
  parent: { key: string; spawnDepth: number } | null
  provisional?: boolean
  forkedFromParent: boolean
  hasContractGaps: boolean
}

export interface SessionPage {
  items: SessionItem[]
  hasMore: boolean
  nextCursor: string | null
}

export interface SessionCount { value: number; exact: boolean }

export interface RequestOptions {
  signal?: AbortSignal
}

/** Backwards-compatible name for existing query callers. */
export type SessionDirectoryQueryOptions = RequestOptions

export interface ResolvedSession {
  key: string
  id: string
}

export type SessionSearchSessionHit = { key: string; title: string; surface: string | null }
export type SessionSearchMessageHit = { key: string; title: string; snippet: string; createdAt: number | null }
export type SessionSearchResult = {
  sessions: SessionSearchSessionHit[]
  messages: SessionSearchMessageHit[]
}
export type SessionSearchRequest = RequestOptions & { query: string; limit?: number }

export type SessionDirectoryErrorCode =
  | 'not-found'
  | 'unsupported'
  | 'forbidden'
  | 'conflict'
  | 'unavailable'
  | 'invalid'

export class SessionDirectoryError extends Error {
  readonly code: SessionDirectoryErrorCode
  readonly cause?: unknown

  constructor(
    code: SessionDirectoryErrorCode,
    message: string,
    options?: { cause?: unknown },
  ) {
    super(message)
    this.name = 'SessionDirectoryError'
    this.code = code
    this.cause = options?.cause
  }
}

export interface SessionDirectory {
  listPage(
    request: { limit: number; cursor?: string } & SessionDirectoryQueryOptions,
  ): Promise<SessionPage>
  count(options?: SessionDirectoryQueryOptions): Promise<SessionCount | null>
  resolve(request: { key: string } & RequestOptions): Promise<ResolvedSession>
  search(request: SessionSearchRequest): Promise<SessionSearchResult>
}

export const SESSION_DIRECTORY_KEY: InjectionKey<SessionDirectory> = Symbol('SessionDirectory')
