<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import { desktopUpdateReady, useDesktopUpdate } from '@/composables/useDesktopUpdate'

const emit = defineEmits<{ open: [] }>()
const { t } = useI18n()
const update = useDesktopUpdate()
const label = computed(() => desktopUpdateReady(update.state.value)
  ? t('updates.desktop.indicatorDownloaded')
  : t('updates.desktop.updateAvailableEntry'))
const accessibleLabel = computed(() => `${label.value} ${update.latestVersion.value}`.trim())
onMounted(update.init)
</script>

<template>
  <button
    v-if="update.visible.value"
    type="button"
    class="sidebar-desktop-update"
    :title="accessibleLabel"
    :aria-label="accessibleLabel"
    data-state="info"
    data-testid="sidebar-desktop-update"
    @click="emit('open')"
  >
    <Icon class="sidebar-desktop-update__icon" name="arrowUp" :size="17" aria-hidden="true" />
    <span class="sidebar-desktop-update__label">{{ label }}</span>
    <span class="sidebar-desktop-update__version">{{ update.latestVersion.value }}</span>
    <Icon class="sidebar-desktop-update__chevron" name="chevronRight" :size="14" aria-hidden="true" />
  </button>
</template>

<style scoped>
.sidebar-desktop-update {
  align-items: center;
  background: transparent;
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  color: var(--text);
  cursor: pointer;
  display: flex;
  flex: 0 0 auto;
  gap: var(--sp-2);
  min-height: 36px;
  padding: var(--sp-2);
  text-align: start;
  width: 100%;
  container-type: inline-size;
  font: inherit;
  font-size: var(--fs-sm);
}
.sidebar-desktop-update > .icon { flex: 0 0 auto; }
.sidebar-desktop-update__icon,
.sidebar-desktop-update__label { color: var(--accent); }
.sidebar-desktop-update__chevron { color: var(--text-muted); }
.sidebar-desktop-update__label { min-width: 0; flex: 1 1 auto; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sidebar-desktop-update__version {
  color: var(--text-muted);
  font-size: var(--fs-xs);
  max-width: 35%;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
@container (max-width: 190px) {
  .sidebar-desktop-update__version { display: none; }
}
.sidebar-desktop-update:hover { background: var(--sidebar-hover, var(--bg-hover)); }
.sidebar-desktop-update:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
@media (max-width: 768px), (pointer: coarse) {
  .sidebar-desktop-update { min-height: 44px; }
}
</style>
