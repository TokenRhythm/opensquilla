import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import i18n from '@/i18n'
import { useSetupCapabilitiesForm } from '@/composables/setup/useSetupCapabilitiesForm'
import { useSetupBehaviorForm } from '@/composables/setup/useSetupBehaviorForm'
import {
  hasEffectiveProvider,
  normalizeCatalogSyncStatus,
  normalizeDiscoveredModels,
  normalizeProbeTimings,
  useSetupProviderForm,
  type ConnectionState,
  type DiscoveredModelCatalog,
  type DiscoveredModelsByProvider,
  type DiscoveredModel,
  type EffectiveMaxTokens,
  type ProviderCredentialPanelState,
} from '@/composables/setup/useSetupProviderForm'
import {
  routerTierProviderParticipates,
  useSetupRouterForm,
  type SetupTierRow,
} from '@/composables/setup/useSetupRouterForm'
import {
  useSetupEnsembleForm,
  type EnsembleCandidateConfig,
  type EnsembleCandidateRole,
  type EnsembleCandidateView,
  type EnsembleCredentialStatus,
} from '@/composables/setup/useSetupEnsembleForm'
import {
  CUSTOM_B5_SELECTION_MODE,
  LEGACY_OPENROUTER_MODEL_OPTIONS,
  ROUTER_DYNAMIC_SELECTION_MODE,
  STATIC_B5_PROFILES,
  staticB5ModeForProvider,
} from '@/types/generated/router_tier_contract'
import { useSetupModelStrategyForm } from '@/composables/setup/useSetupModelStrategyForm'
import { invalidateReadiness } from '@/composables/setup/useReadinessSummary'
import { useSettingsPromotedForm, DEFAULT_LLM_TIMEOUT_SECONDS } from '@/composables/setup/useSettingsPromotedForm'
import { useSettingsSection } from '@/composables/setup/useSettingsSection'
import { SETTINGS_SECTIONS, type SettingsSectionId } from '@/composables/setup/settingsSections'
import { useRpcStore } from '@/stores/rpc'
import { useToasts } from '@/composables/useToasts'
import { useConfirm } from '@/composables/useConfirm'
import { saveFailedMessage } from '@/lib/rpcErrors'
import { copyTextWithFallback } from '@/utils/browser'
import { TEXT_TIERS, IMAGE_TIER, normalizeRouterTier, routerTierLabelKey } from '@/utils/chat/routerTiers'

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

export { SETTINGS_SECTIONS } from '@/composables/setup/settingsSections'
export type { SettingsSectionId } from '@/composables/setup/settingsSections'

const READINESS_KEYS: Record<string, string> = {
  ok: 'setup.readiness.ready',
  optional: 'setup.readiness.optional',
  missing: 'setup.readiness.missing',
  degraded: 'setup.readiness.needsAction',
  unknown: 'setup.readiness.check',
}

function readinessLabel(status: string): string {
  const key = READINESS_KEYS[status]
  return key ? i18n.global.t(key) : ''
}

// The gateway's image_generation section detail is a backend-built English
// sentence (onboarding/status.py, onboarding/next_steps.py) rendered verbatim
// in an otherwise fully localized panel. Until the backend ships a structured
// reason code, recognise only the missing-environment-variable shapes — the
// one actionable state a first-run user routinely hits — and localize them.
// Detection is deliberately narrow: anything unrecognised passes through the
// `generic` key verbatim so no operator-facing information is ever dropped.
const MISSING_ENV_DETAIL_PATTERNS: RegExp[] = [
  // onboarding/status.py `_source_detail`: "env key not visible: NAME",
  // optionally wrapped by `_with_provider` as
  // "provider (env key not visible: NAME)".
  /env key not visible:\s*([A-Z][A-Z0-9_]*)/,
  // onboarding/next_steps.py `_missing_env_warning`:
  // "Image generation provider: $NAME is not set in this shell. …".
  /\$([A-Z][A-Z0-9_]*) is not set in this shell/,
  // Defensive spelling of the same condition, in case a backend variant
  // states it directly. The name capture stays strictly uppercase so prose
  // that merely mentions environment variables cannot match.
  /[Mm]issing environment variable\s+([A-Z][A-Z0-9_]*)/,
]

export function localizeImageActionableDetail(detail: string): string {
  for (const pattern of MISSING_ENV_DETAIL_PATTERNS) {
    const name = detail.match(pattern)?.[1]
    if (name) {
      return i18n.global.t('setup.capabilities.imageStatus.missingEnv', { name })
    }
  }
  // Unrecognised backend prose passes through verbatim. Wrapping it in a
  // catalog entry whose value is nothing but "{detail}" would not localize
  // anything — it would only hide untranslated text behind a translation key
  // and force the i18n leakage guard to make an exception for it. Losing the
  // detail would be worse than showing it in English, so show it as-is until
  // the backend hands us a structured reason code.
  return detail
}

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface ProviderPresetSpec {
  presetId: string
  label: string
  description?: string
  synthesized?: boolean
  defaultModel?: string
  tiers?: Record<string, TierConfig>
}

interface ProviderSpec {
  providerId: string
  label: string
  runtimeSupported?: boolean
  routerSupported?: boolean
  fields?: FieldSpec[]
  whatYouNeed?: string[]
  envKey?: string
  acceptsApiKey?: boolean
  requiresApiKey?: boolean
  defaultBaseUrl?: string
  defaultDirectModel?: string
  defaultModel?: string
  suggestedModels?: string[]
  deployment?: string
  presets?: ProviderPresetSpec[]
}

interface FieldSpec {
  name: string
  label: string
  type?: string
  required?: boolean
  default?: string | boolean | number
  placeholder?: string
  description?: string
  secret?: boolean
  choices?: string[]
  showWhen?: Record<string, string>
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

interface SectionDetail {
  status?: string
  blocking?: boolean
  actionRequired?: boolean
  required?: boolean
  label?: string
  detail?: string
  // Server-computed router mode (router section card only):
  // recommended | openrouter-mix | custom | disabled.
  routerMode?: string
  // Server-owned routing intent. Older gateways omit this field and are
  // treated conservatively as legacy/preserve by the WebUI.
  routerBinding?: 'follow_primary' | 'custom' | 'legacy'
  // Additive mode-aware ownership returned by newer Gateways. The composable
  // accepts both this map and richer per-tier role objects.
  routerProviderRoles?: unknown
  router_provider_roles?: unknown
  tierEnsembleStatuses?: unknown
  tier_ensemble_statuses?: unknown
  tierEnsembleStatus?: unknown
  tier_ensemble_status?: unknown
  configuredAllFailedPolicy?: string
  effectiveAllFailedPolicy?: string
  policyDeprecated?: boolean
  providerResolution?: {
    status?: string
    effectiveProvider?: string
    source?: string
    reasonCode?: string
    actionRequired?: boolean
    actionRecommended?: boolean
  }
}

interface OnboardingStatus {
  needsOnboarding?: boolean
  hasConfig?: boolean
  llmConfigured?: boolean
  llmSource?: string
  sectionDetails?: Record<string, SectionDetail>
  envRecoveryCommands?: Array<{ section?: string; command?: string; label?: string }>
  configPath?: string
  channelCount?: number
  searchConfigured?: boolean
  searchProvider?: string
  searchSource?: string
  searchEnvKey?: string
  imageGenerationEnabled?: boolean
  imageGenerationConfigured?: boolean
  imageGenerationSource?: string
  imageGenerationEnvKey?: string
  imageGenerationProvider?: string
  imageGenerationPrimary?: string
  // Added by gateways that support atomic LLM/image onboarding. Its absence
  // is the compatibility signal for legacy gateways: do not send the new
  // mutation intent and do not invent a recommendation client-side.
  imageGenerationState?: {
    mode?: 'unconfigured' | 'disabled' | 'custom' | 'follow_llm' | string
    operatorManaged?: boolean
    storedEnabled?: boolean
    effective?: {
      enabled?: boolean
      available?: boolean
      dormant?: boolean
      providerId?: string
      primary?: string
      credentialSource?: string
      credentialOwner?: string
      reason?: string
    }
    credentialOptions?: Array<{
      providerId: string
      available: boolean
      source: string
      owner: string
      kind?: string
      envKey?: string
      reason?: string
    }>
    recommendation?: {
      providerId?: string
      reason?: string
      canReuseCredential?: boolean
      actionRequired?: boolean
    } | null
  }
  memoryEmbeddingConfigured?: boolean
  memoryEmbeddingSource?: string
  memoryEmbeddingEnvKey?: string
  memoryEmbeddingProvider?: string
  audioConfigured?: boolean
  audioEnabled?: boolean
  audioSource?: string
  audioEnvKey?: string
  capabilityConfiguration?: Partial<Record<CapabilityId, { resettable?: boolean }>>
  llmCredentialStatus?: {
    provider?: string
    available?: boolean
    source?: string
    envKey?: string
    masked?: string
    revealAllowed?: boolean
  }
  ensembleCredentialStatus?: EnsembleCredentialStatus[]
  llmProfileStatus?: Array<{
    provider?: string
    ready?: boolean
    credentialSource?: string
    credentialEnv?: string
    endpointSource?: string
    proxySource?: string
    reason?: string
    primaryEligible?: boolean
    primaryBlockReason?: string
    lastProbe?: {
      ok?: boolean
      at?: string
      configChanged?: boolean
      failureKind?: string
    }
  }>
}

function c3TierEnsembleStatus(detail: SectionDetail): unknown {
  const perTier = detail.tierEnsembleStatuses ?? detail.tier_ensemble_statuses
  if (perTier && typeof perTier === 'object' && !Array.isArray(perTier)) {
    const statuses = perTier as Record<string, unknown>
    const entry = Object.entries(statuses).find(([name]) => (
      (normalizeRouterTier(name) || name.trim().toLowerCase()) === 'c3'
    ))
    // A present per-tier projection is authoritative. In particular, never
    // borrow the compatibility singleton when this map says C3 is inactive.
    return entry?.[1] ?? null
  }
  // Older Gateways only exposed the compatibility singleton. It remains safe
  // to consume because the panel independently validates exact C3 scope.
  return detail.tierEnsembleStatus ?? detail.tier_ensemble_status ?? null
}

export interface LastProbeStatus {
  ok: boolean
  at: string
  configChanged: boolean
  failureKind: string
}

export interface ConfiguredProviderView {
  providerId: string
  label: string
  active: boolean
  ready: boolean
  credentialSource: string
  credentialEnv: string
  endpointSource: string
  reason: string
  primaryEligible: boolean
  primaryBlockReason: string
  probeModelAvailable: boolean
  lastProbe: LastProbeStatus | null
}

interface OnboardingCatalog {
  providers?: ProviderSpec[]
  routerProfiles?: {
    profiles?: Array<{ providerId: string; tiers?: Record<string, TierConfig> }>
    defaultTier?: string
  }
  searchProviders?: ProviderSpec[]
  imageGenerationProviders?: ProviderSpec[]
  memoryEmbeddingProviders?: ProviderSpec[]
}

interface StoredLlmProfileConfig {
  // config.get redacts api_key; the WebUI never reads or reconstructs it.
  model?: string
  api_key?: string
  api_key_env?: string
  api_key_env_pool?: string[]
  base_url?: string
  proxy?: string
  [key: string]: unknown
}

interface ConfigData {
  llm?: {
    provider?: string
    model?: string
    base_url?: string
    proxy?: string
    api_key_env?: string
    api_key?: string
    [key: string]: unknown
  }
  // Public config.get redacts profile secrets but preserves the profile keys.
  // Those keys are the source of truth for what is actually persisted and
  // therefore belongs in Model Service's "Configured providers" list.
  llm_profiles?: Record<string, StoredLlmProfileConfig>
  llm_request_timeout_seconds?: number
  // Per-provider/per-model overrides (deep-merge subtree; model ids carry
  // dots/colons so dot-path patches cannot address it).
  models?: Record<string, Record<string, { context_window?: number }>>
  squilla_router?: {
    enabled?: boolean
    preset_binding?: 'follow_primary' | 'custom'
    default_tier?: string
    visual_mode?: string
    cross_provider_tiers?: boolean
    tier_provider_mismatch?: string
    tiers?: Record<string, TierConfig>
  }
  llm_ensemble?: {
    enabled?: boolean
    selection_mode?: string
    selection_configured?: boolean
    activation_preview?: {
      selection_mode?: string
      proposer_count?: number
      member_providers?: string[]
      candidates?: EnsembleCandidateConfig[]
      blocked_reason?: string | null
    }
    model_options?: string[]
    candidates?: EnsembleCandidateConfig[]
    min_successful_proposers?: number
    all_failed_policy?: string
    configured_all_failed_policy?: string
    effective_all_failed_policy?: string
    policy_deprecated?: boolean
    proposer_max_retries?: number
    proposer_timeout_seconds?: number
    aggregator_timeout_seconds?: number
  }
  naming?: {
    enabled?: boolean
  }
  search_provider?: string
  search_api_key_env?: string
  search_max_results?: number
  search_proxy?: string
  search_use_env_proxy?: boolean
  search_fallback_policy?: string
  search_diagnostics?: boolean
  memory?: {
    auto_capture_enabled?: boolean
    embedding?: {
      provider?: string
      mode?: string
      remote?: {
        model?: string
        api_key?: string
        api_key_env?: string
        base_url?: string
      }
      local?: { onnx_dir?: string }
      ollama?: { model?: string; base_url?: string }
    }
  }
  image_generation?: {
    size?: string
    output_format?: string
    fallbacks?: string[]
    providers?: Record<string, { api_key?: string; api_key_env?: string; base_url?: string }>
  }
  audio?: {
    enabled?: boolean
    tts?: { voice?: string; model?: string; language_code?: string }
    providers?: Record<string, { api_key?: string; api_key_env?: string; base_url?: string }>
  }
  privacy?: {
    disable_network_observability?: boolean
    network_observability_disabled_effective?: boolean
  }
}

interface EffectiveConfigData {
  fields?: Record<string, { value?: unknown; source?: string }>
}

type CapabilityId = 'search' | 'image_generation' | 'audio' | 'memory_embedding'

export interface SettingsActionItem {
  label: string
  section: SettingsSectionId
}

export function useSetupCatalog() {
// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const rpc = useRpcStore()
const { pushToast } = useToasts()
const { confirm } = useConfirm()
const t = i18n.global.t

const catalog = ref<OnboardingCatalog>({})
const status = ref<OnboardingStatus>({})
const config = ref<ConfigData>({})
const effectiveConfig = ref<EffectiveConfigData>({})
const loaded = ref(false)
const { section, setSection } = useSettingsSection('provider')
const disableNetworkObservability = ref(false)
const capabilityResetPending = ref<CapabilityId | ''>('')
const saveAllPending = ref(false)
const providerSavePending = ref(false)
// The reactive flag drives UI feedback; this synchronous guard closes the
// same-microtask double-click window before the first save RPC can yield.
let saveAllRequestPending = false

const providerForm = useSetupProviderForm()
const configuredProviderProbes = ref<Record<string, ConnectionState>>({})
let configuredProbeEpoch = 0
const providerActivation = ref<{
  providerId: string
  phase: 'idle' | 'discovering' | 'ready' | 'activating' | 'error'
  models: DiscoveredModel[]
  suggestedModel: string
  error: string
}>({ providerId: '', phase: 'idle', models: [], suggestedModel: '', error: '' })
// Activation swaps the primary deployment and reloads the provider editor.
// Keep a synchronous lock as well as the rendered phase so a second click in
// the confirmation microtask cannot start another activation.
let providerActivationRequestPending = false
const providerCredentialRemovalPending = ref(false)
const providerSelectionKind = ref<'primary' | 'profile' | 'new'>('primary')
// This is an operation intent, not persisted image form state. It is reset for
// each provider selection and only rendered for the one safe default case:
// official OpenRouter onboarding while image generation is unconfigured.
const providerImageGenerationOptIn = ref(true)
// A configured primary model is shared with Model Routing, but edits made from
// the provider dialog belong to its verify-then-save flow until the user
// explicitly opens Model Routing.
const providerOwnsFixedModelDraft = ref(false)
const providerFixedModelDraftSnapshot = ref<{
  provider: string
  model: string
} | null>(null)
const behaviorForm = useSetupBehaviorForm()
const routerForm = useSetupRouterForm()
const ensembleForm = useSetupEnsembleForm()
const capabilitiesForm = useSetupCapabilitiesForm()
const promotedForm = useSettingsPromotedForm()

const tierModelCatalogs = ref<DiscoveredModelsByProvider>({})
const tierModelDiscoveries = new Map<string, Promise<void>>()
let tierModelDiscoveryEpoch = 0
type ImageModelCatalogSource = 'live' | 'catalog' | 'none'
interface ImageModelCatalog {
  models: DiscoveredModel[]
  source: ImageModelCatalogSource
}
const imageModelCatalogs = ref<Record<string, ImageModelCatalog>>({})
const imageModelDiscoveries = new Map<string, Promise<void>>()
const imageModelDiscoveryCompleted = new Set<string>()
let imageModelDiscoveryEpoch = 0

function normalizeProviderId(value: unknown): string {
  return String(value || '').trim().toLowerCase()
}

function primaryProviderIsConfigured(
  llm: ConfigData['llm'],
  onboardingStatus: OnboardingStatus,
  effective: EffectiveConfigData,
): boolean {
  if (!normalizeProviderId(llm?.provider)) return false

  // Config/effective may report a materialized provider default as coming from
  // "config" even when provider resolution rejected it because multiple
  // credential-shaped environment variables disagree. In that state the
  // Gateway deliberately withholds every credential and cannot run a turn.
  // Never turn that unresolved default into an Active/Configured provider card;
  // doing so makes a successful read-only probe look like completed setup while
  // the authoritative onboarding status remains blocking.
  const resolution = onboardingStatus.sectionDetails?.llm?.providerResolution
  if (String(resolution?.status || '').trim().toLowerCase() === 'conflict') {
    return false
  }

  // A config file can exist because the user changed an unrelated setting;
  // config.get still materializes llm defaults in that case. Prefer the
  // effective field's provenance so only an operator choice (or a usable
  // registry-default credential) hydrates the primary provider editor.
  const source = String(effective.fields?.['llm.provider']?.source || '')
  if (source) {
    return source !== 'default' || onboardingStatus.llmConfigured === true
  }
  return hasEffectiveProvider(llm || {}, onboardingStatus)
}

function resetTierModelDiscovery() {
  tierModelDiscoveryEpoch += 1
  tierModelCatalogs.value = {}
  tierModelDiscoveries.clear()
}

function resetImageModelDiscovery() {
  imageModelDiscoveryEpoch += 1
  imageModelCatalogs.value = {}
  imageModelDiscoveries.clear()
  imageModelDiscoveryCompleted.clear()
}

function curatedImageModelCatalog(providerId: string): ImageModelCatalog {
  const provider = normalizeProviderId(providerId)
  const spec = imageProviders.value.find(
    item => normalizeProviderId(item.providerId) === provider,
  )
  const prefix = `${provider}/`
  const rawModels = spec?.suggestedModels?.length
    ? spec.suggestedModels
    : (spec?.defaultModel ? [spec.defaultModel] : [])
  const models = Array.from(new Set(
    rawModels
      .map(model => String(model || '').trim())
      .map(model => model.startsWith(prefix) ? model.slice(prefix.length) : model)
      .filter(Boolean),
  )).map<DiscoveredModel>(id => ({
    id,
    name: id,
    contextWindow: null,
    maxOutputTokens: null,
    capabilities: [],
    pricing: null,
    capabilitySource: '',
  }))
  return {
    models,
    source: models.length ? 'catalog' : 'none',
  }
}

function discoverImageGenerationModels(providerId: string): Promise<void> {
  const provider = normalizeProviderId(providerId)
  if (!provider) return Promise.resolve()

  const curated = curatedImageModelCatalog(provider)
  if (!imageModelCatalogs.value[provider]) {
    imageModelCatalogs.value = {
      ...imageModelCatalogs.value,
      [provider]: curated,
    }
  }
  if (
    typeof rpc.supportsMethod === 'function'
    && !rpc.supportsMethod('onboarding.imageGeneration.models.discover')
  ) {
    return Promise.resolve()
  }
  const existing = imageModelDiscoveries.get(provider)
  if (existing) return existing
  if (imageModelDiscoveryCompleted.has(provider)) return Promise.resolve()

  const epoch = imageModelDiscoveryEpoch
  imageModelDiscoveryCompleted.add(provider)
  const request = (async () => {
    try {
      const res = await rpc.call<{
        ok?: boolean
        providerId?: string
        source?: string
        models?: unknown
      }>('onboarding.imageGeneration.models.discover', { providerId: provider })
      if (epoch !== imageModelDiscoveryEpoch) return
      const models = res?.ok ? normalizeDiscoveredModels(res.models) : []
      const source: ImageModelCatalogSource = (
        models.length && res.source === 'live'
          ? 'live'
          : (models.length ? 'catalog' : curated.source)
      )
      imageModelCatalogs.value = {
        ...imageModelCatalogs.value,
        [provider]: models.length ? { models, source } : curated,
      }
    } catch {
      if (epoch !== imageModelDiscoveryEpoch) return
      imageModelCatalogs.value = {
        ...imageModelCatalogs.value,
        [provider]: curated,
      }
    }
  })()
  const tracked = request.finally(() => {
    if (imageModelDiscoveries.get(provider) === tracked) {
      imageModelDiscoveries.delete(provider)
    }
  })
  imageModelDiscoveries.set(provider, tracked)
  return tracked
}

function maybeDiscoverImageGenerationModels(): Promise<void> {
  if (section.value !== 'capabilities') return Promise.resolve()
  return discoverImageGenerationModels(capabilitiesForm.selectedImageProvider.value)
}

function discoverTierProviderModels(providerId: string): Promise<void> {
  const provider = normalizeProviderId(providerId)
  if (!provider) return Promise.resolve()

  const selectedProvider = normalizeProviderId(providerForm.selectedProvider.value)
  if (provider === selectedProvider) {
    // Keep using the provider form's discovery state for the selected provider
    // so its live catalog feeds both Model Service and Model Routing.
    // A selected stored profile has write-only credentials/endpoint state, so
    // the legacy form RPC cannot reconstruct its deployment. Resolve that
    // provider through the profile RPC just like non-selected routing members.
    return providerForm.discoverModels({
      storedProfile: providerSelectionKind.value === 'profile',
    })
  }

  const existing = tierModelDiscoveries.get(provider)
  if (existing) return existing

  const epoch = tierModelDiscoveryEpoch
  const request = (async () => {
    try {
      // Deliberately provider-only. Never forward the selected provider's
      // unsaved apiKey/baseUrl/proxy into another provider's request.
      const discoverProfile = () => rpc.call<{
        ok?: boolean
        source?: string
        models?: unknown
        catalog?: unknown
      }>('onboarding.llmProfile.models.discover', { providerId: provider })
      let res: { ok?: boolean; source?: string; models?: unknown; catalog?: unknown }
      if (provider === normalizeProviderId(config.value.llm?.provider)) {
        // The current provider lives in [llm], not llm_profiles. This branch
        // matters when Model Service is currently editing a different saved
        // profile: the fixed-model picker must still discover the active
        // provider through its primary deployment.
        res = await rpc.call<{
          ok?: boolean
          source?: string
          models?: unknown
          catalog?: unknown
        }>('onboarding.models.discover', { providerId: provider })
      } else {
        try {
          res = await discoverProfile()
        } catch (err) {
          if (!isRpcMethodUnavailableError(err)) throw err
          // Compatibility with pre-profile gateways: the legacy endpoint can
          // still resolve the provider's registry env key. Never send another
          // provider's unsaved credentials in this fallback.
          res = await rpc.call<{
            ok?: boolean
            source?: string
            models?: unknown
            catalog?: unknown
          }>('onboarding.models.discover', { providerId: provider })
        }
      }
      if (epoch !== tierModelDiscoveryEpoch) return
      const source = res?.ok && res.source === 'live' ? 'live' : 'none'
      tierModelCatalogs.value = {
        ...tierModelCatalogs.value,
        [provider]: source === 'live'
          ? {
              models: normalizeDiscoveredModels(res.models),
              source,
              catalog: normalizeCatalogSyncStatus(res.catalog),
            }
          : { models: [], source: 'none' },
      }
    } catch {
      if (epoch !== tierModelDiscoveryEpoch) return
      tierModelCatalogs.value = {
        ...tierModelCatalogs.value,
        [provider]: { models: [], source: 'none' },
      }
    }
  })()
  const tracked = request.finally(() => {
    if (tierModelDiscoveries.get(provider) === tracked) {
      tierModelDiscoveries.delete(provider)
    }
  })
  tierModelDiscoveries.set(provider, tracked)
  return tracked
}

async function maybeDiscoverModelsForStrategy(): Promise<void> {
  if (section.value !== 'modelStrategy') return
  const providers = new Set(
    routerPanel.value.tierRows
      .map(row => normalizeProviderId(row.provider))
      .filter(Boolean),
  )
  const selectedProvider = normalizeProviderId(providerForm.selectedProvider.value)
  if (selectedProvider) providers.add(selectedProvider)
  const activeProvider = normalizeProviderId(config.value.llm?.provider)
  if (activeProvider) providers.add(activeProvider)
  for (const candidate of ensembleForm.candidates.value) {
    if (candidate.enabled === false) continue
    const provider = normalizeProviderId(candidate.provider)
    if (provider) providers.add(provider)
  }
  await Promise.all(Array.from(providers, provider => discoverTierProviderModels(provider)))
}

function maybeDiscoverProviderModels(): Promise<void> {
  if (section.value !== 'provider' || !loaded.value) return Promise.resolve()
  return providerForm.discoverModels({
    storedProfile: providerSelectionKind.value === 'profile',
  })
}

watch(section, value => {
  if (value === 'modelStrategy') {
    providerOwnsFixedModelDraft.value = false
    providerFixedModelDraftSnapshot.value = null
  }
  void maybeDiscoverProviderModels()
  void maybeDiscoverModelsForStrategy()
  void maybeDiscoverImageGenerationModels()
})

// ---------------------------------------------------------------------------
// Lifecycle
// ---------------------------------------------------------------------------

onMounted(async () => {
  await loadData()
  loaded.value = true
})

onUnmounted(() => {
})

// ---------------------------------------------------------------------------
// Data loading
// ---------------------------------------------------------------------------

async function loadData(options: {
  preserveFormDrafts?: boolean
  preserveDirtySectionDrafts?: boolean
  forceResetModelStrategy?: boolean
  resetProviderConnection?: boolean
  throwOnError?: boolean
} = {}) {
  try {
    await rpc.waitForConnection()
    const [cat, st, cfg, effective] = await Promise.all([
      rpc.call<OnboardingCatalog>('onboarding.catalog'),
      rpc.call<OnboardingStatus>('onboarding.status'),
      rpc.call<ConfigData>('config.get'),
      // Optional on older gateways: effective metadata must never block the
      // settings surface or provider saves.
      rpc.call<EffectiveConfigData>('config.effective').catch(() => ({ fields: {} })),
    ])
    // A Provider save owns only the Provider editor. Snapshot every other form
    // after the network round trip but before replacing the config refs. This
    // also catches a draft created in another section while the save was in
    // flight, while clean forms still consume the new config and status.
    const preserve = options.preserveDirtySectionDrafts
      ? {
          behavior: behaviorForm.isDirty.value,
          privacy: privacyDirty.value,
          memoryCapture: promotedForm.captureDirty.value,
          modelStrategy: !options.forceResetModelStrategy && (
            routerForm.isDirty.value
            || ensembleForm.isDirty.value
            || modelStrategyForm.fixedProviderDirty.value
            || modelStrategyForm.fixedModelDirty.value
          ),
          search: capabilitiesForm.searchDirty.value,
          memory: capabilitiesForm.memoryDirty.value,
          image: capabilitiesForm.imageDirty.value,
          audio: promotedForm.audioDirty.value,
        }
      : null
    catalog.value = cat || {}
    status.value = st || {}
    config.value = cfg || {}
    effectiveConfig.value = effective || {}
    // A probe result describes one exact saved deployment. Any successful
    // reload may follow a key, endpoint, model, activation, or deletion
    // mutation, so stale results must never survive it.
    configuredProbeEpoch += 1
    configuredProviderProbes.value = {}
    resetTierModelDiscovery()
    resetImageModelDiscovery()
    if (options.resetProviderConnection) providerForm.resetConnectionState()

    if (!options.preserveFormDrafts) {
      // Initialize form values from config. Credential-only mutations opt out
      // so a refresh of saved status never erases unrelated form drafts.
      providerForm.initFromConfig(
        config.value.llm || {},
        status.value,
        runtimeProviders.value,
        primaryProviderIsConfigured(config.value.llm, status.value, effectiveConfig.value),
      )
      providerImageGenerationOptIn.value = true
      if (!preserve?.modelStrategy) {
        modelStrategyForm.initFixedModel(config.value.llm?.model || '')
        providerOwnsFixedModelDraft.value = false
        providerFixedModelDraftSnapshot.value = null
      }
      providerSelectionKind.value = 'primary'
      // Model discovery is a read-only UI accelerator. Populate the active
      // provider's combobox as soon as the saved editor opens, independently of
      // connection probing. Failure and source=none intentionally leave the
      // free-form model input available.
      if (providerForm.selectedProvider.value) void providerForm.discoverModels()
      if (!preserve?.behavior) behaviorForm.initFromConfig(config.value)
      // Timeout and per-model context are Provider-owned controls. A Provider
      // save must always rebase them from the persisted response even while
      // drafts in Privacy or Capabilities stay untouched.
      promotedForm.initProviderFromConfig(config.value)
      const routerDetail = (status.value.sectionDetails || {}).router || {}
      if (!preserve?.modelStrategy) {
        const ensembleDetail = (status.value.sectionDetails || {}).ensemble || {}
        ensembleForm.initFromConfig({
          ...(config.value.llm_ensemble || {}),
          configured_all_failed_policy: ensembleDetail.configuredAllFailedPolicy,
          effective_all_failed_policy: ensembleDetail.effectiveAllFailedPolicy,
          policy_deprecated: ensembleDetail.policyDeprecated,
        })
      }
      if (!preserve?.modelStrategy) {
        const binding = String(
          routerDetail.routerBinding
          || config.value.squilla_router?.preset_binding
          || '',
        ).trim().toLowerCase()
        routerForm.initFromConfig(
          config.value.squilla_router || {},
          currentRouterProfile.value?.tiers || {},
          currentProvider.value,
          binding === 'follow_primary' || binding === 'custom' ? binding : 'legacy',
          routerDetail.routerProviderRoles || routerDetail.router_provider_roles,
          ensembleForm.selectionMode.value,
          ensembleForm.enabled.value,
          c3TierEnsembleStatus(routerDetail),
        )
      } else {
        // Provider saves keep a dirty Router's values and baselines, while
        // readiness and provider ownership reflect the fresh Gateway status.
        routerForm.refreshRuntimeMetadata(
          routerDetail.routerProviderRoles || routerDetail.router_provider_roles,
          c3TierEnsembleStatus(routerDetail),
        )
      }
      if (!preserve?.search) {
        capabilitiesForm.initSearchFromConfig(config.value, searchProviders.value)
      }
      if (!preserve?.memory) capabilitiesForm.initMemoryFromConfig(config.value)
      if (!preserve?.image) {
        capabilitiesForm.initImageFromConfig(config.value, status.value, imageProviders.value)
      }
      if (!preserve?.memoryCapture) promotedForm.initMemoryCaptureFromConfig(config.value)
      if (!preserve?.audio) promotedForm.initAudioFromConfig(config.value)
      if (!preserve?.privacy) {
        disableNetworkObservability.value = currentDisableNetworkObservability.value
      }
    }
    // Model listing is an optional UI accelerator and may involve an external
    // provider. Start it after core state is ready, but never hold settings
    // loading/saving open while that network request runs.
    void maybeDiscoverModelsForStrategy()
    void maybeDiscoverImageGenerationModels()
    // Every save funnels through this reload, so this is the one spot that can
    // tell snapshot holders outside the dialog (the sidebar banner) that the
    // hot-applied config may have changed readiness.
    invalidateReadiness()
  } catch (err) {
    if (options.throwOnError) throw err
    pushToast(t('setup.toast.loadFailed', { error: err instanceof Error ? err.message : String(err) }), { tone: 'danger' })
  }
}

// ---------------------------------------------------------------------------
// Computed
// ---------------------------------------------------------------------------

const currentProvider = computed(() => (config.value.llm || {}).provider || '')
const currentProviderConfig = computed(() => config.value.llm || {})
const currentModel = computed(() => (config.value.llm || {}).model || '')
const hasConfiguredPrimaryProvider = computed(() => {
  return primaryProviderIsConfigured(
    currentProviderConfig.value,
    status.value,
    effectiveConfig.value,
  )
})
const hasSavedProvider = hasConfiguredPrimaryProvider
// Lazy: routerPanel is declared below; this computed is only evaluated from
// user-triggered strategy switches, long after setup completes.
const modelStrategyTierCandidates = computed(() => ensembleTierCandidates.value)
const modelStrategyForm = useSetupModelStrategyForm(
  routerForm,
  ensembleForm,
  currentProvider,
  modelStrategyTierCandidates,
  currentModel,
)

const runtimeProviders = computed(() => (catalog.value.providers || []).filter(p => p.runtimeSupported))
function providerCatalogLabel(providerId: string): string {
  const id = normalizeProviderId(providerId)
  return runtimeProviders.value.find(provider => normalizeProviderId(provider.providerId) === id)?.label
    || providerId
}

const storedProfileIds = computed(() => new Set(
  Object.keys(config.value.llm_profiles || {})
    .map(providerId => normalizeProviderId(providerId))
    .filter(Boolean),
))

function storedProfileConfig(providerId: string): StoredLlmProfileConfig {
  const normalized = normalizeProviderId(providerId)
  const entry = Object.entries(config.value.llm_profiles || {}).find(
    ([id]) => normalizeProviderId(id) === normalized,
  )
  return entry?.[1] || {}
}

function profileCredentialUiSource(source: unknown, ready: boolean): string {
  const value = String(source || '')
  if (['profile', 'member', 'explicit'].includes(value)) return 'explicit'
  if ([
    'profile_pool',
    'profile_pool_env',
    'profile_env',
    'registry_env',
    'member_env',
    'env',
  ].includes(value)) return ready ? 'env' : 'missing_env'
  if (['keyless', 'not_required'].includes(value)) return 'not_required'
  return value || 'none'
}

function lastProbeView(
  raw?: { ok?: boolean; at?: string; configChanged?: boolean; failureKind?: string },
): LastProbeStatus | null {
  if (!raw?.at) return null
  return {
    ok: raw.ok === true,
    at: String(raw.at),
    configChanged: raw.configChanged === true,
    failureKind: String(raw.failureKind || ''),
  }
}

const configuredProviders = computed<ConfiguredProviderView[]>(() => {
  const active = normalizeProviderId(currentProvider.value)
  const rows = new Map<string, ConfiguredProviderView>()
  const activeCredential = status.value.llmCredentialStatus || {}
  if (active && hasConfiguredPrimaryProvider.value) {
    rows.set(active, {
      providerId: active,
      label: providerCatalogLabel(active),
      active: true,
      ready: activeCredential.available === true || status.value.llmConfigured === true,
      credentialSource: String(activeCredential.source || status.value.llmSource || 'none'),
      credentialEnv: String(activeCredential.envKey || ''),
      endpointSource: '',
      reason: (activeCredential.available === true || status.value.llmConfigured === true)
        ? ''
        : String(status.value.llmSource || ''),
      primaryEligible: false,
      primaryBlockReason: 'already_active',
      probeModelAvailable: runtimeProviders.value.some(provider => normalizeProviderId(provider.providerId) === active)
        && Boolean(currentModel.value || representativeProviderModel(active)),
      lastProbe: null,
    })
  }
  // config.get is the persistence source of truth. Seed stored profiles even
  // when an older Gateway omits the additive llmProfileStatus field; the
  // status rows below enhance these cards with readiness when available.
  for (const id of storedProfileIds.value) {
    if (!id || id === active) continue
    rows.set(id, {
      providerId: id,
      label: providerCatalogLabel(id),
      active: false,
      ready: false,
      credentialSource: 'none',
      credentialEnv: '',
      endpointSource: '',
      reason: 'profile_status_unavailable',
      primaryEligible: false,
      primaryBlockReason: 'profile_status_unavailable',
      probeModelAvailable: runtimeProviders.value.some(provider => normalizeProviderId(provider.providerId) === id)
        && Boolean(representativeProviderModel(id)),
      lastProbe: null,
    })
  }
  for (const profile of status.value.llmProfileStatus || []) {
    const id = normalizeProviderId(profile.provider)
    if (!id) continue
    // llmProfileStatus covers every deployment referenced by Router or
    // Ensemble so those surfaces can show readiness. A status row alone does
    // not mean a persisted llm_profile exists. Showing route-only rows here
    // creates a bogus Delete action whose backend correctly answers
    // "profile does not exist". Keep Model Service scoped to the active
    // provider plus profiles that are actually present in config.get.
    if (id === active && !hasConfiguredPrimaryProvider.value) continue
    if (id !== active && !storedProfileIds.value.has(id)) continue
    const existing = rows.get(id)
    const primaryEligibilityKnown = (
      typeof profile.primaryEligible === 'boolean'
      || typeof profile.primaryBlockReason === 'string'
    )
    rows.set(id, {
      providerId: id,
      label: providerCatalogLabel(id),
      active: id === active,
      ready: profile.ready === true,
      credentialSource: profileCredentialUiSource(profile.credentialSource, profile.ready === true),
      credentialEnv: String(profile.credentialEnv || existing?.credentialEnv || ''),
      endpointSource: String(profile.endpointSource || ''),
      // The presence of an additive status row is authoritative, including an
      // intentionally empty success reason. Falling back to the seeded legacy
      // reason would render a ready profile as "Status unavailable".
      reason: String(profile.reason ?? ''),
      primaryEligible: id === active
        ? false
        : (primaryEligibilityKnown && profile.primaryEligible === true),
      primaryBlockReason: id === active
        ? 'already_active'
        : (primaryEligibilityKnown
            ? String(profile.primaryBlockReason || '')
            : 'profile_status_unavailable'),
      probeModelAvailable: runtimeProviders.value.some(provider => normalizeProviderId(provider.providerId) === id)
        && Boolean(id === active ? (currentModel.value || representativeProviderModel(id)) : representativeProviderModel(id)),
      lastProbe: lastProbeView(profile.lastProbe),
    })
  }
  return Array.from(rows.values()).sort((left, right) => {
    if (left.active !== right.active) return left.active ? -1 : 1
    return left.label.localeCompare(right.label)
  })
})

const configuredProviderIds = computed(() => new Set(
  configuredProviders.value.map(provider => normalizeProviderId(provider.providerId)),
))

const routingProviderOptions = computed(() => {
  // Routing controls may only create deployments for providers that are
  // actually persisted in Model Service. Historical Router/Ensemble
  // references are rendered locally by the relevant editor as disabled
  // compatibility options; promoting them into this global list would make an
  // unconfigured provider selectable again after its profile was removed.
  return Array.from(configuredProviderIds.value, providerId => ({
    providerId,
    label: providerCatalogLabel(providerId),
  }))
})
const searchProviders = computed(() => (catalog.value.searchProviders || []).filter(p => p.runtimeSupported))
const imageProviders = computed(() => (catalog.value.imageGenerationProviders || []).filter(p => p.runtimeSupported))
const memoryProviders = computed(() => catalog.value.memoryEmbeddingProviders || [])
const imageGenerationMode = computed(() => (
  String(status.value.imageGenerationState?.mode || '').trim().toLowerCase()
))
const imageGenerationIntentSupported = computed(() => Boolean(status.value.imageGenerationState))
const imageRecommendation = computed(() => {
  const recommendation = status.value.imageGenerationState?.recommendation
  const providerId = normalizeProviderId(recommendation?.providerId)
  // The server owns recommendation policy. The client only renders a row that
  // is present in the live image-provider catalog, so an older or customized
  // catalog can never surface a phantom provider.
  const provider = imageProviders.value.find(
    candidate => normalizeProviderId(candidate.providerId) === providerId,
  )
  if (
    !provider
    || !providerId
  ) return null
  return {
    providerId,
    label: provider.label,
    canReuseCredential: recommendation?.canReuseCredential === true,
    actionRequired: recommendation?.actionRequired === true,
    // The current gateway recommendation contract intentionally carries only
    // provider identity and reason. Keep the acquisition URL scoped to the
    // catalog-confirmed TokenRhythm row; future recommendations render without
    // an incorrect registration destination until the contract adds one.
    registrationUrl: providerId === 'tokenrhythm'
      ? 'https://tokenrhythm.studio/register'
      : '',
  }
})
const imageCredentialOptions = computed(() => {
  const catalogIds = new Set(imageProviders.value.map(provider => provider.providerId))
  return (status.value.imageGenerationState?.credentialOptions || []).filter(
    option => catalogIds.has(option.providerId),
  )
})
const routerProfiles = computed(() => catalog.value.routerProfiles?.profiles || [])
const currentRouterProfile = computed(() => {
  const providerId = normalizeProviderId(currentProvider.value)
  const persistedProfile = routerProfiles.value.find(
    profile => normalizeProviderId(profile.providerId) === providerId,
  )
  if (persistedProfile) return persistedProfile
  // Curated inline and synthesized presets are intentionally absent from the
  // legacy tier_profile catalog, but their provider entries still carry the
  // managed ladder. Follow-primary must use that ladder too; otherwise a
  // disabled sparse config would expose the settings model's materialized
  // OpenRouter defaults when re-enabled.
  const provider = runtimeProviders.value.find(
    candidate => normalizeProviderId(candidate.providerId) === providerId,
  )
  const preset = provider?.presets?.[0]
  const directModel = currentModel.value
  return preset
    ? {
        providerId,
        tiers: Object.fromEntries(
          Object.entries(preset.tiers || {}).map(([name, tier]) => [
            name,
            {
              ...tier,
              model: String(tier.model || '').trim() ? tier.model : directModel,
            },
          ]),
        ),
      }
    : undefined
})
const providerSpec = computed(() => runtimeProviders.value.find(
  p => normalizeProviderId(p.providerId) === normalizeProviderId(providerForm.selectedProvider.value),
) || null)
const editingPrimaryProvider = computed(() => providerSelectionKind.value === 'primary')
const selectedProfileStatus = computed(() => configuredProviders.value.find(provider => (
  normalizeProviderId(provider.providerId) === normalizeProviderId(providerForm.selectedProvider.value)
)) || null)
const providerEditorConfig = computed(() => {
  if (editingPrimaryProvider.value) return config.value.llm || {}
  const profile = selectedProfileStatus.value
  const stored = storedProfileConfig(providerForm.selectedProvider.value)
  return {
    // Keep catalog defaults effective rather than materializing them into the
    // profile. An explicit saved model wins; otherwise the provider's direct
    // default is shown and activation resolves the same fallback server-side.
    model: stored.model || providerSpec.value?.defaultDirectModel || '',
    api_key_env: stored.api_key_env || profile?.credentialEnv || '',
    base_url: stored.base_url || '',
    proxy: stored.proxy || '',
  }
})
const providerImageGenerationOffer = computed(() => {
  const spec = providerSpec.value
  const baseUrlField = spec?.fields?.find(field => field.name === 'base_url')
  const effectiveBaseUrl = baseUrlField
    ? providerForm.fieldValue(baseUrlField, providerEditorConfig.value)
    : String(spec?.defaultBaseUrl || '')
  return Boolean(
    imageGenerationIntentSupported.value
    && imageGenerationMode.value === 'unconfigured'
    // Profile upserts do not activate a provider and intentionally do not own
    // image routing. The offer belongs only to the active/first-primary
    // configure flow; stored-profile activation still receives the same
    // default intent at the activation boundary.
    && editingPrimaryProvider.value
    && normalizeProviderId(providerForm.selectedProvider.value) === 'openrouter'
    && spec
    && sameEndpointApiBase(effectiveBaseUrl, spec.defaultBaseUrl),
  )
})
const providerFields = computed(() => providerSpec.value?.fields || [])
const providerCoreFields = computed(() => providerFields.value.filter(f => !isProviderCredentialField(f) && !isProviderAdvancedField(f)))
const providerAdvancedFields = computed(() => providerFields.value.filter(f => !isProviderCredentialField(f) && isProviderAdvancedField(f)))

// Providers that run on the user's own hardware, where the runtime frequently
// defaults to a much smaller context window than the model supports. Mirrors the
// backend LOCAL_RUNTIME_PROVIDERS in provider/registry.py — keep in sync.
const LOCAL_PROVIDER_IDS = new Set(['ollama', 'vllm', 'lm_studio', 'ovms', 'local', 'custom'])
const providerIsLocal = computed(() => {
  const spec = providerSpec.value
  if (!spec) return false
  // Union, not either/or: the backend budgets 'custom' (deployment='custom') at
  // the local 8192 default too, so a deployment tag that isn't 'local' must not
  // suppress the known-local-id match.
  const deployment = String(spec.deployment || '').trim().toLowerCase()
  return deployment === 'local' || LOCAL_PROVIDER_IDS.has(spec.providerId)
})

// The global llm.context_window_tokens layer sits between the per-model override
// and catalog auto-detection in the backend's precedence. Surface it so the
// panel readout reflects it when no per-model override is set.
const contextWindowGlobal = computed<number | null>(() => {
  const raw = Number((config.value.llm || {}).context_window_tokens)
  return Number.isFinite(raw) && raw > 0 ? Math.floor(raw) : null
})

const effectiveMaxTokens = computed<EffectiveMaxTokens | null>(() => {
  const fields = effectiveConfig.value.fields || {}
  const effectiveProvider = normalizeProviderId(fields['llm.provider']?.value)
  const selectedProvider = normalizeProviderId(providerForm.selectedProvider.value)
  const effectiveModel = String(fields['llm.model']?.value || '').trim()
  const selectedModel = currentFormModelValue()
  if (
    !effectiveProvider
    || effectiveProvider !== selectedProvider
    || !effectiveModel
    || effectiveModel !== selectedModel
  ) {
    return null
  }
  const record = fields['llm.max_tokens']
  const value = Number(record?.value)
  const source = String(record?.source || '')
  if (
    !Number.isFinite(value)
    || value <= 0
    || !['config', 'catalog', 'default'].includes(source)
  ) {
    return null
  }
  return {
    value: Math.floor(value),
    source: source as EffectiveMaxTokens['source'],
  }
})

const effectiveMaxTokensPending = computed(() => {
  const selectedProvider = normalizeProviderId(providerForm.selectedProvider.value)
  const selectedModel = currentFormModelValue()
  if (!selectedProvider || !selectedModel) return false
  if (selectedNewProfile.value) return true
  if (selectedStoredProfile.value) return providerForm.isDirty.value

  // Pending means that this provider/model is an unsaved candidate. Do not
  // infer it from config.effective: older gateways may omit that optional RPC,
  // which makes the effective value unknown rather than the saved form dirty.
  const savedProvider = normalizeProviderId(config.value.llm?.provider)
  const savedModel = String(config.value.llm?.model || '').trim()
  return (
    providerForm.isDirty.value
    || selectedProvider !== savedProvider
    || selectedModel !== savedModel
  )
})

const providerSummary = computed(() => {
  if (!hasSavedProvider.value) return t('setup.summary.notConfigured')
  const spec = runtimeProviders.value.find(p => p.providerId === currentProvider.value)
  return spec?.label || currentProvider.value
})

const routerSupportText = computed(() => {
  if (!providerSpec.value) return t('setup.provider.chooseProviderShort')
  return providerSpec.value.routerSupported === true ? t('setup.provider.routerReady') : t('setup.provider.directOnly')
})

const routerSupportTone = computed(() => {
  if (!providerSpec.value) return 'is-neutral'
  return providerSpec.value.routerSupported === true ? 'is-ready' : 'is-direct'
})

// The "Configure the router →" affordance must only appear when jumping to the
// Router section shows a consistent, ready view. routerSupportTone tracks the
// *selected* provider (so the pill updates live as you browse providers), but the
// Router panel reflects the *saved* config — so gating the link on the tone alone
// could land on the previously-saved provider's tiers or the "provider first"
// empty state. Require a router-capable provider to actually be saved AND the
// selection to be clean, so selected == saved and the Router view is not stale.
const canConfigureRouter = computed(() =>
  hasSavedProvider.value
  && !providerForm.isDirty.value
  && routerSupportTone.value === 'is-ready',
)

const providerNeeds = computed(() => {
  if (!providerSpec.value) return [t('setup.provider.chooseToSeeFields')]
  return providerSpec.value.whatYouNeed || []
})

const providerAdvancedOpen = computed(() => {
  if (promotedForm.llmTimeoutSeconds.value !== DEFAULT_LLM_TIMEOUT_SECONDS) return true
  if (promotedForm.contextWindowTokens.value.trim() !== '') return true
  return providerAdvancedFields.value.some(f => {
    if (f.required) return true
    const val = providerForm.fieldValue(f, config.value.llm || {}).trim()
    const def = String(f.default || '').trim()
    if (def) return val !== def
    return val.length > 0
  })
})

const providerEnvMissing = computed(() => (
  editingPrimaryProvider.value && status.value.llmSource === 'missing_env'
))
const providerEnvKey = computed(() => (config.value.llm || {}).api_key_env || t('setup.provider.envKeyFallback'))
const providerEnvCommand = computed(() => envRecoveryCommand('llm'))
const searchEnvCommand = computed(() => envRecoveryCommand('search'))
const memoryEnvCommand = computed(() => envRecoveryCommand('memory_embedding'))
const imageEnvCommand = computed(() => envRecoveryCommand('image_generation'))

const routerSummary = computed(() => {
  if (!hasSavedProvider.value) return t('setup.router.chooseProviderFirst')
  if (ensembleProfileActive.value) return t('setup.router.summaryEnsemble')
  if (routerForm.mode.value === 'disabled') return t('setup.router.summaryDisabled')
  if (routerForm.mode.value === 'openrouter-mix') return t('setup.preset.modeCustom')
  return t('setup.router.modeRecommended')
})
const ensembleProfileActive = computed(() => config.value.llm_ensemble?.enabled === true)

const behaviorStatusText = computed(() => {
  return behaviorForm.autoSessionTitles.value
    ? t('setup.behavior.statusOn')
    : t('setup.behavior.statusOff')
})
const currentDisableNetworkObservability = computed(() => config.value.privacy?.disable_network_observability === true)
const currentEffectiveNetworkObservabilityDisabled = computed(() => (
  config.value.privacy?.network_observability_disabled_effective === true
))
const networkObservabilityDisabledByEnvironment = computed(() => (
  currentEffectiveNetworkObservabilityDisabled.value && !currentDisableNetworkObservability.value
))
const privacyDirty = computed(() => disableNetworkObservability.value !== currentDisableNetworkObservability.value)


const modelSummary = computed(() => {
  if (!hasSavedProvider.value) return t('setup.summary.notConfigured')
  return (config.value.llm || {}).model || t('setup.summary.routerDefaults')
})

const providerProxy = computed(() => {
  if (!hasSavedProvider.value) return ''
  return ((config.value.llm || {}).proxy || '').trim()
})

const configPath = computed(() => status.value.configPath || '')

const savedSearchProvider = computed(() => (
  status.value.searchProvider
  || config.value.search_provider
  || 'duckduckgo'
))
const searchSpec = computed(() => searchProviders.value.find(p => p.providerId === capabilitiesForm.selectedSearchProvider.value) || searchProviders.value[0] || null)
const searchRequiresKey = computed(() => searchSpec.value?.requiresApiKey === true)
const searchCurrentAvailable = computed(() => (
  status.value.searchConfigured === true || savedSearchProvider.value === 'duckduckgo'
))
const searchDraftMissingKey = computed(() => (
  capabilitiesForm.searchDirty.value
  && searchRequiresKey.value
  && !capabilitiesForm.searchApiKeyValue.value.trim()
  && !capabilitiesForm.searchApiKeyEnvValue.value.trim()
  && !(
    capabilitiesForm.selectedSearchProvider.value === savedSearchProvider.value
    && status.value.searchConfigured === true
  )
))
const searchKeyPlaceholder = computed(() => (
  searchRequiresKey.value
  && capabilitiesForm.selectedSearchProvider.value === savedSearchProvider.value
  && status.value.searchConfigured === true
    ? t('setup.common.leaveBlankKeep')
    : t('setup.capabilities.searchPasteKey')
))
const savedSearchProviderLabel = computed(() => (
  searchProviders.value.find(provider => provider.providerId === savedSearchProvider.value)?.label
  || savedSearchProvider.value
))
const searchDraftStatusText = computed(() => {
  if (!capabilitiesForm.searchDirty.value) return ''
  const nextProvider = searchSpec.value?.label || capabilitiesForm.selectedSearchProvider.value
  if (capabilitiesForm.selectedSearchProvider.value !== savedSearchProvider.value) {
    return t(
      searchDraftMissingKey.value
        ? 'setup.capabilities.searchDraftSwitchMissingKey'
        : 'setup.capabilities.searchDraftSwitch',
      { current: savedSearchProviderLabel.value, next: nextProvider },
    )
  }
  return t('setup.capabilities.searchDraftCredential', {
    current: savedSearchProviderLabel.value,
  })
})
const searchEnvPlaceholder = computed(() => searchRequiresKey.value ? (searchSpec.value?.envKey || 'SEARCH_API_KEY') : t('setup.common.notRequiredForProvider'))
const searchNeeds = computed(() => credentialNeedList(searchSpec.value?.whatYouNeed, capabilitiesForm.searchApiKeyEnvValue.value || searchSpec.value?.envKey))

const savedMemoryProvider = computed(() => {
  const embedding = config.value.memory?.embedding || {}
  return embedding.provider || embedding.mode || status.value.memoryEmbeddingProvider || 'auto'
})
const savedMemoryProviderLabel = computed(() => (
  memoryProviders.value.find(provider => provider.providerId === savedMemoryProvider.value)?.label
  || savedMemoryProvider.value
))
const memorySpec = computed(() => memoryProviders.value.find(p => p.providerId === capabilitiesForm.selectedMemoryProvider.value) || memoryProviders.value[0] || null)
const memoryApiKeyEnabled = computed(() => capabilitiesForm.selectedMemoryProvider.value === 'auto' || memorySpec.value?.requiresApiKey === true)
const memoryApiKeyPlaceholder = computed(() => memoryApiKeyEnabled.value ? t('setup.common.leaveBlankKeep') : t('setup.common.notRequiredForProvider'))
const memoryEnvPlaceholder = computed(() => memorySpec.value?.envKey || 'PROVIDER_API_KEY')
const memoryNeeds = computed(() => memoryNeedList(memorySpec.value, capabilitiesForm.selectedMemoryProvider.value, capabilitiesForm.memoryApiKeyEnvValue.value || memorySpec.value?.envKey))
const memoryStatusText = computed(() => _memoryEmbeddingStatusText())
const memoryModeTitle = computed(() => {
  if (savedMemoryProvider.value === 'none') return t('setup.capabilities.memoryKeywordTitle')
  if (['auto', 'local'].includes(savedMemoryProvider.value)) {
    return capabilityResettable('memory_embedding')
      ? t('setup.capabilities.memoryCustomTitle', { provider: savedMemoryProviderLabel.value })
      : t('setup.capabilities.memoryBuiltInTitle')
  }
  return t('setup.capabilities.memoryCustomTitle', { provider: savedMemoryProviderLabel.value })
})
const memoryModeDescription = computed(() => {
  if (savedMemoryProvider.value === 'none') return t('setup.capabilities.memoryKeywordDesc')
  if (['auto', 'local'].includes(savedMemoryProvider.value)) {
    return capabilityResettable('memory_embedding')
      ? t('setup.capabilities.memoryCustomDesc', { provider: savedMemoryProviderLabel.value })
      : t('setup.capabilities.memoryBuiltInRuntime')
  }
  return t('setup.capabilities.memoryCustomDesc', { provider: savedMemoryProviderLabel.value })
})
const memoryExpandable = computed(() => capabilityResettable('memory_embedding'))

const imageSpec = computed(() => imageProviders.value.find(
  p => p.providerId === capabilitiesForm.selectedImageProvider.value,
) || null)
const imageModelCatalog = computed<ImageModelCatalog>(() => {
  const provider = normalizeProviderId(capabilitiesForm.selectedImageProvider.value)
  if (!provider) return { models: [], source: 'none' }
  return imageModelCatalogs.value[provider] || curatedImageModelCatalog(provider)
})
const imageNeeds = computed(() => {
  if (!capabilitiesForm.imageIsEnabled.value) return [t('setup.image.noKeyWhileDisabled')]
  return credentialNeedList(imageSpec.value?.whatYouNeed, capabilitiesForm.imageApiKeyEnvValue.value || imageSpec.value?.envKey)
})
const imageStatusText = computed(() => _imageGenerationStatusText())

const audioStatusText = computed(() => {
  if (status.value.audioEnabled !== true) return t('setup.audio.statusDisabled')
  if (status.value.audioConfigured === true) return t('setup.audio.statusReady')
  if (status.value.audioSource === 'missing_env') {
    return _missingEnvStatusText(t('setup.audio.title'), status.value.audioEnvKey, t('setup.audio.statusNeedsKey'))
  }
  return t('setup.audio.statusNeedsKey')
})
const audioBadgeTone = computed(() => {
  if (status.value.audioEnabled !== true) return 'is-muted'
  return status.value.audioConfigured === true ? 'is-ok' : 'is-warn'
})
const audioBadgeLabel = computed(() => {
  if (status.value.audioEnabled !== true) return t('setup.capabilities.statusPending')
  return status.value.audioConfigured === true
    ? t('setup.capabilities.statusAvailable')
    : t('setup.capabilities.statusNeedsAction')
})
const audioKeyPlaceholder = computed(() => promotedForm.audioKeyConfigured.value ? t('setup.common.leaveBlankKeep') : t('setup.audio.pasteKey'))

const providerProbeModel = computed(() => {
  const provider = normalizeProviderId(providerForm.selectedProvider.value)
  if (!provider) return ''

  // Prefer the current editor value (saved profile override or catalog direct
  // default), then reuse a model already proven relevant by a saved route or
  // ensemble member for legacy model-less profiles.
  const editorModel = currentFormModelValue()
  if (editorModel) return editorModel
  for (const tier of Object.values(config.value.squilla_router?.tiers || {})) {
    if (normalizeProviderId(tier.provider) !== provider) continue
    const model = String(tier.model || '').trim()
    if (model) return model
  }
  for (const candidate of config.value.llm_ensemble?.candidates || []) {
    if (candidate.enabled === false || normalizeProviderId(candidate.provider) !== provider) continue
    const model = String(candidate.model || '').trim()
    if (model) return model
  }
  const defaultModel = String(providerSpec.value?.defaultModel || '').trim()
  if (defaultModel) return defaultModel
  return tierModelCatalogs.value[provider]?.models[0]?.id || ''
})

const providerProbeMissingFields = computed(() => {
  if (!providerForm.selectedProvider.value) return []
  if (providerSelectionKind.value === 'profile') {
    return providerProbeModel.value ? [] : [t('setup.common.model')]
  }
  return providerFields.value
    .filter(field => field.required === true && !isProviderCredentialField(field))
    .filter(field => {
      const value = field.name === 'model'
        && editingPrimaryProvider.value
        && hasConfiguredPrimaryProvider.value
        ? currentFormModelValue()
        : providerForm.fieldValue(field, currentProviderConfig.value)
      return !String(value ?? '').trim()
    })
    .map(providerProbeFieldLabel)
})

const providerProbeDisabledReason = computed(() => {
  if (providerProbeMissingFields.value.length === 0) return ''
  return t('setup.provider.probeMissingRequired', {
    fields: providerProbeMissingFields.value.join(t('setup.provider.requiredFieldJoiner')),
  })
})

const providerCredentialPanel = computed<ProviderCredentialPanelState | null>(() => {
  if (!providerSpec.value) return null
  const selectedProviderId = String(providerForm.selectedProvider.value || '').trim().toLowerCase()
  const primaryCredential = status.value.llmCredentialStatus || {}
  const savedProviderId = String(primaryCredential.provider || '').trim().toLowerCase()
  const profileCredential = selectedProfileStatus.value
  const savedMatchesSelected = editingPrimaryProvider.value
    ? selectedProviderId !== '' && savedProviderId === selectedProviderId
    : providerSelectionKind.value === 'profile' && Boolean(profileCredential)
  const savedCredential = editingPrimaryProvider.value
    ? {
        available: primaryCredential.available,
        source: primaryCredential.source,
        envKey: primaryCredential.envKey,
        masked: primaryCredential.masked,
        revealAllowed: primaryCredential.revealAllowed,
      }
    : {
        available: profileCredential?.ready,
        source: profileCredential?.credentialSource,
        envKey: profileCredential?.credentialEnv,
        // Profile status is deliberately redacted more aggressively than the
        // primary credential status. An empty mask keeps the field write-only.
        masked: '',
        revealAllowed: false,
      }
  const configuredPrimaryEnv = String((config.value.llm || {}).api_key_env || '').trim()
  const storedProfile = storedProfileConfig(selectedProviderId)
  const configuredProfileEnv = String(storedProfile.api_key_env || '').trim()
  const configuredProfilePool = Array.isArray(storedProfile.api_key_env_pool)
    && storedProfile.api_key_env_pool.some(value => String(value || '').trim())
  const rawProfileCredentialSource = String(profileCredential?.credentialSource || '')
  const removable = savedMatchesSelected && (
    editingPrimaryProvider.value
      ? savedCredential.source === 'explicit' || Boolean(configuredPrimaryEnv)
      : (
          ['profile', 'profile_pool', 'profile_pool_env', 'profile_env', 'explicit'].includes(
            rawProfileCredentialSource,
          )
          || (
            ['env', 'missing_env'].includes(rawProfileCredentialSource)
            && (Boolean(configuredProfileEnv) || configuredProfilePool)
          )
        )
  )
  const requiresApiKey = providerSpec.value.requiresApiKey !== false
  const acceptsApiKey = providerSpec.value.acceptsApiKey !== undefined
    ? providerSpec.value.acceptsApiKey === true
    // Older gateways do not publish acceptsApiKey. Fall back conservatively:
    // a provider that requires a key necessarily accepts one, while an
    // optional/keyless provider stays keyless instead of exposing a control
    // whose semantics that gateway never advertised.
    : requiresApiKey
  const hasDraftKey = String(providerForm.providerFieldValues.value.api_key || '').trim().length > 0
  const apiKeyEnvValue = providerForm.fieldValue(
    { name: 'api_key_env', label: t('setup.common.apiKeyEnv'), default: providerSpec.value.envKey || '' },
    savedMatchesSelected ? providerEditorConfig.value : {},
  )
  const hasDraftEnv = String(apiKeyEnvValue || '').trim().length > 0
  const envReferenceEdited = hasDraftEnv && providerForm.fieldTouched('api_key_env')
  const credentialReady = !requiresApiKey || hasDraftKey || envReferenceEdited || (
    savedMatchesSelected && savedCredential.available === true
  )
  const fieldsReady = providerProbeMissingFields.value.length === 0
  const probeDisabledReason = !fieldsReady
    ? providerProbeDisabledReason.value
    : (!credentialReady ? t('setup.provider.addKeyToTestHint') : '')

  return {
    providerLabel: providerSpec.value.label || providerForm.selectedProvider.value,
    providerSelected: Boolean(providerForm.selectedProvider.value),
    acceptsApiKey,
    requiresApiKey,
    available: savedMatchesSelected ? savedCredential.available === true : !requiresApiKey,
    removable,
    removing: providerCredentialRemovalPending.value,
    source: savedMatchesSelected
      ? String(savedCredential.source || 'none')
      : (requiresApiKey ? 'none' : 'not_required'),
    envKey: savedMatchesSelected
      ? String(savedCredential.envKey || providerSpec.value.envKey || '')
      : String(providerSpec.value.envKey || ''),
    masked: savedMatchesSelected ? String(savedCredential.masked || '') : '',
    revealAllowed: savedMatchesSelected ? savedCredential.revealAllowed === true : false,
    revealed: providerForm.revealedCredential.value,
    revealError: providerForm.revealError.value,
    replacing: providerForm.replacingCredential.value,
    apiKeyValue: String(providerForm.providerFieldValues.value.api_key || ''),
    apiKeyEnvValue,
    draftCredentialSource: hasDraftKey ? 'key' : (envReferenceEdited ? 'env' : ''),
    probeReady: Boolean(providerForm.selectedProvider.value) && fieldsReady && credentialReady,
    probeDisabledReason,
    probeButtonLabel: credentialReady ? t('setup.provider.testCurrentSettings') : t('setup.provider.addKeyToTest'),
    connection: providerForm.connection.value,
    onReveal: revealProviderCredential,
    // Hiding is local-only and should remain available even while a save or
    // another provider action temporarily locks interactions.
    onHideReveal: () => providerForm.hideRevealedCredential(),
    onReplace: () => {
      if (!providerInteractionLocked()) providerForm.startCredentialReplace()
    },
    onCancelReplace: () => {
      if (!providerInteractionLocked()) providerForm.cancelCredentialReplace()
    },
    onRemoveCredential: () => {
      void removeProviderCredential()
    },
  }
})

const selectedStoredProfile = computed(() => providerSelectionKind.value === 'profile')
const selectedNewProfile = computed(() => providerSelectionKind.value === 'new')
const ensembleProviderIds = computed(() => {
  const ensemble = config.value.llm_ensemble || {}
  const providers = new Set<string>()
  if (ensemble.enabled !== true) return providers

  const add = (value: unknown) => {
    const provider = normalizeProviderId(value)
    if (provider) providers.add(provider)
  }

  // The primary deployment is the baseline from which an enabled ensemble is
  // selected and the fallback target if the ensemble cannot produce a result.
  // A fixed/custom lineup on a different provider is therefore cross-provider
  // even when every ensemble member happens to share that foreign provider.
  add(currentProvider.value)

  const selectionMode = String(ensemble.selection_mode || '')
  const staticProfile = STATIC_B5_PROFILES[selectionMode]
  if (staticProfile) {
    add(staticProfile.provider)
    return providers
  }

  if (selectionMode === CUSTOM_B5_SELECTION_MODE) {
    for (const candidate of ensemble.candidates || []) {
      if (candidate.enabled === false) continue
      add(candidate.provider)
    }
    return providers
  }

  // selection_mode was introduced after model_options. Treat an omitted mode
  // with legacy options as router_dynamic for older Gateway compatibility;
  // unknown explicit modes fail closed instead of advertising a capability
  // the runtime would reject.
  const legacyDynamic = !selectionMode && (ensemble.model_options || []).length > 0
  if (selectionMode === ROUTER_DYNAMIC_SELECTION_MODE || legacyDynamic) {
    for (const candidate of ensemble.candidates || []) {
      if (candidate.enabled === false) continue
      add(candidate.provider || currentProvider.value)
    }
    for (const tier of Object.values(config.value.squilla_router?.tiers || {})) {
      add(tier.provider || currentProvider.value)
    }
    const modelOptions = ensemble.model_options || []
    const isCurrentRuntimeLegacyDefault = selectionMode === ROUTER_DYNAMIC_SELECTION_MODE
      && modelOptions.length === LEGACY_OPENROUTER_MODEL_OPTIONS.length
      && modelOptions.every((model, index) => model === LEGACY_OPENROUTER_MODEL_OPTIONS[index])
    for (const model of isCurrentRuntimeLegacyDefault ? [] : modelOptions) {
      const modelId = String(model || '').trim()
      if (!modelId) continue
      add(modelId.includes('/') ? 'openrouter' : currentProvider.value)
    }
  }
  return providers
})

const multiProviderRoutingEnabled = computed(() => (
  hasConfiguredPrimaryProvider.value
  && (
    (
      config.value.squilla_router?.enabled === true
      && config.value.squilla_router?.cross_provider_tiers === true
    )
    || (
      config.value.llm_ensemble?.enabled === true
      && ensembleProviderIds.value.size > 1
    )
  )
))
const crossProviderRoutingEnabled = computed(() => (
  config.value.squilla_router?.enabled === true
  && config.value.squilla_router?.cross_provider_tiers === true
))
const modelRouterEnabled = computed(() => config.value.squilla_router?.enabled === true)
const routerBinding = computed<'follow_primary' | 'custom' | 'legacy'>(() => {
  const value = String(
    ((status.value.sectionDetails || {}).router || {}).routerBinding
    || config.value.squilla_router?.preset_binding
    || '',
  )
    .trim()
    .toLowerCase()
  if (value === 'follow_primary' || value === 'custom') return value
  return 'legacy'
})
const ensembleEnabled = computed(() => config.value.llm_ensemble?.enabled === true)
function routerConflictsWithTarget(value: string): boolean {
  const target = normalizeProviderId(value)
  if (
    !target
    || !modelRouterEnabled.value
    || routerBinding.value === 'follow_primary'
    || crossProviderRoutingEnabled.value
  ) return false
  return Object.entries(config.value.squilla_router?.tiers || {}).some(([name, tier]) => {
    if (!routerTierProviderParticipates(name, {
      provider: tier.provider || '',
      model: tier.model || '',
      thinkingLevel: tier.thinkingLevel || tier.thinking_level || '',
      supportsImage: tier.supportsImage || tier.supports_image || false,
      ensembleEnabled: typeof tier.ensembleEnabled === 'boolean'
        ? tier.ensembleEnabled
        : tier.ensemble_enabled,
      ensembleSelectionMode: tier.ensembleSelectionMode || tier.ensemble_selection_mode || '',
    }, routerForm.routerProviderRoles.value)) return false
    const provider = normalizeProviderId(tier.provider)
    return Boolean(provider && provider !== target)
  })
}
const activationRouterConflict = computed(() => (
  routerConflictsWithTarget(providerActivation.value.providerId)
))

const providerFormPanel = providerForm.createPanel({
  currentConfig: providerEditorConfig,
  providerSummary,
  runtimeProviders,
  routerSupportTone,
  routerSupportText,
  canConfigureRouter,
  providerNeeds,
  providerCoreFields,
  providerAdvancedFields,
  providerCredentialPanel,
  providerAdvancedOpen,
  providerEnvMissing,
  providerEnvKey,
  providerEnvCommand,
  llmTimeoutSeconds: promotedForm.llmTimeoutSeconds,
  contextWindowTokens: promotedForm.contextWindowTokens,
  contextWindowGlobal,
  effectiveMaxTokens,
  effectiveMaxTokensPending,
  providerIsLocal,
  configuredProviders,
  editingPrimary: editingPrimaryProvider,
  selectedStoredProfile,
  editingNew: selectedNewProfile,
  routingEnabled: multiProviderRoutingEnabled,
  routerEnabled: modelRouterEnabled,
  routerBinding,
  crossProviderRoutingEnabled,
  ensembleEnabled,
  activationRouterConflict,
  configuredProviderProbes,
  activation: providerActivation,
})
const profileSaveSupported = computed(() => (
  typeof rpc.supportsMethod !== 'function'
  || rpc.supportsMethod('onboarding.llmProfile.upsert')
))
const primaryProviderRemovalSupported = computed(() => (
  typeof rpc.supportsMethod !== 'function'
  || rpc.supportsMethod('onboarding.llmProfile.active.remove')
))
const providerPanel = computed(() => {
  const panel = providerFormPanel.value
  return {
    ...panel,
    // Once a primary provider exists, llm.model is owned by Model Routing.
    // Keep the legacy Model Service field as a synchronized secondary view so
    // older operator habits still work without creating a second draft.
    providerFieldValue: (field: Parameters<typeof panel.providerFieldValue>[0]) => (
      editingPrimaryProvider.value
      && hasConfiguredPrimaryProvider.value
      && field.name === 'model'
        ? (
            normalizeProviderId(modelStrategyForm.fixedProvider.value) === normalizeProviderId(currentProvider.value)
              ? modelStrategyForm.fixedModel.value
              : currentModel.value
          )
        : panel.providerFieldValue(field)
    ),
    credentialRemovalPending: providerCredentialRemovalPending.value,
    profileSaveSupported: profileSaveSupported.value,
    primaryProviderRemovalSupported: primaryProviderRemovalSupported.value,
    imageGenerationOffer: providerImageGenerationOffer.value,
    imageGenerationOptIn: providerImageGenerationOptIn.value,
  }
})

const behaviorPanel = behaviorForm.createPanel({
  statusText: behaviorStatusText,
})

const privacyPanel = computed(() => ({
  networkReportingEnabled: !(
    disableNetworkObservability.value || networkObservabilityDisabledByEnvironment.value
  ),
  networkReportingForcedOff: networkObservabilityDisabledByEnvironment.value,
}))

const memoryPanel = computed(() => ({
  autoCapture: promotedForm.memoryAutoCapture.value,
  autoCaptureDirty: promotedForm.captureDirty.value,
}))

const isOpenrouterProvider = computed(() => currentProvider.value.toLowerCase() === 'openrouter')
const normalizedProvider = computed(() => currentProvider.value.toLowerCase())
const modelStrategyCredentialStatus = computed<EnsembleCredentialStatus[]>(() => {
  // Existing ensemble-specific entries remain authoritative for compatibility;
  // profile statuses fill providers that were not already present. Older
  // gateways simply omit llmProfileStatus and retain the previous behavior.
  const rows: EnsembleCredentialStatus[] = [
    ...(status.value.ensembleCredentialStatus || []).map(row => ({ ...row })),
  ]
  const seen = new Set(rows.map(row => normalizeProviderId(row.provider)))
  const active = status.value.llmCredentialStatus
  const activeProvider = normalizeProviderId(active?.provider)
  if (activeProvider && !seen.has(activeProvider)) {
    seen.add(activeProvider)
    rows.push({
      provider: activeProvider,
      available: active?.available === true,
      source: String(active?.source || 'none'),
      envKey: active?.envKey,
    })
  }
  for (const profile of status.value.llmProfileStatus || []) {
    const provider = normalizeProviderId(profile.provider)
    if (!provider || seen.has(provider)) continue
    seen.add(provider)
    rows.push({
      provider,
      available: profile.ready === true,
      source: String(profile.credentialSource || 'none'),
      envKey: profile.credentialEnv,
      reason: profile.reason,
    })
  }
  return rows
})
// openrouter-mix is only valid for the openrouter provider. When the selection
// moves off openrouter while a stored mix mode is loaded, coerce the mode back
// to recommended so the save payload stays valid for the new provider. watch
// only fires on transitions, so an initial load never trips this.
watch(normalizedProvider, (provider) => {
  if (provider !== 'openrouter' && routerForm.mode.value === 'openrouter-mix') {
    routerForm.setRouterMode('recommended')
  }
})
watch(
  () => normalizeProviderId(providerForm.selectedProvider.value),
  () => {
    providerImageGenerationOptIn.value = true
  },
)
const routerPanel = routerForm.createPanel({
  routerSummary,
  ensembleProfileActive,
  hasSavedProvider,
  isOpenrouter: isOpenrouterProvider,
  textTiers: TEXT_TIERS,
  tierLabel,
  providerOptions: routingProviderOptions,
  providerCredentialStatus: modelStrategyCredentialStatus,
  discoveredModelsByProvider: computed(() => {
    const catalogs: DiscoveredModelsByProvider = { ...tierModelCatalogs.value }
    const provider = normalizeProviderId(providerForm.selectedProvider.value)
    if (provider) {
      catalogs[provider] = {
        models: providerForm.connection.value.models,
        source: providerForm.connection.value.modelSource,
      }
    }
    return catalogs
  }),
})

const ensembleTierCandidates = computed(() => routerPanel.value.tierRows
  .filter(row => (
    (TEXT_TIERS as readonly string[]).includes(row.name)
    && configuredProviderIds.value.has(normalizeProviderId(row.provider))
  ))
  .map(row => ({
    provider: row.provider,
    model: row.model,
    tier: row.name,
  })))

// ---------------------------------------------------------------------------
// Routing preset card (Provider panel)
// ---------------------------------------------------------------------------

const selectedPreset = computed<ProviderPresetSpec | null>(() => providerSpec.value?.presets?.[0] || null)

const presetRouterMode = computed(() => (
  String(((status.value.sectionDetails || {}).router || {}).routerMode || 'recommended')
))

// "Configured beyond defaults": any unsaved router edits, or a persisted
// non-recommended mode. A pristine install (no config file yet) defaults to
// openrouter-mix on the wire, but nothing deliberate exists to clobber there,
// so the apply path stays available for true first-run beginners.
const presetRouterCustomized = computed(() => {
  if (routerForm.isDirty.value) return true
  if (status.value.hasConfig === false) return false
  return presetRouterMode.value !== 'recommended'
})

function presetTierRows(preset: ProviderPresetSpec | null): SetupTierRow[] {
  const tiers = preset?.tiers || {}
  const order: string[] = [...TEXT_TIERS, IMAGE_TIER]
  return Object.entries(tiers)
    .map(([name, tier]) => ({ name: normalizeRouterTier(name) || name, tier }))
    .filter(({ name }) => order.includes(name))
    .sort((a, b) => order.indexOf(a.name) - order.indexOf(b.name))
    .map(({ name, tier }) => ({
      name,
      provider: tier.provider || '',
      model: tier.model || '',
      thinkingLevel: tier.thinkingLevel || tier.thinking_level || '',
      supportsImage: tier.supportsImage || tier.supports_image || false,
      ensembleEnabled: typeof tier.ensembleEnabled === 'boolean'
        ? tier.ensembleEnabled
        : tier.ensemble_enabled,
      ensembleSelectionMode:
        tier.ensembleSelectionMode || tier.ensemble_selection_mode || '',
    }))
}

const presetPanel = computed(() => ({
  hasPreset: Boolean(selectedPreset.value),
  presetLabel: selectedPreset.value?.label || '',
  presetDescription: selectedPreset.value?.description || '',
  synthesized: selectedPreset.value?.synthesized === true,
  tierRows: presetTierRows(selectedPreset.value),
  tierLabel,
  routerMode: presetRouterMode.value,
  routerCustomized: presetRouterCustomized.value,
}))

const ensembleStatusText = computed(() => (
  ensembleForm.enabled.value ? t('setup.ensemble.statusOn') : t('setup.ensemble.statusOff')
))

const ensemblePanel = ensembleForm.createPanel({
  statusText: ensembleStatusText,
  activeProvider: currentProvider,
  activeModel: currentModel,
  tierCandidates: modelStrategyTierCandidates,
  credentialStatus: modelStrategyCredentialStatus,
})

const emptyFixedModelCatalog: DiscoveredModelCatalog = { models: [], source: 'none' }
const fixedModelCatalog = computed<DiscoveredModelCatalog>(() => {
  const provider = normalizeProviderId(
    modelStrategyForm.fixedProvider.value || currentProvider.value,
  )
  if (!provider) return emptyFixedModelCatalog
  if (provider === normalizeProviderId(providerForm.selectedProvider.value)) {
    return {
      models: providerForm.connection.value.models,
      source: providerForm.connection.value.modelSource,
    }
  }
  return tierModelCatalogs.value[provider] || emptyFixedModelCatalog
})

const modelStrategyPanel = modelStrategyForm.createPanel({
  hasSavedProvider,
  profileSaveSupported,
  providerLabel: providerSummary,
  routerPanel,
  ensemblePanel,
  routerTemplateState: routerForm.tierTemplateState,
  fixedModelCatalog,
})


const capabilitiesPanel = capabilitiesForm.createPanel({
  searchProviders,
  memoryProviders,
  imageProviders,
  imageSpec,
  imageRecommendation,
  imageCredentialOptions,
  imageModels: computed(() => imageModelCatalog.value.models),
  imageModelSource: computed(() => imageModelCatalog.value.source),
  searchRequiresKey,
  searchKeyPlaceholder,
  searchDraftDirty: capabilitiesForm.searchDirty,
  searchDraftMissingKey,
  searchDraftStatusText,
  searchEnvPlaceholder,
  searchAdvancedOpen: capabilitiesForm.searchAdvancedOpen,
  searchNeeds,
  searchEnvCommand,
  searchStatusText,
  memoryApiKeyEnabled,
  memoryRemoteOptionsOpen: capabilitiesForm.memoryRemoteOptionsOpen,
  memoryRemoteOptionsSummary: capabilitiesForm.memoryRemoteOptionsSummary,
  memoryModelPlaceholder: capabilitiesForm.memoryModelPlaceholder,
  memoryBasePlaceholder: capabilitiesForm.memoryBasePlaceholder,
  memoryOnnxPlaceholder: capabilitiesForm.memoryOnnxPlaceholder,
  memoryApiKeyLabel: capabilitiesForm.memoryApiKeyLabel,
  memoryApiKeyPlaceholder,
  memoryEnvPlaceholder,
  memoryNeeds,
  memoryStatusText,
  memoryModeTitle,
  memoryModeDescription,
  memoryExpandable,
  memoryEnvCommand,
  imageNeeds,
  imageStatusText,
  imageEnvCommand,
  capabilityBadgeTone,
  capabilityBadgeLabel,
  memoryAutoCapture: promotedForm.memoryAutoCapture,
  audioEnabled: promotedForm.audioEnabled,
  audioApiKey: promotedForm.audioApiKey,
  audioApiKeyEnv: promotedForm.audioApiKeyEnv,
  audioBaseUrl: promotedForm.audioBaseUrl,
  audioTtsVoice: promotedForm.audioTtsVoice,
  audioTtsModel: promotedForm.audioTtsModel,
  audioLanguageCode: promotedForm.audioLanguageCode,
  audioStatusText,
  audioBadgeTone,
  audioBadgeLabel,
  audioKeyPlaceholder,
  resettable: capabilityResettable,
  resetPending: capabilityResetPending,
})

const hasSetupAction = computed(() => {
  const details = status.value.sectionDetails || {}
  const actionableDetails = Object.entries(details).filter(([, detail]) => (
    detail.blocking || detail.actionRequired || detail.status === 'missing' || detail.status === 'degraded'
  ))
  // Older gateways may report a global onboarding warning for Channels. Once
  // that is the only actionable detail, it must not create a Settings banner.
  if (actionableDetails.length > 0) {
    return actionableDetails.some(([name]) => name !== 'channels')
  }
  return Boolean(status.value.needsOnboarding)
})

// Banner items: one row per pending action, each deep-linking to its section.
const actionItems = computed<SettingsActionItem[]>(() => {
  if (!hasSetupAction.value) return []
  const items: SettingsActionItem[] = []
  const seen = new Set<string>()
  const push = (label: string, target: SettingsSectionId) => {
    if (seen.has(label)) return
    seen.add(label)
    items.push({ label, section: target })
  }
  const llm = config.value.llm || {}
  if (providerEnvMissing.value) {
    push(t('setup.action.envNotVisible', { envKey: providerEnvKey.value }), 'provider')
  } else if (!llm.provider || !llm.model) {
    push(t('setup.action.connectProvider'), 'provider')
  }
  const details = status.value.sectionDetails || {}
  Object.entries(details).forEach(([name, detail]) => {
    if (name === 'channels') return
    if (!detail.blocking && !detail.actionRequired) return
    if (name === 'llm' || name === 'provider') {
      push(t('setup.action.connectProvider'), 'provider')
      return
    }
    push(setupActionReason(name, detail), sectionForDetailName(name) || 'provider')
  })
  if (!items.length) push(t('setup.action.reviewPending'), 'provider')
  return items
})

const configCliArg = computed(() => {
  const path = status.value.configPath
  return path ? ` --config ${shellArg(path)}` : ''
})

const envRecoveryCommands = computed(() => {
  const cmds = Array.isArray(status.value.envRecoveryCommands) ? status.value.envRecoveryCommands : []
  return cmds
    .filter(entry => entry && entry.command)
    .map(entry => ({ label: entry.label || t('setup.command.setEnvKey'), command: entry.command || '' }))
})

const fixCommands = computed(() => {
  if (!envRecoveryCommands.value.length) return []
  return [
    ...envRecoveryCommands.value,
    { label: t('setup.command.restartAfterEnv'), command: `opensquilla gateway restart${configCliArg.value}` },
  ]
})

const handoffCommands = computed(() => [
  { label: t('setup.command.cliOnboarding'), command: `opensquilla onboard --if-needed${configCliArg.value}` },
  { label: t('setup.command.checkStatus'), command: `opensquilla onboard status${configCliArg.value}` },
])

const recipeCommands = computed(() => [
  { label: t('setup.command.providerOptions'), command: `opensquilla onboard catalog providers${configCliArg.value}` },
  { label: t('setup.command.routerTiers'), command: `opensquilla onboard catalog router${configCliArg.value}` },
  { label: t('setup.command.searchOptions'), command: `opensquilla onboard catalog search${configCliArg.value}` },
  { label: t('setup.command.channelOptions'), command: `opensquilla onboard catalog channels${configCliArg.value}` },
  { label: t('setup.command.imageOptions'), command: `opensquilla onboard catalog image${configCliArg.value}` },
  { label: t('setup.command.memoryOptions'), command: `opensquilla onboard catalog memory${configCliArg.value}` },
])

const configSummary = computed(() => {
  const rows: Array<{ label: string; value: string }> = [
    { label: t('setup.summary.provider'), value: providerSummary.value },
    { label: t('setup.summary.model'), value: modelSummary.value },
  ]
  if (providerProxy.value) rows.push({ label: t('setup.summary.proxy'), value: providerProxy.value })
  rows.push({ label: t('setup.summary.router'), value: routerSummary.value })
  rows.push({ label: t('setup.summary.channels'), value: String(status.value.channelCount || 0) })
  return rows
})

// ---------------------------------------------------------------------------
// Section state
// ---------------------------------------------------------------------------

function isSectionId(value: string): value is SettingsSectionId {
  return SETTINGS_SECTIONS.some(s => s.id === value)
}

// target: explicit section id, 'auto' (first not-ready), or null (first section).
function selectInitialSection(target: string | null) {
  if (target && target !== 'auto' && isSectionId(target)) {
    setSection(target)
    return
  }
  setSection(target === 'auto' ? firstActionSection() : 'provider')
}

function firstActionSection(): SettingsSectionId {
  const details = status.value.sectionDetails || {}
  // Kept in sync with the SETTINGS_SECTIONS rail order so `/settings/auto` lands
  // on the first not-ready section in the same top-to-bottom order the rail reads
  // (Provider -> Model Strategy -> Capabilities).
  const sectionOrder: Array<[string, SettingsSectionId]> = [
    ['llm', 'provider'],
    ['router', 'modelStrategy'],
    ['ensemble', 'modelStrategy'],
    ['search', 'capabilities'],
    ['image_generation', 'capabilities'],
    ['memory_embedding', 'capabilities'],
    ['audio', 'capabilities'],
  ]
  const entry = sectionOrder.find(([name]) => {
    const detail = details[name] || {}
    return stepDetailNeedsAction(detail)
  })
  if (entry) return entry[1]
  if (providerEnvMissing.value) return 'provider'
  return 'provider'
}

function sectionStatus(sectionId: string): { label: string; tone: string } {
  if (sectionId === 'gateway') {
    if (rpc.isConnected) return { label: t('setup.connection.connected'), tone: 'is-ok' }
    if (rpc.isConnecting) return { label: t('setup.connection.connecting'), tone: 'is-muted' }
    return { label: t('setup.connection.disconnected'), tone: 'is-warn' }
  }
  if (sectionId === 'provider') {
    if (providerEnvMissing.value) return { label: t('setup.readiness.needsAction'), tone: 'is-warn' }
    return detailStepStatus((status.value.sectionDetails || {}).llm || (status.value.sectionDetails || {}).provider)
  }
  // General/Security/Memory are always-valid preference toggles, not
  // readiness milestones — a neutral dot (rather than a green "Live" that
  // overstates earned readiness) is honest; the dirty pip already signals
  // unsaved edits.
  if (
    sectionId === 'general'
    || sectionId === 'securityPrivacy'
    || sectionId === 'memory'
    || sectionId === 'ensemble'
  ) {
    return { label: t('setup.status.appliesOnSave'), tone: 'is-muted' }
  }
  if (sectionId === 'modelStrategy' && !hasSavedProvider.value) {
    return { label: t('setup.status.providerFirst'), tone: 'is-muted' }
  }
  if (sectionId === 'modelStrategy') return aggregateStepStatus(['router', 'ensemble'])
  if (sectionId === 'capabilities') {
    return aggregateStepStatus(['search', 'image_generation', 'memory_embedding', 'audio'])
  }
  return { label: t('setup.status.review'), tone: 'is-muted' }
}

function detailStepStatus(detail?: SectionDetail): { label: string; tone: string } {
  if (!detail) return { label: t('setup.status.review'), tone: 'is-muted' }
  if (stepDetailNeedsAction(detail)) return { label: t('setup.readiness.needsAction'), tone: 'is-warn' }
  if (detail.status === 'ok') return { label: t('setup.readiness.ready'), tone: 'is-ok' }
  return { label: readinessLabel(detail.status || '') || t('setup.readiness.optional'), tone: 'is-muted' }
}

function aggregateStepStatus(sectionNames: string[]): { label: string; tone: string } {
  const details = status.value.sectionDetails || {}
  const entries = sectionNames.map(name => details[name]).filter(Boolean) as SectionDetail[]
  if (entries.some(detail => stepDetailNeedsAction(detail))) {
    return { label: t('setup.readiness.needsAction'), tone: 'is-warn' }
  }
  if (entries.length && entries.every(detail => detail.status === 'ok')) {
    return { label: t('setup.readiness.ready'), tone: 'is-ok' }
  }
  return { label: t('setup.readiness.optional'), tone: 'is-muted' }
}

function stepDetailNeedsAction(detail: SectionDetail): boolean {
  return Boolean(detail && (detail.blocking || detail.actionRequired || detail.status === 'missing' || detail.status === 'degraded'))
}

function setupActionReason(name: string, detail: SectionDetail): string {
  const missingEnvPrefix = 'env key not visible: '
  const detailText = String(detail.detail || '')
  if (detailText.startsWith(missingEnvPrefix)) {
    const envKey = detailText.slice(missingEnvPrefix.length).trim()
    if (envKey) return t('setup.action.envNotVisible', { envKey })
  }
  return t('setup.action.setupNeeded', { label: detail.label || name })
}

function sectionForDetailName(name: string): SettingsSectionId | null {
  if (name === 'llm' || name === 'provider') return 'provider'
  if (name === 'router' || name === 'ensemble') return 'modelStrategy'
  if (name === 'search' || name === 'image_generation' || name === 'memory_embedding' || name === 'audio') return 'capabilities'
  return null
}

// ---------------------------------------------------------------------------
// Dirty state
// ---------------------------------------------------------------------------

const providerDirty = computed(() => (
  providerForm.isDirty.value
  || (providerOwnsFixedModelDraft.value && modelStrategyForm.fixedModelDirty.value)
  || (editingPrimaryProvider.value && promotedForm.timeoutDirty.value)
  || (editingPrimaryProvider.value && promotedForm.contextWindowDirty.value)
))
const behaviorDirty = computed(() => behaviorForm.isDirty.value)
const securityPrivacyDirty = computed(() => privacyDirty.value)
const memorySettingsDirty = computed(() => promotedForm.captureDirty.value)
const modelStrategyDirty = computed(() => (
  routerForm.isDirty.value
  || ensembleForm.isDirty.value
  || modelStrategyForm.fixedProviderDirty.value
  || (modelStrategyForm.fixedModelDirty.value && !providerOwnsFixedModelDraft.value)
))
const capabilitiesDirty = computed(() => (
  capabilitiesForm.searchDirty.value
  || capabilitiesForm.memoryDirty.value
  || capabilitiesForm.imageDirty.value
  || promotedForm.audioDirty.value
))

function sectionDirty(sectionId: string): boolean {
  // Provider drafts are intentionally absent from the global settings dirty
  // state. Their editor owns an explicit Save changes action.
  if (sectionId === 'provider') return false
  if (sectionId === 'general') return behaviorDirty.value
  if (sectionId === 'securityPrivacy') return securityPrivacyDirty.value
  if (sectionId === 'memory') return memorySettingsDirty.value
  if (sectionId === 'modelStrategy') return modelStrategyDirty.value
  if (sectionId === 'capabilities') return capabilitiesDirty.value
  return false
}

const dirtySections = computed(() => SETTINGS_SECTIONS.filter(s => sectionDirty(s.id)))
const hasUnsavedChanges = computed(() => dirtySections.value.length > 0)

async function saveDirtySections() {
  if (saveAllRequestPending) return
  saveAllRequestPending = true
  saveAllPending.value = true
  try {
    // Snapshot every section before the first await. Individual save actions can
    // otherwise refresh the catalog and make later dirty flags disappear while
    // their drafts are still waiting to be persisted.
    const work = {
      privacy: securityPrivacyDirty.value,
      memoryCapture: memorySettingsDirty.value,
      provider: false,
      behavior: behaviorDirty.value,
      modelStrategy: modelStrategyDirty.value,
      search: capabilitiesForm.searchDirty.value,
      memory: capabilitiesForm.memoryDirty.value,
      image: capabilitiesForm.imageDirty.value,
      audio: promotedForm.audioDirty.value,
    }
    if (!Object.values(work).some(Boolean)) return

    // A configured primary provider and Model Routing share llm.model. Validate
    // the canonical draft before any earlier section performs a remote write.
    if (
      (modelStrategyForm.fixedProviderDirty.value || modelStrategyForm.fixedModelDirty.value)
      && !modelStrategyForm.fixedModel.value.trim()
    ) {
      pushToast(t('setup.toast.chooseFixedModel'), { tone: 'danger' })
      return
    }
    if (work.search && searchDraftMissingKey.value) {
      pushToast(t('setup.capabilities.searchKeyRequired'), { tone: 'danger' })
      return
    }

    const selectedProviderId = normalizeProviderId(providerForm.selectedProvider.value)
    const restoreProfileSelection = providerSelectionKind.value !== 'primary'
    if (work.privacy && !(await savePrivacy(disableNetworkObservability.value, { reload: false }))) return
    if (work.memoryCapture && !(await saveMemoryAutoCapture({ reload: false }))) return
    if (work.behavior && !(await saveBehavior({ reload: false }))) return
    if (work.modelStrategy && !(await saveModelStrategy({
      reload: false,
      allowUnsavedProvider: false,
    }))) return
    if (work.search && !(await saveSearch({ reload: false }))) return
    if (work.memory && !(await saveMemory({ reload: false }))) return
    if (work.image && !(await saveImage({ reload: false }))) return
    if (work.audio && !(await saveAudio({ reload: false }))) return

    await loadData()
    if (
      restoreProfileSelection
      && selectedProviderId
      && selectedProviderId !== normalizeProviderId(currentProvider.value)
      && storedProfileIds.value.has(selectedProviderId)
    ) {
      applyConfiguredProviderSelection(selectedProviderId)
    }
  } finally {
    saveAllPending.value = false
    saveAllRequestPending = false
  }
}

async function discardChanges() {
  if (saveAllRequestPending) return
  if (providerInteractionLocked()) return
  await loadData()
}

// ---------------------------------------------------------------------------
// Provider helpers
// ---------------------------------------------------------------------------

function isProviderAdvancedField(field: FieldSpec): boolean {
  if (['base_url', 'proxy'].includes(field.name)) return true
  if (field.name === 'model') {
    return providerSpec.value?.routerSupported === true && field.required !== true
  }
  return false
}

function isProviderCredentialField(field: FieldSpec): boolean {
  return field.name === 'api_key' || field.name === 'api_key_env'
}

function providerProbeFieldLabel(field: FieldSpec): string {
  if (field.name === 'model') return t('setup.common.model')
  if (field.name === 'base_url') return t('setup.common.baseUrl')
  return field.label
}

function providerInteractionLocked(): boolean {
  return (
    providerSavePending.value
    || providerActivationRequestPending
    || providerActivation.value.phase === 'activating'
    || providerCredentialRemovalPending.value
  )
}

function selectProvider(value: string) {
  if (providerInteractionLocked()) return
  providerForm.selectProvider(value)
}

function applyConfiguredProviderSelection(value: string) {
  const provider = normalizeProviderId(value)
  if (!provider) return
  if (provider === normalizeProviderId(currentProvider.value)) {
    providerSelectionKind.value = 'primary'
    providerForm.initFromConfig(config.value.llm || {}, status.value, runtimeProviders.value, true)
  } else {
    providerSelectionKind.value = 'profile'
    providerForm.initStoredProfile(provider, storedProfileConfig(provider))
  }
  promotedForm.reseedContextWindow(
    config.value,
    provider,
    provider === normalizeProviderId(currentProvider.value) ? currentFormModelValue() : '',
  )
  // Resolve the selected saved deployment without issuing a chat/probe call.
  // initFromConfig/initStoredProfile bumped the form epoch, so a response from
  // the previously selected row cannot overwrite this provider's catalog.
  void providerForm.discoverModels({ storedProfile: providerSelectionKind.value === 'profile' })
}

function selectConfiguredProvider(value: string) {
  if (providerInteractionLocked()) return
  applyConfiguredProviderSelection(value)
}

async function confirmProviderDraftDiscard(): Promise<boolean> {
  // Provider drafts are provisional until verification succeeds. Do not route
  // them through the settings-wide "unsaved changes" confirmation model.
  return true
}

async function requestSelectConfiguredProvider(value: string) {
  if (providerInteractionLocked()) return
  const next = normalizeProviderId(value)
  if (!next) return
  if (providerFixedModelDraftSnapshot.value == null) {
    providerFixedModelDraftSnapshot.value = {
      provider: modelStrategyForm.fixedProvider.value,
      model: modelStrategyForm.fixedModel.value,
    }
  }
  if (next === normalizeProviderId(providerForm.selectedProvider.value)) {
    // Re-clicking the current row is not a navigation. In particular, do not
    // rehydrate from saved config and silently discard the editor's draft.
    return
  }
  if (!(await confirmProviderDraftDiscard())) return
  applyConfiguredProviderSelection(next)
}

async function requestAddProvider(value: string) {
  if (providerInteractionLocked()) return
  const next = normalizeProviderId(value)
  if (!next || !(await confirmProviderDraftDiscard())) return
  providerForm.selectProvider(next)
  onProviderChange()
}

function cancelProviderEdit() {
  if (providerInteractionLocked()) return
  if (providerOwnsFixedModelDraft.value && providerFixedModelDraftSnapshot.value != null) {
    modelStrategyForm.setFixedProvider(providerFixedModelDraftSnapshot.value.provider)
    modelStrategyForm.setFixedModel(providerFixedModelDraftSnapshot.value.model)
    providerOwnsFixedModelDraft.value = false
  }
  providerFixedModelDraftSnapshot.value = null
  const selected = normalizeProviderId(providerForm.selectedProvider.value)
  if (selected && configuredProviderIds.value.has(selected)) {
    applyConfiguredProviderSelection(selected)
    return
  }
  const fallback = normalizeProviderId(currentProvider.value)
  if (fallback && configuredProviderIds.value.has(fallback)) {
    applyConfiguredProviderSelection(fallback)
  }
}

function freshConfiguredProbe(phase: ConnectionState['phase'] = 'unverified'): ConnectionState {
  return {
    phase,
    failureKind: '',
    detail: '',
    firstResponseMs: null,
    totalMs: null,
    latencyMs: null,
    models: [],
    modelSource: 'none',
    discoverError: '',
  }
}

function providerRpcErrorMessage(err: unknown): string {
  const message = saveFailedMessage(err)
  return isRpcMethodUnavailableError(err)
    ? `${message} ${t('setup.provider.upgradeGatewayHint')}`
    : message
}

function isRpcMethodUnavailableError(err: unknown): boolean {
  return /method.*not found|unknown method|not registered/i.test(saveFailedMessage(err))
}

function representativeProviderModel(providerId: string): string {
  const provider = normalizeProviderId(providerId)
  const savedModel = String(storedProfileConfig(provider).model || '').trim()
  if (savedModel) return savedModel

  const spec = runtimeProviders.value.find(
    item => normalizeProviderId(item.providerId) === provider,
  )
  const directDefault = String(spec?.defaultDirectModel || '').trim()
  if (directDefault) return directDefault

  // Compatibility for model-less profiles created by an older Gateway: a
  // routed deployment remains a useful probe target only when the provider
  // has no catalog direct default.
  for (const tier of Object.values(config.value.squilla_router?.tiers || {})) {
    if (normalizeProviderId(tier.provider) !== provider) continue
    const model = String(tier.model || '').trim()
    if (model) return model
  }
  for (const candidate of config.value.llm_ensemble?.candidates || []) {
    if (normalizeProviderId(candidate.provider) !== provider) continue
    const model = String(candidate.model || '').trim()
    if (model) return model
  }
  return String(spec?.defaultModel || '').trim()
}

async function probeConfiguredProvider(value: string) {
  if (providerInteractionLocked()) return
  const providerId = normalizeProviderId(value)
  const row = configuredProviders.value.find(item => normalizeProviderId(item.providerId) === providerId)
  if (!providerId || !row?.ready || configuredProviderProbes.value[providerId]?.phase === 'probing') return
  configuredProviderProbes.value = {
    ...configuredProviderProbes.value,
    [providerId]: freshConfiguredProbe('probing'),
  }
  const probeEpoch = configuredProbeEpoch
  const active = providerId === normalizeProviderId(currentProvider.value)
  const model = active
    ? String(currentModel.value || representativeProviderModel(providerId))
    : representativeProviderModel(providerId)
  try {
    const res = await rpc.call<{
      ok?: boolean
      failureKind?: string
      message?: string
      firstResponseMs?: number
      totalMs?: number
      latencyMs?: number
    }>(
      active ? 'onboarding.provider.probe' : 'onboarding.llmProfile.probe',
      active ? { providerId, model } : { providerId, model },
    )
    if (probeEpoch !== configuredProbeEpoch) return
    const timings = normalizeProbeTimings(res)
    configuredProviderProbes.value = {
      ...configuredProviderProbes.value,
      [providerId]: {
        ...freshConfiguredProbe(res?.ok ? 'verified' : (res?.failureKind === 'auth_invalid' ? 'key_invalid' : 'unreachable')),
        failureKind: String(res?.failureKind || ''),
        detail: String(res?.message || ''),
        ...timings,
      },
    }
  } catch (err) {
    if (probeEpoch !== configuredProbeEpoch) return
    configuredProviderProbes.value = {
      ...configuredProviderProbes.value,
      [providerId]: {
        ...freshConfiguredProbe('unreachable'),
        detail: saveFailedMessage(err),
      },
    }
  }
}

async function activateProvider(value: string) {
  if (providerInteractionLocked()) return
  const providerId = normalizeProviderId(value)
  const row = configuredProviders.value.find(item => normalizeProviderId(item.providerId) === providerId)
  if (!providerId || !row?.primaryEligible) return
  providerActivationRequestPending = true
  try {
    const discardDraft = providerDirty.value
    if (!(await confirmProviderDraftDiscard())) return
    if (discardDraft) {
      const selected = normalizeProviderId(providerForm.selectedProvider.value)
      applyConfiguredProviderSelection(
        configuredProviderIds.value.has(selected) ? selected : currentProvider.value,
      )
    }
    // A custom/legacy Router that still names the previous provider cannot be
    // executed safely after a primary swap while cross-provider routing is off.
    // Keep its saved tiers intact and turn it off; the operator can review and
    // re-enable it deliberately from Model Routing.
    const routerAction = routerConflictsWithTarget(providerId) ? 'disable' : undefined
    providerActivation.value = {
      providerId,
      phase: 'activating',
      models: [],
      suggestedModel: '',
      error: '',
    }
    try {
      await rpc.call('onboarding.llmProfile.activate', {
        providerId,
        ...(routerAction ? { routerAction } : {}),
        // Activating a stored profile is not controlled by the primary-provider
        // editor switch. Ask the backend for the default; it still verifies the
        // stored endpoint and preserves every operator-owned image route.
        ...imageGenerationIntentPayload(providerId, {
          respectProviderEditorChoice: false,
        }),
      })
      await loadData()
      providerActivation.value = {
        providerId: '', phase: 'idle', models: [], suggestedModel: '', error: '',
      }
      pushToast(t(
        routerAction === 'disable'
          ? 'setup.toast.providerActivatedRouterDisabled'
          : 'setup.toast.providerActivated',
        { provider: providerCatalogLabel(providerId) },
      ))
    } catch (err) {
      providerActivation.value = {
        providerId: '', phase: 'idle', models: [], suggestedModel: '', error: '',
      }
      pushToast(providerRpcErrorMessage(err), { tone: 'danger' })
    }
  } finally {
    providerActivationRequestPending = false
  }
}

function setAutoSessionTitles(enabled: boolean) {
  behaviorForm.setAutoSessionTitles(enabled)
}

function setDisableNetworkObservability(enabled: boolean) {
  disableNetworkObservability.value = enabled
}

function setNetworkReportingEnabled(enabled: boolean) {
  if (networkObservabilityDisabledByEnvironment.value) return
  setDisableNetworkObservability(!enabled)
}

function setMemoryAutoCapture(enabled: boolean) {
  promotedForm.setMemoryAutoCapture(enabled)
}

function setProviderImageGenerationOptIn(enabled: boolean) {
  if (!providerImageGenerationOffer.value) return
  providerImageGenerationOptIn.value = enabled
}

function onProviderChange() {
  if (providerInteractionLocked()) return
  const provider = normalizeProviderId(providerForm.selectedProvider.value)
  if (configuredProviderIds.value.has(provider)) {
    applyConfiguredProviderSelection(provider)
    return
  }
  // config.get contains a runtime default provider even on a pristine install.
  // Until there is an effective primary deployment, the first user selection
  // must configure that primary rather than create an orphan routing profile.
  providerSelectionKind.value = hasConfiguredPrimaryProvider.value ? 'new' : 'primary'
  providerForm.resetForProvider(providerSpec.value, {
    inheritModelDefault: providerSelectionKind.value === 'new',
  })
  // The context-window override is per provider+model, so a provider switch must
  // reseed the field (value + baseline) from the newly-selected provider's saved
  // override — otherwise it keeps showing/saving the previous provider's value.
  promotedForm.reseedContextWindow(config.value, providerForm.selectedProvider.value, currentFormModelValue())
}

// The model id currently entered in the provider form (form value → saved
// config → spec default), trimmed. Drives the per-model context-window override.
function currentFormModelValue(): string {
  if (editingPrimaryProvider.value && hasConfiguredPrimaryProvider.value) {
    return (
      normalizeProviderId(modelStrategyForm.fixedProvider.value) === normalizeProviderId(currentProvider.value)
        ? modelStrategyForm.fixedModel.value
        : currentModel.value
    ).trim()
  }
  const modelField = providerFields.value.find(f => f.name === 'model') || { name: 'model', label: 'model' }
  return String(providerForm.fieldValue(modelField, providerEditorConfig.value) || '').trim()
}

function setFixedModel(value: string) {
  providerOwnsFixedModelDraft.value = false
  providerFixedModelDraftSnapshot.value = null
  updateFixedModel(value)
}

function setFixedProvider(value: string) {
  const provider = normalizeProviderId(value)
  if (!provider || !configuredProviderIds.value.has(provider)) return
  if (provider === modelStrategyForm.fixedProvider.value) return
  providerOwnsFixedModelDraft.value = false
  providerFixedModelDraftSnapshot.value = null
  modelStrategyForm.setFixedProvider(provider)
  modelStrategyForm.setFixedModel(
    provider === normalizeProviderId(currentProvider.value)
      ? String(currentModel.value || representativeProviderModel(provider))
      : representativeProviderModel(provider),
  )
  void discoverTierProviderModels(provider)
}

function updateFixedModel(value: string) {
  modelStrategyForm.setFixedModel(value)
  if (!editingPrimaryProvider.value || !hasConfiguredPrimaryProvider.value) return
  if (
    normalizeProviderId(modelStrategyForm.fixedProvider.value)
    !== normalizeProviderId(currentProvider.value)
  ) return
  const model = String(value ?? '').trim()
  promotedForm.reseedContextWindow(config.value, providerForm.selectedProvider.value, model)
  // The configured-primary editor and Model Routing share this canonical
  // model. A verdict for the previous model must never remain marked verified.
  providerForm.invalidateProbeVerdict()
}

function updateProviderField(name: string, value: unknown) {
  if (providerInteractionLocked()) return
  if (name === 'model' && editingPrimaryProvider.value && hasConfiguredPrimaryProvider.value) {
    if (providerFixedModelDraftSnapshot.value == null) {
      providerFixedModelDraftSnapshot.value = {
        provider: modelStrategyForm.fixedProvider.value,
        model: modelStrategyForm.fixedModel.value,
      }
    }
    providerOwnsFixedModelDraft.value = true
    modelStrategyForm.setFixedProvider(currentProvider.value)
    updateFixedModel(String(value ?? ''))
    return
  }
  providerForm.updateField(name, value)
  // Editing the model field switches which per-model override applies, so reseed
  // the context-window field from the saved override for the new model id.
  if (name === 'model') {
    promotedForm.reseedContextWindow(config.value, providerForm.selectedProvider.value, String(value ?? '').trim())
  }
}

async function revealProviderCredential() {
  if (providerInteractionLocked()) return
  if (!editingPrimaryProvider.value) return
  const providerId = String(providerForm.selectedProvider.value || '').trim()
  if (!providerId) return
  try {
    const res = await rpc.call<{ ok?: boolean; apiKey?: string }>('onboarding.provider.credential.reveal', { providerId })
    if (res?.ok && typeof res.apiKey === 'string' && res.apiKey.length > 0) {
      providerForm.setRevealedCredential(res.apiKey)
      return
    }
    providerForm.setRevealError(t('setup.provider.credentialRevealUnavailable'))
  } catch (err) {
    providerForm.setRevealError(saveFailedMessage(err))
  }
}

async function removeProviderCredential() {
  if (providerInteractionLocked()) return
  const credential = providerCredentialPanel.value
  const provider = normalizeProviderId(providerForm.selectedProvider.value)
  if (!credential || !provider) return
  if (!['explicit', 'env', 'missing_env'].includes(credential.source)) return
  if (!editingPrimaryProvider.value && !selectedStoredProfile.value) return

  const clearingPrimary = editingPrimaryProvider.value
  const providerLabel = credential.providerLabel || providerCatalogLabel(provider)
  const ok = await confirm({
    title: t('setup.provider.removeCredentialConfirmTitle'),
    body: t('setup.provider.removeCredentialConfirmBody', {
      provider: providerLabel,
    }),
    primaryLabel: t('setup.provider.removeCredentialConfirmPrimary'),
  })
  if (!ok) return

  providerCredentialRemovalPending.value = true
  providerForm.hideRevealedCredential()
  try {
    const method = clearingPrimary
      ? 'onboarding.provider.credential.clear'
      : 'onboarding.llmProfile.credential.clear'
    const response = await rpc.call<{
      entry?: {
        externalCredentialActive?: boolean
        credentialEnv?: string
      }
    }>(method, { providerId: provider })
    if (response?.entry?.externalCredentialActive) {
      pushToast(t('setup.toast.providerCredentialExternalStillActive', {
        provider: providerLabel,
        envKey: response.entry.credentialEnv || credential.envKey,
      }), { tone: 'warn' })
    } else {
      pushToast(t('setup.toast.providerCredentialRemoved', {
        provider: providerLabel,
      }))
    }
    // Refresh saved credential status without reinitializing any form. Removing
    // one secret must not silently discard drafts in Provider, Routing, or any
    // other settings section.
    await loadData({ preserveFormDrafts: true, resetProviderConnection: true })
  } catch (err) {
    pushToast(saveFailedMessage(err), { tone: 'danger' })
  } finally {
    providerCredentialRemovalPending.value = false
  }
}

// Optional accelerator: live-probe the CURRENT (possibly unsaved) provider
// form values. Never gates saving. The probe RPC requires a model id, so an
// empty model field falls back to the catalog's default for the provider.
async function probeProviderConnection() {
  if (providerInteractionLocked()) return
  if (!providerCredentialPanel.value?.probeReady) return
  const storedProfileDraft = selectedStoredProfile.value && providerForm.isDirty.value
  const persistedStoredProfile = selectedStoredProfile.value && !storedProfileDraft
  await providerForm.probeConnection({
    defaultModel: selectedStoredProfile.value
      ? providerProbeModel.value
      : currentFormModelValue() || providerSpec.value?.defaultModel || '',
    modelOverride: editingPrimaryProvider.value && hasConfiguredPrimaryProvider.value
      ? currentFormModelValue()
      : undefined,
    storedProfile: persistedStoredProfile,
    draftProfile: storedProfileDraft,
  })
  // Verification is deliberately non-mutating. The editor keeps the verified
  // draft visible so the user can review the discovered model and then commit
  // it with the explicit Save changes action.
}

async function refreshProviderModels() {
  if (providerInteractionLocked()) return
  const storedProfileDraft = selectedStoredProfile.value && providerForm.isDirty.value
  await providerForm.discoverModels({
    storedProfile: selectedStoredProfile.value && !storedProfileDraft,
    draftProfile: storedProfileDraft,
    forceRefresh: true,
  })
}

function imageGenerationUsesProfileCredential(providerId: string): boolean {
  const provider = normalizeProviderId(providerId)
  const effective = status.value.imageGenerationState?.effective
  return Boolean(
    provider
    && effective?.enabled === true
    && normalizeProviderId(effective.providerId) === provider
    && effective.credentialSource === 'llm_fallback'
    && effective.credentialOwner === 'profile',
  )
}

function imageGenerationEnvironmentKey(providerId: string): string {
  const provider = normalizeProviderId(providerId)
  const option = status.value.imageGenerationState?.credentialOptions?.find(
    candidate => normalizeProviderId(candidate.providerId) === provider,
  )
  return String(option?.envKey || status.value.imageGenerationEnvKey || '').trim()
}

async function removeProviderProfile(providerId: string) {
  if (providerInteractionLocked()) return
  const provider = normalizeProviderId(providerId)
  if (!provider) return
  const row = configuredProviders.value.find(item => normalizeProviderId(item.providerId) === provider)
  if (!row) return
  // Defend against stale callers as well as the rendered-list filter. A
  // Router/Ensemble deployment status is not proof that an llm_profile exists,
  // so never show a confirmation or issue a destructive RPC for it.
  if (!row.active && !storedProfileIds.value.has(provider)) return
  if (row.active && !primaryProviderRemovalSupported.value) return
  const replacement = row.active
    ? configuredProviders.value.find(item => (
        normalizeProviderId(item.providerId) !== provider
        && item.ready
        && item.primaryEligible
      ))
    : undefined
  if (row.active && !replacement) {
    pushToast(t('setup.toast.providerActiveRemoveNeedsReplacement'), { tone: 'danger' })
    return
  }
  const imageUsedProfileCredential = imageGenerationUsesProfileCredential(provider)
  if (!(await confirmProviderDraftDiscard())) return
  const baseConfirmationBody = row.active
    ? t('setup.provider.removeActiveConfirmBody', {
        provider: providerCatalogLabel(provider),
        replacement: replacement?.label || '',
      })
    : t('setup.provider.removeConfirmBody', { provider: providerCatalogLabel(provider) })
  const ok = await confirm({
    title: t('setup.provider.removeConfirmTitle'),
    body: imageUsedProfileCredential
      ? `${baseConfirmationBody} ${t('setup.provider.removeConfirmImageCredentialImpact')}`
      : baseConfirmationBody,
    primaryLabel: t('setup.provider.removeConfirmPrimary'),
  })
  if (!ok) return
  try {
    if (row.active && replacement) {
      await rpc.call('onboarding.llmProfile.active.remove', {
        providerId: provider,
        replacementProviderId: replacement.providerId,
        ...imageGenerationIntentPayload(replacement.providerId, {
          respectProviderEditorChoice: false,
        }),
      })
    } else {
      await rpc.call('onboarding.llmProfile.remove', { providerId: provider })
    }
    await loadData()
    const providerLabel = providerCatalogLabel(provider)
    const effectiveImage = status.value.imageGenerationState?.effective
    const imageRouteRetained = normalizeProviderId(effectiveImage?.providerId) === provider
    if (
      imageUsedProfileCredential
      && imageRouteRetained
      && effectiveImage?.available === true
      && effectiveImage.credentialSource === 'env'
    ) {
      pushToast(t('setup.toast.providerProfileRemovedImageEnv', {
        provider: providerLabel,
        envKey: imageGenerationEnvironmentKey(provider),
      }), { tone: 'warn' })
    } else if (
      imageUsedProfileCredential
      && imageRouteRetained
      && effectiveImage?.available !== true
    ) {
      pushToast(t('setup.toast.providerProfileRemovedImageNeedsCredential', {
        provider: providerLabel,
      }), { tone: 'warn' })
    } else {
      pushToast(t('setup.toast.providerProfileRemoved', { provider: providerLabel }))
    }
  } catch (err) {
    pushToast(saveFailedMessage(err), { tone: 'danger' })
    // A transport failure can arrive after the gateway committed the atomic
    // mutation but before its response reached this tab. Reconcile the saved
    // provider list without discarding unrelated settings drafts.
    if (row.active) {
      await loadData({ preserveFormDrafts: true })
    }
  }
}

function updateLlmTimeout(value: number) {
  if (providerInteractionLocked()) return
  promotedForm.setLlmTimeoutSeconds(value)
}

function updateContextWindow(value: string) {
  if (providerInteractionLocked()) return
  promotedForm.setContextWindowTokens(value)
}

function envRecoveryCommand(section: string): string {
  const commands = Array.isArray(status.value.envRecoveryCommands) ? status.value.envRecoveryCommands : []
  const entry = commands.find(e => e && e.section === section && e.command)
  return entry ? (entry.command ?? '') : ''
}

function setRouterMode(value: string) {
  routerForm.setRouterMode(value)
}

function setRouterDefaultTier(value: string) {
  routerForm.setRouterDefaultTier(value)
}

function setRouterVisualMode(value: string) {
  routerForm.setRouterVisualMode(value)
}

function updateTierField(
  name: string,
  key: 'provider' | 'model' | 'thinkingLevel' | 'supportsImage' | 'ensembleEnabled' | 'ensembleSelectionMode',
  value: string | boolean,
) {
  routerForm.updateTierField(name, key, value)
  if (key === 'provider') {
    void discoverTierProviderModels(String(value || ''))
  }
}

// ---------------------------------------------------------------------------
// Ensemble helpers
// ---------------------------------------------------------------------------

function setEnsembleEnabled(value: boolean) {
  ensembleForm.setEnabled(value)
  routerForm.setEnsembleContext(ensembleForm.selectionMode.value, ensembleForm.enabled.value)
}

function setEnsembleSelectionMode(value: string) {
  ensembleForm.setSelectionMode(value)
  routerForm.setEnsembleContext(ensembleForm.selectionMode.value, ensembleForm.enabled.value)
}

function addEnsembleModelOption(value: string) {
  ensembleForm.addModelOption(value)
}

function removeEnsembleModelOption(value: string) {
  ensembleForm.removeModelOption(value)
}

function addEnsembleCandidate(provider: string, model: string, role: EnsembleCandidateRole = '') {
  ensembleForm.addCandidate(provider, model, role)
}

function removeEnsembleCandidate(candidate: EnsembleCandidateView) {
  ensembleForm.removeCandidate(candidate)
}

function replaceEnsembleCandidate(candidate: EnsembleCandidateView, provider: string, model: string) {
  ensembleForm.replaceCandidate(candidate, provider, model)
}

function setEnsembleAggregator(provider: string, model: string) {
  ensembleForm.setAggregator(provider, model)
}

function setEnsembleCandidateRole(candidate: EnsembleCandidateView, role: EnsembleCandidateRole) {
  ensembleForm.setCandidateRole(candidate, role)
}

function importEnsembleTierCandidates() {
  ensembleForm.importTierCandidates(modelStrategyTierCandidates.value)
}

function discoverModelStrategyProviderModels(provider: string) {
  void discoverTierProviderModels(provider)
}

function migrateEnsembleLegacy() {
  ensembleForm.migrateLegacyToCustom(ensembleTierCandidates.value, currentProvider.value)
  routerForm.setEnsembleContext(ensembleForm.selectionMode.value, ensembleForm.enabled.value)
}

function resetEnsembleCandidates() {
  ensembleForm.resetModelOptions()
}

function setEnsembleScheme(scheme: 'preset' | 'custom') {
  ensembleForm.setScheme(scheme, staticB5ModeForProvider(currentProvider.value))
  routerForm.setEnsembleContext(ensembleForm.selectionMode.value, ensembleForm.enabled.value)
}

function setEnsembleMinSuccessful(value: number) {
  ensembleForm.setMinSuccessfulProposers(value)
}

function setEnsembleAllFailedPolicy(value: string) {
  ensembleForm.setAllFailedPolicy(value)
}

function setEnsembleProposerMaxRetries(value: number) {
  ensembleForm.setProposerMaxRetries(value)
}

// ---------------------------------------------------------------------------
// Search / Memory / Image / Audio helpers
// ---------------------------------------------------------------------------

function onSearchProviderChange() {
  capabilitiesForm.onSearchProviderChange(searchSpec.value)
}

function onMemoryProviderChange() {
  capabilitiesForm.onMemoryProviderChange(memorySpec.value, memoryApiKeyEnabled.value)
}

function onImageProviderChange(providerId: string) {
  capabilitiesForm.onImageProviderChange(
    imageProviders.value.find(provider => provider.providerId === providerId),
  )
  void discoverImageGenerationModels(providerId)
}

function useImageRecommendation(providerId: string) {
  const recommendation = imageRecommendation.value
  if (!recommendation || normalizeProviderId(providerId) !== recommendation.providerId) return
  onImageProviderChange(recommendation.providerId)
}

function updateCapabilityField(
  group: 'search' | 'memory' | 'image' | 'audio',
  key: string,
  value: string | number | boolean,
) {
  if (group === 'audio') {
    promotedForm.updateAudioField(key, value as string | boolean)
    return
  }
  if (group === 'memory' && key === 'autoCapture') {
    promotedForm.setMemoryAutoCapture(Boolean(value))
    return
  }
  capabilitiesForm.updateField(group, key, value)
}

function credentialNeedList(items: string[] | undefined, envKey: string | undefined): string[] {
  const key = String(envKey || '').trim()
  if (!key) return items || []
  return (items || []).map(item => {
    if (/API key via [A-Z0-9_]+ or a one-time paste\./.test(item)) {
      return `API key via ${key} or a one-time paste.`
    }
    if (/Remote embedding API key or [A-Z0-9_]+ reference\./.test(item)) {
      return `Remote embedding API key or ${key} reference.`
    }
    return item
  })
}

function memoryNeedList(spec: ProviderSpec | null, providerId: string, envKey: string | undefined): string[] {
  const items = (spec?.whatYouNeed || []).filter(Boolean)
  if (providerId === 'auto' && !String(envKey || '').trim()) {
    return items.filter(item => !/remote fallback credentials/i.test(item))
  }
  return spec?.requiresApiKey ? credentialNeedList(items, envKey || spec.envKey) : items
}

// ---------------------------------------------------------------------------
// Status text helpers
// ---------------------------------------------------------------------------

function searchStatusText(): string {
  if (status.value.searchConfigured === true) {
    return t('setup.search.statusReady')
  }
  if (status.value.searchSource === 'missing_env') {
    return _missingEnvStatusText(t('setup.search.title'), status.value.searchEnvKey, t('setup.search.statusNeedsKey'))
  }
  if (savedSearchProvider.value === 'duckduckgo') return t('setup.search.statusReady')
  return t('setup.search.statusNeedsKey')
}

function _imageGenerationStatusText(): string {
  if (status.value.imageGenerationEnabled === false) {
    return t('setup.image.statusDisabled')
  }
  const detail = (status.value.sectionDetails || {}).image_generation || {}
  const actionableDetail = String(detail.detail || '').trim()
  if ((detail.blocking || detail.actionRequired || detail.status === 'unknown') && actionableDetail) {
    return localizeImageActionableDetail(actionableDetail)
  }
  if (status.value.imageGenerationConfigured === true) {
    if (status.value.imageGenerationSource === 'llm_fallback') {
      return t('setup.image.statusReadyFallback')
    }
    return t('setup.image.statusReady')
  }
  if (status.value.imageGenerationSource === 'missing_env') {
    return _missingEnvStatusText(t('setup.image.title'), status.value.imageGenerationEnvKey, t('setup.image.statusNeedsKey'))
  }
  return t('setup.image.statusNeedsKey')
}

function _memoryEmbeddingStatusText(providerId = ''): string {
  const current = config.value.memory?.embedding || {}
  const savedProvider = current.provider || current.mode || status.value.memoryEmbeddingProvider || 'auto'
  const provider = providerId || savedProvider
  if (provider === 'none') {
    return t('setup.memory.statusNone')
  }
  if (provider === 'local') {
    return t('setup.memory.statusLocal')
  }
  if (provider === 'ollama') {
    return t('setup.memory.statusOllama')
  }
  if (provider === 'auto') {
    return t('setup.memory.statusAuto')
  }
  if (provider === savedProvider && status.value.memoryEmbeddingConfigured === true) {
    return t('setup.memory.statusConfigured')
  }
  if (provider === savedProvider && status.value.memoryEmbeddingSource === 'missing_env') {
    return _missingEnvStatusText(t('setup.memory.remoteEmbeddings'), status.value.memoryEmbeddingEnvKey, t('setup.memory.statusNeedsKey'))
  }
  return t('setup.memory.statusNeedsKey')
}

function _missingEnvStatusText(capability: string, envKey: string | undefined, fallback: string): string {
  const key = String(envKey || '').trim()
  if (!key) return fallback
  return t('setup.status.envNotVisible', { capability, envKey: key })
}

// ---------------------------------------------------------------------------
// Readiness helpers
// ---------------------------------------------------------------------------

function capabilityBadgeTone(name: CapabilityId): string {
  if (name === 'search') {
    if (status.value.searchConfigured === true || savedSearchProvider.value === 'duckduckgo') {
      return 'is-ok'
    }
    return savedSearchProvider.value === 'duckduckgo' ? 'is-muted' : 'is-warn'
  }
  if (name === 'memory_embedding') {
    if (status.value.memoryEmbeddingSource === 'missing_env') return 'is-warn'
    if (['auto', 'local', 'none'].includes(savedMemoryProvider.value)) return 'is-ok'
    return status.value.memoryEmbeddingConfigured === true ? 'is-ok' : 'is-warn'
  }
  if (name === 'image_generation') {
    if (status.value.imageGenerationEnabled !== true) return 'is-muted'
    return status.value.imageGenerationConfigured === true ? 'is-ok' : 'is-warn'
  }
  if (name === 'audio') return audioBadgeTone.value
  return 'is-muted'
}

function capabilityBadgeLabel(name: CapabilityId): string {
  if (name === 'search') {
    if (searchCurrentAvailable.value) {
      return t(capabilitiesForm.searchDirty.value
        ? 'setup.capabilities.statusCurrentAvailable'
        : 'setup.capabilities.statusAvailable')
    }
    return t(capabilitiesForm.searchDirty.value
      ? 'setup.capabilities.statusCurrentNeedsAction'
      : 'setup.capabilities.statusNeedsAction')
  }
  if (name === 'memory_embedding') {
    if (status.value.memoryEmbeddingSource === 'missing_env') {
      return t('setup.capabilities.statusNeedsAction')
    }
    if (savedMemoryProvider.value === 'none') return t('setup.capabilities.statusAvailable')
    if (['auto', 'local'].includes(savedMemoryProvider.value)) {
      return t('setup.capabilities.statusBuiltIn')
    }
    return status.value.memoryEmbeddingConfigured === true
      ? t('setup.capabilities.statusAvailable')
      : t('setup.capabilities.statusNeedsAction')
  }
  if (name === 'image_generation') {
    if (status.value.imageGenerationEnabled !== true) return t('setup.capabilities.statusPending')
    return status.value.imageGenerationConfigured === true
      ? t('setup.capabilities.statusAvailable')
      : t('setup.capabilities.statusNeedsAction')
  }
  if (name === 'audio') return audioBadgeLabel.value
  return t('setup.capabilities.statusPending')
}

function capabilityResettable(name: CapabilityId): boolean {
  return status.value.capabilityConfiguration?.[name]?.resettable === true
}

function capabilityHasDraft(name: CapabilityId): boolean {
  if (name === 'search') return capabilitiesForm.searchDirty.value
  if (name === 'memory_embedding') return capabilitiesForm.memoryDirty.value
  if (name === 'image_generation') return capabilitiesForm.imageDirty.value
  return promotedForm.audioDirty.value
}

function capabilityTitle(name: CapabilityId): string {
  if (name === 'search') return t('setup.search.title')
  if (name === 'memory_embedding') return t('setup.memory.title')
  if (name === 'image_generation') return t('setup.image.title')
  return t('setup.audio.title')
}

async function resetCapability(name: CapabilityId) {
  if (capabilityResetPending.value || !capabilityResettable(name)) return
  const title = capabilityTitle(name)
  const ok = await confirm({
    title: t('setup.capabilities.resetConfirmTitle', { capability: title }),
    body: t(
      capabilityHasDraft(name)
        ? 'setup.capabilities.resetConfirmBodyWithDraft'
        : 'setup.capabilities.resetConfirmBody',
      { capability: title },
    ),
    primaryLabel: name === 'search' || name === 'memory_embedding'
      ? t('setup.capabilities.restorePrimary')
      : t('setup.capabilities.removePrimary'),
  })
  if (!ok) return

  capabilityResetPending.value = name
  try {
    const response = await rpc.call<{ restartRequired?: boolean }>('onboarding.capability.reset', {
      capabilityId: name,
    })
    // Refresh server-owned status while preserving unrelated drafts, then
    // reseed only the capability that the user explicitly reset.
    await loadData({ preserveFormDrafts: true, throwOnError: true })
    if (name === 'search') {
      capabilitiesForm.initSearchFromConfig(config.value, searchProviders.value)
    } else if (name === 'memory_embedding') {
      capabilitiesForm.initMemoryFromConfig(config.value)
    } else if (name === 'image_generation') {
      capabilitiesForm.initImageFromConfig(config.value, status.value, imageProviders.value)
    } else {
      promotedForm.initAudioFromConfig(config.value)
    }
    pushToast(response?.restartRequired
      ? t('setup.toast.capabilityResetRestart', { capability: title })
      : t('setup.toast.capabilityReset', { capability: title }))
  } catch (err) {
    pushToast(saveFailedMessage(err), { tone: 'danger' })
  } finally {
    capabilityResetPending.value = ''
  }
}

// ---------------------------------------------------------------------------
// Save actions
// ---------------------------------------------------------------------------

function sameEndpointOrigin(candidateValue: unknown, storedValue: unknown): boolean {
  const candidate = String(candidateValue || '').trim()
  if (!candidate) return true
  const stored = String(storedValue || '').trim()
  if (!stored) return false
  if (candidate === stored) return true
  try {
    const candidateOrigin = new URL(candidate).origin
    const storedOrigin = new URL(stored).origin
    return candidateOrigin !== 'null' && candidateOrigin === storedOrigin
  } catch {
    return false
  }
}

function sameEndpointApiBase(candidateValue: unknown, officialValue: unknown): boolean {
  const official = String(officialValue || '').trim()
  const candidate = String(candidateValue || '').trim() || official
  if (!official || !candidate) return false
  try {
    const candidateUrl = new URL(candidate)
    const officialUrl = new URL(official)
    const normalizedPath = (value: URL) => value.pathname.replace(/\/+$/, '') || '/'
    return candidateUrl.username === ''
      && candidateUrl.password === ''
      && officialUrl.username === ''
      && officialUrl.password === ''
      && candidateUrl.search === ''
      && candidateUrl.hash === ''
      && officialUrl.search === ''
      && officialUrl.hash === ''
      && candidateUrl.origin !== 'null'
      && candidateUrl.origin === officialUrl.origin
      && normalizedPath(candidateUrl) === normalizedPath(officialUrl)
  } catch {
    return false
  }
}

function imageGenerationIntentPayload(
  providerId: string,
  options: { respectProviderEditorChoice?: boolean } = {},
): Record<string, 'preserve' | 'enable_provider_default'> {
  if (!imageGenerationIntentSupported.value) return {}
  const normalizedProvider = normalizeProviderId(providerId)
  const canEnableDefault = (
    imageGenerationMode.value === 'unconfigured'
    && normalizedProvider === 'openrouter'
  )
  const editorChoiceApplies = (
    options.respectProviderEditorChoice !== false
    && normalizedProvider === normalizeProviderId(providerForm.selectedProvider.value)
  )
  const optedIn = editorChoiceApplies ? providerImageGenerationOptIn.value : true
  return {
    imageGenerationIntent: canEnableDefault
      && optedIn
      && (!editorChoiceApplies || providerImageGenerationOffer.value)
      ? 'enable_provider_default'
      : 'preserve',
  }
}

function providerConfigurePayload(includeProviderModelDraft = false): Record<string, unknown> {
  const payload = providerForm.payload()
  if (editingPrimaryProvider.value && hasConfiguredPrimaryProvider.value) {
    // Model Routing owns the fixed-model draft. Provider saves must preserve
    // the persisted model, not commit a routing edit ahead of its own save.
    // The legacy configure RPC treats an omitted model as reset-to-default, so
    // explicitly carry the last saved value instead of dropping the field.
    payload.model = includeProviderModelDraft
      ? modelStrategyForm.fixedModel.value.trim()
      : String(config.value.llm?.model || '').trim()
  }
  const selectedProviderId = String(providerForm.selectedProvider.value || '').trim().toLowerCase()
  const savedCredential = status.value.llmCredentialStatus || {}
  const savedProviderId = String(savedCredential.provider || '').trim().toLowerCase()
  const credentialPanel = providerCredentialPanel.value
  const hasReplacement = payload.apiKey !== undefined || payload.apiKeyEnv !== undefined
  const endpointMatches = sameEndpointOrigin(payload.baseUrl, config.value.llm?.base_url)
  if (
    credentialPanel?.acceptsApiKey === true
    && credentialPanel.requiresApiKey === false
    && savedCredential.source === 'explicit'
    && selectedProviderId !== ''
    && selectedProviderId === savedProviderId
    && !hasReplacement
    && endpointMatches
  ) {
    payload.preserveApiKey = true
  }
  Object.assign(payload, imageGenerationIntentPayload(selectedProviderId))
  return payload
}

async function patchConfig(patches: Record<string, unknown>): Promise<boolean> {
  if (!Object.keys(patches).length) return false
  const res = await rpc.call<{ restartRequired?: boolean }>('config.patch', { patches })
  return res?.restartRequired === true
}

async function safePatchConfig(patches: Record<string, unknown>): Promise<boolean> {
  if (!Object.keys(patches).length) return false
  const res = await rpc.call<{ restartRequired?: boolean }>('config.patch.safe', { patches })
  return res?.restartRequired === true
}

// Deep-merge form of config.patch: `patch` is a nested object merged into the
// config tree (null deletes a key). Required for the models.<provider>.<model>
// subtree, whose model-id keys contain dots/colons that dot-path patches would
// misparse as path separators.
async function deepPatchConfig(patch: Record<string, unknown>): Promise<boolean> {
  if (!Object.keys(patch).length) return false
  const res = await rpc.call<{ restartRequired?: boolean }>('config.patch', { patch })
  return res?.restartRequired === true
}

interface SaveOptions {
  reload?: boolean
  includeProviderModelDraft?: boolean
}

async function saveProvider(options: SaveOptions = {}): Promise<boolean> {
  if (providerInteractionLocked()) return false
  if (!providerForm.selectedProvider.value) {
    pushToast(t('setup.toast.chooseProvider'), { tone: 'danger' })
    return false
  }
  const replacesPrimaryOnLegacyGateway = (
    !editingPrimaryProvider.value
    && Boolean(currentProvider.value)
    && !profileSaveSupported.value
  )
  if (
    replacesPrimaryOnLegacyGateway
    && providerForm.connection.value.phase !== 'verified'
  ) {
    pushToast(t('setup.provider.currentSettingsNotTested'), { tone: 'danger' })
    return false
  }
  const providerOwnedFixedModelDraft = providerOwnsFixedModelDraft.value
  const providerModelDraftWasSubmitted = options.includeProviderModelDraft === true
  const reloadProviderData = async (
    preserveDirtySectionDrafts = false,
    forceResetModelStrategy = false,
  ) => {
    await loadData({ preserveDirtySectionDrafts, forceResetModelStrategy })
    if (providerOwnedFixedModelDraft && !replacesPrimaryOnLegacyGateway) {
      providerOwnsFixedModelDraft.value = false
      providerFixedModelDraftSnapshot.value = null
      if (providerModelDraftWasSubmitted) {
        modelStrategyForm.initFixedModel(config.value.llm?.model || '')
      }
      // When the provider save intentionally did not submit llm.model, retain
      // that draft under Model Routing ownership instead of silently dropping
      // it or leaving the Provider editor dirty.
    }
    if (
      !replacesPrimaryOnLegacyGateway
      && normalizeProviderId(modelStrategyForm.fixedProvider.value)
        === normalizeProviderId(currentProvider.value)
    ) {
      // The Provider editor always renders the canonical fixed model while the
      // active Provider owns that identity. Rebase its per-model context from
      // the fresh config even when the retained draft originated in Model
      // Routing rather than in this Provider editor. A foreign fixed-Provider
      // draft intentionally keeps the active Provider's persisted context.
      promotedForm.reseedContextWindow(
        config.value,
        modelStrategyForm.fixedProvider.value,
        modelStrategyForm.fixedModel.value,
      )
    }
    providerFixedModelDraftSnapshot.value = null
  }
  providerSavePending.value = true
  try {
    const selectedProviderId = normalizeProviderId(providerForm.selectedProvider.value)
    if (
      !editingPrimaryProvider.value
      && currentProvider.value
      && profileSaveSupported.value
    ) {
      const payload = providerForm.payload()
      // Model is a persisted part of each profile. Preserve an explicit clear
      // so the backend can remove a custom override and fall back to the
      // provider catalog's defaultDirectModel instead of retaining the old
      // value under keep-current/partial-update semantics.
      if (Object.prototype.hasOwnProperty.call(providerForm.providerFieldValues.value, 'model')) {
        payload.model = String(providerForm.providerFieldValues.value.model ?? '').trim()
      }
      const replacesCredential = payload.apiKey !== undefined || payload.apiKeyEnv !== undefined
      // Switching an existing explicit secret to an env reference must clear
      // the old explicit value; otherwise resolver precedence would keep using
      // it and the apparently-saved env choice would never take effect.
      if (payload.apiKeyEnv !== undefined) payload.apiKey = ''
      payload.keepCurrentSecret = selectedStoredProfile.value && !replacesCredential
      await rpc.call('onboarding.llmProfile.upsert', payload)
      if (options.reload !== false) {
        // Saving a routing-only profile refreshes its persisted status without
        // discarding drafts in any other Settings section. Provider-owned
        // fields still reinitialize from config.
        await reloadProviderData(true)
        // This is the trusted completion of the in-flight save. The public
        // selection handler intentionally rejects clicks while that save lock
        // is held, so restore the saved profile through the internal helper.
        applyConfiguredProviderSelection(selectedProviderId)
      }
      pushToast(t('setup.toast.providerProfileSaved', {
        provider: providerCatalogLabel(selectedProviderId),
      }))
      return true
    }
    // Older gateways expose only one active provider and do not implement
    // llmProfile.upsert. On those versions, adding another provider is a
    // replace-primary flow: the legacy configure RPC atomically swaps the
    // provider, credential, endpoint, and default model after verification.
    const payload = providerConfigurePayload(options.includeProviderModelDraft === true)
    await rpc.call('onboarding.provider.configure', payload)
    const restart = await patchConfig(promotedForm.providerPatches())
    // The per-model context-window override rides the deep-merge patch form. Key
    // it on the CURRENT canonical model draft rather than payload.model (which
    // deliberately preserves the saved primary model until Model Routing is
    // saved), and skip the patch entirely when no model is selected.
    const contextModel = currentFormModelValue()
    if (contextModel) {
      const contextPatch = promotedForm.contextWindowPatch(providerForm.selectedProvider.value, contextModel)
      if (contextPatch) await deepPatchConfig(contextPatch)
    }
    if (options.reload !== false) {
      // Replacing the primary deployment on a legacy Gateway changes the
      // identity that Router and the fixed fallback are based on. Rebuild that
      // whole section, but retain dirty drafts in unrelated Settings sections.
      await reloadProviderData(true, replacesPrimaryOnLegacyGateway)
    }
    if (options.reload !== false && providerEnvMissing.value) {
      pushToast(t('setup.toast.envNotVisibleGateway', { envKey: providerEnvKey.value }), { tone: 'danger' })
      return true
    }
    pushToast(restart ? t('setup.toast.providerSavedRestart') : t('setup.toast.providerSaved'))
    return true
  } catch (err) {
    pushToast(providerRpcErrorMessage(err), { tone: 'danger' })
    return false
  } finally {
    providerSavePending.value = false
  }
}

async function saveBehavior(options: SaveOptions = {}): Promise<boolean> {
  try {
    const restart = await safePatchConfig(behaviorForm.patches())
    pushToast(restart ? t('setup.toast.behaviorSavedRestart') : t('setup.toast.behaviorSaved'))
    if (options.reload !== false) await loadData()
    return true
  } catch (err) {
    pushToast(saveFailedMessage(err), { tone: 'danger' })
    return false
  }
}

async function savePrivacy(
  value = disableNetworkObservability.value,
  options: { reload?: boolean } = {},
): Promise<boolean> {
  try {
    const restart = await safePatchConfig({
      'privacy.disable_network_observability': value,
    })
    if (options.reload === false) {
      config.value = {
        ...config.value,
        privacy: {
          ...(config.value.privacy || {}),
          disable_network_observability: value,
          network_observability_disabled_effective: value || networkObservabilityDisabledByEnvironment.value,
        },
      }
      disableNetworkObservability.value = value
    } else {
      await loadData()
    }
    pushToast(restart ? t('setup.toast.privacySavedRestart') : t('setup.toast.privacySaved'))
    return true
  } catch (err) {
    pushToast(saveFailedMessage(err), { tone: 'danger' })
    return false
  }
}

async function saveMemoryAutoCapture(options: SaveOptions = {}): Promise<boolean> {
  try {
    const restart = await safePatchConfig(promotedForm.memoryPatches())
    if (options.reload === false) {
      config.value = {
        ...config.value,
        memory: {
          ...(config.value.memory || {}),
          auto_capture_enabled: promotedForm.memoryAutoCapture.value,
        },
      }
      promotedForm.initMemoryCaptureFromConfig(config.value)
    } else {
      await loadData()
    }
    pushToast(restart ? t('setup.toast.memorySavedRestart') : t('setup.toast.memorySaved'))
    return true
  } catch (err) {
    pushToast(saveFailedMessage(err), { tone: 'danger' })
    return false
  }
}

async function saveRouter() {
  if (!hasSavedProvider.value && routerForm.routingDirty.value) {
    pushToast(t('setup.toast.chooseProviderRouter'), { tone: 'danger' })
    return
  }
  try {
    if (routerForm.routingDirty.value) {
      await rpc.call('onboarding.router.configure', routerForm.payload())
    }
    const restart = await safePatchConfig(routerForm.visualModePatches())
    pushToast(restart ? t('setup.toast.routerSavedRestart') : t('setup.toast.routerSaved'))
    await loadData()
  } catch (err) {
    pushToast(saveFailedMessage(err), { tone: 'danger' })
  }
}

async function saveEnsemble() {
  try {
    // Partial payload: only the keys the user actually changed; the gateway
    // keeps current values for everything omitted. No restart — the turn
    // loop reads [llm_ensemble] live.
    const params = ensembleForm.payload()
    if (Object.keys(params).length) {
      await rpc.call('onboarding.ensemble.configure', params)
    }
    pushToast(t('setup.toast.ensembleSaved'))
    await loadData()
  } catch (err) {
    pushToast(saveFailedMessage(err), { tone: 'danger' })
  }
}

async function saveModelStrategy(options: SaveOptions & {
  allowUnsavedProvider?: boolean
} = {}): Promise<boolean> {
  const routerRoutingPayload = routerForm.routingDirty.value ? routerForm.payload() : null
  const routerVisualPatches = routerForm.visualModePatches()
  const fixedModelPatches = modelStrategyForm.fixedModelPatches()
  const fixedProviderChanged = modelStrategyForm.fixedProviderDirty.value
  const ensemblePayload = ensembleForm.payload()
  const hasRouterWork = Boolean(routerRoutingPayload) || Object.keys(routerVisualPatches).length > 0
  const hasFixedModelWork = fixedProviderChanged || Object.keys(fixedModelPatches).length > 0
  const hasEnsembleWork = Object.keys(ensemblePayload).length > 0
  if (!hasRouterWork && !hasFixedModelWork && !hasEnsembleWork) return true
  if (hasFixedModelWork && !modelStrategyForm.fixedModel.value.trim()) {
    pushToast(t('setup.toast.chooseFixedModel'), { tone: 'danger' })
    return false
  }
  if (!hasSavedProvider.value && routerRoutingPayload && !options.allowUnsavedProvider) {
    pushToast(t('setup.toast.chooseProviderRouter'), { tone: 'danger' })
    return false
  }

  let savedAny = false
  try {
    if (hasRouterWork) {
      if (routerRoutingPayload) {
        await rpc.call('onboarding.router.configure', routerRoutingPayload)
      }
      const restart = await safePatchConfig(routerVisualPatches)
      pushToast(restart ? t('setup.toast.routerSavedRestart') : t('setup.toast.routerSaved'))
      savedAny = true
    }

    if (hasEnsembleWork) {
      await rpc.call('onboarding.ensemble.configure', ensemblePayload)
      pushToast(t('setup.toast.ensembleSaved'))
      savedAny = true
    }

    if (hasFixedModelWork) {
      let restart = false
      if (fixedProviderChanged) {
        const providerId = normalizeProviderId(modelStrategyForm.fixedProvider.value)
        if (!providerId || !configuredProviderIds.value.has(providerId)) {
          pushToast(t('setup.toast.chooseProvider'), { tone: 'danger' })
          return false
        }
        const response = await rpc.call<{ restartRequired?: boolean }>(
          'onboarding.llmProfile.activate',
          {
            providerId,
            model: modelStrategyForm.fixedModel.value.trim(),
            ...imageGenerationIntentPayload(providerId, {
              respectProviderEditorChoice: false,
            }),
          },
        )
        restart = response?.restartRequired === true
        if (!hasRouterWork) {
          pushToast(t('setup.toast.providerActivated', {
            provider: providerCatalogLabel(providerId),
          }))
        }
      } else {
        restart = await patchConfig(fixedModelPatches)
      }
      if (!hasRouterWork && !fixedProviderChanged) {
        pushToast(restart ? t('setup.toast.routerSavedRestart') : t('setup.toast.routerSaved'))
      }
      savedAny = true
    }

    if (savedAny && options.reload !== false) await loadData()
    return savedAny
  } catch (err) {
    pushToast(saveFailedMessage(err), { tone: 'danger' })
    return false
  }
}

// Explicit-user-action preset application (the ONLY path that sends presetId):
// saves the current provider form values plus the preset, then reloads so the
// Router section and routerMode reflect the applied tiers. Confirm-free by
// design — the Router section can always change the result afterwards.
async function applyProviderPreset() {
  if (providerInteractionLocked()) return
  if (!providerForm.selectedProvider.value) {
    pushToast(t('setup.toast.chooseProvider'), { tone: 'danger' })
    return
  }
  const presetId = selectedPreset.value?.presetId || providerForm.selectedProvider.value
  try {
    await rpc.call('onboarding.provider.configure', { ...providerConfigurePayload(), presetId })
    const restart = await patchConfig(promotedForm.providerPatches())
    await loadData()
    if (providerEnvMissing.value) {
      pushToast(t('setup.toast.envNotVisibleGateway', { envKey: providerEnvKey.value }), { tone: 'danger' })
      return
    }
    pushToast(restart ? t('setup.toast.presetAppliedRestart') : t('setup.toast.presetApplied'))
  } catch (err) {
    pushToast(saveFailedMessage(err), { tone: 'danger' })
  }
}

async function saveSearch(options: SaveOptions = {}): Promise<boolean> {
  if (searchDraftMissingKey.value) {
    pushToast(t('setup.capabilities.searchKeyRequired'), { tone: 'danger' })
    return false
  }
  const params = capabilitiesForm.searchPayload()
  try {
    await rpc.call('onboarding.search.configure', params)
    pushToast(t('setup.toast.searchSaved'))
    if (options.reload !== false) await loadData()
    return true
  } catch (err) {
    pushToast(saveFailedMessage(err), { tone: 'danger' })
    return false
  }
}

async function saveMemory(options: SaveOptions = {}): Promise<boolean> {
  const embeddingDirty = capabilitiesForm.memoryDirty.value
  try {
    let envToastShown = false
    if (embeddingDirty) {
      const params = capabilitiesForm.memoryPayload()
      const res = await rpc.call<{ entry?: { remote?: { api_key_env?: string; api_key?: string } }; restartRequired?: boolean }>('onboarding.memory_embedding.configure', params)
      const remote = res?.entry?.remote || {}
      envToastShown = _toastEnvReferenceSave(t('setup.toast.memorySurface'), remote.api_key_env, '', remote.api_key ?? '', res?.restartRequired)
    }
    if (!envToastShown) {
      pushToast(embeddingDirty ? t('setup.toast.memorySavedRestart') : t('setup.toast.memorySaved'))
    }
    if (options.reload !== false) await loadData()
    return true
  } catch (err) {
    pushToast(saveFailedMessage(err), { tone: 'danger' })
    return false
  }
}

async function saveImage(options: SaveOptions = {}): Promise<boolean> {
  const params = capabilitiesForm.imagePayload()
  try {
    const res = await rpc.call<{ entry?: { api_key_env?: string; api_key_source?: string; api_key?: string }; restartRequired?: boolean }>('onboarding.imageGeneration.configure', params)
    const entry = res?.entry || {}
    if (!_toastEnvReferenceSave(t('setup.image.title'), entry.api_key_env, entry.api_key_source, entry.api_key, res?.restartRequired)) {
      pushToast(t('setup.toast.imageSaved'))
    }
    if (options.reload !== false) await loadData()
    return true
  } catch (err) {
    pushToast(saveFailedMessage(err), { tone: 'danger' })
    return false
  }
}

async function saveAudio(options: SaveOptions = {}): Promise<boolean> {
  if (!promotedForm.audioDirty.value) {
    pushToast(t('setup.toast.noAudioChanges'))
    return true
  }
  try {
    const res = await rpc.call<{ entry?: { api_key_env?: string; api_key_source?: string; api_key?: string }; restartRequired?: boolean }>('onboarding.audio.configure', promotedForm.audioPayload())
    const entry = res?.entry || {}
    if (!_toastEnvReferenceSave(t('setup.audio.title'), entry.api_key_env, entry.api_key_source, entry.api_key, res?.restartRequired)) {
      pushToast(res?.restartRequired ? t('setup.toast.audioSavedRestart') : t('setup.toast.audioSaved'))
    }
    if (options.reload !== false) await loadData()
    return true
  } catch (err) {
    pushToast(saveFailedMessage(err), { tone: 'danger' })
    return false
  }
}

function _toastEnvReferenceSave(
  surface: string,
  envKey: string | undefined,
  keySource = '',
  hasInlineKey = '',
  restartRequired = false,
): boolean {
  const key = String(envKey || '').trim()
  if (!key || hasInlineKey) return false
  if (keySource === 'missing_env' || restartRequired) {
    pushToast(t('setup.toast.envSavedRestart', { surface, envKey: key }))
    return true
  }
  pushToast(t('setup.toast.envSavedReference', { surface, envKey: key }))
  return true
}

// ---------------------------------------------------------------------------
// Utility helpers
// ---------------------------------------------------------------------------

function tierLabel(tier: string): string {
  return t(routerTierLabelKey(tier))
}

function shellArg(value: string): string {
  const text = String(value || '')
  if (/^[A-Za-z0-9_@%+=:,./~-]+$/.test(text)) return text
  return `'${text.replace(/'/g, `'\''`)}'`
}

async function copyText(text: string, successMessage: string) {
  if (!text) return
  try {
    await copyTextWithFallback(text)
    pushToast(successMessage)
  } catch (err) {
    pushToast(t('setup.toast.copyFailed', { error: err instanceof Error ? err.message : String(err) }), { tone: 'danger' })
  }
}

async function copyCommand(command: string) {
  await copyText(command, t('setup.toast.copiedCommand'))
}

async function copyConfigPath() {
  await copyText(configPath.value, t('setup.toast.copiedPath'))
}

  return {
    status,
    config,
    section,
    setSection,
    loaded,
    providerPanel,
    behaviorPanel,
    privacyPanel,
    memoryPanel,
    modelStrategyPanel,
    routerPanel,
    presetPanel,
    ensemblePanel,
    capabilitiesPanel,
    loadData,
    hasSavedProvider,
    providerEnvMissing,
    providerEnvKey,
    hasSetupAction,
    actionItems,
    fixCommands,
    handoffCommands,
    recipeCommands,
    configSummary,
    configPath,
    selectInitialSection,
    sectionStatus,
    sectionDirty,
    providerDraftDirty: providerDirty,
    dirtySections,
    hasUnsavedChanges,
    saveAllPending,
    providerSavePending,
    saveDirtySections,
    discardChanges,
    selectProvider,
    selectConfiguredProvider,
    requestSelectConfiguredProvider,
    requestAddProvider,
    cancelProviderEdit,
    setAutoSessionTitles,
    setDisableNetworkObservability,
    setNetworkReportingEnabled,
    setMemoryAutoCapture,
    setProviderImageGenerationOptIn,
    setModelStrategy: modelStrategyForm.setStrategy,
    setFixedProvider,
    setFixedModel,
    setRouterMode,
    setRouterDefaultTier,
    setRouterVisualMode,
    setEnsembleEnabled,
    setEnsembleSelectionMode,
    addEnsembleModelOption,
    removeEnsembleModelOption,
    addEnsembleCandidate,
    removeEnsembleCandidate,
    replaceEnsembleCandidate,
    setEnsembleAggregator,
    setEnsembleCandidateRole,
    importEnsembleTierCandidates,
    discoverModelStrategyProviderModels,
    migrateEnsembleLegacy,
    resetEnsembleCandidates,
    setEnsembleScheme,
    setEnsembleMinSuccessful,
    setEnsembleAllFailedPolicy,
    setEnsembleProposerMaxRetries,
    updateProviderField,
    updateLlmTimeout,
    updateContextWindow,
    probeProviderConnection,
    refreshProviderModels,
    probeConfiguredProvider,
    activateProvider,
    removeProviderProfile,
    revealProviderCredential,
    removeProviderCredential,
    updateTierField,
    updateCapabilityField,
    onProviderChange,
    onSearchProviderChange,
    onMemoryProviderChange,
    onImageProviderChange,
    useImageRecommendation,
    resetCapability,
    saveProvider,
    saveBehavior,
    savePrivacy,
    saveMemoryAutoCapture,
    saveRouter,
    saveEnsemble,
    saveModelStrategy,
    applyProviderPreset,
    saveSearch,
    saveMemory,
    saveImage,
    saveAudio,
    copyCommand,
    copyConfigPath,
  }
}
