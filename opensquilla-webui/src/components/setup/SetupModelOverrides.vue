<script setup lang="ts">
// Per-model parameter overrides for the primary model: context window, max
// output tokens, and input-modality capability flags. Rendered at the bottom
// of the model combobox dropdown (and inline under the input when no live
// catalog exists). Presentational only: props in, events out.
import { useI18n } from 'vue-i18n'

const { t } = useI18n()

const props = defineProps<{
  contextWindow: string
  maxOutputTokens: string
  supportsVision: boolean
  supportsVideo: boolean
  // Engine fallback hints shown as dim placeholders (already compacted).
  contextWindowHint: string
  maxOutputHint: string
  disabled?: boolean
}>()

const emit = defineEmits<{
  updateContextWindow: [value: string]
  updateMaxOutputTokens: [value: string]
  updateCap: [name: 'vision' | 'video', value: boolean]
}>()

// Keep the popup open when a pointer press lands on the override controls:
// the combobox prevents mousedown default to stop its own input from blurring,
// so the pressed control must be focused explicitly.
function onMousedown(event: MouseEvent) {
  event.preventDefault()
  const target = event.target as HTMLElement | null
  const input = target?.closest?.('input')
  if (input instanceof HTMLInputElement) input.focus()
}
</script>

<template>
  <div class="setup-model-overrides" @mousedown="onMousedown" @keydown.stop>
    <div class="setup-model-overrides__head">
      <span class="setup-model-overrides__title">{{ t('setup.provider.modelOverridesTitle') }}</span>
    </div>
    <div class="setup-model-overrides__inputs">
      <label class="setup-model-overrides__field">
        <span class="setup-model-overrides__field-label">{{ t('setup.provider.contextWindowField') }}</span>
        <input
          class="control-input setup-model-overrides__input"
          name="setup_provider_context_window"
          type="number"
          min="0"
          step="1024"
          inputmode="numeric"
          :value="contextWindow"
          :placeholder="contextWindowHint"
          :aria-label="t('setup.provider.contextWindowField')"
          :disabled="disabled"
          @input="emit('updateContextWindow', ($event.target as HTMLInputElement).value)"
        >
      </label>
      <label class="setup-model-overrides__field">
        <span class="setup-model-overrides__field-label">{{ t('setup.provider.maxOutputField') }}</span>
        <input
          class="control-input setup-model-overrides__input"
          name="setup_provider_max_output_tokens"
          type="number"
          min="0"
          step="1024"
          inputmode="numeric"
          :value="maxOutputTokens"
          :placeholder="maxOutputHint"
          :aria-label="t('setup.provider.maxOutputField')"
          :disabled="disabled"
          @input="emit('updateMaxOutputTokens', ($event.target as HTMLInputElement).value)"
        >
      </label>
    </div>
    <div class="setup-model-overrides__caps">
      <label class="setup-model-overrides__cap">
        <input
          type="checkbox"
          class="control-switch"
          role="switch"
          name="setup_provider_supports_vision"
          :checked="supportsVision"
          :aria-label="t('setup.provider.modelCapVision')"
          :disabled="disabled"
          @change="emit('updateCap', 'vision', ($event.target as HTMLInputElement).checked)"
        >
        <span>{{ t('setup.provider.modelCapVision') }}</span>
      </label>
      <label class="setup-model-overrides__cap">
        <input
          type="checkbox"
          class="control-switch"
          role="switch"
          name="setup_provider_supports_video"
          :checked="supportsVideo"
          :aria-label="t('setup.provider.modelCapVideo')"
          :disabled="disabled"
          @change="emit('updateCap', 'video', ($event.target as HTMLInputElement).checked)"
        >
        <span>{{ t('setup.provider.modelCapVideo') }}</span>
      </label>
    </div>
  </div>
</template>

<style scoped>
.setup-model-overrides {
  border-top: 1px solid var(--border);
  display: grid;
  gap: 8px;
  padding: 10px 12px 12px;
}

.setup-model-overrides__head {
  align-items: center;
  display: flex;
}

.setup-model-overrides__title {
  color: var(--text-dim);
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.02em;
}

.setup-model-overrides__inputs {
  display: grid;
  gap: 8px;
  grid-template-columns: 1fr 1fr;
}

.setup-model-overrides__field {
  display: grid;
  gap: 3px;
  min-width: 0;
}

.setup-model-overrides__field-label {
  color: var(--text-dim);
  font-size: 11px;
}

.setup-model-overrides__input {
  width: 100%;
}

.setup-model-overrides__input::placeholder {
  color: var(--text-dim);
  opacity: 0.75;
}

.setup-model-overrides__caps {
  display: flex;
  gap: 18px;
}

.setup-model-overrides__cap {
  align-items: center;
  cursor: pointer;
  display: inline-flex;
  font-size: 12px;
  gap: 6px;
}
</style>
