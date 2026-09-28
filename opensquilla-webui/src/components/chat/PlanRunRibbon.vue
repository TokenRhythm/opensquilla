<template>
  <ExecutionProgressRibbon
    :run="run"
    :default-open="defaultOpen"
    :cancel-busy="cancelBusy"
    :disabled="disabled"
    @cancel="$emit('cancel')"
    @focus-return="$emit('focusReturn')"
  />
</template>

<script setup lang="ts">
import ExecutionProgressRibbon from './ExecutionProgressRibbon.vue'
import type { PlanRunSnapshot } from '@/types/plans'

defineProps<{
  run: PlanRunSnapshot
  defaultOpen?: boolean
  cancelBusy?: boolean
  disabled?: boolean
}>()
defineEmits<{ cancel: []; focusReturn: [] }>()
</script>
