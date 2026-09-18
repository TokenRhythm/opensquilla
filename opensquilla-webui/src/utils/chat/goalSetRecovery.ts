import { createClientRequestId } from './messageIdentity'
import { createPendingRequestStore } from './pendingRequestStore'
import type { GoalExecutionOptions } from '@/modules/goalCenter'

interface PendingGoalSet {
  clientRequestId: string
  clientMessageId: string
}

const requests = createPendingRequestStore(
  'opensquilla.goalSetRecovery.v1',
  (value): value is PendingGoalSet => {
    const pending = value as Partial<PendingGoalSet> | null
    return !!pending && typeof pending.clientRequestId === 'string'
      && typeof pending.clientMessageId === 'string'
  },
)

export function goalSetIdentity(
  sessionKey: string, epoch: number, objective: string, options: GoalExecutionOptions,
): string {
  return JSON.stringify([
    sessionKey, epoch, objective,
    options.tokenBudget ?? null, options.executionPolicy ?? 'foreground',
  ])
}

export function recoverGoalSet(identity: string): PendingGoalSet {
  return requests.recover(identity, () => ({
    clientRequestId: createClientRequestId(), clientMessageId: createClientRequestId(),
  }))
}

export function forgetGoalSet(identity: string) {
  requests.forget(identity)
}

export function forgetGoalSetsForSession(sessionKey: string) {
  requests.forgetMatching(identity => {
    try {
      return JSON.parse(identity)[0] === sessionKey
    } catch {
      return true
    }
  })
}
