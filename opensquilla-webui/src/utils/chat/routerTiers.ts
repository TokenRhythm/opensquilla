import {
  DEFAULT_TEXT_TIER,
  IMAGE_TIER,
  LEGACY_TEXT_TIER_ALIASES,
  TEXT_TIERS,
} from '@/types/generated/router_tier_contract'

export { DEFAULT_TEXT_TIER, IMAGE_TIER, TEXT_TIERS } from '@/types/generated/router_tier_contract'

export function normalizeRouterTextTier(value: unknown): string {
  const raw = String(value || '').trim().toLowerCase()
  if (!raw) return ''
  if ((TEXT_TIERS as readonly string[]).includes(raw)) return raw
  return LEGACY_TEXT_TIER_ALIASES[raw] || ''
}

export function normalizeRouterTier(value: unknown): string {
  const raw = String(value || '').trim()
  if (!raw) return ''
  if (raw === IMAGE_TIER) return IMAGE_TIER
  return normalizeRouterTextTier(raw) || raw.toLowerCase()
}

export function routerTierIndex(value: unknown): number {
  const tier = normalizeRouterTextTier(value)
  return tier ? TEXT_TIERS.indexOf(tier as (typeof TEXT_TIERS)[number]) : -1
}

export function sortRouterTiers(list: string[]): string[] {
  const seen = new Set<string>()
  const tiers = list
    .map(normalizeRouterTier)
    .filter(Boolean)
    .filter((tier) => {
      if (seen.has(tier)) return false
      seen.add(tier)
      return true
    })

  return tiers.sort((a, b) => {
    const ai = routerTierIndex(a)
    const bi = routerTierIndex(b)
    if (ai >= 0 && bi >= 0) return ai - bi
    if (ai >= 0) return -1
    if (bi >= 0) return 1
    if (a === IMAGE_TIER && b !== IMAGE_TIER) return 1
    if (b === IMAGE_TIER && a !== IMAGE_TIER) return -1
    return a.localeCompare(b)
  })
}

export function routerTierLabel(tier: string): string {
  const normalized = normalizeRouterTier(tier)
  return normalized || DEFAULT_TEXT_TIER
}

export function routerTierLabelKey(tier: string): string {
  const normalized = normalizeRouterTier(tier)
  return `setup.router.tiers.${normalized || DEFAULT_TEXT_TIER}`
}
