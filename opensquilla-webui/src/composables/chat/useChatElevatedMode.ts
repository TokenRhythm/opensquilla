import { computed, ref, type Ref } from 'vue'
import {
  ApprovalCenterError,
  type ApprovalCenter,
} from '@/modules/approvalCenter'

const ELEVATED_MODE_KEY = 'opensquilla.elevatedMode'
const ELEVATED_MODE_VERSION_KEY = 'opensquilla.elevatedMode.version'
const ELEVATED_MODE_STORAGE_VERSION = '2'

export interface SetElevatedModeOptions {
  persist?: boolean
  sync?: boolean
}

export interface UseChatElevatedModeOptions {
  sessionKey: Ref<string>
  approvalCenter: Pick<ApprovalCenter, 'setElevatedMode'>
}

export function normalizeElevatedMode(mode: string): string {
  return mode === 'on' || mode === 'bypass' || mode === 'full' ? mode : ''
}

export function isApprovalBypassMode(mode: string): boolean {
  return mode === 'bypass' || mode === 'full'
}

export function useChatElevatedMode(options: UseChatElevatedModeOptions) {
  const elevatedMode = ref('')
  const globalElevatedMode = ref('')
  const elevatedUnavailable = ref(false)

  const effectiveElevatedMode = computed(() => {
    const mode = elevatedMode.value || globalElevatedMode.value
    return normalizeElevatedMode(mode)
  })

  function loadElevatedMode() {
    let mode = ''
    let version = ''
    try {
      mode = localStorage.getItem(ELEVATED_MODE_KEY) || ''
      version = localStorage.getItem(ELEVATED_MODE_VERSION_KEY) || ''
    } catch {}
    if (mode === 'full' && version !== ELEVATED_MODE_STORAGE_VERSION) {
      mode = 'bypass'
      try {
        localStorage.setItem(ELEVATED_MODE_KEY, mode)
        localStorage.setItem(ELEVATED_MODE_VERSION_KEY, ELEVATED_MODE_STORAGE_VERSION)
      } catch {}
    }
    setElevatedMode(mode, { persist: false, sync: true })
  }

  function setElevatedMode(mode: string, modeOptions: SetElevatedModeOptions = {}) {
    const normalized = normalizeElevatedMode(mode)
    elevatedMode.value = normalized
    if (modeOptions.persist !== false) {
      try {
        if (normalized) {
          localStorage.setItem(ELEVATED_MODE_KEY, normalized)
          localStorage.setItem(ELEVATED_MODE_VERSION_KEY, ELEVATED_MODE_STORAGE_VERSION)
        } else {
          localStorage.removeItem(ELEVATED_MODE_KEY)
          localStorage.removeItem(ELEVATED_MODE_VERSION_KEY)
        }
      } catch {}
    }
    if (modeOptions.sync) syncElevatedMode(normalized)
  }

  async function syncElevatedMode(mode: string) {
    if (!options.sessionKey.value || elevatedUnavailable.value) return
    try {
      await options.approvalCenter.setElevatedMode(
        options.sessionKey.value,
        (mode || 'off') as 'off' | 'on' | 'bypass' | 'full',
      )
    } catch (err: unknown) {
      if (err instanceof ApprovalCenterError && err.kind === 'forbidden') {
        elevatedUnavailable.value = true
        try {
          localStorage.removeItem(ELEVATED_MODE_KEY)
          localStorage.removeItem(ELEVATED_MODE_VERSION_KEY)
        } catch {}
        elevatedMode.value = ''
        console.warn('Bypass requires a local owner session (loopback only).')
        return
      }
      console.warn('Failed to sync bypass mode:', err instanceof Error ? err.message : String(err))
    }
  }

  function setGlobalElevatedMode(mode: string) {
    globalElevatedMode.value = normalizeElevatedMode(mode)
  }

  return {
    elevatedMode,
    globalElevatedMode,
    effectiveElevatedMode,
    elevatedUnavailable,
    loadElevatedMode,
    setElevatedMode,
    syncElevatedMode,
    setGlobalElevatedMode,
    normalizeElevatedMode,
    isApprovalBypassMode,
  }
}
