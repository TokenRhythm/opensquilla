<template>
  <div
    v-if="rows.length"
    class="assistant-unified-activity-timeline"
    data-testid="assistant-unified-activity-timeline"
  >
    <template v-for="row in rows" :key="row.key">
      <AssistantActivityTimeline
        v-if="row.type === 'status'"
        data-activity-entry="true"
        data-activity-type="phase"
        :data-activity-id="row.step.id || row.step.key"
        :data-activity-order="row.order"
        :projection="statusProjection(row.step)"
        :show-items="false"
        :state-scope="stateScope"
        :is-tool-group-open="isToolGroupOpen"
        :is-tool-item-open="isToolItemOpen"
        :tool-group-status-text="toolGroupStatusText"
        :tool-status-text="toolStatusText"
        :tool-secondary-text="toolSecondaryText"
      />
      <ReasoningTimeline
        v-else-if="row.type === 'reasoning'"
        data-activity-entry="true"
        data-activity-type="reasoning"
        :data-activity-id="row.blocks[0]?.id"
        :data-activity-order="row.order"
        :blocks="row.blocks"
        :pace-bursts="reasoningPaceBursts"
        :collapse-active="reasoningCollapseActive"
        :nested="reasoningNested"
        :timeline-phase="reasoningTimelinePhase"
        @reveal-complete="$emit('revealComplete')"
      />
      <AssistantActivityTimeline
        v-else
        :projection="itemProjection"
        :timeline-items="row.items"
        preserve-item-groups
        :variant="variant"
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
      </AssistantActivityTimeline>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import AssistantActivityTimeline from '@/components/chat/AssistantActivityTimeline.vue'
import ReasoningTimeline from '@/components/chat/ReasoningTimeline.vue'
import type {
  ChatStreamTimelineItem,
  ChatToolCallGroup,
  ChatToolCallRenderItem,
  ToolResultContext,
} from '@/types/chat'
import type { ReasoningBlock } from '@/types/turnlog'
import {
  type AssistantActivityStatusStep,
  type AssistantActivityTimelineProjection,
  isVisibleActivityStatusStep,
} from '@/utils/chat/assistantActivity'

const props = withDefaults(defineProps<{
  projection: AssistantActivityTimelineProjection
  timelineItems?: ChatStreamTimelineItem[]
  reasoningBlocks?: ReasoningBlock[]
  isToolGroupOpen: (groupId: string) => boolean
  isToolItemOpen: (renderKey: string) => boolean
  toolGroupStatusText: (group: ChatToolCallGroup) => string
  toolStatusText: (call: ChatToolCallRenderItem) => string
  toolSecondaryText: (call: ChatToolCallRenderItem) => string
  toolElapsedText?: (call: ChatToolCallRenderItem) => string
  variant?: 'checklist'
  stateScope?: string
  reasoningPaceBursts?: boolean
  reasoningCollapseActive?: boolean
  reasoningNested?: boolean
  reasoningTimelinePhase?: boolean
}>(), {
  timelineItems: () => [],
  reasoningBlocks: () => [],
  reasoningPaceBursts: false,
  reasoningCollapseActive: false,
  reasoningNested: true,
  reasoningTimelinePhase: true,
})

defineEmits<{
  toggleGroup: [groupId: string]
  toggleItem: [renderKey: string]
  showResult: [content: string, title: string, context?: ToolResultContext]
  revealComplete: []
}>()

const { t } = useI18n()

defineSlots<{
  interrupt?: (props: {
    part: Extract<import('@/types/parts').ChatPart, { type: 'interrupt' }>
  }) => unknown
}>()

type AtomicRow =
  | {
      type: 'status'
      key: string
      order: number
      step: AssistantActivityStatusStep
    }
  | {
      type: 'reasoning'
      key: string
      order: number
      block: ReasoningBlock
    }
  | {
      type: 'item'
      key: string
      order: number
      item: ChatStreamTimelineItem
    }

type ActivityRow =
  | Omit<Extract<AtomicRow, { type: 'status' }>, 'insertion'>
  | { type: 'reasoning'; key: string; order: number; blocks: ReasoningBlock[] }
  | { type: 'items'; key: string; order: number; items: ChatStreamTimelineItem[] }

function validOrder(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value > 0
}

function itemOrder(item: ChatStreamTimelineItem): number | undefined {
  if (validOrder(item.activityOrder)) return item.activityOrder
  if (item.type !== 'tool-group') return undefined
  if (validOrder(item.group.activityOrder)) return item.group.activityOrder
  const callOrders = item.group.calls
    .map(call => call.activityOrder)
    .filter(validOrder)
  return callOrders.length ? Math.min(...callOrders) : undefined
}

const rows = computed<ActivityRow[]>(() => {
  const statusRows: AtomicRow[] = []
  const reasoningRows: AtomicRow[] = []
  const itemRows: AtomicRow[] = []
  for (const step of props.projection.statusSteps.filter(isVisibleActivityStatusStep)) {
    if (!validOrder(step.activityOrder)) return []
    statusRows.push({
      type: 'status',
      key: step.key,
      order: step.activityOrder,
      step,
    })
  }
  for (const block of props.reasoningBlocks.filter(candidate => candidate.text)) {
    if (!validOrder(block.activityOrder)) return []
    reasoningRows.push({
      type: 'reasoning',
      key: `reasoning:${block.id}`,
      order: block.activityOrder,
      block,
    })
  }
  const clusterByCall = new Map(
    props.projection.activityClusters.flatMap(cluster =>
      cluster.calls.map(call => [call.renderKey, cluster] as const),
    ),
  )
  for (const item of props.timelineItems) {
    if (item.type === 'tool-group') {
      for (const call of item.group.calls) {
        const order = call.activityOrder ?? itemOrder(item)
        if (!validOrder(order)) return []
        const cluster = clusterByCall.get(call.renderKey)
        const isRunning = cluster?.isCurrent ?? call.isRunning
        const singleCallItem: ChatStreamTimelineItem = {
          ...item,
          key: `${item.key}:${call.renderKey}`,
          activityOrder: order,
          group: {
            ...item.group,
            label: cluster
              ? String(t(cluster.purpose.code, cluster.purpose.params))
              : item.group.label,
            secondary: cluster
              ? String(t(cluster.footprint.code, cluster.footprint.params))
              : item.group.secondary,
            calls: [call],
            activityOrder: order,
            isRunning,
            isError: call.isError || call.status === 'error',
            status: call.isError || call.status === 'error'
              ? 'error'
              : call.isRunning
                ? ''
                : call.status,
          },
        }
        itemRows.push({
          type: 'item',
          key: `item:${singleCallItem.key}`,
          order,
          item: singleCallItem,
        })
      }
      continue
    }
    const order = itemOrder(item)
    if (!validOrder(order)) return []
    itemRows.push({
      type: 'item',
      key: `item:${item.key}`,
      order,
      item,
    })
  }

  const sources: AtomicRow[][] = [statusRows, reasoningRows, itemRows]
  if (sources.some(source => source.some(
    (row, index) => index > 0 && row.order < source[index - 1]!.order,
  ))) return []

  // Each source is already monotonic by the authoritative stream order.
  // Merge the three sources in O(n), using source position as the stable
  // same-seq tie break: phase -> reasoning -> segment.
  const cursors = sources.map(() => 0)
  const atomic: AtomicRow[] = []
  while (true) {
    let selected = -1
    for (let sourceIndex = 0; sourceIndex < sources.length; sourceIndex += 1) {
      const candidate = sources[sourceIndex]![cursors[sourceIndex]!]
      if (!candidate) continue
      if (selected < 0) {
        selected = sourceIndex
        continue
      }
      const current = sources[selected]![cursors[selected]!]!
      if (
        candidate.order < current.order
        || (candidate.order === current.order && sourceIndex < selected)
      ) selected = sourceIndex
    }
    if (selected < 0) break
    atomic.push(sources[selected]![cursors[selected]!]!)
    cursors[selected]! += 1
  }

  const result: ActivityRow[] = []
  for (const row of atomic) {
    if (row.type === 'status') {
      result.push({
        type: 'status',
        key: row.key,
        order: row.order,
        step: row.step,
      })
      continue
    }
    const previous = result[result.length - 1]
    if (row.type === 'reasoning') {
      result.push({
        type: 'reasoning',
        key: row.key,
        order: row.order,
        blocks: [row.block],
      })
      continue
    }
    if (previous?.type === 'items') {
      previous.items.push(row.item)
    } else {
      result.push({
        type: 'items',
        key: row.key,
        order: row.order,
        items: [row.item],
      })
    }
  }
  return result
})

const itemProjection = computed<AssistantActivityTimelineProjection>(() => ({
  ...props.projection,
  statusSteps: [],
}))

function statusProjection(
  step: AssistantActivityStatusStep,
): AssistantActivityTimelineProjection {
  return {
    ...props.projection,
    activityClusters: [],
    statusSteps: [step],
  }
}
</script>

<style scoped>
.assistant-unified-activity-timeline {
  min-width: 0;
}
</style>
