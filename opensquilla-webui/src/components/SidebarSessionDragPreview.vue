<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{
  drag: { title: string; clientX: number; clientY: number; width: number; height: number }
}>()

// Keep pointer-coordinate tracking in this child: updating the preview must
// not make the list's TransitionGroup measure every row on each pointer move.
const dragPreviewStyle = computed(() => {
  const drag = props.drag
  const width = Math.min(drag.width, 220, window.innerWidth - 24)
  const left = Math.max(12, Math.min(drag.clientX + 16, window.innerWidth - width - 12))
  const top = drag.clientY + drag.height + 16 <= window.innerHeight - 12
    ? drag.clientY + 16
    : Math.max(12, drag.clientY - drag.height - 16)
  return {
    width: `${width}px`,
    height: `${drag.height}px`,
    transform: `translate3d(${left}px, ${top}px, 0)`,
  }
})
</script>

<template>
  <div class="sidebar-session-drag-preview" :style="dragPreviewStyle" aria-hidden="true">
    <span class="sidebar-history-title">{{ drag.title }}</span>
  </div>
</template>
