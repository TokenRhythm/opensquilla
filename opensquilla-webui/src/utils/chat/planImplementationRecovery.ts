import { createClientRequestId } from './messageIdentity'
import { createPendingRequestStore } from './pendingRequestStore'

interface PendingImplementation {
  clientRequestId: string
  targetSessionKey: string
}

const requests = createPendingRequestStore(
  'opensquilla.planImplementationRecovery.v1',
  (value): value is PendingImplementation => {
    const pending = value as Partial<PendingImplementation> | null
    return !!pending && typeof pending.clientRequestId === 'string'
      && typeof pending.targetSessionKey === 'string'
  },
)

export function planImplementationIdentity(
  sessionKey: string, epoch: number, revisionId: string, inNewSession: boolean,
): string {
  return JSON.stringify([sessionKey, epoch, revisionId, inNewSession])
}

export function recoverPlanImplementation(
  identity: string, createTargetSessionKey: () => string,
): PendingImplementation {
  return requests.recover(identity, () => ({
    clientRequestId: createClientRequestId(),
    targetSessionKey: createTargetSessionKey(),
  }))
}

export function forgetPlanImplementation(identity: string) {
  requests.forget(identity)
}
