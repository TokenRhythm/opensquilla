import { describe, expect, it } from 'vitest'
import {
  WORKBENCH_DEFAULT_WIDTH,
  WORKBENCH_MIN_WIDTH,
  defaultWorkbenchWidthPreference,
  parseWorkbenchWidthPreference,
  workbenchDynamicMax,
  workbenchEffectiveWidth,
  workbenchLayoutMode,
} from './layout'

describe('workbench layout', () => {
  it('selects split, overlay, and mobile modes from available space', () => {
    expect(workbenchLayoutMode({ availableWidth: 1280 })).toBe('split')
    expect(workbenchLayoutMode({ availableWidth: 900 })).toBe('overlay')
    expect(workbenchLayoutMode({ availableWidth: 720 })).toBe('mobile-dialog')
    expect(workbenchLayoutMode({ availableWidth: 1200, coarseOnly: true }))
      .toBe('mobile-dialog')
  })

  it('preserves a 480px chat column and caps the pane at 70 percent', () => {
    expect(workbenchDynamicMax(1200)).toBe(720)
    expect(workbenchDynamicMax(2000)).toBe(1400)
    expect(workbenchDynamicMax(960)).toBe(480)
  })

  it('does not overwrite the desktop preference when resolving narrow modes', () => {
    const preference = { version: 1 as const, width: 600, source: 'user' as const }
    expect(workbenchEffectiveWidth(preference, 'split', 1080)).toBe(600)
    expect(workbenchEffectiveWidth(preference, 'overlay', 500)).toBe(476)
    expect(workbenchEffectiveWidth(preference, 'mobile-dialog', 390)).toBe(390)
    expect(preference.width).toBe(600)
  })

  it('uses an even split until the user chooses a fixed width', () => {
    const preference = defaultWorkbenchWidthPreference()
    expect(workbenchEffectiveWidth(preference, 'split', 1200)).toBe(600)
    expect(workbenchEffectiveWidth(preference, 'split', 1600)).toBe(800)
    expect(workbenchEffectiveWidth(preference, 'overlay', 900)).toBe(520)
    expect(preference.source).toBe('default')
  })

  it('safely parses versioned width storage', () => {
    expect(parseWorkbenchWidthPreference(null))
      .toEqual({ version: 1, width: WORKBENCH_DEFAULT_WIDTH, source: 'default' })
    expect(parseWorkbenchWidthPreference('invalid').source).toBe('default')
    expect(parseWorkbenchWidthPreference('{"version":2,"width":700}').width)
      .toBe(WORKBENCH_DEFAULT_WIDTH)
    expect(parseWorkbenchWidthPreference('{"version":1,"width":200}').width)
      .toBe(WORKBENCH_MIN_WIDTH)
    expect(parseWorkbenchWidthPreference('{"version":1,"width":641.8}'))
      .toEqual({ version: 1, width: 642, source: 'user' })
  })
})
