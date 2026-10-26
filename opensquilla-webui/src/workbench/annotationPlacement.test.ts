import { describe, expect, it } from 'vitest'
import { placeAnnotationCard, type AnnotationRect } from './annotationPlacement'

const viewport = { left: 800, top: 180, width: 640, height: 720 }
function place(anchor: AnnotationRect, overrides = {}) {
  return placeAnnotationCard({ anchor, viewport, width: 352, height: 80, ...overrides })
}
describe('shared annotation card placement', () => {
  it('stays adjacent to a right-aligned target by shifting within the viewport', () => {
    const result = place({ left: 1250, top: 300, width: 150, height: 24 })
    expect(result).toMatchObject({ left: 1080, top: 332, side: 'bottom', detached: false })
  })
  it('flips above a selection near the bottom, without covering it', () => {
    const result = place({ left: 900, top: 840, width: 180, height: 24 })
    expect(result).toMatchObject({ top: 752, side: 'top' })
    expect(result.top + result.height).toBeLessThan(840)
  })
  it('preserves the chosen side while typing if it still fits', () => {
    expect(place({ left: 900, top: 500, width: 180, height: 24 },
      { preferredSide: 'top', height: 150 })).toMatchObject({ side: 'top', top: 342 })
  })
  it('follows scroll and scale changes using current target geometry', () => {
    const initial = place({ left: 900, top: 400, width: 180, height: 24 })
    const scrolled = place({ left: 900, top: 300, width: 180, height: 24 })
    expect(scrolled.top - initial.top).toBe(-100)
    expect(place({ left: 900, top: 300, width: 360, height: 48 }).top).toBe(356)
  })
  it('docks beside the nearest edge for an offscreen target', () => {
    expect(place({ left: 900, top: 60, width: 180, height: 24 }))
      .toMatchObject({ detached: true, left: 900, top: 196 })
    expect(place({ left: 900, top: 1000, width: 180, height: 24 }))
      .toMatchObject({ detached: true, left: 900, top: 804, side: 'top' })
  })
  it('keeps a long editor inside a narrow viewport without a negative size', () => {
    const result = place({ left: 900, top: 300, width: 250, height: 24 },
      { viewport: { left: 880, top: 200, width: 260, height: 160 }, height: 250 })
    expect(result).toMatchObject({ left: 888, top: 208, width: 244, height: 144 })
  })
})
