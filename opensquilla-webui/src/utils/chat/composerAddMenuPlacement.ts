const DEFAULT_GAP = 8
const DEFAULT_VIEWPORT_PADDING = 8

interface ComposerAddMenuPlacementInput {
  menuBottom: number
  currentLift: number
  boundaryTop: number
  gap?: number
  viewportPadding?: number
}

interface ComposerAddMenuPlacement {
  lift: number
  maxHeight: number
}

/**
 * Keep the Composer Add menu above an in-flow run surface without changing
 * its normal anchor. `menuBottom + currentLift` reconstructs the menu's
 * unshifted bottom, making repeated measurements stable after the lift has
 * already been applied.
 */
export function resolveComposerAddMenuPlacement({
  menuBottom,
  currentLift,
  boundaryTop,
  gap = DEFAULT_GAP,
  viewportPadding = DEFAULT_VIEWPORT_PADDING,
}: ComposerAddMenuPlacementInput): ComposerAddMenuPlacement {
  const unshiftedBottom = menuBottom + currentLift
  return {
    lift: Math.max(0, Math.ceil(unshiftedBottom - boundaryTop + gap)),
    maxHeight: Math.max(0, Math.floor(boundaryTop - gap - viewportPadding)),
  }
}
