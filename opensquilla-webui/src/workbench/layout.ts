export const WORKBENCH_WIDTH_STORAGE_KEY = 'opensquilla.workbench.width.v1'
export const WORKBENCH_WIDTH_VERSION = 1
export const WORKBENCH_DEFAULT_WIDTH = 520
export const WORKBENCH_MIN_WIDTH = 360
export const WORKBENCH_CHAT_MIN_WIDTH = 480
export const WORKBENCH_MAX_VIEWPORT_RATIO = 0.7
export const WORKBENCH_SPLIT_MIN_WIDTH = 960
export const WORKBENCH_MOBILE_MAX_WIDTH = 720

export type WorkbenchLayoutMode = 'split' | 'overlay' | 'mobile-dialog'
export type WorkbenchWidthSource = 'default' | 'user'

export interface WorkbenchWidthPreference {
  version: 1
  width: number
  source: WorkbenchWidthSource
}

export interface WorkbenchLayoutInput {
  availableWidth: number
  coarseOnly?: boolean
}

function finiteDimension(value: number): number {
  return Number.isFinite(value) ? Math.max(0, value) : 0
}

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(maximum, Math.max(minimum, value))
}

export function defaultWorkbenchWidthPreference(): WorkbenchWidthPreference {
  return {
    version: WORKBENCH_WIDTH_VERSION,
    width: WORKBENCH_DEFAULT_WIDTH,
    source: 'default',
  }
}

export function normalizeWorkbenchWidthPreference(
  preference: WorkbenchWidthPreference,
): WorkbenchWidthPreference {
  if (
    preference.version !== WORKBENCH_WIDTH_VERSION
    || !Number.isFinite(preference.width)
  ) {
    return defaultWorkbenchWidthPreference()
  }
  if (preference.source === 'default') return defaultWorkbenchWidthPreference()
  return {
    version: WORKBENCH_WIDTH_VERSION,
    width: Math.max(WORKBENCH_MIN_WIDTH, Math.round(preference.width)),
    source: 'user',
  }
}

export function parseWorkbenchWidthPreference(
  raw: string | null,
): WorkbenchWidthPreference {
  if (!raw) return defaultWorkbenchWidthPreference()
  try {
    const value = JSON.parse(raw) as Partial<WorkbenchWidthPreference> | null
    if (
      !value
      || value.version !== WORKBENCH_WIDTH_VERSION
      || typeof value.width !== 'number'
    ) {
      return defaultWorkbenchWidthPreference()
    }
    if (value.source === 'default') return defaultWorkbenchWidthPreference()
    if (value.source !== undefined && value.source !== 'user') {
      return defaultWorkbenchWidthPreference()
    }
    return normalizeWorkbenchWidthPreference({
      version: WORKBENCH_WIDTH_VERSION,
      width: value.width,
      // Preferences written before `source` was introduced represent a user
      // choice and must keep their fixed width across upgrades.
      source: 'user',
    })
  } catch {
    return defaultWorkbenchWidthPreference()
  }
}

export function workbenchLayoutMode(input: WorkbenchLayoutInput): WorkbenchLayoutMode {
  const width = finiteDimension(input.availableWidth)
  if (input.coarseOnly || width <= WORKBENCH_MOBILE_MAX_WIDTH) return 'mobile-dialog'
  if (width < WORKBENCH_SPLIT_MIN_WIDTH) return 'overlay'
  return 'split'
}

/** Maximum split width while preserving the minimum chat reading column. */
export function workbenchDynamicMax(availableWidth: number): number {
  const width = finiteDimension(availableWidth)
  return Math.floor(Math.max(
    WORKBENCH_MIN_WIDTH,
    Math.min(
      width * WORKBENCH_MAX_VIEWPORT_RATIO,
      width - WORKBENCH_CHAT_MIN_WIDTH,
    ),
  ))
}

/**
 * Resolve the visual width without mutating the saved preference. Overlay and
 * mobile modes deliberately do not overwrite the desktop split preference.
 */
export function workbenchEffectiveWidth(
  preference: WorkbenchWidthPreference,
  mode: WorkbenchLayoutMode,
  availableWidth: number,
): number {
  const normalized = normalizeWorkbenchWidthPreference(preference)
  const width = finiteDimension(availableWidth)
  if (mode === 'mobile-dialog') return width
  if (mode === 'overlay') {
    return Math.min(normalized.width, Math.max(0, width - 24))
  }
  const preferredWidth = normalized.source === 'default'
    ? Math.round(width / 2)
    : normalized.width
  return clamp(
    preferredWidth,
    WORKBENCH_MIN_WIDTH,
    workbenchDynamicMax(width),
  )
}
