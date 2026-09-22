export const DESKTOP_QUIT_CLEANUP_MS = 10_000
export const DESKTOP_QUIT_TERMINATION_MS = 5_000

export type DesktopQuitActivity = 'idle' | 'active' | 'unknown'

export interface DesktopQuitConfirmationOptions {
  check(): Promise<DesktopQuitActivity>
  confirm(state: Exclude<DesktopQuitActivity, 'idle'>): Promise<boolean>
}

/** Coalesce one activity check and prompt without committing the application's exit. */
export class DesktopQuitConfirmation {
  private pending: Promise<boolean> | null = null

  request(options: DesktopQuitConfirmationOptions): Promise<boolean> {
    if (this.pending) return this.pending
    const pending = Promise.resolve()
      .then(() => options.check())
      .then(state => state === 'idle' ? true : options.confirm(state))
      .finally(() => { this.pending = null })
    this.pending = pending
    return pending
  }
}

type BoundedResult<T> = { completed: true; value: T } | { completed: false }

/** One deadline starts after confirmation and protected writes have settled. */
export class DesktopQuitBudget {
  private readonly startedAt: number
  private readonly controller = new AbortController()
  private cleanupTimer: ReturnType<typeof setTimeout>
  private cleanupDeadline: number
  private totalDeadline: number
  private readonly deadlineListeners = new Set<() => void>()

  constructor(
    cleanupMs = DESKTOP_QUIT_CLEANUP_MS,
    private readonly terminationMs = DESKTOP_QUIT_TERMINATION_MS,
    private readonly now: () => number = () => performance.now(),
  ) {
    this.startedAt = now()
    this.cleanupDeadline = this.startedAt + cleanupMs
    this.totalDeadline = this.cleanupDeadline + terminationMs
    this.cleanupTimer = setTimeout(() => this.controller.abort(), cleanupMs)
  }

  get signal(): AbortSignal { return this.controller.signal }
  get elapsedMs(): number { return Math.max(0, this.now() - this.startedAt) }
  get remainingCleanupMs(): number { return Math.max(0, Math.floor(this.cleanupDeadline - this.now())) }
  get remainingTotalMs(): number {
    return Math.max(0, Math.floor(this.totalDeadline - this.now()))
  }

  tighten(cleanupMs: number, totalMs = cleanupMs + this.terminationMs): void {
    const now = this.now()
    this.totalDeadline = Math.min(this.totalDeadline, now + Math.max(0, totalMs))
    this.cleanupDeadline = Math.min(
      this.cleanupDeadline, this.totalDeadline, now + Math.max(0, cleanupMs),
    )
    clearTimeout(this.cleanupTimer)
    if (this.remainingCleanupMs === 0) this.controller.abort()
    this.cleanupTimer = setTimeout(() => this.controller.abort(), this.remainingCleanupMs)
    for (const listener of this.deadlineListeners) listener()
  }

  cleanup<T>(work: Promise<T>): Promise<BoundedResult<T>> {
    return this.wait(work, () => this.remainingCleanupMs)
  }

  termination<T>(work: Promise<T>): Promise<BoundedResult<T>> {
    return this.wait(work, () => this.remainingTotalMs)
  }

  dispose(): void {
    clearTimeout(this.cleanupTimer)
    this.controller.abort()
  }

  private async wait<T>(work: Promise<T>, remainingMs: () => number): Promise<BoundedResult<T>> {
    let timer: ReturnType<typeof setTimeout> | undefined
    let reschedule: (() => void) | undefined
    try {
      return await Promise.race([
        work.then(value => ({ completed: true as const, value })),
        new Promise<{ completed: false }>(resolve => {
          reschedule = () => {
            if (timer) clearTimeout(timer)
            timer = setTimeout(() => resolve({ completed: false }), remainingMs())
          }
          this.deadlineListeners.add(reschedule)
          reschedule()
        }),
      ])
    } finally {
      if (timer) clearTimeout(timer)
      if (reschedule) this.deadlineListeners.delete(reschedule)
    }
  }
}

export interface DesktopQuitGatewayOptions {
  budget: DesktopQuitBudget
  hasExited(): boolean
  requestQuit(remainingMs: number): Promise<{
    kind: 'quit_accepted' | 'unsupported' | 'rejected' | 'unreachable'
    remainingMs?: number
    totalRemainingMs?: number
  }>
  legacyDrain(): Promise<boolean>
  waitForExit(timeoutMs: number): Promise<boolean>
  terminate(timeoutMs: number): Promise<boolean>
  onLegacyDrain(): void
  onTerminating(): void
}

/** Stop an exact owned child; unsupported peers retain their existing drain policy. */
export async function quitGatewayWithinBudget(options: DesktopQuitGatewayOptions): Promise<boolean> {
  const { budget } = options
  if (options.hasExited()) return true
  const requestedAt = budget.elapsedMs
  const request = await budget.cleanup(options.requestQuit(budget.remainingCleanupMs))
  if (request.completed && request.value.kind === 'rejected') return false
  if (request.completed && request.value.kind === 'unsupported') {
    options.onLegacyDrain()
    return await options.legacyDrain()
  }
  if (request.completed && request.value.kind === 'quit_accepted'
    && request.value.remainingMs !== undefined) {
    // Treat the whole round trip as already spent. Across process clocks this
    // conservatively starts termination before the peer's watchdog can exit.
    const roundTripMs = budget.elapsedMs - requestedAt
    budget.tighten(
      request.value.remainingMs - roundTripMs,
      request.value.totalRemainingMs === undefined
        ? undefined : request.value.totalRemainingMs - roundTripMs,
    )
  }
  if (options.hasExited()) return true
  const graceful = await budget.cleanup(options.waitForExit(budget.remainingCleanupMs))
  if (graceful.completed && graceful.value) return true
  if (options.hasExited()) return true
  options.onTerminating()
  const terminated = await budget.termination(options.terminate(budget.remainingTotalMs))
  return terminated.completed && terminated.value && options.hasExited()
}
