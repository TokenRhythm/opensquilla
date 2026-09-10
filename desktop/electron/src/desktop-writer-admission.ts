interface DesktopWriterWaiter {
  maximumActive: number
  resolve: () => void
}

export interface ExclusiveDesktopWriterOperation {
  admissionToken: symbol
  finish: () => void
}

/**
 * Coordinates Desktop-owned profile writers with lifecycle operations.
 *
 * A normal writer calls `begin` and always invokes the returned idempotent
 * finish callback. Update, quit, or another lifecycle boundary calls `close`
 * before waiting for active writers to drain. An operation that must close
 * admission and reserve its own writer slot atomically uses
 * `tryBeginExclusive`.
 *
 * This class deliberately has no cleanup or deletion policy. It only owns
 * admission and drain state, so recovery, update, and quit can share the same
 * contract without inheriting destructive-operation semantics.
 */
export class DesktopWriterAdmission {
  private readonly closeOwners = new Set<symbol>()
  private active = 0
  private readonly waiters = new Set<DesktopWriterWaiter>()

  get closed(): boolean {
    return this.closeOwners.size > 0
  }

  get activeCount(): number {
    return this.active
  }

  close(label: string): symbol {
    const token = Symbol(`desktop writer admission: ${label}`)
    this.closeOwners.add(token)
    return token
  }

  reopen(token: symbol): boolean {
    return this.closeOwners.delete(token)
  }

  hasOwner(token: symbol): boolean {
    return this.closeOwners.has(token)
  }

  hasOtherOwner(token: symbol): boolean {
    for (const owner of this.closeOwners) {
      if (owner !== token) return true
    }
    return false
  }

  begin(label: string): () => void {
    if (this.closed) {
      throw new Error(`Desktop writer admission is closed; ${label} was not started.`)
    }
    return this.reserveWriter()
  }

  tryBeginExclusive(label: string): ExclusiveDesktopWriterOperation | null {
    if (this.closed) return null
    const admissionToken = this.close(label)
    return {
      admissionToken,
      finish: this.reserveWriter(),
    }
  }

  waitForAtMost(maximumActive: number, signal?: AbortSignal): Promise<void> {
    if (!Number.isSafeInteger(maximumActive) || maximumActive < 0) {
      throw new Error('Desktop writer drain threshold must be a non-negative integer.')
    }
    if (signal?.aborted) return Promise.reject(signal.reason)
    if (this.active <= maximumActive) return Promise.resolve()
    return new Promise((resolve, reject) => {
      const waiter: DesktopWriterWaiter = {
        maximumActive,
        resolve: () => {
          signal?.removeEventListener('abort', abort)
          resolve()
        },
      }
      const abort = () => {
        this.waiters.delete(waiter)
        reject(signal?.reason)
      }
      this.waiters.add(waiter)
      signal?.addEventListener('abort', abort, { once: true })
    })
  }

  private reserveWriter(): () => void {
    this.active += 1
    let finished = false
    return () => {
      if (finished) return
      finished = true
      this.active = Math.max(0, this.active - 1)
      for (const waiter of [...this.waiters]) {
        if (this.active > waiter.maximumActive) continue
        this.waiters.delete(waiter)
        waiter.resolve()
      }
    }
  }
}
