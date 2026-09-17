import { isDeepStrictEqual } from 'node:util'

export type OnboardingFlowState = 'editing' | 'saving' | 'completed' | 'abandoned'

export interface CoordinatedOnboardingFlow<Payload, Result> {
  state: OnboardingFlowState
  savePayload: Payload | null
  savePromise: Promise<Result> | null
}

export type OnboardingSaveRequest<Result> =
  | { kind: 'started'; promise: Promise<Result> }
  | { kind: 'joined'; promise: Promise<Result> }
  | { kind: 'conflict' }
  | { kind: 'inactive' }

/**
 * Owns the identity and per-flow save state for the native onboarding window.
 * Window lifetime, persistence, and app lifecycle policy remain in the main
 * process; this coordinator only prevents overlapping or stale flow work.
 */
export class OnboardingFlowCoordinator<
  Payload,
  Result,
  Flow extends CoordinatedOnboardingFlow<Payload, Result>,
> {
  private activeFlow: Flow | null = null

  get active(): Flow | null {
    return this.activeFlow
  }

  activate(flow: Flow): boolean {
    if (this.activeFlow || flow.state !== 'editing') return false
    this.activeFlow = flow
    return true
  }

  isCurrent(flow: Flow): boolean {
    return this.activeFlow === flow
  }

  canComplete(flow: Flow): boolean {
    return this.activeFlow === flow && flow.state === 'saving'
  }

  abandon(flow: Flow): boolean {
    if (flow.state === 'completed' || flow.state === 'abandoned') return false
    flow.state = 'abandoned'
    if (!flow.savePromise && this.activeFlow === flow) this.activeFlow = null
    return true
  }

  complete(flow: Flow): boolean {
    if (!this.canComplete(flow)) return false
    flow.state = 'completed'
    this.activeFlow = null
    return true
  }

  async waitForAbandonedSave(): Promise<void> {
    const flow = this.activeFlow
    if (!flow || flow.state !== 'abandoned' || !flow.savePromise) return
    await flow.savePromise.catch(() => null)
    if (this.activeFlow === flow && flow.state === 'abandoned') this.activeFlow = null
  }

  requestSave(
    flow: Flow,
    payload: Payload,
    perform: () => Promise<Result>,
  ): OnboardingSaveRequest<Result> {
    if (this.activeFlow !== flow) return { kind: 'inactive' }
    if (flow.savePromise) {
      return isDeepStrictEqual(flow.savePayload, payload)
        ? { kind: 'joined', promise: flow.savePromise }
        : { kind: 'conflict' }
    }
    if (flow.state !== 'editing') return { kind: 'inactive' }

    flow.state = 'saving'
    flow.savePayload = payload
    // Defer by one microtask so the single-flight is published before perform
    // reaches its first await or otherwise yields control.
    const operation = Promise.resolve().then(perform)
    let savePromise!: Promise<Result>
    savePromise = operation.finally(() => {
      if (flow.savePromise !== savePromise) return
      flow.savePromise = null
      flow.savePayload = null
      if (flow.state === 'abandoned' && this.activeFlow === flow) {
        this.activeFlow = null
      }
    })
    flow.savePromise = savePromise
    return { kind: 'started', promise: savePromise }
  }
}
