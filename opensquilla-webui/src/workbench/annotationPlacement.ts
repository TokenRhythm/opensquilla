/** Pure CSS-pixel geometry shared by WebUI Office and the native HTML editor.
 * Electron compiles this source into its own standalone runtime bundle.
 */
export interface AnnotationRect { left: number; top: number; width: number; height: number }
export type AnnotationSide = 'bottom' | 'top' | 'right' | 'left'
export interface AnnotationPlacementInput {
  anchor: AnnotationRect
  viewport: AnnotationRect
  width: number
  height: number
  preferredSide?: AnnotationSide
  gap?: number
  inset?: number
}
export interface AnnotationPlacement extends AnnotationRect {
  side: AnnotationSide
  detached: boolean
}

export function placeAnnotationCard(input: AnnotationPlacementInput): AnnotationPlacement {
  const { anchor, viewport } = input
  const gap = Math.max(0, input.gap ?? 8)
  const inset = Math.max(0, input.inset ?? 8)
  const left = viewport.left + Math.min(inset, viewport.width / 2)
  const top = viewport.top + Math.min(inset, viewport.height / 2)
  const right = viewport.left + viewport.width - Math.min(inset, viewport.width / 2)
  const bottom = viewport.top + viewport.height - Math.min(inset, viewport.height / 2)
  const width = Math.max(0, Math.min(input.width, right - left))
  const height = Math.max(0, Math.min(input.height, bottom - top))
  const clamp = (value: number, min: number, max: number) => Math.max(min, Math.min(value, max))
  // A partially visible target anchors to its visible portion. An offscreen
  // target projects onto the nearest viewport edge, never an unrelated corner.
  const x1 = clamp(anchor.left, left, right)
  const x2 = clamp(anchor.left + anchor.width, left, right)
  const y1 = clamp(anchor.top, top, bottom)
  const y2 = clamp(anchor.top + anchor.height, top, bottom)
  const detached = anchor.left + anchor.width <= viewport.left
    || anchor.left >= viewport.left + viewport.width
    || anchor.top + anchor.height <= viewport.top
    || anchor.top >= viewport.top + viewport.height
  const points: Record<AnnotationSide, { left: number; top: number }> = {
    bottom: { left: clamp(x1, left, right - width), top: y2 + gap },
    top: { left: clamp(x1, left, right - width), top: y1 - height - gap },
    right: { left: x2 + gap, top: clamp(y1, top, bottom - height) },
    left: { left: x1 - width - gap, top: clamp(y1, top, bottom - height) },
  }
  const order: AnnotationSide[] = ['bottom', 'top', 'right', 'left']
  if (input.preferredSide) {
    order.splice(order.indexOf(input.preferredSide), 1)
    order.unshift(input.preferredSide)
  }
  const fits = (side: AnnotationSide) => {
    const point = points[side]
    return point.left >= left && point.top >= top
      && point.left + width <= right && point.top + height <= bottom
  }
  // If the target fills the viewport, choose the placement needing the least
  // adjustment. Cross-axis shifting happens before fitting, so a wide card
  // beside a right-aligned target does not fall back to the viewport bottom.
  const overflow = (side: AnnotationSide) => {
    const point = points[side]
    return Math.max(left - point.left, 0) + Math.max(point.left + width - right, 0)
      + Math.max(top - point.top, 0) + Math.max(point.top + height - bottom, 0)
  }
  const side = order.find(fits) ?? order.reduce((best, candidate) =>
    overflow(candidate) < overflow(best) ? candidate : best, order[0]!)
  return {
    left: clamp(points[side].left, left, right - width),
    top: clamp(points[side].top, top, bottom - height),
    width, height, side, detached,
  }
}
