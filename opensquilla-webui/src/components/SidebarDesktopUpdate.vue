<script setup lang="ts">
import { onMounted } from 'vue'
import Icon from '@/components/Icon.vue'
import { useDesktopUpdate } from '@/composables/useDesktopUpdate'
import { useDesktopUpdatePresentation } from '@/composables/useDesktopUpdatePresentation'

const emit = defineEmits<{ open: [] }>()
const update = useDesktopUpdate()
const { summary, title, iconName, severity } = useDesktopUpdatePresentation(update)
onMounted(update.init)
</script>

<template>
  <button
    v-if="update.visible.value"
    type="button"
    class="sidebar-desktop-update"
    :data-state="severity"
    :title="summary"
    :aria-label="`${title} · ${summary}`"
    data-testid="sidebar-desktop-update"
    @click="emit('open')"
  >
    <Icon :name="iconName" :size="17" aria-hidden="true" />
  </button>
</template>

<style scoped>
.sidebar-desktop-update {
  align-items: center;
  background: transparent;
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  color: var(--accent);
  cursor: pointer;
  display: inline-flex;
  flex: 0 0 36px;
  justify-content: center;
  height: 36px;
  width: 36px;
}
.sidebar-desktop-update[data-state='danger'] { color: var(--danger); }
.sidebar-desktop-update:hover { background: var(--sidebar-hover, var(--bg-hover)); }
.sidebar-desktop-update:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
@media (max-width: 768px), (pointer: coarse) {
  .sidebar-desktop-update { flex-basis: 44px; width: 44px; height: 44px; }
}
</style>
