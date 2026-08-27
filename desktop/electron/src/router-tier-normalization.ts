export interface RouterTier {
  provider: string
  model: string
  description?: string
  supportsImage?: boolean
  imageOnly?: boolean
  thinkingLevel?: string
  ensembleEnabled?: boolean
}

const LEGACY_TEXT_TIER_ALIASES: Record<string, string> = {
  t0: 'c0',
  t1: 'c1',
  t2: 'c2',
  t3: 'c3',
}

function canonicalTierKey(name: string): string {
  return LEGACY_TEXT_TIER_ALIASES[name] ?? name
}

function cloneRouterTiers(tiers: Record<string, RouterTier>): Record<string, RouterTier> {
  return Object.fromEntries(Object.entries(tiers).map(([name, tier]) => [name, { ...tier }]))
}

function normalizeBooleanSetting(raw: unknown, fallback: boolean): boolean {
  if (typeof raw === 'boolean') return raw
  if (typeof raw === 'number') return raw !== 0
  if (typeof raw === 'string') {
    const value = raw.trim().toLowerCase()
    if (['1', 'true', 'yes', 'on'].includes(value)) return true
    if (['0', 'false', 'no', 'off', ''].includes(value)) return false
  }
  return fallback
}

export function normalizeRouterTiers(
  raw: unknown,
  fallback: Record<string, RouterTier>,
): Record<string, RouterTier> {
  if (!raw || typeof raw !== 'object') return cloneRouterTiers(fallback)
  const source = raw as Record<string, unknown>
  const out = cloneRouterTiers(fallback)
  // A persisted tier ladder owns its ensemble opt-in. Clear the fallback
  // value before overlaying legacy credentials so upgrading a Desktop build
  // cannot silently turn an existing single-model C3 into B5 fusion.
  for (const tier of Object.values(out)) delete tier.ensembleEnabled
  for (const [rawName, value] of Object.entries(source)) {
    if (!value || typeof value !== 'object') continue
    const name = canonicalTierKey(rawName)
    // Tier keys are emitted raw into TOML table headers. Drop unsafe keys and
    // retain the profile fallback rather than generating invalid TOML.
    if (!/^[A-Za-z0-9_-]+$/.test(name)) continue
    const tier = value as Record<string, unknown>
    const provider = String(tier.provider || out[name]?.provider || '').trim()
    const model = String(tier.model || out[name]?.model || '').trim()
    if (!provider || !model) continue
    const hasEnsembleEnabled = Object.prototype.hasOwnProperty.call(tier, 'ensembleEnabled')
      || Object.prototype.hasOwnProperty.call(tier, 'ensemble_enabled')
    const ensembleEnabled = tier.ensembleEnabled ?? tier.ensemble_enabled
    out[name] = {
      ...out[name],
      provider,
      model,
      description: String(tier.description || out[name]?.description || ''),
      supportsImage: Boolean(tier.supportsImage ?? tier.supports_image ?? out[name]?.supportsImage),
      imageOnly: Boolean(tier.imageOnly ?? tier.image_only ?? out[name]?.imageOnly),
      thinkingLevel: String(tier.thinkingLevel ?? tier.thinking_level ?? out[name]?.thinkingLevel ?? ''),
      ...(hasEnsembleEnabled
        ? { ensembleEnabled: normalizeBooleanSetting(ensembleEnabled, false) }
        : {}),
    }
  }
  return out
}
