import { readonly, ref } from 'vue'

export const TOOL_DETAIL_DISPLAY_MODES = ['collapsed', 'expanded'] as const
export type ToolDetailDisplayMode = typeof TOOL_DETAIL_DISPLAY_MODES[number]

// Keep the existing key so an explicit expanded preference survives the move
// to two defaults. Legacy auto/compact values now mean collapsed.
export const TOOL_DETAIL_DISPLAY_STORAGE_KEY = 'opensquilla.appearance.toolDetails.v1'

export function parseToolDetailDisplayMode(value: unknown): ToolDetailDisplayMode {
  return TOOL_DETAIL_DISPLAY_MODES.includes(value as ToolDetailDisplayMode)
    ? value as ToolDetailDisplayMode
    : 'collapsed'
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
    return 'collapsed'
  }
}

// One reactive preference shared by Settings and every RunTrace instance in
// this renderer. Missing, legacy auto/compact, and unknown values start collapsed.
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
