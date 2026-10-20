// Runtime implementation is built from the WebUI workbench/annotationPlacement.ts.
export type AnnotationSide = 'bottom' | 'top' | 'right' | 'left'
export interface AnnotationRect {
  left: number
  top: number
  width: number
  height: number
}
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
export function placeAnnotationCard(input: AnnotationPlacementInput): AnnotationPlacement
