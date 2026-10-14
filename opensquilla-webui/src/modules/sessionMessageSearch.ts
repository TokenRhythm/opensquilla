import type { InjectionKey } from 'vue'
import type { SessionReadHistoryPage } from './sessionReadLifecycle'

export interface SessionMessageSearchIdentity {
  readonly key: string
  readonly sessionId?: string
  readonly sessionEpoch?: number
  readonly signal?: AbortSignal
}

export interface SessionMessageSearchRequest extends SessionMessageSearchIdentity {
  readonly query: string
  readonly cursor?: string
  readonly limit?: number
}

export interface SessionMessageSearchContextRequest extends SessionMessageSearchIdentity {
  readonly messageId: string
}

export interface SessionMessageSearchMatch {
  readonly messageId: string
  /** Stable canonical entry coordinate; not a history-page continuation cursor. */
  readonly entryCursor: string
  readonly contentRevision: number
  readonly offsetUtf16: number
  readonly lengthUtf16: number
  readonly snippet: string
}

export interface SessionMessageSearchPage {
  readonly sessionId: string
  readonly sessionEpoch: number
  readonly query: string
  readonly matches: readonly SessionMessageSearchMatch[]
  readonly nextCursor: string | null
  readonly complete: boolean
  /** Latest cumulative proof; may become true after incremental archive verification. */
  readonly coverageComplete: boolean
  readonly scannedEntries: number
  readonly unavailableEntries: number
}

/** Read-only, current-session search. It never changes the live read lease or model context. */
export interface SessionMessageSearch {
  search(request: SessionMessageSearchRequest): Promise<SessionMessageSearchPage>
  context(request: SessionMessageSearchContextRequest): Promise<SessionReadHistoryPage>
}

export type SessionMessageSearchFailure = 'unsupported' | 'stale' | 'busy' | 'unavailable' | 'contract'

export class SessionMessageSearchError extends Error {
  constructor(readonly reason: SessionMessageSearchFailure, message: string, readonly retryAfterMs?: number) {
    super(message)
    this.name = 'SessionMessageSearchError'
  }
}

export const SESSION_MESSAGE_SEARCH_KEY: InjectionKey<SessionMessageSearch> = Symbol('SessionMessageSearch')
