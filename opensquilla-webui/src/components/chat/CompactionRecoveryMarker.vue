<template>
  <span class="compaction-recovery-marker" @mouseenter="showHint('hover')" @mouseleave="hovered = false">
    <button
      ref="button"
      type="button"
      class="compaction-recovery-marker__button"
      :aria-label="label"
      :aria-describedby="hintId"
      @click="toggle"
      @focus="showHint('focus')"
      @blur="focused = false; dismiss()"
      @keydown.esc.stop.prevent="dismiss"
    ><span class="compaction-recovery-marker__ring" aria-hidden="true" /></button>
    <Teleport to="body">
      <span v-if="visible" :id="hintId" role="tooltip" class="compaction-recovery-marker__hint" :style="hintStyle">{{ label }}</span>
    </Teleport>
  </span>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, useId } from 'vue'
import { useI18n } from 'vue-i18n'
const { t } = useI18n()
const open = ref(false)
const dismissed = ref(false)
const hovered = ref(false)
const focused = ref(false)
const button = ref<HTMLButtonElement>()
const hintStyle = ref<Record<string, string>>({})
const visible = computed(() => !dismissed.value && (open.value || hovered.value || focused.value))
const hintId = useId()
const label = computed(() => t('chat.compact.temporarilyReduced'))
function positionHint() {
  const bounds = button.value?.getBoundingClientRect()
  if (!bounds) return
  const width = Math.min(288, window.innerWidth * 0.7)
  hintStyle.value = {
    left: `${Math.max(8, Math.min(bounds.left, window.innerWidth - width - 8))}px`,
    ...(bounds.top < 64
      ? { top: `${bounds.bottom + 8}px` }
      : { bottom: `${window.innerHeight - bounds.top + 8}px` }),
  }
}
function showHint(source: 'hover' | 'focus') {
  if (source === 'hover') hovered.value = true
  else focused.value = true
  positionHint()
  dismissed.value = false
}
function dismiss() {
  open.value = false
  dismissed.value = true
}
function toggle() {
  if (open.value) dismiss()
  else {
    positionHint()
    open.value = true
    dismissed.value = false
  }
}
onMounted(() => {
  window.addEventListener('scroll', dismiss, true)
  window.addEventListener('resize', dismiss)
})
onBeforeUnmount(() => {
  window.removeEventListener('scroll', dismiss, true)
  window.removeEventListener('resize', dismiss)
})
</script>

<style scoped>
.compaction-recovery-marker { position: relative; display: inline-flex; align-items: center; flex: 0 0 auto; width: 0.875rem; height: 1rem; }
.compaction-recovery-marker__button { display: flex; align-items: center; justify-content: center; position: absolute; width: 2rem; height: 2rem; left: 50%; transform: translateX(-50%); padding: 0; border: 0; background: transparent; color: inherit; cursor: var(--cursor-interactive, pointer); border-radius: var(--radius-sm); }
.compaction-recovery-marker__ring { position: relative; width: 0.5rem; height: 0.5rem; border: 1px solid currentColor; border-radius: var(--radius-full); }
.compaction-recovery-marker__ring::after { content: ''; position: absolute; inset: 0; margin: auto; width: 2px; height: 2px; border-radius: var(--radius-full); background: currentColor; }
.compaction-recovery-marker__hint { position: fixed; z-index: 1000; width: max-content; max-width: min(18rem, 70vw); box-sizing: border-box; padding: 0.5rem 0.75rem; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--bg-elevated); color: var(--text); font-size: var(--fs-xs); line-height: 1.5; pointer-events: none; }
@media (pointer: coarse) { .compaction-recovery-marker__button { width: 2.75rem; height: 2.75rem; } }
</style>
