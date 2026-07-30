import { readonly, ref } from 'vue'

export const TOOL_DETAIL_DISPLAY_MODES = ['auto', 'compact', 'expanded'] as const
export type ToolDetailDisplayMode = typeof TOOL_DETAIL_DISPLAY_MODES[number]

// Version the key so a future shape change can fall back safely instead of
// interpreting an older value with new semantics.
export const TOOL_DETAIL_DISPLAY_STORAGE_KEY = 'opensquilla.appearance.toolDetails.v1'

export function parseToolDetailDisplayMode(value: unknown): ToolDetailDisplayMode {
  return TOOL_DETAIL_DISPLAY_MODES.includes(value as ToolDetailDisplayMode)
    ? value as ToolDetailDisplayMode
    : 'auto'
}

function storage(): Storage | null {
  if (typeof window === 'undefined') return null
  try {
    return window.localStorage
  } catch {
    return null
  }
}

function readStoredMode(): ToolDetailDisplayMode {
  try {
    return parseToolDetailDisplayMode(storage()?.getItem(TOOL_DETAIL_DISPLAY_STORAGE_KEY))
  } catch {
    return 'auto'
  }
}

// One reactive preference shared by Settings and every RunTrace instance in
// this renderer. Missing or unknown values preserve the pre-setting behavior.
const mode = ref<ToolDetailDisplayMode>(readStoredMode())

function setMode(next: ToolDetailDisplayMode): void {
  const normalized = parseToolDetailDisplayMode(next)
  mode.value = normalized
  try {
    storage()?.setItem(TOOL_DETAIL_DISPLAY_STORAGE_KEY, normalized)
  } catch {
    // Restricted browser contexts still keep the preference for this page.
  }
}

export function useToolDetailPreference() {
  return {
    mode: readonly(mode),
    setMode,
  }
}
