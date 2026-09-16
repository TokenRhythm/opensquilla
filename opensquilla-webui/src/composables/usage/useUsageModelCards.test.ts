import { computed } from 'vue'
import { describe, expect, it } from 'vitest'

import { useUsageModelCards } from './useUsageModelCards'
import type { UsageSession } from '@/types/usage'
import { usageSession } from '@/testing/usage.test-helper'

function cardsFor(sessions: Partial<UsageSession>[]) {
  const { modelCards } = useUsageModelCards({
    visibleSessions: computed(() => sessions.map(usageSession)),
  })
  return modelCards.value
}

describe('useUsageModelCards', () => {
  it('aggregates a mixed cost source and flags cache-blind estimates from breakdown items', () => {
    const sessions: Partial<UsageSession>[] = [
      {
        session: 's1',
        modelBreakdown: [
          { model: 'm1', costUsd: 1, costSource: 'provider_billed' },
        ],
      },
      {
        session: 's2',
        modelBreakdown: [
          {
            model: 'm1',
            costUsd: 2,
            costSource: 'opensquilla_estimate',
            estimateBasis: 'cache_blind',
          },
        ],
      },
    ]

    const cards = cardsFor(sessions)

    expect(cards).toHaveLength(1)
    const [card] = cards
    expect(card.model).toBe('m1')
    expect(card.costUsd).toBeCloseTo(3)
    expect(card.costSource).toBe('mixed')
    expect(card.anyCacheBlind).toBe(true)
  })

  it('reports a single shared cost source (not mixed) when every item agrees', () => {
    const sessions: Partial<UsageSession>[] = [
      {
        session: 's1',
        modelBreakdown: [{ model: 'm1', costUsd: 1, costSource: 'provider_billed' }],
      },
      {
        session: 's2',
        modelBreakdown: [{ model: 'm1', costUsd: 2, costSource: 'provider_billed' }],
      },
    ]

    const [card] = cardsFor(sessions)

    expect(card.costSource).toBe('provider_billed')
    expect(card.anyCacheBlind).toBe(false)
  })

  it('carries the row costSource/estimateBasis into the synthetic fallback item when a session has no breakdown', () => {
    const sessions: Partial<UsageSession>[] = [
      {
        session: 'fallback-1',
        model: 'm2',
        costUsd: 5,
        costSource: 'provider_billed',
      },
      {
        session: 'fallback-2',
        model: 'm2',
        costUsd: 4,
        costSource: 'opensquilla_estimate',
        estimateBasis: 'cache_blind',
      },
    ]

    const [card] = cardsFor(sessions)

    expect(card.model).toBe('m2')
    expect(card.costUsd).toBeCloseTo(9)
    expect(card.costSource).toBe('mixed')
    expect(card.anyCacheBlind).toBe(true)
  })

  it('keeps a single costSource for an all-billed fallback session with no cache-blind estimate', () => {
    const sessions: Partial<UsageSession>[] = [
      {
        session: 'fallback-1',
        model: 'm3',
        costUsd: 5,
        costSource: 'provider_billed',
      },
    ]

    const [card] = cardsFor(sessions)

    expect(card.costSource).toBe('provider_billed')
    expect(card.anyCacheBlind).toBe(false)
  })
})
