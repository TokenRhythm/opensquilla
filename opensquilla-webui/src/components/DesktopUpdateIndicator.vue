<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from './Icon.vue'
import { useDesktopUpdate } from '@/composables/useDesktopUpdate'
import { useDocumentEvent } from '@/composables/useDocumentEvent'

const { t } = useI18n()
const update = useDesktopUpdate()
const open = ref(false)
const triggerRef = ref<HTMLButtonElement | null>(null)
const popoverStyle = ref<Record<string, string>>({})

onMounted(update.init)

const status = computed(() => update.state.value.status)
const latestVersion = computed(() => update.latestVersion.value)
const manualInstall = computed(() => update.state.value.installMode === 'manual')
const canInstall = computed(() => update.state.value.installMode !== 'unsupported')
const busy = computed(() => update.actionBusy.value || status.value === 'downloading' || status.value === 'applying')
const progressText = computed(() => {
  const progress = update.state.value.progress
  return typeof progress === 'number' ? String(Math.round(progress)) : ''
})

const indicatorLabel = computed(() => {
  if (status.value === 'downloaded') {
    return manualInstall.value
      ? t('updates.desktop.indicatorInstallerReady')
      : t('updates.desktop.indicatorDownloaded')
  }
  if (status.value === 'downloading') {
    return progressText.value
      ? t('updates.desktop.indicatorDownloadingProgress', { progress: progressText.value })
      : t('updates.desktop.indicatorDownloading')
  }
  if (status.value === 'error') return t('updates.desktop.indicatorError')
  return t('updates.desktop.indicatorAvailable', { version: latestVersion.value })
})

const title = computed(() => {
  if (status.value === 'downloaded') {
    return manualInstall.value
      ? t('updates.desktop.manualDownloadedTitle')
      : t('updates.desktop.downloadedTitle')
  }
  if (status.value === 'downloading') return t('updates.desktop.downloadingTitle')
  if (status.value === 'error') return t('updates.desktop.errorTitle')
  return t('updates.desktop.availableTitle', { version: latestVersion.value })
})

const description = computed(() => {
  if (status.value === 'downloaded') {
    return manualInstall.value
      ? t('updates.desktop.manualDownloadedDesc', { version: latestVersion.value })
      : t('updates.desktop.downloadedDesc', { version: latestVersion.value })
  }
  if (status.value === 'downloading') return t('updates.desktop.downloadingDesc')
  if (status.value === 'error') return update.localizedError.value
  if (manualInstall.value) return t('updates.desktop.manualAvailableDesc')
  return t('updates.desktop.availableDesc')
})

const iconName = computed(() => {
  if (status.value === 'downloaded') return 'check'
  if (status.value === 'downloading') return 'refresh'
  if (status.value === 'error') return 'info'
  return 'download'
})

function positionPopover() {
  const trigger = triggerRef.value
  if (!trigger) return
  const rect = trigger.getBoundingClientRect()
  if (window.innerWidth <= 768) {
    popoverStyle.value = {
      position: 'fixed',
      left: 'var(--sp-3)',
      right: 'var(--sp-3)',
      top: `${rect.bottom + 8}px`,
    }
    return
  }
  popoverStyle.value = {
    position: 'fixed',
    right: `${Math.max(12, window.innerWidth - rect.right)}px`,
    top: `${rect.bottom + 8}px`,
  }
}

async function toggle() {
  open.value = !open.value
  if (open.value) {
    await nextTick()
    positionPopover()
  }
}

async function download() {
  await update.download()
}

async function relaunch() {
  await update.relaunch()
}

async function dismiss() {
  open.value = false
  await update.dismiss()
}

useDocumentEvent('click', (event) => {
  if (!open.value) return
  const target = event.target
  if (target instanceof Element && (target.closest('.desktop-update') || target.closest('.desktop-update__popover'))) return
  open.value = false
})
</script>

<template>
  <div v-if="update.visible.value" class="desktop-update">
    <button
      type="button"
      ref="triggerRef"
      class="desktop-update__trigger"
      data-testid="desktop-update-indicator"
      :aria-expanded="open ? 'true' : 'false'"
      aria-haspopup="dialog"
      :title="title"
      @click.stop="toggle"
    >
      <Icon :name="iconName" :size="14" aria-hidden="true" />
      <span class="desktop-update__label">{{ indicatorLabel }}</span>
    </button>

    <Teleport to="body">
      <div v-if="open" class="desktop-update__popover" :style="popoverStyle" role="dialog" :aria-label="title">
        <div class="desktop-update__head">
          <Icon :name="iconName" :size="16" aria-hidden="true" />
          <strong>{{ title }}</strong>
        </div>
        <p class="desktop-update__desc">{{ description }}</p>
        <div class="desktop-update__actions">
          <button
            v-if="status === 'available' && canInstall"
            type="button"
            class="btn btn--primary"
            data-testid="desktop-update-download"
            :disabled="busy"
            @click="download"
          >
            <Icon name="download" :size="14" aria-hidden="true" />
            <span>{{ manualInstall ? t('updates.desktop.downloadInstaller') : t('updates.desktop.download') }}</span>
          </button>
          <button
            v-if="status === 'downloaded' && update.state.value.installMode === 'native'"
            type="button"
            class="btn btn--primary"
            data-testid="desktop-update-relaunch"
            :disabled="busy"
            @click="relaunch"
          >
            <Icon name="refresh" :size="14" aria-hidden="true" />
            <span>{{ t('updates.desktop.relaunch') }}</span>
          </button>
          <button
            v-if="status === 'downloaded' && manualInstall"
            type="button"
            class="btn btn--primary"
            data-testid="desktop-update-show-installer"
            :disabled="busy"
            @click="download"
          >
            <Icon name="download" :size="14" aria-hidden="true" />
            <span>{{ t('updates.desktop.showInstaller') }}</span>
          </button>
          <button
            v-if="status === 'available' || status === 'downloaded' || status === 'error'"
            type="button"
            class="btn btn--ghost"
            data-testid="desktop-update-later"
            :disabled="busy"
            @click="dismiss"
          >
            {{ t('updates.desktop.later') }}
          </button>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.desktop-update {
  position: relative;
}

.desktop-update__trigger {
  align-items: center;
  background: color-mix(in srgb, var(--accent) 10%, var(--bg-surface));
  border: 1px solid color-mix(in srgb, var(--accent) 38%, var(--border));
  border-radius: var(--radius-full);
  color: var(--accent);
  cursor: pointer;
  display: inline-flex;
  font: inherit;
  font-size: var(--fs-xs);
  font-weight: 650;
  gap: 6px;
  max-width: 180px;
  min-height: 24px;
  padding: 3px 10px;
  white-space: nowrap;
}

.desktop-update__label {
  overflow: hidden;
  text-overflow: ellipsis;
}

.desktop-update__trigger:hover {
  background: color-mix(in srgb, var(--accent) 14%, var(--bg-elevated));
}

.desktop-update__trigger:focus-visible {
  outline: 2px solid color-mix(in srgb, var(--accent) 45%, transparent);
  outline-offset: 2px;
}

.desktop-update__popover {
  background: var(--bg-elevated);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-lg);
  color: var(--text);
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  min-width: 260px;
  padding: var(--sp-3);
  z-index: 1000;
}

.desktop-update__head {
  align-items: center;
  color: var(--text);
  display: flex;
  font-size: var(--fs-sm);
  gap: var(--sp-2);
}

.desktop-update__desc {
  color: var(--text-muted);
  font-size: var(--fs-xs);
  line-height: 1.45;
  margin: 0;
}

.desktop-update__actions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
  justify-content: flex-end;
}

@media (max-width: 768px) {
  .desktop-update__trigger {
    height: 40px;
    justify-content: center;
    max-width: 40px;
    min-height: 40px;
    padding: 0;
    width: 40px;
  }

  .desktop-update__label {
    border: 0;
    clip: rect(0 0 0 0);
    height: 1px;
    margin: -1px;
    overflow: hidden;
    padding: 0;
    position: absolute;
    white-space: nowrap;
    width: 1px;
  }

  .desktop-update__popover {
    min-width: 0;
    width: auto;
  }

  .desktop-update__actions {
    justify-content: stretch;
  }

  .desktop-update__actions .btn {
    flex: 1 1 auto;
  }
}
</style>
