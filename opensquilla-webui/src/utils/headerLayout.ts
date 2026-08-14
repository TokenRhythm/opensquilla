export type SessionHeaderLayout = 'wide' | 'compact' | 'tight'
export type SystemHeaderLayout = 'wide' | 'compact' | 'tight'
export type SystemSeverity = 'normal' | 'info' | 'warning' | 'danger'

export const SESSION_HEADER_INITIAL_WIDE_WIDTH = 576
export const SESSION_HEADER_WIDE_EXIT_WIDTH = 544
export const SESSION_HEADER_INITIAL_TIGHT_WIDTH = 184
export const SESSION_HEADER_TIGHT_EXIT_WIDTH = 216

export const SYSTEM_HEADER_INITIAL_WIDE_WIDTH = 960
export const SYSTEM_HEADER_WIDE_EXIT_WIDTH = 928
export const SYSTEM_HEADER_INITIAL_TIGHT_WIDTH = 400
export const SYSTEM_HEADER_TIGHT_ENTER_WIDTH = 384
export const SYSTEM_HEADER_TIGHT_EXIT_WIDTH = 416
export const SYSTEM_HEADER_PRESSURE_COUNT = 2
export const SYSTEM_HEADER_PRESSURE_WIDE_MIN_WIDTH = 1200

interface HeaderInputCapability {
  mobile?: boolean
  coarseOnly?: boolean
}

export interface SessionHeaderLayoutInput extends HeaderInputCapability {
  availableWidth: number
  previousLayout?: SessionHeaderLayout | null
}

export interface SystemHeaderLayoutInput extends HeaderInputCapability {
  topbarWidth: number
  previousLayout?: SystemHeaderLayout | null
  pressureCount?: number
}

const SYSTEM_SEVERITY_RANK: Readonly<Record<SystemSeverity, number>> = {
  normal: 0,
  info: 1,
  warning: 2,
  danger: 3,
}

function finiteDimension(value: number): number {
  return Number.isFinite(value) ? Math.max(0, value) : 0
}

function wideAllowed(input: HeaderInputCapability): boolean {
  return !input.mobile && !input.coarseOnly
}

function capWide<T extends SessionHeaderLayout | SystemHeaderLayout>(
  layout: T,
  allowWide: boolean,
): T {
  return layout === 'wide' && !allowWide ? 'compact' as T : layout
}

/**
 * Resolve the session-owned title and action layout from its actual host width.
 *
 * The dead bands are intentional Schmitt-trigger hysteresis: a wide header does
 * not collapse until 544px, but a compact header needs 576px to expand again;
 * tight follows the same pattern at 184px/216px. A large one-frame resize may
 * skip an adjacent state so the header never waits for a second observation.
 */
export function resolveSessionHeaderLayout(
  input: SessionHeaderLayoutInput,
): SessionHeaderLayout {
  const width = finiteDimension(input.availableWidth)
  const previous = input.previousLayout ?? null
  let layout: SessionHeaderLayout

  if (previous === 'wide') {
    if (width < SESSION_HEADER_INITIAL_TIGHT_WIDTH) layout = 'tight'
    else if (width < SESSION_HEADER_WIDE_EXIT_WIDTH) layout = 'compact'
    else layout = 'wide'
  } else if (previous === 'compact') {
    if (width < SESSION_HEADER_INITIAL_TIGHT_WIDTH) layout = 'tight'
    else if (width >= SESSION_HEADER_INITIAL_WIDE_WIDTH) layout = 'wide'
    else layout = 'compact'
  } else if (previous === 'tight') {
    if (width >= SESSION_HEADER_INITIAL_WIDE_WIDTH) layout = 'wide'
    else if (width >= SESSION_HEADER_TIGHT_EXIT_WIDTH) layout = 'compact'
    else layout = 'tight'
  } else if (width >= SESSION_HEADER_INITIAL_WIDE_WIDTH) {
    layout = 'wide'
  } else if (width < SESSION_HEADER_INITIAL_TIGHT_WIDTH) {
    layout = 'tight'
  } else {
    layout = 'compact'
  }

  return capWide(layout, wideAllowed(input))
}

function normalizedPressureCount(value: number | undefined): number {
  if (value === undefined) return 0
  return Number.isFinite(value) ? Math.max(0, Math.floor(value)) : 0
}

/**
 * Resolve the App-owned system controls from the full topbar width.
 *
 * Two or more simultaneous status pressures reserve more room for the session
 * header below 1200px by capping this group at compact. Pressure is never a
 * reason to select tight by itself; only width can make that transition.
 */
export function resolveSystemHeaderLayout(
  input: SystemHeaderLayoutInput,
): SystemHeaderLayout {
  const width = finiteDimension(input.topbarWidth)
  const previous = input.previousLayout ?? null
  let layout: SystemHeaderLayout

  if (previous === 'wide') {
    if (width < SYSTEM_HEADER_TIGHT_ENTER_WIDTH) layout = 'tight'
    else if (width < SYSTEM_HEADER_WIDE_EXIT_WIDTH) layout = 'compact'
    else layout = 'wide'
  } else if (previous === 'compact') {
    if (width < SYSTEM_HEADER_TIGHT_ENTER_WIDTH) layout = 'tight'
    else if (width >= SYSTEM_HEADER_INITIAL_WIDE_WIDTH) layout = 'wide'
    else layout = 'compact'
  } else if (previous === 'tight') {
    if (width >= SYSTEM_HEADER_INITIAL_WIDE_WIDTH) layout = 'wide'
    else if (width >= SYSTEM_HEADER_TIGHT_EXIT_WIDTH) layout = 'compact'
    else layout = 'tight'
  } else if (width >= SYSTEM_HEADER_INITIAL_WIDE_WIDTH) {
    layout = 'wide'
  } else if (width < SYSTEM_HEADER_INITIAL_TIGHT_WIDTH) {
    layout = 'tight'
  } else {
    layout = 'compact'
  }

  const pressureAllowsWide = !(
    normalizedPressureCount(input.pressureCount) >= SYSTEM_HEADER_PRESSURE_COUNT
    && width < SYSTEM_HEADER_PRESSURE_WIDE_MIN_WIDTH
  )
  return capWide(layout, wideAllowed(input) && pressureAllowsWide)
}

/** Return the highest-severity status, using normal for an empty collection. */
export function highestSystemSeverity(
  severities: readonly SystemSeverity[],
): SystemSeverity {
  let highest: SystemSeverity = 'normal'
  for (const severity of severities) {
    if (SYSTEM_SEVERITY_RANK[severity] > SYSTEM_SEVERITY_RANK[highest]) {
      highest = severity
    }
  }
  return highest
}
