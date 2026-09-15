import { describe, expect, it } from 'vitest'
import { savedRouterActivationSafe } from './savedRouterSafety'

describe('old Gateway Router activation evidence', () => {
  const tiers = { c0: { provider: 'openai' }, c3: { provider: 'other' } }
  it('rejects missing flags, missing final role entries, and foreign execution', () => {
    expect(savedRouterActivationSafe('openai', undefined, tiers, { c0: 'direct', c3: 'dormant_draft' }, false)).toBe(false)
    expect(savedRouterActivationSafe('openai', false, tiers, { c0: 'direct' }, false)).toBe(false)
    expect(savedRouterActivationSafe('openai', false, tiers, { c0: 'direct', c3: 'dynamic_member' }, false)).toBe(false)
  })
  it('accepts proven local execution and respects explicitly enabled cross-provider permission', () => {
    expect(savedRouterActivationSafe('openai', false, tiers, { c0: 'direct', c3: 'dormant_draft' }, false)).toBe(true)
    expect(savedRouterActivationSafe('openai', true, tiers, undefined, true)).toBe(true)
  })
  it('does not reuse global Ensemble dormant roles for a Router mode transition', () => {
    expect(savedRouterActivationSafe('openai', false, tiers, { c0: 'dormant_draft', c3: 'dormant_draft' }, true)).toBe(false)
  })
})
