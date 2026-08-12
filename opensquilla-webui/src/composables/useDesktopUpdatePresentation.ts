import { computed, type ComputedRef } from 'vue'
import { useI18n } from 'vue-i18n'
import type { DesktopUpdateStatus } from '@/platform'
import type { IconName } from '@/utils/icons'
import type { SystemSeverity } from '@/utils/headerLayout'
import type { DesktopUpdateController } from './useDesktopUpdate'

export type DesktopUpdatePresentationSource = Pick<
  DesktopUpdateController,
  'state' | 'actionBusy' | 'latestVersion' | 'localizedError'
>

export interface DesktopUpdatePresentation {
  status: ComputedRef<DesktopUpdateStatus>
  latestVersion: ComputedRef<string>
  manualInstall: ComputedRef<boolean>
  canInstall: ComputedRef<boolean>
  busy: ComputedRef<boolean>
  summary: ComputedRef<string>
  indicatorLabel: ComputedRef<string>
  title: ComputedRef<string>
  description: ComputedRef<string>
  iconName: ComputedRef<IconName>
  severity: ComputedRef<SystemSeverity>
}

/**
 * Translate desktop update lifecycle state into topbar urgency.
 *
 * Non-error update states remain informational: an available or downloaded
 * release is optional user work, not a warning. States excluded from the
 * topbar are normal so consumers cannot accidentally create false attention.
 */
export function desktopUpdateSeverity(status: DesktopUpdateStatus): SystemSeverity {
  if (status === 'error') return 'danger'
  if (status === 'available' || status === 'downloading' || status === 'downloaded') {
    return 'info'
  }
  return 'normal'
}

/**
 * Reusable, localized view state for desktop update controls.
 *
 * Lifecycle and actions stay with useDesktopUpdate. Accepting that controller
 * explicitly keeps this layer presentation-only and prevents a second consumer
 * from starting platform work as a side effect.
 */
export function useDesktopUpdatePresentation(
  update: DesktopUpdatePresentationSource,
): DesktopUpdatePresentation {
  const { t } = useI18n()
  const status = computed(() => update.state.value.status)
  const latestVersion = computed(() => update.latestVersion.value)
  const manualInstall = computed(() => update.state.value.installMode === 'manual')
  const canInstall = computed(() => update.state.value.installMode !== 'unsupported')
  const busy = computed(() => (
    update.actionBusy.value
    || status.value === 'downloading'
    || status.value === 'applying'
  ))
  const progressText = computed(() => {
    const progress = update.state.value.progress
    return typeof progress === 'number' ? String(Math.round(progress)) : ''
  })

  const summary = computed(() => {
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

  const iconName = computed<IconName>(() => {
    if (status.value === 'downloaded') return 'check'
    if (status.value === 'downloading') return 'refresh'
    if (status.value === 'error') return 'info'
    return 'download'
  })
  const severity = computed(() => desktopUpdateSeverity(status.value))

  return {
    status,
    latestVersion,
    manualInstall,
    canInstall,
    busy,
    summary,
    indicatorLabel: summary,
    title,
    description,
    iconName,
    severity,
  }
}
