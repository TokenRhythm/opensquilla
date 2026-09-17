import { IMAGE_TIER, TEXT_TIERS, normalizeRouterTier } from '@/utils/chat/routerTiers'

export type RouterSnapshotRequestKind = 'text' | 'image'
export type RouterSnapshotExecutionKind = 'single_model' | 'ensemble'

export interface RouterTierSnapshotEntryV1 {
  tier: string
  provider?: string
  model: string
  execution_kind: RouterSnapshotExecutionKind
}

export interface RouterTierSnapshotV1 {
  version: 1
  request_kind: RouterSnapshotRequestKind
  tiers: RouterTierSnapshotEntryV1[]
}

const ALLOWED_TIERS = new Set<string>([...TEXT_TIERS, IMAGE_TIER])
const MAX_SNAPSHOT_TIERS = 8

/** Atomically validate a persisted router candidate snapshot. */
export function normalizeRouterTierSnapshot(value: unknown): RouterTierSnapshotV1 | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const source = value as Record<string, unknown>
  if (source.version !== 1) return null
  if (source.request_kind !== 'text' && source.request_kind !== 'image') return null
  if (!Array.isArray(source.tiers) || source.tiers.length < 1 || source.tiers.length > MAX_SNAPSHOT_TIERS) {
    return null
  }

  const seen = new Set<string>()
  const tiers: RouterTierSnapshotEntryV1[] = []
  for (const rawEntry of source.tiers) {
    if (!rawEntry || typeof rawEntry !== 'object' || Array.isArray(rawEntry)) return null
    const entry = rawEntry as Record<string, unknown>
    const tier = normalizeRouterTier(entry.tier)
    const model = typeof entry.model === 'string' ? entry.model.trim() : ''
    const provider = entry.provider === undefined
      ? undefined
      : typeof entry.provider === 'string' ? entry.provider.trim() : null
    const executionKind = entry.execution_kind
    if (
      !ALLOWED_TIERS.has(tier)
      || seen.has(tier)
      || !model
      || provider === null
      || provider === ''
      || (executionKind !== 'single_model' && executionKind !== 'ensemble')
    ) return null
    seen.add(tier)
    tiers.push({
      tier,
      ...(provider ? { provider } : {}),
      model,
      execution_kind: executionKind,
    })
  }

  return {
    version: 1,
    request_kind: source.request_kind,
    tiers,
  }
}
