import { expect, expectTypeOf, it, vi } from 'vitest'

import { createV4RouteFeedback } from '../adapters/gateway/routeFeedbackV4'
import type { RouteFeedbackResult } from './publicData'

it('exposes only the reviewed data fields with their exact nullability', () => {
  expectTypeOf<RouteFeedbackResult>().toEqualTypeOf<{
    readonly accepted: boolean
    readonly reason?: string | null
    readonly recorded?: string | null
  }>()
  expectTypeOf<keyof RouteFeedbackResult>().toEqualTypeOf<'accepted' | 'reason' | 'recorded'>()
})

it('keeps the validated response and its nullable fields unchanged', async () => {
  const raw = { accepted: false, reason: null, recorded: null, futureField: true }
  const request = vi.fn().mockResolvedValue(raw)
  const feedback = createV4RouteFeedback({ request })

  expect(await feedback.submit('synthetic-decision', 'neutral')).toBe(raw)
  expect(request).toHaveBeenCalledExactlyOnceWith('router.feedback.submit', {
    decisionId: 'synthetic-decision', rating: 'neutral',
  })
})
