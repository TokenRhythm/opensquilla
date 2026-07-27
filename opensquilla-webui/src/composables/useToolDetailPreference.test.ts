// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  TOOL_DETAIL_DISPLAY_STORAGE_KEY,
  parseToolDetailDisplayMode,
  useToolDetailPreference,
} from './useToolDetailPreference'

describe('useToolDetailPreference', () => {
  beforeEach(() => {
    useToolDetailPreference().setMode('auto')
    localStorage.clear()
  })

  it('preserves the legacy auto behavior for missing or unknown values', () => {
    expect(parseToolDetailDisplayMode(null)).toBe('auto')
    expect(parseToolDetailDisplayMode('')).toBe('auto')
    expect(parseToolDetailDisplayMode('open')).toBe('auto')
    expect(parseToolDetailDisplayMode({ mode: 'compact' })).toBe('auto')
  })

  it('accepts every supported mode', () => {
    expect(parseToolDetailDisplayMode('auto')).toBe('auto')
    expect(parseToolDetailDisplayMode('compact')).toBe('compact')
    expect(parseToolDetailDisplayMode('expanded')).toBe('expanded')
  })

  it('updates the shared ref and persists immediately', () => {
    const first = useToolDetailPreference()
    const second = useToolDetailPreference()

    first.setMode('compact')

    expect(first.mode.value).toBe('compact')
    expect(second.mode.value).toBe('compact')
    expect(localStorage.getItem(TOOL_DETAIL_DISPLAY_STORAGE_KEY)).toBe('compact')
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
