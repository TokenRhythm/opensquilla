<template>
  <div
    class="activity-narration activity-narration--plain"
    data-activity-entry="true"
    data-activity-type="segment"
    :data-activity-id="item.key"
    :data-activity-order="item.activityOrder"
    v-html="item.html"
  />
</template>

<script setup lang="ts">
import type { ChatStreamTimelineItem } from '@/types/chat'

defineProps<{
  item: Extract<ChatStreamTimelineItem, { type: 'text' }>
}>()
</script>

<style scoped>
.activity-narration {
  min-width: 0;
  color: var(--text-muted);
  font-size: 0.8125rem;
  line-height: 1.58;
  overflow-wrap: anywhere;
}

.activity-narration--plain {
  margin: 0.375rem 0 0.625rem 1.625rem;
}

.activity-narration--plain :deep(p) {
  margin: 0;
}

.activity-narration--plain :deep(p + p) {
  margin-top: 0.5rem;
}
</style>
