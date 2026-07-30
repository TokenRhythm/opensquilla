<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import { useDesktopUpdate } from '@/composables/useDesktopUpdate'

const { t } = useI18n()
const update = useDesktopUpdate()

onMounted(update.init)

const state = computed(() => update.state.value)
const status = computed(() => state.value.status)
const latestVersion = computed(() => update.latestVersion.value || t('updates.desktop.unknownVersion'))
const currentVersion = computed(() => state.value.currentVersion || t('updates.desktop.unknownVersion'))
const manualInstall = computed(() => state.value.installMode === 'manual')
const busy = computed(() => update.loading.value || update.actionBusy.value || status.value === 'downloading' || status.value === 'applying')
const sourceLabel = computed(() => {
  if (state.value.source === 'oss') return t('updates.desktop.sourceOss')
  if (state.value.source === 'github') return t('updates.desktop.sourceGithub')
  return ''
})

const statusTone = computed(() => {
  if (status.value === 'downloaded') return 'control-pill--ok'
  if (status.value === 'available' || status.value === 'downloading' || status.value === 'checking') return 'control-pill--accent'
  if (status.value === 'error') return 'control-pill--danger'
  return 'control-pill--info'
})

const statusLabel = computed(() => {
  if (!state.value.canCheck) return t('updates.desktop.unavailableStatus')
  if (status.value === 'downloaded' && manualInstall.value) return t('updates.desktop.manualDownloadedStatus')
  if (status.value === 'available' && manualInstall.value) return t('updates.desktop.manualAvailableStatus')
  if (status.value === 'available') return t('updates.desktop.availableStatus')
  if (status.value === 'downloading') return t('updates.desktop.downloadingStatus')
  if (status.value === 'downloaded') return t('updates.desktop.downloadedStatus')
  if (status.value === 'checking') return t('updates.desktop.checkingStatus')
  if (status.value === 'not-available') return t('updates.desktop.upToDateStatus')
  if (status.value === 'error') return t('updates.desktop.errorStatus')
  if (status.value === 'applying') return t('updates.desktop.applyingStatus')
  return t('updates.desktop.idleStatus')
})

const description = computed(() => {
  if (!state.value.canCheck) return t('updates.desktop.unsupported')
  if (status.value === 'available' && manualInstall.value) return t('updates.desktop.manualAvailableDesc')
  if (status.value === 'available') return t('updates.desktop.availableDesc')
  if (status.value === 'downloaded') {
    return manualInstall.value
      ? t('updates.desktop.manualDownloadedDesc', { version: latestVersion.value })
      : t('updates.desktop.downloadedDesc', { version: latestVersion.value })
  }
  if (status.value === 'downloading') return t('updates.desktop.downloadingDesc')
  if (status.value === 'not-available') return t('updates.desktop.upToDateDesc', { version: currentVersion.value })
  if (status.value === 'error') return update.localizedError.value
  if (status.value === 'checking') return t('updates.desktop.checkingDesc')
  if (status.value === 'applying') return t('updates.desktop.applyingDesc')
  if (manualInstall.value) return t('updates.desktop.manualIdleDesc')
  return t('updates.desktop.idleDesc')
})

const showDownload = computed(() => state.value.canCheck && state.value.installMode !== 'unsupported' && (
  status.value === 'available' || (manualInstall.value && status.value === 'downloaded')
))
const showRelaunch = computed(() => state.value.installMode === 'native' && status.value === 'downloaded')
const showLater = computed(() => state.value.canCheck && (status.value === 'available' || status.value === 'downloaded' || status.value === 'error'))
</script>

<template>
  <div class="control-row control-row--stack settings-update">
    <div class="control-row__label-block">
      <span class="control-row__label">
        {{ t('updates.desktop.settingsTitle') }}
        <span class="control-pill" :class="statusTone">{{ statusLabel }}</span>
      </span>
      <span class="control-row__desc">{{ description }}</span>
    </div>

    <div class="settings-update__meta">
      <span>
        <strong>{{ t('updates.desktop.currentVersion') }}</strong>
        {{ currentVersion }}
      </span>
      <span v-if="state.latestVersion">
        <strong>{{ t('updates.desktop.latestVersion') }}</strong>
        {{ latestVersion }}
      </span>
      <span v-if="sourceLabel">
        <strong>{{ t('updates.desktop.source') }}</strong>
        {{ sourceLabel }}<template v-if="state.fallbackUsed"> · {{ t('updates.desktop.fallbackUsed') }}</template>
      </span>
    </div>

    <div class="control-row__control settings-update__actions">
      <button
        type="button"
        class="btn btn--ghost"
        :disabled="busy || !state.canCheck"
        @click="update.check"
      >
        <Icon name="refresh" :size="15" aria-hidden="true" />
        <span>{{ t('updates.desktop.check') }}</span>
      </button>
      <button
        v-if="showDownload"
        type="button"
        class="btn btn--primary"
        data-testid="settings-update-download"
        :disabled="busy"
        @click="update.download"
      >
        <Icon name="download" :size="15" aria-hidden="true" />
        <span>{{
          manualInstall
            ? status === 'downloaded'
              ? t('updates.desktop.showInstaller')
              : t('updates.desktop.downloadInstaller')
            : t('updates.desktop.download')
        }}</span>
      </button>
      <button
        v-if="showRelaunch"
        type="button"
        class="btn btn--primary"
        data-testid="settings-update-relaunch"
        :disabled="busy"
        @click="update.relaunch"
      >
        <Icon name="refresh" :size="15" aria-hidden="true" />
        <span>{{ t('updates.desktop.relaunch') }}</span>
      </button>
      <button
        v-if="showLater"
        type="button"
        class="btn btn--ghost"
        :disabled="busy"
        @click="update.dismiss"
      >
        {{ t('updates.desktop.later') }}
      </button>
    </div>
  </div>
</template>

<style scoped>
.settings-update {
  align-items: stretch;
}

.settings-update .control-row__label {
  align-items: center;
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
}

.settings-update__meta {
  color: var(--text-muted);
  display: flex;
  flex-wrap: wrap;
  font-size: var(--fs-xs);
  gap: var(--sp-3);
}

.settings-update__meta strong {
  color: var(--text-dim);
  font-weight: 600;
  margin-right: 4px;
}

.settings-update__actions {
  justify-content: flex-start;
}
</style>
