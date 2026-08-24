import { describe, expect, it } from 'vitest'

import { normalizeRouterTierSnapshot } from './routerTierSnapshot'

describe('normalizeRouterTierSnapshot', () => {
  it('normalizes legacy tier aliases in a complete v1 snapshot', () => {
    expect(normalizeRouterTierSnapshot({
      version: 1,
      request_kind: 'text',
      tiers: [{
        tier: 't1',
        provider: 'test-provider',
        model: 'test/model',
        execution_kind: 'ensemble',
      }],
    })).toEqual({
      version: 1,
      request_kind: 'text',
      tiers: [{
        tier: 'c1',
        provider: 'test-provider',
        model: 'test/model',
        execution_kind: 'ensemble',
      }],
    })
  })

  it.each([
    { version: 2, request_kind: 'text', tiers: [] },
    { version: 1, request_kind: 'audio', tiers: [] },
    {
      version: 1,
      request_kind: 'text',
      tiers: [{ tier: 'c0', model: '', execution_kind: 'single_model' }],
    },
    {
      version: 1,
      request_kind: 'text',
      tiers: [
        { tier: 'c0', model: 'one', execution_kind: 'single_model' },
        { tier: 't0', model: 'two', execution_kind: 'single_model' },
      ],
    },
    {
      version: 1,
      request_kind: 'text',
      tiers: [{ tier: 'c0', model: 'one', execution_kind: 'unknown' }],
    },
  ])('rejects the whole malformed snapshot %#', (snapshot) => {
    expect(normalizeRouterTierSnapshot(snapshot)).toBeNull()
  })
})
