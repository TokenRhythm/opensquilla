<template>
  <div
    v-if="statusSteps.length || items.length"
    class="assistant-activity-timeline"
  >
    <TransitionGroup
      v-if="statusSteps.length"
      name="activity-step"
      tag="ol"
      class="assistant-activity-status"
    >
      <li
        v-for="step in statusSteps"
        :key="step.key"
        class="assistant-activity-status__row"
        :class="{
          'assistant-activity-status__row--current': step.isCurrent,
          'assistant-activity-status__row--maintenance': step.category === 'maintenance',
          'assistant-activity-status__row--failed': step.state === 'failed',
        }"
        :data-testid="step.category === 'maintenance' ? 'compaction-event' : undefined"
        :data-compaction-id="step.id"
        :data-status="step.state"
        :data-source="step.source"
        :data-durability="step.durability"
      >
        <span class="assistant-activity-status__dot" aria-hidden="true" />
        <span
          :role="step.category === 'maintenance' ? (step.state === 'failed' ? 'alert' : 'status') : undefined"
          :aria-live="step.category === 'maintenance' ? (step.state === 'failed' ? 'assertive' : 'polite') : undefined"
          :aria-atomic="step.category === 'maintenance' ? 'true' : undefined"
        >
          <span>{{ t(step.label.code, step.label.params) }}</span>
          <span
            v-if="step.durationSeconds !== undefined"
            class="assistant-activity-status__duration"
            aria-hidden="true"
          >
            · {{ formatDuration(step.durationSeconds) }}
          </span>
          <span
            v-if="step.category === 'maintenance' && step.durability === 'request_scoped'"
            class="assistant-activity-status__detail"
          >
            {{ t('chat.compact.requestScoped') }}
          </span>
        </span>
      </li>
    </TransitionGroup>
    <template v-for="segment in segments" :key="segment.key">
      <ActivityNarration
        v-if="segment.type === 'narration'"
        :item="segment.item"
      />
      <details
        v-else-if="segment.type === 'tools' && segment.items.length > 1"
        class="assistant-activity-tool-batch"
      >
        <summary class="assistant-activity-tool-batch__summary">
          <Icon
            :name="segment.items[0]?.group.iconName || 'gear'"
            :size="14"
            aria-hidden="true"
          />
          <span class="assistant-activity-tool-batch__label">
            {{ toolBatchSummary(segment.items) }}
          </span>
          <Icon
            class="assistant-activity-tool-batch__chevron"
            name="chevronRight"
            :size="13"
            aria-hidden="true"
          />
        </summary>
        <div class="assistant-activity-tool-batch__body">
          <ToolCallTimeline
            :items="segment.items"
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
      </details>
      <ToolCallTimeline
        v-else
        :items="segment.items"
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
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import ActivityNarration from '@/components/chat/ActivityNarration.vue'
import ToolCallTimeline from '@/components/chat/ToolCallTimeline.vue'
import type {
  ChatStreamTimelineItem,
  ChatToolCallGroup,
  ChatToolCallRenderItem,
  ToolResultContext,
} from '@/types/chat'
import {
  type AssistantActivityTimelineProjection,
  isBeforeReasoningActivityStatusStep,
  isRoutineActivityPhaseStep,
  isVisibleActivityStatusStep,
} from '@/utils/chat/assistantActivity'
import { toolIconName, toolOperationKey } from '@/utils/chat/toolDisplay'

const props = withDefaults(defineProps<{
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
  statusPosition?: 'all' | 'before-reasoning' | 'after-reasoning'
  showItems?: boolean
  preserveItemGroups?: boolean
}>(), {
  statusPosition: 'all',
  showItems: true,
})

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
  const meaningfulSteps = props.projection.statusSteps
    .filter(isVisibleActivityStatusStep)
  const positionedSteps = props.statusPosition === 'before-reasoning'
    ? meaningfulSteps.filter(isBeforeReasoningActivityStatusStep)
    : props.statusPosition === 'after-reasoning'
      ? meaningfulSteps.filter(step => !isBeforeReasoningActivityStatusStep(step))
      : meaningfulSteps
  if (!isLive) return positionedSteps

  // During a live turn, routine phase narration is a single changing row.
  // Completed model phases return once the terminal record is rendered.
  return positionedSteps
    .filter(step => !isRoutineActivityPhaseStep(step) || step.isCurrent)
    .slice(-3)
})

function formatDuration(seconds: number): string {
  const whole = Math.max(0, Math.floor(seconds))
  if (whole < 60) return String(t('chat.activityDurationSeconds', { seconds: whole }))
  return String(t('chat.activityDurationMinutes', {
    minutes: Math.floor(whole / 60),
    seconds: whole % 60,
  }))
}

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
  if (props.showItems === false) return []
  if (props.timelineItems?.length) {
    if (props.preserveItemGroups) return props.timelineItems
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

type ToolTimelineItem = Extract<ChatStreamTimelineItem, { type: 'tool-group' }>
type ActivitySegment =
  | {
      type: 'narration'
      key: string
      item: Extract<ChatStreamTimelineItem, { type: 'text' }>
    }
  | {
      type: 'tools'
      key: string
      items: ToolTimelineItem[]
    }
  | {
      type: 'interrupt'
      key: string
      items: Array<Extract<ChatStreamTimelineItem, { type: 'interrupt' }>>
    }

const segments = computed<ActivitySegment[]>(() => {
  const result: ActivitySegment[] = []
  for (const item of items.value) {
    if (item.type === 'text') {
      result.push({ type: 'narration', key: item.key, item })
      continue
    }
    if (item.type === 'interrupt') {
      result.push({ type: 'interrupt', key: item.key, items: [item] })
      continue
    }
    const last = result[result.length - 1]
    if (last?.type === 'tools') {
      last.items.push(item)
    } else {
      result.push({ type: 'tools', key: `tool-batch:${item.key}`, items: [item] })
    }
  }
  return result
})

function toolBatchSummary(batchItems: ChatStreamTimelineItem[]): string {
  const groups = batchItems.filter(
    (item): item is ToolTimelineItem => item.type === 'tool-group',
  )
  const labels = groups.map(item =>
    [item.group.label, item.group.secondary].filter(Boolean).join(' · '),
  )
  const visible = labels.slice(0, 3)
  if (labels.length > visible.length) {
    const remainingCount = groups
      .slice(visible.length)
      .reduce((total, item) => total + item.group.calls.length, 0)
    visible.push(String(t('chat.activity.more', { count: remainingCount })))
  }
  return visible.join(' · ')
}
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

.activity-step-enter-from {
  opacity: 0;
  transform: translateY(0.25rem);
}

.activity-step-enter-active,
.activity-step-move {
  transition:
    opacity var(--dur-base) var(--ease-out),
    transform var(--dur-base) var(--ease-out);
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

.assistant-activity-status__row--maintenance {
  color: color-mix(in srgb, var(--text) 58%, transparent);
}

.assistant-activity-status__row--maintenance .assistant-activity-status__dot {
  width: 0.5rem;
  height: 0.5rem;
  margin: 0 0.1875rem;
  border: 1px solid currentColor;
  background: transparent;
}

.assistant-activity-status__row--maintenance.assistant-activity-status__row--current
  .assistant-activity-status__dot {
  border-color: var(--accent);
  border-right-color: transparent;
  background: transparent;
  animation: compactionActivitySpin 0.9s linear infinite;
}

.assistant-activity-status__row--failed {
  color: var(--danger);
}

.assistant-activity-status__detail {
  margin-left: auto;
  color: color-mix(in srgb, var(--text) 48%, transparent);
  font-size: 0.75rem;
}

@keyframes compactionActivitySpin {
  to { transform: rotate(360deg); }
}

.assistant-activity-tool-batch {
  min-width: 0;
}

.assistant-activity-tool-batch__summary {
  display: flex;
  align-items: center;
  min-height: 1.75rem;
  gap: 0.625rem;
  padding: 0.25rem 0.125rem;
  color: color-mix(in srgb, var(--text) 76%, transparent);
  cursor: pointer;
  list-style: none;
}

.assistant-activity-tool-batch__summary::-webkit-details-marker {
  display: none;
}

.assistant-activity-tool-batch__summary:hover {
  color: var(--text);
}

.assistant-activity-tool-batch__summary:focus-visible {
  outline: none;
  box-shadow: var(--focus-ring);
}

.assistant-activity-tool-batch__label {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 0.8125rem;
}

.assistant-activity-tool-batch__chevron {
  flex: 0 0 auto;
  margin-left: auto;
  opacity: 0.5;
  transition: transform var(--dur-fast) var(--ease-standard);
}

.assistant-activity-tool-batch[open]
  > .assistant-activity-tool-batch__summary
  .assistant-activity-tool-batch__chevron {
  transform: rotate(90deg);
}

.assistant-activity-tool-batch__body {
  min-width: 0;
  padding-left: 1.5rem;
}

@media (prefers-reduced-motion: reduce) {
  .activity-step-enter-active,
  .activity-step-move {
    transition: none;
  }

  .assistant-activity-tool-batch__chevron {
    transition: none;
  }

  .assistant-activity-status__row--maintenance.assistant-activity-status__row--current
    .assistant-activity-status__dot {
    animation: none;
  }
}
</style>
