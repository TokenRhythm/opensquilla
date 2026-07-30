<template>
  <p v-if="isIdle" class="hub-attention-clear" :aria-label="t('sessions.attention.ariaLabel')">
    <span class="hub-attention-clear__mark" aria-hidden="true">✓</span>
    <span class="hub-attention-clear__text">{{ t('sessions.attention.allClear') }}</span>
    <template v-if="costUsd != null">
      <span class="hub-attention-clear__sep" aria-hidden="true">·</span>
      <button
        type="button"
        class="hub-attention-clear__cost"
        @click="emit('open-usage')"
      >
        {{ t(costPeriod === 'today' ? 'sessions.attention.costToday' : 'sessions.attention.costTotal', { amount: '$' + costUsd.toFixed(2) }) }}
      </button>
    </template>
  </p>
  <section
    v-else
    class="hub-attention control-stat-grid"
    style="--control-stat-min: 180px"
    :aria-label="t('sessions.attention.ariaLabel')"
  >
    <button
      type="button"
      class="control-stat control-stat--clickable hub-attention__tile"
      :class="{ 'control-stat--warn': approvalsCount > 0, 'is-blocked': approvalsCount > 0 }"
      @click="emit('open-approvals')"
    >
      <span class="control-stat__label">{{ t('sessions.attention.needsApproval') }}</span>
      <span class="control-stat__value">{{ approvalsCount }}</span>
      <span class="control-stat__hint">
        {{ approvalsCount > 0 ? t('sessions.attention.openBlocked') : t('sessions.attention.nothingWaiting') }}
      </span>
    </button>
    <div
      class="control-stat control-stat--static hub-attention__tile"
      :class="{ 'control-stat--accent': activeCount > 0, 'is-active': runningCount > 0 }"
    >
      <span class="control-stat__label">{{ t('sessions.attention.active') }}</span>
      <span class="control-stat__value">
        {{ activeCount }}<span v-if="runningCount > 0" class="hub-attention__dot" aria-hidden="true"></span>
      </span>
      <span class="control-stat__hint">
        {{ t('sessions.attention.runningQueued', { running: runningCount, queued: queuedCount }) }}
      </span>
    </div>
    <button
      v-if="costUsd != null"
      type="button"
      class="control-stat control-stat--clickable hub-attention__tile"
      @click="emit('open-usage')"
    >
      <span class="control-stat__label">{{ t(costPeriod === 'today' ? 'sessions.attention.costTodayLabel' : 'sessions.attention.costToDate') }}</span>
      <span class="control-stat__value control-stat__value--mono">${{ costUsd.toFixed(2) }}</span>
      <span class="control-stat__hint">{{ t('sessions.attention.viewUsage') }}</span>
    </button>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

const { t } = useI18n()

const props = defineProps<{
  approvalsCount: number
  runningCount: number
  queuedCount: number
  costUsd: number | null
  costPeriod: 'today' | 'total'
}>()

const emit = defineEmits<{
  'open-approvals': []
  'open-usage': []
}>()

const activeCount = computed(() => props.runningCount + props.queuedCount)
// Fully idle: nothing waiting on the operator and nothing in flight.
const isIdle = computed(() => props.approvalsCount === 0 && activeCount.value === 0)
</script>

<style scoped>
.hub-attention-clear {
  align-items: center;
  color: var(--text-muted);
  display: flex;
  flex-wrap: wrap;
  font-size: var(--fs-sm);
  gap: var(--sp-2);
  margin: 0;
  padding: var(--sp-1);
}

.hub-attention-clear__mark {
  color: var(--ok);
  font-weight: 650;
}

.hub-attention-clear__text {
  color: var(--text);
  font-weight: 600;
}

.hub-attention-clear__sep {
  color: var(--text-dim);
}

.hub-attention-clear__cost {
  background: transparent;
  border: none;
  border-radius: var(--radius-sm);
  color: var(--text-muted);
  cursor: pointer;
  font: inherit;
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
  padding: 0;
  transition: color var(--transition);
}

.hub-attention-clear__cost:hover {
  color: var(--text);
}

.hub-attention-clear__cost:focus-visible {
  box-shadow: var(--focus-ring);
  outline: none;
}

.hub-attention__tile {
  font: inherit;
}

.hub-attention__tile.is-blocked {
  animation: hub-attention-glow 2s ease-in-out infinite;
}

.hub-attention__tile.is-active {
  animation: hub-attention-pulse 2s ease-in-out infinite;
}

@keyframes hub-attention-glow {
  0%, 100% { box-shadow: 0 0 0 0 color-mix(in srgb, var(--warn) 0%, transparent); }
  50% { box-shadow: 0 0 0 3px color-mix(in srgb, var(--warn) 35%, transparent); }
}

@keyframes hub-attention-pulse {
  0%, 100% { box-shadow: 0 0 0 0 color-mix(in srgb, var(--accent) 0%, transparent); }
  50% { box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 30%, transparent); }
}

.hub-attention__dot {
  background: var(--ok);
  border-radius: var(--radius-full);
  display: inline-block;
  height: 8px;
  width: 8px;
  animation: pulse 1.5s ease-in-out infinite;
}

@media (prefers-reduced-motion: reduce) {
  .hub-attention__tile.is-blocked,
  .hub-attention__tile.is-active,
  .hub-attention__dot {
    animation: none;
  }
}
</style>
