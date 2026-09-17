<template>
  <div class="reasoning-timeline" data-testid="reasoning-timeline">
    <ReasoningPart
      v-for="block in visibleBlocks"
      :key="block.id"
      :part="asPart(block, visibleTextById.get(block.id) || '')"
      :live="block.status === 'streaming'"
      :nested="nested"
      :timeline-phase="timelinePhase"
      controlled
      :open="openById.get(block.id) === true"
      :follow-tail="tailFollowById.get(block.id) !== false"
      @toggle="toggle(block.id)"
      @tail-follow-change="following => setTailFollow(block.id, following)"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, onScopeDispose, reactive, ref, watch } from 'vue'
import ReasoningPart from '@/components/chat/parts/ReasoningPart.vue'
import type { ChatPart } from '@/types/parts'
import type { ReasoningBlock, ReasoningBlockStatus } from '@/types/turnlog'

const emit = defineEmits<{
  revealComplete: []
}>()

const props = defineProps<{
  blocks: ReasoningBlock[]
  /** The answer phase owns the viewport; retain but fold the prior trace. */
  collapseActive?: boolean
  /** Smooth only coarse live-provider bursts. Settled history stays immediate. */
  paceBursts?: boolean
  /** Render blocks as children of the surrounding activity disclosure. */
  nested?: boolean
  /** Use the compact completed-phase label used by sibling activity rows. */
  timelinePhase?: boolean
}>()

const openById = reactive(new Map<string, boolean>())
// Per-block scroll ownership. A fresh live block follows its tail; scrolling
// upward pauses that behavior until the user returns to the bottom.
const tailFollowById = reactive(new Map<string, boolean>())
// Canonical reasoning is accepted immediately by the turn accumulator. This
// map owns presentation only: coarse providers can deliver a whole reasoning
// passage in one burst, which otherwise looks indistinguishable from a late
// final-text backfill even though the stream contract is working correctly.
const visibleTextById = reactive(new Map<string, string>())
const statusById = new Map<string, ReasoningBlockStatus>()
const presentationEndedAtById = new Map<string, number>()
const collapseAfterReveal = new Set<string>()
const userToggledById = new Set<string>()
const activeBlockId = ref('')
const clock = ref(Date.now())
let timer: ReturnType<typeof setInterval> | null = null
let revealFrame: number | null = null

const LARGE_BURST_CHARS = 96
const REVEAL_CHARS_PER_FRAME = 32

const visibleBlocks = computed(() => props.blocks.filter(block => block.text))

function prefersReducedMotion(): boolean {
  return typeof window !== 'undefined'
    && typeof window.matchMedia === 'function'
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches
}

function safePrefix(text: string, requestedEnd: number): string {
  let end = Math.min(text.length, requestedEnd)
  if (end > 0 && end < text.length) {
    const previousCodeUnit = text.charCodeAt(end - 1)
    if (previousCodeUnit >= 0xD800 && previousCodeUnit <= 0xDBFF) end -= 1
  }
  return text.slice(0, end)
}

function scheduleReveal(): void {
  if (revealFrame !== null || typeof requestAnimationFrame !== 'function') return
  revealFrame = requestAnimationFrame(advanceReveal)
}

function advanceReveal(): void {
  revealFrame = null
  let pending = false
  for (const block of props.blocks) {
    const current = visibleTextById.get(block.id) || ''
    if (current.length >= block.text.length) continue
    visibleTextById.set(
      block.id,
      safePrefix(block.text, current.length + REVEAL_CHARS_PER_FRAME),
    )
    if ((visibleTextById.get(block.id)?.length || 0) < block.text.length) {
      pending = true
    } else if (collapseAfterReveal.delete(block.id) && !userToggledById.has(block.id)) {
      openById.set(block.id, false)
    }
  }
  if (pending) scheduleReveal()
  else emit('revealComplete')
}

function syncVisibleText(): void {
  const currentIds = new Set(props.blocks.map(block => block.id))
  for (const id of visibleTextById.keys()) {
    if (!currentIds.has(id)) visibleTextById.delete(id)
  }

  const reduceMotion = prefersReducedMotion()
  let needsReveal = false
  for (const block of props.blocks) {
    const current = visibleTextById.get(block.id)
    if (current === undefined) {
      if (
        props.paceBursts
        && !reduceMotion
        && block.text.length > LARGE_BURST_CHARS
      ) {
        visibleTextById.set(block.id, safePrefix(block.text, REVEAL_CHARS_PER_FRAME))
        needsReveal = true
      } else {
        visibleTextById.set(block.id, block.text)
      }
      continue
    }

    // Canonical text is never delayed in the turn accumulator; only this
    // presentation prefix is paced. Reduced motion and corrections replace it
    // immediately, including completed reconnect/history snapshots.
    if (!props.paceBursts || reduceMotion || block.text.length < current.length) {
      visibleTextById.set(block.id, block.text)
      continue
    }
    const remaining = block.text.length - current.length
    if (remaining <= 0) continue
    if (remaining <= LARGE_BURST_CHARS && !needsReveal) {
      visibleTextById.set(block.id, block.text)
    } else {
      needsReveal = true
    }
  }
  if (needsReveal) scheduleReveal()
}

watch(
  () => props.blocks.map(block => `${block.id}:${block.status}`).join('|'),
  () => {
    const currentIds = new Set(props.blocks.map(block => block.id))
    for (const id of openById.keys()) {
      if (!currentIds.has(id)) openById.delete(id)
    }
    for (const id of statusById.keys()) {
      if (!currentIds.has(id)) statusById.delete(id)
    }
    for (const id of tailFollowById.keys()) {
      if (!currentIds.has(id)) tailFollowById.delete(id)
    }
    for (const id of presentationEndedAtById.keys()) {
      if (!currentIds.has(id)) presentationEndedAtById.delete(id)
    }
    for (const id of collapseAfterReveal) {
      if (!currentIds.has(id)) collapseAfterReveal.delete(id)
    }
    for (const id of userToggledById) {
      if (!currentIds.has(id)) userToggledById.delete(id)
    }

    for (const block of props.blocks) {
      const previousStatus = statusById.get(block.id)
      if (block.status === 'streaming' && previousStatus !== 'streaming') {
        if (activeBlockId.value && activeBlockId.value !== block.id) {
          openById.set(activeBlockId.value, false)
        }
        openById.set(block.id, true)
        tailFollowById.set(block.id, true)
        collapseAfterReveal.delete(block.id)
        userToggledById.delete(block.id)
        activeBlockId.value = block.id
      } else if (
        previousStatus === 'streaming'
        && block.status !== 'streaming'
      ) {
        const visibleLength = visibleTextById.get(block.id)?.length ?? 0
        const revealPending = Boolean(
          props.paceBursts
          && !prefersReducedMotion()
          && block.text.length > LARGE_BURST_CHARS
          && visibleLength < block.text.length,
        )
        if (revealPending && !userToggledById.has(block.id)) {
          openById.set(block.id, true)
          collapseAfterReveal.add(block.id)
        } else if (!userToggledById.has(block.id)) {
          openById.set(block.id, false)
        }
        if (activeBlockId.value === block.id) activeBlockId.value = ''
      } else if (previousStatus === undefined && block.status !== 'streaming') {
        const revealPending = Boolean(
          props.paceBursts
          && !prefersReducedMotion()
          && block.text.length > LARGE_BURST_CHARS,
        )
        openById.set(block.id, revealPending)
        if (revealPending) collapseAfterReveal.add(block.id)
      }
      statusById.set(block.id, block.status)
    }

    const hasStreaming = props.blocks.some(block => block.status === 'streaming')
    if (hasStreaming && !timer) {
      timer = setInterval(() => { clock.value = Date.now() }, 1000)
    } else if (!hasStreaming && timer) {
      clearInterval(timer)
      timer = null
    }
  },
  { immediate: true },
)

watch(
  () => props.collapseActive,
  collapse => {
    if (!collapse) return
    const now = Date.now()
    for (const block of props.blocks) {
      const visibleLength = visibleTextById.get(block.id)?.length ?? 0
      const revealPending = Boolean(
        props.paceBursts
        && !prefersReducedMotion()
        && block.text.length > LARGE_BURST_CHARS
        && visibleLength < block.text.length,
      )
      if (revealPending && !userToggledById.has(block.id)) {
        openById.set(block.id, true)
        collapseAfterReveal.add(block.id)
      } else if (!userToggledById.has(block.id)) {
        openById.set(block.id, false)
      }
      if (!presentationEndedAtById.has(block.id)) {
        presentationEndedAtById.set(block.id, now)
      }
    }
  },
  { immediate: true },
)

watch(
  () => props.blocks.map(block => `${block.id}:${block.text.length}:${block.status}`).join('|'),
  syncVisibleText,
  { immediate: true },
)

onScopeDispose(() => {
  if (timer) clearInterval(timer)
  if (revealFrame !== null && typeof cancelAnimationFrame === 'function') {
    cancelAnimationFrame(revealFrame)
  }
})

function toggle(blockId: string) {
  userToggledById.add(blockId)
  collapseAfterReveal.delete(blockId)
  openById.set(blockId, openById.get(blockId) !== true)
}

function setTailFollow(blockId: string, following: boolean) {
  tailFollowById.set(blockId, following)
}

function asPart(
  block: ReasoningBlock,
  visibleText: string,
): Extract<ChatPart, { type: 'reasoning' }> {
  clock.value
  const end = block.endedAt ?? presentationEndedAtById.get(block.id) ?? Date.now()
  return {
    type: 'reasoning',
    key: `live-reasoning:${block.id}`,
    text: visibleText,
    seconds: Math.max(0, Math.floor((end - block.startedAt) / 1000)),
  }
}
</script>

<style scoped>
.reasoning-timeline {
  display: flex;
  flex-direction: column;
  min-width: 0;
}
</style>
