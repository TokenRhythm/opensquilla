import { describe, expect, it } from 'vitest'

import {
  buildVariableWindow,
  variableWindowIndexAtOffset,
  type VariableWindowRow,
} from './variableMessageWindow'

function rows(count: number, size = 100): VariableWindowRow[] {
  return Array.from({ length: count }, (_, index) => ({
    key: `message-${index}`,
    estimatedSize: size,
  }))
}

describe('variable message window', () => {
  it('renders two viewports of overscan on both sides', () => {
    const layout = buildVariableWindow({
      rows: rows(200),
      measuredSizes: new Map(),
      viewportStart: 10_000,
      viewportSize: 600,
    })

    expect(layout.entries[0]?.index).toBe(88)
    expect(layout.entries[layout.entries.length - 1]?.index).toBe(117)
    expect(layout.entries).toHaveLength(30)
    expect(layout.topSpacer).toBe(8_800)
    expect(layout.bottomSpacer).toBe(8_200)
    expect(layout.totalSize).toBe(20_000)
  })

  it('uses measured variable heights when finding the visible rows', () => {
    const measured = new Map([
      ['message-0', 40],
      ['message-1', 260],
      ['message-2', 60],
    ])
    const layout = buildVariableWindow({
      rows: rows(5),
      measuredSizes: measured,
      viewportStart: 250,
      viewportSize: 50,
      overscanViewports: 0,
    })

    expect(layout.offsets).toEqual([0, 40, 300, 360, 460, 560])
    expect(layout.viewportStartIndex).toBe(1)
    expect(layout.viewportEndIndex).toBe(1)
    expect(layout.entries.map(entry => entry.index)).toEqual([1])
  })

  it('keeps distant forced rows in logical order with gap spacers', () => {
    const layout = buildVariableWindow({
      rows: rows(100),
      measuredSizes: new Map(),
      viewportStart: 0,
      viewportSize: 200,
      overscanViewports: 0,
      forceIndexes: [80],
    })

    expect(layout.entries.map(entry => entry.index)).toEqual([0, 1, 80])
    expect(layout.entries[2]?.gapBefore).toBe(7_800)
    expect(layout.topSpacer).toBe(0)
    expect(layout.bottomSpacer).toBe(1_900)
    expect(
      layout.topSpacer
      + layout.entries.reduce((sum, entry) => sum + entry.gapBefore + layout.sizes[entry.index], 0)
      + layout.bottomSpacer,
    ).toBe(layout.totalSize)
  })

  it('caps compact-row overscan while preserving visible and forced rows', () => {
    const layout = buildVariableWindow({
      rows: rows(200, 40),
      measuredSizes: new Map(),
      viewportStart: 4_000,
      viewportSize: 600,
      forceIndexes: [180],
    })

    const indexes = layout.entries.map(entry => entry.index)
    for (let index = layout.viewportStartIndex; index <= layout.viewportEndIndex; index += 1) {
      expect(indexes).toContain(index)
    }
    expect(indexes).toContain(180)
    expect(layout.entries).toHaveLength(30)
  })

  it('clamps offset lookup at both ends', () => {
    const offsets = [0, 50, 150, 200]
    expect(variableWindowIndexAtOffset(offsets, -10)).toBe(0)
    expect(variableWindowIndexAtOffset(offsets, 149)).toBe(1)
    expect(variableWindowIndexAtOffset(offsets, 10_000)).toBe(2)
  })
})
