export type ComposerScrollIntent = 'up' | 'down' | 'either' | null

export interface ComposerRetractionSample {
  scrollTop: number
  bottomGap: number
  intent: ComposerScrollIntent
  canCollapse: boolean
  navigationLocked?: boolean
}

export interface ComposerRetractionOptions {
  collapseGap: number
  expandGap: number
  collapseTravel: number
  expandTravel: number
}

export interface ComposerRetractionSnapshot {
  collapsed: boolean
  lastScrollTop: number | null
  direction: 'up' | 'down' | null
  travel: number
}

const DEFAULT_OPTIONS: ComposerRetractionOptions = {
  collapseGap: 120,
  expandGap: 60,
  collapseTravel: 24,
  expandTravel: 8,
}

/**
 * Reduces transcript scroll samples into the floating composer's expanded or
 * retracted state. Only samples paired with recent user intent may toggle the
 * composer; programmatic position changes merely establish a fresh baseline.
 */
export function createComposerRetractionController(
  overrides: Partial<ComposerRetractionOptions> = {},
) {
  const options = { ...DEFAULT_OPTIONS, ...overrides }
  let collapsed = false
  let lastScrollTop: number | null = null
  let direction: 'up' | 'down' | null = null
  let travel = 0

  function clearTravel() {
    direction = null
    travel = 0
  }

  function reset(scrollTop: number | null = null) {
    collapsed = false
    lastScrollTop = scrollTop
    clearTravel()
    return collapsed
  }

  function expand(scrollTop: number | null = lastScrollTop) {
    collapsed = false
    lastScrollTop = scrollTop
    clearTravel()
    return collapsed
  }

  function snapshot(): ComposerRetractionSnapshot {
    return { collapsed, lastScrollTop, direction, travel }
  }

  function observe(sample: ComposerRetractionSample): boolean {
    const previousScrollTop = lastScrollTop
    lastScrollTop = sample.scrollTop

    // A focused control must never remain inside a visibility-hidden footer.
    if (!sample.canCollapse) return expand(sample.scrollTop)

    if (
      previousScrollTop === null
      || sample.navigationLocked
      || sample.intent === null
    ) {
      clearTravel()
      return collapsed
    }

    const delta = sample.scrollTop - previousScrollTop
    if (delta === 0) return collapsed
    const actualDirection = delta < 0 ? 'up' : 'down'

    // A history prepend commonly moves scrollTop down while the user's wheel
    // still points up. Direction agreement prevents that correction from
    // being mistaken for a new gesture.
    if (sample.intent !== 'either' && sample.intent !== actualDirection) {
      clearTravel()
      return collapsed
    }

    if (collapsed) {
      // Signed net travel tolerates 1–2px trackpad jitter instead of erasing a
      // deliberate slow gesture whenever one sample briefly reverses.
      travel = Math.max(0, travel + delta)
      direction = travel > 0 ? 'down' : null
      if (
        actualDirection === 'down'
        && (sample.bottomGap < options.expandGap || travel >= options.expandTravel)
      ) {
        return expand(sample.scrollTop)
      }
      return collapsed
    }

    if (
      sample.bottomGap <= options.collapseGap
      || actualDirection !== 'up'
    ) {
      if (sample.bottomGap <= options.collapseGap) clearTravel()
      else {
        travel = Math.min(0, travel + delta)
        direction = travel < 0 ? 'up' : null
      }
      return collapsed
    }

    travel = Math.min(0, travel + delta)
    direction = travel < 0 ? 'up' : null
    if (travel <= -options.collapseTravel) {
      collapsed = true
      clearTravel()
    }
    return collapsed
  }

  return {
    observe,
    reset,
    expand,
    snapshot,
  }
}
