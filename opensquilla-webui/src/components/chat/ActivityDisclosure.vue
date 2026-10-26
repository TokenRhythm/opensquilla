<template>
  <section
    class="assistant-activity"
    :class="{
      'assistant-activity--live': isLive,
      'assistant-activity--settled': !isLive,
      'assistant-activity--failed': lifecycle === 'failed',
      'assistant-activity--interrupted': lifecycle === 'interrupted',
    }"
    data-testid="assistant-activity"
    data-share-activity="true"
    :data-share-expanded="open ? 'true' : 'false'"
  >
    <div class="assistant-activity__header">
      <button
        v-if="isLive"
        type="button"
        class="assistant-activity__live-head"
        data-share-activity-label
        data-share-control
        :aria-expanded="open"
        :aria-controls="bodyId"
        @click.stop="toggleOpen"
      >
        <ActivityMotion :active="headerMotionActive">
          <span class="assistant-activity__live-elapsed">{{ liveElapsedLabel }}</span>
          <Icon
            class="assistant-activity__summary-arrow"
            name="chevronRight"
            :size="13"
            aria-hidden="true"
          />
        </ActivityMotion>
      </button>
      <button
        v-else
        type="button"
        class="assistant-activity__summary"
        data-share-activity-label
        data-share-control
        :aria-expanded="open"
        :aria-controls="bodyId"
        @click.stop="toggleOpen"
      >
        <span class="assistant-activity__label">{{ resolvedSummaryLabel }}</span>
        <Icon
          class="assistant-activity__summary-arrow"
          name="chevronRight"
          :size="13"
          aria-hidden="true"
        />
      </button>
    </div>
    <!-- Announce phase changes, never the ticking timer. Visible phases belong
         to the chronological body, outside the total-duration header. -->
    <span v-if="isLive" class="assistant-activity__sr-only" role="status" aria-live="polite" aria-atomic="true">
      {{ phaseAnnouncement || liveStatusLabel }}
    </span>
    <div
      v-if="showCollapsedCompaction"
      class="assistant-activity__compaction"
      :class="{ 'assistant-activity__compaction--running': executing && !stale && !phaseWaiting }"
      data-testid="collapsed-compaction-status"
      aria-hidden="true"
    >
      <span class="assistant-activity__compaction-ring" />
      <span>{{ t('chat.compact.compacting') }}</span>
    </div>
    <div
      :id="bodyId"
      class="assistant-activity__body"
      :aria-hidden="!open"
      :inert="open ? undefined : true"
      data-share-activity-body
    >
      <div class="assistant-activity__body-inner">
        <div v-if="detailLabel" class="assistant-activity__detail">
          {{ detailLabel }}
        </div>
        <slot />
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, ref, useId, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import ActivityMotion from './ActivityMotion.vue'
import {
  provideActivityDisclosureVisibility,
  useActivityDisclosureVisibility,
} from '@/composables/chat/useActivityDisclosureVisibility'
import {
  readAssistantActivityExpansion,
  readAssistantActivityManualChoice,
  writeAssistantActivityExpansion,
} from '@/utils/chat/activityDisclosureState'

const props = withDefaults(defineProps<{
  lifecycle?: 'working' | 'answering' | 'settled' | 'failed' | 'interrupted'
  stepCount: number
  failureCount: number
  durationSeconds?: number
  completionConfirmed?: boolean
  summaryLabel?: string
  detailLabel?: string
  phaseLabel?: string
  phaseAnnouncement?: string
  phaseWaiting?: boolean
  compacting?: boolean
  elapsedLabel?: string
  queued?: boolean
  stale?: boolean
  executing?: boolean
  defaultOpen?: boolean
  stateKey?: string
  continuityKey?: string
}>(), {
  lifecycle: 'settled',
  durationSeconds: 0,
  completionConfirmed: false,
  summaryLabel: '',
  detailLabel: '',
  phaseLabel: '',
  compacting: false,
  elapsedLabel: '',
  queued: false,
  stale: false,
  executing: false,
  defaultOpen: false,
  stateKey: '',
  continuityKey: '',
})

const { t } = useI18n()
const emit = defineEmits<{ openChange: [open: boolean] }>()
const bodyId = `assistant-activity-body-${useId()}`
function readOpenState(key: string, continuityKey: string) {
  if (props.lifecycle === 'working' || props.lifecycle === 'answering') {
    // A previous owner may have auto-collapsed this turn while settling.
    // Only a reader's explicit choice should override the live default.
    return readAssistantActivityManualChoice(key, continuityKey) ?? props.defaultOpen
  }
  return readAssistantActivityExpansion(key, props.defaultOpen, continuityKey)
}
const open = ref(readOpenState(props.stateKey, props.continuityKey))
const parentActivityVisible = useActivityDisclosureVisibility()
provideActivityDisclosureVisibility(open)
const manuallyToggled = ref(
  readAssistantActivityManualChoice(props.stateKey, props.continuityKey) !== undefined,
)

function toggleOpen() {
  manuallyToggled.value = true
  open.value = !open.value
  // Done can replace this live component before its queued watcher runs.
  // Persist the click now so the canonical history reads the user's choice.
  writeAssistantActivityExpansion(props.stateKey, open.value, props.continuityKey, true)
}

watch(open, expanded => {
  writeAssistantActivityExpansion(props.stateKey, expanded, props.continuityKey, manuallyToggled.value)
})
watch(open, expanded => emit('openChange', expanded), { immediate: true, flush: 'post' })

watch(() => [props.stateKey, props.continuityKey] as const, ([key, continuityKey]) => {
  manuallyToggled.value = readAssistantActivityManualChoice(key, continuityKey) !== undefined
  open.value = readOpenState(key, continuityKey)
})

watch(
  () => props.defaultOpen,
  (defaultOpen, previousDefaultOpen) => {
    if (defaultOpen === previousDefaultOpen) return
    // Follow the live/terminal default only until the reader chooses a state.
    // Completing a turn must not close details that they explicitly opened.
    if (!manuallyToggled.value) open.value = defaultOpen
  },
)

const isLive = computed(() =>
  props.lifecycle === 'working' || props.lifecycle === 'answering',
)
// The existing live region owns announcements; this is its visible counterpart
// while the detailed maintenance row is folded away.
const showCollapsedCompaction = computed(() =>
  isLive.value && props.compacting && !props.queued && !open.value,
)

// A live turn can be queued or waiting for approval. The owner supplies the
// execution signal; opening details moves motion to the actual current row.
const headerMotionActive = computed(() =>
  parentActivityVisible.value && isLive.value && props.executing && !open.value && !props.stale && !props.phaseWaiting && !showCollapsedCompaction.value,
)

const liveStatusLabel = computed(() => props.phaseLabel || t('chat.activityWorking'))
const liveElapsedLabel = computed(() => {
  // Queued tasks have no authoritative execution clock yet.
  if (props.queued) return t('chat.status.queued')
  // The stream clock is a total in seconds; localize it with the same units
  // as persisted turns so completion does not switch from 107s to 1m 47s.
  const seconds = props.elapsedLabel.match(/^(\d+)s$/)?.[1]
  const value = seconds === undefined ? null : Number(seconds)
  const duration = value === null
    ? props.elapsedLabel || String(t('chat.activityDurationSeconds', { seconds: 0 }))
    : value < 60
      ? String(t('chat.activityDurationSeconds', { seconds: value }))
      : String(t('chat.activityDurationMinutes', { minutes: Math.floor(value / 60), seconds: value % 60 }))
  return t('chat.activityElapsed', { duration })
})

const resolvedSummaryLabel = computed(() => {
  if (props.summaryLabel) return props.summaryLabel
  const seconds = Math.max(0, Math.floor(props.durationSeconds || 0))
  if (seconds > 0) {
    if (seconds < 60) return t('chat.workedForSeconds', { seconds })
    return t('chat.workedForMinutes', {
      minutes: Math.floor(seconds / 60),
      seconds: seconds % 60,
    })
  }
  return t(
    props.lifecycle === 'settled' && props.completionConfirmed
      ? 'chat.activityCompletedItems'
      : 'chat.activityItems',
    { count: Math.max(1, props.stepCount) },
  )
})
</script>

<style scoped>
.assistant-activity {
  min-width: 0;
  margin: 0 0 0.625rem;
  color: var(--text-muted);
}

.assistant-activity__header {
  padding-bottom: 0.375rem;
  border-bottom: 1px solid var(--hairline);
}

.assistant-activity__live-head {
  display: flex;
  align-items: center;
  width: fit-content;
  max-width: 100%;
  min-width: 0;
  min-height: 1.75rem;
  gap: 0.375rem;
  padding: 0.1875rem 0;
  border: 0;
  border-radius: var(--radius-control);
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  font: inherit;
  font-size: 0.8125rem;
  line-height: 1.5;
  text-align: left;
  transition: background-color var(--dur-fast) var(--ease-standard), color var(--dur-fast) var(--ease-standard);
}

.assistant-activity__live-head:hover {
  background: color-mix(in srgb, var(--bg-hover) 58%, transparent);
  color: var(--text);
}

.assistant-activity__live-head:focus-visible {
  outline: none;
  box-shadow: var(--focus-ring);
}

.assistant-activity__sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}

.assistant-activity__live-elapsed {
  flex: 0 0 auto;
  color: var(--text-muted);
  font-size: inherit;
  font-variant-numeric: tabular-nums;
}

.assistant-activity__compaction {
  display: flex;
  align-items: center;
  gap: 0.625rem;
  min-height: 1.75rem;
  padding: 0.25rem 0.125rem;
  color: color-mix(in srgb, var(--text) 58%, transparent);
  font-size: 0.8125rem;
  line-height: 1.45;
}

.assistant-activity__compaction-ring {
  width: 0.5rem;
  height: 0.5rem;
  flex: 0 0 auto;
  margin: 0 0.1875rem;
  border: 1px solid currentColor;
  border-radius: var(--radius-full);
}

.assistant-activity__compaction--running .assistant-activity__compaction-ring {
  border-color: var(--accent);
  border-right-color: transparent;
  animation: collapsedCompactionSpin 0.9s linear infinite;
}

@keyframes collapsedCompactionSpin {
  to { transform: rotate(360deg); }
}

@media (prefers-reduced-motion: reduce) {
  .assistant-activity__compaction--running .assistant-activity__compaction-ring {
    animation: none;
  }
}

/* The shared mask supplies the dimming; do not dim small metadata twice. */
.activity-motion--active .assistant-activity__live-elapsed {
  color: var(--text);
}

.assistant-activity__summary {
  display: inline-flex;
  align-items: center;
  max-width: 100%;
  min-height: 1.75rem;
  gap: 0.375rem;
  padding: 0.1875rem 0;
  border: 0;
  border-radius: var(--radius-control);
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  font: inherit;
  font-size: 0.8125rem;
  line-height: 1.5;
  text-align: left;
  transition: background-color var(--dur-fast) var(--ease-standard), color var(--dur-fast) var(--ease-standard);
}

.assistant-activity__summary:hover {
  background: color-mix(in srgb, var(--bg-hover) 58%, transparent);
  color: var(--text);
}

.assistant-activity__summary:focus-visible {
  outline: none;
  box-shadow: var(--focus-ring);
  color: var(--text);
}

.assistant-activity__label {
  min-width: 0;
  white-space: normal;
  overflow-wrap: anywhere;
  font-variant-numeric: tabular-nums;
}

.assistant-activity__summary-arrow {
  flex: 0 0 auto;
  color: currentColor;
  opacity: 0;
  transform-origin: center;
  transition:
    opacity var(--dur-fast) var(--ease-standard),
    transform var(--dur-fast) var(--ease-standard);
}

.assistant-activity__summary:hover .assistant-activity__summary-arrow,
.assistant-activity__summary:focus-visible .assistant-activity__summary-arrow,
.assistant-activity__live-head:hover .assistant-activity__summary-arrow,
.assistant-activity__live-head:focus-visible .assistant-activity__summary-arrow {
  opacity: 0.8;
  transform: translateX(0);
}

.assistant-activity__summary[aria-expanded="true"] .assistant-activity__summary-arrow,
.assistant-activity__live-head[aria-expanded="true"] .assistant-activity__summary-arrow {
  transform: rotate(90deg);
}

.assistant-activity__summary[aria-expanded="true"]:hover .assistant-activity__summary-arrow,
.assistant-activity__summary[aria-expanded="true"]:focus-visible .assistant-activity__summary-arrow,
.assistant-activity__live-head[aria-expanded="true"]:hover .assistant-activity__summary-arrow,
.assistant-activity__live-head[aria-expanded="true"]:focus-visible .assistant-activity__summary-arrow {
  opacity: 0.8;
  transform: rotate(90deg);
}

.assistant-activity__body {
  display: grid;
  grid-template-rows: 0fr;
  min-width: 0;
  opacity: 0;
  pointer-events: none;
  visibility: hidden;
  transform: translateY(-0.25rem);
  transition:
    grid-template-rows var(--dur-base) var(--ease-standard),
    opacity var(--dur-fast) var(--ease-standard),
    transform var(--dur-base) var(--ease-standard);
}

.assistant-activity[data-share-expanded="true"] > .assistant-activity__body {
  grid-template-rows: 1fr;
  opacity: 1;
  pointer-events: auto;
  visibility: visible;
  transform: translateY(0);
}

.assistant-activity__body-inner {
  display: grid;
  min-height: 0;
  min-width: 0;
  overflow: hidden;
  gap: 0;
  margin: 0;
  padding: 0;
  border: 0;
  background: transparent;
}

.assistant-activity[data-share-expanded="true"] > .assistant-activity__body > .assistant-activity__body-inner {
  margin-top: 0.75rem;
}

/* Older history can render reasoning and the following item batch separately. */
.assistant-activity__body-inner > :deep(.reasoning-timeline + .assistant-activity-timeline:has(> .activity-narration:first-child)),
.assistant-activity__body-inner > :deep(.thinking-fold + .assistant-activity-timeline:has(> .activity-narration:first-child)) {
  margin-top: 0.75rem;
}

/* Touch users need a larger target, while desktop keeps the compact reading
   rhythm. This changes only the affordance, not the disclosure semantics. */
@media (pointer: coarse) {
  .assistant-activity__live-head,
  .assistant-activity__summary {
    min-height: 2.75rem;
    padding-inline: 0;
  }
}

.assistant-activity__detail {
  color: var(--text-muted);
  font-size: 0.75rem;
  line-height: 1.5;
}

@media (max-width: 480px) {
  .assistant-activity__summary {
    flex-wrap: wrap;
  }

  .assistant-activity__live-head {
    gap: 0.375rem;
  }
}

@media (hover: none) {
  .assistant-activity__summary-arrow {
    opacity: 0.55;
  }
}

@media (prefers-reduced-motion: reduce) {
  .assistant-activity__summary,
  .assistant-activity__live-head,
  .assistant-activity__summary-arrow,
  .assistant-activity__body {
    transition: none;
  }
}
</style>
