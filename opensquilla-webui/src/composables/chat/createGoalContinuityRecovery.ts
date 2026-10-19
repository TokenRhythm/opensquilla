import { GoalCenterError, type GoalRecoveryAction } from '@/modules/goalCenter'

export const GOAL_CONTINUITY_BUDGET_MS = 120_000
export const GOAL_CONTINUITY_ATTEMPT_MS = 7_000

export interface GoalContinuityOwner {
  sessionKey: string
  sessionId: string
  epoch: number
  goalId: string
  token: string
  authority: string | null
}

export interface GoalContinuityDecision {
  readonly reason: 'budget-exhausted' | 'rejected'
  readonly error: unknown
  readonly action?: GoalRecoveryAction
}

interface Incident {
  expiresAt: number
  notBefore: number
  attempts: number
  terminal?: GoalContinuityDecision
  terminalHandled?: boolean
  manualEpisode?: boolean
  explicitAdmissionPending?: boolean
}

export function goalContinuityRetryable(error: unknown): boolean {
  if (!(error instanceof GoalCenterError)) return false
  if (error.retryable === false) return false
  if (error.recoveryAction && error.recoveryAction !== 'retry' && error.recoveryAction !== 'await-subscription') return false
  return error.code === 'unavailable' || error.retryable === true
}

/** One bounded same-token incident; transport replacement does not renew it. */
export function createGoalContinuityRecovery(options: {
  isCurrent: (owner: GoalContinuityOwner) => boolean
  attempt: (owner: GoalContinuityOwner, signal: AbortSignal, admission: { explicit: boolean }) => Promise<boolean>
  onWorking: (working: boolean) => void
  onTerminal: (error: unknown, action: GoalRecoveryAction | undefined) => void
  refresh?: (owner: GoalContinuityOwner, signal: AbortSignal) => Promise<void>
}) {
  const incidents = new Map<string, Incident>()
  let generation = 0
  let active: { key: string; controller: AbortController; promise: Promise<void> } | null = null
  let disposed = false
  function cancel() {
    generation++
    active?.controller.abort()
    active = null
    options.onWorking(false)
  }
  function pause(signal: AbortSignal, ms: number): Promise<void> {
    return new Promise(resolve => {
      const finish = () => { clearTimeout(timer); signal.removeEventListener('abort', finish); resolve() }
      const timer = setTimeout(finish, Math.max(0, ms))
      signal.addEventListener('abort', finish, { once: true })
      if (signal.aborted) finish()
    })
  }
  function ownerKey(owner: GoalContinuityOwner): string {
    return JSON.stringify([owner.authority, owner.sessionKey, owner.sessionId, owner.epoch, owner.goalId, owner.token])
  }
  function terminalDecision(incident: Incident): GoalContinuityDecision | undefined {
    if (!incident.terminal && performance.now() >= incident.expiresAt) incident.terminal = {
      reason: 'budget-exhausted',
      error: new GoalCenterError('unavailable', 'Goal continuity recovery deadline elapsed.', { retryable: false }),
    }
    return incident.terminal
  }
  function decision(owner: GoalContinuityOwner): GoalContinuityDecision | null {
    const incident = incidents.get(ownerKey(owner))
    return incident ? terminalDecision(incident) ?? null : null
  }
  function canRetry(owner: GoalContinuityOwner): boolean {
    const incident = incidents.get(ownerKey(owner))
    if (!incident) return false
    const terminal = terminalDecision(incident)
    return terminal ? terminal.reason === 'budget-exhausted' : incident.manualEpisode === true
  }
  function bindAuthority(owner: GoalContinuityOwner) {
    if (owner.authority === null) return
    const oldKey = ownerKey({ ...owner, authority: null })
    const incident = incidents.get(oldKey)
    cancel()
    if (!incident) return
    const key = ownerKey(owner)
    const existing = incidents.get(key)
    if (existing) {
      existing.expiresAt = Math.min(existing.expiresAt, incident.expiresAt)
      existing.notBefore = Math.max(existing.notBefore, incident.notBefore)
      existing.attempts = Math.max(existing.attempts, incident.attempts)
      // A server rejection stays stronger than a local deadline when the
      // first proven authority binds an already-observed incident.
      if (existing.terminal?.reason !== 'rejected') existing.terminal = incident.terminal ?? existing.terminal
      existing.terminalHandled ||= incident.terminalHandled
      existing.manualEpisode ||= incident.manualEpisode
      existing.explicitAdmissionPending ||= incident.explicitAdmissionPending
    } else incidents.set(key, incident)
    incidents.delete(oldKey)
  }
  function recover(owner: GoalContinuityOwner, admission: { explicit?: boolean } = {}): Promise<void> {
    if (disposed || !options.isCurrent(owner)) return Promise.resolve()
    const key = ownerKey(owner)
    if (active?.key === key) return active.promise
    let incident = incidents.get(key)
    if (admission.explicit) {
      if (!incident || !canRetry(owner)) return Promise.resolve()
      if (terminalDecision(incident)?.reason === 'budget-exhausted') {
        incident.expiresAt = performance.now() + GOAL_CONTINUITY_BUDGET_MS
        incident.attempts = 0
        incident.terminal = undefined
        incident.terminalHandled = false
        incident.manualEpisode = true
        incident.explicitAdmissionPending = true
        // Deliberate renewal never overrides a server's not-before hint.
      }
    }
    cancel()
    const ownGeneration = generation
    const controller = new AbortController()
    const current = () => !disposed && generation === ownGeneration && !controller.signal.aborted && options.isCurrent(owner)
    if (!incident) {
      incident = { expiresAt: performance.now() + GOAL_CONTINUITY_BUDGET_MS, notBefore: 0, attempts: 0 }
      incidents.set(key, incident)
      // Retain incident tombstones for this controller lifetime: navigation
      // through other Goals must not make a rejected or exhausted token fresh.
    }
    const recovery = incident
    const run = async () => {
      options.onWorking(true)
      while (current()) {
        const terminal = terminalDecision(recovery)
        if (terminal) {
          const action = terminal.action
          options.onTerminal(terminal.error, action)
          if (!recovery.terminalHandled && action === 'refresh-goal' && options.refresh && current()) {
            recovery.terminalHandled = true
            const terminalController = new AbortController()
            const abort = () => terminalController.abort()
            controller.signal.addEventListener('abort', abort, { once: true })
            const timeout = setTimeout(abort, Math.max(0, Math.min(GOAL_CONTINUITY_ATTEMPT_MS, recovery.expiresAt - performance.now())))
            try {
              await Promise.race([
                options.refresh(owner, terminalController.signal),
                pause(terminalController.signal, Math.max(0, Math.min(GOAL_CONTINUITY_ATTEMPT_MS, recovery.expiresAt - performance.now()))),
              ])
            } catch { /* A failed status refresh cannot restart rejected continuity. */ }
            finally {
              terminalController.abort()
              clearTimeout(timeout)
              controller.signal.removeEventListener('abort', abort)
            }
          }
          return
        }
        if (recovery.notBefore > performance.now()) {
          await pause(controller.signal, Math.min(recovery.notBefore, recovery.expiresAt) - performance.now())
        }
        if (!current()) return
        if (performance.now() >= recovery.expiresAt) continue
        const attempt = new AbortController()
        const abort = () => attempt.abort()
        controller.signal.addEventListener('abort', abort, { once: true })
        const timeoutMs = Math.min(GOAL_CONTINUITY_ATTEMPT_MS, recovery.expiresAt - performance.now())
        let timeout: ReturnType<typeof setTimeout> | undefined
        let cancelled!: () => void
        try {
          const explicit = recovery.explicitAdmissionPending === true
          recovery.explicitAdmissionPending = false
          const accepted = await Promise.race([
            options.attempt(owner, attempt.signal, { explicit }),
            new Promise<never>((_, reject) => {
              cancelled = () => reject(new GoalCenterError('unavailable', 'Goal continuity attempt cancelled.', { retryable: false }))
              controller.signal.addEventListener('abort', cancelled, { once: true })
              timeout = setTimeout(() => {
                attempt.abort()
                reject(new GoalCenterError('unavailable', 'Goal continuity attempt timed out.', { retryable: true, recoveryAction: 'retry' }))
              }, timeoutMs)
            }),
          ])
          if (disposed || generation !== ownGeneration || controller.signal.aborted || attempt.signal.aborted) return
          if (accepted) incidents.delete(key)
          return
        } catch (error) {
          if (!current()) return
          if (!goalContinuityRetryable(error)) recovery.terminal = {
            reason: 'rejected', error,
            action: error instanceof GoalCenterError ? error.recoveryAction : undefined,
          }
          else recovery.notBefore = performance.now() + Math.max(
            error instanceof GoalCenterError ? error.retryAfterMs : 0,
            Math.min(15_000, 500 * 2 ** Math.min(recovery.attempts++, 5)),
          )
        } finally {
          if (timeout !== undefined) clearTimeout(timeout)
          if (cancelled) controller.signal.removeEventListener('abort', cancelled)
          controller.signal.removeEventListener('abort', abort)
          attempt.abort()
        }
      }
    }
    // Publish the real joinable owner before callbacks can re-enter recovery.
    const promise = Promise.resolve().then(run).finally(() => {
      if (generation === ownGeneration) {
        active = null
        options.onWorking(false)
      }
    })
    active = { key, controller, promise }
    return promise
  }
  return {
    recover,
    decision,
    canRetry,
    cancel,
    bindAuthority,
    dispose() { disposed = true; cancel(); incidents.clear() },
  }
}
