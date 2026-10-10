import type { TransportCallOptions } from './transportTypes'
import {
  projectHistoryMessage,
  projectHistoryCompaction,
  projectHistoryTurnOutcome,
} from './sessionHistoryV4'
import {
  SESSIONS_READ_OPEN_V2_METHOD,
  type Params as ReadOpenParams,
  type Result as ReadOpenResult,
} from '@/contracts/generated/v4/sessionsReadOpenV2'
import {
  SESSIONS_READ_STATE_V2_METHOD,
  type Result as ReadStateResult,
} from '@/contracts/generated/v4/sessionsReadStateV2'
import {
  SESSIONS_READ_INSTALL_V2_METHOD,
  type Result as ReadInstallResult,
} from '@/contracts/generated/v4/sessionsReadInstallV2'
import { SESSIONS_READ_CLOSE_V2_METHOD, type Result as ReadCloseResult } from '@/contracts/generated/v4/sessionsReadCloseV2'
import {
  SESSIONS_HISTORY_PAGE_V2_METHOD,
  type Params as HistoryV2Params,
  type Result as HistoryV2Result,
  type HistoryItem as HistoryV2Item,
} from '@/contracts/generated/v4/sessionsHistoryPageV2'
import {
  validateParams as validateSessionsReadOpenV2Params,
  validateResult as validateSessionsReadOpenV2Result,
} from '@/contracts/generated/v4/sessionsReadOpenV2Validators.mjs'
import {
  validateParams as validateSessionsReadStateV2Params,
  validateResult as validateSessionsReadStateV2Result,
} from '@/contracts/generated/v4/sessionsReadStateV2Validators.mjs'
import {
  validateParams as validateSessionsReadInstallV2Params,
  validateResult as validateSessionsReadInstallV2Result,
} from '@/contracts/generated/v4/sessionsReadInstallV2Validators.mjs'
import {
  validateParams as validateSessionsReadCloseV2Params,
  validateResult as validateSessionsReadCloseV2Result,
} from '@/contracts/generated/v4/sessionsReadCloseV2Validators.mjs'
import {
  validateParams as validateSessionsHistoryPageV2Params,
  validateResult as validateSessionsHistoryPageV2Result,
} from '@/contracts/generated/v4/sessionsHistoryPageV2Validators.mjs'
import type {
  SessionReadContentRef,
  SessionReadHistoryPage,
  SessionReadJsonObject,
  SessionReadMessage,
  SessionReadPortHistoryRequest,
} from '@/modules/sessionReadLifecycle'

const READ_TIMEOUT_MS = 7_000

type SessionReadV2Status =
  | 'staged'
  | 'base_applied'
  | 'catching_up'
  | 'installed'
  | 'rebase_required'
  | 'retired'

interface SessionReadV2Progress {
  status: SessionReadV2Status
  base_seq: number
  target_seq: number
  next_seq: number
  consumed_through_seq: number
  handoff_seq?: number | null
  proof_id?: string | null
}

interface SessionReadV2StateManifest {
  length: number
  sha256: string
  schema_version: number
  chunks: readonly {
    index: number
    offset: number
    byte_length: number
    sha256: string
  }[]
}

export interface SessionReadV2Open {
  lease_id: string
  recovery_id: string
  session_id: string
  session_epoch: number
  connection_epoch: string
  subscription_epoch: string
  state_revision: number
  base_stream_generation: string
  base_stream_seq: number
  state_manifest: SessionReadV2StateManifest
}

export interface SessionReadV2State {
  lease_id: string
  recovery_id: string
  session_id: string
  session_epoch: number
  state_revision: number
  status: SessionReadV2Status
  progress: SessionReadV2Progress
}

export interface SessionReadV2Install {
  lease_id: string
  recovery_id: string
  status: SessionReadV2Status
  progress: SessionReadV2Progress
}

export interface SessionReadV2Close {
  lease_id: string
  closed: true
  status?: SessionReadV2Status
}

export interface SessionReadV2Transport {
  request<T = unknown>(
    method: string,
    params?: Record<string, unknown>,
    options?: TransportCallOptions,
  ): Promise<T>
  readonly generation: number
  supports?(method: string): boolean
  waitForConsumption?(key: string, cursor: {
    streamGeneration: string
    fromSeq: number
    toSeq: number
  }): Promise<void>
}

export interface SessionReadV2Lease {
  readonly open: SessionReadV2Open
  readonly recoveryId: string
  readonly sessionId: string
  readonly sessionEpoch: number
  readonly baseStreamGeneration: string
  readonly baseStreamSeq: number
  state(): Promise<SessionReadV2State>
  install(params?: {
    baseApplied?: boolean
    consumedThroughSeq?: number
    ackThroughSeq?: number
  }): Promise<SessionReadV2Install>
  /** Wait for the event consumer, then prove and install the current tail. */
  installConsumed(): Promise<SessionReadV2Install>
  close(): Promise<SessionReadV2Close | null>
}

export function supportsSessionReadV2(rpc: Pick<SessionReadV2Transport, 'supports'>): boolean {
  return [
    SESSIONS_READ_OPEN_V2_METHOD,
    SESSIONS_READ_STATE_V2_METHOD,
    SESSIONS_READ_INSTALL_V2_METHOD,
    SESSIONS_READ_CLOSE_V2_METHOD,
    SESSIONS_HISTORY_PAGE_V2_METHOD,
  ].every(method => rpc.supports?.(method) === true)
}

function objectValue(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : null
}

function contractError(method: string): Error {
  return new Error(`${method} violated its generated v4 Contract.`)
}

function requireResult<T>(method: string, value: unknown, validate: (value: unknown) => boolean): T {
  if (!validate(value)) throw contractError(`${method} result`)
  return value as T
}

function callOptions(generation: number, signal?: AbortSignal): TransportCallOptions {
  return {
    expectedGeneration: generation,
    timeoutMs: READ_TIMEOUT_MS,
    timeoutAction: 'reject',
    abortAction: 'reject',
    recoveryClass: 'safe-read',
    ...(signal ? { signal, cancelOnAbort: true } : {}),
  }
}

function objectProjection(value: unknown): SessionReadJsonObject {
  const object = objectValue(value)
  if (!object) return Object.freeze({})
  return Object.freeze(Object.fromEntries(Object.entries(object).map(([key, child]) => [
    key,
    Array.isArray(child)
      ? Object.freeze(child.map(item => objectValue(item) ? objectProjection(item) : item))
      : objectValue(child) ? objectProjection(child) : child,
  ])))
}

function contentRef(item: HistoryV2Item, key: string): SessionReadContentRef | undefined {
  const handle = item.contents.find(value => value.availability === 'ready')
  if (!handle) return undefined
  const ref = handle.ref
  return Object.freeze({
    version: 1,
    sessionKey: key,
    sessionId: ref.session_id,
    messageId: item.message_id,
    byteLength: ref.byte_length,
    revision: ref.revision,
    ...(ref.view ? { view: ref.view } : {}),
    ...(ref.source ? { source: ref.source } : {}),
    sha256: ref.sha256 ?? undefined,
  })
}

function projectV2HistoryMessage(item: HistoryV2Item, key: string, index: number): SessionReadMessage {
  // The canonical display projection is shared with chat.history. A storage
  // preview is not an assistant answer and must never be JSON-unwrapped here.
  const message = projectHistoryMessage(item.message, index)
  const ref = item.content_availability === 'ready'
    ? message.contentRef ?? contentRef(item, key)
    : undefined
  return Object.freeze({
    ...message,
    id: item.item_id || item.message_id || `history:${index}`,
    messageId: item.message_id || null,
    transcriptId: message.transcriptId || item.item_id || null,
    contentRef: ref,
    previewComplete: item.preview_complete,
    contentRevision: item.source_revision,
    contentAvailability: item.content_availability,
    contentUnavailableReason: message.contentUnavailableReason
      ?? (item.content_availability === 'preparing' ? 'content_metadata_pending'
        : item.content_availability === 'unavailable' ? 'content_reference_unavailable' : undefined),
    additional: objectProjection({
      ...message.additional,
      order: item.order,
      contents: item.contents,
    }),
  })
}

function projectV2History(
  result: HistoryV2Result,
  key: string,
  direction: 'latest' | 'before' | 'after',
): SessionReadHistoryPage {
  const hasMore = direction === 'after' ? result.has_more_after : result.has_more_before
  return Object.freeze({
    messages: Object.freeze(result.items.map((item, index) => projectV2HistoryMessage(item, key, index))),
    hasMore,
    oldestCursor: result.before_cursor,
    newestCursor: result.after_cursor,
    scope: result.history_scope === 'latest_window' ? 'latestWindow' : result.history_scope,
    loadedCount: result.items.length,
    pageSize: result.items.length,
    canonicalAvailable: result.canonical_available,
    canonicalComplete: result.canonical_complete,
    compactionSummaries: Object.freeze(result.compaction_summaries.map(projectHistoryCompaction)),
    turnOutcomes: Object.freeze(result.turn_outcomes.map(projectHistoryTurnOutcome)),
    additional: Object.freeze({
      projectionRevision: result.projection_revision,
      sessionId: result.session_id,
      sessionEpoch: result.session_epoch,
      completeForRequestedWindow: result.complete_for_requested_window,
    }),
  })
}

export async function requestV2SessionHistory(
  rpc: SessionReadV2Transport,
  key: string,
  request: SessionReadPortHistoryRequest,
  expectedGeneration: number,
  onSent?: (generation: number) => void,
): Promise<SessionReadHistoryPage> {
  const direction: HistoryV2Params['direction'] = request.direction === 'after' ? 'after' : 'before'
  const params: HistoryV2Params = {
    key,
    direction,
    cursor: request.direction === 'latest' ? null : request.cursor,
    target_items: Math.min(256, Math.max(1, request.limit)),
    target_bytes: 1_048_576,
    projection: { include_content_refs: true, include_tool_metadata: true, include_outcomes: true },
  }
  if (!validateSessionsHistoryPageV2Params(params)) throw contractError(`${SESSIONS_HISTORY_PAGE_V2_METHOD} params`)
  const raw = await rpc.request(
    SESSIONS_HISTORY_PAGE_V2_METHOD,
    { ...params },
    { ...callOptions(expectedGeneration, request.signal), ...(onSent ? { onSent } : {}) },
  )
  const result = requireResult<HistoryV2Result>(
    SESSIONS_HISTORY_PAGE_V2_METHOD, raw, validateSessionsHistoryPageV2Result,
  )
  return projectV2History(result, key, request.direction)
}

export async function openV2SessionRead(
  rpc: SessionReadV2Transport,
  key: string,
  expectedGeneration = rpc.generation,
  signal?: AbortSignal,
): Promise<SessionReadV2Lease> {
  const params: ReadOpenParams = { key }
  if (!validateSessionsReadOpenV2Params(params)) throw contractError(`${SESSIONS_READ_OPEN_V2_METHOD} params`)
  const raw = await rpc.request(
    SESSIONS_READ_OPEN_V2_METHOD,
    { ...params },
    callOptions(expectedGeneration, signal),
  )
  const open = requireResult<ReadOpenResult>(SESSIONS_READ_OPEN_V2_METHOD, raw, validateSessionsReadOpenV2Result)
  if (rpc.generation !== expectedGeneration) throw new Error('Session read v2 generation changed during open.')
  const state = {
    leaseId: open.lease_id,
    recoveryId: open.recovery_id,
    closed: false,
    lastConsumed: open.base_stream_seq,
    lastAck: open.base_stream_seq,
  }
  const leaseParams = (extra: Record<string, unknown> = {}) => ({
    key,
    lease_id: state.leaseId,
    recovery_id: state.recoveryId,
    ...extra,
  })
  async function stateRead(): Promise<SessionReadV2State> {
    if (state.closed) throw new Error('Session read v2 lease is closed.')
    const params = leaseParams()
    if (!validateSessionsReadStateV2Params(params)) throw contractError(`${SESSIONS_READ_STATE_V2_METHOD} params`)
    const result = requireResult<ReadStateResult>(SESSIONS_READ_STATE_V2_METHOD, await rpc.request(
      SESSIONS_READ_STATE_V2_METHOD, params, callOptions(expectedGeneration, signal),
    ), validateSessionsReadStateV2Result)
    if (result.lease_id !== state.leaseId || result.recovery_id !== state.recoveryId) {
      throw new Error('Session read v2 lease identity changed.')
    }
    state.lastConsumed = Math.max(state.lastConsumed, result.progress.consumed_through_seq)
    return result
  }
  async function install(extra: {
    baseApplied?: boolean
    consumedThroughSeq?: number
    ackThroughSeq?: number
  } = {}): Promise<SessionReadV2Install> {
    if (state.closed) throw new Error('Session read v2 lease is closed.')
    const consumed = extra.consumedThroughSeq
    const ack = extra.ackThroughSeq
    if (consumed !== undefined && (!Number.isSafeInteger(consumed) || consumed < state.lastConsumed)) {
      throw new Error('Session read v2 consumption watermark regressed.')
    }
    if (ack !== undefined && (!Number.isSafeInteger(ack) || ack < state.lastAck)) {
      throw new Error('Session read v2 ACK watermark regressed.')
    }
    const params = leaseParams({
      ...(extra.baseApplied === undefined ? {} : { base_applied: extra.baseApplied }),
      ...(consumed === undefined ? {} : { consumed_through_seq: consumed }),
      ...(ack === undefined ? {} : { ack_through_seq: ack }),
    })
    if (!validateSessionsReadInstallV2Params(params)) throw contractError(`${SESSIONS_READ_INSTALL_V2_METHOD} params`)
    const result = requireResult<ReadInstallResult>(SESSIONS_READ_INSTALL_V2_METHOD, await rpc.request(
      SESSIONS_READ_INSTALL_V2_METHOD, params, callOptions(expectedGeneration, signal),
    ), validateSessionsReadInstallV2Result)
    if (result.lease_id !== state.leaseId || result.recovery_id !== state.recoveryId) {
      throw new Error('Session read v2 install identity changed.')
    }
    state.lastConsumed = Math.max(state.lastConsumed, result.progress.consumed_through_seq)
    state.lastAck = Math.max(state.lastAck, result.progress.consumed_through_seq)
    return result
  }
  return Object.freeze({
    open,
    recoveryId: open.recovery_id,
    sessionId: open.session_id,
    sessionEpoch: open.session_epoch,
    baseStreamGeneration: open.base_stream_generation,
    baseStreamSeq: open.base_stream_seq,
    state: stateRead,
    install,
    async installConsumed() {
      const current = await stateRead()
      if (current.status === 'rebase_required' || current.status === 'retired') {
        throw Object.assign(new Error('Session read v2 requires a fresh recovery base.'), {
          code: current.status === 'rebase_required' ? 'REBASE_REQUIRED' : 'READ_STALE',
        })
      }
      const target = current.progress.target_seq
      if (target > state.lastConsumed) {
        if (!rpc.waitForConsumption) throw new Error('Session read v2 requires a consumption fence.')
        await rpc.waitForConsumption(key, {
          streamGeneration: open.base_stream_generation,
          fromSeq: state.lastConsumed,
          toSeq: target,
        })
      }
      return install({ consumedThroughSeq: target, ackThroughSeq: target })
    },
    async close() {
      if (state.closed) return null
      state.closed = true
      const params = leaseParams()
      if (!validateSessionsReadCloseV2Params(params)) throw contractError(`${SESSIONS_READ_CLOSE_V2_METHOD} params`)
      const result = requireResult<ReadCloseResult>(SESSIONS_READ_CLOSE_V2_METHOD, await rpc.request(
        SESSIONS_READ_CLOSE_V2_METHOD, params, callOptions(expectedGeneration),
      ), validateSessionsReadCloseV2Result)
      if (result.lease_id !== state.leaseId || result.closed !== true) throw new Error('Session read v2 close identity changed.')
      return result
    },
  })
}
