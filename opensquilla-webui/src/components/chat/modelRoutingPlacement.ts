export interface ModelRoutingViewport {
  left: number
  top: number
  width: number
  height: number
}

export interface ModelRoutingAnchor {
  left: number
  right: number
  top: number
  bottom: number
}

export interface ModelRoutingPlacementInput {
  viewport: ModelRoutingViewport
  anchor: ModelRoutingAnchor
  primaryHeight: number
  primaryWidth?: number
  singleRowTop?: number
}

export interface ModelRoutingPlacement {
  primaryLeft: number
  /** Viewport coordinate of the lower edge, not a CSS `bottom` distance. */
  primaryBottom: number
  availableHeight: number
  width: number
  compact: boolean
  submenuLeft: number
  submenuTop: number
  submenuHeight: number
  submenuSide: 'right' | 'left' | 'compact'
}

const MARGIN = 12
const GAP = 8
const SUBMENU_WIDTH = 316
const SUBMENU_MAX_HEIGHT = 360
const COMPACT_MAX_WIDTH = 352

const clamp = (value: number, min: number, max: number) => Math.max(min, Math.min(value, max))

/**
 * Keep the primary menu attached to its trigger. Submenu visibility/content is
 * deliberately absent from the input so opening it cannot relocate the primary.
 * Coordinates use the same CSS-pixel space as getBoundingClientRect; visual
 * viewport offsets must therefore be included in viewport.left/top.
 */
export function calculateModelRoutingPlacement({
  viewport,
  anchor,
  primaryHeight,
  primaryWidth = 224,
  singleRowTop,
}: ModelRoutingPlacementInput): ModelRoutingPlacement {
  const horizontalMargin = Math.min(MARGIN, viewport.width / 2)
  const verticalMargin = Math.min(MARGIN, viewport.height / 2)
  const leftLimit = viewport.left + horizontalMargin
  const rightLimit = viewport.left + viewport.width - horizontalMargin
  const topLimit = viewport.top + verticalMargin
  const bottomLimit = viewport.top + viewport.height - verticalMargin
  const usableWidth = rightLimit - leftLimit
  const desktopWidth = Math.min(primaryWidth, usableWidth)
  const desktopLeft = clamp(anchor.right - desktopWidth, leftLimit, rightLimit - desktopWidth)
  const fitsRight = desktopLeft + desktopWidth + GAP + SUBMENU_WIDTH <= rightLimit
  const fitsLeft = desktopLeft - GAP - SUBMENU_WIDTH >= leftLimit
  const compact = viewport.width < 600 || (!fitsRight && !fitsLeft)
  const width = compact ? Math.min(COMPACT_MAX_WIDTH, usableWidth) : desktopWidth
  const primaryLeft = clamp(anchor.right - width, leftLimit, rightLimit - width)
  const primaryBottom = clamp(anchor.top - GAP, topLimit, bottomLimit)
  const availableHeight = primaryBottom - topLimit
  const submenuHeight = Math.min(SUBMENU_MAX_HEIGHT, availableHeight)
  const primaryTop = primaryBottom - Math.min(Math.max(0, primaryHeight), availableHeight)
  const submenuSide = compact ? 'compact' : fitsRight ? 'right' : 'left'

  return {
    primaryLeft,
    primaryBottom,
    availableHeight,
    width,
    compact,
    submenuLeft: compact
      ? primaryLeft
      : submenuSide === 'right'
        ? primaryLeft + width + GAP
        : primaryLeft - GAP - SUBMENU_WIDTH,
    submenuTop: compact
      ? primaryBottom - submenuHeight
      : clamp(singleRowTop ?? primaryTop, topLimit, primaryBottom - submenuHeight),
    submenuHeight,
    submenuSide,
  }
}
