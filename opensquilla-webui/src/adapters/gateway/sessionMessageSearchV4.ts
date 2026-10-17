import { readTransportFailure, type TransportCallOptions } from './transportTypes'
import { projectV2History } from './sessionReadV2'
import {
  SESSIONS_MESSAGES_SEARCH_METHOD,
  type Params as SearchParams,
  type Result as SearchResult,
} from '@/contracts/generated/v4/sessionsMessagesSearch'
import {
  validateParams as validateSearchParams,
  validateResult as validateSearchResult,
} from '@/contracts/generated/v4/sessionsMessagesSearchValidators.mjs'
import {
  SESSIONS_HISTORY_PAGE_V2_METHOD,
  type Params as HistoryParams,
  type Result as HistoryResult,
} from '@/contracts/generated/v4/sessionsHistoryPageV2'
import {
  validateParams as validateHistoryParams,
  validateResult as validateHistoryResult,
} from '@/contracts/generated/v4/sessionsHistoryPageV2Validators.mjs'
import {
  SessionMessageSearchError,
  type SessionMessageSearch,
  type SessionMessageSearchIdentity,
} from '@/modules/sessionMessageSearch'

interface SearchTransport {
  readonly generation: number
  supports(method: string): boolean
  request<T = unknown>(method: string, params?: Record<string, unknown>, options?: TransportCallOptions): Promise<T>
}
const READ_TIMEOUT_MS = 10_000

function identity(request: SessionMessageSearchIdentity) {
  return {
    key: request.key,
    ...(request.sessionId !== undefined ? { session_id: request.sessionId } : {}),
    ...(request.sessionEpoch !== undefined ? { session_epoch: request.sessionEpoch } : {}),
  }
}

function contractFailure(): SessionMessageSearchError {
  return new SessionMessageSearchError('contract', 'Session message search returned inconsistent data.')
}

function checkIdentity(
  request: SessionMessageSearchIdentity,
  result: { session_id: string; session_epoch: number },
) {
  if ((request.sessionId !== undefined && result.session_id !== request.sessionId)
    || (request.sessionEpoch !== undefined && result.session_epoch !== request.sessionEpoch)) {
    throw new SessionMessageSearchError('stale', 'Session identity changed during search.')
  }
}

/** Generated validation and transport policy stay inside this Gateway boundary. */
export function createV4SessionMessageSearch(rpc: SearchTransport): SessionMessageSearch {
  async function read(method: string, params: Record<string, unknown>, request: SessionMessageSearchIdentity) {
    if (request.signal?.aborted) throw new DOMException('Search was cancelled.', 'AbortError')
    if (!rpc.supports(SESSIONS_MESSAGES_SEARCH_METHOD) || !rpc.supports(method)) {
      throw new SessionMessageSearchError('unsupported', 'Session message search is unavailable on this Gateway.')
    }
    const generation = rpc.generation
    const options: TransportCallOptions = {
      expectedGeneration: generation,
      timeoutMs: READ_TIMEOUT_MS,
      timeoutAction: 'reject',
      abortAction: 'reject',
      recoveryClass: 'safe-read',
      ...(request.signal ? { signal: request.signal, cancelOnAbort: true } : {}),
    }
    try {
      const result = await rpc.request(method, params, options)
      if (request.signal?.aborted) throw new DOMException('Search was cancelled.', 'AbortError')
      if (rpc.generation !== generation) {
        throw new SessionMessageSearchError('stale', 'Gateway connection changed during search.')
      }
      return result
    } catch (error) {
      if (request.signal?.aborted) throw new DOMException('Search was cancelled.', 'AbortError')
      if (error instanceof SessionMessageSearchError
        || (error instanceof Error && error.name === 'AbortError')) throw error
      if (rpc.generation !== generation) {
        throw new SessionMessageSearchError('stale', 'Gateway connection changed during search.')
      }
      const { code, retryAfterMs } = readTransportFailure(error)
      if (code === 'STORAGE_BUSY') {
        throw new SessionMessageSearchError('busy', 'Session message search is busy.', retryAfterMs)
      }
      if (code === 'HISTORY_STALE' || code === 'REBASE_REQUIRED') {
        throw new SessionMessageSearchError('stale', 'Session history changed during search.')
      }
      if (code === 'METHOD_NOT_FOUND' || code === 'UNKNOWN_METHOD' || code === 'UNSUPPORTED') {
        throw new SessionMessageSearchError('unsupported', 'Session message search is unavailable on this Gateway.')
      }
      throw new SessionMessageSearchError('unavailable', 'Session message search is temporarily unavailable.')
    }
  }

  return {
    async search(request) {
      const params: SearchParams = {
        ...identity(request),
        query: request.query,
        ...(request.cursor !== undefined ? { cursor: request.cursor } : {}),
        limit: request.limit ?? 50,
      }
      if (!validateSearchParams(params)) throw new TypeError('Invalid session message search request.')
      const raw = await read(SESSIONS_MESSAGES_SEARCH_METHOD, { ...params }, request)
      if (!validateSearchResult(raw)) throw contractFailure()
      const result = raw as SearchResult
      checkIdentity(request, result)
      if (result.query !== request.query || result.matches.length > params.limit!
        || result.complete !== (result.next_cursor === null)
        || (result.next_cursor !== null && result.next_cursor === request.cursor)) throw contractFailure()
      return Object.freeze({
        sessionId: result.session_id,
        sessionEpoch: result.session_epoch,
        query: result.query,
        matches: Object.freeze(result.matches.map(match => Object.freeze({
          messageId: match.message_id,
          entryCursor: match.entry_cursor,
          contentRevision: match.content_revision,
          offsetUtf16: match.offset_utf16,
          lengthUtf16: match.length_utf16,
          snippet: match.snippet,
        }))),
        nextCursor: result.next_cursor,
        complete: result.complete,
        coverageComplete: result.coverage_complete,
        scannedEntries: result.scanned_entries,
        unavailableEntries: result.unavailable_entries,
      })
    },
    async context(request) {
      const params: HistoryParams = {
        ...identity(request),
        anchor_message_id: request.messageId,
        direction: 'before',
        target_items: 20,
        target_bytes: 1_048_576,
        projection: { include_content_refs: true, include_tool_metadata: true, include_outcomes: true },
      }
      if (!validateHistoryParams(params)) throw new TypeError('Invalid session search context request.')
      const raw = await read(SESSIONS_HISTORY_PAGE_V2_METHOD, { ...params }, request)
      if (!validateHistoryResult(raw)) throw contractFailure()
      const result = raw as HistoryResult
      checkIdentity(request, result)
      if (!result.items.some(item => item.message_id === request.messageId)) {
        throw new SessionMessageSearchError('stale', 'The matching message is no longer available.')
      }
      return projectV2History(result, request.key, 'before')
    },
  }
}
