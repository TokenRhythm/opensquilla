import { readonly, ref } from 'vue'

export const ROUTER_VISUAL_EFFECTS_STORAGE_KEY = 'opensquilla.routerFx'

function storage(): Storage | null {
  try {
    return globalThis.localStorage ?? null
  } catch {
    return null
  }
}

function readStoredPreference(): boolean {
  try {
    const saved = storage()?.getItem(ROUTER_VISUAL_EFFECTS_STORAGE_KEY)
    if (!saved) return true
    const parsed = JSON.parse(saved) as { enabled?: unknown }
    return typeof parsed.enabled === 'boolean' ? parsed.enabled : true
  } catch {
    return true
  }
}

// Settings and Chat share one renderer-local preference so changing it updates
// an open conversation immediately. Keep the existing key and payload shape so
// upgrades preserve the user's current choice.
const enabled = ref(readStoredPreference())

function setEnabled(next: boolean): void {
  enabled.value = Boolean(next)
  try {
    storage()?.setItem(ROUTER_VISUAL_EFFECTS_STORAGE_KEY, JSON.stringify({
      enabled: enabled.value,
      variant: 'default',
    }))
  } catch {
    // Restricted browser contexts still keep the preference for this page.
  }
}

export function useRouterVisualEffectsPreference() {
  return {
    enabled: readonly(enabled),
    setEnabled,
  }
}
