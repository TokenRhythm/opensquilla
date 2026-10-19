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
          'assistant-activity-status__row--routine': isRoutineActivityPhaseStep(step),
          'assistant-activity-status__row--animating': step.isCurrent && executing && activityVisible && step.providerRecovery?.phase !== 'retry_wait',
          'assistant-activity-status__row--maintenance': step.category === 'maintenance',
          'assistant-activity-status__row--failed': isFailure(step),
        }"
        :data-testid="step.category === 'maintenance' ? 'compaction-event' : undefined"
        :data-compaction-id="step.id"
        :data-status="step.state"
        :data-source="step.source"
        :data-durability="step.durability"
      >
        <CompactionRecoveryMarker v-if="isTemporaryRecovery(step)" />
        <span v-else-if="!isRoutineActivityPhaseStep(step)" class="assistant-activity-status__dot" aria-hidden="true" />
        <ActivityMotion
          class="assistant-activity-status__content"
          :active="isRoutineActivityPhaseStep(step) && step.isCurrent && executing && activityVisible"
          :role="step.category === 'maintenance' ? (isFailure(step) ? 'alert' : 'status') : undefined"
          :aria-live="step.category === 'maintenance' ? (isFailure(step) ? 'assertive' : 'polite') : undefined"
          :aria-atomic="step.category === 'maintenance' ? 'true' : undefined"
        >
          <span :aria-hidden="step.providerRecovery?.phase === 'retry_wait' ? 'true' : undefined">{{ statusText(step) }}</span>
          <span v-if="step.providerRecovery?.phase === 'retry_wait'" class="sr-only">{{ statusText(step, true) }}</span>
          <span v-if="step.category !== 'maintenance' && step.httpStatus && (step.providerRecovery?.phase === 'retry_wait' || step.state === 'failed')" class="assistant-activity-status__detail">{{ t('chat.httpStatus', { status: step.httpStatus }) }}</span>
          <span
            v-if="step.category !== 'maintenance' && !isRoutineActivityPhaseStep(step) && step.durationSeconds !== undefined"
            class="assistant-activity-status__duration"
            aria-hidden="true"
          >
            · {{ formatDuration(step.durationSeconds) }}
          </span>
        </ActivityMotion>
      </li>
    </TransitionGroup>
    <template v-for="segment in renderSegments()" :key="segment.key">
      <ActivityNarration
        v-if="segment.type === 'narration'"
        :item="segment.item"
      />
      <ToolCallTimeline
        v-else
        :items="segment.items"
        :executing="executing"
        :variant="variant"
        presentation="activity"
        :state-scope="stateScope"
        :session-key="sessionKey"
        :is-tool-group-open="isToolGroupOpen"
        :is-tool-item-open="isToolItemOpen"
        :tool-group-status-text="toolGroupStatusText"
        :tool-status-text="toolStatusText"
        :tool-secondary-text="toolSecondaryText"
        :tool-elapsed-text="toolElapsedText"
        @toggle-group="(id, open) => $emit('toggleGroup', id, open)"
        @toggle-item="(id, open) => $emit('toggleItem', id, open)"
        @show-result="(content, title, context) => $emit('showResult', content, title, context)"
      >
        <template #interrupt>
          <InterruptSlotContent v-if="segment.type === 'interrupt'" :nodes="segment.content" />
        </template>
      </ToolCallTimeline>
    </template>
  </div>
</template>

<script setup lang="ts">
import { Comment, Fragment, Text, computed, isVNode, type VNode } from 'vue'
import { useI18n } from 'vue-i18n'
import { chatErrorPresentation } from '@/utils/chat/chatErrorPresentation'
import { useProviderRecoveryClock } from '@/composables/chat/useProviderRecoveryClock'
import ActivityMotion from '@/components/chat/ActivityMotion.vue'
import CompactionRecoveryMarker from './CompactionRecoveryMarker.vue'
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
  type AssistantActivityStatusStep,
  providerActivityLabel,
  isBeforeReasoningActivityStatusStep,
  isRoutineActivityPhaseStep,
  isVisibleActivityStatusStep,
} from '@/utils/chat/assistantActivity'
import { toolIconName, toolOperationKey } from '@/utils/chat/toolDisplay'
import { useActivityDisclosureVisibility } from '@/composables/chat/useActivityDisclosureVisibility'

const props = withDefaults(defineProps<{
  projection: AssistantActivityTimelineProjection
  timelineItems?: ChatStreamTimelineItem[]
  isToolGroupOpen: (groupId: string) => boolean | undefined
  isToolItemOpen: (renderKey: string) => boolean | undefined
  toolGroupStatusText: (group: ChatToolCallGroup) => string
  toolStatusText: (call: ChatToolCallRenderItem) => string
  toolSecondaryText: (call: ChatToolCallRenderItem) => string
  toolElapsedText?: (call: ChatToolCallRenderItem) => string
  variant?: 'checklist'
  stateScope?: string
  sessionKey?: string
  statusPosition?: 'all' | 'before-reasoning' | 'after-reasoning'
  showItems?: boolean
  executing?: boolean
}>(), {
  statusPosition: 'all',
  showItems: true,
  executing: false,
})

defineEmits<{
  toggleGroup: [groupId: string, open: boolean]
  toggleItem: [renderKey: string, open: boolean]
  showResult: [content: string, title: string, context?: ToolResultContext]
}>()

const slots = defineSlots<{
  interrupt?: (props: {
    part: Extract<import('@/types/parts').ChatPart, { type: 'interrupt' }>
  }) => VNode[]
}>()

const { t } = useI18n()
const activityVisible = useActivityDisclosureVisibility()
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

  // The shared visibility predicate keeps only the current routine phase.
  // Terminal history retains actual content and exceptional transitions.
  return positionedSteps.slice(-3)
})

const now = useProviderRecoveryClock(() => activityVisible.value && statusSteps.value.some(step => step.isCurrent && step.providerRecovery?.phase === 'retry_wait'))
function isFailure(step: AssistantActivityStatusStep): boolean {
  return step.state === 'failed' && (step.category !== 'maintenance' || step.source === 'manual')
}
function isTemporaryRecovery(step: AssistantActivityStatusStep): boolean {
  return step.category === 'maintenance' && step.state === 'completed' && step.durability === 'request_scoped'
}
function statusText(step: AssistantActivityStatusStep, announce = false): string {
  if (isTemporaryRecovery(step)) return String(t('chat.compact.compacted'))
  if (step.category === 'maintenance' && step.state === 'failed' && !isFailure(step)) return String(t('chat.compact.incomplete'))
  if (step.state === 'failed' && step.failureKind) return `${t(step.label.code, step.label.params)} · ${t(chatErrorPresentation({ failureKind: step.failureKind }).messageKey)}`
  const descriptor = providerActivityLabel(step, now.value, announce)
  const recovery = String(t(descriptor.code, descriptor.params))
  return step.category === 'maintenance' && step.isCurrent && step.providerRecovery
    ? `${t(step.label.code, step.label.params)} · ${recovery}` : recovery
}
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
    // The original items own call order, narration boundaries and aggregate
    // failure evidence. RunTrace flattens their calls for presentation.
    // Whitespace-only stream segments can have raw text but no rendered HTML.
    // Keep them in the canonical stream, but do not let an empty narration
    // claim both adjacent paragraph gaps in the activity presentation.
    return props.timelineItems.filter(item => item.type !== 'text' || !!item.html.trim())
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
      content?: VNode[]
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

function hasSlotContent(nodes: unknown[]): boolean {
  return nodes.some(node => {
    if (typeof node === 'string' || typeof node === 'number') return !!String(node).trim()
    if (Array.isArray(node)) return hasSlotContent(node)
    if (!isVNode(node) || node.type === Comment) return false
    if (node.type === Text) return typeof node.children === 'string' && !!node.children.trim()
    if (node.type === Fragment) {
      return Array.isArray(node.children) && hasSlotContent(node.children)
    }
    return true
  })
}

/* Evaluate each interrupt slot once, during render. Empty history slots must
   not introduce invisible tool batches between two visible reading blocks. */
function renderSegments(): ActivitySegment[] {
  return segments.value.flatMap<ActivitySegment>(segment => {
    if (segment.type !== 'interrupt') return [segment]
    const content = slots.interrupt?.({ part: segment.items[0]!.part }) ?? []
    return hasSlotContent(content) ? [{ ...segment, content }] : []
  })
}

function InterruptSlotContent({ nodes }: { nodes?: VNode[] }) {
  return nodes
}

</script>

<style scoped>
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; border: 0; }
.assistant-activity-timeline {
  display: flow-root;
  min-width: 0;
}

.assistant-activity-timeline :deep(.tool-timeline--checklist) {
  gap: 0;
}

/* Keep tool rows dense; only narrative boundaries receive reading space. */
.assistant-activity-timeline > .activity-narration + .activity-narration {
  margin-top: 0.75rem;
}

/* Both trace wrappers use display:contents; put spacing on the first real
   row box, and do not give an empty interrupt slot a visible gap. */
.assistant-activity-timeline > .activity-narration + :deep(.tool-timeline > .tool-row-group > :first-child:not(:empty)) {
  margin-top: 0.75rem;
}

.assistant-activity-timeline > .tool-timeline + .activity-narration,
.assistant-activity-timeline > .assistant-activity-status + .activity-narration {
  margin-top: 0.75rem;
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

/* Routine progress shares thinking's text motion and reading origin.
   The disclosure header owns elapsed time; no second ticking label or dot. */
.assistant-activity-status__row--routine {
  padding-inline: 0;
  color: var(--text-dim);
}

.assistant-activity-status__row--routine .assistant-activity-status__content {
  flex: 0 1 auto;
}

.assistant-activity-status__row--routine.activity-step-enter-from {
  opacity: 1;
  transform: none;
}

.assistant-activity-status__content {
  min-width: 0;
  overflow-wrap: anywhere;
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

.assistant-activity-status__row--maintenance.assistant-activity-status__row--animating
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

@media (prefers-reduced-motion: reduce) {
  .activity-step-enter-active,
  .activity-step-move {
    transition: none;
  }

  .assistant-activity-status__row--maintenance.assistant-activity-status__row--animating
    .assistant-activity-status__dot {
    animation: none;
  }
}
</style>
