<template>
  <span
    class="execution-todo-marker"
    :class="`execution-todo-marker--${status}`"
    aria-hidden="true"
  >
    <Icon v-if="status === 'completed'" name="check" :size="11" />
    <span v-else-if="status === 'in_progress'" class="execution-todo-marker__dot" />
    <span v-else-if="status === 'blocked'" class="execution-todo-marker__glyph">!</span>
    <span v-else-if="status === 'skipped'" class="execution-todo-marker__glyph">–</span>
  </span>
</template>

<script setup lang="ts">
import Icon from '@/components/Icon.vue'
import type { PlanRunStepStatus } from '@/types/plans'

defineProps<{
  status: PlanRunStepStatus
}>()
</script>

<style scoped>
.execution-todo-marker {
  display: inline-grid;
  width: 18px;
  height: 18px;
  place-items: center;
  flex: 0 0 auto;
  border: 1px solid var(--border-strong);
  border-radius: 50%;
  background: var(--bg-elevated);
  color: var(--text-muted);
  line-height: 1;
  transition:
    border-color var(--dur-fast) var(--ease-standard),
    background var(--dur-fast) var(--ease-standard),
    color var(--dur-fast) var(--ease-standard);
}

.execution-todo-marker--in_progress {
  border-color: color-mix(in srgb, var(--accent) 58%, var(--border));
  background: color-mix(in srgb, var(--accent) 7%, var(--bg-elevated));
  color: var(--accent);
}

.execution-todo-marker--completed {
  border-color: color-mix(in srgb, var(--ok) 54%, var(--border));
  background: color-mix(in srgb, var(--ok) 12%, var(--bg-elevated));
  color: var(--ok);
}

.execution-todo-marker--blocked {
  border-color: color-mix(in srgb, var(--danger) 56%, var(--border));
  background: color-mix(in srgb, var(--danger) 7%, var(--bg-elevated));
  color: var(--danger);
}

.execution-todo-marker--skipped {
  border-color: color-mix(in srgb, var(--text-muted) 40%, var(--border));
  color: var(--text-muted);
}

.execution-todo-marker__dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
}

.execution-todo-marker__glyph {
  font-size: 11px;
  font-weight: 750;
}

@media (prefers-reduced-motion: reduce) {
  .execution-todo-marker {
    transition: none;
  }
}
</style>
