<script setup lang="ts">
import ControlSelect from '@/components/ControlSelect.vue'
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import ControlSwitch from '@/components/ControlSwitch.vue'
import SetupModelCombobox from '@/components/setup/SetupModelCombobox.vue'
import SetupCapabilityCredential from '@/components/setup/SetupCapabilityCredential.vue'
import type { ImageCredentialSource } from '@/composables/setup/useSetupCapabilitiesForm'
import type { DiscoveredModel } from '@/composables/setup/useSetupProviderForm'

const { t } = useI18n()

type CapabilityId = 'search' | 'memory_embedding' | 'image_generation' | 'audio'
type CapabilityGroup = 'search' | 'image' | 'audio'

interface ProviderOption {
  providerId: string
  label: string
  requiresApiKey?: boolean
}

interface CapabilitiesPanelContract {
  form: {
    searchProvider: string
    searchApiKey: string
    imageProvider: string
    imagePrimary: string
    imageApiKey: string
    imageEnabled: boolean
    imageKeyConfigured: boolean
    imageCredentialSource: ImageCredentialSource
    audioApiKey: string
  }
  options: {
    searchProviders: ProviderOption[]
    imageProviders: ProviderOption[]
    imageCredentialOptions: Array<{
      providerId: string
      available: boolean
      source: string
      owner: string
    }>
    imageRecommendation?: {
      providerId: string
      label: string
      canReuseCredential: boolean
      actionRequired: boolean
      registrationUrl: string
    } | null
    imageModels: DiscoveredModel[]
  }
  state: {
    searchRequiresKey: boolean
    searchKeyPlaceholder: string
    searchDraftDirty: boolean
    searchDraftMissingKey: boolean
    searchDraftStatusText: string
    searchStatusText: string
    memoryStatusText: string
    memoryModeTitle: string
    memoryModeDescription: string
    memoryExpandable: boolean
    imageStatusText: string
    imageModelSource: string
    audioStatusText: string
    audioKeyPlaceholder: string
    capabilityBadgeTone: (name: CapabilityId) => string
    capabilityBadgeLabel: (name: CapabilityId) => string
    resettable: (name: CapabilityId) => boolean
    resetPending: CapabilityId | ''
  }
}

const props = defineProps<{
  panel: CapabilitiesPanelContract
}>()

const emit = defineEmits<{
  updateField: [group: CapabilityGroup, key: string, value: string | boolean]
  searchProviderChange: []
  imageProviderChange: [providerId: string]
  useImageRecommendation: [providerId: string]
  resetCapability: [capabilityId: CapabilityId]
}>()

// Enter the capability settings at a quiet overview. Operators can expand
// exactly the capability they intend to edit instead of landing inside the
// first form by default.
const expanded = ref<CapabilityId | ''>('')
const imageKeyEditorOpen = ref(false)
const imageCredentialNeedsInput = computed(() => (
  props.panel.form.imageCredentialSource === 'none'
  || props.panel.form.imageCredentialSource === 'missing_env'
))
const imageCredentialInputVisible = computed(() => (
  imageCredentialNeedsInput.value || imageKeyEditorOpen.value
))
const recommendedImageProvider = computed(() => props.panel.options.imageRecommendation || null)
const configuredImageProviderIds = computed(() => new Set(
  props.panel.options.imageCredentialOptions
    .filter(option => (
      option.available
      && option.source === 'llm_fallback'
      && (option.owner === 'primary' || option.owner === 'profile')
    ))
    .map(option => option.providerId),
))
const configuredImageProviders = computed(() => {
  const recommendedId = recommendedImageProvider.value?.providerId
  return props.panel.options.imageProviders.filter(provider => (
    provider.providerId !== recommendedId
    && configuredImageProviderIds.value.has(provider.providerId)
  ))
})
const otherImageProviders = computed(() => {
  const recommendedId = recommendedImageProvider.value?.providerId
  return props.panel.options.imageProviders.filter(provider => (
    provider.providerId !== recommendedId
    && !configuredImageProviderIds.value.has(provider.providerId)
  ))
})

watch(
  () => [props.panel.form.imageProvider, props.panel.form.imageCredentialSource],
  () => {
    imageKeyEditorOpen.value = false
  },
)

function toggle(capabilityId: CapabilityId) {
  if (capabilityId === 'memory_embedding' && !props.panel.state.memoryExpandable) return
  expanded.value = expanded.value === capabilityId ? '' : capabilityId
}

function onSearchProviderSelect(event: Event) {
  emit('updateField', 'search', 'provider', (event.target as HTMLSelectElement).value)
  emit('searchProviderChange')
}

function onImageProviderSelect(event: Event) {
  emit('imageProviderChange', (event.target as HTMLSelectElement).value)
}

function searchProviderLabel(provider: ProviderOption): string {
  const key = provider.requiresApiKey === true
    ? 'setup.capabilities.searchRequiresKey'
    : 'setup.capabilities.searchNoKey'
  return `${provider.label} · ${t(key)}`
}

function imageProviderLabel(): string {
  return props.panel.options.imageProviders.find(
    provider => provider.providerId === props.panel.form.imageProvider,
  )?.label || props.panel.form.imageProvider
}

function imageCredentialTitle(): string {
  const provider = imageProviderLabel()
  const source = props.panel.form.imageCredentialSource
  if (source === 'account') return t('account.accountSource')
  if (source === 'explicit') return t('setup.capabilities.imageCredentialDirectTitle')
  if (source === 'llm_fallback') {
    return t('setup.capabilities.imageCredentialLlmTitle', { provider })
  }
  if (source === 'env') {
    return t('setup.capabilities.imageCredentialEnvTitle', { provider })
  }
  if (source === 'configured') return t('setup.capabilities.imageCredentialManagedTitle')
  if (source === 'missing_env') {
    return t('setup.capabilities.imageCredentialMissingEnvTitle')
  }
  return t('setup.capabilities.imageCredentialMissingTitle', { provider })
}

function imageCredentialDetail(): string {
  const provider = imageProviderLabel()
  const source = props.panel.form.imageCredentialSource
  if (source === 'account') return t(props.panel.form.imageKeyConfigured ? 'account.status.signed_in' : 'account.modelUnavailable')
  if (source === 'explicit') return t('setup.capabilities.imageCredentialDirectDetail')
  if (source === 'llm_fallback') {
    return t('setup.capabilities.imageCredentialLlmDetail', { provider })
  }
  if (source === 'env') return t('setup.capabilities.imageCredentialEnvDetail')
  if (source === 'configured') return t('setup.capabilities.imageCredentialManagedDetail')
  if (source === 'missing_env') {
    return t('setup.capabilities.imageCredentialMissingEnvDetail')
  }
  return t('setup.capabilities.imageCredentialMissingDetail')
}

function imageCredentialHint(): string {
  if (imageCredentialNeedsInput.value) {
    return t('setup.capabilities.imageCredentialMissingHint')
  }
  if (props.panel.form.imageCredentialSource === 'explicit') {
    return t('setup.capabilities.imageCredentialDedicatedHint')
  }
  return t('setup.capabilities.imageCredentialReuseHint')
}

function imageCredentialActionLabel(): string {
  return props.panel.form.imageCredentialSource === 'explicit'
    ? t('setup.capabilities.imageCredentialReplace')
    : t('setup.capabilities.imageCredentialUseDedicated')
}

function resetLabel(capabilityId: CapabilityId): string {
  if (capabilityId === 'search') return t('setup.capabilities.restoreSearch')
  if (capabilityId === 'memory_embedding') return t('setup.capabilities.restoreMemory')
  return t('setup.capabilities.removeConfiguration')
}
</script>

<template>
  <div class="setup-capabilities">
    <div class="control-section__head">
      <h3 class="control-section__title">{{ t('settings.rail.capabilities') }}</h3>
      <p class="control-section__desc setup-capabilities__intro">
        {{ t('setup.capabilities.intro') }}
      </p>
    </div>

    <section
      v-for="capability in ([
        { id: 'search', title: t('setup.search.title'), status: panel.state.searchStatusText },
        { id: 'memory_embedding', title: t('setup.memory.title'), status: panel.state.memoryStatusText },
        { id: 'image_generation', title: t('setup.image.title'), status: panel.state.imageStatusText },
        { id: 'audio', title: t('setup.audio.title'), status: panel.state.audioStatusText },
      ] as const)"
      :key="capability.id"
      class="capability-card"
      :class="{
        'is-open': expanded === capability.id,
        'is-static': capability.id === 'memory_embedding' && !panel.state.memoryExpandable,
      }"
    >
      <h3 class="capability-card__heading">
        <button
          v-if="capability.id !== 'memory_embedding' || panel.state.memoryExpandable"
          :id="`capability-${capability.id}-trigger`"
          type="button"
          class="capability-card__trigger"
          :aria-expanded="expanded === capability.id ? 'true' : 'false'"
          :aria-controls="`capability-${capability.id}-panel`"
          @click="toggle(capability.id)"
        >
          <span class="capability-card__title">{{ capability.title }}</span>
          <span
            class="control-pill capability-card__status"
            :class="panel.state.capabilityBadgeTone(capability.id)"
          >{{ panel.state.capabilityBadgeLabel(capability.id) }}</span>
          <svg
            class="capability-card__chevron"
            aria-hidden="true"
            viewBox="0 0 20 20"
          >
            <path d="m6 8 4 4 4-4" />
          </svg>
        </button>
        <div v-else class="capability-card__static">
          <span class="capability-card__static-copy">
            <span class="capability-card__title">{{ capability.title }}</span>
            <span class="capability-card__static-summary">
              {{ panel.state.memoryModeTitle }} · {{ panel.state.memoryModeDescription }}
            </span>
          </span>
          <span
            class="control-pill capability-card__status"
            :class="panel.state.capabilityBadgeTone(capability.id)"
          >{{ panel.state.capabilityBadgeLabel(capability.id) }}</span>
        </div>
      </h3>

      <div
        v-if="capability.id !== 'memory_embedding' || panel.state.memoryExpandable"
        v-show="expanded === capability.id"
        :id="`capability-${capability.id}-panel`"
        class="capability-card__panel"
        role="region"
        :aria-labelledby="`capability-${capability.id}-trigger`"
      >
        <p class="capability-card__description">{{ capability.status }}</p>

        <template v-if="capability.id === 'search'">
          <label class="control-row" for="setup-search-provider">
            <div class="control-row__label-block">
              <span class="control-row__label">{{ t('setup.common.provider') }}</span>
              <span class="control-row__desc">{{ t('setup.capabilities.searchProviderHint') }}</span>
            </div>
            <div class="control-row__control">
              <ControlSelect
                class="control-input"
                :value="panel.form.searchProvider"
                id="setup-search-provider"
                name="setup_search_provider"
                :aria-label="t('setup.common.provider')"
                @change="onSearchProviderSelect"
              >
                <option
                  v-for="provider in panel.options.searchProviders"
                  :key="provider.providerId"
                  :value="provider.providerId"
                >{{ searchProviderLabel(provider) }}</option>
              </ControlSelect>
            </div>
          </label>
          <p
            v-if="panel.state.searchDraftStatusText"
            class="capability-card__draft-note"
          >{{ panel.state.searchDraftStatusText }}</p>
          <label v-if="panel.state.searchRequiresKey" class="control-row">
            <div class="control-row__label-block">
              <span class="control-row__label">{{ t('setup.common.apiKey') }}</span>
            </div>
            <div class="control-row__control">
              <input
                class="control-input"
                :value="panel.form.searchApiKey"
                name="setup_search_api_key"
                type="password"
                autocomplete="off"
                data-1p-ignore
                data-bwignore
                data-form-type="other"
                data-lpignore="true"
                data-protonpass-ignore="true"
                :placeholder="panel.state.searchKeyPlaceholder"
                :aria-invalid="panel.state.searchDraftMissingKey ? 'true' : undefined"
                :aria-describedby="panel.state.searchDraftMissingKey
                  ? 'setup-search-api-key-error'
                  : undefined"
                @input="emit('updateField', 'search', 'apiKey', ($event.target as HTMLInputElement).value)"
              >
            </div>
          </label>
          <p
            v-if="panel.state.searchDraftMissingKey"
            id="setup-search-api-key-error"
            class="capability-card__field-error"
            role="alert"
          >{{ t('setup.capabilities.searchKeyRequired') }}</p>
        </template>

        <div v-else-if="capability.id === 'memory_embedding'" class="capability-card__builtin">
          <strong>{{ panel.state.memoryModeTitle }}</strong>
          <span>{{ panel.state.memoryModeDescription }}</span>
        </div>

        <template v-else-if="capability.id === 'image_generation'">
          <div class="control-row">
            <div class="control-row__label-block">
              <span class="control-row__label">
                {{ t('setup.capabilities.imageEnabledTitle') }}
              </span>
              <span class="control-row__desc">
                {{ t('setup.capabilities.imageEnabledHint') }}
              </span>
            </div>
            <div class="control-row__control">
              <ControlSwitch
                name="setup_image_enabled"
                :checked="panel.form.imageEnabled"
                :aria-label="t('setup.capabilities.imageEnabledTitle')"
                @change="emit('updateField', 'image', 'enabled', $event)"
              />
            </div>
          </div>
          <template v-if="panel.form.imageEnabled">
            <label class="control-row" for="setup-image-provider">
            <div class="control-row__label-block">
              <span class="control-row__label">{{ t('setup.common.provider') }}</span>
            </div>
            <div class="control-row__control">
              <ControlSelect
                class="control-input"
                :value="panel.form.imageProvider"
                id="setup-image-provider"
                name="setup_image_provider"
                :aria-label="t('setup.capabilities.imageProviderSelectLabel')"
                @change="onImageProviderSelect"
              >
                <option value="" disabled>{{ t('setup.capabilities.imageProviderPlaceholder') }}</option>
                <optgroup
                  v-if="recommendedImageProvider"
                  :label="t('setup.capabilities.imageRecommendedGroup')"
                >
                  <option :value="recommendedImageProvider.providerId">
                    {{ recommendedImageProvider.label }} · {{ t('setup.capabilities.imageRecommendedBadge') }}
                  </option>
                </optgroup>
                <optgroup
                  v-if="configuredImageProviders.length"
                  :label="t('setup.capabilities.imageConfiguredProvidersGroup')"
                >
                  <option
                    v-for="provider in configuredImageProviders"
                    :key="provider.providerId"
                    :value="provider.providerId"
                  >{{ provider.label }}</option>
                </optgroup>
                <optgroup
                  v-if="otherImageProviders.length"
                  :label="t('setup.capabilities.imageOtherProvidersGroup')"
                >
                  <option
                    v-for="provider in otherImageProviders"
                    :key="provider.providerId"
                    :value="provider.providerId"
                  >{{ provider.label }}</option>
                </optgroup>
              </ControlSelect>
            </div>
          </label>
          <template v-if="panel.form.imageProvider">
            <SetupModelCombobox
              :field="{
                name: 'image_model_identifier',
                label: t('setup.capabilities.imageModelSummary'),
                description: t('setup.capabilities.imageModelHint'),
              }"
              :value="panel.form.imagePrimary"
              :models="panel.options.imageModels"
              :model-source="panel.state.imageModelSource"
              input-class="capability-card__model-input"
              @update="emit('updateField', 'image', 'primary', $event)"
            />
            <div class="control-row">
              <div class="control-row__label-block">
                <span class="control-row__label">
                  {{ t('setup.capabilities.imageCredentialLabel') }}
                </span>
                <span class="control-row__desc">{{ imageCredentialHint() }}</span>
              </div>
              <div class="control-row__control capability-card__credential-control">
                <SetupCapabilityCredential
                  :ready="!imageCredentialNeedsInput"
                  :title="imageCredentialTitle()"
                  :detail="imageCredentialDetail()"
                  :action-label="!imageCredentialNeedsInput && !imageKeyEditorOpen ? imageCredentialActionLabel() : undefined"
                  @action="imageKeyEditorOpen = true"
                />
                <input
                  v-if="imageCredentialInputVisible"
                  class="control-input"
                  :value="panel.form.imageApiKey"
                  name="setup_image_api_key"
                  type="password"
                  :aria-label="t('setup.capabilities.imageCredentialInputLabel')"
                  autocomplete="off"
                  data-1p-ignore
                  data-bwignore
                  data-form-type="other"
                  data-lpignore="true"
                  data-protonpass-ignore="true"
                  :placeholder="panel.form.imageKeyConfigured
                    ? t('setup.capabilities.imageKeyPlaceholderKeep')
                    : t('setup.capabilities.imageKeyPlaceholderNew')"
                  @input="emit('updateField', 'image', 'apiKey', ($event.target as HTMLInputElement).value)"
                >
              </div>
            </div>
          </template>
          </template>
        </template>

        <template v-else>
          <div class="capability-card__summary">
            <span>{{ t('setup.common.provider') }}</span>
            <strong>ElevenLabs</strong>
          </div>
          <label class="control-row">
            <div class="control-row__label-block">
              <span class="control-row__label">{{ t('setup.common.apiKey') }}</span>
            </div>
            <div class="control-row__control">
              <input
                class="control-input"
                :value="panel.form.audioApiKey"
                name="setup_audio_api_key"
                type="password"
                autocomplete="off"
                data-1p-ignore
                data-bwignore
                data-form-type="other"
                data-lpignore="true"
                data-protonpass-ignore="true"
                :placeholder="panel.state.audioKeyPlaceholder"
                @input="emit('updateField', 'audio', 'apiKey', ($event.target as HTMLInputElement).value)"
              >
            </div>
          </label>
        </template>

        <div v-if="panel.state.resettable(capability.id)" class="capability-card__actions">
          <button
            type="button"
            class="btn btn--danger-ghost"
            :disabled="Boolean(panel.state.resetPending)"
            :aria-busy="panel.state.resetPending === capability.id ? 'true' : undefined"
            @click="emit('resetCapability', capability.id)"
          >{{ resetLabel(capability.id) }}</button>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.setup-capabilities {
  display: grid;
  gap: 12px;
}

.setup-capabilities__intro {
  margin: 0 0 4px;
  color: var(--text-muted);
  font-size: 13px;
  line-height: 1.55;
}

.capability-card {
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--bg-surface);
}

.capability-card__heading {
  margin: 0;
}

.capability-card__trigger,
.capability-card__static {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto auto;
  align-items: center;
  width: 100%;
  gap: 12px;
  padding: 17px 18px;
  border: 0;
  color: inherit;
  background: transparent;
  text-align: left;
}

.capability-card__trigger {
  cursor: pointer;
}

.capability-card__trigger:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -3px;
}

.capability-card__title {
  overflow: hidden;
  font-size: 15px;
  font-weight: 650;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.capability-card__static {
  grid-template-columns: minmax(0, 1fr) auto;
}

.capability-card__static-copy {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 4px;
}

.capability-card__static-summary {
  overflow: hidden;
  color: var(--text-muted);
  font-size: 12px;
  font-weight: 400;
  line-height: 1.4;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.capability-card__chevron {
  width: 18px;
  height: 18px;
  flex: 0 0 18px;
  color: var(--text-muted);
  fill: none;
  stroke: currentColor;
  stroke-linecap: round;
  stroke-linejoin: round;
  stroke-width: 1.75;
  transition: transform var(--dur-fast) var(--ease-standard);
}

.capability-card.is-open .capability-card__chevron {
  transform: rotate(180deg);
}

.capability-card__panel {
  padding: 0 18px 16px;
  border-top: 1px solid var(--border);
}

.capability-card__description {
  margin: 14px 0 4px;
  color: var(--text-muted);
  font-size: 13px;
  line-height: 1.5;
}

.capability-card__draft-note {
  margin: 10px 0 0;
  padding: 9px 11px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-elevated);
  color: var(--text-muted);
  font-size: 12px;
  line-height: 1.45;
}

.capability-card__field-error {
  margin: 7px 0 0;
  color: var(--warn);
  font-size: 12px;
  line-height: 1.4;
  text-align: right;
}

.capability-card__model-input {
  width: min(100%, 360px);
  max-width: 360px;
}

.capability-card__credential-control {
  display: grid;
  justify-content: stretch;
  width: min(100%, 360px);
  max-width: 360px;
  gap: 8px;
}

.capability-card__builtin,
.capability-card__summary {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-top: 14px;
  padding: 12px 14px;
  border-radius: var(--radius-md);
  background: var(--bg-elevated);
  color: var(--text-muted);
  font-size: 13px;
}

.capability-card__builtin {
  align-items: flex-start;
  flex-direction: column;
  gap: 4px;
}

.capability-card__builtin strong,
.capability-card__summary strong {
  color: var(--text);
  font-weight: 600;
}

.capability-card__summary strong {
  overflow: hidden;
  text-align: right;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.capability-card__actions {
  display: flex;
  justify-content: flex-end;
  padding-top: 12px;
}

.btn--danger-ghost {
  color: var(--danger);
  background: transparent;
}

@media (prefers-reduced-motion: reduce) {
  .capability-card__chevron {
    transition: none;
  }
}

@media (max-width: 640px) {
  .capability-card__static-summary {
    white-space: normal;
  }

  .capability-card__field-error {
    text-align: left;
  }

}
</style>
