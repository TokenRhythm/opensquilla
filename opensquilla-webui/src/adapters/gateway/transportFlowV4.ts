import {
  TRANSPORT_FLOW_UPDATE_METHOD,
  type TransportFlowUpdateParams,
  type TransportFlowUpdateResult,
} from '@/contracts/generated/v4/transportFlowUpdate'
import {
  type SessionFlowUpdateV2Params,
  type SessionFlowUpdateV2Result,
} from '@/contracts/generated/v4/transportSessionFlowV2'
import { validateSessionFlowUpdateV2Params, validateSessionFlowUpdateV2Result } from '@/contracts/generated/v4/transportSessionFlowV2Validators.mjs'
import {
  TRANSPORT_SESSION_FLOW_V2_CAPABILITY,
  TRANSPORT_SESSION_FLOW_V2_METHOD,
  TRANSPORT_SESSION_FLOW_V2_SCHEMA_CAPABILITY,
} from '@/contracts/transportFlowCapabilities'
import type { TransportFlowDirtyPayload } from '@/contracts/generated/v4/transportFlowDirty'
import {
  validateTransportFlowUpdateParams,
  validateTransportFlowUpdateResult,
} from '@/contracts/generated/v4/transportFlowUpdateValidators.mjs'
import { validateTransportFlowDirtyPayload } from '@/contracts/generated/v4/transportFlowDirtyValidators.mjs'
import type {
  TransportCallOptions, TransportConsumptionHandler, TransportDeliveryReceipt,
  TransportEventHandler, TransportInstalledReceipt, TransportLaneRetireReceipt,
  TransportRecoveryResult,
} from './transportTypes'

interface FlowSource {
  readonly connectionGeneration: number
  request<T = unknown>(method: string, params?: Record<string, unknown>, options?: TransportCallOptions): Promise<T>
  on(event: string, handler: TransportEventHandler): () => void
  enableConsumptionFlow(): void
  consumeEvent: (event: string, ...args: Parameters<TransportConsumptionHandler>) => Promise<'applied' | 'dirty'>
  recoverGap(detail: unknown): Promise<TransportRecoveryResult>
  supportsRecovery?(): boolean
  failProtocol?(generation: number): void
}

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : null
}

function sessionKey(payload: unknown): string | null {
  const data = record(payload)
  const value = data?.session_key ?? data?.sessionKey ?? data?.key
  return typeof value === 'string' && value.length > 0 && value.length <= 4096 ? value : null
}

// Keep every retired epoch that the Gateway can legally retain. Evicting an
// epoch while its physical frames may still be in flight lets a late frame
// look like a new lane and can release the replacement ledger incorrectly.
const MAX_LANE_EPOCH_HISTORY = 16

/** One per app transport. Receipt completion means domain ownership, not paint. */
export class TransportFlowV4 {
  private epoch: string | null = null
  private generation = -1
  private revision = 0
  private ack = 0
  private sentAck = 0
  private pending = new Set<number>()
  private completed: [number, number][] = []
  private unowned = new Map<number, { receipt: TransportDeliveryReceipt; key: string | null }>()
  private staged = new Map<number, {
    promise: Promise<void>; resolve: () => void; reject: (error: Error) => void
  }>()
  private observations = new Map<number, ReturnType<typeof setTimeout>>()
  private dirty = new Map<string, number>()
  private resumes = new Map<string, {
    receipt: TransportInstalledReceipt; promise: Promise<void>
    resolve: () => void; reject: (error: Error) => void
  }>()
  private timer: ReturnType<typeof setTimeout> | null = null
  private recoveryTimer: ReturnType<typeof setTimeout> | null = null
  private inFlight = false
  private preferDirty = false
  private recovery: Promise<boolean> | null = null
  private recoveryKeys = new Set<string>()
  private recoveryJobs = new Map<string, Promise<boolean>>()
  // These are existing unowned receipts, not a permanent session blacklist.
  // A later verified snapshot can cover them; terminal failure alone cannot.
  private suspendedRecoveries = new Map<string, { version: string; receipts: TransportDeliveryReceipt[] }>()
  private invalidations = new Map<string, number>()
  private globalInvalidation = 0
  private consumedCursors = new Map<string, { generation: string; sequence: number }>()
  private consumedWaiters = new Map<string, Set<() => void>>()
  private consumers = new Map<number, { key: string | null; work: Promise<void> }>()
  private recoveryGlobal = false
  private sessionFlowV2 = false
  private isolateSessionFlowUpdates = false
  private laneAck = new Map<string, number>()
  private laneSentAck = new Map<string, number>()
  private laneRetired = new Map<string, { finalId: number }>()
  private pendingRetires = new Map<string, { receipt: TransportLaneRetireReceipt; promise: Promise<void>; resolve: () => void; reject: (error: Error) => void }>()
  private laneEpochByKey = new Map<string, string>()
  private laneEpochHistory = new Map<string, Set<string>>()
  private laneByDelivery = new Map<number, { epoch: string; key: string; complete: boolean }>()
  private subscriptions: (() => void)[] = []

  constructor(private readonly source: FlowSource) {
    this.subscriptions.push(
      source.on('_hello', (hello: unknown) => this.start(record(hello)?.policy)),
      source.on('_state', (state: unknown) => { if (state !== 'connected') this.reset() }),
      source.on('*', (event: unknown, payload: unknown, meta: unknown) => {
        if (typeof event === 'string') this.receive(event, payload, record(meta) ?? {})
      }),
    )
    source.enableConsumptionFlow()
  }

  get enabled(): boolean { return this.epoch !== null }

  /** Bounded, on-demand counters only: never payloads, identities or history. */
  get diagnostics() {
    return Object.freeze({
      enabled: this.enabled,
      pendingFrames: this.pending.size,
      ackDeliveryId: this.ack,
      acknowledgedDeliveryId: this.sentAck,
      stagedFrames: this.staged.size,
      pendingInstallations: this.resumes.size,
      unownedFrames: this.unowned.size,
      queuedRecoveryKeys: this.recoveryKeys.size,
      recoveryInFlight: this.recoveryJobs.size > 0 || this.recovery !== null,
      controlInFlight: this.inFlight,
      observedConsumers: this.observations.size,
      completedRanges: this.completed.length,
      retainedLaneDeliveries: this.laneByDelivery.size,
      dirtyKeyCount: this.dirty.size,
    })
  }

  private start(policy: unknown): void {
    this.reset()
    const flow = record(record(policy)?.transport_flow)
    if (!flow || typeof flow.delivery_epoch !== 'string' || !flow.delivery_epoch
      || flow.delivery_epoch.length > 128 || flow.window_frames !== 128
      || flow.window_bytes !== 4 * 1024 * 1024) return
    this.epoch = flow.delivery_epoch
    this.generation = this.source.connectionGeneration
    this.sessionFlowV2 = flow.capability === TRANSPORT_SESSION_FLOW_V2_CAPABILITY
      || flow.capability === TRANSPORT_SESSION_FLOW_V2_SCHEMA_CAPABILITY
  }

  reset(): void {
    this.revision++
    this.epoch = null
    this.generation = -1
    this.ack = this.sentAck = 0
    this.pending.clear()
    this.completed = []
    this.unowned.clear()
    for (const timer of this.observations.values()) clearTimeout(timer)
    this.observations.clear()
    for (const waiter of this.staged.values()) waiter.reject(new Error('Connection changed'))
    this.staged.clear()
    this.dirty.clear()
    for (const waiter of this.resumes.values()) waiter.reject(new Error('Connection changed'))
    this.resumes.clear()
    if (this.timer !== null) clearTimeout(this.timer)
    if (this.recoveryTimer !== null) clearTimeout(this.recoveryTimer)
    this.timer = null
    this.recoveryTimer = null
    this.inFlight = false
    this.preferDirty = false
    this.recovery = null
    this.recoveryKeys.clear()
    this.recoveryJobs.clear()
    this.suspendedRecoveries.clear()
    this.invalidations.clear()
    this.globalInvalidation = 0
    this.consumers.clear()
    this.consumedCursors.clear()
    this.consumedWaiters.clear()
    this.recoveryGlobal = false
    this.sessionFlowV2 = false
    this.isolateSessionFlowUpdates = false
    this.laneAck.clear()
    this.laneSentAck.clear()
    this.laneRetired.clear()
    for (const waiter of this.pendingRetires.values()) waiter.reject(new Error('Connection changed'))
    this.pendingRetires.clear()
    this.laneEpochByKey.clear()
    this.laneEpochHistory.clear()
    this.laneByDelivery.clear()
  }

  close(): void {
    this.reset()
    for (const unsubscribe of this.subscriptions.splice(0)) unsubscribe()
  }

  private current(revision: number): boolean {
    return revision === this.revision && this.enabled
      && this.source.connectionGeneration === this.generation
  }

  private receipt(value: unknown): TransportDeliveryReceipt | null {
    const data = record(value)
    if (!this.enabled || data?.delivery_epoch !== this.epoch
      || !Number.isSafeInteger(data.delivery_id) || (data.delivery_id as number) <= 0) return null
    return data as unknown as TransportDeliveryReceipt
  }

  receive(event: string, payload: unknown, meta: Record<string, unknown>): void {
    if (!this.enabled) return
    if (event === 'transport.flow.dirty') {
      const notice = payload as TransportFlowDirtyPayload
      if (validateTransportFlowDirtyPayload(notice) && notice.delivery_epoch === this.epoch) {
        this.requireRecovery(notice.dirty_keys, notice.global_dirty)
        // An explicitly invalidated reservation carries no business event.
        // Owning its dirty notification is sufficient to retire that receipt.
        const invalidated = this.receipt(meta.flow)
        if (invalidated) {
          if (!notice.global_dirty && notice.dirty_keys.length === 0) {
            void this.acknowledgeDelivery(invalidated).catch(() => {})
          } else this.markComplete(invalidated)
        }
      }
      return
    }
    const receipt = this.receipt(meta.flow)
    if (!receipt || receipt.delivery_id <= this.ack || this.pending.has(receipt.delivery_id)
      || this.completed.some(([start, end]) => start <= receipt.delivery_id && receipt.delivery_id <= end)) return
    // Completed receipts behind a lane's consumer hole still own credit.
    // Count them as well as pending consumers before retaining another frame.
    const highestComplete = this.completed[this.completed.length - 1]?.[1] ?? this.ack
    if (this.pending.size >= 256 || this.laneByDelivery.size >= 256
      || receipt.delivery_id > highestComplete + 512) {
      this.requireRecovery([], true)
      return
    }
    if (this.sessionFlowV2) {
      const lane = record(meta.session_flow_v2)
      const connectionEpoch = lane?.connection_epoch
      const subscriptionEpoch = lane?.subscription_epoch
      const laneDelivery = lane?.delivery_id
      if (connectionEpoch !== this.epoch || subscriptionEpoch === undefined
        || typeof subscriptionEpoch !== 'string' || !subscriptionEpoch
        || laneDelivery !== receipt.delivery_id) {
        this.requireRecovery([], true)
        return
      }
      const key = sessionKey(payload)
      if (!key) {
        this.requireRecovery([], true)
        return
      }
      const retired = this.laneRetired.get(subscriptionEpoch)
      if (retired) {
        // Unsubscribe owns disposal of this lane. Frames already in the
        // FIFO writer may arrive before its confirmation, but cannot reopen
        // the old consumer or generate an ordinary consumption ACK.
        if (receipt.delivery_id <= retired.finalId) this.markComplete(receipt)
        else this.requireRecovery([key], false)
        return
      }
      const knownEpochs = this.laneEpochHistory.get(key) ?? new Set<string>()
      const priorEpoch = this.laneEpochByKey.get(key)
      if (priorEpoch && priorEpoch !== subscriptionEpoch && knownEpochs.has(subscriptionEpoch)) {
        // A frame from an already retired subscription arrived after its key
        // was replaced. Keep it out of the replacement lane ledger.
        this.requireRecovery([], true)
        return
      }
      if (knownEpochs.size >= MAX_LANE_EPOCH_HISTORY && !knownEpochs.has(subscriptionEpoch)) {
        // The server rejects a seventeenth retained epoch. Do not evict an
        // older fence on the client: a late physical frame for that epoch
        // must remain stale rather than being reclassified as a new lane.
        this.requireRecovery([], true)
        return
      }
      knownEpochs.add(subscriptionEpoch)
      this.laneEpochHistory.set(key, knownEpochs)
      this.laneEpochByKey.set(key, subscriptionEpoch)
      this.laneByDelivery.set(receipt.delivery_id, { epoch: subscriptionEpoch, key, complete: false })
    }
    const revision = this.revision
    this.pending.add(receipt.delivery_id)
    let recoveryRequired = false
    this.observations.set(receipt.delivery_id, setTimeout(() => {
      this.observations.delete(receipt.delivery_id)
      if (!this.current(revision) || !this.pending.has(receipt.delivery_id)) return
      // 100ms is only an observation. No event is dropped or ACKed here.
      // Recovery must fence late domain writes and explicitly own correctness
      // before this credit can be released.
      recoveryRequired = true
      const key = sessionKey(payload)
      this.unowned.set(receipt.delivery_id, { receipt, key })
      this.requireRecovery(key ? [key] : [], !key)
    }, 100))
    const work = this.source.consumeEvent(event, payload, meta).then(result => {
      if (!this.current(revision) || recoveryRequired || !this.pending.has(receipt.delivery_id)) return
      if (result === 'dirty') {
        const key = sessionKey(payload)
        if (key) this.dirty.set(key, (this.dirty.get(key) ?? 0) + 1)
        // The consumer has already registered/fenced its recovery ownership.
        this.requireRecovery(key ? [key] : [], !key)
      }
      if (result === 'applied') {
        const data = record(payload)
        const key = sessionKey(payload)
        if (key && typeof data?.stream_generation === 'string' && Number.isSafeInteger(data.stream_seq)) {
          const previous = this.consumedCursors.get(key)
          if (this.consumedCursors.size >= 256 && !previous) this.consumedCursors.delete(this.consumedCursors.keys().next().value!)
          this.consumedCursors.set(key, {
            generation: data.stream_generation,
            sequence: previous?.generation === data.stream_generation
              ? Math.max(previous.sequence, data.stream_seq as number) : data.stream_seq as number,
          })
          for (const notify of this.consumedWaiters.get(key) ?? []) notify()
          this.consumedWaiters.delete(key)
        }
      }
      this.markComplete(receipt)
    }, () => {
      if (!this.current(revision) || recoveryRequired || !this.pending.has(receipt.delivery_id)) return
      const timer = this.observations.get(receipt.delivery_id)
      if (timer !== undefined) clearTimeout(timer)
      this.observations.delete(receipt.delivery_id)
      // An absent/failed consumer did NOT take ownership. Only successful
      // authoritative recovery can release this delivery's credit.
      const key = sessionKey(payload)
      this.unowned.set(receipt.delivery_id, { receipt, key })
      this.requireRecovery(key ? [key] : [], !key)
    }).finally(() => {
      if (this.consumers.get(receipt.delivery_id)?.work === work) {
        this.consumers.delete(receipt.delivery_id)
      }
    })
    this.consumers.set(receipt.delivery_id, { key: sessionKey(payload), work })
  }

  private markComplete(value: TransportDeliveryReceipt): void {
    const receipt = this.receipt(value)
    if (!receipt || receipt.delivery_id <= this.ack) return
    this.pending.delete(receipt.delivery_id)
    if (this.sessionFlowV2) {
      const lane = this.laneByDelivery.get(receipt.delivery_id)
      if (lane && !this.laneRetired.has(lane.epoch)) {
        lane.complete = true
        // The wire ACK is cumulative within this subscription, not a maximum
        // of arbitrary completions. Preserve receive order until the first
        // unfinished owner; deliveries belonging to other lanes do not block.
        for (const [id, queued] of this.laneByDelivery) {
          if (queued.epoch !== lane.epoch) continue
          if (!queued.complete) break
          this.laneAck.set(lane.epoch, id)
          this.laneByDelivery.delete(id)
        }
      } else {
        this.laneByDelivery.delete(receipt.delivery_id)
      }
    }
    const unowned = this.unowned.get(receipt.delivery_id)
    this.unowned.delete(receipt.delivery_id)
    if (unowned?.key) {
      const suspended = this.suspendedRecoveries.get(unowned.key)
      if (suspended) {
        suspended.receipts = suspended.receipts.filter(item => item.delivery_id !== receipt.delivery_id)
        if (!suspended.receipts.length) this.suspendedRecoveries.delete(unowned.key)
      }
    }
    const timer = this.observations.get(receipt.delivery_id)
    if (timer !== undefined) clearTimeout(timer)
    this.observations.delete(receipt.delivery_id)
    const ranges: [number, number][] = [...this.completed, [receipt.delivery_id, receipt.delivery_id]]
      .sort((a, b) => a[0] - b[0]) as [number, number][]
    this.completed = []
    for (const range of ranges) {
      const last = this.completed[this.completed.length - 1]
      if (last && range[0] <= last[1] + 1) last[1] = Math.max(last[1], range[1])
      else this.completed.push(range)
    }
    while (this.completed[0] && this.completed[0][0] <= this.ack + 1) {
      this.ack = Math.max(this.ack, this.completed.shift()![1])
    }
    this.schedule(this.sessionFlowV2 ? 0 : (this.ack - this.sentAck >= 32 ? 0 : 50))
  }

  /** Staging frees the one recovery slot even when earlier ordinary ACKs wait. */
  acknowledgeDelivery(value: TransportDeliveryReceipt): Promise<void> {
    const receipt = this.receipt(value)
    if (!receipt) return Promise.resolve()
    const previous = this.staged.get(receipt.delivery_id)
    if (previous) return previous.promise
    if (this.staged.size >= 16) return Promise.reject(new Error('Too many pending snapshot staging ACKs'))
    this.markComplete(receipt)
    let resolve!: () => void
    let reject!: (error: Error) => void
    const promise = new Promise<void>((yes, no) => { resolve = yes; reject = no })
    this.staged.set(receipt.delivery_id, { promise, resolve, reject })
    // One wire ACK at a time, including a late abandoned read queued behind
    // an ACK whose reply was lost. Never lose that valid discard responsibility.
    void this.flush()
    return promise
  }

  /** Confirm retirement by epoch so a replacement with the same key stays independent. */
  retireLane(receipt: TransportLaneRetireReceipt): Promise<void> {
    if (!this.enabled || !this.sessionFlowV2 || receipt.connection_epoch !== this.epoch) {
      return Promise.reject(new Error('Session lane is not current'))
    }
    const previous = this.pendingRetires.get(receipt.subscription_epoch)
    if (previous && JSON.stringify(previous.receipt) === JSON.stringify(receipt)) return previous.promise
    if (!previous && this.pendingRetires.size >= 16) {
      return Promise.reject(new Error('Too many pending lane retire confirmations'))
    }
    previous?.reject(new Error('Lane retire confirmation superseded'))
    let resolve!: () => void
    let reject!: (error: Error) => void
    const promise = new Promise<void>((yes, no) => { resolve = yes; reject = no })
    this.pendingRetires.set(receipt.subscription_epoch, { receipt, promise, resolve, reject })
    this.laneRetired.set(receipt.subscription_epoch, {
      finalId: receipt.final_published_id,
    })
    this.laneAck.delete(receipt.subscription_epoch)
    this.laneSentAck.delete(receipt.subscription_epoch)
    for (const [id, lane] of this.laneByDelivery) {
      if (lane.epoch === receipt.subscription_epoch && id <= receipt.final_published_id) {
        this.markComplete({ delivery_epoch: this.epoch!, delivery_id: id })
        this.laneByDelivery.delete(id)
      }
    }
    void this.flush()
    return promise
  }

  private forgetRetiredLane(epoch: string): void {
    this.laneRetired.delete(epoch)
    this.laneAck.delete(epoch)
    this.laneSentAck.delete(epoch)
    for (const [id, lane] of this.laneByDelivery) {
      if (lane.epoch === epoch) this.laneByDelivery.delete(id)
    }
    // The retire reply follows this lane's physical frames on the same FIFO
    // socket. A single-record stale reply also ends this old responsibility;
    // it cannot invalidate an unrelated lane. Remaining local consumers are
    // fenced by pending membership, not permanent epoch history.
    for (const [key, epochs] of this.laneEpochHistory) {
      epochs.delete(epoch)
      if (!epochs.size) this.laneEpochHistory.delete(key)
      if (this.laneEpochByKey.get(key) === epoch) this.laneEpochByKey.delete(key)
    }
  }

  async resumeFlow(receipt: TransportInstalledReceipt): Promise<void> {
    if (!this.enabled) return
    const previous = this.resumes.get(receipt.key)
    if (previous && JSON.stringify(previous.receipt) === JSON.stringify(receipt)) {
      return previous.promise
    }
    if (!previous && this.resumes.size >= 128) throw new Error('Too many pending snapshot installations')
    previous?.reject(new Error('Snapshot installation superseded'))
    let resolve!: () => void
    let reject!: (error: Error) => void
    const promise = new Promise<void>((yes, no) => { resolve = yes; reject = no })
    this.resumes.set(receipt.key, { receipt, promise, resolve, reject })
    void this.flush()
    // A queued or lost control reply is not a server-accepted installation.
    // Only the matching successful update below resolves this waiter.
    return promise
  }

  recoveryVersion(key: string): string {
    return `${this.generation}:${this.globalInvalidation}:${this.invalidations.get(key) ?? 0}`
  }

  snapshotInstalled(key: string, version: string): void {
    const suspended = this.suspendedRecoveries.get(key)
    // Pausing advances this key's invalidation. A current install therefore
    // began after the pause, including after a bounded-map global rollover.
    if (!suspended || this.recoveryVersion(key) !== version) return
    this.suspendedRecoveries.delete(key)
    for (const receipt of suspended.receipts) this.markComplete(receipt)
  }

  private suspendRecovery(key: string, receipts: TransportDeliveryReceipt[], version: string): void {
    const outstanding = receipts.filter(receipt => this.unowned.get(receipt.delivery_id)?.receipt === receipt)
    if (!key || !outstanding.length || this.recoveryVersion(key) !== version) return
    if (this.suspendedRecoveries.get(key)?.version === version) return
    // Fence a snapshot that began before this terminal recovery completed.
    this.invalidations.set(key, (this.invalidations.get(key) ?? 0) + 1)
    this.suspendedRecoveries.set(key, { version: this.recoveryVersion(key), receipts: outstanding })
  }

  async waitForConsumption(key: string, cursor?: { streamGeneration: string; fromSeq: number; toSeq: number }): Promise<void> {
    const revision = this.revision
    // The FIFO proof follows its tail events. Capture the consumers already
    // dispatched at that point, never an unrelated session's pending consumer.
    await Promise.all([...this.consumers.entries()]
      .filter(([id, item]) => item.key === key && !this.unowned.has(id))
      .map(([, item]) => item.work))
    if (cursor && cursor.toSeq > cursor.fromSeq) {
      // Recovery replay is deliberately delivered in bounded batches. The
      // first batch ACK may cause the Gateway to enqueue the next one, so a
      // single cursor sample would incorrectly report SNAPSHOT_STALE. Keep
      // waiting for the authoritative final waterline while the connection
      // remains current; the caller's outer budget bounds this loop.
      const knownCursor = this.consumedCursors.get(key)
      if ((!knownCursor || knownCursor.generation !== cursor.streamGeneration)
        && !this.resumes.has(key)
        && ![...this.consumers.values()].some(item => item.key === key)) {
        throw Object.assign(new Error('Snapshot replay tail was not consumed.'), { code: 'SNAPSHOT_STALE' })
      }
      for (let attempt = 0; attempt < 140; attempt++) {
        const consumed = this.consumedCursors.get(key)
        if (consumed?.generation === cursor.streamGeneration && consumed.sequence >= cursor.toSeq) return
        if (!this.current(revision)) throw Object.assign(new Error('Snapshot replay tail was not consumed.'), { code: 'SNAPSHOT_STALE' })
        let notify!: () => void
        const notified = new Promise<void>(resolve => { notify = resolve })
        const waiters = this.consumedWaiters.get(key) ?? new Set<() => void>()
        waiters.add(notify)
        this.consumedWaiters.set(key, waiters)
        await Promise.race([notified, new Promise<void>(resolve => setTimeout(resolve, 50))])
        waiters.delete(notify)
        if (!waiters.size) this.consumedWaiters.delete(key)
      }
      throw Object.assign(new Error('Snapshot replay tail was not consumed.'), { code: 'SNAPSHOT_STALE' })
    }
  }

  private requireRecovery(keys: string[], global: boolean, invalidate = true): Promise<boolean> | null {
    if (!this.enabled) return null
    if (invalidate) {
      if (global) this.globalInvalidation++
      for (const key of keys) {
        if (this.invalidations.size >= 256 && !this.invalidations.has(key)) {
          this.globalInvalidation++
          this.invalidations.clear()
        }
        this.invalidations.set(key, (this.invalidations.get(key) ?? 0) + 1)
      }
    }
    if (!this.source.supportsRecovery?.()) return this.legacyRequireRecovery(keys, global)
    for (const key of keys) {
      if (this.recoveryKeys.size < 256 || this.recoveryKeys.has(key)) this.recoveryKeys.add(key)
      else this.recoveryGlobal = true
    }
    this.recoveryGlobal ||= global
    const revision = this.revision
    const launch = (key: string, scopeGlobal: boolean) => {
      const owned = [...this.unowned.values()].filter(entry => entry.key === key)
      const capturedVersion = this.recoveryVersion(key)
      const work = Promise.resolve().then(() => this.source.recoverGap({
        reason: 'transport_flow_dirty', keys: scopeGlobal ? [] : [key], global: scopeGlobal,
      })).catch(() => false).then(ok => {
        if (!this.current(revision)) return false
        if (ok === true && capturedVersion === this.recoveryVersion(key)) {
          this.suspendedRecoveries.delete(key)
          if (!scopeGlobal) for (const entry of owned) this.markComplete(entry.receipt)
          return true
        }
        if (typeof ok === 'object' && ok.retryable === false && capturedVersion === this.recoveryVersion(key)) {
          this.suspendRecovery(key, owned.map(entry => entry.receipt), capturedVersion)
          return false
        }
        if (scopeGlobal) this.recoveryGlobal = true
        else this.recoveryKeys.add(key)
        return false
      }).finally(() => {
        if (!this.current(revision) || this.recoveryJobs.get(key) !== work) return
        this.recoveryJobs.delete(key)
        if (this.recoveryGlobal || this.recoveryKeys.size) this.scheduleRecovery()
      })
      this.recoveryJobs.set(key, work)
    }
    if (this.recoveryGlobal && this.recoveryJobs.size < 2 && !this.recoveryJobs.has('')) {
      this.recoveryGlobal = false
      // Tagged receipts still need an exact session proof, even after a global
      // owner accepts responsibility for its active read admissions.
      for (const entry of this.unowned.values()) if (entry.key) this.recoveryKeys.add(entry.key)
      launch('', true)
    }
    for (const key of this.recoveryKeys) {
      if (this.recoveryJobs.size >= 2) break
      if (this.recoveryJobs.has(key)) continue
      this.recoveryKeys.delete(key)
      launch(key, false)
    }
    return this.recoveryJobs.size
      ? Promise.all(this.recoveryJobs.values()).then(results => results.every(Boolean)) : null
  }

  private legacyRequireRecovery(keys: string[], global: boolean): Promise<boolean> | null {
    if (!this.enabled) return null
    for (const key of keys) {
      if (this.recoveryKeys.size < 256 || this.recoveryKeys.has(key)) this.recoveryKeys.add(key)
      else this.recoveryGlobal = true
    }
    this.recoveryGlobal ||= global
    if (this.recovery) return this.recovery
    if (this.recoveryTimer !== null) return null
    const requestedKeys = new Set(this.recoveryKeys)
    const requestedGlobal = this.recoveryGlobal
    this.recoveryKeys.clear()
    this.recoveryGlobal = false
    // A global recovery expands the source's active read admissions. It must
    // also name every tagged receipt whose completion we intend to claim.
    if (requestedGlobal) {
      for (const entry of this.unowned.values()) if (entry.key !== null) requestedKeys.add(entry.key)
    }
    if (!requestedGlobal && requestedKeys.size === 0) return null
    const revision = this.revision
    const ownedByThisRecovery = [...this.unowned.values()]
      .filter(entry => entry.key !== null && requestedKeys.has(entry.key))
    const complete = (coveredKeys: ReadonlySet<string>) => {
      if (!this.current(revision)) return
      // Untagged unknown events have no proven Session owner. A generic true
      // result cannot confer that authority, nor cover receipts joining later.
      for (const entry of ownedByThisRecovery) {
        if (entry.key !== null && coveredKeys.has(entry.key)) this.markComplete(entry.receipt)
      }
    }
    const versions = new Map([...requestedKeys].map(key => [key, this.recoveryVersion(key)]))
    const suspend = (key: string) => this.suspendRecovery(key,
      ownedByThisRecovery.filter(entry => entry.key === key).map(entry => entry.receipt), versions.get(key)!)
    const recover = async (scopeKeys: string[], scopeGlobal: boolean): Promise<TransportRecoveryResult> => {
      if (!this.current(revision)) return false
      try {
        return await this.source.recoverGap({ reason: 'transport_flow_dirty', keys: scopeKeys, global: scopeGlobal })
      } catch { return false }
    }
    const work = Promise.resolve().then(async () => {
      const scopeKeys = [...requestedKeys]
      const ok = await recover(scopeKeys, requestedGlobal)
      if (!this.current(revision)) return false
      if (ok === true) {
        complete(requestedKeys)
        return true
      }
      // A missing B owner must not prevent already-authoritative A from
      // releasing its credit. Keep failed retry intents scoped to their keys.
      const failedKeys: string[] = []
      if (scopeKeys.length > 1 || (requestedGlobal && scopeKeys.length > 0)) {
        for (const key of scopeKeys) {
          if (!this.current(revision)) return false
          const result = await recover([key], false)
          if (result === true) complete(new Set([key]))
          else if (typeof result === 'object' && result.retryable === false) suspend(key)
          else failedKeys.push(key)
        }
      } else if (typeof ok === 'object' && ok.retryable === false) {
        for (const key of scopeKeys) suspend(key)
      } else failedKeys.push(...scopeKeys)
      if (this.current(revision)) {
        for (const key of failedKeys) {
          if (this.recoveryKeys.size < 256 || this.recoveryKeys.has(key)) this.recoveryKeys.add(key)
          else this.recoveryGlobal = true
        }
        // A genuinely global failure remains global; a keyed failure never
        // becomes a global retry merely because it joined another read.
        this.recoveryGlobal ||= requestedGlobal && ok === false
      }
      return false
    }).finally(() => {
      if (!this.current(revision) || this.recovery !== work) return
      this.recovery = null
      if (this.recoveryGlobal || this.recoveryKeys.size > 0) this.scheduleRecovery()
    })
    this.recovery = work
    return work
  }

  private scheduleRecovery(): void {
    if (this.recoveryTimer !== null) return
    const revision = this.revision
    // Keep automatic read recovery alive without a tight snapshot/BUSY loop.
    this.recoveryTimer = setTimeout(() => {
      this.recoveryTimer = null
      if (this.current(revision)) this.requireRecovery([], false, false)
    }, 1000)
  }

  private schedule(delay: number): void {
    if (!this.enabled || this.timer !== null) return
    this.timer = setTimeout(() => { this.timer = null; void this.flush() }, delay)
  }

  private async flushSessionFlowV2(): Promise<void> {
    if (!this.enabled || this.inFlight || !this.sessionFlowV2) return
    let consumed = [...this.laneAck.entries()]
      .filter(([lane, id]) => !this.laneRetired.has(lane) && id > (this.laneSentAck.get(lane) ?? 0))
      .slice(0, 16)
      .map(([subscription_epoch, through_delivery_id]) => ({
        subscription_epoch, through_delivery_id,
      }))
    let staged = [...this.staged.keys()].slice(0, 16).map(delivery_id => ({
      delivery_epoch: this.epoch!, delivery_id,
    }))
    let discarded = [...this.pendingRetires.values()].slice(0, 16).map(({ receipt }) => ({
      subscription_epoch: receipt.subscription_epoch,
      retire_token: receipt.retire_token,
      final_published_id: receipt.final_published_id,
    }))
    // A batch-level stale error does not identify the failed record. Retry
    // individual responsibilities only on this exceptional path; never
    // abandon another lane's valid ACK, staging or retire confirmation.
    if (this.isolateSessionFlowUpdates) {
      if (discarded.length) { discarded = discarded.slice(0, 1); staged = []; consumed = [] }
      else if (staged.length) { staged = staged.slice(0, 1); consumed = [] }
      else consumed = consumed.slice(0, 1)
    }
    if (!consumed.length && !staged.length && !discarded.length) return
    const revision = this.revision
    const params: SessionFlowUpdateV2Params = {
      connection_epoch: this.epoch!,
      ...(consumed.length
        ? { consumed: consumed as SessionFlowUpdateV2Params['consumed'] }
        : {}),
      ...(staged.length
        ? { staged_recovery: staged as SessionFlowUpdateV2Params['staged_recovery'] }
        : {}),
      ...(discarded.length
        ? { discarded_lanes: discarded as SessionFlowUpdateV2Params['discarded_lanes'] }
        : {}),
    }
    if (!validateSessionFlowUpdateV2Params(params)) return
    this.inFlight = true
    try {
      const result = await this.source.request<SessionFlowUpdateV2Result>(
        TRANSPORT_SESSION_FLOW_V2_METHOD, { ...params }, {
          expectedGeneration: this.generation, timeoutMs: 7_000,
          timeoutAction: 'reject', abortAction: 'reject',
        },
      )
      if (!this.current(revision) || !validateSessionFlowUpdateV2Result(result)
        || result.connection_epoch !== this.epoch) throw new Error('Invalid session flow v2 reply')
      for (const item of consumed) {
        const acknowledged = result.consumed.find(
          value => value.subscription_epoch === item.subscription_epoch,
        )
        if (acknowledged && acknowledged.through_delivery_id >= item.through_delivery_id
          && !this.laneRetired.has(item.subscription_epoch)) {
          this.laneSentAck.set(item.subscription_epoch, acknowledged.through_delivery_id)
        }
      }
      for (const item of staged) {
        const acknowledged = result.staged_recovery.some(
          value => value.delivery_epoch === item.delivery_epoch && value.delivery_id >= item.delivery_id,
        )
        if (acknowledged) {
          this.staged.get(item.delivery_id)?.resolve()
          this.staged.delete(item.delivery_id)
        }
      }
      for (const item of discarded) {
        const acknowledged = result.discarded_lanes.some(value =>
          value.subscription_epoch === item.subscription_epoch
          && value.retire_token === item.retire_token
          && value.final_published_id >= item.final_published_id)
        if (acknowledged) {
          this.pendingRetires.get(item.subscription_epoch)?.resolve()
          this.pendingRetires.delete(item.subscription_epoch)
          this.forgetRetiredLane(item.subscription_epoch)
        }
      }
    } catch (error) {
      if (this.current(revision)) {
        const failure = record(error)
        const code = failure?.code ?? record(failure?.data)?.code
        if (code === 'LANE_RETIRED' || code === 'FLOW_STALE' || code === 'NOT_FOUND') {
          if (consumed.length + staged.length + discarded.length > 1) {
            this.isolateSessionFlowUpdates = true
            return
          }
          // Only a single-record failure identifies which responsibility is
          // stale. Other queued records remain owned and will be retried.
          for (const item of consumed) {
            this.laneAck.delete(item.subscription_epoch)
            this.laneSentAck.delete(item.subscription_epoch)
          }
          for (const item of staged) {
            this.staged.get(item.delivery_id)?.reject(
              error instanceof Error ? error : new Error(String(code)),
            )
            this.staged.delete(item.delivery_id)
          }
          for (const item of discarded) {
            this.pendingRetires.get(item.subscription_epoch)?.reject(
              error instanceof Error ? error : new Error(String(code)),
            )
            this.pendingRetires.delete(item.subscription_epoch)
            this.forgetRetiredLane(item.subscription_epoch)
          }
        } else this.schedule(1000)
      }
    } finally {
      if (this.current(revision)) {
        this.inFlight = false
        if ([...this.laneAck.entries()].some(([lane, id]) => id > (this.laneSentAck.get(lane) ?? 0))
          || this.staged.size || this.pendingRetires.size) this.schedule(this.staged.size ? 0 : 50)
        else {
          this.isolateSessionFlowUpdates = false
          if (this.dirty.size || this.resumes.size) this.schedule(0)
        }
      }
    }
  }

  private async flush(): Promise<void> {
    if (!this.enabled || this.inFlight) return
    const hasSessionLaneWork = this.sessionFlowV2 && (
      [...this.laneAck.entries()].some(([lane, id]) => id > (this.laneSentAck.get(lane) ?? 0))
      || this.staged.size > 0 || this.pendingRetires.size > 0
    )
    if (hasSessionLaneWork) {
      await this.flushSessionFlowV2()
      return
    }
    // v2 owns ordinary credit release per subscription lane.  Dirty and
    // snapshot-installation records still use the v1 recovery fields for
    // compatibility, but never replay the global cumulative ACK.
    if (this.sessionFlowV2 && !this.dirty.size && !this.resumes.size) return
    if (!this.sessionFlowV2 && this.ack === this.sentAck
      && !this.dirty.size && !this.resumes.size && !this.staged.size) return
    const revision = this.revision
    const modern = this.source.supportsRecovery?.() === true
    const hasCredit = (!this.sessionFlowV2 && this.ack !== this.sentAck) || this.staged.size > 0
    // Give pending invalidation a turn after one credit batch even while
    // tokens keep arriving. Its stale ACK cannot consume any new credit.
    const dirtyOnly = modern && this.dirty.size > 0 && (!hasCredit || this.preferDirty)
    const keys = modern && !dirtyOnly ? [] : [...this.dirty.keys()].slice(0, 128)
    const dirtyVersions = new Map(keys.map(key => [key, this.dirty.get(key)]))
    // The server validates and applies one snapshot identity atomically.
    // Keep the per-key queue, but never batch multiple resume authorities.
    const resumes = dirtyOnly ? [] : [...this.resumes.values()].slice(0, 1).map(waiter => waiter.receipt)
    const staged = dirtyOnly ? [] : [...this.staged.keys()].slice(0, 1)
    const params: TransportFlowUpdateParams = {
      delivery_epoch: this.epoch!,
      ack_delivery_id: dirtyOnly || this.sessionFlowV2 ? this.sentAck : this.ack,
      ...(keys.length ? { dirty_keys: keys } : {}),
      ...(resumes.length ? { resume: [resumes[0]] as [TransportInstalledReceipt] } : {}),
      ...(staged.length ? { staged_delivery_ids: [staged[0]] as [number] } : {}),
    }
    if (!validateTransportFlowUpdateParams(params)) return
    this.inFlight = true
    if (modern) this.preferDirty = !dirtyOnly
    try {
      const result = await this.source.request<TransportFlowUpdateResult>(TRANSPORT_FLOW_UPDATE_METHOD, { ...params }, {
        expectedGeneration: this.generation, timeoutMs: modern ? 7_000 : 10_000,
        timeoutAction: 'reject', abortAction: 'reject',
      })
      if (!this.current(revision)) return
      if (!validateTransportFlowUpdateResult(result) || result.delivery_epoch !== this.epoch
        || result.ack_delivery_id > this.ack || result.ack_delivery_id < params.ack_delivery_id
        || (!modern && result.ack_delivery_id !== params.ack_delivery_id)) throw new Error('Invalid flow reply')
      this.sentAck = Math.max(this.sentAck, result.ack_delivery_id)
      for (const id of staged) {
        this.staged.get(id)?.resolve()
        this.staged.delete(id)
      }
      for (const key of keys) if (this.dirty.get(key) === dirtyVersions.get(key)) this.dirty.delete(key)
      for (const receipt of resumes) {
        const waiter = this.resumes.get(receipt.key)
        if (waiter?.receipt !== receipt) continue
        this.resumes.delete(receipt.key)
        if (result.global_dirty || result.dirty_keys.includes(receipt.key)) {
          waiter.reject(new Error('Snapshot installation requires reconciliation'))
        } else waiter.resolve()
      }
      if (!modern && (result.dirty_keys.length || result.global_dirty)) {
        this.requireRecovery(result.dirty_keys, result.global_dirty)
      }
    } catch (error) {
      const failure = record(error)
      const code = failure?.code ?? record(failure?.data)?.code
      if (modern && (code === 'INVALID_REQUEST' || code === 'UNAUTHORIZED' || code === 'NOT_FOUND')) {
        for (const key of keys) if (this.dirty.get(key) === dirtyVersions.get(key)) this.dirty.delete(key)
        for (const id of staged) {
          this.staged.get(id)?.reject(error instanceof Error ? error : new Error(String(code)))
          this.staged.delete(id)
        }
        // Invalid credit is a connection protocol failure; do not hide it in
        // an unbounded retry loop. Future receipts may still be independent.
        if (!keys.length) {
          this.source.failProtocol?.(this.generation)
          this.reset()
        }
        return
      }
      // Credit updates are idempotent; retry only this connection-local RPC.
      // Never replay a business mutation or recycle the shared socket here.
      if (this.current(revision)) this.schedule(1000)
    } finally {
      if (this.current(revision)) {
        this.inFlight = false
        if (this.ack !== this.sentAck || this.dirty.size || this.resumes.size || this.staged.size) {
          this.schedule(this.staged.size ? 0 : 50)
        }
      }
    }
  }
}
