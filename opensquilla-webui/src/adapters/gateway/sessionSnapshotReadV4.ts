import {
  SESSIONS_MESSAGES_SNAPSHOT_READ_METHOD,
  type SessionsMessagesSnapshotReadResult,
} from '@/contracts/generated/v4/sessionsMessagesSnapshotRead'
import {
  validateSessionsMessagesSnapshotReadParams,
  validateSessionsMessagesSnapshotReadResult,
} from '@/contracts/generated/v4/sessionsMessagesSnapshotReadValidators.mjs'
import type { SessionsMessagesSnapshotResult } from '@/contracts/generated/v4/sessionsMessagesSnapshot'
import { validateSessionsMessagesSnapshotResult } from '@/contracts/generated/v4/sessionsMessagesSnapshotValidators.mjs'
import { SESSIONS_MESSAGES_RESUME_METHOD, type SessionsMessagesResumeResult } from '@/contracts/generated/v4/sessionsMessagesResume'
import { validateSessionsMessagesResumeParams, validateSessionsMessagesResumeResult } from '@/contracts/generated/v4/sessionsMessagesResumeValidators.mjs'
import { SESSIONS_MESSAGES_SNAPSHOT_RELEASE_METHOD } from '@/contracts/generated/v4/sessionsMessagesSnapshotRelease'
import { validateSessionsMessagesSnapshotReleaseParams, validateSessionsMessagesSnapshotReleaseResult } from '@/contracts/generated/v4/sessionsMessagesSnapshotReleaseValidators.mjs'
import { SessionReadContractError, SessionReadFailure } from '@/modules/sessionReadLifecycle'
import { readTransportFailure, type TransportCallOptions } from './transportTypes'

export interface SnapshotDeliveryReceipt {
  delivery_epoch: string
  delivery_id: number
}

export interface SnapshotInstalledReceipt {
  key: string
  snapshot_id: string
  sync_revision: string
  stream_generation: string
  stream_seq: number
}

interface SnapshotReader {
  request<T = unknown>(method: string, params?: Record<string, unknown>, options?: TransportCallOptions): Promise<T>
  readonly generation: number
  supports?(method: string): boolean
  acknowledgeDelivery?(receipt: SnapshotDeliveryReceipt): Promise<void> | void
  resumeFlow?(receipt: SnapshotInstalledReceipt): Promise<void> | void
  recoveryVersion?(key: string): string
  waitForConsumption?(key: string, cursor?: { streamGeneration: string; fromSeq: number; toSeq: number }): Promise<void>
  failProtocol?(generation: number): void
}

export interface StagedSessionSnapshot {
  value: {
    key: string; task_id: string | null; stream_generation: string; current_stream_seq: number
    events: Array<{ event: string; payload: Record<string, unknown> }>
    [key: string]: unknown
  }
  sessionId: string | null
  sessionEpoch: number | null
  confirmInstalled(): Promise<void>
  assertInstalledCurrent(): void
}

export interface SessionSnapshotTransfer {
  readonly retired: boolean
  readonly installed: boolean
  readonly budgetExhausted: boolean
  read(onSent?: (generation: number) => void): Promise<StagedSessionSnapshot>
  release(): void
}

const MAX_BYTES = 25 * 1024 * 1024
const SEGMENT_BYTES = 192 * 1024
const TRANSFER_MS = 120_000
let revision = 0

export function supportsSnapshotRecovery(rpc: Pick<SnapshotReader, 'supports'>): boolean {
  return rpc.supports?.(SESSIONS_MESSAGES_RESUME_METHOD) === true
    && rpc.supports?.(SESSIONS_MESSAGES_SNAPSHOT_RELEASE_METHOD) === true
}

/** Transfer state survives individual RPC timeouts; it never survives its lease. */
export function createV4SessionSnapshotTransfer(
  rpc: SnapshotReader, key: string, signal: AbortSignal, generation: number,
): SessionSnapshotTransfer {
  const syncRevision = `${generation}-${Date.now()}-${++revision}`
  const deadline = performance.now() + TRANSFER_MS
  const version = rpc.recoveryVersion?.(key)
  const modern = supportsSnapshotRecovery(rpc)
  const controller = new AbortController()
  let first: SessionsMessagesSnapshotReadResult | null = null
  let staging: Uint8Array | null = null
  let written = 0
  let index = 0
  let retired = false
  let installed = false
  let exhausted = false
  let pendingCredit: SnapshotDeliveryReceipt | null = null
  let pending: Promise<StagedSessionSnapshot> | null = null
  let staged: StagedSessionSnapshot | null = null
  let releaseStarted = false
  const expire = () => { exhausted = true; release() }
  const timer = setTimeout(expire, TRANSFER_MS)
  signal.addEventListener('abort', release, { once: true })

  function remaining(maximum: number): number {
    if (exhausted || performance.now() >= deadline) {
      expire()
      throw new SessionReadFailure('budget-exhausted', 'Snapshot recovery budget exhausted. Retry explicitly or use paginated history.', false)
    }
    return Math.max(1, Math.min(maximum, deadline - performance.now()))
  }
  function assertCurrent() {
    if (!installed) remaining(TRANSFER_MS)
    if (signal.aborted || retired || rpc.generation !== generation) {
      throw new DOMException('Snapshot read superseded.', 'AbortError')
    }
    if (version !== rpc.recoveryVersion?.(key)) {
      release()
      throw new SessionReadFailure('busy', 'Snapshot invalidated by a newer delivery gap.', true)
    }
  }
  function invalid(): never {
    throw new SessionReadContractError('Invalid or inconsistent session snapshot transfer.')
  }
  function release() {
    if (retired) return
    retired = true
    controller.abort()
    clearTimeout(timer)
    signal.removeEventListener('abort', release)
    staging = null
    staged = null
    if (!modern || releaseStarted || rpc.generation !== generation) return
    releaseStarted = true
    const params = { key, sync_revision: syncRevision, ...(first ? { snapshot_id: first.snapshot_id } : {}) }
    if (!validateSessionsMessagesSnapshotReleaseParams(params)) {
      rpc.failProtocol?.(generation)
      return
    }
    // Connection-owned cleanup must outlive the aborted read. Retry one lost
    // reply on this generation; teardown/absolute server expiry bounds failure.
    void (async () => {
      for (let attempt = 0; attempt < 2 && rpc.generation === generation; attempt++) {
        try {
          const result = await rpc.request(SESSIONS_MESSAGES_SNAPSHOT_RELEASE_METHOD, params, {
            expectedGeneration: generation, timeoutMs: 7_000, timeoutAction: 'reject', abortAction: 'reject',
          })
          if (!validateSessionsMessagesSnapshotReleaseResult(result)) rpc.failProtocol?.(generation)
          return
        } catch { /* A second idempotent release may recover a lost response. */ }
      }
    })()
  }
  async function bounded<T>(work: Promise<T> | T, maximum = 7_000): Promise<T> {
    const timeout = remaining(maximum)
    return new Promise<T>((resolve, reject) => {
      const abort = () => finish(() => reject(new DOMException('Snapshot read superseded.', 'AbortError')))
      const timeoutId = setTimeout(() => finish(() => reject(
        new SessionReadFailure('timeout', 'Snapshot control confirmation timed out.', true),
      )), timeout)
      const finish = (settle: () => void) => {
        clearTimeout(timeoutId)
        controller.signal.removeEventListener('abort', abort)
        settle()
      }
      controller.signal.addEventListener('abort', abort, { once: true })
      if (controller.signal.aborted) abort()
      Promise.resolve(work).then(value => finish(() => resolve(value)), error => finish(() => reject(error)))
    })
  }
  async function confirmCredit() {
    const receipt = pendingCredit
    if (!receipt || rpc.generation !== generation) return
    await bounded(rpc.acknowledgeDelivery?.(receipt))
    if (pendingCredit === receipt) pendingCredit = null
  }
  async function requestOwned(method: string, params: Record<string, unknown>, options: TransportCallOptions): Promise<unknown> {
    try {
      return await rpc.request(method, params, options)
    } catch (error) {
      const code = readTransportFailure(error).code?.toUpperCase()
      // A rejected ownership/replay proof cannot become valid by retrying the
      // same frozen base. Timeouts and BUSY retain their resumable transfer.
      if (code === 'SNAPSHOT_STALE' || code === 'SNAPSHOT_EXPIRED') release()
      throw error
    }
  }
  async function read(onSent?: (generation: number) => void): Promise<StagedSessionSnapshot> {
    assertCurrent()
    if (staged) return staged
    await confirmCredit()
    while (index < (first?.segment_count ?? 1)) {
      assertCurrent()
      const params = {
        key, sync_revision: syncRevision,
        ...(first ? { snapshot_id: first.snapshot_id, segment_index: index } : {}),
      }
      if (!validateSessionsMessagesSnapshotReadParams(params)) invalid()
      const raw = await requestOwned(SESSIONS_MESSAGES_SNAPSHOT_READ_METHOD, params, {
        signal: controller.signal, expectedGeneration: generation, timeoutMs: remaining(15_000),
        cancelOnAbort: true, timeoutAction: 'reject', abortAction: 'reject',
        ...(onSent ? { onSent } : {}),
      })
      if (!validateSessionsMessagesSnapshotReadResult(raw)) {
        rpc.failProtocol?.(generation)
        release()
        invalid()
      }
      const segment = raw as SessionsMessagesSnapshotReadResult
      if (retired || signal.aborted || rpc.generation !== generation) {
        if (segment.delivery && rpc.generation === generation) {
          const discard = Promise.resolve(rpc.acknowledgeDelivery?.(segment.delivery))
          if (modern) void discard.catch(() => {})
          else await discard
        }
        assertCurrent()
      }
      // A valid bounded envelope owns discard responsibility even when its
      // semantic owner/index/data are wrong. An arbitrary delivery field does not.
      try {
        if (segment.key !== key || segment.sync_revision !== syncRevision || segment.segment_index !== index
          || segment.byte_length < 1 || segment.byte_length > MAX_BYTES
          || segment.segment_count !== Math.ceil(segment.byte_length / SEGMENT_BYTES)
          || segment.data.length > Math.ceil(SEGMENT_BYTES / 3) * 4) invalid()
        if (first) {
          for (const field of ['snapshot_id', 'segment_count', 'byte_length', 'stream_generation',
            'current_stream_seq', 'task_id', 'session_id', 'session_epoch'] as const) {
            if (segment[field] !== first[field]) invalid()
          }
        }
        let binary: string
        try { binary = atob(segment.data) } catch { invalid() }
        const expectedBytes = Math.min(SEGMENT_BYTES, segment.byte_length - written)
        if (binary!.length !== expectedBytes) invalid()
        if (!first) {
          first = segment
          staging = new Uint8Array(segment.byte_length)
        }
        if (staging && !retired && !signal.aborted && rpc.generation === generation) {
          if (written + binary!.length > staging.length) invalid()
          for (let offset = 0; offset < binary!.length; offset++) staging[written + offset] = binary!.charCodeAt(offset)
          written += binary!.length
          index++
        }
      } catch (error) {
        if (segment.delivery && rpc.generation === generation) {
          void Promise.resolve(rpc.acknowledgeDelivery?.(segment.delivery)).catch(() => {})
        }
        release()
        throw error
      }
      if (segment.delivery && rpc.generation === generation) {
        pendingCredit = segment.delivery
        // Credit belongs to the connection even when abort races this result.
        const confirmation = rpc.acknowledgeDelivery?.(segment.delivery)
        if (retired || signal.aborted) {
          await confirmation
          assertCurrent()
        }
        await bounded(confirmation)
        pendingCredit = null
      }
      assertCurrent()
    }
    if (!first || !staging || written !== first.byte_length) invalid()
    let parsed: unknown
    try { parsed = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(staging)) } catch { release(); invalid() }
    if (!validateSessionsMessagesSnapshotResult(parsed)) { release(); invalid() }
    const value = parsed as SessionsMessagesSnapshotResult
    if (value.key !== key || value.stream_generation !== first.stream_generation
      || value.current_stream_seq !== first.current_stream_seq || value.task_id !== first.task_id) { release(); invalid() }
    const receipt: SnapshotInstalledReceipt = {
      key, snapshot_id: first.snapshot_id, sync_revision: syncRevision,
      stream_generation: first.stream_generation, stream_seq: first.current_stream_seq,
    }
    const sessionId = first.session_id
    const sessionEpoch = first.session_epoch
    staged = {
      value, sessionId, sessionEpoch,
      assertInstalledCurrent: assertCurrent,
      async confirmInstalled() {
        assertCurrent()
        if (!installed) {
          let replayToSeq = receipt.stream_seq
          if (modern) {
            if (!validateSessionsMessagesResumeParams(receipt)) { release(); invalid() }
            const raw = await requestOwned(SESSIONS_MESSAGES_RESUME_METHOD, { ...receipt }, {
              signal: controller.signal, expectedGeneration: generation, timeoutMs: remaining(7_000),
              cancelOnAbort: true, timeoutAction: 'reject', abortAction: 'reject',
            })
            if (!validateSessionsMessagesResumeResult(raw)) { rpc.failProtocol?.(generation); release(); invalid() }
            const proof = raw as SessionsMessagesResumeResult
            if (Object.entries(receipt).some(([field, value]) => proof[field as keyof SessionsMessagesResumeResult] !== value)
              || proof.session_id !== sessionId || proof.session_epoch !== sessionEpoch
              || proof.replay_to_seq < receipt.stream_seq) { release(); invalid() }
            replayToSeq = proof.replay_to_seq
          } else await bounded(rpc.resumeFlow?.(receipt))
          assertCurrent()
          try {
            await bounded(rpc.waitForConsumption?.(key, {
              streamGeneration: receipt.stream_generation, fromSeq: receipt.stream_seq, toSeq: replayToSeq,
            }))
          } catch (error) {
            if ((error as { code?: string })?.code === 'SNAPSHOT_STALE') {
              release()
              throw new SessionReadFailure('busy', 'Snapshot replay tail is incomplete.', true)
            }
            throw error
          }
          assertCurrent()
          installed = true
          clearTimeout(timer)
          staging = null
        }
      },
    }
    return staged
  }
  if (signal.aborted) release()
  return {
    get budgetExhausted() { return exhausted },
    get retired() { return retired }, get installed() { return installed },
    release,
    read(onSent) {
      if (pending) return pending
      const work = read(onSent).catch(error => {
        if (exhausted) throw new SessionReadFailure('budget-exhausted', 'Snapshot recovery budget exhausted. Retry explicitly or use paginated history.', false)
        throw error
      })
      const observed = work.finally(() => { if (pending === observed) pending = null })
      pending = observed
      return observed
    },
  }
}

/** Compatibility entry point for standalone callers; leases retain the owner. */
export function readV4SessionSnapshot(
  rpc: SnapshotReader, key: string, signal: AbortSignal, generation: number,
  onSent?: (generation: number) => void,
): Promise<StagedSessionSnapshot> {
  return createV4SessionSnapshotTransfer(rpc, key, signal, generation).read(onSent)
}
