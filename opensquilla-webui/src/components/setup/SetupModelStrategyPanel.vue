<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import SetupModelCombobox from '@/components/setup/SetupModelCombobox.vue'
import SetupTierTable from '@/components/setup/SetupTierTable.vue'
import { useDocumentEvent } from '@/composables/useDocumentEvent'
import type { ModelStrategy } from '@/composables/setup/useSetupModelStrategyForm'
import type {
  RouterProviderRoles,
  SetupProviderCredentialStatus,
  SetupProviderOption,
  SetupTierRow,
  TierEnsembleRuntimeStatus,
} from '@/composables/setup/useSetupRouterForm'
import type {
  DiscoveredModelCatalog,
  DiscoveredModelsByProvider,
} from '@/composables/setup/useSetupProviderForm'
import {
  type EnsembleCandidateRole,
  type EnsembleCandidateView,
  type EnsembleCredentialStatus,
  type EnsembleCustomLineupView,
  type EnsembleEffectiveFacts,
  type EnsembleFixedProfileView,
  type EnsembleScheme,
} from '@/composables/setup/useSetupEnsembleForm'

const { t } = useI18n()

interface StrategyCard {
  id: ModelStrategy
  enabled: boolean
  titleKey: string
  descKey: string
  badgeKey?: string
}

interface RouterPanelContract {
  routerDefaultTier: string
  routerVisualMode: string
  routerVisualModeOptions: readonly { value: string; label: string }[]
  routerConfigDisabled: boolean
  hasSavedProvider: boolean
  textTiers: readonly string[]
  tierRows: readonly SetupTierRow[]
  tierLabel: (tier: string) => string
  providerOptions: readonly SetupProviderOption[]
  providerCredentialStatus: readonly SetupProviderCredentialStatus[]
  discoveredModelsByProvider?: DiscoveredModelsByProvider
  hasMixedTierProviders: boolean
  routerProviderRoles?: RouterProviderRoles
  tierEnsembleStatus?: TierEnsembleRuntimeStatus | null
  tierEnsembleStatusFresh?: boolean
}

interface EnsemblePanelContract {
  enabled: boolean
  activeProvider?: string
  activeModel?: string
  selectionMode: string
  scheme: EnsembleScheme
  schemeCardsAvailable: boolean
  modelOptions: string[]
  candidates: readonly { provider: string; model: string; source?: string; enabled?: boolean; role?: string }[]
  tierCandidates: readonly EnsembleCandidateView[]
  customCandidates: readonly EnsembleCandidateView[]
  custom: EnsembleCustomLineupView
  fixedProfile: EnsembleFixedProfileView | null
  presetProviderMismatch?: boolean
  presetFacts: EnsembleEffectiveFacts
  minSuccessfulProposers: number
  allFailedPolicy: string
  showCandidateEditor: boolean
  statusText: string
}

interface SinglePanelContract {
  providerId: string
  providerLabel: string
  model: string
  models: DiscoveredModelCatalog['models']
  modelSource: DiscoveredModelCatalog['source']
}

interface ModelStrategyPanelContract {
  activeStrategy: ModelStrategy
  hasSavedProvider: boolean
  profileSaveSupported: boolean
  providerLabel: string
  routerTemplateState: string
  cards: readonly StrategyCard[]
  router: RouterPanelContract
  ensemble: EnsemblePanelContract
  single: SinglePanelContract
}

const props = defineProps<{
  panel: ModelStrategyPanelContract
  routingModeBusy?: boolean
}>()

const emit = defineEmits<{
  updateStrategy: [value: ModelStrategy]
  updateFixedProvider: [value: string]
  updateFixedModel: [value: string]
  updateRouterDefaultTier: [value: string]
  updateRouterVisualMode: [value: string]
  updateTierField: [name: string, key: 'provider' | 'model' | 'thinkingLevel' | 'supportsImage' | 'ensembleEnabled' | 'ensembleSelectionMode', value: string | boolean]
  updateEnsembleScheme: [value: 'preset' | 'custom']
  addEnsembleCandidate: [provider: string, model: string, role: EnsembleCandidateRole]
  removeEnsembleCandidate: [candidate: EnsembleCandidateView]
  replaceEnsembleCandidate: [candidate: EnsembleCandidateView, provider: string, model: string]
  setEnsembleAggregator: [provider: string, model: string]
  requestProviderModels: [provider: string]
  importEnsembleTierCandidates: []
  migrateEnsembleLegacy: []
  updateEnsembleMinSuccessful: [value: number]
  updateEnsembleAllFailedPolicy: [value: string]
  updateEnsembleProposerMaxRetries: [value: number]
  goToSection: [value: string]
}>()

const showRouterDetails = computed(() => props.panel.activeStrategy === 'router')

const fixedModelIsPrimaryStrategy = computed(() => props.panel.activeStrategy === 'single')
const routerEditingDisabled = computed(() => !props.panel.hasSavedProvider)
const newCandidateProvider = ref('')
const newCandidateModel = ref('')
const addCandidateOpen = ref(false)
const aggregatorPickerOpen = ref(false)
const replacementCandidate = ref<EnsembleCandidateView | null>(null)
const replacementProvider = ref('')
const replacementModel = ref('')
const aggregatorProvider = ref('')
const aggregatorModel = ref('')
const fixedProviderMenuOpen = ref(false)
const fixedProviderActiveIndex = ref(0)
const fixedProviderControlRef = ref<HTMLElement | null>(null)
const fixedProviderTriggerRef = ref<HTMLButtonElement | null>(null)
const fixedProviderMenuStyle = ref<Record<string, string>>({})

const FIXED_PROVIDER_MENU_MAX_HEIGHT = 272
const FIXED_PROVIDER_MENU_GAP = 4
const FIXED_PROVIDER_MENU_MARGIN = 8

function displayProvider(provider: string): string {
  const normalized = String(provider || '').trim().toLowerCase()
  if (!normalized) return props.panel.providerLabel
  const option = (props.panel.router.providerOptions || []).find(row => (
    String(row.providerId || '').trim().toLowerCase() === normalized
  ))
  if (option?.label) return option.label
  if (normalized === 'openrouter') return 'OpenRouter'
  if (normalized === 'openai') return 'OpenAI'
  if (normalized === 'deepseek') return 'DeepSeek'
  if (normalized === 'anthropic') return 'Anthropic'
  if (normalized === 'groq') return 'Groq'
  if (normalized === 'tokenrhythm') return 'TokenRhythm'
  return normalized
}

const defaultRouteModel = computed(() => {
  const tier = props.panel.router.tierRows.find(row => row.name === props.panel.router.routerDefaultTier)
    || props.panel.router.tierRows[0]
  return tier?.model || ''
})

const ensembleScheme = computed(() => props.panel.ensemble.scheme)
const customLineup = computed(() => props.panel.ensemble.custom)
const activeProviderId = computed(() => String(
  props.panel.ensemble.activeProvider
  || customLineup.value.inheritedAggregatorProvider
  || '',
).trim().toLowerCase())
const currentModel = computed(() => (
  props.panel.single.model
  || props.panel.ensemble.activeModel
  || customLineup.value.inheritedAggregatorModel
  || defaultRouteModel.value
  || props.panel.providerLabel
))
const currentProvider = computed(() => displayProvider(
  activeProviderId.value,
) || props.panel.providerLabel)
const emptyCandidateCatalog: DiscoveredModelCatalog = { models: [], source: 'none' }
const candidateModelCatalog = computed(() => (
  props.panel.router.discoveredModelsByProvider?.[newCandidateProvider.value]
  || emptyCandidateCatalog
))
const replacementModelCatalog = computed(() => (
  props.panel.router.discoveredModelsByProvider?.[replacementProvider.value]
  || emptyCandidateCatalog
))
const aggregatorModelCatalog = computed(() => (
  props.panel.router.discoveredModelsByProvider?.[aggregatorProvider.value]
  || emptyCandidateCatalog
))
const configuredProviderIds = computed(() => new Set(
  (props.panel.router.providerOptions || [])
    .filter(option => option.disabled !== true)
    .map(option => String(option.providerId || '').trim().toLowerCase())
    .filter(Boolean),
))
const showAddProviderShortcut = computed(() => (
  props.panel.profileSaveSupported
  && configuredProviderIds.value.size === 1
))
const fixedProviderOptions = computed(() => providerOptionsFor(props.panel.single.providerId))
const selectedFixedProviderOption = computed(() => (
  fixedProviderOptions.value.find(option => option.providerId === props.panel.single.providerId)
  || fixedProviderOptions.value[0]
))
const fixedProviderActiveOptionId = computed(() => (
  fixedProviderMenuOpen.value
    ? `setup-model-strategy-fixed-provider-option-${fixedProviderActiveIndex.value}`
    : undefined
))

function closeFixedProviderMenu(restoreFocus = false) {
  fixedProviderMenuOpen.value = false
  if (restoreFocus) fixedProviderTriggerRef.value?.focus()
}

function updateFixedProviderMenuPosition() {
  const trigger = fixedProviderTriggerRef.value
  if (!trigger) return
  const rect = trigger.getBoundingClientRect()
  const viewportHeight = window.innerHeight
  const spaceBelow = viewportHeight - rect.bottom - FIXED_PROVIDER_MENU_GAP - FIXED_PROVIDER_MENU_MARGIN
  const spaceAbove = rect.top - FIXED_PROVIDER_MENU_GAP - FIXED_PROVIDER_MENU_MARGIN
  const openUp = spaceBelow < FIXED_PROVIDER_MENU_MAX_HEIGHT && spaceAbove > spaceBelow
  const maxHeight = Math.max(
    120,
    Math.min(FIXED_PROVIDER_MENU_MAX_HEIGHT, openUp ? spaceAbove : spaceBelow),
  )
  fixedProviderMenuStyle.value = {
    left: `${rect.left}px`,
    width: `${rect.width}px`,
    maxHeight: `${maxHeight}px`,
    top: openUp ? 'auto' : `${rect.bottom + FIXED_PROVIDER_MENU_GAP}px`,
    bottom: openUp ? `${viewportHeight - rect.top + FIXED_PROVIDER_MENU_GAP}px` : 'auto',
  }
}

function openFixedProviderMenu() {
  const selectedIndex = fixedProviderOptions.value.findIndex(option => (
    option.providerId === props.panel.single.providerId && option.disabled !== true
  ))
  const firstEnabled = fixedProviderOptions.value.findIndex(option => option.disabled !== true)
  fixedProviderActiveIndex.value = selectedIndex >= 0 ? selectedIndex : Math.max(0, firstEnabled)
  updateFixedProviderMenuPosition()
  fixedProviderMenuOpen.value = true
}

function toggleFixedProviderMenu() {
  if (fixedProviderMenuOpen.value) closeFixedProviderMenu()
  else openFixedProviderMenu()
}

function moveFixedProviderActive(delta: -1 | 1) {
  const options = fixedProviderOptions.value
  if (!options.length) return
  let index = fixedProviderActiveIndex.value
  for (let step = 0; step < options.length; step += 1) {
    index = (index + delta + options.length) % options.length
    if (options[index]?.disabled !== true) {
      fixedProviderActiveIndex.value = index
      scrollFixedProviderActiveIntoView()
      return
    }
  }
}

function scrollFixedProviderActiveIntoView() {
  void nextTick(() => {
    document.getElementById(fixedProviderActiveOptionId.value || '')
      ?.scrollIntoView?.({ block: 'nearest' })
  })
}

function chooseFixedProviderOption(option: SetupProviderOption) {
  if (option.disabled === true) return
  changeFixedProvider(option.providerId)
  closeFixedProviderMenu(true)
}

function onFixedProviderTriggerKeydown(event: KeyboardEvent) {
  if (event.key === 'Tab' && fixedProviderMenuOpen.value) {
    closeFixedProviderMenu()
    return
  }
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault()
    if (!fixedProviderMenuOpen.value) openFixedProviderMenu()
    else moveFixedProviderActive(event.key === 'ArrowDown' ? 1 : -1)
    return
  }
  if (event.key === 'Home' && fixedProviderMenuOpen.value) {
    event.preventDefault()
    const firstEnabled = fixedProviderOptions.value.findIndex(option => option.disabled !== true)
    if (firstEnabled >= 0) {
      fixedProviderActiveIndex.value = firstEnabled
      scrollFixedProviderActiveIntoView()
    }
    return
  }
  if (event.key === 'End' && fixedProviderMenuOpen.value) {
    event.preventDefault()
    let lastEnabled = -1
    for (let index = fixedProviderOptions.value.length - 1; index >= 0; index -= 1) {
      if (fixedProviderOptions.value[index]?.disabled !== true) {
        lastEnabled = index
        break
      }
    }
    if (lastEnabled >= 0) {
      fixedProviderActiveIndex.value = lastEnabled
      scrollFixedProviderActiveIntoView()
    }
    return
  }
  if (event.key === 'Enter' && fixedProviderMenuOpen.value) {
    event.preventDefault()
    const option = fixedProviderOptions.value[fixedProviderActiveIndex.value]
    if (option) chooseFixedProviderOption(option)
    return
  }
  if (event.key === 'Escape' && fixedProviderMenuOpen.value) {
    event.preventDefault()
    event.stopPropagation()
    closeFixedProviderMenu(true)
  }
}

function onFixedProviderControlFocusout(event: FocusEvent) {
  const next = event.relatedTarget
  if (next instanceof Node && fixedProviderControlRef.value?.contains(next)) return
  closeFixedProviderMenu()
}

useDocumentEvent('click', (event) => {
  if (
    fixedProviderMenuOpen.value
    && event.target instanceof Node
    && !fixedProviderControlRef.value?.contains(event.target)
  ) {
    closeFixedProviderMenu()
  }
})

function isConfiguredProvider(provider: string): boolean {
  return configuredProviderIds.value.has(String(provider || '').trim().toLowerCase())
}
const canSubmitCandidate = computed(() => (
  Boolean(newCandidateProvider.value && newCandidateModel.value.trim())
  && isConfiguredProvider(newCandidateProvider.value)
  && customLineup.value.canAddProposer
))
const replacementDuplicate = computed(() => {
  const current = replacementCandidate.value
  const nextProvider = replacementProvider.value.trim().toLowerCase()
  const nextModel = replacementModel.value.trim()
  if (!current || !nextProvider || !nextModel) return false
  if (nextProvider === current.provider && nextModel === current.model) return false
  return customLineup.value.proposers.some(candidate => (
    candidate.key !== current.key
    && candidate.provider === nextProvider
    && candidate.model === nextModel
  ))
})
const canSubmitReplacement = computed(() => {
  const current = replacementCandidate.value
  const nextProvider = replacementProvider.value.trim()
  const nextModel = replacementModel.value.trim()
  return Boolean(
    current
    && nextProvider
    && isConfiguredProvider(nextProvider)
    && nextModel
    && (nextProvider !== current.provider || nextModel !== current.model)
    && !replacementDuplicate.value,
  )
})
const currentAggregatorProvider = computed(() => (
  customLineup.value.aggregator?.provider
  || customLineup.value.inheritedAggregatorProvider
  || activeProviderId.value
))
const currentAggregatorModel = computed(() => (
  customLineup.value.aggregator?.model
  || customLineup.value.inheritedAggregatorModel
  || currentModel.value
))
const canSubmitAggregator = computed(() => {
  const nextModel = aggregatorModel.value.trim()
  return Boolean(
    aggregatorProvider.value
    && isConfiguredProvider(aggregatorProvider.value)
    && nextModel
    && (
      aggregatorProvider.value !== currentAggregatorProvider.value
      || nextModel !== currentAggregatorModel.value
    ),
  )
})
const activeFacts = computed(() => (
  ensembleScheme.value === 'preset'
    ? props.panel.ensemble.presetFacts
    : customLineup.value.facts
))
const quorumOptions = computed(() => Array.from(
  { length: Math.max(1, activeFacts.value.proposerCount) },
  (_, index) => index + 1,
))
const displayedMinSuccessful = computed(() => Math.min(
  Math.max(1, Math.trunc(Number(props.panel.ensemble.minSuccessfulProposers) || 1)),
  Math.max(1, activeFacts.value.proposerCount),
))
const proposerRetryOptions = Array.from({ length: 11 }, (_, index) => index)
const c3FusionActive = computed(() => props.panel.router.tierRows.some(row => (
  row.name === 'c3'
  && (
    row.ensembleEnabled === true
    || (row.ensembleEnabled === undefined && Boolean(row.ensembleSelectionMode))
  )
)))
const savedTierEnsembleStatus = computed(() => {
  if (!c3FusionActive.value || props.panel.router.tierEnsembleStatusFresh !== true) return null
  const status = props.panel.router.tierEnsembleStatus
  if (!status || !['ready', 'conditional', 'blocked'].includes(status.runtimeStatus)) return null
  // New Gateways project this status per tier. Require an exact C3 scope so a
  // mixed legacy profile can never show another tier's plan as C3 readiness.
  if (status.activationTiers.length !== 1 || status.activationTiers[0] !== 'c3') return null
  const scopedModes = Object.keys(status.tierSelectionModes)
  if (scopedModes.length !== 1 || scopedModes[0] !== 'c3') return null
  return status
})
const displayedProposerMaxRetries = computed(() => {
  if (!c3FusionActive.value) return activeFacts.value.proposerMaxRetries
  return savedTierEnsembleStatus.value?.effectiveProposerMaxRetries ?? 1
})
const displayedC3ProposerCount = computed(() => (
  savedTierEnsembleStatus.value?.proposerCount
  ?? Math.max(1, activeFacts.value.proposerCount)
))
const displayedC3MinSuccessful = computed(() => (
  savedTierEnsembleStatus.value?.effectiveMinSuccessfulProposers
  ?? Math.min(displayedMinSuccessful.value, displayedC3ProposerCount.value)
))
const staleTierEnsembleStatus = computed(() => (
  c3FusionActive.value
  && Boolean(props.panel.router.tierEnsembleStatus)
  && props.panel.router.tierEnsembleStatusFresh === false
))
const savedTierEnsembleBlocked = computed(() => {
  const status = savedTierEnsembleStatus.value
  if (!status) return false
  return status.runtimeStatus === 'blocked'
    || status.configurationReady === false
    || status.fixedFallbackReady === false
    || Boolean(status.blockedReason)
})
const ensemblePlanNeedsAttention = computed(() => {
  const credentialUnavailable = (candidate: EnsembleCandidateView | undefined | null) => (
    candidate?.credential?.available === false
  )
  if (!props.panel.single.providerId || !props.panel.single.model.trim()) return true
  // A local edit invalidates the server snapshot. Do not upgrade a previously
  // blocked/conditional saved plan to ready using a less complete heuristic;
  // retain an attention state until the edited configuration is saved.
  if (staleTierEnsembleStatus.value) return true
  if (savedTierEnsembleStatus.value) return savedTierEnsembleBlocked.value
  if (ensembleScheme.value === 'preset') {
    const profile = props.panel.ensemble.fixedProfile
    return !profile
      || props.panel.ensemble.presetProviderMismatch === true
      || profile.proposers.some(credentialUnavailable)
      || credentialUnavailable(profile.aggregator)
  }
  if (ensembleScheme.value === 'legacy') {
    return props.panel.ensemble.tierCandidates.length === 0
      || props.panel.ensemble.tierCandidates.some(credentialUnavailable)
  }
  return customLineup.value.belowMinimum
    || customLineup.value.proposers.some(credentialUnavailable)
    || credentialUnavailable(customLineup.value.aggregator)
    || (!customLineup.value.aggregator && !customLineup.value.inheritedAggregatorModel)
})
const ensemblePlanStatus = computed<'ready' | 'attention' | 'blocked'>(() => {
  if (props.panel.router.routerProviderRoles?.c3 === 'blocked') return 'blocked'
  if (savedTierEnsembleBlocked.value) return 'blocked'
  return ensemblePlanNeedsAttention.value ? 'attention' : 'ready'
})
const ensemblePlanBlockedReason = computed(() => (
  savedTierEnsembleBlocked.value
    ? savedTierEnsembleStatus.value?.blockedReason
      || (savedTierEnsembleStatus.value?.fixedFallbackReady === false
        ? savedTierEnsembleStatus.value.fixedFallbackBlockedReason || 'missing_fixed_fallback'
        : 'configuration_unavailable')
    : ''
))
const legacyProposers = computed(() => (
  props.panel.ensemble.customCandidates.filter(candidate => candidate.role !== 'aggregator')
))
const legacyAggregators = computed(() => (
  props.panel.ensemble.customCandidates.filter(candidate => candidate.role === 'aggregator')
))
function closeLineupEditors() {
  newCandidateProvider.value = ''
  newCandidateModel.value = ''
  replacementCandidate.value = null
  replacementProvider.value = ''
  replacementModel.value = ''
  aggregatorProvider.value = ''
  aggregatorModel.value = ''
  addCandidateOpen.value = false
  aggregatorPickerOpen.value = false
}

watch(activeProviderId, () => {
  // A model id is provider-scoped. Never carry a half-entered value across a
  // provider configuration change while the settings dialog remains mounted.
  closeLineupEditors()
})
watch(() => props.panel.single.providerId, () => closeFixedProviderMenu())
watch(fixedProviderMenuOpen, open => {
  if (open) {
    updateFixedProviderMenuPosition()
    window.addEventListener('scroll', updateFixedProviderMenuPosition, { capture: true, passive: true })
    window.addEventListener('resize', updateFixedProviderMenuPosition)
  } else {
    window.removeEventListener('scroll', updateFixedProviderMenuPosition, { capture: true })
    window.removeEventListener('resize', updateFixedProviderMenuPosition)
  }
})

onBeforeUnmount(() => {
  window.removeEventListener('scroll', updateFixedProviderMenuPosition, { capture: true })
  window.removeEventListener('resize', updateFixedProviderMenuPosition)
})
watch(ensembleScheme, closeLineupEditors)
watch(() => props.panel.activeStrategy, closeLineupEditors)

function submitCandidate() {
  const provider = newCandidateProvider.value
  const model = newCandidateModel.value.trim()
  if (!provider || !isConfiguredProvider(provider) || !model) return
  emit('addEnsembleCandidate', provider, model, 'proposer')
  closeLineupEditors()
}

function openCandidateEditor() {
  closeLineupEditors()
  newCandidateProvider.value = activeProviderId.value
  requestProviderModels(newCandidateProvider.value)
  addCandidateOpen.value = true
}

function startCandidateReplacement(candidate: EnsembleCandidateView, event?: MouseEvent) {
  const menu = (event?.currentTarget as HTMLElement | null)?.closest('details')
  menu?.removeAttribute('open')
  closeLineupEditors()
  replacementCandidate.value = candidate
  replacementProvider.value = candidate.provider
  requestProviderModels(replacementProvider.value)
}

function cancelCandidateReplacement() {
  replacementCandidate.value = null
  replacementProvider.value = ''
  replacementModel.value = ''
}

function submitCandidateReplacement() {
  const current = replacementCandidate.value
  const provider = replacementProvider.value.trim().toLowerCase()
  const model = replacementModel.value.trim()
  if (
    !current
    || !provider
    || !isConfiguredProvider(provider)
    || !model
    || replacementDuplicate.value
  ) return
  if (provider === current.provider && model === current.model) return
  emit('replaceEnsembleCandidate', current, provider, model)
  closeLineupEditors()
}

function promoteAggregator(candidate: EnsembleCandidateView, event?: MouseEvent) {
  if (!isConfiguredProvider(candidate.provider)) return
  const menu = (event?.currentTarget as HTMLElement | null)?.closest('details')
  menu?.removeAttribute('open')
  emit('setEnsembleAggregator', candidate.provider, candidate.model)
  closeLineupEditors()
}

function toggleAggregatorPicker() {
  const shouldOpen = !aggregatorPickerOpen.value
  closeLineupEditors()
  aggregatorPickerOpen.value = shouldOpen
  if (shouldOpen) {
    aggregatorProvider.value = currentAggregatorProvider.value
    requestProviderModels(aggregatorProvider.value)
  }
}

function submitAggregator() {
  const provider = aggregatorProvider.value
  const model = aggregatorModel.value.trim()
  if (
    !provider
    || !isConfiguredProvider(provider)
    || !model
    || (
      provider === currentAggregatorProvider.value
      && model === currentAggregatorModel.value
    )
  ) return
  emit('setEnsembleAggregator', provider, model)
  closeLineupEditors()
}

function providerOptionsFor(provider: string): SetupProviderOption[] {
  const current = String(provider || '').trim().toLowerCase()
  const seen = new Set<string>()
  const options: SetupProviderOption[] = []
  for (const option of props.panel.router.providerOptions || []) {
    const providerId = String(option.providerId || '').trim().toLowerCase()
    if (!providerId || seen.has(providerId)) continue
    seen.add(providerId)
    options.push({
      providerId,
      label: option.label || providerId,
      disabled: option.disabled === true,
    })
  }
  if (current && !seen.has(current)) {
    options.push({
      providerId: current,
      label: `${current} (${t('setup.summary.notConfigured')})`,
      disabled: true,
    })
  }
  return options
}

function requestProviderModels(provider: string) {
  const normalized = String(provider || '').trim().toLowerCase()
  if (normalized) emit('requestProviderModels', normalized)
}

function changeCandidateProvider(provider: string) {
  const normalized = provider.trim().toLowerCase()
  if (normalized === newCandidateProvider.value) return
  newCandidateProvider.value = normalized
  newCandidateModel.value = ''
  requestProviderModels(normalized)
}

function changeReplacementProvider(provider: string) {
  const normalized = provider.trim().toLowerCase()
  if (normalized === replacementProvider.value) return
  replacementProvider.value = normalized
  replacementModel.value = ''
  requestProviderModels(normalized)
}

function changeAggregatorProvider(provider: string) {
  const normalized = provider.trim().toLowerCase()
  if (normalized === aggregatorProvider.value) return
  aggregatorProvider.value = normalized
  aggregatorModel.value = ''
  requestProviderModels(normalized)
}

function changeFixedProvider(provider: string) {
  const normalized = provider.trim().toLowerCase()
  if (!normalized || normalized === props.panel.single.providerId) return
  emit('updateFixedProvider', normalized)
  requestProviderModels(normalized)
}

function candidateLabel(candidate: EnsembleCandidateView): string {
  return `${displayProvider(candidate.provider)} · ${candidate.model}`
}

function credentialKey(status: EnsembleCredentialStatus | undefined): string {
  if (!status) return 'setup.modelStrategy.credentialUnknown'
  if (status.available) return 'setup.modelStrategy.credentialReady'
  if (status.source === 'missing_env') return 'setup.modelStrategy.credentialMissingEnv'
  return 'setup.modelStrategy.credentialNeeded'
}

function credentialLabel(candidate: EnsembleCandidateView): string {
  return t(credentialKey(candidate.credential), {
    provider: displayProvider(candidate.provider),
    envKey: candidate.credential?.envKey || '',
  })
}
</script>

<template>
  <section class="control-section setup-model-strategy">
    <div class="control-section__head setup-model-strategy__page-head">
      <h3 class="control-section__title">{{ t('setup.modelStrategy.title') }}</h3>
      <div class="setup-model-strategy__page-meta">
        <p class="control-section__desc">{{ t('setup.modelStrategy.desc') }}</p>
        <p class="control-section__desc">{{ t('setup.modelStrategy.newSessionDefault') }}</p>
        <button
          type="button"
          class="setup-inline-link setup-model-strategy__page-action"
          @click="emit('goToSection', 'provider')"
        >
          {{ t('setup.modelStrategy.manageProviders') }}
          <Icon name="chevronRight" :size="14" aria-hidden="true" />
        </button>
      </div>
    </div>

    <div
      v-if="!panel.hasSavedProvider"
      class="setup-model-strategy__empty"
      data-testid="model-strategy-provider-first"
    >
      <span class="setup-model-strategy__empty-icon" aria-hidden="true">
        <Icon name="router" :size="20" />
      </span>
      <div>
        <strong>{{ t('setup.modelStrategy.providerFirstTitle') }}</strong>
        <p>{{ t('setup.modelStrategy.providerFirst') }}</p>
        <button type="button" class="btn btn--primary" @click="emit('goToSection', 'provider')">
          {{ t('setup.modelStrategy.providerAction') }}
          <Icon name="chevronRight" :size="15" aria-hidden="true" />
        </button>
      </div>
    </div>

    <template v-else>
      <section class="setup-model-strategy__mode" :aria-label="t('setup.modelStrategy.modeTitle')">
        <div class="setup-model-strategy__cards" role="radiogroup" :aria-label="t('setup.modelStrategy.modeTitle')">
          <label
            v-for="card in panel.cards"
            :key="card.id"
            class="setup-model-strategy__card"
            :class="{ 'is-active': card.enabled }"
            :data-strategy-id="card.id"
          >
            <input
              class="setup-model-strategy__card-input"
              type="radio"
              name="setup_model_strategy"
              :value="card.id"
              :checked="card.enabled"
              :disabled="routingModeBusy === true"
              @change="emit('updateStrategy', card.id)"
            >
            <span class="setup-model-strategy__card-heading">
              <span class="setup-model-strategy__card-title">{{ t(card.titleKey) }}</span>
              <span
                v-if="card.badgeKey"
                class="control-pill control-pill--info"
              >{{ t(card.badgeKey) }}</span>
            </span>
            <span class="setup-model-strategy__card-desc">{{ t(card.descKey) }}</span>
          </label>
        </div>
      </section>

      <p v-if="showRouterDetails && panel.router.hasMixedTierProviders" class="setup-model-strategy__notice">
        {{ t('setup.modelStrategy.crossProviderNotice') }}
      </p>

      <section v-if="showRouterDetails" class="control-section setup-model-strategy__detail">
        <div class="control-section__head">
          <h4 class="control-section__title">{{ t('setup.modelStrategy.routerTitle') }}</h4>
          <p class="control-section__desc">
            {{ t('setup.modelStrategy.routerDependency', { provider: currentProvider, model: currentModel }) }}
          </p>
        </div>

        <div class="setup-model-strategy__roles-head">
          <h5>{{ t('setup.modelStrategy.modelRolesTitle') }}</h5>
          <button
            v-if="showAddProviderShortcut"
            type="button"
            class="setup-inline-link setup-model-strategy__add-provider"
            data-testid="router-add-provider"
            aria-describedby="router-add-provider-hint"
            :title="t('setup.modelStrategy.addProviderHint')"
            @click="emit('goToSection', 'provider')"
          >
            <Icon name="plus" :size="13" aria-hidden="true" />
            {{ t('setup.provider.addProvider') }}
          </button>
          <span
            v-if="showAddProviderShortcut"
            id="router-add-provider-hint"
            class="setup-model-strategy__sr-only"
          >
            {{ t('setup.modelStrategy.addProviderHint') }}
          </span>
        </div>

        <SetupTierTable
          :rows="panel.router.tierRows"
          :tier-label="panel.router.tierLabel"
          :disabled="routerEditingDisabled"
          :provider-options="panel.router.providerOptions"
          :provider-credential-status="panel.router.providerCredentialStatus"
          :models-by-provider="panel.router.discoveredModelsByProvider || {}"
          :fixed-fallback-provider="panel.single.providerLabel"
          :fixed-fallback-model="panel.single.model"
          :ensemble-all-failed-policy="panel.ensemble.allFailedPolicy"
          :ensemble-min-successful="displayedC3MinSuccessful"
          :ensemble-proposer-count="displayedC3ProposerCount"
          :ensemble-proposer-max-retries="displayedProposerMaxRetries"
          :ensemble-plan-status="ensemblePlanStatus"
          :ensemble-plan-blocked-reason="ensemblePlanBlockedReason"
          :ensemble-fixed-fallback-ready="savedTierEnsembleStatus?.fixedFallbackReady"
          :router-provider-roles="panel.router.routerProviderRoles || {}"
          :effective-ensemble-selection-mode="panel.ensemble.selectionMode"
          @update-tier-field="(name, key, value) => emit('updateTierField', name, key, value)"
          @migrate-legacy-ensemble="emit('migrateEnsembleLegacy')"
        />

        <details
          class="setup-model-strategy__runtime setup-model-strategy__advanced"
          data-testid="router-advanced-options"
        >
          <summary>
            <Icon name="gear" :size="16" aria-hidden="true" />
            <span class="setup-model-strategy__runtime-title">
              {{ t('setup.modelStrategy.advancedTitle') }}
            </span>
            <Icon class="setup-model-strategy__runtime-chevron" name="chevronDown" :size="15" aria-hidden="true" />
          </summary>
          <div class="setup-model-strategy__runtime-body">
            <label class="control-row">
              <div class="control-row__label-block">
                <span class="control-row__label">{{ t('setup.modelStrategy.fallbackTierLabel') }}</span>
                <span class="control-row__desc">{{ t('setup.modelStrategy.fallbackTierDesc') }}</span>
              </div>
              <div class="control-row__control">
                <select
                  class="control-input"
                  :value="panel.router.routerDefaultTier"
                  name="setup_model_strategy_router_default_tier"
                  :disabled="routerEditingDisabled"
                  @change="emit('updateRouterDefaultTier', ($event.target as HTMLSelectElement).value)"
                >
                  <option v-for="tier in panel.router.textTiers" :key="tier" :value="tier">{{ panel.router.tierLabel(tier) }}</option>
                </select>
              </div>
            </label>
          </div>
        </details>
      </section>

      <section
        v-else-if="panel.activeStrategy === 'ensemble'"
        class="control-section setup-model-strategy__detail setup-model-strategy__ensemble"
        data-testid="ensemble-panel"
      >
        <div
          v-if="panel.ensemble.schemeCardsAvailable && ensembleScheme !== 'legacy'"
          class="setup-model-strategy__schemes"
          role="group"
          :aria-label="t('setup.modelStrategy.schemeLabel')"
        >
          <button
            type="button"
            class="setup-model-strategy__scheme"
            :class="{ 'is-active': ensembleScheme === 'preset' }"
            data-testid="ensemble-scheme-preset"
            :aria-pressed="ensembleScheme === 'preset' ? 'true' : 'false'"
            @click="emit('updateEnsembleScheme', 'preset')"
          >
            {{ t('setup.modelStrategy.schemePresetTitle') }}
          </button>
          <button
            type="button"
            class="setup-model-strategy__scheme"
            :class="{ 'is-active': ensembleScheme === 'custom' }"
            data-testid="ensemble-scheme-custom"
            :aria-pressed="ensembleScheme === 'custom' ? 'true' : 'false'"
            @click="emit('updateEnsembleScheme', 'custom')"
          >
            {{ t('setup.modelStrategy.schemeCustomTitle') }}
          </button>
        </div>

        <div
          v-if="ensembleScheme === 'legacy'"
          class="setup-model-strategy__notice setup-model-strategy__notice--legacy"
          data-testid="ensemble-legacy-banner"
        >
          <span>
            {{ t('setup.modelStrategy.legacyDynamicNotice') }}
            {{ panel.ensemble.allFailedPolicy === 'error'
              ? t('setup.modelStrategy.failurePolicyErrorDesc')
              : t('setup.modelStrategy.ensembleFailure', {
                provider: panel.single.providerLabel,
                model: panel.single.model,
              }) }}
          </span>
          <button
            type="button"
            class="btn"
            data-testid="ensemble-migrate-legacy"
            @click="emit('migrateEnsembleLegacy')"
          >
            {{ t('setup.modelStrategy.legacyDynamicMigrate') }}
          </button>
        </div>

        <div
          v-if="ensembleScheme === 'legacy'"
          class="setup-model-strategy__lineup"
          data-testid="ensemble-legacy-lineup"
        >
          <section class="setup-model-strategy__step">
            <header class="setup-model-strategy__step-head">
              <span class="setup-model-strategy__step-number" aria-hidden="true">1</span>
              <span class="setup-model-strategy__step-title">{{ t('setup.modelStrategy.proposerSectionLabel') }}</span>
              <span class="setup-model-strategy__count">
                {{ t('setup.modelStrategy.proposerCountCompact', {
                  count: legacyProposers.length,
                  max: customLineup.maxProposers,
                }) }}
              </span>
            </header>
            <div
              v-if="legacyProposers.length"
              class="setup-model-strategy__candidate-list setup-model-strategy__candidate-list--grouped"
              role="list"
            >
              <div
                v-for="candidate in legacyProposers"
                :key="candidate.key"
                class="setup-model-strategy__candidate"
                role="listitem"
              >
                <span class="setup-model-strategy__candidate-label">{{ candidateLabel(candidate) }}</span>
                <span
                  class="setup-model-strategy__credential"
                  :class="{ 'is-missing': candidate.credential && !candidate.credential.available }"
                >
                  {{ credentialLabel(candidate) }}
                </span>
              </div>
            </div>
            <p v-else class="setup-model-strategy__notice">{{ t('setup.modelStrategy.ensembleEmpty') }}</p>
          </section>

          <section class="setup-model-strategy__step">
            <header class="setup-model-strategy__step-head">
              <span class="setup-model-strategy__step-number" aria-hidden="true">2</span>
              <span class="setup-model-strategy__step-title">{{ t('setup.modelStrategy.aggregatorSectionLabel') }}</span>
            </header>
            <div class="setup-model-strategy__candidate-list setup-model-strategy__candidate-list--aggregator" role="list">
              <div
                v-for="candidate in legacyAggregators"
                :key="candidate.key"
                class="setup-model-strategy__candidate"
                role="listitem"
              >
                <span class="setup-model-strategy__candidate-label">{{ candidateLabel(candidate) }}</span>
                <span
                  class="setup-model-strategy__credential"
                  :class="{ 'is-missing': candidate.credential && !candidate.credential.available }"
                >
                  {{ credentialLabel(candidate) }}
                </span>
              </div>
              <div
                v-if="legacyAggregators.length === 0"
                class="setup-model-strategy__candidate setup-model-strategy__candidate--inherited"
                role="listitem"
              >
                <span class="setup-model-strategy__candidate-main">
                  <span class="setup-model-strategy__candidate-label">
                    {{ displayProvider(customLineup.inheritedAggregatorProvider) }} · {{ customLineup.inheritedAggregatorModel || currentModel }}
                  </span>
                  <span class="setup-model-strategy__candidate-source">{{ t('setup.modelStrategy.aggregatorInheritedNote') }}</span>
                </span>
              </div>
            </div>
          </section>
        </div>

        <div
          v-if="ensembleScheme === 'preset' && panel.ensemble.presetProviderMismatch && panel.ensemble.fixedProfile"
          class="setup-model-strategy__notice setup-model-strategy__notice--legacy"
          data-testid="ensemble-preset-provider-mismatch"
        >
          <span>{{ t('setup.modelStrategy.presetProviderMismatchNotice', {
            presetProvider: panel.ensemble.fixedProfile.providerLabel,
            activeProvider: panel.providerLabel,
          }) }}</span>
        </div>

        <div
          v-if="ensembleScheme === 'preset' && panel.ensemble.fixedProfile"
          class="setup-model-strategy__lineup"
          data-testid="ensemble-preset-lineup"
        >
          <section class="setup-model-strategy__step">
            <header class="setup-model-strategy__step-head">
              <span class="setup-model-strategy__step-number" aria-hidden="true">1</span>
              <span class="setup-model-strategy__step-title">{{ t('setup.modelStrategy.proposerSectionLabel') }}</span>
              <span class="setup-model-strategy__count">
                {{ t('setup.modelStrategy.proposerCountCompact', {
                  count: panel.ensemble.fixedProfile.proposers.length,
                  max: panel.ensemble.fixedProfile.proposers.length,
                }) }}
              </span>
            </header>
            <div class="setup-model-strategy__candidate-list setup-model-strategy__candidate-list--grouped" role="list">
              <div
                v-for="candidate in panel.ensemble.fixedProfile.proposers"
                :key="candidate.key"
                class="setup-model-strategy__candidate"
                role="listitem"
              >
                <span class="setup-model-strategy__candidate-label">{{ candidateLabel(candidate) }}</span>
                <span
                  class="setup-model-strategy__credential"
                  :class="{ 'is-missing': candidate.credential && !candidate.credential.available }"
                >
                  <span
                    v-if="candidate.credential?.available"
                    class="setup-model-strategy__credential-dot"
                    aria-hidden="true"
                  ></span>
                  {{ credentialLabel(candidate) }}
                </span>
              </div>
            </div>
          </section>

          <section class="setup-model-strategy__step">
            <header class="setup-model-strategy__step-head">
              <span class="setup-model-strategy__step-number" aria-hidden="true">2</span>
              <span class="setup-model-strategy__step-title">{{ t('setup.modelStrategy.aggregatorSectionLabel') }}</span>
            </header>
            <div class="setup-model-strategy__candidate-list setup-model-strategy__candidate-list--aggregator" role="list">
              <div class="setup-model-strategy__candidate" role="listitem">
                <span class="setup-model-strategy__candidate-label">
                  {{ candidateLabel(panel.ensemble.fixedProfile.aggregator) }}
                </span>
                <span
                  class="setup-model-strategy__credential"
                  :class="{ 'is-missing': panel.ensemble.fixedProfile.aggregator.credential && !panel.ensemble.fixedProfile.aggregator.credential.available }"
                >
                  <span
                    v-if="panel.ensemble.fixedProfile.aggregator.credential?.available"
                    class="setup-model-strategy__credential-dot"
                    aria-hidden="true"
                  ></span>
                  {{ credentialLabel(panel.ensemble.fixedProfile.aggregator) }}
                </span>
              </div>
            </div>
            <p class="setup-model-strategy__preset-hint">{{ t('setup.modelStrategy.presetReadOnlyHint') }}</p>
          </section>
        </div>

        <div
          v-if="ensembleScheme === 'custom'"
          class="setup-model-strategy__lineup"
          data-testid="ensemble-custom-lineup"
        >
          <section class="setup-model-strategy__step">
            <header class="setup-model-strategy__step-head">
              <span class="setup-model-strategy__step-number" aria-hidden="true">1</span>
              <span class="setup-model-strategy__step-title">{{ t('setup.modelStrategy.proposerSectionLabel') }}</span>
              <span class="setup-model-strategy__count" data-testid="ensemble-proposer-count">
                {{ t('setup.modelStrategy.proposerCountCompact', {
                  count: customLineup.proposerCount,
                  max: customLineup.maxProposers,
                }) }}
              </span>
              <button
                type="button"
                class="setup-model-strategy__import"
                data-testid="setup-model-strategy-import-tiers"
                :disabled="!panel.ensemble.tierCandidates.length || !customLineup.canAddProposer"
                @click="emit('importEnsembleTierCandidates')"
              >
                {{ t('setup.modelStrategy.importTierCandidates') }}
              </button>
            </header>

            <div
              v-if="customLineup.proposers.length"
              class="setup-model-strategy__candidate-list setup-model-strategy__candidate-list--grouped"
              role="list"
            >
              <div
                v-for="candidate in customLineup.proposers"
                :key="candidate.key"
                class="setup-model-strategy__candidate"
                role="listitem"
              >
                <span class="setup-model-strategy__candidate-label">{{ candidateLabel(candidate) }}</span>
                <span
                  class="setup-model-strategy__credential"
                  :class="{ 'is-missing': candidate.credential && !candidate.credential.available }"
                >
                  <span
                    v-if="candidate.credential?.available"
                    class="setup-model-strategy__credential-dot"
                    aria-hidden="true"
                  ></span>
                  {{ credentialLabel(candidate) }}
                </span>
                <details class="setup-model-strategy__candidate-actions">
                  <summary :aria-label="t('setup.modelStrategy.candidateActionsAria', { model: candidate.model })">
                    <Icon name="moreHorizontal" :size="17" aria-hidden="true" />
                  </summary>
                  <div class="setup-model-strategy__candidate-menu">
                    <button
                      type="button"
                      data-testid="ensemble-replace-proposer"
                      @click="startCandidateReplacement(candidate, $event)"
                    >
                      <Icon name="regenerate" :size="14" aria-hidden="true" />
                      {{ t('setup.modelStrategy.replaceModel') }}
                    </button>
                    <button
                      type="button"
                      data-testid="ensemble-promote-aggregator"
                      :disabled="!isConfiguredProvider(candidate.provider)"
                      @click="promoteAggregator(candidate, $event)"
                    >
                      <Icon name="fork" :size="14" aria-hidden="true" />
                      {{ t('setup.modelStrategy.promoteAggregator') }}
                    </button>
                    <button
                      type="button"
                      class="is-danger"
                      :aria-label="t('setup.modelStrategy.removeCandidateAria', { model: candidate.model })"
                      @click="emit('removeEnsembleCandidate', candidate)"
                    >
                      <Icon name="trash" :size="14" aria-hidden="true" />
                      {{ t('setup.channels.remove') }}
                    </button>
                  </div>
                </details>
              </div>
            </div>

            <p v-else class="setup-model-strategy__notice">
              {{ t('setup.modelStrategy.ensembleEmpty') }}
            </p>

            <div
              v-if="replacementCandidate"
              class="setup-model-strategy__editor"
              data-testid="ensemble-replace-proposer-editor"
            >
              <span class="setup-model-strategy__editor-title">
                {{ t('setup.modelStrategy.replaceCandidateTitle', { model: replacementCandidate.model }) }}
              </span>
              <select
                class="control-input setup-model-strategy__candidate-provider"
                name="setup_model_strategy_replace_candidate_provider"
                :value="replacementProvider"
                :aria-label="t('setup.modelStrategy.addCandidateProviderLabel')"
                @change="changeReplacementProvider(($event.target as HTMLSelectElement).value)"
              >
                <option
                  v-for="option in providerOptionsFor(replacementProvider)"
                  :key="option.providerId"
                  :value="option.providerId"
                  :disabled="option.disabled"
                >
                  {{ option.label }}
                </option>
              </select>
              <SetupModelCombobox
                cell
                class="setup-model-strategy__candidate-model"
                input-class="control-input"
                :field="{
                  name: 'ensemble_candidate_replacement',
                  label: t('setup.modelStrategy.addCandidateModelLabel'),
                  placeholder: t('setup.modelStrategy.addCandidateModelPlaceholder'),
                }"
                :value="replacementModel"
                :models="replacementModelCatalog.models"
                :model-source="replacementModelCatalog.source"
                @update="replacementModel = $event"
              />
              <p
                v-if="replacementDuplicate"
                class="setup-model-strategy__editor-error"
                data-testid="ensemble-replacement-duplicate"
              >
                {{ t('setup.modelStrategy.candidateDuplicate') }}
              </p>
              <div class="setup-model-strategy__editor-actions">
                <button type="button" class="btn btn--ghost" @click="cancelCandidateReplacement">
                  {{ t('common.cancel') }}
                </button>
                <button
                  type="button"
                  class="btn btn--primary"
                  data-testid="ensemble-replace-proposer-confirm"
                  :disabled="!canSubmitReplacement"
                  @click="submitCandidateReplacement"
                >
                  {{ t('setup.modelStrategy.replaceCandidateConfirm') }}
                </button>
              </div>
            </div>

            <div
              v-if="addCandidateOpen"
              class="setup-model-strategy__editor"
              data-testid="ensemble-add-proposer-editor"
            >
              <select
                class="control-input setup-model-strategy__candidate-provider"
                name="setup_model_strategy_add_candidate_provider"
                :value="newCandidateProvider"
                :aria-label="t('setup.modelStrategy.addCandidateProviderLabel')"
                @change="changeCandidateProvider(($event.target as HTMLSelectElement).value)"
              >
                <option
                  v-for="option in providerOptionsFor(newCandidateProvider)"
                  :key="option.providerId"
                  :value="option.providerId"
                  :disabled="option.disabled"
                >
                  {{ option.label }}
                </option>
              </select>
              <SetupModelCombobox
                cell
                class="setup-model-strategy__candidate-model"
                input-class="control-input"
                :field="{
                  name: 'ensemble_candidate_model',
                  label: t('setup.modelStrategy.addCandidateModelLabel'),
                  placeholder: t('setup.modelStrategy.addCandidateModelPlaceholder'),
                }"
                :value="newCandidateModel"
                :models="candidateModelCatalog.models"
                :model-source="candidateModelCatalog.source"
                @update="newCandidateModel = $event"
              />
              <div class="setup-model-strategy__editor-actions">
                <button
                  type="button"
                  class="btn btn--ghost"
                  @click="closeLineupEditors"
                >
                  {{ t('common.cancel') }}
                </button>
                <button
                  type="button"
                  class="btn btn--primary"
                  data-testid="setup-model-strategy-add-candidate"
                  :disabled="!canSubmitCandidate"
                  @click="submitCandidate"
                >
                  {{ t('setup.modelStrategy.addProposer') }}
                </button>
              </div>
            </div>
            <button
              v-else
              type="button"
              class="setup-model-strategy__add-trigger"
              data-testid="setup-model-strategy-add-candidate-trigger"
              :disabled="!customLineup.canAddProposer"
              @click="openCandidateEditor"
            >
              <Icon name="plus" :size="16" aria-hidden="true" />
              {{ t('setup.modelStrategy.addProposer') }}
            </button>

            <div class="setup-model-strategy__guidance">
              <p
                v-if="customLineup.belowMinimum && customLineup.proposers.length"
                class="setup-model-strategy__notice"
                data-testid="ensemble-below-minimum"
              >
                {{ t('setup.modelStrategy.ensembleMinimum') }}
              </p>
              <p
                v-if="customLineup.capacity === 'warn'"
                class="setup-model-strategy__notice"
                data-testid="ensemble-capacity-warn"
              >
                {{ t('setup.modelStrategy.capacityWarn', { calls: customLineup.facts.perTurnCalls }) }}
              </p>
              <p
                v-if="customLineup.capacity === 'full'"
                class="setup-model-strategy__notice"
                data-testid="ensemble-capacity-full"
              >
                {{ t('setup.modelStrategy.capacityFull', { max: customLineup.maxProposers }) }}
              </p>
              <p
                v-if="customLineup.diversityWarning"
                class="setup-model-strategy__notice"
                data-testid="ensemble-diversity-warn"
              >
                {{ t('setup.modelStrategy.diversityHint') }}
              </p>
            </div>
          </section>

          <section class="setup-model-strategy__step">
            <header class="setup-model-strategy__step-head">
              <span class="setup-model-strategy__step-number" aria-hidden="true">2</span>
              <span class="setup-model-strategy__step-title">{{ t('setup.modelStrategy.aggregatorSectionLabel') }}</span>
            </header>

            <div class="setup-model-strategy__candidate-list setup-model-strategy__candidate-list--aggregator" role="list">
              <div
                v-if="customLineup.aggregator"
                class="setup-model-strategy__candidate"
                role="listitem"
                data-testid="ensemble-custom-aggregator"
              >
                <span class="setup-model-strategy__candidate-label">{{ candidateLabel(customLineup.aggregator) }}</span>
                <span
                  class="setup-model-strategy__credential"
                  :class="{ 'is-missing': customLineup.aggregator.credential && !customLineup.aggregator.credential.available }"
                >
                  <span
                    v-if="customLineup.aggregator.credential?.available"
                    class="setup-model-strategy__credential-dot"
                    aria-hidden="true"
                  ></span>
                  {{ credentialLabel(customLineup.aggregator) }}
                </span>
                <button
                  type="button"
                  class="setup-model-strategy__replace-aggregator"
                  data-testid="ensemble-replace-aggregator"
                  @click="toggleAggregatorPicker"
                >
                  {{ t('setup.modelStrategy.replaceModel') }}
                </button>
              </div>
              <div
                v-else
                class="setup-model-strategy__candidate setup-model-strategy__candidate--inherited"
                role="listitem"
                data-testid="ensemble-custom-aggregator-inherited"
              >
                <span class="setup-model-strategy__candidate-main">
                  <span class="setup-model-strategy__candidate-label">
                    {{ displayProvider(customLineup.inheritedAggregatorProvider) }} · {{ customLineup.inheritedAggregatorModel || currentModel }}
                  </span>
                  <span class="setup-model-strategy__candidate-source">{{ t('setup.modelStrategy.aggregatorInheritedNote') }}</span>
                </span>
                <button
                  type="button"
                  class="setup-model-strategy__replace-aggregator"
                  data-testid="ensemble-replace-aggregator"
                  @click="toggleAggregatorPicker"
                >
                  {{ t('setup.modelStrategy.chooseAggregator') }}
                </button>
              </div>
            </div>

            <div
              v-if="aggregatorPickerOpen"
              class="setup-model-strategy__editor setup-model-strategy__aggregator-picker"
              data-testid="ensemble-aggregator-picker"
            >
              <span class="setup-model-strategy__editor-title">{{ t('setup.modelStrategy.aggregatorPickerTitle') }}</span>
              <div v-if="customLineup.proposers.length" class="setup-model-strategy__aggregator-options">
                <span class="setup-model-strategy__editor-label">{{ t('setup.modelStrategy.aggregatorFromProposers') }}</span>
                <button
                  v-for="candidate in customLineup.proposers"
                  :key="candidate.key"
                  type="button"
                  class="setup-model-strategy__aggregator-option"
                  data-testid="ensemble-aggregator-option"
                  :disabled="!isConfiguredProvider(candidate.provider)"
                  @click="promoteAggregator(candidate)"
                >
                  <span>{{ candidateLabel(candidate) }}</span>
                  <Icon name="chevronRight" :size="14" aria-hidden="true" />
                </button>
              </div>
              <span class="setup-model-strategy__editor-label">{{ t('setup.modelStrategy.aggregatorOtherModel') }}</span>
              <select
                class="control-input setup-model-strategy__candidate-provider"
                name="setup_model_strategy_aggregator_provider"
                :value="aggregatorProvider"
                :aria-label="t('setup.modelStrategy.addCandidateProviderLabel')"
                @change="changeAggregatorProvider(($event.target as HTMLSelectElement).value)"
              >
                <option
                  v-for="option in providerOptionsFor(aggregatorProvider)"
                  :key="option.providerId"
                  :value="option.providerId"
                  :disabled="option.disabled"
                >
                  {{ option.label }}
                </option>
              </select>
              <SetupModelCombobox
                cell
                class="setup-model-strategy__candidate-model"
                input-class="control-input"
                :field="{
                  name: 'ensemble_aggregator_model',
                  label: t('setup.modelStrategy.addCandidateModelLabel'),
                  placeholder: t('setup.modelStrategy.addCandidateModelPlaceholder'),
                }"
                :value="aggregatorModel"
                :models="aggregatorModelCatalog.models"
                :model-source="aggregatorModelCatalog.source"
                @update="aggregatorModel = $event"
              />
              <div class="setup-model-strategy__editor-actions">
                <button
                  type="button"
                  class="btn btn--ghost"
                  @click="closeLineupEditors"
                >
                  {{ t('common.cancel') }}
                </button>
                <button
                  type="button"
                  class="btn btn--primary"
                  data-testid="setup-model-strategy-set-aggregator"
                  :disabled="!canSubmitAggregator"
                  @click="submitAggregator"
                >
                  {{ t('setup.modelStrategy.chooseAggregator') }}
                </button>
              </div>
            </div>
          </section>
        </div>

        <p
          v-if="ensembleScheme !== 'legacy'"
          class="setup-model-strategy__facts"
          data-testid="ensemble-effective-summary"
        >
          <Icon name="gauge" :size="16" aria-hidden="true" />
          {{ t('setup.modelStrategy.effectiveSummary', {
            calls: activeFacts.perTurnCalls,
            proposers: activeFacts.proposerCount,
          }) }}
        </p>

        <details
          v-if="ensembleScheme !== 'legacy'"
          class="setup-model-strategy__runtime"
          data-testid="ensemble-runtime-strategy"
        >
          <summary>
            <Icon name="gear" :size="16" aria-hidden="true" />
            <span class="setup-model-strategy__runtime-title">
              {{ t('setup.modelStrategy.runtimeStrategyTitle') }}
              <small>{{ t('setup.modelStrategy.runtimeStrategyDesc') }}</small>
            </span>
            <Icon class="setup-model-strategy__runtime-chevron" name="chevronDown" :size="15" aria-hidden="true" />
          </summary>
          <div class="setup-model-strategy__runtime-body">
            <label class="control-row">
              <div class="control-row__label-block">
                <span class="control-row__label">{{ t('setup.modelStrategy.successThresholdLabel') }}</span>
                <span class="control-row__desc">{{ t('setup.modelStrategy.successThresholdDesc') }}</span>
              </div>
              <div class="control-row__control">
                <select
                  class="control-input"
                  :value="displayedMinSuccessful"
                  name="setup_model_strategy_min_successful"
                  data-testid="ensemble-success-threshold"
                  @change="emit('updateEnsembleMinSuccessful', Number(($event.target as HTMLSelectElement).value))"
                >
                  <option v-for="quorum in quorumOptions" :key="quorum" :value="quorum">
                    {{ t('setup.modelStrategy.successThresholdExact', {
                      quorum,
                      proposers: activeFacts.proposerCount,
                    }) }}
                  </option>
                </select>
              </div>
            </label>
            <label class="control-row">
              <div class="control-row__label-block">
                <span class="control-row__label">{{ t('setup.modelStrategy.failurePolicyLabel') }}</span>
                <span v-if="panel.ensemble.allFailedPolicy === 'fallback_single'" class="control-row__desc">
                  {{ t('setup.modelStrategy.ensembleFailure', {
                    provider: panel.single.providerLabel,
                    model: panel.single.model,
                  }) }}
                </span>
                <span v-else class="control-row__desc">
                  {{ t('setup.modelStrategy.failurePolicyErrorDesc') }}
                </span>
              </div>
              <div class="control-row__control">
                <select
                  class="control-input"
                  :value="panel.ensemble.allFailedPolicy"
                  name="setup_model_strategy_all_failed_policy"
                  data-testid="ensemble-failure-policy"
                  @change="emit('updateEnsembleAllFailedPolicy', ($event.target as HTMLSelectElement).value)"
                >
                  <option value="fallback_single">{{ t('setup.ensemble.allFailedFallback') }}</option>
                  <option value="error">{{ t('setup.ensemble.allFailedError') }}</option>
                </select>
              </div>
            </label>
            <label class="control-row">
              <div class="control-row__label-block">
                <span class="control-row__label">{{ t('setup.modelStrategy.proposerRetriesLabel') }}</span>
                <span class="control-row__desc">{{ t('setup.modelStrategy.proposerRetriesDesc') }}</span>
              </div>
              <div class="control-row__control">
                <select
                  class="control-input"
                  :value="activeFacts.proposerMaxRetries"
                  name="setup_model_strategy_proposer_max_retries"
                  data-testid="ensemble-proposer-retries"
                  @change="emit('updateEnsembleProposerMaxRetries', Number(($event.target as HTMLSelectElement).value))"
                >
                  <option v-for="retries in proposerRetryOptions" :key="retries" :value="retries">
                    {{ t('setup.modelStrategy.proposerRetriesOption', { count: retries }) }}
                  </option>
                </select>
              </div>
            </label>
            <div class="setup-model-strategy__runtime-limits">
              <strong>{{ t('setup.modelStrategy.runtimeLimitsLabel') }}</strong>
              <span>
                {{ t('setup.modelStrategy.runtimeLimits', {
                  proposerTimeout: activeFacts.proposerTimeoutSeconds,
                  configuredAggregatorTimeout: activeFacts.configuredAggregatorTimeoutSeconds,
                  aggregatorTimeout: activeFacts.aggregatorTimeoutSeconds,
                }) }}
              </span>
            </div>
          </div>
        </details>
      </section>

      <section
        class="control-section setup-model-strategy__detail"
        :class="{ 'setup-model-strategy__detail--secondary': !fixedModelIsPrimaryStrategy }"
        data-testid="setup-model-strategy-fixed-section"
      >
        <div v-if="fixedModelIsPrimaryStrategy" class="control-section__head">
          <h4 class="control-section__title">
            {{ t('setup.modelStrategy.singleTitle') }}
          </h4>
          <p class="control-section__desc">
            {{ t('setup.modelStrategy.singleDependency') }}
          </p>
        </div>
        <div class="setup-model-strategy__single-provider">
          <span
            id="setup-model-strategy-fixed-provider-label"
            class="setup-model-strategy__single-provider-label"
          >
            {{ t('setup.modelStrategy.singleProviderLabel') }}
          </span>
          <span
            v-if="fixedProviderOptions.length > 1"
            ref="fixedProviderControlRef"
            class="setup-model-strategy__single-provider-control"
            :class="{ 'is-open': fixedProviderMenuOpen }"
            @focusout="onFixedProviderControlFocusout"
          >
            <button
              ref="fixedProviderTriggerRef"
              type="button"
              class="control-input setup-model-strategy__single-provider-select"
              role="combobox"
              aria-haspopup="listbox"
              aria-controls="setup-model-strategy-fixed-provider-listbox"
              aria-labelledby="setup-model-strategy-fixed-provider-label"
              :aria-expanded="fixedProviderMenuOpen ? 'true' : 'false'"
              :aria-activedescendant="fixedProviderActiveOptionId"
              data-testid="setup-model-strategy-fixed-provider-trigger"
              @click="toggleFixedProviderMenu"
              @keydown="onFixedProviderTriggerKeydown"
            >
              <span class="setup-model-strategy__single-provider-value">
                {{ selectedFixedProviderOption?.label || panel.single.providerLabel }}
              </span>
            </button>
            <Icon
              class="setup-model-strategy__single-provider-chevron"
              name="chevronDown"
              :size="14"
              aria-hidden="true"
            />
            <Teleport to="body">
              <Transition name="fixed-provider-menu">
                <div
                  v-if="fixedProviderMenuOpen"
                  id="setup-model-strategy-fixed-provider-listbox"
                  class="setup-model-strategy__single-provider-menu"
                  role="listbox"
                  :aria-labelledby="'setup-model-strategy-fixed-provider-label'"
                  :style="fixedProviderMenuStyle"
                >
                  <button
                    v-for="(option, index) in fixedProviderOptions"
                    :id="`setup-model-strategy-fixed-provider-option-${index}`"
                    :key="option.providerId"
                    type="button"
                    tabindex="-1"
                    class="setup-model-strategy__single-provider-option"
                    :class="{
                      'is-active': index === fixedProviderActiveIndex,
                      'is-selected': option.providerId === panel.single.providerId,
                    }"
                    role="option"
                    :aria-selected="option.providerId === panel.single.providerId ? 'true' : 'false'"
                    :disabled="option.disabled"
                    @mousedown.prevent
                    @mouseenter="fixedProviderActiveIndex = index"
                    @click="chooseFixedProviderOption(option)"
                  >
                    <span>{{ option.label }}</span>
                    <Icon
                      v-if="option.providerId === panel.single.providerId"
                      class="setup-model-strategy__single-provider-check"
                      name="check"
                      :size="14"
                      aria-hidden="true"
                    />
                  </button>
                </div>
              </Transition>
            </Teleport>
          </span>
          <strong v-else>{{ panel.single.providerLabel }}</strong>
        </div>
        <SetupModelCombobox
          class="setup-model-strategy__fixed-model-row"
          data-testid="setup-model-strategy-fixed-model"
          :field="{
            name: 'model_strategy_fixed_model',
            label: t('setup.modelStrategy.singleModelLabel'),
            description: t('setup.modelStrategy.singleModelDesc'),
            descriptionPresentation: 'info',
            placeholder: t('setup.modelStrategy.singleModelPlaceholder'),
            required: true,
          }"
          :value="panel.single.model"
          :models="panel.single.models"
          :model-source="panel.single.modelSource"
          @update="emit('updateFixedModel', $event)"
        />
        <p v-if="fixedModelIsPrimaryStrategy" class="setup-model-strategy__muted">
          {{ t('setup.modelStrategy.singleDesc') }}
        </p>
      </section>
    </template>
  </section>
</template>

<style scoped>
.setup-model-strategy {
  gap: var(--sp-3);
}

.setup-model-strategy__page-head {
  align-items: flex-start;
  flex-direction: column;
  gap: var(--sp-1);
}

.setup-model-strategy__page-head .control-section__desc {
  flex: none;
}

.setup-model-strategy__page-meta {
  align-items: center;
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
}

.setup-inline-link.setup-model-strategy__page-action {
  font-size: var(--fs-sm);
}

.setup-model-strategy__empty {
  align-items: flex-start;
  background: var(--bg-elevated);
  border-radius: var(--radius-card);
  box-shadow: var(--elev-1);
  display: flex;
  gap: var(--sp-3);
  padding: var(--sp-4);
}

.setup-model-strategy__empty-icon {
  align-items: center;
  background: color-mix(in srgb, var(--accent) 10%, transparent);
  border-radius: var(--radius-md);
  color: var(--accent);
  display: inline-flex;
  flex: 0 0 auto;
  justify-content: center;
  padding: var(--sp-2);
}

.setup-model-strategy__empty p {
  color: var(--text-muted);
  font-size: var(--fs-sm);
  line-height: 1.5;
  margin: var(--sp-1) 0 var(--sp-3);
}

.setup-model-strategy__mode {
  display: grid;
  gap: var(--sp-2);
}

.setup-model-strategy__cards {
  display: grid;
  gap: var(--sp-2);
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
}

/* Mist cards: floating paper — hairline ring + soft shadow, selected = ink ring. */
.setup-model-strategy__card {
  background: var(--bg-elevated);
  border: 1px solid var(--hairline);
  border-radius: var(--radius-card);
  box-shadow: var(--elev-1);
  color: var(--text);
  cursor: pointer;
  display: grid;
  gap: var(--sp-1);
  min-height: 5.5rem;
  padding: var(--sp-3);
  text-align: left;
  transition: box-shadow var(--dur-base) var(--ease-out), border-color var(--dur-base) var(--ease-out), transform var(--dur-base) var(--ease-out);
}

.setup-model-strategy__card:has(.setup-model-strategy__card-input:focus-visible) {
  outline: 2px solid color-mix(in srgb, var(--accent) 72%, transparent);
  outline-offset: 2px;
}

.setup-model-strategy__card-input {
  height: 1px;
  opacity: 0;
  overflow: hidden;
  pointer-events: none;
  position: absolute;
  width: 1px;
}

.setup-model-strategy__card:hover {
  box-shadow: var(--elev-1-hover);
  transform: translateY(-1px);
}

.setup-model-strategy__card.is-active {
  background: color-mix(in srgb, var(--accent) 8%, var(--bg-elevated));
  border-color: color-mix(in srgb, var(--accent) 78%, var(--border));
  box-shadow: var(--elev-1);
}

@media (prefers-reduced-motion: reduce) {
  .setup-model-strategy__card,
  .setup-model-strategy__card:hover {
    transform: none;
    transition: none;
  }
}

.setup-model-strategy__card-title {
  font-size: var(--fs-sm);
  font-weight: 700;
}

.setup-model-strategy__card-heading {
  align-items: center;
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
  justify-content: space-between;
}

.setup-model-strategy__card-desc {
  color: var(--text-muted);
  font-size: var(--fs-xs);
  line-height: 1.35;
}

.setup-inline-link {
  align-items: center;
  background: none;
  border: 0;
  color: var(--accent);
  cursor: pointer;
  display: inline-flex;
  gap: var(--sp-1);
  font: inherit;
  font-weight: 600;
  padding: 0;
}

.setup-inline-link:hover {
  text-decoration: underline;
}

/* Sections separate by whitespace, not rules — mist keeps line-work minimal. */
.setup-model-strategy__detail {
  padding-top: var(--sp-4);
}

/* In router/ensemble mode the fixed model is only the fallback — keep it
   present but visually quiet so the active strategy owns the page. */
.setup-model-strategy__detail--secondary {
  opacity: 0.92;
  padding-top: 0;
}

.setup-model-strategy__detail.setup-model-strategy__detail--secondary {
  margin-top: var(--sp-2);
}

.setup-model-strategy__single-provider {
  align-items: center;
  display: flex;
  gap: var(--sp-4);
  justify-content: space-between;
  padding: var(--sp-2) 0;
}

.setup-model-strategy__single-provider span {
  color: var(--text-muted);
  font-size: var(--fs-sm);
}

.setup-model-strategy__single-provider .setup-model-strategy__single-provider-label {
  color: var(--text);
  font-weight: 700;
}

.setup-model-strategy__single-provider strong {
  font-size: var(--fs-sm);
}

.setup-model-strategy__single-provider-control {
  flex: 0 1 58%;
  inline-size: min(26rem, 58%);
  min-width: 0;
  position: relative;
}

.setup-model-strategy__single-provider-select {
  align-items: center;
  appearance: none;
  cursor: pointer;
  display: flex;
  inline-size: 100%;
  justify-content: flex-start;
  max-width: none;
  padding-inline-end: 2.5rem;
  text-align: left;
}

.setup-model-strategy__single-provider-value {
  color: var(--text);
  display: block;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.setup-model-strategy__single-provider-chevron {
  color: var(--text-dim);
  pointer-events: none;
  position: absolute;
  right: var(--sp-3);
  top: 50%;
  transform: translateY(-50%);
  transform-origin: 50% 50%;
  transition:
    color var(--dur-fast) var(--ease-standard),
    transform var(--dur-base) var(--ease-spring);
}

.setup-model-strategy__single-provider-control:focus-within
  .setup-model-strategy__single-provider-chevron {
  color: var(--accent);
}

.setup-model-strategy__single-provider-control.is-open
  .setup-model-strategy__single-provider-chevron {
  transform: translateY(-50%) rotate(180deg);
}

.setup-model-strategy__single-provider-menu {
  background: color-mix(in srgb, var(--accent) 2%, var(--bg-surface));
  border: 1px solid color-mix(in srgb, var(--text) 10%, transparent);
  border-radius: var(--radius-control);
  box-shadow: var(--shadow-lg);
  display: grid;
  gap: 2px;
  overscroll-behavior: contain;
  overflow-x: hidden;
  overflow-y: auto;
  padding: var(--sp-1);
  position: fixed;
  scrollbar-gutter: stable;
  z-index: 440;
}

.setup-model-strategy__single-provider-option {
  align-items: center;
  background: transparent;
  border: 0;
  border-radius: var(--radius-sm);
  color: var(--text-secondary);
  cursor: pointer;
  display: flex;
  font: inherit;
  font-size: var(--fs-sm);
  gap: var(--sp-2);
  justify-content: space-between;
  min-height: 38px;
  padding: var(--sp-2) var(--sp-3);
  text-align: left;
  width: 100%;
}

.setup-model-strategy__single-provider-option:hover,
.setup-model-strategy__single-provider-option.is-active {
  background: color-mix(in srgb, var(--accent) 7%, transparent);
  color: var(--text);
}

.setup-model-strategy__single-provider-option.is-selected {
  background: color-mix(in srgb, var(--accent) 10%, transparent);
  color: var(--text);
  font-weight: 600;
}

.setup-model-strategy__single-provider-option:focus-visible {
  box-shadow: var(--focus-ring-inset);
  outline: 0;
}

.setup-model-strategy__single-provider-option:disabled {
  cursor: not-allowed;
  opacity: 0.48;
}

.setup-model-strategy__single-provider-check {
  color: var(--accent);
  flex: 0 0 auto;
}

.fixed-provider-menu-enter-active,
.fixed-provider-menu-leave-active {
  transition:
    opacity var(--dur-fast) var(--ease-standard),
    transform var(--dur-fast) var(--ease-out);
  transform-origin: 50% 0;
}

.fixed-provider-menu-enter-from,
.fixed-provider-menu-leave-to {
  opacity: 0;
  transform: translateY(-4px) scale(0.985);
}

.setup-model-strategy__fixed-model-row.control-row--stack {
  align-items: center;
  flex-direction: row;
  gap: var(--sp-4);
  padding: var(--sp-2) 0;
}

.setup-model-strategy__fixed-model-row :deep(.control-row__label-block) {
  flex: 1 1 auto;
}

.setup-model-strategy__fixed-model-row :deep(.control-row__label) {
  font-weight: 700;
}

.setup-model-strategy__fixed-model-row :deep(.control-row__control) {
  flex: 0 1 58%;
  justify-content: flex-end;
  width: min(26rem, 58%);
}

.setup-model-strategy__single-provider-select,
.setup-model-strategy__fixed-model-row :deep(.control-input) {
  background: var(--bg-surface-2);
  border: 1px solid transparent;
  border-radius: var(--radius-control);
  max-width: none;
  min-height: 42px;
  transition:
    background var(--dur-fast) var(--ease-standard),
    border-color var(--dur-fast) var(--ease-standard),
    box-shadow var(--dur-fast) var(--ease-standard);
}

.setup-model-strategy__fixed-model-row :deep(.control-input) {
  width: 100%;
}

.setup-model-strategy__single-provider-select:hover,
.setup-model-strategy__fixed-model-row :deep(.control-input:hover) {
  background: color-mix(in srgb, var(--text) 3%, var(--bg-surface-2));
}

.setup-model-strategy__single-provider-select:focus,
.setup-model-strategy__fixed-model-row :deep(.control-input:focus) {
  background: var(--bg-surface-2);
  border-color: color-mix(in srgb, var(--accent) 36%, transparent);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 11%, transparent);
  outline: 0;
}

@media (prefers-reduced-motion: reduce) {
  .setup-model-strategy__single-provider-chevron,
  .fixed-provider-menu-enter-active,
  .fixed-provider-menu-leave-active {
    transition: none;
  }
}

.setup-model-strategy__roles-head {
  align-items: center;
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
  justify-content: space-between;
  margin-top: var(--sp-1);
}

.setup-model-strategy__roles-head h5,
.setup-model-strategy__roles-head p {
  margin: 0;
}

.setup-model-strategy__roles-head p {
  color: var(--text-muted);
  font-size: var(--fs-xs);
  line-height: 1.45;
}

.setup-inline-link.setup-model-strategy__add-provider {
  flex: 0 0 auto;
  font-size: var(--fs-xs);
}

.setup-model-strategy__sr-only {
  clip: rect(0, 0, 0, 0);
  height: 1px;
  margin: -1px;
  overflow: hidden;
  padding: 0;
  position: absolute;
  white-space: nowrap;
  width: 1px;
}

.setup-model-strategy__candidate-list {
  display: grid;
  gap: var(--sp-1);
}

.setup-model-strategy__candidate-group {
  display: grid;
  gap: var(--sp-1);
}

.setup-model-strategy__group-label {
  color: var(--text-muted);
  font-size: var(--fs-xs);
  font-weight: 700;
}

.setup-model-strategy__candidate-head {
  align-items: center;
  display: flex;
  gap: var(--sp-2);
  justify-content: space-between;
}

.setup-model-strategy__candidate {
  align-items: center;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text);
  display: flex;
  font-size: var(--fs-sm);
  gap: var(--sp-2);
  justify-content: space-between;
  padding: var(--sp-2);
}

.setup-model-strategy__candidate--aggregator {
  border-color: color-mix(in srgb, var(--accent) 38%, var(--border));
}

.setup-model-strategy__candidate-main {
  display: grid;
  gap: 2px;
  min-width: 0;
}

.setup-model-strategy__candidate-label {
  overflow-wrap: anywhere;
}

.setup-model-strategy__candidate-source {
  color: var(--text-muted);
  font-size: var(--fs-xs);
}

.setup-model-strategy__credential {
  color: var(--text-muted);
  flex: 0 0 auto;
  font-size: var(--fs-xs);
}

.setup-model-strategy__credential.is-missing {
  color: var(--warn);
}

.setup-model-strategy__candidate-remove {
  align-items: center;
  background: none;
  border: none;
  border-radius: var(--radius-full);
  color: var(--text-muted);
  cursor: pointer;
  display: inline-flex;
  flex: 0 0 auto;
  font-size: var(--fs-md);
  height: 1.5rem;
  justify-content: center;
  line-height: 1;
  padding: 0;
  width: 1.5rem;
}

.setup-model-strategy__candidate-remove:hover {
  background: var(--bg-hover);
  color: var(--text);
}

.setup-model-strategy__candidate-add {
  display: grid;
  gap: var(--sp-2);
  grid-template-columns: minmax(7rem, 0.4fr) minmax(10rem, 1fr) minmax(7rem, auto) auto;
}

.setup-model-strategy__candidate-add .control-input {
  min-width: 0;
}

@media (max-width: 720px) {
  .setup-model-strategy__candidate-add {
    grid-template-columns: 1fr;
  }
}

.setup-model-strategy__notice {
  background: color-mix(in srgb, var(--warn) 10%, transparent);
  border: 1px solid color-mix(in srgb, var(--warn) 42%, var(--border));
  border-radius: var(--radius-md);
  color: var(--text);
  font-size: var(--fs-xs);
  line-height: 1.4;
  margin: 0;
  padding: var(--sp-2);
}

.setup-model-strategy__notice--legacy {
  align-items: center;
  display: flex;
  gap: var(--sp-2);
  justify-content: space-between;
}

.setup-model-strategy__pipeline {
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  color: var(--text-muted);
  font-size: var(--fs-xs);
  line-height: 1.4;
  margin: 0;
  padding: var(--sp-2);
}

.setup-model-strategy__schemes {
  display: grid;
  gap: var(--sp-2);
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
}

.setup-model-strategy__scheme {
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  color: var(--text);
  cursor: pointer;
  display: grid;
  gap: var(--sp-1);
  padding: var(--sp-2);
  text-align: left;
}

.setup-model-strategy__scheme:hover {
  border-color: color-mix(in srgb, var(--accent) 48%, var(--border));
}

.setup-model-strategy__scheme.is-active {
  background: color-mix(in srgb, var(--accent) 8%, var(--bg-elevated));
  border-color: color-mix(in srgb, var(--accent) 62%, var(--border));
}

.setup-model-strategy__scheme-title {
  align-items: center;
  display: flex;
  flex-wrap: wrap;
  font-size: var(--fs-sm);
  font-weight: 700;
  gap: var(--sp-1);
}

.setup-model-strategy__scheme-badge {
  background: var(--accent);
  border-radius: var(--radius-full);
  color: var(--accent-foreground);
  font-size: 0.6875rem;
  font-weight: 600;
  padding: 1px var(--sp-2);
}

.setup-model-strategy__scheme-badge--soft {
  background: none;
  border: 1px solid var(--border);
  color: var(--text-muted);
}

.setup-model-strategy__scheme-desc {
  color: var(--text-muted);
  font-size: var(--fs-xs);
  line-height: 1.35;
}

.setup-model-strategy__count {
  color: var(--text-muted);
  font-size: var(--fs-xs);
  font-variant-numeric: tabular-nums;
  font-weight: 500;
  margin-left: var(--sp-1);
}

.setup-model-strategy__capacity {
  display: flex;
  gap: var(--sp-1);
}

.setup-model-strategy__capacity-cell {
  background: var(--bg-hover);
  border-radius: var(--radius-xs);
  flex: 1;
  height: 6px;
}

.setup-model-strategy__capacity-cell.is-filled {
  background: var(--accent);
}

.setup-model-strategy__capacity-cell.is-warn {
  background: var(--warn);
}

.setup-model-strategy__candidate--inherited {
  border-style: dashed;
}

.setup-model-strategy__role-select {
  flex: 0 0 auto;
  font-size: var(--fs-xs);
  max-width: 9.5rem;
}

.setup-model-strategy__facts {
  background: var(--bg-elevated);
  border-radius: var(--radius-md);
  color: var(--text-muted);
  font-size: var(--fs-xs);
  font-variant-numeric: tabular-nums;
  line-height: 1.5;
  margin: 0;
  padding: var(--sp-2);
}

.setup-model-strategy__muted {
  color: var(--text-muted);
  font-size: var(--fs-xs);
  line-height: 1.45;
  margin: 0;
}

.setup-warning__action {
  background: none;
  border: none;
  color: var(--accent);
  cursor: pointer;
  display: block;
  font: inherit;
  font-weight: 600;
  margin-top: var(--sp-1);
  padding: 0;
}

.setup-warning__action:hover {
  text-decoration: underline;
}

/* Ensemble editor: one explicit Proposer → Aggregator flow. The declarations
   below intentionally override the older table-like ensemble styling above so
   legacy configuration can remain readable without exposing advisory roles. */
.setup-model-strategy__cards {
  gap: var(--sp-2);
}

.setup-model-strategy__card {
  align-content: start;
  min-height: 4.5rem;
  padding: 10px var(--sp-2);
}

.setup-model-strategy__ensemble {
  display: grid;
  gap: var(--sp-3);
}

.setup-model-strategy__ensemble > .control-section__head {
  display: grid;
  gap: 3px;
}

.setup-model-strategy__schemes {
  background: var(--bg-surface-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  display: grid;
  gap: 2px;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  justify-self: start;
  padding: 2px;
  width: min(100%, 260px);
}

.setup-model-strategy__scheme {
  background: transparent;
  border: 0;
  border-radius: var(--radius-sm);
  color: var(--text-muted);
  display: block;
  font-size: var(--fs-sm);
  font-weight: 600;
  min-height: 2rem;
  padding: 5px var(--sp-3);
  text-align: center;
}

.setup-model-strategy__scheme:hover {
  background: var(--bg-hover);
  border-color: transparent;
  color: var(--text);
}

.setup-model-strategy__scheme.is-active {
  background: color-mix(in srgb, var(--accent) 15%, var(--bg-elevated));
  border-color: transparent;
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--accent) 26%, transparent);
  color: var(--accent-hover);
}

.setup-model-strategy__lineup {
  display: grid;
  gap: var(--sp-2);
}

.setup-model-strategy__step {
  display: grid;
  gap: var(--sp-2);
  min-width: 0;
}

.setup-model-strategy__step-head {
  align-items: center;
  display: flex;
  gap: var(--sp-2);
  min-height: 1.75rem;
}

.setup-model-strategy__step-number {
  align-items: center;
  background: var(--accent);
  border-radius: var(--radius-full);
  color: var(--accent-foreground);
  display: inline-flex;
  flex: 0 0 auto;
  font-size: var(--fs-xs);
  font-weight: 800;
  height: 1.5rem;
  justify-content: center;
  width: 1.5rem;
}

.setup-model-strategy__step-title {
  color: var(--text);
  font-size: var(--fs-sm);
  font-weight: 700;
}

.setup-model-strategy__count {
  color: var(--text-muted);
  font-size: var(--fs-xs);
  font-weight: 500;
  margin: 0;
}

@media (max-width: 680px) {
  .setup-model-strategy__single-provider {
    align-items: stretch;
    flex-direction: column;
    gap: var(--sp-2);
  }

  .setup-model-strategy__single-provider-control {
    inline-size: 100%;
    width: 100%;
  }

  .setup-model-strategy__fixed-model-row.control-row--stack {
    align-items: stretch;
    flex-direction: column;
    gap: var(--sp-2);
  }

  .setup-model-strategy__fixed-model-row :deep(.control-row__control) {
    width: 100%;
  }
}

.setup-model-strategy__import {
  background: transparent;
  border: 0;
  color: var(--text-muted);
  cursor: pointer;
  font: inherit;
  font-size: var(--fs-xs);
  margin-left: auto;
  padding: 4px 0;
}

.setup-model-strategy__import:not(:disabled):hover {
  color: var(--accent-hover);
}

.setup-model-strategy__import:disabled {
  cursor: not-allowed;
  opacity: var(--state-disabled-opacity);
}

.setup-model-strategy__candidate-list--grouped,
.setup-model-strategy__candidate-list--aggregator {
  background: color-mix(in srgb, var(--bg-surface-2) 60%, transparent);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  display: block;
}

.setup-model-strategy__candidate-list--aggregator {
  background: color-mix(in srgb, var(--accent) 6%, var(--bg-surface-2));
  border-color: color-mix(in srgb, var(--accent) 48%, var(--border));
  box-shadow: inset 3px 0 0 var(--accent);
}

.setup-model-strategy__candidate {
  background: transparent;
  border: 0;
  border-bottom: 1px solid var(--border);
  border-radius: 0;
  display: flex;
  gap: var(--sp-3);
  min-height: 2.85rem;
  padding: 8px var(--sp-3);
  position: relative;
}

.setup-model-strategy__candidate:last-child {
  border-bottom: 0;
}

.setup-model-strategy__candidate--inherited {
  border-style: none;
}

.setup-model-strategy__candidate-label,
.setup-model-strategy__candidate-main {
  min-width: 0;
}

.setup-model-strategy__candidate-label {
  flex: 1 1 auto;
  font-size: var(--fs-sm);
  line-height: 1.35;
}

.setup-model-strategy__credential {
  align-items: center;
  display: inline-flex;
  gap: 7px;
  margin-left: auto;
}

.setup-model-strategy__credential-dot {
  background: var(--ok);
  border-radius: var(--radius-full);
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--ok) 12%, transparent);
  height: 6px;
  width: 6px;
}

.setup-model-strategy__candidate-actions {
  flex: 0 0 auto;
  position: relative;
}

.setup-model-strategy__candidate-actions > summary {
  align-items: center;
  border-radius: var(--radius-sm);
  color: var(--text-muted);
  cursor: pointer;
  display: inline-flex;
  height: 1.75rem;
  justify-content: center;
  list-style: none;
  width: 2rem;
}

.setup-model-strategy__candidate-actions > summary::-webkit-details-marker {
  display: none;
}

.setup-model-strategy__candidate-actions > summary:hover,
.setup-model-strategy__candidate-actions[open] > summary {
  background: var(--bg-hover);
  color: var(--text);
}

.setup-model-strategy__candidate-menu {
  background: var(--bg-elevated);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-md);
  box-shadow: 0 12px 28px color-mix(in srgb, var(--bg) 72%, transparent);
  display: grid;
  min-width: 10.5rem;
  padding: 4px;
  position: absolute;
  right: 0;
  top: calc(100% + 4px);
  z-index: 12;
}

.setup-model-strategy__candidate-menu button {
  align-items: center;
  background: transparent;
  border: 0;
  border-radius: var(--radius-sm);
  color: var(--text);
  cursor: pointer;
  display: flex;
  font: inherit;
  font-size: var(--fs-xs);
  gap: var(--sp-2);
  padding: 8px;
  text-align: left;
}

.setup-model-strategy__candidate-menu button:hover {
  background: var(--bg-hover);
}

.setup-model-strategy__candidate-menu button.is-danger {
  color: var(--danger);
}

.setup-model-strategy__add-trigger {
  align-items: center;
  background: transparent;
  border: 1px dashed var(--border-strong);
  border-radius: var(--radius-md);
  color: var(--text-muted);
  cursor: pointer;
  display: flex;
  font: inherit;
  font-size: var(--fs-sm);
  gap: var(--sp-2);
  justify-content: center;
  min-height: 2.3rem;
  width: 100%;
}

.setup-model-strategy__add-trigger:not(:disabled):hover {
  background: color-mix(in srgb, var(--accent) 6%, transparent);
  border-color: color-mix(in srgb, var(--accent) 48%, var(--border));
  color: var(--accent-hover);
}

.setup-model-strategy__add-trigger:disabled {
  cursor: not-allowed;
  opacity: var(--state-disabled-opacity);
}

.setup-model-strategy__editor {
  align-items: end;
  background: var(--bg-surface-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
  padding: var(--sp-3);
}

.setup-model-strategy__editor-title {
  color: var(--text);
  flex: 1 0 100%;
  font-size: var(--fs-sm);
  font-weight: 700;
}

.setup-model-strategy__editor-label {
  color: var(--text-muted);
  flex: 1 0 100%;
  font-size: var(--fs-xs);
  font-weight: 600;
}

.setup-model-strategy__editor-error {
  color: var(--danger);
  flex: 1 0 100%;
  font-size: var(--fs-xs);
  margin: 0;
}

.setup-model-strategy__candidate-provider {
  flex: 0 1 12rem;
  min-height: 2.25rem;
}

.setup-model-strategy__candidate-model {
  flex: 1 1 15rem;
  min-width: 12rem;
}

.setup-model-strategy__editor-actions {
  display: flex;
  flex: 0 0 auto;
  gap: var(--sp-1);
  margin-left: auto;
}

.setup-model-strategy__guidance {
  display: grid;
  gap: var(--sp-1);
}

.setup-model-strategy__replace-aggregator {
  background: transparent;
  border: 0;
  color: var(--accent-hover);
  cursor: pointer;
  flex: 0 0 auto;
  font: inherit;
  font-size: var(--fs-xs);
  font-weight: 700;
  padding: 5px 0 5px var(--sp-2);
}

.setup-model-strategy__replace-aggregator:hover {
  text-decoration: underline;
}

.setup-model-strategy__aggregator-picker {
  align-items: stretch;
  display: grid;
}

.setup-model-strategy__aggregator-options {
  display: grid;
  gap: 3px;
}

.setup-model-strategy__aggregator-option {
  align-items: center;
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text);
  cursor: pointer;
  display: flex;
  font: inherit;
  font-size: var(--fs-xs);
  gap: var(--sp-2);
  justify-content: space-between;
  padding: 8px var(--sp-2);
  text-align: left;
}

.setup-model-strategy__aggregator-option:hover {
  border-color: color-mix(in srgb, var(--accent) 48%, var(--border));
}

.setup-model-strategy__preset-hint {
  color: var(--text-muted);
  font-size: var(--fs-xs);
  margin: 0;
  padding-left: var(--sp-1);
}

.setup-model-strategy__facts {
  align-items: center;
  background: var(--bg-surface-2);
  border: 1px solid transparent;
  display: flex;
  gap: var(--sp-2);
  min-height: 2.5rem;
  padding: 8px var(--sp-3);
}

.setup-model-strategy__runtime {
  background: var(--bg-surface-2);
  border: 1px solid transparent;
  border-radius: var(--radius-md);
}

.setup-model-strategy__runtime[open] {
  border-color: var(--border);
}

.setup-model-strategy__runtime > summary {
  align-items: center;
  color: var(--text);
  cursor: pointer;
  display: flex;
  gap: var(--sp-2);
  list-style: none;
  min-height: 3rem;
  padding: 8px var(--sp-3);
}

.setup-model-strategy__runtime > summary::-webkit-details-marker {
  display: none;
}

.setup-model-strategy__runtime > summary:hover {
  background: var(--bg-hover);
  border-radius: var(--radius-md);
}

.setup-model-strategy__runtime-title {
  display: grid;
  flex: 1;
  font-size: var(--fs-sm);
  font-weight: 700;
  gap: 2px;
}

.setup-model-strategy__runtime-title small {
  color: var(--text-muted);
  font-size: var(--fs-xs);
  font-weight: 400;
}

.setup-model-strategy__runtime-chevron {
  color: var(--text-muted);
  transition: transform var(--transition);
}

.setup-model-strategy__runtime[open] .setup-model-strategy__runtime-chevron {
  transform: rotate(180deg);
}

.setup-model-strategy__runtime-body {
  border-top: 1px solid var(--border);
  display: grid;
  padding: 0 var(--sp-3);
}

.setup-model-strategy__runtime-body .control-row {
  padding-left: 0;
  padding-right: 0;
}

.setup-model-strategy__runtime-value {
  color: var(--text);
  display: block;
  font-size: var(--fs-sm);
  overflow-wrap: anywhere;
  padding: var(--sp-2) 0;
}

.setup-model-strategy__runtime-limits {
  align-items: start;
  border-top: 1px solid var(--border);
  color: var(--text-muted);
  display: grid;
  font-size: var(--fs-xs);
  gap: 3px;
  padding: var(--sp-3) 0;
}

.setup-model-strategy__runtime-limits strong {
  color: var(--text);
  font-size: var(--fs-sm);
}

.setup-model-strategy__scheme:focus-visible,
.setup-model-strategy__import:focus-visible,
.setup-model-strategy__candidate-actions > summary:focus-visible,
.setup-model-strategy__candidate-menu button:focus-visible,
.setup-model-strategy__add-trigger:focus-visible,
.setup-model-strategy__replace-aggregator:focus-visible,
.setup-model-strategy__aggregator-option:focus-visible,
.setup-model-strategy__runtime > summary:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

@media (max-width: 720px) {
  .setup-model-strategy__cards {
    grid-template-columns: 1fr;
  }

  .setup-model-strategy__schemes {
    width: 100%;
  }

  .setup-model-strategy__step-head {
    flex-wrap: wrap;
  }

  .setup-model-strategy__import {
    flex-basis: 100%;
    margin-left: calc(1.5rem + var(--sp-2));
    text-align: left;
  }

  .setup-model-strategy__candidate {
    gap: var(--sp-2);
    padding-left: var(--sp-2);
    padding-right: var(--sp-2);
  }

  .setup-model-strategy__credential {
    font-size: 0;
  }

  .setup-model-strategy__editor {
    align-items: stretch;
    flex-direction: column;
  }

  .setup-model-strategy__candidate-provider,
  .setup-model-strategy__candidate-model {
    flex-basis: auto;
    min-width: 0;
    width: 100%;
  }

  .setup-model-strategy__editor-actions {
    justify-content: flex-end;
    margin-left: 0;
  }

  .setup-model-strategy__runtime-body .control-row {
    align-items: stretch;
    flex-direction: column;
  }
}
</style>
