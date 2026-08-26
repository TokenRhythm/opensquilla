import { describe, expect, it } from 'vitest'

import {
  FOUR_TIER_MAPPING_SELECTION_MODE,
  isFourTierMappingSelectionMode,
} from './modelRouting'

describe('four-tier mapping selection mode', () => {
  it('recognizes only the canonical mode value', () => {
    expect(isFourTierMappingSelectionMode(FOUR_TIER_MAPPING_SELECTION_MODE)).toBe(true)
    expect(isFourTierMappingSelectionMode(' FOUR_TIER_MAPPING ')).toBe(true)
    expect(isFourTierMappingSelectionMode('fixed_four_tier_v2')).toBe(false)
    expect(isFourTierMappingSelectionMode('fixed-four-tier-v2')).toBe(false)
  })
})
