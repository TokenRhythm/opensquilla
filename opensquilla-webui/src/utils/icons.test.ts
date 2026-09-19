import { describe, expect, it } from 'vitest'

import { getIconSvg } from './icons'

describe('sidebar toggle icons', () => {
  it('uses a full divider for the visible sidebar state', () => {
    const svg = getIconSvg('sidebar-visible', 18)

    expect(svg).toContain('M9.5 4.5v15')
    expect(svg).not.toContain('l6 6')
    expect(svg).not.toContain('l-6 6')
  })

  it('uses a short rail for the hidden sidebar state', () => {
    const svg = getIconSvg('sidebar-hidden', 18)

    expect(svg).toContain('M8.5 9v6')
    expect(svg).not.toContain('l6 6')
    expect(svg).not.toContain('l-6 6')
  })
})

describe('generated-suggestion icons', () => {
  it('draws a four-point sparkle for a generated draft', () => {
    const svg = getIconSvg('sparkle', 12)

    // The star, not one of the two small marks beside it: a control that
    // renders only the marks reads as a plus.
    expect(svg).toContain('m12 3-1.9 5.8')
    expect(svg).toContain('M5 3v4')
  })
})

describe('plan disclosure icons', () => {
  it('uses outward corners for expand and inward corners for collapse', () => {
    expect(getIconSvg('expand', 15)).toContain('15 3 21 3 21 9')
    expect(getIconSvg('collapse', 15)).toContain('4 14 10 14 10 20')
  })
})
