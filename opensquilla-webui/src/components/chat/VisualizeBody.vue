<template>
  <template v-for="segment in segments" :key="segment.key">
    <VisualizeCanvas v-if="segment.receipt" :receipt="segment.receipt" :session-key="sessionKey" />
    <div v-else v-html="segment.html" />
  </template>
</template>
<script setup lang="ts">
import { computed } from 'vue'
import { splitVisualizeBody } from '@/visualize/receipts'
import type { VisualizeReceipt } from '@/visualize/types'
import VisualizeCanvas from './VisualizeCanvas.vue'
const props = withDefaults(defineProps<{ html: string; receipts?: VisualizeReceipt[]; sessionKey?: string; fallback?: boolean }>(), { receipts: () => [], sessionKey: '', fallback: true })
const segments = computed(() => splitVisualizeBody(props.html, props.receipts, props.fallback))
</script>
