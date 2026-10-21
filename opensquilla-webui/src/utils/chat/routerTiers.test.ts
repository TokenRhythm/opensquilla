import { describe, expect, it } from 'vitest'
import { normalizeRouterTierMapping, sortRouterTiers } from './routerTiers'

describe('normalizeRouterTierMapping', () => {
  it.each([false, true])('prefers canonical keys independent of insertion order (alias first: %s)', aliasFirst => {
    const canonical = Object.freeze({ model: 'configured/primary' })
    const alias = Object.freeze({ model: 'configured/legacy' })
    const source = Object.freeze(Object.fromEntries(aliasFirst
      ? [['t0', alias], ['c0', canonical]]
      : [['c0', canonical], ['t0', alias]]))

    expect(normalizeRouterTierMapping(source)).toEqual({ c0: canonical })
    expect(Object.keys(source)).toHaveLength(2)
  })

  it('normalizes alias-only cached models and retains canonical tier sorting', () => {
    const source = Object.freeze({ t3: 'last', t1: 'middle', t0: 'first', t2: 'strong' })
    const normalized = normalizeRouterTierMapping(source)

    expect(normalized).toEqual({ c3: 'last', c1: 'middle', c0: 'first', c2: 'strong' })
    expect(sortRouterTiers(Object.keys(normalized))).toEqual(['c0', 'c1', 'c2', 'c3'])
    expect(normalized).not.toBe(source)
  })
})
