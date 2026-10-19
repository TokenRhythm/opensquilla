import type { DeliveryWalRecord } from './pendingInputWal'

/** Retirement is request proof, never permission to recreate an absent session. */
export function retiredMissingSessionCandidate(
  records: readonly DeliveryWalRecord[], identity: string, sessionKey: string,
): DeliveryWalRecord | null {
  const scoped = records.filter(record => record.deliveryIdentity === identity
    && record.requestSessionKey === sessionKey)
  if (scoped.some(record => ['prepared', 'submitting', 'unknown'].includes(record.phase))) return null
  const retired = scoped.filter(record => record.retired && record.phase === 'not-sent'
    && record.request?.kind === 'send' && record.request.request.kind === 'new-turn')
    .sort((left, right) => right.createdAt - left.createdAt)[0]
  if (!retired) return null
  // A newer accepted send supersedes the restored draft. Ties are ambiguous,
  // so a positive receipt always wins over old negative proof.
  if (scoped.some(record => record.ownerRequestId !== retired.ownerRequestId
    && record.createdAt >= retired.createdAt && !record.retired)) return null
  return retired
}
