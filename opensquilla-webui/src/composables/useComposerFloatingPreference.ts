import { readonly, ref } from 'vue'

export const COMPOSER_FX_STORAGE_KEY = 'opensquilla.composerFx'

function storage(): Storage | null {
  try {
    return globalThis.localStorage ?? null
  } catch {
    return null
  }
}

function readStoredPreference(): boolean {
  try {
    const saved = storage()?.getItem(COMPOSER_FX_STORAGE_KEY)
    if (!saved) return true
    const parsed = JSON.parse(saved) as { enabled?: unknown }
    return typeof parsed.enabled === 'boolean' ? parsed.enabled : true
  } catch {
    return true
  }
}

// Settings → Appearance and the conversation view share one renderer-local
// preference, so flipping the toggle updates an open chat immediately. On by
// default (the floating/retracting composer is the established behaviour);
// turning it off docks the composer in the normal layout and disables the
// scroll-based retract.
const enabled = ref(readStoredPreference())

function setEnabled(next: boolean): void {
  enabled.value = Boolean(next)
  try {
    storage()?.setItem(COMPOSER_FX_STORAGE_KEY, JSON.stringify({
      enabled: enabled.value,
    }))
  } catch {
    // Restricted browser contexts still keep the preference for this page.
  }
}

export function useComposerFloatingPreference() {
  return {
    enabled: readonly(enabled),
    setEnabled,
  }
}
