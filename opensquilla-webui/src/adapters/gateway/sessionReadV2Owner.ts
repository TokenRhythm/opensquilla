import { SessionReadFailure } from '@/modules/sessionReadLifecycle'
import { SESSIONS_READ_OPEN_V2_METHOD } from '@/contracts/generated/v4/sessionsReadOpenV2'
import { SESSIONS_READ_CLOSE_V2_METHOD } from '@/contracts/generated/v4/sessionsReadCloseV2'
import { validateResult as validateOpen } from '@/contracts/generated/v4/sessionsReadOpenV2Validators.mjs'
import { validateResult as validateClose } from '@/contracts/generated/v4/sessionsReadCloseV2Validators.mjs'
import type { SessionReadV2Open, SessionReadV2Transport } from './sessionReadV2'

const RESPONSE_WAIT_MS = 7_000

type Outcome = { value: unknown } | { error: unknown } | { barrier: true }
interface WireRequest {
  readonly sequence: number
  readonly controller: AbortController
  readonly result: Promise<Outcome>
  sent: boolean
  finish(outcome: Outcome): void
}
interface Observer { reject(error: unknown): void; cleanup: boolean; consumer?: SessionReadV2Handle }

interface WireJob {
  owner: SessionReadV2Owner
  cleanup: boolean
  consumer?: SessionReadV2Handle
  execute(): Promise<unknown>
  resolve(value: unknown): void
  reject(error: unknown): void
}

/** The slot covers an operation, not a live lease. Main chat and side chat may
 * keep independent installed leases on the same connection. */
class SessionReadV2WireQueue {
  private active: WireJob | null = null
  private pending: WireJob[] = []
  failure: SessionReadFailure | null = null

  run(owner: SessionReadV2Owner, execute: () => Promise<unknown>, cleanup = false, consumer?: SessionReadV2Handle): Promise<unknown> {
    if (this.failure) return Promise.reject(this.failure)
    return new Promise((resolve, reject) => {
      const job = { owner, execute, cleanup, consumer, resolve, reject }
      if (cleanup) this.pending.unshift(job)
      else this.pending.push(job)
      this.advance()
    })
  }

  cancelUnsent(owner: SessionReadV2Owner, consumer?: SessionReadV2Handle): void {
    this.pending = this.pending.filter(job => {
      if (job.owner !== owner || job.cleanup) return true
      if (consumer && job.consumer !== consumer) return true
      job.reject(aborted())
      return false
    })
  }

  block(error: SessionReadFailure): void {
    this.failure = error
    for (const job of this.pending.splice(0)) job.reject(error)
  }

  private advance(): void {
    if (this.active || this.failure) return
    const job = this.pending.shift()
    if (!job) return
    this.active = job
    const finish = () => {
      if (this.active !== job) return
      this.active = null
      // A late wire result can settle an operation previously shown unresolved.
      this.failure = job.owner.terminalFailure
    }
    void job.execute().then(value => {
      finish()
      job.resolve(value)
      this.advance()
    }, error => {
      finish()
      job.reject(error)
      this.advance()
    })
  }
}

function aborted(): Error {
  return Object.assign(new Error('Session read was superseded.'), { name: 'AbortError' })
}

function errorCode(error: unknown): string {
  if (!error || typeof error !== 'object') return ''
  const value = error as { code?: unknown; data?: { code?: unknown } }
  const code = value.code ?? value.data?.code
  return typeof code === 'string' ? code.toUpperCase() : ''
}

function unresolved(): SessionReadFailure {
  return new SessionReadFailure('unresolved', 'Session recovery completion could not be confirmed. Refresh this page to recover.', false)
}

/** Owns server work independently of the view that requested it. These four
 * methods use the Gateway's ordinary FIFO; a local abort is not cancellation. */
export class SessionReadV2Owner {
  private retired = false
  private openResult: SessionReadV2Open | null = null
  private tail: Promise<unknown> = Promise.resolve()
  private closing: Promise<unknown> | null = null
  private failure: SessionReadFailure | null = null
  private retirementFailure: SessionReadFailure | null = null
  private probeUsed = false
  private sequence = 0
  private readonly wires = new Set<WireRequest>()
  private readonly observers = new Set<Observer>()
  private readonly consumers = new Set<SessionReadV2Handle>()
  private opening: Promise<unknown> | null = null

  constructor(
    private readonly rpc: SessionReadV2Transport,
    readonly key: string,
    readonly generation: number,
    private readonly queue: SessionReadV2WireQueue,
    private readonly released: (owner: SessionReadV2Owner) => void,
    private readonly blocked: (owner: SessionReadV2Owner, error: unknown) => void,
  ) {}

  get terminalFailure(): SessionReadFailure | null { return this.failure }
  get isRetired(): boolean { return this.retired }

  attach(signal?: AbortSignal): SessionReadV2Handle {
    const consumer = new SessionReadV2Handle(this, signal)
    this.consumers.add(consumer)
    return consumer
  }

  release(consumer: SessionReadV2Handle): Promise<unknown> {
    consumer.detach()
    this.consumers.delete(consumer)
    this.rejectObservers(aborted(), false, consumer)
    this.queue.cancelUnsent(this, consumer)
    return this.consumers.size === 0 || this.retired ? this.close() : Promise.resolve(null)
  }

  request(method: string, params: Record<string, unknown>, consumer: SessionReadV2Handle): Promise<unknown> {
    if (this.retired || consumer.closed) return Promise.reject(this.failure ?? this.retirementFailure ?? consumer.closedError)
    const isOpen = method === SESSIONS_READ_OPEN_V2_METHOD
    if (isOpen && this.openResult) return Promise.resolve(this.openResult)
    if (isOpen && this.opening) return this.observe(this.opening, false, consumer)
    const operation = this.tail.catch(() => {}).then(() => {
      if (this.retired || (!isOpen && consumer.closed)) throw this.failure ?? this.retirementFailure ?? consumer.closedError
      return this.queue.run(this, () => this.requestWire(method, params), false, isOpen ? undefined : consumer)
    })
    // Only actual wire completion advances this tail. Consumers may leave early.
    this.tail = operation
    if (isOpen) {
      this.opening = operation
      void operation.finally(() => { this.opening = null }).catch(() => {})
    }
    return this.observe(operation, false, consumer)
  }

  close(): Promise<unknown> {
    if (!this.retired) {
      this.retired = true
      for (const consumer of this.consumers) consumer.detach(this.retirementFailure ?? aborted())
      this.consumers.clear()
      this.rejectObservers(this.retirementFailure ?? aborted(), false)
      this.queue.cancelUnsent(this)
      // A safe-read waiting for connection admission has not reached the server.
      for (const wire of this.wires) if (!wire.sent) wire.controller.abort()
    }
    if (!this.closing) {
      const operation = this.tail.catch(() => {}).then(async () => {
        if (this.rpc.generation !== this.generation) return null
        if (!this.openResult) {
          if (this.failure) throw this.failure
          return null
        }
        const params = this.leaseParams()
        // An admission refusal is completed work, but not completed cleanup.
        // Retry once; later demand may retry a known refusal without adding debt.
        return this.queue.run(this, async () => {
          for (let attempt = 0; ; attempt++) {
            try {
              return await this.requestWire(SESSIONS_READ_CLOSE_V2_METHOD, params)
            } catch (error) {
              const code = errorCode(error)
              if (code === 'READ_STALE') return null
              const retryable = code === 'UNAVAILABLE' || code === 'STORAGE_BUSY' || code === 'READ_UNAVAILABLE'
              if (!retryable || this.failure || attempt === 1 || this.rpc.generation !== this.generation) throw error
            }
          }
        }, true)
      })
      this.closing = operation
      void operation.then(() => {
        this.released(this)
      }, error => {
        this.closing = null
        this.blocked(this, error)
      })
    }
    return this.observe(this.closing, true)
  }

  /** Only a replaced physical socket makes an unknown old result irrelevant. */
  retireGeneration(): void {
    this.retired = true
    for (const consumer of this.consumers) consumer.detach()
    this.consumers.clear()
    this.queue.cancelUnsent(this)
    for (const wire of [...this.wires]) {
      wire.finish({ error: aborted() })
      wire.controller.abort()
    }
    this.rejectObservers(aborted(), true)
  }

  private leaseParams(): Record<string, unknown> {
    if (!this.openResult) throw unresolved()
    return { key: this.key, lease_id: this.openResult.lease_id, recovery_id: this.openResult.recovery_id }
  }

  private observe(operation: Promise<unknown>, cleanup = false, consumer?: SessionReadV2Handle): Promise<unknown> {
    if (this.failure) return Promise.reject(this.failure)
    if (!cleanup && (this.retired || consumer?.closed)) return Promise.reject(aborted())
    return new Promise((resolve, reject) => {
      const observer = { reject, cleanup, consumer }
      this.observers.add(observer)
      void operation.then(resolve, reject).finally(() => this.observers.delete(observer))
    })
  }

  private rejectObservers(error: unknown, cleanup: boolean, consumer?: SessionReadV2Handle): void {
    for (const observer of this.observers) {
      if (!cleanup && observer.cleanup) continue
      if (consumer && observer.consumer !== consumer) continue
      this.observers.delete(observer)
      observer.reject(error)
    }
  }

  private failUnresolved(): void {
    if (this.failure || this.rpc.generation !== this.generation) return
    this.failure = unresolved()
    this.queue.block(this.failure)
    this.rejectObservers(this.failure, true)
    this.blocked(this, this.failure)
  }

  private startWire(method: string, params: Record<string, unknown>): WireRequest {
    if (this.wires.size >= 2) throw unresolved()
    const controller = new AbortController()
    let complete!: (value: Outcome) => void
    let finished = false
    const result = new Promise<Outcome>(resolve => { complete = resolve })
    const wire: WireRequest = {
      sequence: ++this.sequence,
      controller,
      result,
      sent: false,
      finish: outcome => {
        if (finished) return
        finished = true
        this.wires.delete(wire)
        complete(outcome)
      },
    }
    this.wires.add(wire)
    // No consumer signal or local response deadline may discard a late result.
    let response: Promise<unknown>
    try {
      response = this.rpc.request(method, params, {
        expectedGeneration: this.generation,
        signal: controller.signal,
        timeoutMs: 0,
        abortAction: 'reject',
        timeoutAction: 'reject',
        recoveryClass: 'safe-read',
        onSent: () => { wire.sent = true },
      })
    } catch (error) {
      wire.finish({ error })
      return wire
    }
    void response.then(value => {
      if (finished || this.rpc.generation !== this.generation) {
        wire.finish({ error: aborted() })
        return
      }
      if (method === SESSIONS_READ_OPEN_V2_METHOD) {
        if (!validateOpen(value)) {
          this.failUnresolved()
          wire.finish({ error: this.failure })
          return
        }
        this.openResult = value as SessionReadV2Open
      }
      if (method === SESSIONS_READ_CLOSE_V2_METHOD && (!validateClose(value)
        || (value as { lease_id: string }).lease_id !== params.lease_id)) {
        this.failUnresolved()
        wire.finish({ error: this.failure })
        return
      }
      this.acceptBarrier(wire)
      wire.finish({ value })
    }, error => {
      // Exact close returning READ_STALE proves the FIFO executed that close.
      // An admission rejection does not prove that earlier requests finished.
      if (method === SESSIONS_READ_CLOSE_V2_METHOD && errorCode(error) === 'READ_STALE') {
        this.acceptBarrier(wire)
      }
      wire.finish({ error })
      if (method !== SESSIONS_READ_CLOSE_V2_METHOD
        && (errorCode(error) === 'REBASE_REQUIRED' || errorCode(error) === 'READ_STALE')) {
        this.retirementFailure = new SessionReadFailure('rebase-required', 'The session recovery lease is stale; acquire a fresh read.', true, 0, error)
        this.rejectObservers(this.retirementFailure, false)
        void this.close().catch(() => {})
      }
    })
    return wire
  }

  private acceptBarrier(wire: WireRequest): void {
    this.failure = null
    for (const prior of [...this.wires]) {
      if (prior.sequence >= wire.sequence) continue
      prior.finish({ barrier: true })
      // The later FIFO response proves the old handler is done. This abort only
      // removes its local waiter; these methods do not send server cancel frames.
      prior.controller.abort()
    }
  }

  private async requestWire(method: string, params: Record<string, unknown>): Promise<unknown> {
    if (this.rpc.generation !== this.generation) throw aborted()
    const first = this.startWire(method, params)
    let probe: WireRequest | undefined
    let terminalTimer: ReturnType<typeof setTimeout> | undefined
    const timer = setTimeout(() => {
      const timeout = new SessionReadFailure('timeout', 'Session recovery response timed out.', true)
      this.rejectObservers(timeout, false)
      this.retirementFailure = new SessionReadFailure('rebase-required', 'Session recovery was retired after an unconfirmed response; acquire a fresh read.', true)
      void this.close().catch(() => {})
      if (!this.probeUsed && this.wires.size < 2 && first.sent
        && this.rpc.generation === this.generation) {
        this.probeUsed = true
        const probeMethod = method === SESSIONS_READ_CLOSE_V2_METHOD ? method : SESSIONS_READ_OPEN_V2_METHOD
        const probeParams = probeMethod === SESSIONS_READ_CLOSE_V2_METHOD ? params : { key: this.key }
        probe = this.startWire(probeMethod, probeParams)
      }
      terminalTimer = setTimeout(() => this.failUnresolved(), RESPONSE_WAIT_MS)
    }, RESPONSE_WAIT_MS)
    const outcome = await first.result
    // A probe may itself create/reuse a lease. Do not release the shared wire
    // slot until both requests have responses or a later FIFO barrier covers it.
    if (probe) await probe.result
    clearTimeout(timer)
    if (terminalTimer !== undefined) clearTimeout(terminalTimer)
    if ('error' in outcome) throw outcome.error
    if ('value' in outcome) return outcome.value
    if (method === SESSIONS_READ_OPEN_V2_METHOD && this.openResult) return this.openResult
    if (method === SESSIONS_READ_CLOSE_V2_METHOD) return null
    // A probe proves execution, not successful installation or its watermark.
    throw new SessionReadFailure('timeout', 'Session recovery result was reconciled after its consumer left.', true)
  }
}

/** Two mounted views may display the same session. Closing one consumer must
 * not retire the shared connection/key lease still used by the other view. */
export class SessionReadV2Handle {
  closed = false
  closedError: Error = aborted()
  private readonly abortHandler = () => { void this.close().catch(() => {}) }

  constructor(private readonly owner: SessionReadV2Owner, private readonly signal?: AbortSignal) {
    signal?.addEventListener('abort', this.abortHandler, { once: true })
  }

  request(method: string, params: Record<string, unknown>): Promise<unknown> {
    return this.owner.request(method, params, this)
  }

  close(): Promise<unknown> { return this.owner.release(this) }

  detach(error: Error = aborted()): void {
    this.closed = true
    this.closedError = error
    this.signal?.removeEventListener('abort', this.abortHandler)
  }
}

class SessionReadV2Owners {
  private readonly active = new Set<SessionReadV2Owner>()
  private queue = new SessionReadV2WireQueue()
  private generation: number

  constructor(private readonly rpc: SessionReadV2Transport) { this.generation = rpc.generation }

  async acquire(key: string, generation: number, signal?: AbortSignal): Promise<SessionReadV2Handle> {
    if (signal?.aborted || this.rpc.generation !== generation) throw aborted()
    if (this.generation !== generation) {
      for (const owner of this.active) owner.retireGeneration()
      this.active.clear()
      this.queue = new SessionReadV2WireQueue()
      this.generation = generation
    }
    if (this.queue.failure) throw this.queue.failure
    const reusable = [...this.active].find(prior => prior.key === key && !prior.isRetired)
    if (reusable) return reusable.attach(signal)
    // Open is idempotent by connection/key. Do not adopt an old lease before
    // its close. Finish any known failed retirement before accumulating leases
    // for more keys; a refusal can be retried without adding unknown wire work.
    for (const prior of this.active) {
      if (prior.isRetired) await waitForConsumer(prior.close(), signal)
    }
    if (signal?.aborted || this.rpc.generation !== generation) throw aborted()
    if (this.queue.failure) throw this.queue.failure
    const acquired = [...this.active].find(prior => prior.key === key && !prior.isRetired)
    if (acquired) return acquired.attach(signal)
    const owner = new SessionReadV2Owner(this.rpc, key, generation, this.queue,
      released => this.active.delete(released),
      (blocked, error) => {
        if (blocked.generation === this.generation && this.rpc.generation === this.generation
          && error instanceof SessionReadFailure && error.kind === 'unresolved') this.queue.block(error)
      })
    this.active.add(owner)
    return owner.attach(signal)
  }
}

function waitForConsumer<T>(operation: Promise<T>, signal?: AbortSignal): Promise<T> {
  if (!signal) return operation
  if (signal.aborted) return Promise.reject(aborted())
  return new Promise((resolve, reject) => {
    const cancel = () => reject(aborted())
    signal.addEventListener('abort', cancel, { once: true })
    void operation.then(resolve, reject).finally(() => signal.removeEventListener('abort', cancel))
  })
}

const owners = new WeakMap<SessionReadV2Transport, SessionReadV2Owners>()

export function acquireSessionReadV2Owner(
  rpc: SessionReadV2Transport, key: string, generation: number, signal?: AbortSignal,
): Promise<SessionReadV2Handle> {
  let coordinator = owners.get(rpc)
  if (!coordinator) {
    coordinator = new SessionReadV2Owners(rpc)
    owners.set(rpc, coordinator)
  }
  return coordinator.acquire(key, generation, signal)
}
