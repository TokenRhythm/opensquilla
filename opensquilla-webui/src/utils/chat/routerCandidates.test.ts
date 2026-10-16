import { describe, expect, it } from 'vitest'
import { hasConfiguredRouterCandidates, resolveRouterCandidates } from './routerCandidates'

const configuration = {
  slots: ['c3', 'c1', 'c0', 'c2'],
  models: {},
  configs: {
    c0: { model: 'configured/z-first' },
    c1: { model: 'configured/a-second' },
    c2: { model: 'configured/m-third' },
    c3: { model: 'configured/b-fourth' },
  },
}

describe('resolveRouterCandidates', () => {
  it('preserves recorded snapshot order and winner identity independently of current configuration', () => {
    const snapshot = Object.freeze({
      version: 1, request_kind: 'image', tiers: Object.freeze([
        Object.freeze({ tier: 'c2', model: 'accepted/old-winner', provider: 'profile-a', execution_kind: 'ensemble' }),
        Object.freeze({ tier: 'c0', model: 'accepted/z-first', execution_kind: 'single_model' }),
        Object.freeze({ tier: 'c1', model: 'accepted/a-second', execution_kind: 'single_model' }),
      ]),
    })
    const result = resolveRouterCandidates({ ...configuration, tier: 't2', model: 'accepted/winner', snapshot })

    expect(result.source).toBe('snapshot')
    expect(result.candidates).toEqual([
      { tier: 'c2', tiers: ['c2'], model: 'accepted/winner', provider: 'profile-a', executionKind: 'ensemble' },
      { tier: 'c0', tiers: ['c0'], model: 'accepted/z-first', executionKind: 'single_model' },
      { tier: 'c1', tiers: ['c1'], model: 'accepted/a-second', executionKind: 'single_model' },
    ])
    expect(snapshot.tiers[0]?.model).toBe('accepted/old-winner')
  })

  it('merges matching deployments without merging providers or execution kinds', () => {
    const result = resolveRouterCandidates({
      ...configuration, tier: 'c2', model: 'shared-model', snapshot: {
        version: 1, request_kind: 'text', tiers: [
          { tier: 'c0', model: 'shared-model', provider: 'Profile-A', execution_kind: 'single_model' },
          { tier: 'c1', model: 'shared-model', provider: 'profile-b', execution_kind: 'single_model' },
          { tier: 'c2', model: 'shared-model', provider: 'profile-a', execution_kind: 'single_model' },
          { tier: 'c3', model: 'shared-model', provider: 'profile-a', execution_kind: 'ensemble' },
        ],
      },
    })

    expect(result.candidates.map(candidate => [candidate.provider, candidate.executionKind, candidate.tiers])).toEqual([
      ['Profile-A', 'single_model', ['c0', 'c2']],
      ['profile-b', 'single_model', ['c1']],
      ['profile-a', 'ensemble', ['c3']],
    ])
  })

  it('keeps an older unknown-provider entry distinct from a recorded deployment', () => {
    const result = resolveRouterCandidates({
      ...configuration, tier: 'c1', model: 'shared-model', snapshot: {
        version: 1, request_kind: 'text', tiers: [
          { tier: 'c0', model: 'shared-model', execution_kind: 'single_model' },
          { tier: 'c1', model: 'shared-model', provider: 'profile-a', execution_kind: 'single_model' },
        ],
      },
    })

    expect(result.candidates.map(candidate => [candidate.provider, candidate.tiers])).toEqual([
      [undefined, ['c0']], ['profile-a', ['c1']],
    ])
  })

  it('falls back atomically to canonical configuration order for an invalid snapshot', () => {
    const result = resolveRouterCandidates({
      ...configuration, tier: 'c2', model: 'accepted/winner', snapshot: {
        version: 1, request_kind: 'image', tiers: [
          { tier: 'c0', model: 'invalid/first', execution_kind: 'single_model' },
          { tier: 't0', model: 'invalid/duplicate', execution_kind: 'single_model' },
        ],
      },
    })

    expect(result.source).toBe('configuration')
    expect(result.candidates.map(candidate => candidate.model)).toEqual([
      'configured/z-first', 'configured/a-second', 'accepted/winner', 'configured/b-fourth',
    ])
  })

  it('resolves old alias maps with canonical model/configuration precedence', () => {
    const result = resolveRouterCandidates({
      tier: 't1', model: 'shared-model', slots: ['t3', 't1', 't0', 't2'],
      models: { c0: 'canonical/first', t0: 'legacy/first', t2: 'legacy/third', t3: 'legacy/fourth' },
      configs: {
        c1: { model: 'shared-model', provider: 'profile-a' },
        t1: { model: 'legacy/second', provider: 'profile-b' },
      },
    })

    expect(result.candidates.map(candidate => [candidate.tier, candidate.model, candidate.provider])).toEqual([
      ['c0', 'canonical/first', undefined],
      ['c1', 'shared-model', 'profile-a'],
      ['c2', 'legacy/third', undefined],
      ['c3', 'legacy/fourth', undefined],
    ])
  })

  it('keeps provider-specific configured models as separate candidates', () => {
    const result = resolveRouterCandidates({
      tier: 'c1', model: 'shared-model', slots: ['c1', 'c0'], models: {},
      configs: {
        c0: { model: 'shared-model', provider: 'profile-a' },
        c1: { model: 'shared-model', provider: 'profile-b' },
      },
    })

    expect(result.candidates.map(candidate => [candidate.provider, candidate.tiers])).toEqual([
      ['profile-a', ['c0']], ['profile-b', ['c1']],
    ])
  })

  it('includes image-only and legacy image candidates only when recorded as the winner', () => {
    const options = {
      slots: ['image_model', 'c1', 'c0'], models: {},
      configs: {
        c0: { model: 'configured/text' },
        c1: { model: 'configured/image-only', imageOnly: true },
        image_model: { model: 'legacy/vision' },
      },
    }
    expect(resolveRouterCandidates({ ...options, tier: 'c0', model: 'configured/text' }).candidates.map(candidate => candidate.tier))
      .toEqual(['c0'])
    expect(resolveRouterCandidates({ ...options, tier: 'image_model', model: 'legacy/vision' }).candidates.map(candidate => candidate.tier))
      .toEqual(['c0', 'image_model'])
    expect(resolveRouterCandidates({ ...options, tier: 'c1', model: 'configured/image-only' }).candidates.map(candidate => candidate.tier))
      .toEqual(['c0', 'c1'])
  })
})

describe('hasConfiguredRouterCandidates', () => {
  it('uses the same eligibility rules for legacy alias-only configuration', () => {
    expect(hasConfiguredRouterCandidates({
      slots: ['t0', 'image_model'], models: { t0: 'configured/text' }, configs: {},
    })).toBe(true)
    expect(hasConfiguredRouterCandidates({
      slots: [], models: {}, configs: {
        t0: { model: 'configured/image-only', imageOnly: true },
        image_model: { model: 'legacy/vision' },
      },
    })).toBe(false)
  })
})
