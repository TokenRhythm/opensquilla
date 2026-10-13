<script setup lang="ts">
import Icon from '@/components/Icon.vue'

defineProps<{
  ready: boolean
  title: string
  detail: string
  actionLabel?: string
  actionTestId?: string
  disabled?: boolean
}>()

const emit = defineEmits<{ action: [] }>()
</script>

<template>
  <div class="capability-card__credential-source" :class="{ 'is-missing': !ready }" role="status">
    <span class="capability-card__credential-icon" aria-hidden="true">
      <Icon :name="ready ? 'check' : 'info'" :size="14" />
    </span>
    <span class="capability-card__credential-copy">
      <strong :title="title">{{ title }}</strong>
      <span class="capability-card__credential-detail">{{ detail }}</span>
    </span>
    <button v-if="actionLabel" type="button" class="capability-card__credential-action"
      :disabled="disabled" :data-testid="actionTestId" @click="emit('action')">{{ actionLabel }}</button>
  </div>
</template>

<style scoped>
.capability-card__credential-source {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  gap: 10px;
  padding: 10px 11px;
  border: 1px solid color-mix(in srgb, var(--ok) 32%, var(--border));
  border-radius: var(--radius-md);
  background: color-mix(in srgb, var(--ok) 7%, var(--bg-elevated));
}

.capability-card__credential-source.is-missing {
  border-color: color-mix(in srgb, var(--warn) 36%, var(--border));
  background: color-mix(in srgb, var(--warn) 7%, var(--bg-elevated));
}

.capability-card__credential-icon {
  display: grid;
  width: 24px;
  height: 24px;
  place-items: center;
  border-radius: 50%;
  color: var(--ok);
  background: color-mix(in srgb, var(--ok) 15%, transparent);
}

.capability-card__credential-source.is-missing .capability-card__credential-icon {
  color: var(--warn);
  background: color-mix(in srgb, var(--warn) 15%, transparent);
}

.capability-card__credential-copy {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 2px;
}

.capability-card__credential-copy strong {
  overflow: hidden;
  color: var(--text);
  font-size: 12px;
  font-weight: 600;
  line-height: 1.35;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.capability-card__credential-copy span {
  overflow-wrap: anywhere;
  color: var(--text-muted);
  font-size: 11px;
  line-height: 1.4;
}

.capability-card__credential-action {
  padding: 4px 0 4px 8px;
  border: 0;
  color: var(--accent);
  background: transparent;
  font: inherit;
  font-size: 12px;
  font-weight: 600;
  white-space: nowrap;
  cursor: pointer;
}

.capability-card__credential-action:hover {
  color: var(--accent-hover);
}

.capability-card__credential-action:disabled {
  color: var(--text-muted);
  cursor: default;
}

.capability-card__credential-action:focus-visible {
  border-radius: var(--radius-xs);
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

@media (max-width: 640px) {
  .capability-card__credential-source {
    grid-template-columns: auto minmax(0, 1fr);
  }

  .capability-card__credential-action {
    grid-column: 2;
    justify-self: start;
    padding-left: 0;
  }
}
</style>
