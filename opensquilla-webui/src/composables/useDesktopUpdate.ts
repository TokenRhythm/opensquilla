import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { getPlatform, type DesktopUpdateErrorCode, type DesktopUpdateState } from '@/platform'

const idleUpdateState: DesktopUpdateState = {
  status: 'idle',
  currentVersion: '',
  latestVersion: null,
  progress: null,
  checkedAt: null,
  error: null,
  errorCode: null,
  snoozedUntil: null,
  canCheck: false,
  canNativeInstall: false,
  installMode: 'unsupported',
  releaseUrl: null,
  source: null,
  fallbackUsed: false,
}

const TOPBAR_STATUSES = new Set(['available', 'downloading', 'downloaded', 'error'])

const state = ref<DesktopUpdateState>({ ...idleUpdateState })
const ready = ref(false)
const loading = ref(false)
const actionBusy = ref(false)

let initialized = false
let unsubscribe: (() => void) | null = null

function updateState(next: DesktopUpdateState) {
  state.value = { ...idleUpdateState, ...next }
  ready.value = true
}

function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

function snoozeActive(value: DesktopUpdateState): boolean {
  if (!value.snoozedUntil) return false
  const expiresAt = Date.parse(value.snoozedUntil)
  return Number.isFinite(expiresAt) && expiresAt > Date.now()
}

function updateErrorTranslationKey(code: DesktopUpdateErrorCode): string {
  if (code === 'source_unreachable') return 'updates.desktop.errorSourceUnreachable'
  if (code === 'manifest_invalid') return 'updates.desktop.errorManifestInvalid'
  if (code === 'checksum_unavailable') return 'updates.desktop.errorChecksumUnavailable'
  if (code === 'integrity_failed') return 'updates.desktop.errorIntegrityFailed'
  if (code === 'download_failed') return 'updates.desktop.errorDownloadFailed'
  return 'updates.desktop.errorInstallFailed'
}

async function runAction(action: () => Promise<DesktopUpdateState>) {
  actionBusy.value = true
  try {
    updateState(await action())
  } catch (err) {
    updateState({
      ...state.value,
      status: 'error',
      error: errorMessage(err),
      errorCode: null,
      progress: null,
    })
  } finally {
    actionBusy.value = false
  }
}

async function refreshDesktopUpdate() {
  const platform = getPlatform()
  if (!platform.capabilities.isDesktop) {
    updateState({ ...idleUpdateState })
    return
  }
  loading.value = true
  try {
    updateState(await platform.updates.getState())
  } catch (err) {
    updateState({
      ...idleUpdateState,
      status: 'error',
      error: errorMessage(err),
      errorCode: null,
      canCheck: true,
      canNativeInstall: true,
      installMode: 'native',
    })
  } finally {
    loading.value = false
  }
}

function initDesktopUpdate() {
  if (initialized) return
  initialized = true
  const platform = getPlatform()
  if (platform.capabilities.isDesktop) {
    unsubscribe = platform.updates.onState(updateState)
  }
  void refreshDesktopUpdate()
}

export function useDesktopUpdate() {
  const { t } = useI18n()
  const platform = getPlatform()
  const isNativeDesktopUpdate = computed(() => platform.capabilities.isDesktop && state.value.canNativeInstall)
  const isManagedDesktopUpdate = computed(() => (
    platform.capabilities.isDesktop
    && state.value.canCheck
  ))
  const visible = computed(() =>
    isManagedDesktopUpdate.value &&
    TOPBAR_STATUSES.has(state.value.status) &&
    !snoozeActive(state.value),
  )
  const latestVersion = computed(() => state.value.latestVersion || state.value.currentVersion || '')
  const localizedError = computed(() => {
    if (state.value.errorCode) return t(updateErrorTranslationKey(state.value.errorCode))
    return state.value.error || t('updates.desktop.errorFallback')
  })

  return {
    state,
    ready,
    loading,
    actionBusy,
    visible,
    latestVersion,
    localizedError,
    isNativeDesktopUpdate,
    isManagedDesktopUpdate,
    init: initDesktopUpdate,
    refresh: refreshDesktopUpdate,
    check: () => runAction(() => platform.updates.check()),
    download: () => runAction(() => platform.updates.download()),
    relaunch: () => runAction(() => platform.updates.relaunch()),
    dismiss: () => runAction(() => platform.updates.dismiss()),
  }
}

export function stopDesktopUpdateSubscriptionForTests() {
  unsubscribe?.()
  unsubscribe = null
  initialized = false
}
