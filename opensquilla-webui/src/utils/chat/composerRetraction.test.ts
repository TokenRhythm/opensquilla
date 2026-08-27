import { describe, expect, it } from 'vitest'
import { createComposerRetractionController, type ComposerScrollIntent } from './composerRetraction'

function sample(
  controller: ReturnType<typeof createComposerRetractionController>,
  scrollTop: number,
  options: {
    bottomGap?: number
    intent?: ComposerScrollIntent
    canCollapse?: boolean
    navigationLocked?: boolean
  } = {},
) {
  return controller.observe({
    scrollTop,
    bottomGap: options.bottomGap ?? 500,
    intent: options.intent === undefined ? 'either' : options.intent,
    canCollapse: options.canCollapse ?? true,
    navigationLocked: options.navigationLocked,
  })
}

describe('composer retraction controller', () => {
  it('uses the first far-from-bottom sample only to establish a baseline', () => {
    const controller = createComposerRetractionController({ collapseTravel: 4 })

    expect(sample(controller, 100, { intent: 'up' })).toBe(false)
    expect(controller.snapshot().lastScrollTop).toBe(100)
  })

  it('accumulates slow upward travel instead of requiring one large scroll event', () => {
    const controller = createComposerRetractionController()
    sample(controller, 300, { intent: null })

    for (const top of [298, 296, 294, 292, 290, 288, 286, 284, 282, 280, 278]) {
      expect(sample(controller, top, { intent: 'up' })).toBe(false)
    }
    expect(sample(controller, 276, { intent: 'up' })).toBe(true)
  })

  it('lets small reverse jitter offset rather than erase accumulated travel', () => {
    const controller = createComposerRetractionController()
    sample(controller, 300, { intent: null })
    sample(controller, 287, { intent: 'up' })
    sample(controller, 289, { intent: 'down' })

    expect(sample(controller, 278, { intent: 'up' })).toBe(false)
    expect(sample(controller, 267, { intent: 'up' })).toBe(true)
  })

  it('uses separate live-edge gaps for collapse and expansion', () => {
    const controller = createComposerRetractionController({ collapseTravel: 4, expandTravel: 4 })
    sample(controller, 300, { intent: null })

    expect(sample(controller, 290, { bottomGap: 120, intent: 'up' })).toBe(false)
    expect(sample(controller, 280, { bottomGap: 121, intent: 'up' })).toBe(true)
    expect(sample(controller, 290, { bottomGap: 60, intent: null })).toBe(true)
    expect(sample(controller, 295, { bottomGap: 59, intent: null })).toBe(true)
    expect(sample(controller, 296, { bottomGap: 59, intent: 'down' })).toBe(false)
  })

  it('only toggles for scroll movement that matches recent user intent', () => {
    const controller = createComposerRetractionController({ collapseTravel: 4 })
    sample(controller, 300, { intent: null })

    expect(sample(controller, 200, { intent: null })).toBe(false)
    expect(sample(controller, 320, { intent: 'up' })).toBe(false)
    expect(sample(controller, 310, { intent: 'up' })).toBe(true)
  })

  it('ignores minimap navigation while locked and syncs the final baseline', () => {
    const controller = createComposerRetractionController({ collapseTravel: 4 })
    sample(controller, 300, { intent: null })

    expect(sample(controller, 100, { intent: 'either', navigationLocked: true })).toBe(false)
    expect(sample(controller, 90, { intent: null })).toBe(false)
    expect(sample(controller, 85, { intent: 'up' })).toBe(true)
  })

  it('does not collapse while the composer owns an active interaction', () => {
    const controller = createComposerRetractionController({ collapseTravel: 4 })
    sample(controller, 300, { intent: null })

    expect(sample(controller, 290, { intent: 'up', canCollapse: false })).toBe(false)
    expect(sample(controller, 285, { intent: 'up', canCollapse: true })).toBe(true)
    expect(sample(controller, 280, { intent: null, canCollapse: false })).toBe(false)
  })

  it('reset clears collapsed state, baseline, and accumulated travel', () => {
    const controller = createComposerRetractionController({ collapseTravel: 4 })
    sample(controller, 300, { intent: null })
    sample(controller, 290, { intent: 'up' })
    expect(controller.snapshot().collapsed).toBe(true)

    expect(controller.reset()).toBe(false)
    expect(controller.snapshot()).toEqual({
      collapsed: false,
      lastScrollTop: null,
      direction: null,
      travel: 0,
    })
  })
})
