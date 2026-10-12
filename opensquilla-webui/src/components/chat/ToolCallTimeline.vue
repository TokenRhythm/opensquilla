<template>
  <RunTrace
    :items="items"
    :executing="executing"
    :variant="variant"
    :presentation="presentation"
    :state-scope="stateScope"
    :session-key="sessionKey"
    :show-bulk-toggle="presentation !== 'activity'"
    :is-tool-group-open="isToolGroupOpen"
    :is-tool-item-open="isToolItemOpen"
    :tool-group-status-text="toolGroupStatusText"
    :tool-status-text="toolStatusText"
    :tool-secondary-text="toolSecondaryText"
    :tool-elapsed-text="toolElapsedText"
    @toggle-group="(id, open) => $emit('toggleGroup', id, open)"
    @toggle-item="(id, open) => $emit('toggleItem', id, open)"
    @open-change="$emit('openChange', $event)"
    @show-result="(content, title, context) => $emit('showResult', content, title, context)"
  >
    <template #interrupt="{ part }">
      <slot name="interrupt" :part="part" />
    </template>
  </RunTrace>
</template>

<script setup lang="ts">
import RunTrace from '@/components/run/RunTrace.vue'
import type {
  ChatStreamTimelineItem,
  ChatToolCallGroup,
  ChatToolCallRenderItem,
  ToolResultContext,
} from '@/types/chat'

withDefaults(defineProps<{
  items: ChatStreamTimelineItem[]
  isToolGroupOpen: (groupId: string) => boolean | undefined
  isToolItemOpen: (renderKey: string) => boolean | undefined
  toolGroupStatusText: (group: ChatToolCallGroup) => string
  toolStatusText: (call: ChatToolCallRenderItem) => string
  toolSecondaryText: (call: ChatToolCallRenderItem) => string
  toolElapsedText?: (call: ChatToolCallRenderItem) => string
  variant?: 'checklist'
  presentation?: 'activity'
  stateScope?: string
  sessionKey?: string
  executing?: boolean
}>(), {
  executing: false,
})

defineEmits<{
  toggleGroup: [groupId: string, open: boolean]
  toggleItem: [renderKey: string, open: boolean]
  openChange: [open: boolean]
  showResult: [content: string, title: string, context?: ToolResultContext]
}>()

defineSlots<{
  interrupt?: (props: {
    part: Extract<import('@/types/parts').ChatPart, { type: 'interrupt' }>
  }) => unknown
}>()
</script>
