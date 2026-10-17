// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  TOOL_DETAIL_DISPLAY_STORAGE_KEY,
  parseToolDetailDisplayMode,
  useToolDetailPreference,
} from './useToolDetailPreference'

describe('useToolDetailPreference', () => {
  beforeEach(() => {
    useToolDetailPreference().setMode('collapsed')
    localStorage.clear()
  })

  it('starts collapsed for missing, legacy, or unknown values', () => {
    expect(parseToolDetailDisplayMode(null)).toBe('collapsed')
    expect(parseToolDetailDisplayMode('')).toBe('collapsed')
    expect(parseToolDetailDisplayMode('open')).toBe('collapsed')
    expect(parseToolDetailDisplayMode('auto')).toBe('collapsed')
    expect(parseToolDetailDisplayMode('compact')).toBe('collapsed')
    expect(parseToolDetailDisplayMode({ mode: 'expanded' })).toBe('collapsed')
  })

  it('accepts every supported mode', () => {
    expect(parseToolDetailDisplayMode('collapsed')).toBe('collapsed')
    expect(parseToolDetailDisplayMode('expanded')).toBe('expanded')
  })

  it('updates the shared ref and persists immediately', () => {
    const first = useToolDetailPreference()
    const second = useToolDetailPreference()

    first.setMode('expanded')

    expect(first.mode.value).toBe('expanded')
    expect(second.mode.value).toBe('expanded')
    expect(localStorage.getItem(TOOL_DETAIL_DISPLAY_STORAGE_KEY)).toBe('expanded')

    first.setMode('collapsed')
    expect(second.mode.value).toBe('collapsed')
    expect(localStorage.getItem(TOOL_DETAIL_DISPLAY_STORAGE_KEY)).toBe('collapsed')
  })

  it.each([
    [null, 'collapsed'],
    ['auto', 'collapsed'],
    ['compact', 'collapsed'],
    ['expanded', 'expanded'],
    ['collapsed', 'collapsed'],
    ['invalid', 'collapsed'],
  ])('loads stored %s as %s without losing an explicit expanded preference', async (stored, expected) => {
    if (stored !== null) localStorage.setItem(TOOL_DETAIL_DISPLAY_STORAGE_KEY, stored)
    vi.resetModules()
    const loaded = await import('./useToolDetailPreference')
    expect(loaded.useToolDetailPreference().mode.value).toBe(expected)
  })

  it('keeps the in-memory preference when browser storage is unavailable', () => {
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('storage disabled')
    })
    const preference = useToolDetailPreference()

    expect(() => preference.setMode('expanded')).not.toThrow()
    expect(preference.mode.value).toBe('expanded')

    setItem.mockRestore()
  })
})
