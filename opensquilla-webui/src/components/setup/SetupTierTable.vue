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
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import ControlSwitch from '@/components/ControlSwitch.vue'
import SetupModelCombobox from '@/components/setup/SetupModelCombobox.vue'
import type {
  SetupProviderCredentialStatus,
  SetupProviderOption,
  SetupTierRow,
} from '@/composables/setup/useSetupRouterForm'
import type {
  DiscoveredModelCatalog,
  DiscoveredModelsByProvider,
} from '@/composables/setup/useSetupProviderForm'

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
}>(), {
  disabled: false,
  readonly: false,
  modelsByProvider: () => ({}),
  providerOptions: () => [],
  providerCredentialStatus: () => [],
})

const emit = defineEmits<{
  updateTierField: [name: string, key: 'provider' | 'model' | 'thinkingLevel' | 'supportsImage' | 'ensembleEnabled' | 'ensembleSelectionMode', value: string | boolean]
}>()

const THINKING_LEVELS = ['', 'off', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh']
const ENSEMBLE_CHOICE = '__shared_ensemble__'
const EMPTY_CATALOG: DiscoveredModelCatalog = { models: [], source: 'none' }

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

const c3EnsembleActive = computed(() => props.rows.some(row => (
  row.name === 'c3' && tierEnsembleActive(row)
)))

function rowFieldsDisabled(row: SetupTierRow): boolean {
  return dependentFieldsDisabled(row)
    || (row.name === 'image_model' && c3EnsembleActive.value)
}

function imageSwitchDisabled(row: SetupTierRow): boolean {
  return rowFieldsDisabled(row) || (row.name === 'c3' && tierEnsembleActive(row))
}

function displayedImageSupport(row: SetupTierRow): boolean {
  if (row.name === 'c3' && tierEnsembleActive(row)) return false
  if (row.name === 'image_model' && c3EnsembleActive.value) return false
  return row.supportsImage
}

function modelChoiceValue(row: SetupTierRow): string {
  return row.name === 'c3' && tierEnsembleActive(row) ? ENSEMBLE_CHOICE : row.model
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
  if (props.rows.some(row => credentialFor(row)?.available === false)) return true

  const configuredProviders = new Set(props.providerOptions
    .filter(option => option.disabled !== true)
    .map(option => String(option.providerId || '').trim().toLowerCase())
    .filter(Boolean))

  if (configuredProviders.size !== 1) return true

  const [onlyProvider] = [...configuredProviders]
  return props.rows.some(row => row.provider.trim().toLowerCase() !== onlyProvider)
})

// The combobox dropdown is absolutely positioned; the table's rounded-corner
// overflow clip would cut it off, so overflow opens up only when a combobox
// is actually rendered.
const hasCombobox = computed(() => props.rows.some(row => hasLiveCatalog(row)))
</script>

<template>
  <div
    class="setup-tier-table"
    :class="{
      'setup-tier-table--open': hasCombobox,
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
      :class="{ 'is-disabled': rowFieldsDisabled(tier) }"
      role="row"
      :aria-disabled="rowFieldsDisabled(tier) ? 'true' : undefined"
    >
      <span class="setup-tier-table__tier">{{ tierLabel(tier.name) }}</span>
      <template v-if="showProviderColumn">
        <span v-if="readonly" class="setup-tier-table__readonly" :aria-label="t('setup.router.tierProviderAria', { tier: tier.name })" :title="t('setup.router.tierProviderAria', { tier: tier.name })">{{ tier.provider || '-' }}</span>
        <div v-else class="setup-tier-table__provider-cell">
          <select
            :value="tier.provider.trim().toLowerCase()"
            :aria-label="t('setup.router.tierProviderAria', { tier: tier.name })"
            :aria-invalid="credentialFor(tier) && !credentialFor(tier)?.available ? 'true' : undefined"
            :disabled="rowFieldsDisabled(tier)"
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
          <span class="setup-tier-table__readonly" :aria-label="t('setup.router.tierModelAria', { tier: tier.name })" :title="tier.model || undefined">
            {{ tierEnsembleActive(tier) ? t('setup.router.tierUseEnsemble') : tier.model || '-' }}
          </span>
          <small v-if="tierEnsembleActive(tier)" class="setup-tier-table__model-note">
            {{ t(
              legacyTierEnsembleActive(tier)
                ? 'setup.router.tierLegacyEnsembleSummary'
                : 'setup.router.tierEnsembleSummary',
              { model: tier.model || '-' },
            ) }}
          </small>
          <small v-if="tier.name === 'image_model' && c3EnsembleActive" class="setup-tier-table__model-note">
            {{ t('setup.router.tierEnsembleImageDisabled') }}
          </small>
        </div>
        <span class="setup-tier-table__readonly" :aria-label="t('setup.router.tierThinkingAria', { tier: tier.name })">{{ tier.thinkingLevel || '-' }}</span>
        <ControlSwitch :checked="displayedImageSupport(tier)" :disabled="true" :aria-label="t('setup.router.tierImageAria', { tier: tier.name })" />
      </template>
      <template v-else>
        <div class="setup-tier-table__model-cell">
          <SetupModelCombobox
            cell
            :field="{ name: `tier_${tier.name}_model`, label: t('setup.router.tierModelAria', { tier: tier.name }), placeholder: t('setup.router.tierModelAria', { tier: tier.name }) }"
            :value="modelChoiceValue(tier)"
            :models="catalogFor(tier).models"
            :model-source="catalogFor(tier).source"
            :disabled="rowFieldsDisabled(tier)"
            :leading-option="tier.name === 'c3' ? {
              value: ENSEMBLE_CHOICE,
              label: t('setup.router.tierUseEnsemble'),
              description: t('setup.router.tierUseEnsembleDescription'),
            } : undefined"
            @update="(val) => updateModelChoice(tier, val)"
          />
          <small v-if="tierEnsembleActive(tier)" class="setup-tier-table__model-note">
            {{ t(
              legacyTierEnsembleActive(tier)
                ? 'setup.router.tierLegacyEnsembleSummary'
                : 'setup.router.tierEnsembleSummary',
              { model: tier.model || '-' },
            ) }}
          </small>
          <small v-if="tier.name === 'image_model' && c3EnsembleActive" class="setup-tier-table__model-note">
            {{ t('setup.router.tierEnsembleImageDisabled') }}
          </small>
        </div>
        <select :value="tier.thinkingLevel" :aria-label="t('setup.router.tierThinkingAria', { tier: tier.name })" :disabled="rowFieldsDisabled(tier)" @change="emit('updateTierField', tier.name, 'thinkingLevel', ($event.target as HTMLSelectElement).value)">
          <option v-for="v in THINKING_LEVELS" :key="v" :value="v">{{ v || '-' }}</option>
        </select>
        <ControlSwitch :checked="displayedImageSupport(tier)" :disabled="imageSwitchDisabled(tier)" :aria-label="t('setup.router.tierImageAria', { tier: tier.name })" @change="(v) => emit('updateTierField', tier.name, 'supportsImage', v)" />
      </template>
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
}

.setup-tier-table__model-note {
  color: var(--text-muted);
  font-size: 10px;
  line-height: 1.2;
  overflow-wrap: anywhere;
}

.setup-tier-table__row.is-disabled:not(.is-head) {
  color: var(--text-muted);
}

@media (max-width: 760px) {
  .setup-tier-table--without-provider .setup-tier-table__row {
    min-width: 460px;
  }
}
</style>
