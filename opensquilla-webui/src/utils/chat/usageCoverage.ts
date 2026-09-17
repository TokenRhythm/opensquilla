import type { ChatMessageMeta } from '@/types/chat'

type UsageCoverageTranslator = (
  key: string,
  named?: Record<string, string | number>,
) => string

export function unknownUsageEventCount(meta: ChatMessageMeta): number {
  const count = Number(meta.unknownUsageEvents)
  return Number.isFinite(count) ? Math.max(0, Math.floor(count)) : 0
}

export function hasIncompleteUsageCoverage(meta: ChatMessageMeta): boolean {
  const coverageStatus = String(meta.coverageStatus || '').trim().toLowerCase()
  return meta.usageUnknown === true
    || unknownUsageEventCount(meta) > 0
    || Boolean(coverageStatus && coverageStatus !== 'complete')
}

export function hasKnownUsageSubtotal(meta: ChatMessageMeta): boolean {
  if (typeof meta.hasKnownUsage === 'boolean') return meta.hasKnownUsage
  if (
    meta.input > 0
    || meta.output > 0
    || meta.cachedTokens > 0
    || meta.reasoningTokens > 0
    || meta.costUsd > 0
  ) return true
  return Boolean(meta.ensemble && (
    meta.ensemble.costUsd > 0
    || meta.ensemble.models.some(member =>
      member.input > 0 || member.output > 0 || member.costUsd > 0,
    )
  ))
}

export function usageCoverageText(
  meta: ChatMessageMeta,
  translate: UsageCoverageTranslator,
): string {
  if (!hasIncompleteUsageCoverage(meta)) return ''
  const baseKey = hasKnownUsageSubtotal(meta)
    ? 'chat.msgMeta.usageIncompleteSubtotal'
    : 'chat.msgMeta.usageIncompleteUnknown'
  const base = translate(baseKey)
  const count = unknownUsageEventCount(meta)
  if (count === 0) return base
  return `${base} · ${translate('chat.msgMeta.unknownProviderCalls', { count })}`
}
