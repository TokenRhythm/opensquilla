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

/** Keep background preparation quiet; downloaded is the shell's verified-byte state. */
export function desktopUpdateReady(value: DesktopUpdateState): boolean {
  return value.status === 'downloaded'
    && Boolean(value.latestVersion?.trim())
    && !value.error && !value.errorCode
    && value.installMode !== 'unsupported'
    && (value.canInstall ?? value.installMode === 'native')
}

export function desktopUpdateEntryAvailable(value: DesktopUpdateState): boolean {
  if (desktopUpdateReady(value)) return true
  // Older/manual-only shells still need a route to initiate download or reveal
  // their installer. Never infer this capability from temporary update status.
  return value.canAutoDownload !== true
    && Boolean(value.latestVersion?.trim())
    && !value.error && !value.errorCode
    && (value.status === 'available' || value.status === 'downloaded')
}

const state = ref<DesktopUpdateState>({ ...idleUpdateState })
const ready = ref(false)
const loading = ref(false)
const actionBusy = ref(false)

let initialized = false
const visibilityNow = ref(Date.now())
let snoozeTimer: ReturnType<typeof setTimeout> | undefined

function refreshSnoozeDeadline() {
  if (snoozeTimer !== undefined) clearTimeout(snoozeTimer)
  snoozeTimer = undefined
  visibilityNow.value = Date.now()
  const expiresAt = Date.parse(state.value.snoozedUntil || '')
  if (Number.isFinite(expiresAt) && expiresAt > visibilityNow.value) {
    // A downloaded native update emits no further checks. Wake the existing
    // reminder at its deadline rather than waiting for an unrelated state event.
    snoozeTimer = setTimeout(refreshSnoozeDeadline,
      Math.min(expiresAt - visibilityNow.value, 2_147_483_647))
  }
}

function updateState(next: DesktopUpdateState) {
  state.value = { ...idleUpdateState, ...next }
  refreshSnoozeDeadline()
  ready.value = true
}

function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

function snoozeActive(value: DesktopUpdateState): boolean {
  if (!value.snoozedUntil) return false
  const expiresAt = Date.parse(value.snoozedUntil)
  return Number.isFinite(expiresAt) && expiresAt > visibilityNow.value
}

function updateErrorTranslationKey(code: DesktopUpdateErrorCode): string {
  if (code === 'source_unreachable') return 'updates.desktop.errorSourceUnreachable'
  if (code === 'manifest_invalid') return 'updates.desktop.errorManifestInvalid'
  if (code === 'checksum_unavailable') return 'updates.desktop.errorChecksumUnavailable'
  if (code === 'integrity_failed') return 'updates.desktop.errorIntegrityFailed'
  if (code === 'signature_invalid') return 'updates.desktop.errorSignatureInvalid'
  if (code === 'signature_unavailable') return 'updates.desktop.errorSignatureUnavailable'
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
    platform.updates.onState(updateState)
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
  const snoozed = computed(() => snoozeActive(state.value))
  const visible = computed(() =>
    isManagedDesktopUpdate.value &&
    desktopUpdateEntryAvailable(state.value) &&
    !snoozed.value,
  )
  const latestVersion = computed(() => state.value.latestVersion || state.value.currentVersion || '')
  const localizedError = computed(() => {
    // The main process supplies a localized recovery instruction for installer
    // handoff failures (for example, multiple installed copies).
    if (state.value.errorCode === 'install_failed' && state.value.error) return state.value.error
    if (state.value.errorCode) return t(updateErrorTranslationKey(state.value.errorCode))
    return state.value.error || t('updates.desktop.errorFallback')
  })

  return {
    state,
    ready,
    loading,
    actionBusy,
    visible,
    snoozed,
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

export type DesktopUpdateController = ReturnType<typeof useDesktopUpdate>
