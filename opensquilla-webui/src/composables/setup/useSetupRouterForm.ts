import { computed, ref, type ComputedRef } from 'vue'
import i18n from '@/i18n'
import {
  DEFAULT_TEXT_TIER,
  IMAGE_TIER,
  TEXT_TIERS,
  normalizeRouterTier,
} from '@/utils/chat/routerTiers'
import {
  DORMANT_SHARED_SELECTION_MODES,
  ROUTER_DYNAMIC_SELECTION_MODE,
} from '@/types/generated/router_tier_contract'
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
  ensembleEnabled?: boolean
  ensembleSelectionMode?: string
}

export type RouterTierProviderRole = 'direct' | 'dormant_draft' | 'dynamic_member' | 'blocked'
export type RouterProviderRoles = Record<string, RouterTierProviderRole>

export interface TierEnsembleRuntimeStatus {
  selectionMode: string
  activationTiers: string[]
  tierSelectionModes: Record<string, string>
  runtimeStatus: string
  configurationReady: boolean | null
  blockedReason: string
  blockedTierCandidates: Array<Record<string, unknown>>
  fixedFallbackReady: boolean | null
  fixedFallbackBlockedReason: string
  proposerCount: number | null
  configuredMinSuccessfulProposers: number | null
  effectiveMinSuccessfulProposers: number | null
  configuredProposerMaxRetries: number | null
  effectiveProposerMaxRetries: number | null
  proposerMaxRetriesSource: string
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
    const sharedEnsembleTier = tierName === 'c3'
    const tierPayload: Record<string, unknown> = {
      provider: tier.provider,
      model: tier.model,
      thinkingLevel: tier.thinkingLevel,
      supportsImage: tier.supportsImage,
    }
    if (sharedEnsembleTier && typeof tier.ensembleEnabled === 'boolean') {
      tierPayload.ensembleEnabled = tier.ensembleEnabled
    }
    if (
      tier.ensembleSelectionMode
      || (sharedEnsembleTier && typeof tier.ensembleEnabled === 'boolean')
    ) {
      tierPayload.ensembleSelectionMode = tier.ensembleSelectionMode || ''
    }
    tiers[tierName] = tierPayload
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
  ensembleEnabled?: boolean
  ensemble_enabled?: boolean
  ensembleSelectionMode?: string
  ensemble_selection_mode?: string
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

export interface RouterRoutingModeState {
  mode: string
  sharedSelectionMode: string
  ensembleGloballyEnabled: boolean
  providerRoleContextDirty: boolean
}

function normalizeRouterProviderRole(value: unknown): RouterTierProviderRole {
  const raw = String(
    value && typeof value === 'object'
      ? (value as Record<string, unknown>).role || (value as Record<string, unknown>).providerRole
      : value || '',
  ).trim().toLowerCase()
  if (raw === 'dormant_draft' || raw === 'dormant') return 'dormant_draft'
  if (raw === 'dynamic_member' || raw === 'dynamic' || raw === 'plan_anchor') return 'dynamic_member'
  if (raw === 'blocked') return 'blocked'
  return 'direct'
}

export function normalizeRouterProviderRoles(value: unknown): RouterProviderRoles {
  const out: RouterProviderRoles = {}
  if (Array.isArray(value)) {
    for (const entry of value) {
      if (!entry || typeof entry !== 'object') continue
      const row = entry as Record<string, unknown>
      const name = normalizeRouterTier(String(row.tier || row.name || '')) || String(row.tier || row.name || '')
      if (name) out[name] = normalizeRouterProviderRole(row.role || row.providerRole)
    }
    return out
  }
  if (!value || typeof value !== 'object') return out
  for (const [name, role] of Object.entries(value as Record<string, unknown>)) {
    const tier = normalizeRouterTier(name) || name
    if (tier) out[tier] = normalizeRouterProviderRole(role)
  }
  return out
}

function normalizeTierEnsembleRuntimeStatus(value: unknown): TierEnsembleRuntimeStatus | null {
  if (!value || typeof value !== 'object') return null
  const raw = value as Record<string, unknown>
  const runtimeStatus = String(raw.runtimeStatus ?? raw.runtime_status ?? '').trim().toLowerCase()
  if (!runtimeStatus) return null
  const configurationReady = raw.configurationReady ?? raw.configuration_ready
  const fixedFallbackReady = raw.fixedFallbackReady ?? raw.fixed_fallback_ready
  const blockedTierCandidates = raw.blockedTierCandidates ?? raw.blocked_tier_candidates
  const activationTiers = raw.activationTiers ?? raw.activation_tiers
  const configuredProposerMaxRetries = Number(
    raw.configuredProposerMaxRetries ?? raw.configured_proposer_max_retries,
  )
  const effectiveProposerMaxRetries = Number(
    raw.effectiveProposerMaxRetries ?? raw.effective_proposer_max_retries,
  )
  const proposerCount = Number(raw.proposerCount ?? raw.proposer_count)
  const configuredMinSuccessfulProposers = Number(
    raw.configuredMinSuccessfulProposers ?? raw.configured_min_successful_proposers,
  )
  const effectiveMinSuccessfulProposers = Number(
    raw.effectiveMinSuccessfulProposers ?? raw.effective_min_successful_proposers,
  )
  const rawTierSelectionModes = raw.tierSelectionModes ?? raw.tier_selection_modes
  const tierSelectionModes: Record<string, string> = {}
  if (rawTierSelectionModes && typeof rawTierSelectionModes === 'object' && !Array.isArray(rawTierSelectionModes)) {
    for (const [name, mode] of Object.entries(rawTierSelectionModes as Record<string, unknown>)) {
      const tier = normalizeRouterTier(name) || name.trim().toLowerCase()
      const selectionMode = String(mode || '').trim()
      if (tier && selectionMode) tierSelectionModes[tier] = selectionMode
    }
  }
  return {
    selectionMode: String(raw.selectionMode ?? raw.selection_mode ?? '').trim(),
    activationTiers: Array.isArray(activationTiers)
      ? activationTiers.map(tier => normalizeRouterTier(String(tier)) || String(tier)).filter(Boolean)
      : [],
    tierSelectionModes,
    runtimeStatus,
    configurationReady: typeof configurationReady === 'boolean' ? configurationReady : null,
    blockedReason: String(raw.blockedReason ?? raw.blocked_reason ?? '').trim(),
    blockedTierCandidates: Array.isArray(blockedTierCandidates)
      ? blockedTierCandidates.filter(row => row && typeof row === 'object') as Array<Record<string, unknown>>
      : [],
    fixedFallbackReady: typeof fixedFallbackReady === 'boolean' ? fixedFallbackReady : null,
    fixedFallbackBlockedReason: String(
      raw.fixedFallbackBlockedReason ?? raw.fixed_fallback_blocked_reason ?? '',
    ).trim(),
    proposerCount: Number.isFinite(proposerCount)
      ? Math.max(1, Math.trunc(proposerCount))
      : null,
    configuredMinSuccessfulProposers: Number.isFinite(configuredMinSuccessfulProposers)
      ? Math.max(1, Math.trunc(configuredMinSuccessfulProposers))
      : null,
    effectiveMinSuccessfulProposers: Number.isFinite(effectiveMinSuccessfulProposers)
      ? Math.max(1, Math.trunc(effectiveMinSuccessfulProposers))
      : null,
    configuredProposerMaxRetries: Number.isFinite(configuredProposerMaxRetries)
      ? Math.max(0, Math.trunc(configuredProposerMaxRetries))
      : null,
    effectiveProposerMaxRetries: Number.isFinite(effectiveProposerMaxRetries)
      ? Math.max(0, Math.trunc(effectiveProposerMaxRetries))
      : null,
    proposerMaxRetriesSource: String(
      raw.proposerMaxRetriesSource ?? raw.proposer_max_retries_source ?? '',
    ).trim(),
  }
}

export function routerTierProviderRole(
  name: string,
  _tier: SetupTierValue,
  roles: RouterProviderRoles,
): RouterTierProviderRole {
  // The Gateway owns the complete mode-aware role calculation, including the
  // case where router_dynamic consumes every text tier as a lineup member.
  // Older Gateways omit the additive map, so a missing entry intentionally
  // remains the conservative execution dependency (`direct`).
  return roles[normalizeRouterTier(name) || name] || 'direct'
}

export function routerTierProviderParticipates(
  name: string,
  tier: SetupTierValue,
  roles: RouterProviderRoles,
): boolean {
  const role = routerTierProviderRole(name, tier, roles)
  return role === 'direct' || role === 'dynamic_member'
}

function deriveDraftRouterProviderRoles(
  tiers: Record<string, SetupTierValue>,
  sharedSelectionMode: string,
  ensembleGloballyEnabled: boolean,
): RouterProviderRoles {
  const selectionMode = String(sharedSelectionMode || '').trim()
  const c3 = tiers.c3
  const dynamicMembersActive = (
    selectionMode === ROUTER_DYNAMIC_SELECTION_MODE
    && (ensembleGloballyEnabled || c3?.ensembleEnabled === true)
  ) || Object.values(tiers).some(tier => (
    tier.ensembleEnabled === undefined
    && tier.ensembleSelectionMode === ROUTER_DYNAMIC_SELECTION_MODE
  ))

  const out: RouterProviderRoles = {}
  for (const [name, tier] of Object.entries(tiers)) {
    const normalized = normalizeRouterTier(name) || name
    if (dynamicMembersActive && (TEXT_TIERS as readonly string[]).includes(normalized)) {
      out[normalized] = 'dynamic_member'
    } else if (tier.ensembleEnabled === undefined && tier.ensembleSelectionMode) {
      // Retained pre-boolean tier plans still own their routed turn. Keep the
      // backend's legacy-first precedence when local shared-plan edits make us
      // recompute roles; only router_dynamic (handled above) consumes all text
      // tiers as dynamic members.
      out[normalized] = 'direct'
    } else if (
      ensembleGloballyEnabled
      && (TEXT_TIERS as readonly string[]).includes(normalized)
      && DORMANT_SHARED_SELECTION_MODES.includes(
        selectionMode as (typeof DORMANT_SHARED_SELECTION_MODES)[number],
      )
    ) {
      out[normalized] = 'dormant_draft'
    } else if (normalized === 'c3' && tier.ensembleEnabled === true) {
      out[normalized] = DORMANT_SHARED_SELECTION_MODES.includes(
        selectionMode as (typeof DORMANT_SHARED_SELECTION_MODES)[number],
      )
        ? 'dormant_draft'
        : 'blocked'
    } else {
      out[normalized] = 'direct'
    }
  }
  return out
}

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
  const persistedRouterProviderRoles = ref<RouterProviderRoles>({})
  const tierEnsembleStatus = ref<TierEnsembleRuntimeStatus | null>(null)
  const sharedSelectionMode = ref('')
  const ensembleGloballyEnabled = ref(false)
  const providerRoleContextDirty = ref(false)
  const routerProviderRoles = computed<RouterProviderRoles>(() => (
    providerRoleContextDirty.value
      ? deriveDraftRouterProviderRoles(
          tierValues.value,
          sharedSelectionMode.value,
          ensembleGloballyEnabled.value,
        )
      : persistedRouterProviderRoles.value
  ))
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
    Object.entries(tierValues.value).forEach(([name, tier]) => {
      if (!routerTierProviderParticipates(name, tier, routerProviderRoles.value)) return
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

  function refreshRuntimeMetadata(
    providerRoles?: unknown,
    currentTierEnsembleStatus?: unknown,
  ) {
    persistedRouterProviderRoles.value = normalizeRouterProviderRoles(providerRoles)
    tierEnsembleStatus.value = normalizeTierEnsembleRuntimeStatus(currentTierEnsembleStatus)
  }

  function initFromConfig(
    router: RouterConfig,
    profileTiers: Record<string, TierConfig>,
    provider = '',
    statusBinding?: RouterBinding,
    providerRoles?: unknown,
    currentSharedSelectionMode = '',
    currentEnsembleGloballyEnabled = false,
    currentTierEnsembleStatus?: unknown,
  ) {
    activeProvider.value = provider.toLowerCase()
    crossProviderTiers.value = router.cross_provider_tiers === true
    tierProviderMismatch.value = router.tier_provider_mismatch === 'veto' ? 'veto' : 'route'
    refreshRuntimeMetadata(providerRoles, currentTierEnsembleStatus)
    sharedSelectionMode.value = String(currentSharedSelectionMode || '').trim()
    ensembleGloballyEnabled.value = currentEnsembleGloballyEnabled === true
    providerRoleContextDirty.value = false
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
        ensembleEnabled: tierName === 'c3'
          ? typeof tier.ensembleEnabled === 'boolean'
            ? tier.ensembleEnabled
            : typeof tier.ensemble_enabled === 'boolean'
              ? tier.ensemble_enabled
              : undefined
          : undefined,
        ensembleSelectionMode:
          tier.ensembleSelectionMode || tier.ensemble_selection_mode || '',
      }
    })
    tierValues.value = next
    routerBaseline.value = routerSerialized.value
    visualModeBaseline.value = routerVisualMode.value
  }

  function updateTierField(name: string, key: keyof SetupTierValue, value: string | boolean) {
    const tier = tierValues.value[name]
    if (!tier) return
    if (key === 'ensembleEnabled' && (normalizeRouterTier(name) || name) !== 'c3') return
    if (key === 'provider') {
      const provider = String(value || '').trim().toLowerCase()
      const currentProvider = String(tier.provider || '').trim().toLowerCase()
      if (provider === currentProvider) return
      const dynamicMember = routerTierProviderRole(
        name,
        tier,
        routerProviderRoles.value,
      ) === 'dynamic_member'
      // Provider and model form one routing identity. Replace the whole row in
      // one reactive assignment so no observer can see a foreign model id
      // paired with the newly selected provider.
      tierValues.value = {
        ...tierValues.value,
        [name]: {
          ...tier,
          provider,
          model: '',
          ensembleEnabled: dynamicMember
            ? tier.ensembleEnabled
            : tier.ensembleEnabled === undefined
              ? undefined
              : false,
          ensembleSelectionMode: dynamicMember ? tier.ensembleSelectionMode : '',
        },
      }
      return
    }
    if (key === 'supportsImage') {
      tier.supportsImage = Boolean(value)
    } else if (key === 'ensembleEnabled') {
      tier.ensembleEnabled = Boolean(value)
    } else {
      tier[key] = String(value)
    }
    if (key === 'ensembleEnabled' || key === 'ensembleSelectionMode') {
      providerRoleContextDirty.value = true
    }
  }

  function setEnsembleContext(selectionMode: string, globallyEnabled: boolean) {
    const nextSelectionMode = String(selectionMode || '').trim()
    const nextGloballyEnabled = globallyEnabled === true
    if (
      sharedSelectionMode.value === nextSelectionMode
      && ensembleGloballyEnabled.value === nextGloballyEnabled
    ) return
    sharedSelectionMode.value = nextSelectionMode
    ensembleGloballyEnabled.value = nextGloballyEnabled
    providerRoleContextDirty.value = true
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
        ensembleEnabled: tier.ensembleEnabled,
        ensembleSelectionMode: tier.ensembleSelectionMode,
      }))
  }

  function setRouterMode(value: string) {
    routerMode.value = value
  }

  function captureRoutingModeState(): RouterRoutingModeState {
    return {
      mode: routerMode.value,
      sharedSelectionMode: sharedSelectionMode.value,
      ensembleGloballyEnabled: ensembleGloballyEnabled.value,
      providerRoleContextDirty: providerRoleContextDirty.value,
    }
  }

  function restoreRoutingModeState(state: RouterRoutingModeState) {
    routerMode.value = state.mode
    sharedSelectionMode.value = state.sharedSelectionMode
    ensembleGloballyEnabled.value = state.ensembleGloballyEnabled
    providerRoleContextDirty.value = state.providerRoleContextDirty
  }

  /**
   * The global direct/router/ensemble selector is persisted by
   * `models.routing.set`. Keep that acknowledged mode out of this form's
   * detailed-routing dirty state while retaining any unsaved tier edits.
   */
  function acceptRoutingModeChange() {
    try {
      const baseline = JSON.parse(routerBaseline.value) as Record<string, unknown>
      baseline.m = routerMode.value
      routerBaseline.value = JSON.stringify(baseline)
    } catch {
      // The baseline is internal JSON produced immediately above. This
      // fallback is defensive for corrupted devtools state and favors a
      // coherent form over repeatedly submitting an already-accepted mode.
      routerBaseline.value = routerSerialized.value
    }
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
        routerProviderRoles: { ...routerProviderRoles.value },
        tierEnsembleStatus: tierEnsembleStatus.value
          ? {
              ...tierEnsembleStatus.value,
              tierSelectionModes: { ...tierEnsembleStatus.value.tierSelectionModes },
              blockedTierCandidates: tierEnsembleStatus.value.blockedTierCandidates.map(row => ({ ...row })),
            }
          : null,
      }
    })
  }

  return {
    mode,
    defaultTier,
    visibleModeChoice,
    tierTemplateState,
    hasMixedTierProviders,
    routerProviderRoles,
    tierEnsembleStatus,
    routingDirty,
    visualModeDirty,
    isDirty,
    refreshRuntimeMetadata,
    initFromConfig,
    setRouterMode,
    captureRoutingModeState,
    restoreRoutingModeState,
    acceptRoutingModeChange,
    enableFromSavedBinding,
    setRouterDefaultTier,
    setRouterVisualMode,
    updateTierField,
    setEnsembleContext,
    payload,
    visualModePatches,
    createPanel,
  }
}
