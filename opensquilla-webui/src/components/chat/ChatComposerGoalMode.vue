<template>
  <div
    v-if="active"
    class="composer-goal-mode"
    role="group"
    :aria-label="t('chat.goal.modeLabel')"
    :title="t('chat.goal.modeReady')"
  >
    <span class="composer-goal-mode__icon" aria-hidden="true">
      <Icon name="target" :size="13" />
    </span>
    <span class="composer-goal-mode__label">{{ t('chat.goal.modeLabel') }}</span>
    <button
      type="button"
      class="composer-goal-mode__toggle"
      :aria-pressed="active"
      :aria-label="t('chat.goal.turnOffMode')"
      :title="t('chat.goal.turnOffMode')"
      @click="$emit('disarm')"
    >
      <Icon name="x" :size="12" aria-hidden="true" />
    </button>
  </div>
</template>

<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'

defineProps<{
  active: boolean
}>()

defineEmits<{
  disarm: []
}>()

const { t } = useI18n()
</script>

<style scoped>
.composer-goal-mode {
  display: inline-flex;
  flex: 0 1 auto;
  align-items: center;
  min-width: 0;
  min-height: 28px;
  gap: 6px;
  margin-left: auto;
  padding: 2px 3px 2px 9px;
  border: 1px solid var(--border);
  border-radius: var(--radius-full);
  background: transparent;
  color: var(--text-muted);
}

.composer-goal-mode__icon {
  display: inline-flex;
  flex: none;
}

.composer-goal-mode__label {
  color: var(--text);
  font-size: var(--fs-xs);
  font-weight: 650;
  white-space: nowrap;
}

.composer-goal-mode__toggle {
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
}

.composer-goal-mode__toggle:hover,
.composer-goal-mode__toggle:focus-visible {
  background: var(--bg-hover);
  color: var(--text);
}

.composer-goal-mode__toggle:focus-visible {
  outline: 0;
  box-shadow: var(--focus-ring);
}
</style>
