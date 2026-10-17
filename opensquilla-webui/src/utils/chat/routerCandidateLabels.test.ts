import { describe, expect, it } from 'vitest'
import type { RouterCandidate } from './routerCandidates'
import { routerCandidateLabels } from './routerCandidateLabels'

function candidate(tier: string, model: string, provider?: string, executionKind: RouterCandidate['executionKind'] = 'single_model'): RouterCandidate {
  return { tier, tiers: [tier], model, ...(provider ? { provider } : {}), executionKind }
}

describe('routerCandidateLabels', () => {
  it('uses short names for unique labels without sorting candidates', () => {
    expect(routerCandidateLabels([
      candidate('c0', 'models/z-first'), candidate('c1', 'models/a-second'),
    ])).toEqual(['z-first', 'a-second'])
  })

  it('uses full model identifiers when different models share a short name', () => {
    expect(routerCandidateLabels([
      candidate('c0', 'namespace-z/shared'), candidate('c1', 'namespace-a/shared'),
    ])).toEqual(['namespace-z/shared', 'namespace-a/shared'])
  })

  it('retains a nonempty label for an opaque model identifier ending in a slash', () => {
    expect(routerCandidateLabels([candidate('c0', 'vendor/')])).toEqual(['vendor/'])
  })

  it('uses providers for identical models and tiers when an older provider is unknown', () => {
    expect(routerCandidateLabels([
      candidate('c0', 'shared-model', 'profile-z'),
      candidate('c1', 'shared-model', 'profile-a'),
      candidate('c2', 'shared-model'),
    ])).toEqual(['shared-model · profile-z', 'shared-model · profile-a', 'shared-model · C2'])
  })

  it('uses tiers to distinguish execution kinds within the same deployment without mutating candidates', () => {
    const candidates = [candidate('c0', 'shared-model', 'profile-a'), candidate('c3', 'shared-model', 'profile-a', 'ensemble')]
    const before = JSON.stringify(candidates)

    expect(routerCandidateLabels(candidates)).toEqual(['shared-model · profile-a · C0', 'shared-model · profile-a · C3'])
    expect(JSON.stringify(candidates)).toBe(before)
  })
})
