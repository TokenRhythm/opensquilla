<template>
  <section v-if="embedded" class="thinking-block">
    <div v-if="!hideSummary" class="thinking-block__header">
      <Icon name="moreHorizontal" :size="12" aria-hidden="true" />
      <span>{{ summary }}</span>
    </div>
    <div
      ref="bodyElement"
      class="thinking-block__body"
      role="region"
      tabindex="0"
      :aria-label="summary"
      @scroll.passive="onBodyScroll"
    >{{ part.text }}</div>
  </section>
  <!-- `live` is the one prop that only the in-activity usage sets, so it doubles
       as the nested-context marker: inside ActivityDisclosure the activity body
       already draws the fold's left rule, and a second rule here reads as
       doubled chrome. -->
  <details
    v-else
    class="thinking-fold"
    :class="{
      'thinking-fold--in-activity': nested || live,
      'thinking-fold--timeline-phase': timelinePhase,
    }"
    :open="controlled ? open : undefined"
  >
    <summary class="thinking-fold__summary" @click="onSummaryClick">
      <Icon class="thinking-fold__chevron" name="chevronRight" :size="12" />
      <span>{{ summary }}</span>
    </summary>
    <div
      ref="bodyElement"
      class="thinking-fold__body"
      role="region"
      tabindex="0"
      :aria-label="summary"
      @scroll.passive="onBodyScroll"
    >{{ part.text }}</div>
  </details>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import type { ChatPart } from '@/types/parts'

const { t } = useI18n()

const props = defineProps<{
  part: Extract<ChatPart, { type: 'reasoning' }>
  embedded?: boolean
  /** Nested inside the outer activity disclosure. */
  nested?: boolean
  /** Streaming turn: label the fold "Thinking · Ns" (matching the live
   * elapsed ticker) instead of the settled "Thought for …" wording. */
  live?: boolean
  /** Finished activity timeline: use the same compact noun + duration style as sibling phases. */
  timelinePhase?: boolean
  /** The parent disclosure already names this content, so avoid a second
   * "thought" label when reasoning is embedded in a Plan process. */
  hideSummary?: boolean
  /** Controlled disclosure state for the live reasoning timeline. Omit to keep
   * the settled-history disclosure natively controlled by the browser. */
  open?: boolean
  /** Explicitly opt into controlled disclosure ownership. Vue casts an absent
   * optional Boolean prop to false, so `open` alone cannot distinguish a
   * settled native fold from a controlled closed fold. */
  controlled?: boolean
  /** Keep a live trace pinned to its newest content until the user scrolls up. */
  followTail?: boolean
}>()

const emit = defineEmits<{
  toggle: []
  tailFollowChange: [following: boolean]
}>()

const bodyElement = ref<HTMLElement | null>(null)
const TAIL_THRESHOLD_PX = 4

function pinToTail() {
  if (!props.live || !props.followTail || props.open === false) return
  void nextTick(() => {
    const body = bodyElement.value
    if (!body || !props.live || !props.followTail || props.open === false) return
    body.scrollTop = body.scrollHeight
  })
}

function onBodyScroll(event: Event) {
  if (!props.live || props.followTail === undefined) return
  const body = event.currentTarget as HTMLElement
  const distanceFromTail = body.scrollHeight - body.clientHeight - body.scrollTop
  emit('tailFollowChange', distanceFromTail <= TAIL_THRESHOLD_PX)
}

watch(
  () => [props.part.text, props.open, props.live, props.followTail] as const,
  pinToTail,
  { immediate: true, flush: 'post' },
)

function onSummaryClick(event: MouseEvent) {
  if (!props.controlled) return
  event.preventDefault()
  emit('toggle')
}

const summary = computed(() => {
  const seconds = props.part.seconds || 0
  if (props.live) return t('chat.thinkingForSeconds', { seconds })
  if (props.timelinePhase) {
    const duration = seconds < 60
      ? t('chat.activityDurationSeconds', { seconds })
      : t('chat.activityDurationMinutes', {
          minutes: Math.floor(seconds / 60),
          seconds: seconds % 60,
        })
    return `${t('chat.activity.provider.reasoning')} · ${duration}`
  }
  if (seconds < 1) return t('chat.thoughtProcess')
  if (seconds < 60) return t('chat.thoughtForSeconds', { seconds })
  return t('chat.thoughtForMinutes', { minutes: Math.floor(seconds / 60), seconds: seconds % 60 })
})
</script>

<style scoped>
.thinking-block {
  min-width: 0;
}
.thinking-block__header {
  display: flex;
  align-items: center;
  gap: 0.375rem;
  margin-bottom: 0.25rem;
  color: var(--text-dim);
  font-size: 0.75rem;
  line-height: 1.5;
}
.thinking-block__body {
  color: var(--text-muted);
  font-size: 0.8125rem;
  line-height: 1.55;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 16rem;
  overflow-y: auto;
  padding-right: 0.5rem;
}

/* Reasoning disclosure — self-contained fold (chevron, focus ring, capped
 * scrolling body) used for settled history rows and reusable as-is for the
 * live streaming fold, kept local so this part needs no shared sheet. */
.thinking-fold { margin: 0 0 0.5rem; font-size: 0.8125rem; color: var(--text-dim); }
.thinking-fold__summary {
  display: inline-flex; align-items: center; gap: 0.375rem;
  padding: 0.125rem 0.25rem; border-radius: var(--radius-sm);
  cursor: pointer; list-style: none; color: var(--text-dim); line-height: 1.5;
}
.thinking-fold__summary::-webkit-details-marker { display: none; }
.thinking-fold__summary:hover { color: var(--text-muted); }
.thinking-fold__summary:focus-visible {
  outline: none;
  box-shadow: var(--focus-ring);
}
.thinking-fold__chevron { flex-shrink: 0; transition: transform var(--dur-fast) var(--ease-standard); }
.thinking-fold[open] > .thinking-fold__summary .thinking-fold__chevron { transform: rotate(90deg); }
.thinking-fold__body {
  margin: 0.25rem 0 0.375rem; padding: 0.375rem 0.75rem;
  border-left: 2px solid var(--border); color: var(--text-muted);
  line-height: 1.55; white-space: pre-wrap; word-break: break-word;
  max-height: 16rem; overflow-y: auto;
}
/* Nested inside the activity fold the outer 1px frame already rules the left
 * edge, so the fold's own 2px rule (and the padding that indents from it)
 * would draw two parallel lines — drop both for that variant only. */
.thinking-fold--in-activity > .thinking-fold__body { border-left: none; padding-left: 0; }
.thinking-fold--timeline-phase {
  margin-bottom: 0;
  font-size: 0.75rem;
}
.thinking-fold--timeline-phase > .thinking-fold__summary {
  min-height: 1.75rem;
  gap: 0.625rem;
  padding: 0.25rem 0.125rem;
  border-radius: 0;
  line-height: 1.45;
}
@media (prefers-reduced-motion: reduce) {
  .thinking-fold__chevron { transition: none; }
}
</style>
