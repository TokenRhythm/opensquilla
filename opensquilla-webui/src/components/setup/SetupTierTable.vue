<script setup lang="ts">
// The one tier table, extracted from the Router panel so the provider preset
// card can preview preset tiers with the identical component. Presentational
// only: props in, events out — no RPC, no form state.
//
// Three render modes per cell:
//   • default    — the stable model input stays in free-text mode;
//   • combobox   — that same input gains a provider-scoped catalog only when
//                  a verified live listing exists (no remount on async arrival);
//   • readonly   — preset preview: no editable controls at all.
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import ControlSwitch from '@/components/ControlSwitch.vue'
import Icon from '@/components/Icon.vue'
import SetupModelCombobox from '@/components/setup/SetupModelCombobox.vue'
import type {
  RouterProviderRoles,
  SetupProviderCredentialStatus,
  SetupProviderOption,
  SetupTierRow,
} from '@/composables/setup/useSetupRouterForm'
import type {
  DiscoveredModelCatalog,
  DiscoveredModelsByProvider,
} from '@/composables/setup/useSetupProviderForm'
import { ROUTER_DYNAMIC_SELECTION_MODE } from '@/types/generated/router_tier_contract'

const { t } = useI18n()

const props = withDefaults(defineProps<{
  rows: readonly SetupTierRow[]
  tierLabel: (tier: string) => string
  disabled?: boolean
  readonly?: boolean
  // Provider-scoped live catalogs. A tier only receives the catalog belonging
  // to its own normalized provider id, so mixed-provider routes stay isolated.
  modelsByProvider?: DiscoveredModelsByProvider
  providerOptions?: readonly SetupProviderOption[]
  providerCredentialStatus?: readonly SetupProviderCredentialStatus[]
  // Model Strategy supplies the one global direct/fallback target. Preset
  // previews omit it and receive generic, still truthful shared-plan copy.
  fixedFallbackProvider?: string
  fixedFallbackModel?: string
  ensembleAllFailedPolicy?: string
  ensembleMinSuccessful?: number
  ensembleProposerCount?: number
  ensembleProposerMaxRetries?: number
  ensemblePlanStatus?: 'ready' | 'attention' | 'blocked'
  ensemblePlanBlockedReason?: string
  ensembleFixedFallbackReady?: boolean | null
  routerProviderRoles?: RouterProviderRoles
  effectiveEnsembleSelectionMode?: string
}>(), {
  disabled: false,
  readonly: false,
  modelsByProvider: () => ({}),
  providerOptions: () => [],
  providerCredentialStatus: () => [],
  routerProviderRoles: () => ({}),
})

const emit = defineEmits<{
  updateTierField: [name: string, key: 'provider' | 'model' | 'thinkingLevel' | 'supportsImage' | 'ensembleEnabled' | 'ensembleSelectionMode', value: string | boolean]
  migrateLegacyEnsemble: []
}>()

const THINKING_LEVELS = ['', 'off', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh']
const ENSEMBLE_CHOICE = '__shared_ensemble__'
const EMPTY_CATALOG: DiscoveredModelCatalog = { models: [], source: 'none' }
const COMPACT_VIEWPORT_MAX_WIDTH = 760
const ENSEMBLE_TOOLTIP_MAX_WIDTH = 340
const ENSEMBLE_TOOLTIP_GAP = 7
const ENSEMBLE_TOOLTIP_MARGIN = 12
const hoveredEnsembleDetails = ref('')
const focusedEnsembleDetails = ref('')
const clickedEnsembleDetails = ref('')
const pointerActivationDetails = ref('')
const pointerActivationWasOpen = ref(false)
const ensembleDetailsAnchor = ref<HTMLElement | null>(null)
const ensembleTooltipUsesViewport = ref(false)
const ensembleTooltipPlacement = ref<'top' | 'bottom'>('top')
const ensembleTooltipStyle = ref<Record<string, string>>({})

function catalogFor(row: SetupTierRow): DiscoveredModelCatalog {
  const provider = row.provider.trim().toLowerCase()
  return props.modelsByProvider[provider] || EMPTY_CATALOG
}

function hasLiveCatalog(row: SetupTierRow): boolean {
  if (props.readonly || rowFieldsDisabled(row)) return false
  if (row.name === 'c3') return true
  const catalog = catalogFor(row)
  return catalog.source === 'live' && catalog.models.length > 0
}

function providerOptionsFor(row: SetupTierRow): SetupProviderOption[] {
  const current = row.provider.trim().toLowerCase()
  const seen = new Set<string>()
  const options: SetupProviderOption[] = []
  for (const option of props.providerOptions) {
    const providerId = String(option.providerId || '').trim().toLowerCase()
    if (!providerId || seen.has(providerId)) continue
    seen.add(providerId)
    options.push({
      providerId,
      label: option.label || providerId,
      disabled: option.disabled === true,
    })
  }
  // Keep historical/custom provider ids round-trippable without making an
  // unconfigured deployment selectable for new routing assignments.
  if (current && !seen.has(current)) {
    options.push({
      providerId: current,
      label: `${current} (${t('setup.summary.notConfigured')})`,
      disabled: true,
    })
  }
  return options
}

function credentialFor(row: SetupTierRow): SetupProviderCredentialStatus | undefined {
  const provider = row.provider.trim().toLowerCase()
  return props.providerCredentialStatus.find(status => (
    String(status.provider || '').trim().toLowerCase() === provider
  ))
}

function providerLabel(row: SetupTierRow): string {
  const provider = row.provider.trim().toLowerCase()
  return providerOptionsFor(row).find(option => option.providerId === provider)?.label || provider
}

function providerIsConfigured(row: SetupTierRow): boolean {
  const provider = row.provider.trim().toLowerCase()
  return props.providerOptions.some(option => (
    String(option.providerId || '').trim().toLowerCase() === provider
    && option.disabled !== true
  ))
}

function dependentFieldsDisabled(row: SetupTierRow): boolean {
  return props.disabled || !providerIsConfigured(row)
}

function tierEnsembleActive(row: SetupTierRow): boolean {
  if (row.ensembleEnabled === true) return true
  if (row.ensembleEnabled === false) return false
  return Boolean(row.ensembleSelectionMode)
}

function legacyTierEnsembleActive(row: SetupTierRow): boolean {
  return row.ensembleEnabled === undefined && Boolean(row.ensembleSelectionMode)
}

function providerManagedByEnsemble(row: SetupTierRow): boolean {
  if (row.name !== 'c3' || row.ensembleEnabled !== true) return false
  const role = props.routerProviderRoles[row.name] || 'direct'
  return role === 'dormant_draft' || role === 'blocked'
}

function rowFieldsDisabled(row: SetupTierRow): boolean {
  // The saved C3 provider/model are only the sleeping single-model draft while
  // shared fusion is selected. An unavailable draft provider must not trap the
  // user in fusion or make the active shared plan appear unavailable.
  if (providerManagedByEnsemble(row)) return props.disabled
  return dependentFieldsDisabled(row)
}

function providerFieldDisabled(): boolean {
  // An invalid/retired saved provider disables its dependent fields, not the
  // remediation control itself. C3 fusion only disables C3's own image input;
  // the dedicated image route remains an independent editable capability.
  return props.disabled
}

function imageSwitchDisabled(row: SetupTierRow): boolean {
  return rowFieldsDisabled(row) || (row.name === 'c3' && tierEnsembleActive(row))
}

function displayedImageSupport(row: SetupTierRow): boolean {
  if (row.name === 'c3' && tierEnsembleActive(row)) return false
  return row.supportsImage
}

function modelChoiceValue(row: SetupTierRow): string {
  return row.name === 'c3' && tierEnsembleActive(row) ? ENSEMBLE_CHOICE : row.model
}

function modelFieldLabel(row: SetupTierRow): string {
  return row.name === 'c3'
    ? t('setup.router.tierC3ChoiceAria')
    : t('setup.router.tierModelAria', { tier: row.name })
}

function ensembleSummaryId(row: SetupTierRow): string {
  return `setup-tier-${row.name}-ensemble-summary`
}

function ensembleDetailsId(row: SetupTierRow): string {
  return `setup-tier-${row.name}-ensemble-details`
}

function sharedTierEnsembleActive(row: SetupTierRow): boolean {
  return row.name === 'c3' && row.ensembleEnabled === true
}

function legacyDynamicTierEnsembleActive(row: SetupTierRow): boolean {
  if (row.name !== 'c3' || !tierEnsembleActive(row)) return false
  if (legacyTierEnsembleActive(row)) return row.ensembleSelectionMode === ROUTER_DYNAMIC_SELECTION_MODE
  return row.ensembleEnabled === true
    && String(props.effectiveEnsembleSelectionMode || '').trim() === ROUTER_DYNAMIC_SELECTION_MODE
}

function compatibilityTierEnsembleActive(row: SetupTierRow): boolean {
  return legacyTierEnsembleActive(row) || legacyDynamicTierEnsembleActive(row)
}

function effectiveTierEnsembleSelectionMode(row: SetupTierRow): string {
  if (legacyTierEnsembleActive(row)) return String(row.ensembleSelectionMode || '').trim()
  if (sharedTierEnsembleActive(row)) {
    return String(props.effectiveEnsembleSelectionMode || '').trim()
  }
  return ''
}

function thinkingManagedByEnsemble(row: SetupTierRow): boolean {
  return row.name === 'c3'
    && tierEnsembleActive(row)
    && effectiveTierEnsembleSelectionMode(row) !== ROUTER_DYNAMIC_SELECTION_MODE
}

function compactSharedTierEnsembleActive(row: SetupTierRow): boolean {
  return sharedTierEnsembleActive(row) && !legacyDynamicTierEnsembleActive(row)
}

function ensemblePlanStatusLabel(): string {
  if (props.ensemblePlanStatus === 'blocked') {
    return t('setup.router.tierEnsemblePlanBlocked')
  }
  return props.ensemblePlanStatus === 'attention'
    ? t('setup.router.tierEnsemblePlanAttention')
    : t('setup.router.tierEnsemblePlanReady')
}

const ensemblePlanBlockedReasonLabel = computed(() => {
  const reason = String(props.ensemblePlanBlockedReason || '').trim().toLowerCase()
  if (!reason) return ''
  if (
    props.ensembleFixedFallbackReady === false
    || reason === 'missing_fixed_fallback'
    || reason.startsWith('fixed_fallback:')
  ) return t('setup.router.tierEnsembleBlockedFixedFallback')
  if (reason === 'configuration_unavailable' || reason === 'unknown_selection_mode') {
    return t('setup.router.tierEnsembleBlockedGeneric')
  }
  // Static/custom member credential and lineup failures, plus dynamic member
  // resolution failures, all belong to the fusion plan rather than its valid
  // fixed fallback target. Keep their remediation copy distinct.
  return t('setup.router.tierEnsembleBlockedMember')
})

const fallbackContextProvided = computed(() => (
  props.fixedFallbackProvider !== undefined || props.fixedFallbackModel !== undefined
))

const hasExactFallbackTarget = computed(() => Boolean(
  String(props.fixedFallbackProvider || '').trim()
  && String(props.fixedFallbackModel || '').trim(),
))

function ensembleSummary(row: SetupTierRow): string {
  if (props.ensembleAllFailedPolicy === 'error') {
    return t('setup.router.tierEnsembleErrorSummary')
  }
  if (compatibilityTierEnsembleActive(row)) {
    const keyPrefix = legacyDynamicTierEnsembleActive(row)
      ? 'tierLegacyDynamicEnsemble'
      : 'tierLegacyEnsemble'
    if (hasExactFallbackTarget.value) {
      return t(`setup.router.${keyPrefix}Summary`, {
        provider: String(props.fixedFallbackProvider || '').trim(),
        model: String(props.fixedFallbackModel || '').trim(),
      })
    }
    if (fallbackContextProvided.value) {
      return t(`setup.router.${keyPrefix}FallbackMissingSummary`)
    }
    return t(`setup.router.${keyPrefix}SummaryGeneric`)
  }
  if (hasExactFallbackTarget.value) {
    return t('setup.router.tierEnsembleSummary', {
      provider: String(props.fixedFallbackProvider || '').trim(),
      model: String(props.fixedFallbackModel || '').trim(),
    })
  }
  if (fallbackContextProvided.value) {
    return t('setup.router.tierEnsembleFallbackMissingSummary')
  }
  return t('setup.router.tierEnsembleSummaryGeneric')
}

function ensemblePolicySummary(row: SetupTierRow): string {
  if (!sharedTierEnsembleActive(row)) return ''
  const proposers = Math.max(1, Math.trunc(Number(props.ensembleProposerCount) || 1))
  const quorum = Math.min(
    proposers,
    Math.max(1, Math.trunc(Number(props.ensembleMinSuccessful) || 1)),
  )
  const retries = Math.max(0, Math.trunc(Number(props.ensembleProposerMaxRetries) || 0))
  return t('setup.router.tierEnsemblePolicyFacts', { quorum, proposers, retries })
}

function showInlineEnsembleSummary(row: SetupTierRow): boolean {
  if (compatibilityTierEnsembleActive(row)) return true
  if (!sharedTierEnsembleActive(row)) return false
  return props.ensemblePlanStatus !== 'ready' || !hasExactFallbackTarget.value
}

function showInlinePlanStatus(row: SetupTierRow): boolean {
  return sharedTierEnsembleActive(row)
    && Boolean(props.ensemblePlanStatus)
    && props.ensemblePlanStatus !== 'ready'
}

function showInlineBlockedReason(row: SetupTierRow): boolean {
  return showInlinePlanStatus(row) && Boolean(ensemblePlanBlockedReasonLabel.value)
}

function showInlineImageRule(row: SetupTierRow): boolean {
  return row.name === 'c3' && compatibilityTierEnsembleActive(row)
}

function ensembleDescriptionId(row: SetupTierRow): string | undefined {
  if (!tierEnsembleActive(row)) return undefined
  return showInlineEnsembleSummary(row)
    ? ensembleSummaryId(row)
    : ensembleDetailsId(row)
}

function ensembleDetailsOpen(row: SetupTierRow): boolean {
  const id = ensembleDetailsId(row)
  return clickedEnsembleDetails.value === id
    || hoveredEnsembleDetails.value === id
    || focusedEnsembleDetails.value === id
}

const openEnsembleDetailsId = computed(() => (
  clickedEnsembleDetails.value
  || focusedEnsembleDetails.value
  || hoveredEnsembleDetails.value
))

function compactEnsembleTooltip(): boolean {
  return typeof window !== 'undefined' && window.innerWidth <= COMPACT_VIEWPORT_MAX_WIDTH
}

function ensembleHoverAvailable(): boolean {
  return typeof window === 'undefined'
    || typeof window.matchMedia !== 'function'
    || window.matchMedia('(hover: hover)').matches
}

function ensembleDetailsTrigger(event: Event): HTMLElement | null {
  const target = event.currentTarget
  if (!(target instanceof HTMLElement)) return null
  if (target.matches('.setup-tier-table__ensemble-details-trigger')) return target
  return target.querySelector<HTMLElement>('.setup-tier-table__ensemble-details-trigger')
}

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(Math.max(value, minimum), Math.max(minimum, maximum))
}

// At the compact breakpoint the tier table becomes a horizontal scrollport.
// Portal the tooltip to <body> and clamp it to the viewport so scrolling to
// the Thinking/Image columns cannot crop a left-aligned C3 explanation.
function updateEnsembleTooltipPosition() {
  if (!openEnsembleDetailsId.value) return

  const viewportWidth = window.innerWidth
  const viewportHeight = window.innerHeight
  const useViewport = viewportWidth <= COMPACT_VIEWPORT_MAX_WIDTH
  const viewportModeChanged = ensembleTooltipUsesViewport.value !== useViewport
  if (viewportModeChanged) {
    ensembleTooltipUsesViewport.value = useViewport
  }
  if (!useViewport) {
    ensembleTooltipStyle.value = {}
    ensembleTooltipPlacement.value = 'top'
    return
  }

  const availableWidth = Math.max(1, viewportWidth - (2 * ENSEMBLE_TOOLTIP_MARGIN))
  const effectiveMaxWidth = Math.min(ENSEMBLE_TOOLTIP_MAX_WIDTH, availableWidth)
  const maxWidth = `${effectiveMaxWidth}px`
  const maxWidthChanged = ensembleTooltipStyle.value.maxWidth !== maxWidth
  if (viewportModeChanged || maxWidthChanged) {
    // Apply the final cap after Teleport moves the tooltip, then measure its
    // wrapped height and actual max-content width in that final layout.
    ensembleTooltipStyle.value = { maxWidth }
    void nextTick(updateEnsembleTooltipPosition)
    return
  }

  const anchor = ensembleDetailsAnchor.value
  const tooltip = document.getElementById(openEnsembleDetailsId.value)
  if (!anchor || !tooltip) return

  const anchorRect = anchor.getBoundingClientRect()
  const tooltipRect = tooltip.getBoundingClientRect()
  const tooltipWidth = Math.min(
    tooltipRect.width || effectiveMaxWidth,
    effectiveMaxWidth,
  )
  const tooltipHeight = tooltipRect.height
  const preferredLeft = anchorRect.right - tooltipWidth
  const left = clamp(
    preferredLeft,
    ENSEMBLE_TOOLTIP_MARGIN,
    viewportWidth - tooltipWidth - ENSEMBLE_TOOLTIP_MARGIN,
  )

  const topPosition = anchorRect.top - tooltipHeight - ENSEMBLE_TOOLTIP_GAP
  const bottomPosition = anchorRect.bottom + ENSEMBLE_TOOLTIP_GAP
  const fitsAbove = topPosition >= ENSEMBLE_TOOLTIP_MARGIN
  const fitsBelow = bottomPosition + tooltipHeight <= viewportHeight - ENSEMBLE_TOOLTIP_MARGIN
  const spaceAbove = anchorRect.top - ENSEMBLE_TOOLTIP_GAP - ENSEMBLE_TOOLTIP_MARGIN
  const spaceBelow = viewportHeight
    - anchorRect.bottom
    - ENSEMBLE_TOOLTIP_GAP
    - ENSEMBLE_TOOLTIP_MARGIN
  const placeBelow = !fitsAbove && (fitsBelow || spaceBelow > spaceAbove)
  const maximumTop = viewportHeight - tooltipHeight - ENSEMBLE_TOOLTIP_MARGIN
  const top = clamp(
    placeBelow ? bottomPosition : topPosition,
    ENSEMBLE_TOOLTIP_MARGIN,
    maximumTop,
  )

  ensembleTooltipPlacement.value = placeBelow ? 'bottom' : 'top'
  ensembleTooltipStyle.value = {
    left: `${left}px`,
    maxWidth,
    top: `${top}px`,
  }
}

function showEnsembleDetails(
  row: SetupTierRow,
  source: 'hover' | 'focus',
  event: Event,
) {
  const id = ensembleDetailsId(row)
  if (source === 'hover' && compactEnsembleTooltip() && !ensembleHoverAvailable()) return
  ensembleDetailsAnchor.value = ensembleDetailsTrigger(event)
  if (
    source === 'focus'
    && compactEnsembleTooltip()
    && pointerActivationDetails.value === id
  ) return
  if (source === 'hover') hoveredEnsembleDetails.value = id
  else focusedEnsembleDetails.value = id
}

function clearEnsembleDetails(id: string) {
  if (hoveredEnsembleDetails.value === id) hoveredEnsembleDetails.value = ''
  if (focusedEnsembleDetails.value === id) focusedEnsembleDetails.value = ''
  if (clickedEnsembleDetails.value === id) clickedEnsembleDetails.value = ''
  if (pointerActivationDetails.value === id) {
    pointerActivationDetails.value = ''
    pointerActivationWasOpen.value = false
  }
  if (!openEnsembleDetailsId.value) ensembleDetailsAnchor.value = null
}

function hideEnsembleDetails(row: SetupTierRow, source: 'hover' | 'focus') {
  const id = ensembleDetailsId(row)
  if (source === 'hover' && hoveredEnsembleDetails.value === id) hoveredEnsembleDetails.value = ''
  if (source === 'focus') {
    if (focusedEnsembleDetails.value === id) focusedEnsembleDetails.value = ''
    if (clickedEnsembleDetails.value === id) clickedEnsembleDetails.value = ''
    if (pointerActivationDetails.value === id) {
      pointerActivationDetails.value = ''
      pointerActivationWasOpen.value = false
    }
  }
  if (!openEnsembleDetailsId.value) {
    ensembleDetailsAnchor.value = null
  }
}

function beginEnsembleDetailsPointerActivation(row: SetupTierRow, event: Event) {
  if (!compactEnsembleTooltip()) return
  const id = ensembleDetailsId(row)
  ensembleDetailsAnchor.value = ensembleDetailsTrigger(event)
  pointerActivationDetails.value = id
  pointerActivationWasOpen.value = ensembleDetailsOpen(row)
}

function cancelEnsembleDetailsPointerActivation(row: SetupTierRow) {
  const id = ensembleDetailsId(row)
  if (pointerActivationDetails.value !== id) return
  pointerActivationDetails.value = ''
  pointerActivationWasOpen.value = false
}

function toggleEnsembleDetails(row: SetupTierRow, event: MouseEvent) {
  if (!compactEnsembleTooltip()) return
  const id = ensembleDetailsId(row)
  const trigger = ensembleDetailsTrigger(event)
  ensembleDetailsAnchor.value = trigger
  const pointerActivation = pointerActivationDetails.value === id
  const keyboardActivation = event.detail === 0 && !pointerActivation
  const shouldClose = clickedEnsembleDetails.value === id
    || (pointerActivation && pointerActivationWasOpen.value)
    || (keyboardActivation && ensembleDetailsOpen(row))

  if (shouldClose) {
    clearEnsembleDetails(id)
    if (!keyboardActivation) trigger?.blur()
    return
  }

  clickedEnsembleDetails.value = id
  if (trigger && document.activeElement !== trigger) trigger.focus({ preventScroll: true })
  pointerActivationDetails.value = ''
  pointerActivationWasOpen.value = false
}

function onEnsembleDetailsKeydown(row: SetupTierRow, event: KeyboardEvent) {
  if (event.key !== 'Escape' || !ensembleDetailsOpen(row)) return
  event.preventDefault()
  event.stopPropagation()
  clearEnsembleDetails(ensembleDetailsId(row))
}

function startEnsembleTooltipTracking() {
  updateEnsembleTooltipPosition()
  window.addEventListener('scroll', updateEnsembleTooltipPosition, {
    capture: true,
    passive: true,
  })
  window.addEventListener('resize', updateEnsembleTooltipPosition)
}

function stopEnsembleTooltipTracking() {
  window.removeEventListener('scroll', updateEnsembleTooltipPosition, { capture: true })
  window.removeEventListener('resize', updateEnsembleTooltipPosition)
}

watch(openEnsembleDetailsId, id => {
  stopEnsembleTooltipTracking()
  if (!id) return
  startEnsembleTooltipTracking()
  void nextTick(updateEnsembleTooltipPosition)
})

onBeforeUnmount(stopEnsembleTooltipTracking)

function c3StateAnnouncement(row: SetupTierRow): string {
  if (tierEnsembleActive(row)) {
    return [
      sharedTierEnsembleActive(row) && props.ensemblePlanStatus
        ? ensemblePlanStatusLabel()
        : '',
      sharedTierEnsembleActive(row) ? ensemblePlanBlockedReasonLabel.value : '',
      ensembleSummary(row),
      t('setup.router.tierEnsembleImageRouting'),
    ].filter(Boolean).join(' ')
  }
  return t('setup.router.tierSingleModelAnnouncement', { model: row.model || '-' })
}

function updateModelChoice(row: SetupTierRow, value: string) {
  if (row.name !== 'c3') {
    emit('updateTierField', row.name, 'model', value)
    return
  }
  if (value === ENSEMBLE_CHOICE) {
    emit('updateTierField', row.name, 'ensembleEnabled', true)
    emit('updateTierField', row.name, 'ensembleSelectionMode', '')
    return
  }
  emit('updateTierField', row.name, 'ensembleEnabled', false)
  emit('updateTierField', row.name, 'ensembleSelectionMode', '')
  emit('updateTierField', row.name, 'model', value)
}

const showProviderColumn = computed(() => {
  if (props.readonly) return true
  if (props.rows.some(row => (
    !providerManagedByEnsemble(row) && credentialFor(row)?.available === false
  ))) return true

  const configuredProviders = new Set(props.providerOptions
    .filter(option => option.disabled !== true)
    .map(option => String(option.providerId || '').trim().toLowerCase())
    .filter(Boolean))

  if (configuredProviders.size !== 1) return true

  const [onlyProvider] = [...configuredProviders]
  return props.rows.some(row => (
    !providerManagedByEnsemble(row)
    && row.provider.trim().toLowerCase() !== onlyProvider
  ))
})

// The combobox dropdown and compact-plan tooltip are absolutely positioned;
// the table's rounded-corner overflow clip must open whenever either floats.
const hasCombobox = computed(() => props.rows.some(row => hasLiveCatalog(row)))
const allowsFloatingContent = computed(() => (
  hasCombobox.value || props.rows.some(row => compactSharedTierEnsembleActive(row))
))
</script>

<template>
  <div
    class="setup-tier-table"
    :class="{
      'setup-tier-table--open': allowsFloatingContent,
      'setup-tier-table--without-provider': !showProviderColumn,
    }"
    role="table"
    :aria-disabled="disabled ? 'true' : undefined"
  >
    <div class="setup-tier-table__row is-head" role="row">
      <span>{{ t('setup.router.colTier') }}</span><span v-if="showProviderColumn">{{ t('setup.router.colProvider') }}</span><span>{{ t('setup.router.colModel') }}</span><span>{{ t('setup.router.colThinking') }}</span><span>{{ t('setup.router.colImage') }}</span>
    </div>
    <div
      v-for="tier in rows"
      :key="tier.name"
      class="setup-tier-table__row"
      :class="{ 'is-disabled': providerFieldDisabled() }"
      role="row"
      :aria-disabled="providerFieldDisabled() ? 'true' : undefined"
    >
      <span class="setup-tier-table__tier">{{ tierLabel(tier.name) }}</span>
      <template v-if="showProviderColumn">
        <span
          v-if="readonly || providerManagedByEnsemble(tier)"
          class="setup-tier-table__readonly"
          :aria-label="providerManagedByEnsemble(tier)
            ? t('setup.router.tierProviderManagedByEnsembleAria', { tier: tier.name })
            : t('setup.router.tierProviderAria', { tier: tier.name })"
          :title="providerManagedByEnsemble(tier)
            ? t('setup.router.tierProviderManagedByEnsemble')
            : t('setup.router.tierProviderAria', { tier: tier.name })"
        >{{ providerManagedByEnsemble(tier) ? t('setup.router.tierProviderManagedByEnsemble') : tier.provider || '-' }}</span>
        <div v-else class="setup-tier-table__provider-cell">
          <select
            :value="tier.provider.trim().toLowerCase()"
            :aria-label="t('setup.router.tierProviderAria', { tier: tier.name })"
            :aria-invalid="credentialFor(tier) && !credentialFor(tier)?.available ? 'true' : undefined"
            :disabled="providerFieldDisabled()"
            @change="emit('updateTierField', tier.name, 'provider', ($event.target as HTMLSelectElement).value)"
          >
            <option v-if="!tier.provider" value="" disabled>-</option>
            <option
              v-for="option in providerOptionsFor(tier)"
              :key="option.providerId"
              :value="option.providerId"
              :disabled="option.disabled"
            >
              {{ option.label }}
            </option>
          </select>
          <small
            v-if="credentialFor(tier) && !credentialFor(tier)?.available"
            class="setup-tier-table__provider-warning"
          >
            {{ t('setup.modelStrategy.credentialNeeded', { provider: providerLabel(tier) }) }}
          </small>
        </div>
      </template>
      <template v-if="readonly">
        <div class="setup-tier-table__model-cell">
          <div class="setup-tier-table__model-primary">
            <span
              class="setup-tier-table__readonly"
              :aria-label="modelFieldLabel(tier)"
              :aria-describedby="ensembleDescriptionId(tier)"
              :title="tierEnsembleActive(tier) ? t('setup.router.tierUseEnsemble') : tier.model || undefined"
            >
              {{ tierEnsembleActive(tier) ? t('setup.router.tierUseEnsemble') : tier.model || '-' }}
            </span>
            <span
              v-if="compactSharedTierEnsembleActive(tier)"
              class="setup-tier-table__ensemble-details"
              @mouseenter="showEnsembleDetails(tier, 'hover', $event)"
              @mouseleave="hideEnsembleDetails(tier, 'hover')"
            >
              <button
                type="button"
                class="setup-tier-table__ensemble-details-trigger"
                :aria-label="t('setup.router.tierEnsembleDetailsAria')"
                :aria-describedby="ensembleDetailsId(tier)"
                :aria-expanded="ensembleDetailsOpen(tier) ? 'true' : 'false'"
                :data-open="ensembleDetailsOpen(tier) ? 'true' : 'false'"
                @pointerdown="beginEnsembleDetailsPointerActivation(tier, $event)"
                @pointercancel="cancelEnsembleDetailsPointerActivation(tier)"
                @pointerleave="cancelEnsembleDetailsPointerActivation(tier)"
                @focus="showEnsembleDetails(tier, 'focus', $event)"
                @blur="hideEnsembleDetails(tier, 'focus')"
                @click="toggleEnsembleDetails(tier, $event)"
                @keydown="onEnsembleDetailsKeydown(tier, $event)"
              >
                <Icon name="info" :size="13" aria-hidden="true" />
              </button>
              <Teleport to="body" :disabled="!ensembleTooltipUsesViewport">
                <span
                  :id="ensembleDetailsId(tier)"
                  class="setup-tier-table__ensemble-tooltip"
                  :class="{
                    'is-open': ensembleDetailsOpen(tier),
                    'is-viewport-positioned': ensembleTooltipUsesViewport,
                  }"
                  :data-placement="ensembleTooltipPlacement"
                  :style="ensembleTooltipUsesViewport ? ensembleTooltipStyle : undefined"
                  role="tooltip"
                >
                  <strong>{{ ensemblePlanStatusLabel() }}</strong>
                  <span v-if="ensemblePlanBlockedReasonLabel">{{ ensemblePlanBlockedReasonLabel }}</span>
                  <span>{{ ensembleSummary(tier) }}</span>
                  <span v-if="ensemblePolicySummary(tier)">{{ ensemblePolicySummary(tier) }}</span>
                  <span>{{ t('setup.router.tierEnsembleImageRouting') }}</span>
                </span>
              </Teleport>
            </span>
          </div>
          <small
            v-if="showInlineEnsembleSummary(tier)"
            :id="ensembleSummaryId(tier)"
            class="setup-tier-table__model-note"
          >
            {{ ensembleSummary(tier) }} {{ ensemblePolicySummary(tier) }}
          </small>
          <small
            v-if="showInlinePlanStatus(tier)"
            class="setup-tier-table__plan-status"
            :class="ensemblePlanStatus === 'blocked' ? 'is-blocked' : 'needs-attention'"
          >{{ ensemblePlanStatusLabel() }}</small>
          <small
            v-if="showInlineBlockedReason(tier)"
            class="setup-tier-table__blocked-reason"
          >{{ ensemblePlanBlockedReasonLabel }}</small>
          <small v-if="showInlineImageRule(tier)" class="setup-tier-table__model-note">
            {{ t('setup.router.tierEnsembleImageRouting') }}
          </small>
        </div>
        <span
          class="setup-tier-table__readonly"
          :aria-label="thinkingManagedByEnsemble(tier)
            ? t('setup.router.tierThinkingManagedByEnsembleAria', { tier: tier.name })
            : t('setup.router.tierThinkingAria', { tier: tier.name })"
        >{{ thinkingManagedByEnsemble(tier) ? t('setup.router.tierThinkingManagedByEnsemble') : tier.thinkingLevel || '-' }}</span>
        <ControlSwitch :checked="displayedImageSupport(tier)" :disabled="true" :aria-label="t('setup.router.tierImageAria', { tier: tier.name })" />
      </template>
      <template v-else>
        <div class="setup-tier-table__model-cell">
          <div class="setup-tier-table__model-primary">
            <SetupModelCombobox
              cell
              :field="{ name: `tier_${tier.name}_model`, label: modelFieldLabel(tier), placeholder: modelFieldLabel(tier) }"
              :value="modelChoiceValue(tier)"
              :models="catalogFor(tier).models"
              :model-source="catalogFor(tier).source"
              :disabled="rowFieldsDisabled(tier)"
              :commit-on-select="tier.name === 'c3'"
              :external-description-id="ensembleDescriptionId(tier)"
              :leading-option="tier.name === 'c3' ? {
                value: ENSEMBLE_CHOICE,
                label: t('setup.router.tierUseEnsemble'),
                description: t('setup.router.tierUseEnsembleDescription'),
              } : undefined"
              @update="(val) => updateModelChoice(tier, val)"
            />
            <span
              v-if="compactSharedTierEnsembleActive(tier)"
              class="setup-tier-table__ensemble-details"
              @mouseenter="showEnsembleDetails(tier, 'hover', $event)"
              @mouseleave="hideEnsembleDetails(tier, 'hover')"
            >
              <button
                type="button"
                class="setup-tier-table__ensemble-details-trigger"
                :aria-label="t('setup.router.tierEnsembleDetailsAria')"
                :aria-describedby="ensembleDetailsId(tier)"
                :aria-expanded="ensembleDetailsOpen(tier) ? 'true' : 'false'"
                :data-open="ensembleDetailsOpen(tier) ? 'true' : 'false'"
                @pointerdown="beginEnsembleDetailsPointerActivation(tier, $event)"
                @pointercancel="cancelEnsembleDetailsPointerActivation(tier)"
                @pointerleave="cancelEnsembleDetailsPointerActivation(tier)"
                @focus="showEnsembleDetails(tier, 'focus', $event)"
                @blur="hideEnsembleDetails(tier, 'focus')"
                @click="toggleEnsembleDetails(tier, $event)"
                @keydown="onEnsembleDetailsKeydown(tier, $event)"
              >
                <Icon name="info" :size="13" aria-hidden="true" />
              </button>
              <Teleport to="body" :disabled="!ensembleTooltipUsesViewport">
                <span
                  :id="ensembleDetailsId(tier)"
                  class="setup-tier-table__ensemble-tooltip"
                  :class="{
                    'is-open': ensembleDetailsOpen(tier),
                    'is-viewport-positioned': ensembleTooltipUsesViewport,
                  }"
                  :data-placement="ensembleTooltipPlacement"
                  :style="ensembleTooltipUsesViewport ? ensembleTooltipStyle : undefined"
                  role="tooltip"
                >
                  <strong>{{ ensemblePlanStatusLabel() }}</strong>
                  <span v-if="ensemblePlanBlockedReasonLabel">{{ ensemblePlanBlockedReasonLabel }}</span>
                  <span>{{ ensembleSummary(tier) }}</span>
                  <span v-if="ensemblePolicySummary(tier)">{{ ensemblePolicySummary(tier) }}</span>
                  <span>{{ t('setup.router.tierEnsembleImageRouting') }}</span>
                </span>
              </Teleport>
            </span>
          </div>
          <small
            v-if="showInlineEnsembleSummary(tier)"
            :id="ensembleSummaryId(tier)"
            class="setup-tier-table__model-note"
          >
            {{ ensembleSummary(tier) }} {{ ensemblePolicySummary(tier) }}
          </small>
          <button
            v-if="legacyDynamicTierEnsembleActive(tier)"
            type="button"
            class="setup-inline-link setup-tier-table__legacy-migrate"
            data-testid="tier-ensemble-migrate-legacy"
            @click="emit('migrateLegacyEnsemble')"
          >{{ t('setup.modelStrategy.legacyDynamicMigrate') }}</button>
          <small
            v-if="showInlinePlanStatus(tier)"
            class="setup-tier-table__plan-status"
            :class="ensemblePlanStatus === 'blocked' ? 'is-blocked' : 'needs-attention'"
          >{{ ensemblePlanStatusLabel() }}</small>
          <small
            v-if="showInlineBlockedReason(tier)"
            class="setup-tier-table__blocked-reason"
          >{{ ensemblePlanBlockedReasonLabel }}</small>
          <small v-if="showInlineImageRule(tier)" class="setup-tier-table__model-note">
            {{ t('setup.router.tierEnsembleImageRouting') }}
          </small>
        </div>
        <span
          v-if="thinkingManagedByEnsemble(tier)"
          class="setup-tier-table__readonly"
          :aria-label="t('setup.router.tierThinkingManagedByEnsembleAria', { tier: tier.name })"
          :title="t('setup.router.tierThinkingManagedByEnsemble')"
        >{{ t('setup.router.tierThinkingManagedByEnsemble') }}</span>
        <select v-else :value="tier.thinkingLevel" :aria-label="t('setup.router.tierThinkingAria', { tier: tier.name })" :disabled="rowFieldsDisabled(tier)" @change="emit('updateTierField', tier.name, 'thinkingLevel', ($event.target as HTMLSelectElement).value)">
          <option v-for="v in THINKING_LEVELS" :key="v" :value="v">{{ v || '-' }}</option>
        </select>
        <ControlSwitch :checked="displayedImageSupport(tier)" :disabled="imageSwitchDisabled(tier)" :aria-label="t('setup.router.tierImageAria', { tier: tier.name })" @change="(v) => emit('updateTierField', tier.name, 'supportsImage', v)" />
      </template>
      <span
        v-if="tier.name === 'c3' && !readonly"
        class="setup-tier-table__sr-only"
        role="status"
        aria-live="polite"
        aria-atomic="true"
      >{{ c3StateAnnouncement(tier) }}</span>
    </div>
  </div>
</template>

<style scoped>
/* Let the combobox dropdown escape the table's rounded-corner clip; the head
   row keeps its own rounding so the corners still look clipped. */
.setup-tier-table--open {
  overflow: visible;
}

.setup-tier-table--open .setup-tier-table__row.is-head {
  border-radius: var(--radius-md) var(--radius-md) 0 0;
}

.setup-tier-table--without-provider .setup-tier-table__row {
  grid-template-columns: 140px minmax(0, 1fr) 120px 60px;
}

.setup-tier-table__provider-cell {
  display: grid;
  gap: 2px;
  min-width: 0;
}

.setup-tier-table__provider-cell select {
  min-width: 0;
  width: 100%;
}

.setup-tier-table__provider-warning {
  color: var(--danger);
  font-size: 10px;
  line-height: 1.2;
}

.setup-tier-table__model-cell {
  display: grid;
  gap: 3px;
  min-width: 0;
  position: relative;
}

.setup-tier-table__model-primary {
  align-items: center;
  display: grid;
  gap: 4px;
  grid-template-columns: minmax(0, 1fr) auto;
  min-width: 0;
}

.setup-tier-table__model-note {
  color: var(--text-muted);
  font-size: 10px;
  line-height: 1.2;
  overflow-wrap: anywhere;
}

.setup-tier-table__legacy-migrate {
  align-self: start;
  font-size: 10px;
  justify-self: start;
  line-height: 1.2;
  padding: 0;
}

.setup-tier-table__plan-status {
  font-size: 10px;
  font-weight: 600;
  line-height: 1.2;
}

.setup-tier-table__plan-status.is-ready {
  color: var(--ok);
}

.setup-tier-table__plan-status.needs-attention {
  color: var(--warning-text, var(--text-muted));
}

.setup-tier-table__plan-status.is-blocked {
  color: var(--danger);
}

.setup-tier-table__blocked-reason {
  color: var(--danger);
  font-size: 10px;
  line-height: 1.2;
}

.setup-tier-table__ensemble-details {
  align-items: center;
  display: inline-flex;
  justify-self: end;
  position: relative;
}

.setup-tier-table__ensemble-details-trigger {
  align-items: center;
  background: transparent;
  border: 0;
  border-radius: var(--radius-full);
  color: var(--text-dim);
  cursor: help;
  display: inline-flex;
  height: 24px;
  justify-content: center;
  padding: 0;
  width: 24px;
}

.setup-tier-table__ensemble-details-trigger:hover {
  color: var(--text);
}

.setup-tier-table__ensemble-details-trigger:focus-visible {
  color: var(--text);
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}

.setup-tier-table__ensemble-tooltip {
  background: var(--text);
  border-radius: var(--radius-sm);
  bottom: calc(100% + 7px);
  box-shadow: var(--shadow-md);
  color: var(--bg-elevated);
  display: grid;
  font-size: var(--fs-xs);
  font-weight: 400;
  gap: 5px;
  inset-inline-end: 0;
  inset-inline-start: auto;
  line-height: 1.4;
  max-width: min(340px, calc(100vw - 24px));
  opacity: 0;
  padding: 8px 10px;
  pointer-events: none;
  position: absolute;
  text-align: left;
  visibility: hidden;
  white-space: normal;
  width: max-content;
  z-index: 40;
}

.setup-tier-table__ensemble-tooltip.is-viewport-positioned {
  bottom: auto;
  inset: auto;
  position: fixed;
  z-index: 440;
}

.setup-tier-table__ensemble-tooltip.is-open {
  opacity: 1;
  visibility: visible;
}

.setup-tier-table__row.is-disabled:not(.is-head) {
  color: var(--text-muted);
}

.setup-tier-table__sr-only {
  clip: rect(0, 0, 0, 0);
  clip-path: inset(50%);
  height: 1px;
  margin: -1px;
  overflow: hidden;
  padding: 0;
  position: absolute;
  white-space: nowrap;
  width: 1px;
}

@media (max-width: 760px) {
  .setup-tier-table--without-provider .setup-tier-table__row {
    min-width: 460px;
  }
}
</style>
