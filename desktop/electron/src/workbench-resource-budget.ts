export type WorkbenchResourceKind = 'browser' | 'preview'
export type WorkbenchResourcePhase = 'creating' | 'resident' | 'disposing'

// Preview ownership spans chats, so hidden previews must not prevent opening a
// new document. Keep their reservations for replacement and cleanup accounting.
export const WORKBENCH_RESOURCE_LIMITS = { browser: 8, preview: Number.POSITIVE_INFINITY } as const

export interface WorkbenchResourceReservation {
  /** Hold the same slot across replacement; old cleanup cannot free it early. */
  retain(): WorkbenchResourceReservation
  setPhase(phase: WorkbenchResourcePhase): void
  release(): void
}

/** Admission, not eviction: unknown pages retain their forms, JS and run targets.
 * Reservations include asynchronous creation and cleanup, so rapid switching or
 * closing cannot temporarily bypass a bounded kind's limit. Unbounded previews
 * retain the same cleanup accounting. This is not a heap-byte budget.
 */
export class WorkbenchResourceBudget {
  private readonly reservations = new Map<symbol, { kind: WorkbenchResourceKind; phase: WorkbenchResourcePhase }>()

  constructor(private readonly limits: Readonly<Record<WorkbenchResourceKind, number>> = WORKBENCH_RESOURCE_LIMITS) {}

  hasCapacity(kind: WorkbenchResourceKind): boolean {
    return this.snapshot().filter(entry => entry.kind === kind).length < this.limits[kind]
  }

  acquire(kind: WorkbenchResourceKind): WorkbenchResourceReservation | null {
    if (!this.hasCapacity(kind)) return null
    const token = Symbol(kind)
    const entry = { kind, phase: 'creating' as WorkbenchResourcePhase }
    this.reservations.set(token, entry)
    let references = 0
    const handle = (): WorkbenchResourceReservation => {
      references++
      let released = false
      return {
        retain: () => {
          if (released) throw new Error('Cannot retain a released workbench resource')
          return handle()
        },
        setPhase: phase => { if (!released) entry.phase = phase },
        release: () => {
          if (released) return
          released = true
          if (--references === 0) this.reservations.delete(token)
        },
      }
    }
    return handle()
  }

  snapshot(): ReadonlyArray<Readonly<{ kind: WorkbenchResourceKind; phase: WorkbenchResourcePhase }>> {
    return Array.from(this.reservations.values(), entry => ({ ...entry }))
  }
}
