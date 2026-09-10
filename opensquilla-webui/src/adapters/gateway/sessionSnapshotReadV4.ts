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
import { SessionReadContractError } from '@/modules/sessionReadLifecycle'
import type { TransportCallOptions } from './transportTypes'

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
  acknowledgeDelivery?(receipt: SnapshotDeliveryReceipt): Promise<void> | void
  resumeFlow?(receipt: SnapshotInstalledReceipt): Promise<void> | void
}

export interface StagedSessionSnapshot {
  // Private adapter data, never exported through a domain Module.
  value: {
    key: string
    task_id: string | null
    stream_generation: string
    current_stream_seq: number
    events: Array<{ event: string, payload: Record<string, unknown> }>
    [key: string]: unknown
  }
  sessionId: string | null
  sessionEpoch: number | null
  confirmInstalled(): Promise<void>
}

const MAX_BYTES = 25 * 1024 * 1024
const SEGMENT_BYTES = 192 * 1024
let revision = 0

/** A transfer owns one bounded byte array; no partial projection escapes it. */
export async function readV4SessionSnapshot(
  rpc: SnapshotReader,
  key: string,
  signal: AbortSignal,
  generation: number,
  onSent?: (generation: number) => void,
): Promise<StagedSessionSnapshot> {
  const syncRevision = `${generation}-${Date.now()}-${++revision}`
  let first: SessionsMessagesSnapshotReadResult | null = null
  let staging: Uint8Array | null = null
  let written = 0
  function assertCurrent() {
    if (signal.aborted || rpc.generation !== generation) {
      throw new DOMException('Snapshot read superseded.', 'AbortError')
    }
  }
  function invalid(): never {
    throw new SessionReadContractError('Invalid or inconsistent session snapshot transfer.')
  }
  for (let index = 0; index < (first?.segment_count ?? 1); index++) {
    assertCurrent()
    const params = {
      key, sync_revision: syncRevision,
      ...(first ? { snapshot_id: first.snapshot_id, segment_index: index } : {}),
    }
    if (!validateSessionsMessagesSnapshotReadParams(params)) invalid()
    const raw = await rpc.request(SESSIONS_MESSAGES_SNAPSHOT_READ_METHOD, params, {
      signal, expectedGeneration: generation, timeoutMs: 15_000,
      timeoutAction: 'reject', abortAction: 'reject',
      ...(index === 0 && onSent ? { onSent } : {}),
    })
    if (!validateSessionsMessagesSnapshotReadResult(raw)) invalid()
    const segment = raw as SessionsMessagesSnapshotReadResult
    if (segment.key !== key || segment.sync_revision !== syncRevision || segment.segment_index !== index
      || segment.byte_length < 1 || segment.byte_length > MAX_BYTES
      || segment.segment_count < 1 || segment.segment_count > Math.ceil(MAX_BYTES / SEGMENT_BYTES)
      || segment.segment_count !== Math.ceil(segment.byte_length / SEGMENT_BYTES)
      || segment.data.length > Math.ceil(SEGMENT_BYTES / 3) * 4) invalid()
    if (first) {
      for (const field of ['snapshot_id', 'segment_count', 'byte_length', 'stream_generation',
        'current_stream_seq', 'task_id', 'session_id', 'session_epoch'] as const) {
        if (segment[field] !== first[field]) invalid()
      }
    } else {
      first = segment
      staging = new Uint8Array(segment.byte_length)
    }
    let binary: string
    try { binary = atob(segment.data) } catch { invalid() }
    const expectedBytes = Math.min(SEGMENT_BYTES, segment.byte_length - written)
    if (binary!.length !== expectedBytes || written + binary!.length > staging!.length) invalid()
    for (let offset = 0; offset < binary!.length; offset++) staging![written + offset] = binary!.charCodeAt(offset)
    written += binary!.length
    // This acknowledges bounded byte ownership, not semantic installation.
    const delivery = (segment as SessionsMessagesSnapshotReadResult & { delivery?: SnapshotDeliveryReceipt }).delivery
    // An abort can race a successful RPC result. Discard its validated bounded
    // bytes on this connection, releasing recovery credit without installing
    // them. A replacement connection must never ACK the former epoch.
    if (delivery && rpc.generation === generation) await rpc.acknowledgeDelivery?.(delivery)
    assertCurrent()
  }
  if (!first || !staging || written !== first.byte_length) invalid()
  let parsed: unknown
  try { parsed = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(staging!)) } catch { invalid() }
  if (!validateSessionsMessagesSnapshotResult(parsed)) invalid()
  const value = parsed as SessionsMessagesSnapshotResult
  if (value.key !== key || value.stream_generation !== first!.stream_generation
    || value.current_stream_seq !== first!.current_stream_seq || value.task_id !== first!.task_id) invalid()
  const receipt: SnapshotInstalledReceipt = {
    key, snapshot_id: first!.snapshot_id, sync_revision: syncRevision,
    stream_generation: first!.stream_generation, stream_seq: first!.current_stream_seq,
  }
  let installed = false
  return {
    value, sessionId: first!.session_id, sessionEpoch: first!.session_epoch,
    async confirmInstalled() {
      assertCurrent()
      if (installed) return
      await rpc.resumeFlow?.(receipt)
      installed = true
    },
  }
}
