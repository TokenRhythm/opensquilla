import { describe, expect, it } from 'vitest'

import {
  SESSION_HEADER_INITIAL_TIGHT_WIDTH,
  SESSION_HEADER_INITIAL_WIDE_WIDTH,
  SESSION_HEADER_TIGHT_EXIT_WIDTH,
  SESSION_HEADER_WIDE_EXIT_WIDTH,
  SYSTEM_HEADER_INITIAL_TIGHT_WIDTH,
  SYSTEM_HEADER_INITIAL_WIDE_WIDTH,
  SYSTEM_HEADER_PRESSURE_COUNT,
  SYSTEM_HEADER_PRESSURE_WIDE_MIN_WIDTH,
  SYSTEM_HEADER_TIGHT_ENTER_WIDTH,
  SYSTEM_HEADER_TIGHT_EXIT_WIDTH,
  SYSTEM_HEADER_WIDE_EXIT_WIDTH,
  highestSystemSeverity,
  resolveSessionHeaderLayout,
  resolveSystemHeaderLayout,
  type SessionHeaderLayout,
  type SystemHeaderLayout,
} from './headerLayout'

describe('resolveSessionHeaderLayout', () => {
  it('keeps the approved initial and hysteresis thresholds stable', () => {
    expect({
      initialWide: SESSION_HEADER_INITIAL_WIDE_WIDTH,
      wideExit: SESSION_HEADER_WIDE_EXIT_WIDTH,
      initialTight: SESSION_HEADER_INITIAL_TIGHT_WIDTH,
      tightExit: SESSION_HEADER_TIGHT_EXIT_WIDTH,
    }).toEqual({
      initialWide: 576,
      wideExit: 544,
      initialTight: 184,
      tightExit: 216,
    })
  })

  it.each<[number, SessionHeaderLayout]>([
    [183, 'tight'],
    [184, 'compact'],
    [575, 'compact'],
    [576, 'wide'],
  ])('uses the initial layout boundary at %dpx', (availableWidth, expected) => {
    expect(resolveSessionHeaderLayout({ availableWidth })).toBe(expected)
  })

  it('keeps wide and compact stable inside their 544px/576px dead band', () => {
    expect(resolveSessionHeaderLayout({ availableWidth: 543, previousLayout: 'wide' }))
      .toBe('compact')
    expect(resolveSessionHeaderLayout({ availableWidth: 544, previousLayout: 'wide' }))
      .toBe('wide')
    expect(resolveSessionHeaderLayout({ availableWidth: 575, previousLayout: 'compact' }))
      .toBe('compact')
    expect(resolveSessionHeaderLayout({ availableWidth: 576, previousLayout: 'compact' }))
      .toBe('wide')
    expect(resolveSessionHeaderLayout({ availableWidth: 560, previousLayout: 'wide' }))
      .toBe('wide')
    expect(resolveSessionHeaderLayout({ availableWidth: 560, previousLayout: 'compact' }))
      .toBe('compact')
  })

  it('keeps compact and tight stable inside their 184px/216px dead band', () => {
    expect(resolveSessionHeaderLayout({ availableWidth: 183, previousLayout: 'compact' }))
      .toBe('tight')
    expect(resolveSessionHeaderLayout({ availableWidth: 184, previousLayout: 'compact' }))
      .toBe('compact')
    expect(resolveSessionHeaderLayout({ availableWidth: 215, previousLayout: 'tight' }))
      .toBe('tight')
    expect(resolveSessionHeaderLayout({ availableWidth: 216, previousLayout: 'tight' }))
      .toBe('compact')
    expect(resolveSessionHeaderLayout({ availableWidth: 200, previousLayout: 'compact' }))
      .toBe('compact')
    expect(resolveSessionHeaderLayout({ availableWidth: 200, previousLayout: 'tight' }))
      .toBe('tight')
  })

  it('allows a large resize to skip an intermediate state', () => {
    expect(resolveSessionHeaderLayout({ availableWidth: 100, previousLayout: 'wide' }))
      .toBe('tight')
    expect(resolveSessionHeaderLayout({ availableWidth: 800, previousLayout: 'tight' }))
      .toBe('wide')
  })

  it('caps mobile and coarse-only surfaces at compact without forcing tight', () => {
    expect(resolveSessionHeaderLayout({ availableWidth: 900, mobile: true })).toBe('compact')
    expect(resolveSessionHeaderLayout({ availableWidth: 900, coarseOnly: true })).toBe('compact')
    expect(resolveSessionHeaderLayout({
      availableWidth: 900,
      previousLayout: 'wide',
      mobile: true,
    })).toBe('compact')
    expect(resolveSessionHeaderLayout({ availableWidth: 100, mobile: true })).toBe('tight')
  })

  it('fails closed to tight for invalid dimensions', () => {
    expect(resolveSessionHeaderLayout({ availableWidth: Number.NaN })).toBe('tight')
    expect(resolveSessionHeaderLayout({ availableWidth: -100 })).toBe('tight')
  })
})

describe('resolveSystemHeaderLayout', () => {
  it('keeps the approved initial, hysteresis, and pressure thresholds stable', () => {
    expect({
      initialWide: SYSTEM_HEADER_INITIAL_WIDE_WIDTH,
      wideExit: SYSTEM_HEADER_WIDE_EXIT_WIDTH,
      initialTight: SYSTEM_HEADER_INITIAL_TIGHT_WIDTH,
      tightEnter: SYSTEM_HEADER_TIGHT_ENTER_WIDTH,
      tightExit: SYSTEM_HEADER_TIGHT_EXIT_WIDTH,
      pressureCount: SYSTEM_HEADER_PRESSURE_COUNT,
      pressureWideMin: SYSTEM_HEADER_PRESSURE_WIDE_MIN_WIDTH,
    }).toEqual({
      initialWide: 960,
      wideExit: 928,
      initialTight: 400,
      tightEnter: 384,
      tightExit: 416,
      pressureCount: 2,
      pressureWideMin: 1200,
    })
  })

  it.each<[number, SystemHeaderLayout]>([
    [399, 'tight'],
    [400, 'compact'],
    [959, 'compact'],
    [960, 'wide'],
  ])('uses the initial layout boundary at %dpx', (topbarWidth, expected) => {
    expect(resolveSystemHeaderLayout({ topbarWidth })).toBe(expected)
  })

  it('keeps wide and compact stable inside their 928px/960px dead band', () => {
    expect(resolveSystemHeaderLayout({ topbarWidth: 927, previousLayout: 'wide' }))
      .toBe('compact')
    expect(resolveSystemHeaderLayout({ topbarWidth: 928, previousLayout: 'wide' }))
      .toBe('wide')
    expect(resolveSystemHeaderLayout({ topbarWidth: 959, previousLayout: 'compact' }))
      .toBe('compact')
    expect(resolveSystemHeaderLayout({ topbarWidth: 960, previousLayout: 'compact' }))
      .toBe('wide')
    expect(resolveSystemHeaderLayout({ topbarWidth: 944, previousLayout: 'wide' }))
      .toBe('wide')
    expect(resolveSystemHeaderLayout({ topbarWidth: 944, previousLayout: 'compact' }))
      .toBe('compact')
  })

  it('keeps compact and tight stable inside their 384px/416px dead band', () => {
    expect(resolveSystemHeaderLayout({ topbarWidth: 383, previousLayout: 'compact' }))
      .toBe('tight')
    expect(resolveSystemHeaderLayout({ topbarWidth: 384, previousLayout: 'compact' }))
      .toBe('compact')
    expect(resolveSystemHeaderLayout({ topbarWidth: 415, previousLayout: 'tight' }))
      .toBe('tight')
    expect(resolveSystemHeaderLayout({ topbarWidth: 416, previousLayout: 'tight' }))
      .toBe('compact')
    expect(resolveSystemHeaderLayout({ topbarWidth: 400, previousLayout: 'compact' }))
      .toBe('compact')
    expect(resolveSystemHeaderLayout({ topbarWidth: 400, previousLayout: 'tight' }))
      .toBe('tight')
  })

  it('allows a large resize to skip an intermediate state', () => {
    expect(resolveSystemHeaderLayout({ topbarWidth: 300, previousLayout: 'wide' }))
      .toBe('tight')
    expect(resolveSystemHeaderLayout({ topbarWidth: 1300, previousLayout: 'tight' }))
      .toBe('wide')
  })

  it('caps concurrent status pressure at compact only below 1200px', () => {
    expect(resolveSystemHeaderLayout({ topbarWidth: 1199, pressureCount: 1 })).toBe('wide')
    expect(resolveSystemHeaderLayout({ topbarWidth: 1199, pressureCount: 2 })).toBe('compact')
    expect(resolveSystemHeaderLayout({ topbarWidth: 1200, pressureCount: 2 })).toBe('wide')
    expect(resolveSystemHeaderLayout({ topbarWidth: 1199, pressureCount: 3 })).toBe('compact')
  })

  it('does not let pressure force an otherwise compact header to tight', () => {
    expect(resolveSystemHeaderLayout({ topbarWidth: 700, pressureCount: 9 })).toBe('compact')
    expect(resolveSystemHeaderLayout({
      topbarWidth: 500,
      previousLayout: 'compact',
      pressureCount: 9,
    })).toBe('compact')
  })

  it('caps mobile and coarse-only surfaces at compact without forcing tight', () => {
    expect(resolveSystemHeaderLayout({ topbarWidth: 1400, mobile: true })).toBe('compact')
    expect(resolveSystemHeaderLayout({ topbarWidth: 1400, coarseOnly: true })).toBe('compact')
    expect(resolveSystemHeaderLayout({ topbarWidth: 700, mobile: true })).toBe('compact')
    expect(resolveSystemHeaderLayout({ topbarWidth: 300, coarseOnly: true })).toBe('tight')
  })

  it('normalizes invalid dimensions and pressure counts', () => {
    expect(resolveSystemHeaderLayout({ topbarWidth: Number.NaN })).toBe('tight')
    expect(resolveSystemHeaderLayout({
      topbarWidth: 1199,
      pressureCount: Number.NaN,
    })).toBe('wide')
    expect(resolveSystemHeaderLayout({ topbarWidth: 1199, pressureCount: -10 })).toBe('wide')
  })
})

describe('highestSystemSeverity', () => {
  it('uses normal for no active status', () => {
    expect(highestSystemSeverity([])).toBe('normal')
  })

  it('selects the highest severity independent of input order', () => {
    expect(highestSystemSeverity(['info', 'danger', 'warning'])).toBe('danger')
    expect(highestSystemSeverity(['warning', 'normal', 'info'])).toBe('warning')
    expect(highestSystemSeverity(['normal', 'info'])).toBe('info')
  })
})
