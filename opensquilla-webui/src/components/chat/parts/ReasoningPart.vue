<template>
  <section v-if="embedded" class="thinking-block">
    <div v-if="!hideSummary" class="thinking-block__header">
      <ActivityMotion :active="live === true && motionActive !== false && activityVisible">
        <Icon name="moreHorizontal" :size="12" aria-hidden="true" />
        <span>{{ summary }}</span>
      </ActivityMotion>
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
    @toggle="onDisclosureToggle"
  >
    <summary class="thinking-fold__summary" @click="onSummaryClick">
      <ActivityMotion class="thinking-fold__motion" :active="live === true && motionActive !== false && activityVisible">
        <span class="thinking-fold__label">{{ summary }}</span>
        <Icon class="thinking-fold__chevron" name="chevronRight" :size="12" aria-hidden="true" />
      </ActivityMotion>
      <span v-if="live && previewLine" class="thinking-fold__preview" aria-hidden="true"><span class="thinking-fold__preview-line">{{ previewLine }}</span></span>
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
import { computed, nextTick, onScopeDispose, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import ActivityMotion from '@/components/chat/ActivityMotion.vue'
import type { ChatPart } from '@/types/parts'
import { useActivityDisclosureVisibility } from '@/composables/chat/useActivityDisclosureVisibility'

const { t } = useI18n()
const activityVisible = useActivityDisclosureVisibility()

const props = withDefaults(defineProps<{
  part: Extract<ChatPart, { type: 'reasoning' }>
  embedded?: boolean
  /** Nested inside the outer activity disclosure. */
  nested?: boolean
  /** Streaming turn. Nested summaries leave elapsed time to the turn header. */
  live?: boolean
  /** Stop feedback when the parent has moved beyond reasoning, without changing
   * the existing label or disclosure ownership. */
  motionActive?: boolean
  /** Canonical arrived text for the compact preview. */
  previewText?: string
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
}>(), { motionActive: true })

const emit = defineEmits<{
  toggle: []
  openChange: [open: boolean]
  tailFollowChange: [following: boolean]
}>()

const bodyElement = ref<HTMLElement | null>(null)
const TAIL_THRESHOLD_PX = 4
const PREVIEW_LINE_DWELL_MS = 500
const previewLine = ref('')
const nativeOpen = ref(false)
let previewIndex = -1
let previewIdentity = ''
let previewShownAt = 0
let previewTimer: ReturnType<typeof setTimeout> | undefined
let pendingPreview: { index: number; text: string } | undefined

function clearPreviewTimer() {
  if (previewTimer !== undefined) clearTimeout(previewTimer)
  previewTimer = undefined
  pendingPreview = undefined
}

function showPreview(line: { index: number; text: string }) {
  clearPreviewTimer()
  previewLine.value = line.text
  previewIndex = line.index
  previewShownAt = Date.now()
}

// Pace only changes of the compact preview's line. The canonical body stays
// immediate, same-line text grows in place, and pending lines never form a queue.
watch(() => [props.previewText ?? props.part.text, props.part.key, props.live,
  props.motionActive, props.controlled ? props.open : nativeOpen.value, activityVisible.value] as const,
([text, identity, live, motionActive, expanded, visible]) => {
  const lines = text.split(/\r?\n/)
  let index = lines.length - 1
  while (index >= 0 && !lines[index]!.trim()) index--
  const latest = { index, text: index < 0 ? '' : lines[index]!.trim() }
  if (identity !== previewIdentity || !live || motionActive === false || expanded || !visible
    || previewIndex < 0 || latest.index < previewIndex) {
    previewIdentity = identity
    showPreview(latest)
    return
  }
  if (latest.index === previewIndex) {
    clearPreviewTimer()
    previewLine.value = latest.text
    return
  }
  const remaining = PREVIEW_LINE_DWELL_MS - (Date.now() - previewShownAt)
  if (remaining <= 0) {
    showPreview(latest)
    return
  }
  pendingPreview = latest
  if (previewTimer === undefined) previewTimer = setTimeout(() => {
    if (pendingPreview) showPreview(pendingPreview)
  }, remaining)
}, { immediate: true })
onScopeDispose(clearPreviewTimer)

function onDisclosureToggle(event: Event) {
  nativeOpen.value = (event.target as HTMLDetailsElement).open
  emit('openChange', nativeOpen.value)
}

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
  if (props.live) return props.nested || props.timelinePhase
    ? t('chat.thinking') : t('chat.thinkingForSeconds', { seconds })
  if (props.timelinePhase) {
    if (seconds < 1) return t('chat.thoughtProcess')
    const duration = seconds < 60
      ? t('chat.activityDurationSeconds', { seconds })
      : t('chat.activityDurationMinutes', {
          minutes: Math.floor(seconds / 60),
          seconds: seconds % 60,
        })
    return `${t('chat.thoughtProcess')} · ${duration}`
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
  display: flex; align-items: center; column-gap: 0.375rem; min-width: 0;
  padding: 0.25rem 0; border-radius: var(--radius-sm);
  cursor: pointer; list-style: none; color: var(--text-dim); line-height: 1.5;
}
.thinking-fold__summary::-webkit-details-marker { display: none; }
.thinking-fold__summary:hover { color: var(--text-muted); }
.thinking-fold__summary:focus-visible {
  outline: none;
  box-shadow: var(--focus-ring);
}
.thinking-fold__chevron { flex-shrink: 0; opacity: 0; transition: opacity var(--dur-fast) var(--ease-standard), transform var(--dur-fast) var(--ease-standard); }
.thinking-fold__summary:hover .thinking-fold__chevron,
.thinking-fold__summary:focus-visible .thinking-fold__chevron { opacity: 0.8; }
.thinking-fold[open] > .thinking-fold__summary .thinking-fold__chevron { transform: rotate(90deg); }
.thinking-fold__label { flex: 0 0 auto; white-space: nowrap; }
.thinking-fold__motion { flex: 0 0 auto; }
.thinking-fold__preview { display: flex; justify-content: flex-end; flex: 1 1 auto; min-width: 0; overflow: hidden; color: var(--text-dim); font-size: 0.8125rem; }
.thinking-fold__preview-line { flex: 0 0 auto; min-width: 100%; white-space: nowrap; }
.thinking-fold[open] .thinking-fold__preview { display: none; }
.thinking-fold__body {
  margin: 0.25rem 0 0.375rem; padding: 0.125rem 0;
  color: var(--text-muted);
  line-height: 1.55; white-space: pre-wrap; word-break: break-word;
  max-height: 16rem; overflow-y: auto;
}
.thinking-fold--timeline-phase {
  margin-bottom: 0;
  font-size: 0.75rem;
}
.thinking-fold--timeline-phase > .thinking-fold__summary {
  min-height: 2rem;
  gap: 0.625rem;
  padding: 0.375rem 0;
  border-radius: 0;
  line-height: 1.45;
}
/* Structured timelines and legacy history share the same nested row dimensions. */
.thinking-fold--in-activity {
  margin-bottom: 0;
  font-size: 0.8125rem;
}
.thinking-fold--in-activity > .thinking-fold__summary {
  min-height: 2rem;
  padding: 0.375rem 0;
  line-height: 1.45;
}
@media (pointer: coarse) {
  .thinking-fold__summary,
  .thinking-fold--in-activity > .thinking-fold__summary,
  .thinking-fold--timeline-phase > .thinking-fold__summary { min-height: 2.75rem; }
}
@media (prefers-reduced-motion: reduce) {
  .thinking-fold__chevron { transition: none; }
}
</style>
