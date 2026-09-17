import { readTransportFailure, type TransportCallOptions as RpcCallOptions } from './transportTypes'
import {
  SESSIONS_PREVIEW_METHOD,
  type SessionsPreviewParams,
  type SessionsPreviewResult,
} from '@/contracts/generated/v4/sessionsPreview'
import {
  validateSessionsPreviewParams,
  validateSessionsPreviewResult,
} from '@/contracts/generated/v4/sessionsPreviewValidators.mjs'
import {
  SESSIONS_EXECUTION_LOG_READ_METHOD,
  type SessionsExecutionLogReadResult,
} from '@/contracts/generated/v4/sessionsExecutionLogRead'
import {
  validateSessionsExecutionLogReadParams,
  validateSessionsExecutionLogReadResult,
} from '@/contracts/generated/v4/sessionsExecutionLogReadValidators.mjs'
import {
  SessionInspectionContractError,
  SessionInspectionLogNotReadyError,
  type ExecutionLogPage,
  type SessionInspection,
  type SessionInspectionRequestOptions,
} from '@/modules/sessionInspection'
import type {
  SessionReadHistoryOptions,
  SessionReadPortHistoryRequest,
} from '@/modules/sessionReadLifecycle'
import {
  requestV4SessionHistory,
  type SessionHistoryV4Policy,
  type SessionHistoryV4Transport,
} from './sessionHistoryV4'

const DEFAULT_INSPECTION_PAGE_SIZE = 20
const MAX_INSPECTION_PAGE_SIZE = 200
const DEFAULT_PREVIEW_BUDGET_MS = 15_000

interface SessionInspectionV4Transport extends SessionHistoryV4Transport {
  request<T = unknown>(
    method: string,
    params?: Record<string, unknown>,
    options?: RpcCallOptions,
  ): Promise<T>
}

export interface SessionInspectionV4Options {
  readonly concurrentHistoryReads: () => boolean
  readonly now?: () => number
}

function contractError(message: string): SessionInspectionContractError {
  return new SessionInspectionContractError(
    `${message} violated its generated v4 Contract.`,
  )
}

function normalizedKey(value: string): string {
  const key = value.trim()
  if (!key) throw new TypeError('SessionInspection sessionKey must not be empty.')
  return key
}

function normalizedCursor(value: string): string {
  const cursor = value.trim()
  if (!cursor) throw new TypeError('SessionInspection.history.before cursor must not be empty.')
  return cursor
}

function normalizedLimit(value: number | undefined): number {
  if (value === undefined) return DEFAULT_INSPECTION_PAGE_SIZE
  if (!Number.isInteger(value) || value < 1 || value > MAX_INSPECTION_PAGE_SIZE) {
    throw new RangeError(
      `SessionInspection history limit must be between 1 and ${MAX_INSPECTION_PAGE_SIZE}.`,
    )
  }
  return value
}

function normalizedPositive(value: number | undefined, name: string): number | undefined {
  if (value === undefined) return undefined
  if (!Number.isFinite(value) || value <= 0) {
    throw new RangeError(`${name} must be a positive finite number.`)
  }
  return value
}

function requestSignal(value: AbortSignal | undefined): AbortSignal {
  return value ?? new AbortController().signal
}

function previewTimeoutMs(
  options: SessionInspectionRequestOptions,
  now: () => number,
): number {
  const budget = normalizedPositive(
    options.budgetMs,
    'SessionInspectionRequestOptions.budgetMs',
  ) ?? DEFAULT_PREVIEW_BUDGET_MS
  const deadline = normalizedPositive(
    options.deadlineAt,
    'SessionInspectionRequestOptions.deadlineAt',
  )
  return Math.max(1, Math.min(budget, deadline === undefined ? budget : deadline - now()))
}

/** Generated v4 Adapter for bounded, non-live session inspection. */
export function createV4SessionInspection(
  rpc: SessionInspectionV4Transport,
  options: SessionInspectionV4Options,
): SessionInspection {
  const now = options.now ?? Date.now
  const historyPolicy: SessionHistoryV4Policy = {
    concurrentHistoryReads: options.concurrentHistoryReads,
    now,
  }

  async function preview(
    sessionKey: string,
    requestOptions: SessionInspectionRequestOptions = {},
  ) {
    const key = normalizedKey(sessionKey)
    const params: SessionsPreviewParams = { keys: [key] }
    if (!validateSessionsPreviewParams(params)) {
      throw contractError(`${SESSIONS_PREVIEW_METHOD} params`)
    }
    const raw = await rpc.request(
      SESSIONS_PREVIEW_METHOD,
      params,
      {
        signal: requestSignal(requestOptions.signal),
        timeoutMs: previewTimeoutMs(requestOptions, now),
        timeoutAction: 'reject',
        abortAction: 'reject',
      },
    )
    if (!validateSessionsPreviewResult(raw)) {
      throw contractError(`${SESSIONS_PREVIEW_METHOD} result`)
    }
    const result = raw as SessionsPreviewResult
    const row = result.previews.find(item => item.key === key) ?? null
    return row
      ? Object.freeze({
          key: row.key,
          title: row.title,
          lastMessage: row.lastMessage,
          updatedAt: row.updatedAt,
        })
      : null
  }

  async function readExecutionLog(
    sessionKey: string,
    handle: string,
    offset: number,
    requestOptions: SessionInspectionRequestOptions = {},
  ): Promise<ExecutionLogPage> {
    if (!handle.trim() || !Number.isSafeInteger(offset) || offset < 0) {
      throw new TypeError('An execution log handle and non-negative offset are required.')
    }
    const params = { sessionKey: normalizedKey(sessionKey), handle, offset, limit: 12000 }
    if (!validateSessionsExecutionLogReadParams(params)) {
      throw new TypeError('Invalid execution log page request.')
    }
    let raw: SessionsExecutionLogReadResult
    try {
      raw = await rpc.request<SessionsExecutionLogReadResult>(
        SESSIONS_EXECUTION_LOG_READ_METHOD,
        params,
        {
          signal: requestSignal(requestOptions.signal),
          timeoutMs: previewTimeoutMs(requestOptions, now),
          timeoutAction: 'reject',
          abortAction: 'reject',
        },
      )
    } catch (error) {
      if (readTransportFailure(error).code === 'NOT_READY') {
        throw new SessionInspectionLogNotReadyError()
      }
      throw error
    }
    if (!validateSessionsExecutionLogReadResult(raw)) {
      throw contractError(SESSIONS_EXECUTION_LOG_READ_METHOD)
    }
    const returnedChars = Array.from(raw.content).length
    const end = raw.offset + returnedChars
    if (raw.handle !== handle || raw.offset !== Math.min(offset, raw.chars)
      || raw.returned_chars !== returnedChars || end > raw.chars
      || raw.next_offset !== (end < raw.chars ? end : null)
      || (end < raw.chars && returnedChars === 0)) {
      throw new SessionInspectionContractError('Inconsistent execution log page.')
    }
    return Object.freeze({
      handle,
      content: raw.content,
      offset: raw.offset,
      nextOffset: raw.next_offset,
      chars: raw.chars,
      complete: raw.complete,
    })
  }

  function historyRequest(
    sessionKey: string,
    direction: 'latest' | 'before',
    cursor: string | null,
    historyOptions: SessionReadHistoryOptions = {},
  ) {
    const key = normalizedKey(sessionKey)
    const limit = normalizedLimit(historyOptions.limit)
    const budgetMs = normalizedPositive(
      historyOptions.budgetMs,
      'SessionReadHistoryOptions.budgetMs',
    )
    const deadlineAt = normalizedPositive(
      historyOptions.deadlineAt,
      'SessionReadHistoryOptions.deadlineAt',
    )
    const signal = requestSignal(historyOptions.signal)
    const request: SessionReadPortHistoryRequest = direction === 'latest'
      ? { direction, limit, signal, budgetMs, deadlineAt }
      : {
          direction,
          cursor: cursor ?? '',
          limit,
          signal,
          budgetMs,
          deadlineAt,
        }
    return requestV4SessionHistory(rpc, key, request, {
      includeSummaries: false,
      policy: historyPolicy,
      contractError,
    })
  }

  return Object.freeze({
    preview,
    readExecutionLog,
    history: Object.freeze({
      latest: (sessionKey: string, historyOptions?: SessionReadHistoryOptions) =>
        historyRequest(sessionKey, 'latest', null, historyOptions),
      before: (
        sessionKey: string,
        cursor: string,
        historyOptions?: SessionReadHistoryOptions,
      ) => historyRequest(sessionKey, 'before', normalizedCursor(cursor), historyOptions),
    }),
  })
}
