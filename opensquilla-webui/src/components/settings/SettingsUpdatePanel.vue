<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import { useDesktopUpdate } from '@/composables/useDesktopUpdate'
import { useDesktopUpdatePresentation } from '@/composables/useDesktopUpdatePresentation'

const { t } = useI18n()
const update = useDesktopUpdate()
const { canInstall, retryError } = useDesktopUpdatePresentation(update)

onMounted(update.init)

const state = computed(() => update.state.value)
const status = computed(() => state.value.status)
const latestVersion = computed(() => update.latestVersion.value || t('updates.desktop.unknownVersion'))
const currentVersion = computed(() => state.value.currentVersion || t('updates.desktop.unknownVersion'))
const manualInstall = computed(() => state.value.installMode === 'manual')
const busy = computed(() => update.loading.value || update.actionBusy.value || status.value === 'downloading' || status.value === 'applying')
const progress = computed(() => typeof state.value.progress === 'number' && Number.isFinite(state.value.progress)
  ? Math.min(100, Math.max(0, Math.round(state.value.progress)))
  : null)
const sourceLabel = computed(() => {
  if (state.value.source === 'oss') return t('updates.desktop.sourceOss')
  if (state.value.source === 'github') return t('updates.desktop.sourceGithub')
  return ''
})

const statusTone = computed(() => {
  if (retryError.value) return 'control-pill--danger'
  if (status.value === 'downloaded') return 'control-pill--ok'
  if (status.value === 'available' || status.value === 'downloading' || status.value === 'checking') return 'control-pill--accent'
  if (status.value === 'error') return 'control-pill--danger'
  return 'control-pill--info'
})

const statusLabel = computed(() => {
  if (!state.value.canCheck) return t('updates.desktop.unavailableStatus')
  if (retryError.value) return t('updates.desktop.errorStatus')
  if (status.value === 'downloaded' && manualInstall.value) return t('updates.desktop.manualDownloadedStatus')
  if (status.value === 'available' && manualInstall.value) return t('updates.desktop.manualAvailableStatus')
  if (status.value === 'available') return t('updates.desktop.availableStatus')
  if (status.value === 'downloading') return t('updates.desktop.downloadingStatus')
  if (status.value === 'downloaded') return t('updates.desktop.downloadedStatus')
  if (status.value === 'checking') return t('updates.desktop.checkingStatus')
  if (status.value === 'not-available') return t('updates.desktop.upToDateStatus')
  if (status.value === 'error') return t('updates.desktop.errorStatus')
  if (status.value === 'applying') return t(manualInstall.value ? 'updates.desktop.manualApplyingStatus' : 'updates.desktop.applyingStatus')
  return t('updates.desktop.idleStatus')
})

const description = computed(() => {
  if (!state.value.canCheck) return t('updates.desktop.unsupported')
  if (retryError.value) return update.localizedError.value
  if (status.value === 'available' && manualInstall.value) return t('updates.desktop.manualAvailableDesc')
  if (status.value === 'available') return t('updates.desktop.availableDesc')
  if (status.value === 'downloaded') {
    return manualInstall.value
      ? t(canInstall.value ? 'updates.desktop.signedDownloadedDesc' : 'updates.desktop.manualDownloadedDesc', { version: latestVersion.value })
      : t('updates.desktop.downloadedDesc', { version: latestVersion.value })
  }
  if (status.value === 'downloading') return t('updates.desktop.downloadingDesc')
  if (status.value === 'not-available') return t('updates.desktop.upToDateDesc', { version: currentVersion.value })
  if (status.value === 'error') return update.localizedError.value
  if (status.value === 'checking') return t('updates.desktop.checkingDesc')
  if (status.value === 'applying') return t(manualInstall.value ? 'updates.desktop.manualApplyingDesc' : 'updates.desktop.applyingDesc')
  if (manualInstall.value) return t('updates.desktop.manualIdleDesc')
  return t('updates.desktop.idleDesc')
})

const showDownload = computed(() => state.value.canCheck && state.value.installMode !== 'unsupported' && (
  status.value === 'available' || (manualInstall.value && status.value === 'downloaded')
))
const releaseNotesUrl = computed(() => {
  const value = state.value.releaseNotesUrl
  if (!value || !state.value.latestVersion) return null
  try {
    const url = new URL(value)
    return url.protocol === 'https:' && !url.username && !url.password ? url.href : null
  } catch { return null }
})
const showRelaunch = computed(() => (canInstall.value && status.value === 'downloaded')
  || (state.value.installMode !== 'unsupported' && status.value === 'applying'))
const showLater = computed(() => state.value.canCheck && (status.value === 'available' || status.value === 'downloaded' || status.value === 'error'))
</script>

<template>
  <div class="control-row control-row--stack settings-update">
    <div class="control-row__label-block">
      <h3 id="settings-gateway-updates-title" class="control-row__label settings-update__title">
        {{ t('updates.desktop.settingsTitle') }}
        <span class="control-pill" :class="statusTone">{{ statusLabel }}</span>
      </h3>
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

    <div v-if="status === 'downloading'" class="settings-update__progress">
      <div
        class="settings-update__track"
        :class="{ 'is-indeterminate': progress === null }"
        role="progressbar"
        :aria-label="t('updates.desktop.downloadingStatus')"
        :aria-valuenow="progress ?? undefined"
        :aria-valuemin="0"
        :aria-valuemax="100"
      ><span :style="progress === null ? undefined : { width: `${progress}%` }"></span></div>
      <span v-if="progress !== null" class="settings-update__percentage">{{ progress }}%</span>
    </div>

    <div class="control-row__control settings-update__actions">
      <button
        v-if="showRelaunch"
        type="button"
        class="btn btn--primary"
        data-testid="settings-update-relaunch"
        :disabled="busy"
        :aria-busy="status === 'applying' || undefined"
        @click="update.relaunch"
      >
        <Icon name="refresh" :size="15" aria-hidden="true" />
        <span>{{ t(status === 'applying'
          ? manualInstall ? 'updates.desktop.manualApplyingStatus' : 'updates.desktop.applyingStatus'
          : manualInstall ? 'updates.desktop.quitAndInstall' : 'updates.desktop.relaunch') }}</span>
      </button>
      <button
        v-if="status === 'error'"
        type="button"
        class="btn btn--primary"
        data-testid="settings-update-check"
        :disabled="busy || !state.canCheck"
        @click="update.check"
      >
        <Icon name="refresh" :size="15" aria-hidden="true" />
        <span>{{ t('updates.desktop.retry') }}</span>
      </button>
      <button
        v-if="showLater"
        data-testid="settings-update-later"
        type="button"
        class="btn btn--ghost"
        :disabled="busy || update.snoozed.value"
        @click="update.dismiss"
      >
        {{ t(update.snoozed.value ? 'updates.desktop.reminderSnoozed' : 'updates.desktop.later') }}
      </button>
      <button
        v-if="showDownload"
        type="button"
        class="btn"
        :class="showRelaunch ? 'btn--ghost' : 'btn--primary'"
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
        v-if="status !== 'error'"
        type="button"
        class="btn btn--ghost"
        data-testid="settings-update-check"
        :disabled="busy || !state.canCheck"
        @click="update.check"
      >
        <Icon name="refresh" :size="15" aria-hidden="true" />
        <span>{{ t('updates.desktop.check') }}</span>
      </button>
    </div>
    <a
      v-if="releaseNotesUrl"
      :href="releaseNotesUrl"
      target="_blank"
      rel="noopener noreferrer"
      class="settings-update__release-notes"
      data-testid="settings-update-release-notes"
    >
      {{ t('updates.desktop.viewReleaseNotes') }}
      <Icon name="externalLink" :size="14" aria-hidden="true" />
    </a>
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

.settings-update__title {
  font-size: var(--fs-md);
  margin: 0;
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

.settings-update__release-notes {
  align-self: flex-start;
  display: inline-flex;
  align-items: center;
  gap: var(--sp-1);
}

.settings-update__actions {
  flex-wrap: wrap;
  justify-content: flex-start;
}

.settings-update__progress { display: flex; align-items: center; gap: var(--sp-3); max-width: 420px; }
.settings-update__track { flex: 1; height: 4px; background: var(--bg-hover); border-radius: var(--radius-full); overflow: hidden; }
.settings-update__track span { display: block; height: 100%; background: var(--accent); border-radius: inherit; }
.settings-update__track.is-indeterminate span { width: 35%; animation: settings-update-progress var(--dur-pulse) var(--ease-standard) infinite alternate; }
.settings-update__percentage { color: var(--text-muted); font-size: var(--fs-xs); font-variant-numeric: tabular-nums; }
@keyframes settings-update-progress { to { transform: translateX(180%); } }
@media (prefers-reduced-motion: reduce) { .settings-update__track.is-indeterminate span { animation: none; } }
</style>
