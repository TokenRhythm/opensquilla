import type { UsageSession, UsageSnapshot } from '@/types/usage'

/** Reuse the existing table shape without adding parent totals to child rows. */
export function usageTaskRows(snapshot: UsageSnapshot | null): UsageSession[] {
  if (!snapshot) return []
  if (snapshot.taskGroups === undefined) return snapshot.sessions
  return snapshot.taskGroups.map(group => {
    const totals = group.totals
    return {
      session: group.sessionKey || group.rootSessionId,
      sessionKey: group.sessionKey || '',
      sessionId: group.rootSessionId,
      sessionEpoch: group.rootSessionEpoch,
      taskGroupId: group.taskGroupId,
      sessionKind: 'root',
      title: group.title,
      attributionStatus: group.attributionStatus,
      startedAt: group.firstUsageAtMs ?? undefined,
      updatedAt: group.lastUsageAtMs ?? undefined,
      inputTokens: totals.input,
      outputTokens: totals.output,
      cacheReadTokens: totals.cacheRead,
      cacheWriteTokens: totals.cacheWrite,
      costUsd: totals.cost,
      costUsdExact: totals.costUsdExact,
      billedCostUsd: totals.billedCost,
      estimatedCostUsd: totals.estimatedCost,
      estimatedEventCount: totals.estimatedEventCount,
      missingCostEntries: totals.missingCostEntries,
      costSource: totals.costSource,
      costSourceCounts: totals.costSourceCounts,
      costEphemeral: false,
      estimateBasis: '',
      model: group.modelBreakdown?.length === 1 ? group.modelBreakdown[0].model || '' : '',
      modelBreakdown: group.modelBreakdown || [],
      nativeBilledByCurrency: totals.nativeBilledByCurrency || {},
      pendingBillingReceiptCount: totals.pendingBillingReceiptCount || 0,
      nativeBillingExpectedReceiptCount: totals.nativeBillingExpectedReceiptCount || 0,
      nativeBillingMissingConfirmedReceiptCount: totals.nativeBillingMissingConfirmedReceiptCount || 0,
    }
  })
}
