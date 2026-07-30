<template>
  <section
    v-if="isLive"
    class="assistant-activity assistant-activity--live"
    data-testid="assistant-activity"
    data-share-activity="true"
    :data-share-expanded="open ? 'true' : 'false'"
  >
    <button
      type="button"
      class="assistant-activity__live-head"
      data-share-activity-label
      data-share-control
      :aria-expanded="open"
      :aria-controls="bodyId"
      @click.stop="open = !open"
    >
      <span
        class="assistant-activity__live-dot"
        :class="{ 'is-active': !stale, 'is-stale': stale }"
        aria-hidden="true"
      />
      <span
        class="assistant-activity__live-label"
        :class="{ 'is-stale': stale }"
        role="status"
        aria-live="polite"
        aria-atomic="true"
      >
        {{ liveStatusLabel }}
      </span>
      <span
        v-if="elapsedLabel"
        class="assistant-activity__live-elapsed"
        aria-hidden="true"
      >
        · {{ elapsedLabel }}
      </span>
      <span v-if="stepCount > 0" class="assistant-activity__live-step">
        {{ t('chat.activity.liveStep', { n: stepCount }) }}
      </span>
      <Icon
        class="assistant-activity__summary-arrow"
        name="chevronRight"
        :size="13"
        aria-hidden="true"
      />
    </button>
    <div
      :id="bodyId"
      class="assistant-activity__body"
      :class="{ 'is-open': open }"
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

  <section
    v-else
    class="assistant-activity assistant-activity--settled"
    :class="{
      'assistant-activity--failed': lifecycle === 'failed',
      'assistant-activity--interrupted': lifecycle === 'interrupted',
    }"
    data-testid="assistant-activity"
    data-share-activity="true"
    :data-share-expanded="open ? 'true' : 'false'"
  >
    <button
      type="button"
      class="assistant-activity__summary"
      data-share-activity-label
      data-share-control
      :aria-expanded="open"
      :aria-controls="bodyId"
      @click.stop="open = !open"
    >
      <span class="assistant-activity__label">{{ resolvedSummaryLabel }}</span>
      <Icon
        class="assistant-activity__summary-arrow"
        name="chevronRight"
        :size="13"
        aria-hidden="true"
      />
    </button>
    <div
      :id="bodyId"
      class="assistant-activity__body"
      :class="{ 'is-open': open }"
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
import {
  readAssistantActivityExpansion,
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
  elapsedLabel?: string
  stale?: boolean
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
  elapsedLabel: '',
  stale: false,
  defaultOpen: false,
  stateKey: '',
  continuityKey: '',
})

const { t } = useI18n()
const bodyId = `assistant-activity-body-${useId()}`
const initialOpen = () => props.defaultOpen
const open = ref(readAssistantActivityExpansion(
  props.stateKey,
  initialOpen(),
  props.continuityKey,
))

watch(open, expanded => {
  writeAssistantActivityExpansion(props.stateKey, expanded, props.continuityKey)
})

watch(() => [props.stateKey, props.continuityKey] as const, ([key, continuityKey]) => {
  open.value = readAssistantActivityExpansion(key, initialOpen(), continuityKey)
})

watch(
  () => props.defaultOpen,
  (defaultOpen, previousDefaultOpen) => {
    if (defaultOpen && !previousDefaultOpen) {
      open.value = true
    }
  },
)

const isLive = computed(() =>
  props.lifecycle === 'working' || props.lifecycle === 'answering',
)

const liveStatusLabel = computed(() => props.phaseLabel || t('chat.activityWorking'))

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

.assistant-activity--live {
  display: grid;
  gap: 0.25rem;
  margin-top: 0.25rem;
}

.assistant-activity__live-head {
  display: flex;
  align-items: center;
  width: fit-content;
  max-width: 100%;
  min-width: 0;
  min-height: 1.75rem;
  gap: 0.5rem;
  padding: 0.1875rem 0.25rem;
  border: 0;
  background: transparent;
  color: color-mix(in srgb, var(--text) 90%, transparent);
  cursor: pointer;
  font: inherit;
  font-size: 0.875rem;
  line-height: 1.5;
  text-align: left;
}

.assistant-activity__live-head:hover {
  color: var(--text);
}

.assistant-activity__live-head:focus-visible {
  outline: none;
  box-shadow: var(--focus-ring);
}

.assistant-activity__live-dot {
  width: 0.4375rem;
  height: 0.4375rem;
  flex: 0 0 auto;
  border-radius: var(--radius-full);
  background: var(--accent);
}

.assistant-activity__live-dot.is-active {
  animation: assistant-activity-pulse 2.3s var(--ease-standard) infinite;
}

.assistant-activity__live-dot.is-stale {
  background: var(--warn-fill);
}

.assistant-activity__live-label {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.assistant-activity__live-label.is-stale {
  color: var(--warn);
}

.assistant-activity__live-elapsed {
  flex: 0 0 auto;
  color: var(--text-muted);
  font-size: 0.75rem;
  font-variant-numeric: tabular-nums;
}

.assistant-activity__live-step {
  flex: 0 0 auto;
  color: var(--text-muted);
  font-size: 0.75rem;
  font-variant-numeric: tabular-nums;
}

.assistant-activity__live-step::before {
  content: "·";
  margin-right: 0.375rem;
  color: var(--text-dim);
}

.assistant-activity__summary {
  display: inline-flex;
  align-items: center;
  max-width: 100%;
  min-height: 1.75rem;
  gap: 0.375rem;
  padding: 0.1875rem 0.25rem;
  border: 0;
  border-radius: 0;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  font: inherit;
  font-size: 0.75rem;
  line-height: 1.4;
  text-align: left;
  transition: color var(--dur-fast) var(--ease-standard);
}

.assistant-activity__summary:hover {
  color: var(--text);
  background: transparent;
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
}

.assistant-activity__summary-arrow {
  flex: 0 0 auto;
  color: currentColor;
  opacity: 0.34;
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
  opacity: 0.55;
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

.assistant-activity__body.is-open {
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
  gap: 0.25rem;
  margin: 0.125rem 0 0.25rem;
  padding: 0 0 0 0.75rem;
  border: 0;
  border-left: 1px solid var(--border);
  background: transparent;
}

.assistant-activity__detail {
  color: var(--text-muted);
  font-size: 0.75rem;
  line-height: 1.5;
}

.assistant-activity__body.is-open .assistant-activity__detail,
.assistant-activity__body.is-open :deep(.thinking-block),
.assistant-activity__body.is-open :deep(.assistant-activity-status__row),
.assistant-activity__body.is-open :deep(.tool-row) {
  animation: assistant-activity-item-enter var(--dur-base) var(--ease-standard) backwards;
}

.assistant-activity__body.is-open :deep(.thinking-block) {
  animation-delay: 35ms;
}

.assistant-activity__body.is-open :deep(.assistant-activity-status__row:nth-child(1)),
.assistant-activity__body.is-open :deep(.tool-row:nth-child(1)) {
  animation-delay: 60ms;
}

.assistant-activity__body.is-open :deep(.assistant-activity-status__row:nth-child(2)),
.assistant-activity__body.is-open :deep(.tool-row:nth-child(2)) {
  animation-delay: 90ms;
}

.assistant-activity__body.is-open :deep(.assistant-activity-status__row:nth-child(n + 3)),
.assistant-activity__body.is-open :deep(.tool-row:nth-child(n + 3)) {
  animation-delay: 120ms;
}

.assistant-activity--settled[data-share-expanded="true"]::after {
  content: "";
  display: block;
  height: 1px;
  background: var(--border);
}

@keyframes assistant-activity-pulse {
  0%,
  100% { opacity: 0.45; }
  50% { opacity: 1; }
}

@keyframes assistant-activity-item-enter {
  from {
    opacity: 0;
    transform: translateY(-0.1875rem);
  }
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
  .assistant-activity__summary-arrow,
  .assistant-activity__body {
    transition: none;
  }

  .assistant-activity__live-dot.is-active {
    animation: none;
  }

  .assistant-activity__body.is-open .assistant-activity__detail,
  .assistant-activity__body.is-open :deep(.thinking-block),
  .assistant-activity__body.is-open :deep(.assistant-activity-status__row),
  .assistant-activity__body.is-open :deep(.tool-row) {
    animation: none;
  }
}
</style>
