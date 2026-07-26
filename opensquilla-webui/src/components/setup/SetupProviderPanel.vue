<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import SetupField from '@/components/SetupField.vue'
import SetupNeedList from '@/components/SetupNeedList.vue'
import SetupCommandBlock from '@/components/setup/SetupCommandBlock.vue'
import SetupProviderCredentialCard from '@/components/setup/SetupProviderCredentialCard.vue'
import SetupProviderRecommendation from '@/components/setup/SetupProviderRecommendation.vue'
import SetupModelCombobox from '@/components/setup/SetupModelCombobox.vue'
import SetupProviderCatalogDialog from '@/components/setup/SetupProviderCatalogDialog.vue'
import type {
  ConnectionState,
  DiscoveredModel,
  ProviderCredentialPanelState,
} from '@/composables/setup/useSetupProviderForm'
import { parseContextWindowInput } from '@/composables/setup/useSettingsPromotedForm'
import type { SetupTierRow } from '@/composables/setup/useSetupRouterForm'
import { localizedRelativeTime } from '@/utils/messageTime'

const { t, locale } = useI18n()

interface ProviderOption {
  providerId: string
  label: string
}

interface FieldSpec {
  name: string
  label: string
  type?: string
  default?: string | boolean | number
  [key: string]: unknown
}

interface ProviderPanelContract {
  providerSummary: string
  providerSelected: string
  runtimeProviders: ProviderOption[]
  routerSupportTone: string
  routerSupportText: string
  canConfigureRouter: boolean
  providerNeeds: string[]
  providerCoreFields: FieldSpec[]
  providerAdvancedFields: FieldSpec[]
  credentialPanel: ProviderCredentialPanelState | null
  providerAdvancedOpen: boolean
  providerEnvMissing: boolean
  providerEnvKey: string
  providerEnvCommand: string
  llmTimeoutSeconds: number
  contextWindowTokens: string
  contextWindowGlobal: number | null
  effectiveMaxTokens: {
    value: number
    source: 'config' | 'catalog' | 'default'
  } | null
  providerIsLocal: boolean
  configuredProviders: Array<{
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
    lastProbe?: { ok: boolean; at: string; configChanged: boolean; failureKind: string } | null
  }>
  credentialRemovalPending: boolean
  editingPrimary: boolean
  selectedStoredProfile: boolean
  editingNew: boolean
  routingEnabled: boolean
  routerEnabled: boolean
  routerBinding: 'follow_primary' | 'custom' | 'legacy'
  crossProviderRoutingEnabled: boolean
  ensembleEnabled: boolean
  activationRouterConflict: boolean
  configuredProviderProbes: Record<string, ConnectionState>
  activation: {
    providerId: string
    phase: 'idle' | 'discovering' | 'ready' | 'activating' | 'error'
    models: DiscoveredModel[]
    suggestedModel: string
    error: string
  }
  connection: ConnectionState
  providerFieldValue: (field: FieldSpec) => string
}

interface PresetCardContract {
  hasPreset: boolean
  presetLabel: string
  presetDescription: string
  synthesized: boolean
  tierRows: SetupTierRow[]
  tierLabel: (tier: string) => string
  routerMode: string
  routerCustomized: boolean
}

const props = defineProps<{
  panel: ProviderPanelContract
  // Optional routing-preset card contract (absent on older gateways whose
  // catalog carries no presets — the card simply doesn't render).
  preset?: PresetCardContract | null
}>()

const emit = defineEmits<{
  updateProviderSelected: [value: string]
  providerChange: []
  updateProviderField: [name: string, value: unknown]
  updateLlmTimeout: [value: number]
  updateContextWindow: [value: string]
  probeConnection: []
  applyPreset: []
  copy: [command: string]
  goToSection: [value: string]
  selectConfiguredProvider: [value: string]
  removeProviderProfile: [value: string]
  addProvider: [value: string]
  probeConfiguredProvider: [value: string]
  activateProvider: [providerId: string]
}>()

const addOpen = ref(false)
const listExpanded = ref(false)
const addButtonRef = ref<HTMLButtonElement | null>(null)
const editorHeadingRef = ref<HTMLElement | null>(null)
const sectionRef = ref<HTMLElement | null>(null)
const pendingRemoval = ref<{ providerId: string; index: number } | null>(null)

const selectedProviderLabel = computed(() => (
  props.panel.credentialPanel?.providerLabel || props.panel.providerSelected
))

const readyProviderCount = computed(() => (
  props.panel.configuredProviders.filter(provider => provider.ready).length
))

const editingPrimaryDraft = computed(() => (
  props.panel.editingPrimary
  && !props.panel.configuredProviders.some(provider => (
    provider.active && isEditingProvider(provider.providerId)
  ))
))

const allProviderFields = computed(() => [
  ...props.panel.providerCoreFields,
  ...props.panel.providerAdvancedFields,
])

const modelFields = computed(() => allProviderFields.value.filter(field => field.name === 'model'))
const endpointFields = computed(() => allProviderFields.value.filter(
  field => field.name === 'base_url' || field.name === 'proxy',
))
const otherProviderFields = computed(() => allProviderFields.value.filter(
  field => !['model', 'base_url', 'proxy'].includes(field.name),
))

function isEditingProvider(providerId: string): boolean {
  return props.panel.providerSelected.trim().toLowerCase() === providerId.trim().toLowerCase()
}

function displayField(field: FieldSpec): FieldSpec {
  if (field.name !== 'model') return field
  return {
    ...field,
    label: props.panel.editingPrimary
      ? t('setup.provider.defaultModelLabel', { provider: selectedProviderLabel.value })
      : t('setup.provider.profileModelLabel'),
    description: props.panel.editingPrimary
      ? t('setup.provider.defaultModelDesc')
      : t('setup.provider.profileModelDesc'),
  }
}

const configuredIds = computed(() => new Set(
  props.panel.configuredProviders.map(provider => provider.providerId.trim().toLowerCase()),
))

const visibleConfiguredProviders = computed(() => (
  props.panel.configuredProviders.length >= 5 && !listExpanded.value
    ? props.panel.configuredProviders.slice(0, 3)
    : props.panel.configuredProviders
))

function chooseAddProvider(providerId: string) {
  emit('addProvider', providerId)
}

function closeAddPicker(restoreFocus = true) {
  if (!addOpen.value) return
  addOpen.value = false
  if (restoreFocus) void nextTick(() => addButtonRef.value?.focus())
}

function toggleAddPicker() {
  if (addOpen.value) {
    closeAddPicker()
    return
  }
  addOpen.value = true
}

function selectConfigured(providerId: string) {
  emit('selectConfiguredProvider', providerId)
}

function testConfigured(providerId: string) {
  emit('probeConfiguredProvider', providerId)
}

function activateConfigured(providerId: string) {
  emit('activateProvider', providerId)
}

function removeConfigured(providerId: string) {
  pendingRemoval.value = {
    providerId,
    index: props.panel.configuredProviders.findIndex(row => row.providerId === providerId),
  }
  emit('removeProviderProfile', providerId)
}

const selectedConfiguredProvider = computed(() => props.panel.configuredProviders.find(
  provider => isEditingProvider(provider.providerId),
))

const modelUsageModeKey = computed(() => {
  if (props.panel.ensembleEnabled) return 'setup.provider.modelUsageEnsemble'
  if (props.panel.routerEnabled) return 'setup.provider.modelUsageRouter'
  return 'setup.provider.modelUsageFixed'
})

const modelUsageDescriptionKey = computed(() => {
  if (props.panel.ensembleEnabled) return 'setup.provider.modelUsageEnsembleDesc'
  if (props.panel.routerEnabled) return 'setup.provider.modelUsageRouterDesc'
  return 'setup.provider.modelUsageFixedDesc'
})

// One routing entry per panel: while the configured-primary model card is
// shown (it carries the mode pills and the Model Routing link), the bottom
// model-usage summary must not render its duplicate.
const primaryModelCardVisible = computed(() => (
  modelFields.value.length > 0 && props.panel.editingPrimary && !editingPrimaryDraft.value
))

const configuredModelLabelKey = computed(() => (
  props.panel.routerEnabled || props.panel.ensembleEnabled
    ? 'setup.provider.configuredModelFallbackLabel'
    : 'setup.provider.configuredModelCurrentLabel'
))

const modelSectionTitle = computed(() => {
  if (!props.panel.editingPrimary) {
    return t('setup.provider.profileModelTitle', { provider: selectedProviderLabel.value })
  }
  if (editingPrimaryDraft.value) {
    return t('setup.provider.defaultModelTitle', { provider: selectedProviderLabel.value })
  }
  return t('setup.provider.configuredModelTitle', { provider: selectedProviderLabel.value })
})

const modelSectionDesc = computed(() => {
  if (!props.panel.editingPrimary) return t('setup.provider.profileModelGroupDesc')
  if (editingPrimaryDraft.value) return t('setup.provider.defaultModelGroupDesc')
  if (props.panel.ensembleEnabled) return t('setup.provider.configuredModelDescEnsemble')
  if (props.panel.routerEnabled) return t('setup.provider.configuredModelDescRouter')
  return t('setup.provider.configuredModelDescFixed')
})

watch(() => props.panel.providerSelected, (value, previous) => {
  if (!value || value === previous) return
  const selectedFromPicker = addOpen.value
  addOpen.value = false
  void nextTick(() => {
    const credentialInput = selectedFromPicker
      ? sectionRef.value?.querySelector<HTMLInputElement>('input[name="setup_provider_api_key"]:not([disabled])')
      : null
    const target = credentialInput ?? editorHeadingRef.value
    target?.scrollIntoView({ block: 'nearest' })
    target?.focus({ preventScroll: true })
  })
})

watch(() => props.panel.configuredProviders, rows => {
  if (rows.length < 5) listExpanded.value = false
  const pending = pendingRemoval.value
  if (pending && !rows.some(row => row.providerId === pending.providerId)) {
    pendingRemoval.value = null
    void nextTick(() => {
      const next = rows[Math.min(pending.index, Math.max(0, rows.length - 1))]
      const nextRow = next
        ? Array.from(sectionRef.value?.querySelectorAll<HTMLElement>('[data-provider-id]') || [])
            .find(element => element.dataset.providerId === next.providerId)
        : null
      const target = nextRow?.querySelector<HTMLElement>('.setup-provider-card__select')
        ?? sectionRef.value?.querySelector<HTMLElement>('[data-provider-picker-trigger]')
      target?.focus()
    })
  }
}, { deep: true })

function probeFor(providerId: string): ConnectionState {
  return (props.panel.configuredProviderProbes || {})[providerId.toLowerCase()] || {
    phase: 'unverified', failureKind: '', detail: '',
    firstResponseMs: null, totalMs: null, latencyMs: null,
    models: [], modelSource: 'none', discoverError: '',
  }
}

const activationState = computed(() => props.panel.activation || {
  providerId: '', phase: 'idle' as const, models: [], suggestedModel: '', error: '',
})

function activationDisabledReason(provider: ProviderPanelContract['configuredProviders'][number]): string {
  if (provider.primaryEligible) return ''
  if (provider.primaryBlockReason === 'missing_model') {
    return t('setup.provider.activationModelRequiredHint')
  }
  if (provider.primaryBlockReason === 'primary_pool_unsupported') {
    return t('setup.provider.activationPoolUnsupported')
  }
  if (['profile_status_unavailable', 'runtime_unsupported', 'unknown_provider'].includes(
    provider.primaryBlockReason,
  )) {
    return t('setup.provider.activationStatusUnavailable')
  }
  return t('setup.provider.activationUnavailable')
}

function activationInProgress(providerId: string): boolean {
  return activationState.value.phase === 'activating'
    && activationState.value.providerId.toLowerCase() === providerId.toLowerCase()
}

const activationBusy = computed(() => activationState.value.phase === 'activating')
const providerBusy = computed(() => (
  activationBusy.value || props.panel.credentialRemovalPending
))

watch(() => props.panel.credentialRemovalPending, (pending, wasPending) => {
  if (pending || !wasPending) return
  void nextTick(() => {
    const credentialInput = sectionRef.value?.querySelector<HTMLInputElement>(
      'input[name="setup_provider_api_key"]:not([disabled])',
    )
    const target = credentialInput ?? editorHeadingRef.value
    target?.scrollIntoView({ block: 'nearest' })
    target?.focus({ preventScroll: true })
  })
})

function activationActionLabel(
  provider: ProviderPanelContract['configuredProviders'][number],
): string {
  const action = activationInProgress(provider.providerId)
    ? t('setup.provider.activating')
    : t('setup.provider.makeActive')
  return `${action} — ${provider.label}`
}

const PROBE_FAILURE_SENTENCE_KEYS: Record<string, string> = {
  auth_invalid: 'setup.provider.failureAuth',
  insufficient_credits: 'setup.provider.failureCredits',
  rate_limited: 'setup.provider.failureRateLimited',
  provider_overloaded: 'setup.provider.failureOverloaded',
  model_not_found: 'setup.provider.failureModelNotFound',
  transport_transient: 'setup.provider.failureUnreachable',
  bad_request: 'setup.provider.failureBadRequest',
}

const PROTOCOL_FAILURE_KINDS = new Set([
  'malformed_response',
  'invalid_stream_frame',
  'invalid_stream_order',
])

function probeFailureSentence(state: ConnectionState): string {
  const key = PROBE_FAILURE_SENTENCE_KEYS[state.failureKind]
  if (key) return t(key)
  if (state.detail) return state.detail
  return t('setup.provider.failureGeneric')
}

function configuredStatus(provider: ProviderPanelContract['configuredProviders'][number]): string {
  if (providerStatusUnavailable(provider)) return t('setup.provider.profileStatusUnavailable')
  return provider.ready ? t('setup.provider.profileReady') : t('setup.provider.profileNeedsCredentials')
}

function providerStatusUnavailable(provider: ProviderPanelContract['configuredProviders'][number]): boolean {
  return [
    'profile_status_unavailable',
    'runtime_unsupported',
    'unknown_provider',
  ].includes(provider.reason)
}

function configuredTestLabel(provider: ProviderPanelContract['configuredProviders'][number]): string {
  if (probeFor(provider.providerId).phase === 'probing') return t('setup.provider.testing')
  if (providerStatusUnavailable(provider)) return t('setup.provider.profileStatusUnavailable')
  if (!provider.ready) return t('setup.provider.addKeyToTest')
  if (!provider.probeModelAvailable) return t('setup.provider.addModelToTest')
  return t('setup.provider.testSavedConnection')
}

function configuredRowFor(
  providerId: string,
): ProviderPanelContract['configuredProviders'][number] | undefined {
  return props.panel.configuredProviders.find(row => (
    row.providerId.toLowerCase() === providerId.toLowerCase()
  ))
}

function probeStatus(providerId: string): string {
  const state = probeFor(providerId)
  if (state.phase === 'probing') return t('setup.provider.testing')
  if (state.phase === 'unverified') {
    const provider = configuredRowFor(providerId)
    if (!provider?.ready) return ''
    const lastProbe = provider.lastProbe
    if (lastProbe?.ok) {
      return lastProbe.configChanged
        ? t('setup.provider.verifiedConfigChanged')
        : t('setup.provider.lastVerifiedAgo', {
            ago: localizedRelativeTime(lastProbe.at, locale.value),
          })
    }
    if (lastProbe) {
      return t('setup.provider.lastVerifyFailedAgo', {
        ago: localizedRelativeTime(lastProbe.at, locale.value),
      })
    }
    return t('setup.provider.connectionNotTested')
  }
  if (state.phase === 'verified') {
    return t('setup.provider.connected')
  }
  if (PROTOCOL_FAILURE_KINDS.has(state.failureKind)) {
    return t('setup.provider.streamIncompatible')
  }
  if (state.phase === 'key_invalid') {
    return t('setup.provider.keyRejected', { reason: probeFailureSentence(state) })
  }
  if (state.phase === 'unreachable') {
    return t('setup.provider.notReachable', { reason: probeFailureSentence(state) })
  }
  return ''
}

function probeToneClass(providerId: string): string {
  const state = probeFor(providerId)
  if (state.phase === 'probing') return ''
  if (state.phase === 'verified') return 'is-ready'
  if (state.phase !== 'unverified') return 'is-warn'
  const lastProbe = configuredRowFor(providerId)?.lastProbe
  if (lastProbe?.ok) return lastProbe.configChanged ? '' : 'is-ready'
  return 'is-warn'
}

function providerIdentityLabel(
  provider: ProviderPanelContract['configuredProviders'][number],
): string {
  const parts = [t('setup.provider.editProvider', { provider: provider.label })]
  if (provider.active) parts.push(t('setup.provider.activeBadge'))
  if (isEditingProvider(provider.providerId)) parts.push(t('setup.provider.editingBadge'))
  parts.push(configuredStatus(provider))
  const probe = probeStatus(provider.providerId)
  if (probe) parts.push(probe)
  return parts.join(' — ')
}

function probeTiming(providerId: string, field: 'firstResponseMs' | 'totalMs'): string {
  const duration = probeFor(providerId)[field]
  if (typeof duration !== 'number' || !Number.isFinite(duration) || duration < 0) return ''
  return t(
    field === 'firstResponseMs'
      ? 'setup.provider.firstModelResponse'
      : 'setup.provider.completeProbeDuration',
    { duration: Math.round(duration) },
  )
}

function useCombobox(field: FieldSpec): boolean {
  // Keep one stable model-picker shell before, during, and after discovery.
  // SetupModelCombobox degrades to a normal free-text input when no live
  // catalog exists, so async discovery never swaps the focused DOM control.
  return field.name === 'model'
}

// ---------------------------------------------------------------------------
// Context-window override (advanced)
// ---------------------------------------------------------------------------

// Local runtimes commonly truncate silently below this window; warn when the
// effective budget lands at or under it.
const LOCAL_CONTEXT_WINDOW_WARN_TOKENS = 8192

const currentModelId = computed(() => {
  const fields = [...props.panel.providerCoreFields, ...props.panel.providerAdvancedFields]
  const modelField = fields.find(f => f.name === 'model') || { name: 'model', label: 'model' }
  return String(props.panel.providerFieldValue(modelField) || '').trim()
})

// Auto-detected window: the discovery row for the model currently in the form.
const contextWindowAuto = computed<number | null>(() => {
  if (!currentModelId.value) return null
  const row = props.panel.connection.models.find(m => m.id === currentModelId.value)
  return typeof row?.contextWindow === 'number' ? row.contextWindow : null
})

const contextWindowOverride = computed<number | null>(() => (
  parseContextWindowInput(props.panel.contextWindowTokens)
))

// Precedence mirrors the backend resolver (provider/resolution.py): a per-model
// override wins, else the global llm.context_window_tokens layer, else the
// auto-detected discovery window.
const contextWindowEffective = computed<{ value: number | null; source: 'override' | 'config' | 'auto' }>(() => {
  if (contextWindowOverride.value != null) {
    return { value: contextWindowOverride.value, source: 'override' }
  }
  if (props.panel.contextWindowGlobal != null && props.panel.contextWindowGlobal > 0) {
    return { value: props.panel.contextWindowGlobal, source: 'config' }
  }
  return { value: contextWindowAuto.value, source: 'auto' }
})

const contextWindowReadout = computed(() => t('setup.provider.contextWindowReadout', {
  auto: contextWindowAuto.value != null
    ? String(contextWindowAuto.value)
    : t('setup.provider.contextWindowUnknown'),
  override: contextWindowOverride.value != null
    ? String(contextWindowOverride.value)
    : t('setup.provider.contextWindowNone'),
  effective: contextWindowEffective.value.value != null
    ? String(contextWindowEffective.value.value)
    : t('setup.provider.contextWindowUnknown'),
}))

const effectiveMaxTokensReadout = computed(() => {
  const record = props.panel.effectiveMaxTokens
  if (!record) return ''
  const sourceKey = {
    config: 'setup.provider.effectiveSourceConfig',
    catalog: 'setup.provider.effectiveSourceCatalog',
    default: 'setup.provider.effectiveSourceDefault',
  }[record.source]
  return t('setup.provider.effectiveMaxTokens', {
    tokens: new Intl.NumberFormat(locale.value).format(record.value),
    source: t(sourceKey),
  })
})

const showContextWindowWarning = computed(() => (
  props.panel.providerIsLocal
  && contextWindowEffective.value.value != null
  && contextWindowEffective.value.value <= LOCAL_CONTEXT_WINDOW_WARN_TOKENS
))

const showTokenRhythmRecommendation = computed(() => {
  return props.panel.runtimeProviders.some(
    provider => provider.providerId.trim().toLowerCase() === 'tokenrhythm',
  )
})

const tokenRhythmSelected = computed(() => (
  props.panel.providerSelected.trim().toLowerCase() === 'tokenrhythm'
))

const tokenRhythmCredentialReplacementRequired = computed(() => (
  tokenRhythmSelected.value
  && Boolean(props.panel.credentialPanel?.masked)
  && !props.panel.credentialPanel?.replacing
))
</script>

<template>
  <section ref="sectionRef" class="control-section setup-provider-page">
    <div class="control-section__head setup-provider-page__head">
      <h3 class="control-section__title">{{ t('setup.provider.pageTitle') }}</h3>
      <p class="control-section__desc">{{ t('setup.provider.pageDesc') }}</p>
    </div>

    <fieldset
      class="setup-provider-interactions"
      :disabled="providerBusy"
      :aria-busy="providerBusy ? 'true' : undefined"
    >

    <div class="setup-provider-overview">
      <div class="setup-provider-overview__copy">
        <div class="setup-provider-overview__title-row">
          <h4>{{ t('setup.provider.configuredTitle') }}</h4>
          <span class="control-pill">{{ t('setup.provider.configuredCount', { count: panel.configuredProviders.length }) }}</span>
        </div>
        <p
          v-if="panel.configuredProviders.length > 0"
          id="setup-provider-configured-desc"
        >{{ t('setup.provider.configuredSummary', {
          count: panel.configuredProviders.length,
          ready: readyProviderCount,
        }) }}</p>
      </div>
      <div class="setup-provider-add">
        <button
          v-if="!addOpen"
          ref="addButtonRef"
          type="button"
          class="btn btn--primary"
          data-provider-picker-trigger
          aria-controls="setup-provider-catalog-picker"
          :aria-expanded="addOpen ? 'true' : 'false'"
          @click="toggleAddPicker"
        >
          <Icon name="plus" :size="15" aria-hidden="true" />
          {{ t('setup.provider.addProvider') }}
        </button>
      </div>
    </div>

    <SetupProviderCatalogDialog
      :open="addOpen"
      :providers="panel.runtimeProviders"
      :configured-ids="Array.from(configuredIds)"
      @close="closeAddPicker"
      @select="chooseAddProvider"
    />

    <div
      v-if="panel.configuredProviders.length === 0 && !panel.providerSelected"
      class="setup-provider-empty"
      data-testid="provider-empty-state"
    >
      <span class="setup-provider-empty__icon" aria-hidden="true">
        <Icon name="agents" :size="20" />
      </span>
      <div>
        <strong>{{ t('setup.provider.emptyTitle') }}</strong>
        <p>{{ t('setup.provider.configuredEmpty') }}</p>
        <p class="setup-provider-empty__next">{{ t('setup.provider.emptyRoutingHint') }}</p>
      </div>
    </div>

    <ul
      v-if="panel.configuredProviders.length > 0"
      class="setup-provider-list"
      data-testid="configured-provider-list"
    >
      <li
        v-for="provider in visibleConfiguredProviders"
        :key="provider.providerId"
        class="setup-provider-card"
        :class="{ 'is-selected': isEditingProvider(provider.providerId) }"
        :data-provider-id="provider.providerId"
      >
        <button
          type="button"
          class="setup-provider-card__identity setup-provider-card__select"
          :aria-label="providerIdentityLabel(provider)"
          :aria-current="isEditingProvider(provider.providerId) ? 'true' : undefined"
          @click="selectConfigured(provider.providerId)"
        >
          <span class="setup-provider-card__name-row">
            <span class="setup-provider-card__name">{{ provider.label }}</span>
            <span v-if="provider.active" class="control-pill control-pill--ok">{{ t('setup.provider.activeBadge') }}</span>
            <span v-if="isEditingProvider(provider.providerId)" class="control-pill setup-provider-card__editing">
              {{ t('setup.provider.editingBadge') }}
            </span>
          </span>
          <span
            class="setup-provider-card__status"
            :class="provider.ready ? 'is-ready' : 'is-warn'"
            :title="provider.reason || undefined"
          >{{ configuredStatus(provider) }}</span>
          <span
            v-if="probeStatus(provider.providerId)"
            class="setup-provider-card__probe"
            :class="probeToneClass(provider.providerId)"
            aria-live="polite"
          >
            <span>{{ probeStatus(provider.providerId) }}</span>
            <span
              v-if="probeTiming(provider.providerId, 'firstResponseMs')"
              class="setup-provider-card__probe-timing setup-provider-card__probe-timing--primary"
            > · {{ probeTiming(provider.providerId, 'firstResponseMs') }}</span>
            <span
              v-if="probeTiming(provider.providerId, 'totalMs')"
              class="setup-provider-card__probe-timing"
            > · {{ probeTiming(provider.providerId, 'totalMs') }}</span>
          </span>
        </button>
        <div class="setup-provider-card__actions">
          <button
            type="button"
            class="btn setup-provider-card__test"
            :disabled="!provider.ready || !provider.probeModelAvailable || probeFor(provider.providerId).phase === 'probing'"
            :title="providerStatusUnavailable(provider) ? t('setup.provider.statusUnavailableTestHint') : (!provider.ready ? t('setup.provider.addKeyToTestHint') : (!provider.probeModelAvailable ? t('setup.provider.addModelToTestHint') : undefined))"
            :aria-label="`${configuredTestLabel(provider)} — ${provider.label}`"
            :aria-describedby="provider.ready && provider.probeModelAvailable ? 'setup-provider-configured-desc' : undefined"
            @click="testConfigured(provider.providerId)"
          >{{ configuredTestLabel(provider) }}</button>
          <button
            v-if="!provider.active"
            type="button"
            class="btn btn--ghost setup-provider-card__activate"
            :disabled="Boolean(activationDisabledReason(provider)) || activationInProgress(provider.providerId)"
            :title="activationDisabledReason(provider) || undefined"
            :aria-label="activationActionLabel(provider)"
            @click="activateConfigured(provider.providerId)"
          >{{ activationInProgress(provider.providerId)
            ? t('setup.provider.activating')
            : t('setup.provider.makeActive') }}</button>
          <button
            v-if="!provider.active"
            type="button"
            class="btn btn--ghost setup-provider-card__delete"
            :aria-label="`${t('common.delete')} — ${provider.label}`"
            @click="removeConfigured(provider.providerId)"
          >{{ t('common.delete') }}</button>
        </div>
      </li>
    </ul>
    <button
      v-if="panel.configuredProviders.length >= 5"
      type="button"
      class="btn btn--ghost setup-provider-list__toggle"
      :aria-expanded="listExpanded ? 'true' : 'false'"
      @click="listExpanded = !listExpanded"
    >{{ listExpanded ? t('setup.provider.showFewerProviders') : t('setup.provider.viewAllProviders', { count: panel.configuredProviders.length }) }}</button>

    <SetupProviderRecommendation
      v-if="showTokenRhythmRecommendation"
      :token-rhythm-selected="tokenRhythmSelected"
      :credential-replacement-required="tokenRhythmCredentialReplacementRequired"
    />

    <template v-if="panel.providerSelected">
    <div class="setup-provider-editor-head" data-testid="provider-editor-scope">
      <div>
        <div class="setup-provider-editor-head__title-row">
          <h4 ref="editorHeadingRef" tabindex="-1">{{ t('setup.provider.editingTitle', { provider: selectedProviderLabel }) }}</h4>
          <span class="control-pill">
            {{ editingPrimaryDraft
              ? t('setup.provider.editingRoleNewPrimary')
              : (panel.editingPrimary
                ? t('setup.provider.editingRolePrimary')
                : (panel.editingNew ? t('setup.provider.editingRoleNew') : t('setup.provider.editingRoleProfile'))) }}
          </span>
        </div>
        <p>{{ editingPrimaryDraft
          ? t('setup.provider.editingNewPrimary')
            : (panel.editingPrimary
              ? t('setup.provider.editingPrimary')
              : (panel.editingNew ? t('setup.provider.editingNew') : t('setup.provider.editingProfile'))) }}</p>
      </div>
      <button
        v-if="panel.selectedStoredProfile && selectedConfiguredProvider"
        type="button"
        class="btn setup-provider-editor-head__activate"
        :disabled="Boolean(activationDisabledReason(selectedConfiguredProvider)) || activationInProgress(selectedConfiguredProvider.providerId)"
        :title="activationDisabledReason(selectedConfiguredProvider) || undefined"
        :aria-label="activationActionLabel(selectedConfiguredProvider)"
        @click="activateConfigured(selectedConfiguredProvider.providerId)"
      >{{ activationInProgress(selectedConfiguredProvider.providerId)
        ? t('setup.provider.activating')
        : t('setup.provider.makeActive') }}</button>
    </div>
    <SetupNeedList :items="panel.providerNeeds" :label="t('setup.provider.needs')" />

    <SetupProviderCredentialCard
      v-if="panel.credentialPanel"
      :panel="panel.credentialPanel"
      @reveal="panel.credentialPanel.onReveal?.()"
      @hide-reveal="panel.credentialPanel.onHideReveal?.()"
      @replace="panel.credentialPanel.onReplace?.()"
      @cancel-replace="panel.credentialPanel.onCancelReplace?.()"
      @remove-credential="panel.credentialPanel.onRemoveCredential?.()"
      @test-connection="emit('probeConnection')"
      @update-field="(name, value) => emit('updateProviderField', name, value)"
    />

    <section v-if="modelFields.length" class="setup-provider-model setup-provider-model--primary">
      <div class="setup-provider-options__head">
        <h5>{{ modelSectionTitle }}</h5>
        <p>{{ modelSectionDesc }}</p>
      </div>
      <div
        v-if="primaryModelCardVisible"
        class="setup-provider-model__routing-owner"
        data-testid="configured-primary-model-readonly"
      >
        <div class="setup-provider-model__routing-value">
          <div class="setup-provider-model__routing-meta">
            <span class="setup-provider-model__routing-label">{{ t(configuredModelLabelKey) }}</span>
            <span class="control-pill control-pill--accent">{{ t(modelUsageModeKey) }}</span>
            <span v-if="panel.crossProviderRoutingEnabled" class="control-pill">
              {{ t('setup.provider.crossProviderActive') }}
            </span>
          </div>
          <strong>{{ currentModelId || t('setup.provider.contextWindowNoModel') }}</strong>
        </div>
        <button type="button" class="btn btn--ghost" @click="emit('goToSection', 'modelStrategy')">
          {{ t('setup.provider.configureModelRouting') }}
          <Icon name="chevronRight" :size="15" aria-hidden="true" />
        </button>
      </div>
      <template v-else v-for="field in modelFields" :key="field.name">
        <SetupModelCombobox
          v-if="useCombobox(field)"
          :field="displayField(field)"
          :value="panel.providerFieldValue(field)"
          :models="panel.connection.models"
          :model-source="panel.connection.modelSource"
          @update="(val) => emit('updateProviderField', 'model', val)"
        />
        <SetupField
          v-else
          :field="displayField(field)"
          :value="panel.providerFieldValue(field)"
          scope="provider"
          @update="(name, val) => emit('updateProviderField', name, val)"
        />
      </template>
      <p v-if="panel.editingPrimary && editingPrimaryDraft" class="setup-provider-model__semantics">
        {{ t('setup.provider.defaultModelSemantics') }}
      </p>
      <p v-else-if="!panel.editingPrimary" class="setup-provider-profile-model-hint">
        {{ t('setup.provider.profileModelHint') }}
      </p>
      <p
        v-if="panel.editingPrimary && effectiveMaxTokensReadout"
        class="setup-effective-output"
        data-testid="setup-effective-max-tokens"
        aria-live="polite"
      >{{ effectiveMaxTokensReadout }}</p>
    </section>

    <details class="setup-provider-options" :open="panel.providerAdvancedOpen">
      <summary class="control-row control-row--divider">
        {{ t('setup.provider.providerOptions', { provider: selectedProviderLabel }) }}
      </summary>

      <section v-if="endpointFields.length" class="setup-provider-options__group setup-provider-endpoint">
        <div class="setup-provider-options__head">
          <h5>{{ t('setup.provider.endpointTitle', { provider: selectedProviderLabel }) }}</h5>
          <p>{{ t('setup.provider.endpointDesc', { provider: selectedProviderLabel }) }}</p>
        </div>
        <template v-for="field in endpointFields" :key="field.name">
          <SetupField
            :field="field"
            :value="panel.providerFieldValue(field)"
            scope="provider"
            @update="(name, val) => emit('updateProviderField', name, val)"
          />
        </template>
      </section>

      <section v-if="otherProviderFields.length" class="setup-provider-options__group">
        <template v-for="field in otherProviderFields" :key="field.name">
          <SetupField
            :field="field"
            :value="panel.providerFieldValue(field)"
            scope="provider"
            @update="(name, val) => emit('updateProviderField', name, val)"
          />
        </template>
      </section>

      <section v-if="panel.editingPrimary && modelFields.length" class="setup-provider-options__group">
      <label class="control-row">
        <div class="control-row__label-block">
          <span class="control-row__label">{{ t('setup.provider.contextWindowIdentityLabel', { provider: selectedProviderLabel, model: currentModelId || t('setup.provider.contextWindowNoModel') }) }}</span>
          <span class="control-row__desc">{{ t('setup.provider.contextWindowIdentityDesc') }}</span>
        </div>
        <div class="control-row__control setup-context-window">
          <input
            class="control-input control-input--narrow"
            :value="panel.contextWindowTokens"
            name="setup_provider_context_window"
            type="number"
            min="0"
            step="1024"
            inputmode="numeric"
            :placeholder="t('setup.provider.contextWindowAuto')"
            :disabled="!currentModelId"
            @input="emit('updateContextWindow', ($event.target as HTMLInputElement).value)"
          >
          <span class="setup-context-window__readout" aria-live="polite">{{ contextWindowReadout }}</span>
        </div>
      </label>
      <div v-if="showContextWindowWarning" class="setup-warning">
        {{ t('setup.provider.contextWindowLocalWarning', { tokens: contextWindowEffective.value }) }}
      </div>
      </section>
    </details>

    <details v-if="panel.editingPrimary" class="setup-provider-global-settings" :open="panel.providerAdvancedOpen">
      <summary class="control-row control-row--divider">
        {{ t('setup.provider.runtimeDefaultsTitle') }}
      </summary>
      <p class="setup-provider-global-settings__desc">{{ t('setup.provider.runtimeDefaultsDesc') }}</p>
      <label class="control-row">
        <div class="control-row__label-block">
          <span class="control-row__label">{{ t('setup.provider.timeoutLabel') }}</span>
          <span class="control-row__desc">{{ t('setup.provider.timeoutGlobalDesc') }}</span>
        </div>
        <div class="control-row__control">
          <input
            class="control-input control-input--narrow"
            :value="panel.llmTimeoutSeconds"
            name="setup_provider_request_timeout"
            type="number"
            min="1"
            step="1"
            inputmode="numeric"
            @input="emit('updateLlmTimeout', Number(($event.target as HTMLInputElement).value))"
          >
        </div>
      </label>
    </details>

    <div v-if="panel.providerEnvMissing" class="setup-warning">
      <div>{{ t('setup.provider.envMissing', { envKey: panel.providerEnvKey }) }}</div>
      <SetupCommandBlock
        v-if="panel.providerEnvCommand"
        class="setup-warning__command"
        :command="panel.providerEnvCommand"
        :copy-label="t('setup.provider.copyKeyCommand')"
        @copy="emit('copy', $event)"
      />
    </div>
    </template>

    <div
      v-if="panel.configuredProviders.length > 0 && !primaryModelCardVisible"
      class="setup-provider-routing"
      data-testid="provider-model-usage"
    >
      <div class="setup-provider-routing__main">
        <span class="setup-provider-routing__icon" aria-hidden="true">
          <Icon name="router" :size="18" />
        </span>
        <div>
          <strong>{{ t('setup.provider.modelUsageTitle') }}</strong>
          <p>{{ t(modelUsageDescriptionKey) }}</p>
          <div class="setup-provider-routing__states">
            <span class="control-pill control-pill--accent">{{ t(modelUsageModeKey) }}</span>
            <span v-if="panel.crossProviderRoutingEnabled" class="control-pill">
              {{ t('setup.provider.crossProviderActive') }}
            </span>
          </div>
        </div>
      </div>
      <button type="button" class="btn btn--ghost" @click="emit('goToSection', 'modelStrategy')">
        {{ t('setup.provider.configureModelRouting') }}
        <Icon name="chevronRight" :size="15" aria-hidden="true" />
      </button>
    </div>

    </fieldset>

  </section>
</template>

<style scoped>
.control-section {
  container: provider-panel / inline-size;
}

.setup-provider-page__head {
  align-items: flex-start;
  flex-direction: column;
  gap: var(--sp-1);
}

.setup-provider-page__head .control-section__desc {
  flex: none;
}

.setup-provider-interactions {
  border: 0;
  margin: 0;
  min-inline-size: 0;
  padding: 0;
}

.setup-provider-list {
  display: grid;
  gap: var(--sp-2);
  list-style: none;
  margin-block: var(--sp-3);
  padding: 0;
}

.setup-provider-overview {
  align-items: center;
  border-bottom: 1px solid var(--border);
  display: flex;
  gap: var(--sp-3);
  justify-content: space-between;
  padding-bottom: var(--sp-3);
}

.setup-provider-overview__title-row {
  align-items: center;
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
}

.setup-provider-overview h4,
.setup-provider-overview p {
  margin: 0;
}

.setup-provider-overview p {
  color: var(--text-muted);
  font-size: var(--fs-xs);
  margin-top: var(--sp-1);
}

.setup-provider-empty {
  align-items: flex-start;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  display: flex;
  gap: var(--sp-3);
  margin-block: var(--sp-3);
  padding: var(--sp-4);
}

.setup-provider-empty__icon,
.setup-provider-routing__icon {
  align-items: center;
  background: color-mix(in srgb, var(--accent) 10%, transparent);
  border-radius: var(--radius-md);
  color: var(--accent);
  display: inline-flex;
  flex: 0 0 auto;
  justify-content: center;
  padding: var(--sp-2);
}

.setup-provider-empty p {
  color: var(--text-muted);
  font-size: var(--fs-sm);
  margin: var(--sp-1) 0 0;
}

.setup-provider-empty .setup-provider-empty__next {
  color: var(--text-secondary);
  font-size: var(--fs-xs);
  margin-top: var(--sp-2);
}

.setup-provider-card {
  align-items: center;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  display: flex;
  gap: var(--sp-3);
  justify-content: space-between;
  min-height: 54px;
  padding: var(--sp-2) var(--sp-3);
}

.setup-provider-card.is-selected {
  border-color: var(--accent);
  box-shadow: inset 3px 0 0 var(--accent);
}

.setup-provider-card__identity {
  appearance: none;
  align-self: stretch;
  background: transparent;
  border: 0;
  color: inherit;
  cursor: pointer;
  display: grid;
  flex: 1 1 auto;
  font: inherit;
  gap: var(--sp-1);
  min-width: 0;
  padding: 0;
  text-align: left;
}

.setup-provider-card__name-row,
.setup-provider-card__actions {
  align-items: center;
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
}

.setup-provider-card__actions {
  flex: 0 0 auto;
  justify-content: flex-end;
}

.setup-provider-card__activate {
  color: var(--accent);
}

.setup-provider-card__delete {
  color: var(--danger);
}

.setup-provider-card__name {
  color: var(--text);
  font-weight: 650;
}

.setup-provider-card__identity:hover .setup-provider-card__name { color: var(--accent); }
.setup-provider-card__identity:focus-visible {
  border-radius: var(--radius-sm);
  outline: 2px solid var(--accent);
  outline-offset: 3px;
}

.setup-provider-card__status {
  color: var(--text-muted);
  font-size: var(--fs-xs);
}

.setup-provider-card__probe {
  font-size: var(--fs-xs);
}

.setup-provider-card__probe.is-ready { color: var(--ok); }
.setup-provider-card__probe.is-warn { color: var(--danger); }

.setup-provider-card__probe-timing {
  color: var(--text-muted);
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
}

.setup-provider-card__probe-timing--primary {
  color: currentColor;
}

.setup-provider-list__toggle {
  margin-top: calc(var(--sp-2) * -1);
}

.setup-provider-card__status.is-ready {
  color: var(--ok);
}

.setup-provider-card__status.is-warn {
  color: var(--warn);
}

.setup-provider-list__empty {
  color: var(--text-muted);
  font-size: var(--fs-sm);
  margin: 0;
}

.setup-provider-add {
  flex: 0 0 auto;
  position: relative;
}

.setup-provider-add .btn {
  align-items: center;
  display: inline-flex;
  gap: var(--sp-1);
}

.setup-provider-routing {
  align-items: center;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  display: flex;
  gap: var(--sp-3);
  justify-content: space-between;
  margin-block: var(--sp-3);
  padding: var(--sp-3);
}

.setup-provider-routing__main {
  align-items: flex-start;
  display: flex;
  gap: var(--sp-3);
  min-width: 0;
}

.setup-provider-routing > .btn {
  align-items: center;
  display: inline-flex;
  flex: 0 0 auto;
  gap: var(--sp-1);
}

.setup-provider-routing {
  background: transparent;
  border-color: var(--border);
}

.setup-provider-routing p {
  color: var(--text-muted);
  font-size: var(--fs-sm);
  margin: var(--sp-1) 0 0;
}

.setup-provider-routing__states {
  color: var(--text-muted);
  display: flex;
  flex-wrap: wrap;
  font-size: var(--fs-xs);
  gap: var(--sp-2) var(--sp-3);
  margin-top: var(--sp-2);
}

.setup-provider-routing__states .control-pill {
  text-transform: none;
}

.setup-provider-routing p,
.setup-provider-editor-head p {
  color: var(--text-muted);
  font-size: var(--fs-sm);
  margin: var(--sp-1) 0 0;
}

.setup-provider-editor-head {
  align-items: center;
  background: color-mix(in srgb, var(--bg-surface) 94%, transparent);
  border-block: 1px solid var(--border);
  display: flex;
  gap: var(--sp-3);
  justify-content: space-between;
  margin-top: var(--sp-4);
  padding: var(--sp-3) 0;
}

.setup-provider-editor-head__activate {
  flex: 0 0 auto;
}

.setup-provider-editor-head h4 {
  margin: 0;
}

.setup-provider-editor-head__title-row {
  align-items: center;
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
}

.setup-provider-card__editing {
  color: var(--text-muted);
}

.setup-provider-options,
.setup-provider-global-settings {
  border-bottom: 1px solid var(--border);
}

.setup-provider-options__group {
  padding-block: var(--sp-2);
}

.setup-provider-options__group + .setup-provider-options__group {
  border-top: 1px solid var(--border);
}

.setup-provider-options__head {
  margin-bottom: var(--sp-1);
}

.setup-provider-options__head h5 {
  font-size: var(--fs-sm);
  margin: 0;
}

.setup-provider-options__head p,
.setup-provider-global-settings__desc {
  color: var(--text-muted);
  font-size: var(--fs-xs);
  margin: var(--sp-1) 0 var(--sp-2);
}

.setup-provider-model--primary {
  border-bottom: 1px solid var(--border);
  padding-block: var(--sp-3);
}

.setup-provider-model__routing-owner {
  align-items: center;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  display: flex;
  gap: var(--sp-3);
  justify-content: space-between;
  padding: var(--sp-3);
}

.setup-provider-model__routing-value {
  display: grid;
  gap: var(--sp-1);
  min-width: 0;
}

.setup-provider-model__routing-meta {
  align-items: center;
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
}

.setup-provider-model__routing-label {
  color: var(--text-muted);
  font-size: var(--fs-xs);
}

.setup-provider-model__routing-value strong {
  overflow-wrap: anywhere;
}

.setup-provider-model__routing-owner .btn {
  align-items: center;
  display: inline-flex;
  flex: 0 0 auto;
  gap: var(--sp-1);
}

.setup-provider-model__semantics {
  color: var(--text-muted);
  font-size: var(--fs-xs);
  margin: calc(var(--sp-2) * -1) 0 var(--sp-2);
}

.setup-provider-endpoint :deep(.control-row__control) {
  max-width: min(100%, 680px);
  width: min(100%, 680px);
}

.setup-provider-endpoint :deep(.control-input) {
  width: 100%;
}

.setup-effective-output {
  color: var(--text-secondary);
  font-size: var(--fs-sm);
  margin: calc(var(--sp-2) * -1) 0 var(--sp-3);
}

/* The negative top margin above assumes a preceding field/hint whose bottom
   margin absorbs it; the read-only routing card has none, so restore a gap. */
.setup-provider-model__routing-owner + .setup-effective-output {
  margin-top: var(--sp-2);
}

.setup-provider-profile-model-hint {
  color: var(--text-muted);
  font-size: var(--fs-xs);
  margin: calc(var(--sp-2) * -1) 0 var(--sp-2);
}

/* Test-connection row: button + status pill side by side; the pill can wrap
   under the button on narrow widths. */
.setup-connection__actions {
  align-items: center;
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
}

.setup-connection__actions .btn {
  align-items: center;
  display: inline-flex;
  gap: var(--sp-2);
}

.setup-connection__spinner {
  animation: setup-connection-spin var(--dur-pulse) linear infinite;
  border: 2px solid color-mix(in srgb, currentColor 30%, transparent);
  border-radius: var(--radius-full);
  border-top-color: currentColor;
  display: inline-block;
  height: 12px;
  width: 12px;
}

@keyframes setup-connection-spin {
  to { transform: rotate(360deg); }
}

.setup-connection__hint {
  color: var(--text-muted);
  font-size: var(--fs-xs);
}

/* Context-window override: number input with an auto/override/effective
   readout underneath. Tabular numerals keep the readout steady as it updates. */
.setup-context-window {
  align-items: flex-end;
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
}

.setup-context-window__readout {
  color: var(--text-muted);
  font-size: var(--fs-xs);
  font-variant-numeric: tabular-nums;
  text-align: right;
}

@container provider-panel (max-width: 720px) {
  .setup-provider-overview {
    align-items: flex-start;
    flex-direction: column;
  }

  .setup-provider-routing {
    align-items: stretch;
    flex-direction: column;
  }

  .setup-provider-routing > .btn {
    align-self: flex-start;
  }

  .setup-provider-model__routing-owner {
    align-items: stretch;
    flex-direction: column;
  }

  .setup-provider-model__routing-owner .btn {
    align-self: flex-start;
  }

  .setup-provider-editor-head {
    align-items: flex-start;
    flex-direction: column;
  }

  :deep([data-testid="tokenrhythm-recommendation"] ol) {
    grid-template-columns: 1fr;
  }
}

@container provider-panel (max-width: 560px) {
  .setup-provider-card {
    align-items: stretch;
    display: grid;
    grid-template-columns: minmax(0, 1fr);
  }

  .setup-provider-card__actions {
    align-self: start;
    justify-content: flex-start;
  }

  .setup-provider-card__actions .btn {
    flex: 1 1 auto;
  }

}
</style>
