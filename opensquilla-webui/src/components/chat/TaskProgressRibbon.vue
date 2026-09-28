<template>
  <ExecutionProgressRibbon
    :run="execution"
    kind="task"
    :explanation="progress.explanation"
    :cancel-busy="cancelBusy"
    :disabled="disabled"
    @cancel="$emit('cancel')"
    @focus-return="$emit('focusReturn')"
  />
</template>

<script setup lang="ts">
import { computed } from 'vue'
import ExecutionProgressRibbon from './ExecutionProgressRibbon.vue'
import type { ExecutionProgress } from '@/types/taskProgress'

const props = defineProps<{
  taskId: string
  progress: ExecutionProgress
  cancelBusy?: boolean
  disabled?: boolean
}>()
defineEmits<{ cancel: []; focusReturn: [] }>()

// A view of the active task, without creating a Plan revision or PlanRun.
const execution = computed(() => {
  const steps = props.progress.steps.map((step, index) => ({
    stepId: `${index}:${step.text}`,
    title: step.text,
    status: step.status,
  }))
  return {
    runId: props.taskId,
    status: 'running' as const,
    currentStepId: steps.find(step => step.status === 'in_progress')?.stepId,
    steps,
  }
})
</script>
