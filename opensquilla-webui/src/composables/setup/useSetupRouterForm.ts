import { computed, ref, type ComputedRef } from 'vue'
import i18n from '@/i18n'
import {
  DEFAULT_TEXT_TIER,
  IMAGE_TIER,
  normalizeRouterTier,
} from '@/utils/chat/routerTiers'
import {
  DEFAULT_ROUTER_VISUAL_MODE,
  normalizeRouterVisualMode,
  type RouterVisualMode,
} from '@/utils/chat/routerVisualMode'
import type { DiscoveredModelsByProvider } from '@/composables/setup/useSetupProviderForm'

export interface SetupTierValue {
  provider: string
  model: string
  thinkingLevel: string
  supportsImage: boolean
}

export interface SetupTierRow extends SetupTierValue {
  name: string
}

export interface SetupProviderOption {
  providerId: string
  label: string
  disabled?: boolean
}

export interface SetupProviderCredentialStatus {
  provider: string
  available: boolean
  source?: string
  envKey?: string
  reason?: string
}

export type RouterConfigDisabledReason = 'single-model' | 'ensemble' | null
export type VisibleRouterModeChoice = 'router' | 'single'
export type TierTemplateState = 'recommended' | 'custom' | 'disabled'

const ROUTER_VISUAL_MODE_VALUES: readonly RouterVisualMode[] = ['real_candidates', 'legacy_grid']

function routerVisualModeOptions(): Array<{ value: RouterVisualMode; label: string }> {
  return ROUTER_VISUAL_MODE_VALUES.map((value) => ({
    value,
    label: i18n.global.t(`setup.router.visualMode.${value}`),
  }))
}

export function buildRouterPayload(
  mode: string,
  defaultTier: string,
  tierValues: Record<string, SetupTierValue>,
): Record<string, unknown> {
  const tiers: Record<string, Record<string, unknown>> = {}
  Object.entries(tierValues).forEach(([name, tier]) => {
    const tierName = normalizeRouterTier(name) || name
    tiers[tierName] = {
      provider: tier.provider,
      model: tier.model,
      thinkingLevel: tier.thinkingLevel,
      supportsImage: tier.supportsImage,
    }
  })
  return { mode, defaultTier: normalizeRouterTier(defaultTier) || DEFAULT_TEXT_TIER, tiers }
}

interface TierConfig {
  provider?: string
  model?: string
  thinkingLevel?: string
  thinking_level?: string
  supportsImage?: boolean
  supports_image?: boolean
}

interface RouterConfig {
  enabled?: boolean
  preset_binding?: 'follow_primary' | 'custom'
  default_tier?: string
  visual_mode?: string
  tier_profile?: string | null
  cross_provider_tiers?: boolean
  tier_provider_mismatch?: string
  tiers?: Record<string, TierConfig>
}

export type RouterBinding = 'follow_primary' | 'custom' | 'legacy'

interface RouterPanelContext {
  routerSummary: ComputedRef<string>
  ensembleProfileActive: ComputedRef<boolean>
  hasSavedProvider: ComputedRef<boolean>
  isOpenrouter: ComputedRef<boolean>
  textTiers: readonly string[]
  tierLabel: (tier: string) => string
  // Optional provider-scoped catalogs so mixed-provider tier rows never share
  // model ids. Missing/empty catalogs keep the existing free-text input.
  discoveredModelsByProvider?: ComputedRef<DiscoveredModelsByProvider>
  providerOptions?: ComputedRef<SetupProviderOption[]>
  providerCredentialStatus?: ComputedRef<SetupProviderCredentialStatus[]>
}

export function useSetupRouterForm() {
  const routerMode = ref('recommended')
  const routerDefaultTier = ref(DEFAULT_TEXT_TIER)
  const routerVisualMode = ref<RouterVisualMode>(DEFAULT_ROUTER_VISUAL_MODE)
  const tierValues = ref<Record<string, SetupTierValue>>({})
  const activeProvider = ref('')
  const savedBinding = ref<RouterBinding>('legacy')
  const crossProviderTiers = ref(false)
  const tierProviderMismatch = ref<'route' | 'veto'>('route')
  const mode = computed(() => routerMode.value)
  const defaultTier = computed(() => routerDefaultTier.value)
  const routerModeChoice = computed(() =>
    routerMode.value === 'disabled'
      ? 'disabled'
      : 'recommended',
  )
  const visibleModeChoice = computed<VisibleRouterModeChoice>(() =>
    routerMode.value === 'disabled' ? 'single' : 'router',
  )
  const tierProviderIds = computed(() => {
    const ids = new Set<string>()
    Object.values(tierValues.value).forEach((tier) => {
      const provider = String(tier.provider || '').trim().toLowerCase()
      if (provider) ids.add(provider)
    })
    return ids
  })
  const hasMixedTierProviders = computed(() => {
    if (tierProviderIds.value.size > 1) return true
    const only = Array.from(tierProviderIds.value)[0] || ''
    return Boolean(only && activeProvider.value && only !== activeProvider.value.toLowerCase())
  })

  function routerConfigDisabledReason(ensembleProfileActive: boolean): RouterConfigDisabledReason {
    if (ensembleProfileActive) return 'ensemble'
    if (routerMode.value === 'disabled') return 'single-model'
    return null
  }

  const routerSerialized = computed(() => JSON.stringify({ m: routerMode.value, d: routerDefaultTier.value, t: tierValues.value }))
  // Seed from the initial state so the pristine form is never dirty while config loads.
  const routerBaseline = ref(routerSerialized.value)
  const visualModeBaseline = ref(routerVisualMode.value)
  const routingDirty = computed(() => routerSerialized.value !== routerBaseline.value)
  const visualModeDirty = computed(() => routerVisualMode.value !== visualModeBaseline.value)
  const tierTemplateState = computed<TierTemplateState>(() => {
    if (routerMode.value === 'disabled') return 'disabled'
    if (hasMixedTierProviders.value) return 'custom'
    if (routerMode.value === 'openrouter-mix') return 'custom'
    if (routerMode.value === 'recommended' && !routingDirty.value) return 'recommended'
    return 'custom'
  })
  const isDirty = computed(() => routingDirty.value || visualModeDirty.value)

  function initFromConfig(
    router: RouterConfig,
    profileTiers: Record<string, TierConfig>,
    provider = '',
    statusBinding?: RouterBinding,
  ) {
    activeProvider.value = provider.toLowerCase()
    crossProviderTiers.value = router.cross_provider_tiers === true
    tierProviderMismatch.value = router.tier_provider_mismatch === 'veto' ? 'veto' : 'route'
    // Ownership is a server contract, not a shape inferred from tier_profile.
    // Missing ownership is an historical/older-Gateway config and must be
    // treated conservatively: editing it may explicitly adopt `custom`, but it
    // must never silently become a follow-primary preset.
    const binding: RouterBinding = statusBinding
      || router.preset_binding
      || 'legacy'
    savedBinding.value = binding
    if (router.enabled === false) {
      routerMode.value = 'disabled'
    } else if (binding === 'follow_primary') {
      routerMode.value = 'recommended'
    } else if (binding === 'legacy' && provider.toLowerCase() === 'openrouter' && !router.tier_profile) {
      routerMode.value = 'openrouter-mix'
    } else {
      routerMode.value = 'custom'
    }
    routerDefaultTier.value = normalizeRouterTier(router.default_tier || '') || DEFAULT_TEXT_TIER
    routerVisualMode.value = normalizeRouterVisualMode(router.visual_mode)

    const hasProfileTiers = Object.keys(profileTiers || {}).length > 0
    const tiers = binding === 'follow_primary' && hasProfileTiers
      ? { ...profileTiers }
      : Object.assign({}, profileTiers || {}, router.tiers || {})
    const next: Record<string, SetupTierValue> = {}
    Object.entries(tiers).forEach(([name, tier]) => {
      const tierName = normalizeRouterTier(name) || name
      next[tierName] = {
        provider: tier.provider || '',
        model: tier.model || '',
        thinkingLevel: tier.thinkingLevel || tier.thinking_level || '',
        supportsImage: tier.supportsImage || tier.supports_image || false,
      }
    })
    tierValues.value = next
    routerBaseline.value = routerSerialized.value
    visualModeBaseline.value = routerVisualMode.value
  }

  function updateTierField(name: string, key: keyof SetupTierValue, value: string | boolean) {
    const tier = tierValues.value[name]
    if (!tier) return
    if (key === 'provider') {
      const provider = String(value || '').trim().toLowerCase()
      const currentProvider = String(tier.provider || '').trim().toLowerCase()
      if (provider === currentProvider) return
      // Provider and model form one routing identity. Replace the whole row in
      // one reactive assignment so no observer can see a foreign model id
      // paired with the newly selected provider.
      tierValues.value = {
        ...tierValues.value,
        [name]: { ...tier, provider, model: '' },
      }
      return
    }
    if (key === 'supportsImage') {
      tier.supportsImage = Boolean(value)
    } else {
      tier[key] = String(value)
    }
  }

  function tierRows(textTiers: readonly string[]): SetupTierRow[] {
    return Object.entries(tierValues.value)
      .filter(([name]) => textTiers.includes(name) || name === IMAGE_TIER)
      .map(([name, tier]) => ({
        name,
        provider: tier.provider,
        model: tier.model,
        thinkingLevel: tier.thinkingLevel,
        supportsImage: tier.supportsImage,
      }))
  }

  function setRouterMode(value: string) {
    routerMode.value = value
  }

  function enableFromSavedBinding() {
    routerMode.value = savedBinding.value === 'follow_primary' ? 'recommended' : 'custom'
  }

  function setRouterDefaultTier(value: string) {
    routerDefaultTier.value = normalizeRouterTier(value) || DEFAULT_TEXT_TIER
  }

  function setRouterVisualMode(value: string) {
    routerVisualMode.value = normalizeRouterVisualMode(value)
  }

  function payload(): Record<string, unknown> {
    const mode = routerMode.value === 'disabled'
      ? 'disabled'
      : hasMixedTierProviders.value
        ? 'custom'
        : routerMode.value === 'recommended'
          ? 'recommended'
          : 'custom'
    const body = buildRouterPayload(mode, routerDefaultTier.value, tierValues.value)
    if (hasMixedTierProviders.value) {
      body.crossProviderTiers = true
      body.tierProviderMismatch = 'veto'
    } else if (crossProviderTiers.value) {
      body.crossProviderTiers = true
      body.tierProviderMismatch = tierProviderMismatch.value
    }
    return body
  }

  function visualModePatches(): Record<string, unknown> {
    if (!visualModeDirty.value) return {}
    return { 'squilla_router.visual_mode': routerVisualMode.value }
  }

  function createPanel(context: RouterPanelContext) {
    return computed(() => {
      const disabledReason = routerConfigDisabledReason(context.ensembleProfileActive.value)
      return {
        routerSummary: context.routerSummary.value,
        ensembleProfileActive: context.ensembleProfileActive.value,
        routerMode: routerMode.value,
        routerModeChoice: routerModeChoice.value,
        routerConfigDisabled: disabledReason !== null,
        routerConfigDisabledReason: disabledReason,
        routerDefaultTier: routerDefaultTier.value,
        routerVisualMode: routerVisualMode.value,
        routerVisualModeDirty: visualModeDirty.value,
        routerVisualModeOptions: routerVisualModeOptions(),
        hasSavedProvider: context.hasSavedProvider.value,
        textTiers: context.textTiers,
        tierRows: tierRows(context.textTiers),
        tierLabel: context.tierLabel,
        hasMixedTierProviders: hasMixedTierProviders.value,
        discoveredModelsByProvider: context.discoveredModelsByProvider?.value ?? {},
        providerOptions: context.providerOptions?.value ?? [],
        providerCredentialStatus: context.providerCredentialStatus?.value ?? [],
      }
    })
  }

  return {
    mode,
    defaultTier,
    visibleModeChoice,
    tierTemplateState,
    hasMixedTierProviders,
    routingDirty,
    visualModeDirty,
    isDirty,
    initFromConfig,
    setRouterMode,
    enableFromSavedBinding,
    setRouterDefaultTier,
    setRouterVisualMode,
    updateTierField,
    payload,
    visualModePatches,
    createPanel,
  }
}
