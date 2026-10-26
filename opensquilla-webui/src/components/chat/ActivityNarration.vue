<template>
  <div
    ref="root"
    class="activity-narration activity-narration--plain"
    data-activity-entry="true"
    data-activity-type="segment"
    :data-activity-id="item.key"
    :data-activity-order="item.activityOrder"
    v-html="item.html"
  />
</template>

<script setup lang="ts">
import { ref } from 'vue'
import type { ChatStreamTimelineItem } from '@/types/chat'
import { useMarkdownEnhancements } from '@/composables/chat/useMarkdownEnhancements'

const props = defineProps<{
  item: Extract<ChatStreamTimelineItem, { type: 'text' }>
}>()
const root = ref<HTMLElement | null>(null)
useMarkdownEnhancements(root, { version: () => props.item.html })
</script>

<style scoped>
.activity-narration {
  display: flow-root;
  min-width: 0;
  color: var(--text);
  font-size: var(--fs-md);
  line-height: 1.7;
  overflow-wrap: anywhere;
}

.activity-narration--plain {
  margin: 0;
}

.activity-narration :deep(pre) {
  max-width: 100%;
  margin: 1rem 0;
  overflow-x: auto;
  padding: 0.75rem 0.875rem 0.875rem;
  font-family: var(--font-mono);
  font-size: 0.8125rem;
  line-height: 1.65;
  border: 1px solid var(--code-block-border);
  border-radius: var(--radius-control);
  background: var(--code-block-bg);
  box-shadow: none;
}
.activity-narration :deep(pre code) {
  background: transparent;
  padding: 0;
  color: var(--text);
  font-family: var(--font-mono);
  font-size: 0.8125rem;
}

.activity-narration :deep(> :first-child) {
  margin-top: 0;
}

.activity-narration :deep(> :last-child) {
  margin-bottom: 0;
}
</style>
