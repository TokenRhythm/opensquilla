import { describe, expect, it } from 'vitest'
import {
  WORKBENCH_DEFAULT_WIDTH,
  WORKBENCH_MIN_WIDTH,
  defaultWorkbenchWidthPreference,
  parseWorkbenchWidthPreference,
  workbenchDynamicMax,
  workbenchEffectiveWidth,
  workbenchLayoutMode,
  workbenchShouldYieldSidebar,
} from './layout'

describe('workbench layout', () => {
  it('uses one content pane below the exact non-overlapping split budget', () => {
    expect(workbenchLayoutMode({ availableWidth: 1280 })).toBe('split')
    expect(workbenchLayoutMode({ availableWidth: 841 })).toBe('split')
    expect(workbenchLayoutMode({ availableWidth: 840 })).toBe('single-pane')
    expect(workbenchLayoutMode({ availableWidth: 390 })).toBe('single-pane')
    expect(workbenchLayoutMode({ availableWidth: Number.NaN })).toBe('single-pane')
  })

  it('reserves 480px for chat plus the divider before sizing the pane', () => {
    expect(workbenchDynamicMax(1200)).toBe(719)
    expect(workbenchDynamicMax(2000)).toBe(1400)
    expect(workbenchDynamicMax(960)).toBe(479)
    expect(workbenchDynamicMax(841)).toBe(360)
  })

  it('uses the same content-only single pane for maximize and constrained width', () => {
    expect(workbenchLayoutMode({ availableWidth: 1280, maximized: true })).toBe('single-pane')
    expect(workbenchLayoutMode({ availableWidth: 720, maximized: true })).toBe('single-pane')
    const preference = { version: 1 as const, width: 614, source: 'user' as const }
    expect(workbenchEffectiveWidth(preference, 'single-pane', 1280)).toBe(1280)
    expect(workbenchEffectiveWidth(preference, 'split', 1280)).toBe(614)
    expect(workbenchEffectiveWidth(preference, 'single-pane', Number.NaN)).toBe(0)
  })

  it('preserves the user split width through narrow layout and restoration', () => {
    const preference = { version: 1 as const, width: 600, source: 'user' as const }
    expect(workbenchEffectiveWidth(preference, 'split', 1081)).toBe(600)
    expect(workbenchEffectiveWidth(preference, 'single-pane', 500)).toBe(500)
    expect(workbenchEffectiveWidth(preference, 'split', 1200)).toBe(600)
    expect(preference.width).toBe(600)
  })

  it('uses an even split until the user chooses a fixed width', () => {
    const preference = defaultWorkbenchWidthPreference()
    expect(workbenchEffectiveWidth(preference, 'split', 1200)).toBe(600)
    expect(workbenchEffectiveWidth(preference, 'split', 1600)).toBe(800)
    expect(workbenchEffectiveWidth(preference, 'split', 841)).toBe(360)
    expect(preference.source).toBe('default')
  })

  it('temporarily yields navigation without reversing priority at the split boundary', () => {
    const base = { sidebarWidth: 260, workbenchExpanded: true, sidebarDrawer: false }
    expect(workbenchShouldYieldSidebar({ ...base, shellWidth: 1200 })).toBe(false)
    expect(workbenchShouldYieldSidebar({ ...base, shellWidth: 1101 })).toBe(false)
    expect(workbenchShouldYieldSidebar({ ...base, shellWidth: 1100 })).toBe(true)
    expect(workbenchShouldYieldSidebar({ ...base, shellWidth: 841 })).toBe(true)
    expect(workbenchShouldYieldSidebar({ ...base, shellWidth: 840 })).toBe(true)
    expect(workbenchShouldYieldSidebar({ ...base, shellWidth: 900, workbenchExpanded: false })).toBe(false)
    expect(workbenchShouldYieldSidebar({ ...base, shellWidth: 900, sidebarDrawer: true })).toBe(false)
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
