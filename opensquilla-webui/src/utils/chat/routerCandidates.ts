import {
  IMAGE_TIER,
  normalizeRouterTier,
  normalizeRouterTierMapping,
  sortRouterTiers,
} from '@/utils/chat/routerTiers'
import {
  normalizeRouterTierSnapshot,
  type RouterSnapshotExecutionKind,
} from '@/utils/chat/routerTierSnapshot'

export interface RouterCandidateTierConfig {
  model?: string
  provider?: string
  imageOnly?: boolean
  ensembleEnabled?: boolean
}

export interface RouterCandidateConfiguration {
  slots: readonly string[]
  models: Readonly<Record<string, string>>
  configs: Readonly<Record<string, RouterCandidateTierConfig>>
}

export interface RouterCandidate {
  tier: string
  tiers: string[]
  model: string
  provider?: string
  executionKind: RouterSnapshotExecutionKind
}

interface RouterCandidateEntry {
  tier: string
  model: string
  provider?: string
  executionKind: RouterSnapshotExecutionKind
}

export interface RouterCandidateResolution {
  source: 'snapshot' | 'configuration'
  candidates: RouterCandidate[]
}

/** Resolve accepted candidates before applying labels or visual layouts. */
export function resolveRouterCandidates(
  options: RouterCandidateConfiguration & { tier: string; model: string; snapshot?: unknown },
): RouterCandidateResolution {
  const winnerTier = normalizeRouterTier(options.tier)
  const winnerModel = options.model.trim()
  const snapshot = normalizeRouterTierSnapshot(options.snapshot)
  // History can supply image context without current user attachments. The
  // accepted snapshot owns both its request kind and recorded candidate order.
  const entries: RouterCandidateEntry[] = snapshot
    ? snapshot.tiers
      .filter(entry => entry.tier !== IMAGE_TIER || entry.tier === winnerTier)
      .map(entry => ({
        tier: entry.tier,
        model: entry.model,
        ...(entry.provider ? { provider: entry.provider } : {}),
        executionKind: entry.execution_kind,
      }))
    : configuredEntries(options, winnerTier, winnerModel)

  const winnerEntry = entries.find(entry => entry.tier === winnerTier)
  if (winnerEntry && winnerModel) {
    winnerEntry.model = winnerModel
  } else if (!winnerEntry && winnerTier && winnerModel) {
    entries.push({ tier: winnerTier, model: winnerModel, executionKind: 'single_model' })
  }

  const candidates = new Map<string, RouterCandidate>()
  for (const entry of entries) {
    if (!entry.model) continue
    // Provider profiles are independent deployments even for the same model.
    const identity = JSON.stringify([
      entry.executionKind,
      (entry.provider || '').trim().toLowerCase(),
      entry.model,
    ])
    const existing = candidates.get(identity)
    if (existing) existing.tiers.push(entry.tier)
    else candidates.set(identity, { ...entry, tiers: [entry.tier] })
  }
  return { source: snapshot ? 'snapshot' : 'configuration', candidates: [...candidates.values()] }
}

export function hasConfiguredRouterCandidates(configuration: RouterCandidateConfiguration): boolean {
  return configuredEntries(configuration).length > 0
}

function configuredEntries(
  configuration: RouterCandidateConfiguration,
  winnerTier = '',
  winnerModel = '',
): RouterCandidateEntry[] {
  const configs = normalizeRouterTierMapping(configuration.configs)
  const models = normalizeRouterTierMapping(configuration.models)
  const configuredTiers = configuration.slots.length
    ? [...configuration.slots]
    : Object.keys(configs)
  if (winnerTier) configuredTiers.push(winnerTier)

  return sortRouterTiers(configuredTiers).flatMap((tier): RouterCandidateEntry[] => {
    if (tier === IMAGE_TIER && tier !== winnerTier) return []
    const config = configs[tier]
    if (tier !== winnerTier && config?.imageOnly) return []
    const model = (tier === winnerTier && winnerModel
      ? winnerModel
      : config?.model || models[tier] || '').trim()
    if (!model) return []
    const provider = config?.provider?.trim()
    return [{
      tier,
      model,
      ...(provider ? { provider } : {}),
      executionKind: config?.ensembleEnabled === true ? 'ensemble' : 'single_model',
    }]
  })
}
