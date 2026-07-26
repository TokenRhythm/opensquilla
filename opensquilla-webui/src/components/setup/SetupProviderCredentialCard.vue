<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import type { ConnectionState } from '@/composables/setup/useSetupProviderForm'

const { t } = useI18n()

interface ProviderCredentialPanelContract {
  providerLabel: string
  providerSelected: boolean
  acceptsApiKey: boolean
  requiresApiKey: boolean
  source: string
  available: boolean
  removable: boolean
  removing: boolean
  envKey: string
  masked: string
  revealAllowed: boolean
  revealed: string
  revealError: string
  replacing: boolean
  apiKeyValue: string
  apiKeyEnvValue: string
  draftCredentialSource?: '' | 'key' | 'env'
  probeReady: boolean
  probeDisabledReason: string
  probeButtonLabel: string
  connection: ConnectionState
}

const props = defineProps<{
  panel: ProviderCredentialPanelContract
}>()

const emit = defineEmits<{
  reveal: []
  hideReveal: []
  replace: []
  cancelReplace: []
  removeCredential: []
  testConnection: []
  updateField: [name: string, value: string]
}>()

const showApiKey = ref(false)
const detailsOpen = ref(false)
const draftCredentialSource = computed(() => (
  props.panel.draftCredentialSource
  || (props.panel.apiKeyValue.trim().length > 0 ? 'key' : '')
))

const title = computed(() => t('setup.provider.credentialTitle', { provider: props.panel.providerLabel }))
const statusText = computed(() => {
  if (draftCredentialSource.value === 'key') return t('setup.provider.credentialDraftReady')
  if (draftCredentialSource.value === 'env') return t('setup.provider.credentialEnvDraftReady')
  if (props.panel.source === 'not_required') {
    return props.panel.acceptsApiKey
      ? t('setup.provider.credentialOptional')
      : t('setup.provider.credentialNotRequired')
  }
  return props.panel.available
    ? t('setup.provider.credentialReady')
    : t('setup.provider.credentialNeedsKey')
})
const statusTone = computed(() => {
  if (draftCredentialSource.value) return 'control-pill--warn'
  if (props.panel.source === 'not_required') return ''
  return props.panel.available ? 'control-pill--ok' : 'control-pill--warn'
})
const sourceText = computed(() => {
  if (draftCredentialSource.value === 'key') return t('setup.provider.credentialSourceDraft')
  if (draftCredentialSource.value === 'env') {
    return t('setup.provider.credentialSourceEnvDraft', { envKey: props.panel.apiKeyEnvValue })
  }
  switch (props.panel.source) {
    case 'explicit':
      return t('setup.provider.credentialSourceExplicit')
    case 'env':
      return t('setup.provider.credentialSourceEnv', { envKey: props.panel.envKey })
    case 'missing_env':
      return t('setup.provider.credentialSourceMissingEnv', { envKey: props.panel.envKey })
    case 'not_required':
      return props.panel.acceptsApiKey
        ? t('setup.provider.credentialSourceOptional')
        : t('setup.provider.credentialSourceNotRequired')
    default:
      return t('setup.provider.credentialSourceNone')
  }
})
const displayValue = computed(() => props.panel.revealed || props.panel.masked || '')
const showRevealButton = computed(() => props.panel.revealAllowed && Boolean(props.panel.masked))
const credentialRevealed = computed(() => Boolean(props.panel.revealed))
const credentialToggleLabel = computed(() => (
  credentialRevealed.value ? t('setup.provider.hideApiKey') : t('setup.provider.showApiKey')
))
const writeOnlySavedCredential = computed(() => (
  props.panel.available && !props.panel.revealAllowed && !props.panel.masked
))
const showPublicHint = computed(() => (
  !props.panel.revealAllowed && (Boolean(props.panel.masked) || props.panel.available)
))
const showCredentialControls = computed(() => props.panel.providerSelected && props.panel.acceptsApiKey)
const hasRemovableCredential = computed(() => (
  showCredentialControls.value
  && props.panel.removable
))
const removeCredentialLabel = computed(() => t(
  props.panel.removing
    ? 'setup.provider.removingCredential'
    : 'setup.provider.removeCredential',
))
const removeCredentialAriaLabel = computed(() => (
  `${removeCredentialLabel.value} — ${props.panel.providerLabel}`
))
const apiKeyLabel = computed(() => (
  props.panel.requiresApiKey
    ? t('setup.common.apiKey')
    : t('setup.provider.optionalApiKeyLabel')
))
const apiKeyHelper = computed(() => (
  props.panel.requiresApiKey
    ? ''
    : t('setup.provider.optionalApiKeyHelper')
))
// The masked/readonly display plus the "Replace key" guard only make sense
// when a saved secret actually exists; an empty credential must be directly
// typable (first-run setup would otherwise dead-end on a locked input).
const hasSavedKey = computed(() => Boolean(props.panel.masked))
const editingCredential = computed(() => props.panel.replacing || !hasSavedKey.value)
const credentialInputPlaceholder = computed(() => {
  if (writeOnlySavedCredential.value && !props.panel.replacing) {
    return t('setup.provider.credentialWriteOnlyPlaceholder')
  }
  return props.panel.replacing
    ? t('setup.provider.credentialReplacePlaceholder')
    : t('setup.provider.credentialEnterPlaceholder')
})

// A secret input must always start hidden. The plaintext toggle is local
// state on a card that stays mounted across saves and provider switches, so
// reset it whenever the editable input goes away or the card starts showing
// a different provider's credential — otherwise a toggle made while typing a
// first key would reopen the next secret in plaintext.
watch(editingCredential, editing => {
  if (!editing) showApiKey.value = false
})
watch(() => props.panel.providerLabel, () => { showApiKey.value = false })
const probing = computed(() => props.panel.connection.phase === 'probing')
const probeHintId = computed(() => (
  `setup-provider-probe-hint-${props.panel.providerLabel.toLowerCase().replace(/[^a-z0-9]+/g, '-')}`
))

const FAILURE_SENTENCE_KEYS: Record<string, string> = {
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

function failureSentence(connection: ConnectionState): string {
  const key = FAILURE_SENTENCE_KEYS[connection.failureKind]
  if (key) return t(key)
  if (connection.detail) return connection.detail
  return t('setup.provider.failureGeneric')
}

const connectionPill = computed(() => {
  const connection = props.panel.connection
  if (connection.phase === 'unverified') {
    return { tone: '', text: t('setup.provider.currentSettingsNotTested'), title: '' }
  }
  if (connection.phase === 'verified') {
    return { tone: 'control-pill--ok', text: t('setup.provider.endpointVerified'), title: '' }
  }
  const title = [connection.failureKind, connection.detail].filter(Boolean).join(' — ')
  if (PROTOCOL_FAILURE_KINDS.has(connection.failureKind)) {
    return {
      tone: 'control-pill--warn',
      text: t('setup.provider.streamIncompatible'),
      title,
    }
  }
  if (connection.phase === 'key_invalid') {
    return {
      tone: 'control-pill--danger',
      text: t('setup.provider.keyRejected', { reason: failureSentence(connection) }),
      title,
    }
  }
  if (connection.phase === 'unreachable') {
    return {
      tone: 'control-pill--warn',
      text: t('setup.provider.notReachable', { reason: failureSentence(connection) }),
      title,
    }
  }
  return null
})

function roundedMilliseconds(value: number | null): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0
    ? Math.round(value)
    : null
}

// A model probe has two distinct clocks. First response describes perceived
// model responsiveness; total duration proves the stream also terminated
// correctly. An older gateway supplies only latencyMs, which the composable
// maps to totalMs and is therefore never mislabeled as first response.
const firstResponseText = computed(() => {
  const duration = roundedMilliseconds(props.panel.connection.firstResponseMs)
  return duration == null ? '' : t('setup.provider.firstModelResponse', { duration })
})
const completeProbeText = computed(() => {
  const duration = roundedMilliseconds(props.panel.connection.totalMs)
  return duration == null ? '' : t('setup.provider.completeProbeDuration', { duration })
})
const hasProbeTimings = computed(() => Boolean(firstResponseText.value || completeProbeText.value))
const failureProbeTimings = computed(() => {
  const phase = props.panel.connection.phase
  return phase === 'key_invalid' || phase === 'unreachable'
})

const verdictModelsText = computed(() => {
  const connection = props.panel.connection
  if (connection.phase !== 'verified' || connection.modelSource !== 'live') return ''
  if (connection.models.length === 0) return ''
  const joiner = t('setup.provider.verdictSampleJoiner')
  const samples = connection.models.slice(0, 3).map(model => model.id).join(joiner)
  return t('setup.provider.verdictModels', { count: connection.models.length, samples })
})
</script>

<template>
  <section
    class="setup-provider-credential"
    :aria-busy="panel.removing ? 'true' : undefined"
  >
    <div class="setup-provider-credential__head">
      <div>
        <h4 class="setup-provider-credential__title">{{ title }}</h4>
        <p class="setup-provider-credential__source">{{ sourceText }}</p>
      </div>
      <strong class="control-pill" :class="statusTone">{{ statusText }}</strong>
    </div>

    <div class="setup-provider-credential__body">
      <template v-if="!editingCredential">
        <label v-if="showCredentialControls" class="control-row control-row--stack setup-provider-credential__field">
          <div class="control-row__label-block">
            <span class="control-row__label">{{ apiKeyLabel }}</span>
            <span v-if="apiKeyHelper" class="control-row__desc">{{ apiKeyHelper }}</span>
          </div>
          <div class="control-row__control setup-provider-credential__field-row">
            <div class="setup-provider-credential__input-shell">
              <input
                class="control-input setup-provider-credential__input"
                :value="displayValue"
                name="setup_provider_api_key_display"
                type="text"
                readonly
                :placeholder="t('setup.provider.credentialHiddenPlaceholder')"
              >
              <button
                v-if="showRevealButton"
                type="button"
                class="setup-provider-credential__input-action"
                :aria-label="credentialToggleLabel"
                :title="credentialToggleLabel"
                @click="credentialRevealed ? emit('hideReveal') : emit('reveal')"
              >
                <Icon :name="credentialRevealed ? 'eye-off' : 'eye'" :size="14" />
              </button>
            </div>
            <button
              type="button"
              class="btn setup-provider-credential__replace"
              @click="emit('replace')"
            >{{ t('setup.provider.replaceCredential') }}</button>
            <button
              v-if="hasRemovableCredential"
              type="button"
              class="btn btn--ghost setup-provider-credential__remove"
              :disabled="panel.removing"
              :aria-busy="panel.removing ? 'true' : undefined"
              :aria-label="removeCredentialAriaLabel"
              @click="emit('removeCredential')"
            >
              <span v-if="panel.removing" class="setup-connection__spinner" aria-hidden="true"></span>
              {{ removeCredentialLabel }}
            </button>
          </div>
        </label>
        <p v-if="panel.revealError" class="setup-provider-credential__error">{{ panel.revealError }}</p>
      </template>

      <template v-else>
        <label v-if="showCredentialControls" class="control-row control-row--stack setup-provider-credential__field">
          <div class="control-row__label-block">
            <span class="control-row__label">{{ apiKeyLabel }}</span>
            <span v-if="apiKeyHelper" class="control-row__desc">{{ apiKeyHelper }}</span>
          </div>
          <div class="control-row__control setup-provider-credential__field-row">
            <div class="setup-provider-credential__input-shell">
              <input
                class="control-input setup-provider-credential__input"
                :value="panel.apiKeyValue"
                name="setup_provider_api_key"
                :type="showApiKey ? 'text' : 'password'"
                :placeholder="credentialInputPlaceholder"
                autocomplete="off"
                @input="emit('updateField', 'api_key', ($event.target as HTMLInputElement).value)"
              >
              <button
                type="button"
                class="setup-provider-credential__input-action"
                :aria-label="showApiKey ? t('setup.provider.hideApiKey') : t('setup.provider.showApiKey')"
                :title="showApiKey ? t('setup.provider.hideApiKey') : t('setup.provider.showApiKey')"
                @click="showApiKey = !showApiKey"
              >
                <Icon :name="showApiKey ? 'eye-off' : 'eye'" :size="14" />
              </button>
            </div>
            <button
              v-if="panel.replacing"
              type="button"
              class="btn setup-provider-credential__replace"
              @click="emit('cancelReplace')"
            >{{ t('common.cancel') }}</button>
            <button
              v-else-if="hasRemovableCredential"
              type="button"
              class="btn btn--ghost setup-provider-credential__remove"
              :disabled="panel.removing"
              :aria-busy="panel.removing ? 'true' : undefined"
              :aria-label="removeCredentialAriaLabel"
              @click="emit('removeCredential')"
            >
              <span v-if="panel.removing" class="setup-connection__spinner" aria-hidden="true"></span>
              {{ removeCredentialLabel }}
            </button>
          </div>
        </label>
      </template>
      <p v-if="showPublicHint" class="control-row__desc">{{ t('setup.provider.credentialPublicHint') }}</p>
    </div>

    <div class="setup-provider-credential__footer">
      <div class="control-row__label-block">
        <span class="control-row__label">{{ t('setup.provider.connectionLabel') }}</span>
        <span class="control-row__desc">{{ t('setup.provider.connectionDesc') }}</span>
      </div>
      <div class="control-row__control control-row__control--stack">
        <div class="setup-connection__actions">
          <button
            type="button"
            class="btn"
            :disabled="!panel.providerSelected || !panel.probeReady || probing"
            :title="!panel.probeReady ? panel.probeDisabledReason : undefined"
            :aria-busy="probing ? 'true' : undefined"
            :aria-describedby="panel.probeDisabledReason ? probeHintId : undefined"
            @click="emit('testConnection')"
          >
            <span v-if="probing" class="setup-connection__spinner" aria-hidden="true"></span>
            {{ probing ? t('setup.provider.testing') : (panel.probeButtonLabel || t('setup.provider.testConnection')) }}
          </button>
          <span role="status" aria-live="polite" aria-atomic="true">
            <strong
              v-if="connectionPill"
              class="control-pill"
              :class="connectionPill.tone"
              :title="connectionPill.title || undefined"
            >{{ connectionPill.text }}</strong>
            <template v-if="failureProbeTimings && hasProbeTimings">
              <span v-if="firstResponseText" class="setup-connection__timing setup-connection__timing--primary">· {{ firstResponseText }}</span>
              <span v-if="completeProbeText" class="setup-connection__timing setup-connection__timing--secondary">· {{ completeProbeText }}</span>
            </template>
          </span>
        </div>
        <span v-if="panel.probeDisabledReason" :id="probeHintId" class="setup-connection__hint">{{ panel.probeDisabledReason }}</span>
        <div class="setup-connection__verdict" aria-live="polite">
          <template v-if="panel.connection.phase === 'verified'">
            <span v-if="firstResponseText" class="setup-connection__timing setup-connection__timing--primary">{{ firstResponseText }}</span>
            <span v-if="completeProbeText" class="setup-connection__timing setup-connection__timing--secondary">{{ firstResponseText ? '· ' : '' }}{{ completeProbeText }}</span>
            <span v-if="verdictModelsText" class="setup-connection__verdict-models">· {{ verdictModelsText }}</span>
          </template>
        </div>
        <span
          v-if="panel.connection.phase === 'verified' && panel.connection.discoverError"
          class="setup-connection__hint"
        >{{ t('setup.provider.discoverFailed') }}</span>
      </div>
    </div>

    <details v-if="showCredentialControls" class="setup-provider-credential__details" :open="detailsOpen">
      <summary class="setup-provider-credential__summary" @click.prevent="detailsOpen = !detailsOpen">{{ t('setup.provider.credentialSourceOptions') }}</summary>
      <label v-if="detailsOpen" class="control-row control-row--stack">
        <div class="control-row__label-block">
          <span class="control-row__label">{{ t('setup.common.apiKeyEnv') }}</span>
        </div>
        <div class="control-row__control">
          <input
            class="control-input"
            :value="panel.apiKeyEnvValue"
            name="setup_provider_api_key_env"
            :placeholder="panel.envKey || t('setup.provider.envKeyFallback')"
            @input="emit('updateField', 'api_key_env', ($event.target as HTMLInputElement).value)"
          >
        </div>
      </label>
    </details>
  </section>
</template>

<style scoped>
.setup-provider-credential {
  margin: var(--sp-2) 0;
  padding: var(--sp-2) 0;
  border-block: 1px solid var(--border);
  background: transparent;
}

.setup-provider-credential__head {
  display: flex;
  align-items: flex-start;
  flex-wrap: wrap;
  justify-content: space-between;
  gap: var(--sp-3);
  margin-bottom: var(--sp-1);
}

.setup-provider-credential__title {
  margin: 0;
  font-size: 14px;
  line-height: 1.4;
}

.setup-provider-credential__source {
  margin: 4px 0 0;
  color: var(--text-muted);
  font-size: 13px;
  line-height: 1.4;
}

.setup-provider-credential__head > .control-pill {
  flex: 0 0 auto;
  width: auto;
}

.setup-provider-credential__body,
.setup-provider-credential__footer {
  display: grid;
  gap: var(--sp-1);
}

.setup-provider-credential__footer {
  align-items: flex-start;
  display: flex;
  justify-content: space-between;
  margin-top: var(--sp-1);
}

.setup-provider-credential__footer > .control-row__label-block {
  flex: 1 1 260px;
  min-width: 0;
}

.setup-provider-credential__footer > .control-row__control {
  flex: 0 1 auto;
  min-width: 0;
}

.setup-provider-credential__field {
  padding: 0;
}

.setup-provider-credential__field-row {
  align-items: center;
  display: flex;
  gap: var(--sp-2);
}

.setup-provider-credential__input-shell {
  flex: 1 1 auto;
  min-width: 0;
  position: relative;
}

.setup-provider-credential__input {
  padding-right: 40px;
}

.setup-provider-credential__input-action {
  position: absolute;
  top: 50%;
  right: 10px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  margin: 0;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--text-muted);
  transform: translateY(-50%);
  cursor: pointer;
}

.setup-provider-credential__input-action:hover {
  color: var(--text);
}

.setup-provider-credential__replace {
  flex: 0 0 auto;
  white-space: nowrap;
}

.setup-provider-credential__remove {
  align-items: center;
  color: var(--danger);
  display: inline-flex;
  flex: 0 0 auto;
  gap: var(--sp-1);
  white-space: nowrap;
}

.setup-provider-credential__remove.btn--ghost:not(:disabled):hover {
  background: color-mix(in srgb, var(--danger) 10%, transparent);
  color: var(--danger);
}

.setup-connection__actions {
  align-items: center;
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
  justify-content: flex-end;
}

.setup-connection__actions .btn {
  align-items: center;
  display: inline-flex;
  gap: var(--sp-2);
  white-space: nowrap;
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

/* Verdict line under the pill: probe timings + discovered-model summary. */
.setup-connection__verdict {
  color: var(--text-muted);
  display: flex;
  flex-wrap: wrap;
  font-size: var(--fs-xs);
  gap: var(--sp-1);
  justify-content: flex-end;
}

.setup-connection__timing {
  color: var(--text-muted);
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.setup-connection__timing--primary {
  color: var(--text);
}

.setup-connection__verdict-models {
  min-width: 0;
  overflow-wrap: anywhere;
}

.setup-provider-credential__error {
  margin: 0;
  color: var(--danger);
  font-size: 13px;
  line-height: 1.4;
}

.setup-provider-credential__details {
  margin-top: var(--sp-1);
}

.setup-provider-credential__summary {
  cursor: pointer;
  color: var(--text-muted);
  font-size: 13px;
}

@media (max-width: 520px) {
  .setup-provider-credential__head {
    align-items: flex-start;
    gap: var(--sp-2);
  }

  .setup-provider-credential__head > :first-child {
    flex: 1 1 220px;
    min-width: 0;
  }

  .setup-provider-credential__footer {
    align-items: flex-start;
    flex-direction: column;
    flex-wrap: nowrap;
    gap: var(--sp-2);
  }

  .setup-provider-credential__footer > .control-row__label-block {
    flex: 0 0 auto;
    min-width: 0;
    width: 100%;
  }

  .setup-provider-credential__footer > .control-row__control {
    align-items: flex-start;
    flex: 0 0 auto;
    justify-content: flex-start;
    width: 100%;
  }

  .setup-provider-credential__footer .control-row__desc {
    display: block;
    max-width: 100%;
    overflow-wrap: anywhere;
    white-space: normal;
  }

  .setup-provider-credential__replace {
    width: auto;
  }

  .setup-provider-credential__field-row {
    flex-wrap: wrap;
  }

  .setup-provider-credential__field-row .setup-provider-credential__input-shell {
    flex-basis: 100%;
  }

  .setup-provider-credential__remove {
    width: auto;
  }

  .setup-connection__actions {
    justify-content: flex-start;
  }

  .setup-connection__hint {
    max-width: 100%;
    overflow-wrap: anywhere;
  }
}
</style>
