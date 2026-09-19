import {
  TRANSPORT_FLOW_UPDATE_METHOD,
  type TransportFlowUpdateParams,
  type TransportFlowUpdateResult,
} from '@/contracts/generated/v4/transportFlowUpdate'
import type { TransportFlowDirtyPayload } from '@/contracts/generated/v4/transportFlowDirty'
import {
  validateTransportFlowUpdateParams,
  validateTransportFlowUpdateResult,
} from '@/contracts/generated/v4/transportFlowUpdateValidators.mjs'
import { validateTransportFlowDirtyPayload } from '@/contracts/generated/v4/transportFlowDirtyValidators.mjs'
import type {
  TransportCallOptions, TransportConsumptionHandler, TransportDeliveryReceipt,
  TransportEventHandler, TransportInstalledReceipt,
} from './transportTypes'

interface FlowSource {
  readonly connectionGeneration: number
  request<T = unknown>(method: string, params?: Record<string, unknown>, options?: TransportCallOptions): Promise<T>
  on(event: string, handler: TransportEventHandler): () => void
  enableConsumptionFlow(): void
  consumeEvent: (event: string, ...args: Parameters<TransportConsumptionHandler>) => Promise<'applied' | 'dirty'>
  recoverGap(detail: unknown): Promise<boolean>
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
  private invalidations = new Map<string, number>()
  private globalInvalidation = 0
  private consumedCursors = new Map<string, { generation: string; sequence: number }>()
  private consumers = new Map<number, { key: string | null; work: Promise<void> }>()
  private recoveryGlobal = false
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
    this.invalidations.clear()
    this.globalInvalidation = 0
    this.consumers.clear()
    this.consumedCursors.clear()
    this.recoveryGlobal = false
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
    // A valid peer has at most 128 ordinary deliveries and one recovery piece.
    // Do not allocate an unbounded sparse ACK map for malformed peers.
    const highestComplete = this.completed[this.completed.length - 1]?.[1] ?? this.ack
    if (this.pending.size >= 256 || receipt.delivery_id > highestComplete + 512) {
      this.requireRecovery([], true)
      return
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
      if (!this.current(revision) || recoveryRequired) return
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
        }
      }
      this.markComplete(receipt)
    }, () => {
      if (!this.current(revision) || recoveryRequired) return
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
    this.unowned.delete(receipt.delivery_id)
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
    this.schedule(this.ack - this.sentAck >= 32 ? 0 : 50)
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

  async waitForConsumption(key: string, cursor?: { streamGeneration: string; fromSeq: number; toSeq: number }): Promise<void> {
    // The FIFO proof follows its tail events. Capture the consumers already
    // dispatched at that point, never an unrelated session's pending consumer.
    await Promise.all([...this.consumers.entries()]
      .filter(([id, item]) => item.key === key && !this.unowned.has(id))
      .map(([, item]) => item.work))
    if (cursor && cursor.toSeq > cursor.fromSeq) {
      const consumed = this.consumedCursors.get(key)
      if (!consumed || consumed.generation !== cursor.streamGeneration || consumed.sequence < cursor.toSeq) {
        throw Object.assign(new Error('Snapshot replay tail was not consumed.'), { code: 'SNAPSHOT_STALE' })
      }
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
        if (ok && capturedVersion === this.recoveryVersion(key)) {
          if (!scopeGlobal) for (const entry of owned) this.markComplete(entry.receipt)
          return true
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
    const recover = async (scopeKeys: string[], scopeGlobal: boolean): Promise<boolean> => {
      if (!this.current(revision)) return false
      try {
        return await this.source.recoverGap({ reason: 'transport_flow_dirty', keys: scopeKeys, global: scopeGlobal })
      } catch { return false }
    }
    const work = Promise.resolve().then(async () => {
      const scopeKeys = [...requestedKeys]
      const ok = await recover(scopeKeys, requestedGlobal)
      if (!this.current(revision)) return false
      if (ok) {
        complete(requestedKeys)
        return true
      }
      // A missing B owner must not prevent already-authoritative A from
      // releasing its credit. Keep failed retry intents scoped to their keys.
      const failedKeys: string[] = []
      if (scopeKeys.length > 1 || (requestedGlobal && scopeKeys.length > 0)) {
        for (const key of scopeKeys) {
          if (!this.current(revision)) return false
          if (await recover([key], false)) complete(new Set([key]))
          else failedKeys.push(key)
        }
      } else failedKeys.push(...scopeKeys)
      if (this.current(revision)) {
        for (const key of failedKeys) {
          if (this.recoveryKeys.size < 256 || this.recoveryKeys.has(key)) this.recoveryKeys.add(key)
          else this.recoveryGlobal = true
        }
        // A genuinely global failure remains global; a keyed failure never
        // becomes a global retry merely because it joined another read.
        this.recoveryGlobal ||= requestedGlobal
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

  private async flush(): Promise<void> {
    if (!this.enabled || this.inFlight) return
    if (this.ack === this.sentAck && !this.dirty.size && !this.resumes.size && !this.staged.size) return
    const revision = this.revision
    const modern = this.source.supportsRecovery?.() === true
    const hasCredit = this.ack !== this.sentAck || this.staged.size > 0
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
      delivery_epoch: this.epoch!, ack_delivery_id: dirtyOnly ? this.sentAck : this.ack,
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
