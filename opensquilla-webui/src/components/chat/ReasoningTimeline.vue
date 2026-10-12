<template>
  <div class="reasoning-timeline" data-testid="reasoning-timeline">
    <ReasoningPart
      v-for="block in visibleBlocks"
      :key="block.id"
      :part="asPart(block)"
      :live="block.status === 'streaming'"
      :motion-active="executing === true && !collapseActive"
      :preview-text="block.text"
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
import type { ReasoningBlock } from '@/types/turnlog'
import {
  readAssistantActivityManualChoice,
  writeAssistantActivityExpansion,
} from '@/utils/chat/activityDisclosureState'

const emit = defineEmits<{
  openChange: [open: boolean]
}>()

const props = defineProps<{
  blocks: ReasoningBlock[]
  /** Preserve explicit choices when a live block becomes its historical row. */
  sessionKey?: string
  /** Explicit live-execution gate: history, queued work and approval waits stay static. */
  executing?: boolean
  /** Freeze the displayed reasoning duration when answer organization starts. */
  collapseActive?: boolean
  /** Render blocks as children of the surrounding activity disclosure. */
  nested?: boolean
  /** Use the compact completed-phase label used by sibling activity rows. */
  timelinePhase?: boolean
}>()

const openById = reactive(new Map<string, boolean>())
const identityById = new Map<string, string>()
// Per-block scroll ownership. A fresh live block follows its tail; scrolling
// upward pauses that behavior until the user returns to the bottom.
const tailFollowById = reactive(new Map<string, boolean>())
const presentationEndedAtById = new Map<string, number>()
const clock = ref(Date.now())
let timer: ReturnType<typeof setInterval> | null = null

const visibleBlocks = computed(() => props.blocks.filter(block => block.text))
watch(() => visibleBlocks.value.some(block => openById.get(block.id) === true),
  open => emit('openChange', open), { immediate: true, flush: 'post' })

function disclosureIdentity(block: ReasoningBlock): string {
  // Block id and startedAt survive the live -> history clone. A later turn
  // may reuse the id, so neither the id nor the changing message key suffices.
  return JSON.stringify([props.sessionKey || '', 'reasoning-block', block.id, block.startedAt])
}

watch(
  () => [props.sessionKey, props.blocks.map(block => `${block.id}:${block.startedAt}:${block.status}`).join('|')],
  () => {
    const currentIds = new Set(props.blocks.map(block => block.id))
    for (const id of openById.keys()) {
      if (!currentIds.has(id)) {
        openById.delete(id)
        identityById.delete(id)
      }
    }
    for (const id of tailFollowById.keys()) {
      if (!currentIds.has(id)) tailFollowById.delete(id)
    }
    for (const id of presentationEndedAtById.keys()) {
      if (!currentIds.has(id)) presentationEndedAtById.delete(id)
    }
    for (const block of props.blocks) {
      // New reasoning stays compact; neither deltas, completion, nor the next
      // block can override a reader's explicit choice for an existing block.
      const identity = disclosureIdentity(block)
      if (identityById.get(block.id) !== identity) {
        identityById.set(block.id, identity)
        openById.set(block.id, props.sessionKey
          ? readAssistantActivityManualChoice(identity) ?? false : false)
        tailFollowById.set(block.id, true)
        presentationEndedAtById.delete(block.id)
      }
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
      if (!presentationEndedAtById.has(block.id)) {
        presentationEndedAtById.set(block.id, now)
      }
    }
  },
  { immediate: true },
)


onScopeDispose(() => {
  if (timer) clearInterval(timer)
})

function toggle(blockId: string) {
  const open = openById.get(blockId) !== true
  openById.set(blockId, open)
  const block = props.blocks.find(candidate => candidate.id === blockId)
  if (props.sessionKey && block) {
    writeAssistantActivityExpansion(disclosureIdentity(block), open, '', true)
  }
}

function setTailFollow(blockId: string, following: boolean) {
  tailFollowById.set(blockId, following)
}

function asPart(block: ReasoningBlock): Extract<ChatPart, { type: 'reasoning' }> {
  clock.value
  const end = block.endedAt ?? presentationEndedAtById.get(block.id) ?? Date.now()
  return {
    type: 'reasoning',
    key: `live-reasoning:${block.id}`,
    text: block.text,
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
