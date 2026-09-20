import { describe, expect, it } from 'vitest'
import {
  calculateModelRoutingPlacement,
  type ModelRoutingAnchor,
  type ModelRoutingPlacement,
  type ModelRoutingPlacementInput,
  type ModelRoutingViewport,
} from './modelRoutingPlacement'

const viewport: ModelRoutingViewport = { left: 0, top: 0, width: 1366, height: 900 }
const anchor: ModelRoutingAnchor = { left: 520, right: 640, top: 780, bottom: 816 }
const input: ModelRoutingPlacementInput = { viewport, anchor, primaryHeight: 280 }

function expectContained(result: ModelRoutingPlacement, view: ModelRoutingViewport) {
  const left = view.left + Math.min(12, view.width / 2)
  const right = view.left + view.width - Math.min(12, view.width / 2)
  const top = view.top + Math.min(12, view.height / 2)
  const bottom = view.top + view.height - Math.min(12, view.height / 2)
  const submenuWidth = result.compact ? result.width : 316
  expect(result.primaryLeft).toBeGreaterThanOrEqual(left)
  expect(result.primaryLeft + result.width).toBeLessThanOrEqual(right)
  expect(result.primaryBottom).toBeGreaterThanOrEqual(top)
  expect(result.primaryBottom).toBeLessThanOrEqual(bottom)
  expect(result.availableHeight).toBeGreaterThanOrEqual(0)
  expect(result.primaryBottom - result.availableHeight).toBeGreaterThanOrEqual(top)
  expect(result.submenuLeft).toBeGreaterThanOrEqual(left)
  expect(result.submenuLeft + submenuWidth).toBeLessThanOrEqual(right)
  expect(result.submenuTop).toBeGreaterThanOrEqual(top)
  expect(result.submenuHeight).toBeGreaterThanOrEqual(0)
  expect(result.submenuHeight).toBeLessThanOrEqual(360)
  expect(result.submenuTop + result.submenuHeight).toBeLessThanOrEqual(result.primaryBottom)
  if (result.compact) {
    expect(result.submenuLeft).toBe(result.primaryLeft)
    expect(result.submenuTop + result.submenuHeight).toBe(result.primaryBottom)
  } else if (result.submenuSide === 'right') {
    expect(result.submenuLeft).toBe(result.primaryLeft + result.width + 8)
  } else {
    expect(result.submenuLeft + 316 + 8).toBe(result.primaryLeft)
  }
}

describe('native model routing placement', () => {
  it('attaches the primary to its trigger and places the submenu on the right', () => {
    const placed = calculateModelRoutingPlacement(input)
    expect(placed).toMatchObject({
      primaryLeft: 416,
      primaryBottom: 772,
      availableHeight: 760,
      width: 224,
      compact: false,
      submenuLeft: 648,
      submenuTop: 412,
      submenuHeight: 360,
      submenuSide: 'right',
    })
  })

  it('opens left when the trigger is near the right edge without moving the primary', () => {
    const placed = calculateModelRoutingPlacement({
      ...input,
      anchor: { ...anchor, left: 1170, right: 1290 },
    })
    expect(placed).toMatchObject({
      primaryLeft: 1066,
      width: 224,
      submenuLeft: 742,
      submenuSide: 'left',
    })
    expect(placed.primaryLeft + placed.width).toBe(1290)
  })

  it('uses the single-model row top when space permits and clamps upward when it does not', () => {
    const roomy = calculateModelRoutingPlacement({
      ...input,
      primaryHeight: 600,
      singleRowTop: 230,
    })
    expect(roomy.submenuTop).toBe(230)
    const lowRow = calculateModelRoutingPlacement({
      ...input,
      primaryHeight: 280,
      singleRowTop: 600,
    })
    expect(lowRow.submenuTop + lowRow.submenuHeight).toBe(lowRow.primaryBottom)
    const aboveViewport = calculateModelRoutingPlacement({ ...input, singleRowTop: -40 })
    expect(aboveViewport.submenuTop).toBe(12)
  })

  it('keeps primary geometry identical as measurement and list placement become available', () => {
    const first = calculateModelRoutingPlacement({ ...input, primaryHeight: 0 })
    for (const primaryHeight of [120, 280, 500, 1200]) {
      for (const singleRowTop of [undefined, 0, 260, 740]) {
        const measured = calculateModelRoutingPlacement({ ...input, primaryHeight, singleRowTop })
        expect({
          left: measured.primaryLeft,
          bottom: measured.primaryBottom,
          width: measured.width,
        }).toEqual({ left: first.primaryLeft, bottom: first.primaryBottom, width: first.width })
      }
    }
  })

  it('drills in place when neither side fits, even above the mobile breakpoint', () => {
    const placed = calculateModelRoutingPlacement({
      ...input,
      viewport: { ...viewport, width: 600 },
      anchor: { left: 300, right: 420, top: 700, bottom: 736 },
    })
    expect(placed).toMatchObject({
      compact: true,
      width: 352,
      primaryLeft: 68,
      submenuLeft: 68,
      submenuSide: 'compact',
    })
    expect(placed.submenuTop + placed.submenuHeight).toBe(placed.primaryBottom)
  })

  it('allows a desktop submenu at exactly 600px when one side fits', () => {
    const placed = calculateModelRoutingPlacement({
      ...input,
      viewport: { ...viewport, width: 600 },
      anchor: { left: 468, right: 588, top: 700, bottom: 736 },
    })
    expect(placed).toMatchObject({
      compact: false,
      width: 224,
      primaryLeft: 364,
      submenuLeft: 40,
      submenuSide: 'left',
    })
  })

  it('translates all coordinates with visualViewport offsets', () => {
    const initial = calculateModelRoutingPlacement({ ...input, singleRowTop: 260 })
    const translated = calculateModelRoutingPlacement({
      ...input,
      viewport: { ...viewport, left: 83, top: 127 },
      anchor: {
        left: anchor.left + 83,
        right: anchor.right + 83,
        top: anchor.top + 127,
        bottom: anchor.bottom + 127,
      },
      singleRowTop: 260 + 127,
    })
    expect(translated).toEqual({
      ...initial,
      primaryLeft: initial.primaryLeft + 83,
      primaryBottom: initial.primaryBottom + 127,
      submenuLeft: initial.submenuLeft + 83,
      submenuTop: initial.submenuTop + 127,
    })
  })

  it('follows a moving sidebar anchor without introducing submenu-width offsets', () => {
    for (let right = 450; right <= 1250; right += 20) {
      const placed = calculateModelRoutingPlacement({
        ...input,
        anchor: { ...anchor, left: right - 120, right },
      })
      expect(placed.primaryLeft + placed.width).toBe(right)
      expect(placed.primaryBottom).toBe(anchor.top - 8)
      expectContained(placed, viewport)
    }
  })

  it('handles resized and partially offscreen triggers using the visible viewport', () => {
    for (const width of [224, 236, 280]) {
      const placed = calculateModelRoutingPlacement({
        ...input,
        primaryWidth: width,
        anchor: { left: 1220, right: 1510, top: 1100, bottom: 1160 },
      })
      expect(placed.primaryLeft + placed.width).toBe(viewport.width - 12)
      expect(placed.primaryBottom).toBe(viewport.height - 12)
      expectContained(placed, viewport)
    }
  })

  it('reduces submenu height above a high trigger instead of overflowing the viewport', () => {
    const placed = calculateModelRoutingPlacement({
      ...input,
      anchor: { ...anchor, top: 160, bottom: 196 },
    })
    expect(placed).toMatchObject({
      primaryBottom: 152,
      availableHeight: 140,
      submenuHeight: 140,
      submenuTop: 12,
    })
  })

  it.each([320, 375, 390, 600, 768, 895, 1024, 1366, 1920])(
    'contains both panels at viewport width %i across edge anchors, heights and offsets',
    (width) => {
      for (const height of [240, 480, 725, 900, 1080]) {
        for (const offset of [
          { left: 0, top: 0 },
          { left: 37, top: 83 },
        ]) {
          const view = { width, height, ...offset }
          for (const right of [-40, 24, width * 0.5, width - 12, width + 70]) {
            for (const top of [-20, 24, height * 0.5, height - 48, height + 40]) {
              const anchorRect = {
                left: right - 100 + offset.left,
                right: right + offset.left,
                top: top + offset.top,
                bottom: top + 36 + offset.top,
              }
              const placed = calculateModelRoutingPlacement({
                viewport: view,
                anchor: anchorRect,
                primaryHeight: 280,
                singleRowTop: top - 150 + offset.top,
              })
              expectContained(placed, view)
              if (width < 600) expect(placed.compact).toBe(true)
            }
          }
        }
      }
    },
  )
})
