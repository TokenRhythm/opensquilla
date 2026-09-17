<template>
  <details class="status-history">
    <summary class="status-history__summary">
      <Icon class="status-history__chevron" name="chevronRight" :size="12" />
      <span>{{ t('chat.activitySteps', { count: entries.length }) }}</span>
    </summary>
    <ol class="status-history__list">
      <li v-for="(entry, i) in entries" :key="`${entry.action}:${entry.at}:${i}`" class="status-history__row">
        <span class="status-history__dot" aria-hidden="true" />
        <span class="status-history__label">{{ entry.label }}</span>
        <span v-if="gapText(i)" class="status-history__gap">{{ gapText(i) }}</span>
      </li>
    </ol>
  </details>
</template>

<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import type { StatusPart } from '@/types/parts'

const { t } = useI18n()

const props = defineProps<{
  entries: StatusPart[]
}>()

// Time the agent spent in each phase = gap to the next transition. The last
// phase has no successor, so it shows no duration (the turn ended there).
function gapText(i: number): string {
  const next = props.entries[i + 1]
  if (!next) return ''
  const ms = next.at - props.entries[i].at
  if (ms < 0) return ''
  const s = ms / 1000
  if (s < 1) return `${ms}ms`
  if (s < 60) return `${+s.toFixed(1)}s`
  const m = Math.floor(s / 60)
  return `${m}m ${Math.round(s % 60)}s`
}
</script>

<style scoped>
.status-history {
  margin: 0.5rem 0;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: color-mix(in srgb, var(--bg-surface) 55%, transparent);
}
.status-history__summary {
  display: flex;
  align-items: center;
  gap: 0.375rem;
  padding: 0.4375rem 0.625rem;
  font-size: 0.75rem;
  color: var(--text-muted);
  cursor: pointer;
  list-style: none;
}
.status-history__summary::-webkit-details-marker { display: none; }
.status-history__summary:focus-visible {
  outline: none;
  box-shadow: var(--focus-ring-inset);
  border-radius: var(--radius-md);
}
.status-history__chevron { transition: transform var(--dur-fast) var(--ease-standard); flex-shrink: 0; color: var(--text-dim); }
.status-history[open] .status-history__chevron { transform: rotate(90deg); }
.status-history__list {
  margin: 0;
  padding: 0.125rem 0.625rem 0.5rem 0.75rem;
  list-style: none;
}
.status-history__row {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.1875rem 0;
  font-size: 0.75rem;
  color: var(--text-muted);
}
.status-history__dot {
  width: 0.375rem;
  height: 0.375rem;
  border-radius: var(--radius-full);
  background: var(--text-dim);
  flex-shrink: 0;
}
.status-history__label { color: var(--text); }
.status-history__gap {
  margin-left: auto;
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
  font-size: 0.6875rem;
  color: var(--text-dim);
}
</style>
