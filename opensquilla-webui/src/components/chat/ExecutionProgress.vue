<template>
    <details v-if="progress.steps.length" class="goal-ribbon__progress">
      <summary>{{ progressSummary }}</summary>
      <p v-if="progress.explanation" class="goal-ribbon__explanation">
        {{ progress.explanation }}
      </p>
      <ol class="goal-ribbon__steps">
        <li
          v-for="(step, index) in progress.steps"
          :key="`${index}:${step.text}`"
          :data-status="step.status"
        >
          <span class="goal-ribbon__step-marker" aria-hidden="true">
            {{ step.status === 'completed' ? '✓' : step.status === 'in_progress' ? '●' : '○' }}
          </span>
          <span>{{ step.text }}</span>
        </li>
      </ol>
    </details>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import type { ExecutionProgress } from '@/types/taskProgress'

const props = defineProps<{ progress: ExecutionProgress }>()
const { t } = useI18n()
const progressSummary = computed(() => t('chat.goal.progressSummary', {
  completed: props.progress.steps.filter(step => step.status === 'completed').length,
  total: props.progress.steps.length,
}))
</script>

<style scoped>
.goal-ribbon__progress {
  margin: 6px 0 0 23px;
  color: var(--text-muted, var(--muted));
}
.goal-ribbon__progress summary {
  width: max-content;
  cursor: pointer;
  font-weight: 500;
}
.goal-ribbon__explanation {
  margin: 6px 0 4px;
}
.goal-ribbon__steps {
  display: grid;
  gap: 3px;
  margin: 4px 0 0;
  padding: 0;
  list-style: none;
}
.goal-ribbon__steps li {
  display: flex;
  gap: 6px;
}
.goal-ribbon__steps li[data-status='completed'] {
  color: var(--text-muted, var(--muted));
  text-decoration: line-through;
}
.goal-ribbon__steps li[data-status='in_progress'] .goal-ribbon__step-marker {
  color: var(--accent);
}
.goal-ribbon__step-marker {
  flex: 0 0 1em;
  text-align: center;
}
</style>
