<template>
  <div
    v-if="statusSteps.length || items.length"
    class="assistant-activity-timeline"
  >
    <ol v-if="statusSteps.length" class="assistant-activity-status">
      <li
        v-for="step in statusSteps"
        :key="step.key"
        class="assistant-activity-status__row"
        :class="{ 'assistant-activity-status__row--current': step.isCurrent }"
      >
        <span class="assistant-activity-status__dot" aria-hidden="true" />
        <span>{{ t(step.label.code, step.label.params) }}</span>
      </li>
    </ol>
    <ToolCallTimeline
      v-if="items.length"
      :items="items"
      :variant="variant"
      presentation="activity"
      :state-scope="stateScope"
      :is-tool-group-open="isToolGroupOpen"
      :is-tool-item-open="isToolItemOpen"
      :tool-group-status-text="toolGroupStatusText"
      :tool-status-text="toolStatusText"
      :tool-secondary-text="toolSecondaryText"
      :tool-elapsed-text="toolElapsedText"
      @toggle-group="$emit('toggleGroup', $event)"
      @toggle-item="$emit('toggleItem', $event)"
      @show-result="(content, title, context) => $emit('showResult', content, title, context)"
    >
      <template #interrupt="{ part }">
        <slot name="interrupt" :part="part" />
      </template>
    </ToolCallTimeline>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import ToolCallTimeline from '@/components/chat/ToolCallTimeline.vue'
import type {
  ChatStreamTimelineItem,
  ChatToolCallGroup,
  ChatToolCallRenderItem,
  ToolResultContext,
} from '@/types/chat'
import {
  type AssistantActivityTimelineProjection,
  isSemanticActivityStatusStep,
} from '@/utils/chat/assistantActivity'
import { toolIconName, toolOperationKey } from '@/utils/chat/toolDisplay'

const props = defineProps<{
  projection: AssistantActivityTimelineProjection
  timelineItems?: ChatStreamTimelineItem[]
  isToolGroupOpen: (groupId: string) => boolean
  isToolItemOpen: (renderKey: string) => boolean
  toolGroupStatusText: (group: ChatToolCallGroup) => string
  toolStatusText: (call: ChatToolCallRenderItem) => string
  toolSecondaryText: (call: ChatToolCallRenderItem) => string
  toolElapsedText?: (call: ChatToolCallRenderItem) => string
  variant?: 'checklist'
  stateScope?: string
}>()

defineEmits<{
  toggleGroup: [groupId: string]
  toggleItem: [renderKey: string]
  showResult: [content: string, title: string, context?: ToolResultContext]
}>()

defineSlots<{
  interrupt?: (props: {
    part: Extract<import('@/types/parts').ChatPart, { type: 'interrupt' }>
  }) => unknown
}>()

const { t } = useI18n()
const statusSteps = computed(() => {
  const isLive = props.projection.lifecycle === 'working'
    || props.projection.lifecycle === 'answering'
  if (!isLive) return props.projection.statusSteps

  // The live header owns the current lifecycle phase. Repeating that phase in
  // the body creates pairs such as "Working / Working" and makes transport
  // phases look like meaningful agent actions. The shared predicate keeps this
  // body filter and the header's step count agreeing by construction;
  // completed/history playback can still show the full phase record when the
  // user expands it.
  return props.projection.statusSteps
    .filter(isSemanticActivityStatusStep)
    .slice(-3)
})

function clusterItem(
  cluster: AssistantActivityTimelineProjection['activityClusters'][number],
): Extract<ChatStreamTimelineItem, { type: 'tool-group' }> | null {
  const first = cluster.calls[0]
  if (!first) return null
  const group: ChatToolCallGroup = {
    groupId: cluster.key,
    operationKey: toolOperationKey(first.name),
    label: String(t(cluster.purpose.code, cluster.purpose.params)),
    iconName: toolIconName(first.name),
    calls: cluster.calls,
    secondary: String(t(cluster.footprint.code, cluster.footprint.params)),
    isRunning: cluster.isCurrent,
    isError: cluster.isFailure,
    status: cluster.isFailure
      ? 'error'
      : cluster.state === 'complete'
        ? 'success'
        : '',
  }
  return {
    type: 'tool-group',
    key: cluster.key,
    group,
  }
}

const items = computed<ChatStreamTimelineItem[]>(() => {
  if (props.timelineItems?.length) {
    const clusterByCall = new Map(
      props.projection.activityClusters.flatMap(cluster =>
        cluster.calls.map(call => [call.renderKey, cluster] as const),
      ),
    )
    const emitted = new Set<string>()
    const result: ChatStreamTimelineItem[] = []

    for (const item of props.timelineItems) {
      if (item.type === 'text' || item.type === 'interrupt') {
        result.push(item)
        continue
      }
      for (const call of item.group.calls) {
        const cluster = clusterByCall.get(call.renderKey)
        if (!cluster || emitted.has(cluster.key)) continue
        const projected = clusterItem(cluster)
        if (projected) result.push(projected)
        emitted.add(cluster.key)
      }
    }
    return result
  }

  return props.projection.activityClusters.flatMap(cluster => clusterItem(cluster) ?? [])
})
</script>

<style scoped>
.assistant-activity-timeline {
  min-width: 0;
}

.assistant-activity-status {
  display: grid;
  gap: 0;
  margin: 0;
  padding: 0;
  list-style: none;
}

.assistant-activity-status__row {
  display: flex;
  align-items: center;
  min-height: 1.75rem;
  gap: 0.625rem;
  padding: 0.25rem 0.125rem;
  color: color-mix(in srgb, var(--text) 62%, transparent);
  font-size: 0.8125rem;
  line-height: 1.45;
}

.assistant-activity-status__row--current {
  color: color-mix(in srgb, var(--text) 82%, transparent);
}

/* The dot centers in the same 0.875rem marker column the tool-row icons use,
   so phase text and tool-row labels share one left origin (1.625rem). */
.assistant-activity-status__dot {
  width: 0.375rem;
  height: 0.375rem;
  flex: 0 0 auto;
  margin: 0 0.25rem;
  border-radius: var(--radius-full);
  background: currentColor;
}

.assistant-activity-status__row--current .assistant-activity-status__dot {
  background: var(--accent);
}
</style>
