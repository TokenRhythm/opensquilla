<template>
  <div
    v-if="available && mode === 'plan'"
    class="composer-plan-mode"
    role="group"
    :aria-label="t('chat.planMode.label')"
    :title="t('chat.planMode.readOnly')"
  >
    <span class="composer-plan-mode__icon" aria-hidden="true">
      <Icon name="listChecks" :size="13" />
    </span>
    <span class="composer-plan-mode__label">{{ t('chat.planMode.label') }}</span>
    <span
      v-if="appliesNextTurn"
      class="composer-plan-mode__next"
      role="status"
      aria-live="polite"
    >
      {{ t('chat.planMode.nextTurn') }}
    </span>
    <button
      type="button"
      class="composer-plan-mode__toggle"
      :disabled="busy || disabled"
      :aria-pressed="mode === 'plan'"
      :aria-label="busy ? t('chat.planMode.updating') : t('chat.planMode.turnOff')"
      :title="busy ? t('chat.planMode.updating') : t('chat.planMode.turnOff')"
      @click="$emit('setMode', mode === 'plan' ? 'default' : 'plan')"
    >
      <span v-if="busy" aria-hidden="true">…</span>
      <Icon v-else name="x" :size="12" aria-hidden="true" />
    </button>
  </div>
</template>

<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import type { CollaborationMode } from '@/types/plans'

defineProps<{
  available: boolean
  mode: CollaborationMode
  busy: boolean
  disabled?: boolean
  appliesNextTurn: boolean
}>()

defineEmits<{
  setMode: [mode: CollaborationMode]
}>()

const { t } = useI18n()
</script>

<style scoped>
.composer-plan-mode {
  display: inline-flex;
  flex: 0 1 auto;
  align-items: center;
  min-width: 0;
  min-height: 28px;
  gap: 6px;
  margin-left: auto;
  padding: 2px 3px 2px 9px;
  border: 1px solid color-mix(in srgb, var(--accent) 26%, var(--border));
  border-radius: var(--radius-full);
  background: color-mix(in srgb, var(--accent) 7%, var(--bg-surface));
  color: var(--text-muted);
}

.composer-plan-mode__icon {
  display: inline-flex;
  flex: none;
  color: var(--accent);
}

.composer-plan-mode__label {
  color: var(--text);
  font-size: var(--fs-xs);
  font-weight: 650;
  white-space: nowrap;
}

.composer-plan-mode__next {
  flex: none;
  min-width: 0;
  overflow: hidden;
  color: var(--accent);
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.composer-plan-mode__toggle {
  display: inline-grid;
  place-items: center;
  flex: none;
  width: 22px;
  height: 22px;
  padding: 0;
  border: 1px solid transparent;
  border-radius: 50%;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  font: inherit;
  font-size: var(--fs-xs);
}

.composer-plan-mode__toggle:hover:not(:disabled),
.composer-plan-mode__toggle:focus-visible {
  border-color: var(--border);
  background: var(--bg-hover);
  color: var(--text);
  outline: none;
}

.composer-plan-mode__toggle:focus-visible {
  box-shadow: var(--focus-ring);
}

.composer-plan-mode__toggle:disabled {
  opacity: var(--state-disabled-opacity);
  cursor: not-allowed;
}

@media (max-width: 640px) {
  .composer-plan-mode {
    max-width: none;
  }

  .composer-plan-mode__next {
    display: none;
  }
}
</style>
