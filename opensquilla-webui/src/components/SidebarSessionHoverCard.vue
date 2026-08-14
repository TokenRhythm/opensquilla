<script lang="ts">
export const SESSION_PREVIEW_WIDTH = 272
export const SESSION_PREVIEW_HEIGHT = 104

type PreviewPosition = { left: string; top: string }
type PreviewSize = { width: number; height: number }
type PreviewViewport = { width: number; height: number }

export function fitSessionPreviewPosition(
  position: PreviewPosition,
  size: PreviewSize,
  viewport: PreviewViewport,
): PreviewPosition {
  const edge = 12
  const left = Math.max(
    edge,
    Math.min(Number.parseFloat(position.left), viewport.width - edge - size.width),
  )
  const top = Math.max(
    edge,
    Math.min(Number.parseFloat(position.top), viewport.height - edge - size.height),
  )
  return {
    left: `${Math.round(left)}px`,
    top: `${Math.round(top)}px`,
  }
}

export function sessionPreviewPosition(
  rect: Pick<DOMRect, 'left' | 'right' | 'top'>,
  viewport: { width: number; height: number },
): { left: string; top: string } {
  const gap = 8
  const edge = 12
  const left = rect.right + gap + SESSION_PREVIEW_WIDTH <= viewport.width - edge
    ? rect.right + gap
    : Math.max(edge, rect.left - gap - SESSION_PREVIEW_WIDTH)
  const top = Math.max(
    edge,
    Math.min(rect.top, viewport.height - edge - SESSION_PREVIEW_HEIGHT),
  )
  return {
    left: `${Math.round(left)}px`,
    top: `${Math.round(top)}px`,
  }
}
</script>

<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { formatRelativeTime } from './sessions/sessionDisplay'
import Icon from './Icon.vue'

const props = defineProps<{
  title: string
  updatedAt: number
  projectName?: string
  position: { left: string; top: string }
}>()

const relativeUpdatedAt = computed(() => formatRelativeTime(props.updatedAt))
const previewEl = ref<HTMLElement | null>(null)
const resolvedPosition = ref(props.position)

async function fitMeasuredCard() {
  await nextTick()
  if (!previewEl.value) return
  const rect = previewEl.value.getBoundingClientRect()
  resolvedPosition.value = fitSessionPreviewPosition(
    props.position,
    { width: rect.width, height: rect.height },
    { width: window.innerWidth, height: window.innerHeight },
  )
}

watch(() => props.position, (position) => {
  resolvedPosition.value = position
  void fitMeasuredCard()
})
onMounted(() => { void fitMeasuredCard() })
</script>

<template>
  <div
    ref="previewEl"
    id="sidebar-session-preview"
    class="sidebar-session-preview"
    role="tooltip"
    :style="resolvedPosition"
  >
    <div class="sidebar-session-preview__head">
      <span class="sidebar-session-preview__title">{{ title }}</span>
      <span class="sidebar-session-preview__time">{{ relativeUpdatedAt }}</span>
    </div>
    <div
      v-if="projectName"
      class="sidebar-session-preview__project"
      data-testid="sidebar-session-project"
    >
      <Icon name="folder" :size="15" aria-hidden="true" />
      <span>{{ projectName }}</span>
    </div>
  </div>
</template>
